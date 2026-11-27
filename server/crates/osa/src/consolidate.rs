//! 记忆整理：每天按 `memory.consolidate_at`（默认 "03:00"，空着不整理）跑一次，也能在面板上立即跑、撤销最近一次。
//!
//! 一次整理：删掉过了截止日的；每个 Bot 的记忆、共享的记忆各问一次便宜的模型（filter 角色），让它返回一份
//! 操作列表（keep / rewrite / merge / delete，带编号），服务端照着做；置顶的不交给模型改；模型的回答解析不了
//! 就这一组不动。最后把这段时间做过的事写成一条每日摘要（kind=digest，共享，只留最近 KEEP_DIGESTS 条）。
//!
//! 改到的记忆先把整行原样存进 memories_history（每次整理一行“标记”，memory_id 为空，记着改了几条），
//! 撤销时照着放回去。只留最近 KEEP_RUNS 次。

use crate::db::{self, RowExt};
use crate::memories::{self, active_sql};
use crate::web::{Res, ok};
use crate::{bots, config, memory, providers, py, server};
use axum::routing::post;
use parking_lot::Mutex;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::LazyLock;

pub const KEEP_RUNS: i64 = 7;
pub const KEEP_DIGESTS: i64 = 14;
/// 一组少于这么多条就不调模型
pub const MIN_NOTES: usize = 4;
/// 模型要删的超过这一组的这么多（比例），当作答坏了，这一组不动
const MAX_DELETE_SHARE: f64 = 0.5;
/// 每日摘要最多列几件事
const DIGEST_ITEMS: usize = 20;
/// 错过了整理时间（服务没开着），多久以内开起来时补一次
const CATCH_UP: f64 = 20.0 * 3600.0;

const PROMPT: &str = include_str!("prompts/consolidate.txt");

/// 同一时间只跑一次整理（或撤销）
static LOCK: LazyLock<tokio::sync::Mutex<()>> = LazyLock::new(|| tokio::sync::Mutex::new(()));
/// 正在跑的整理做到哪了（面板轮询）
static PROGRESS: LazyLock<Mutex<Value>> = LazyLock::new(|| Mutex::new(json!({"running": false})));

pub fn migrate() {
    db::batch(
        "create table if not exists memories_history(
          id integer primary key, run integer, memory_id integer, row text, created real, ts real,
          changed integer default 0, undone integer default 0);
         create index if not exists memories_history_run on memories_history(run);",
    );
}

// ---------- 留底和撤销 ----------

fn columns() -> Vec<String> {
    db::columns("memories")
}

/// 整行（列名 → 值）
fn row_json(mid: i64) -> Option<Value> {
    let cols = columns();
    let r = db::row(&format!("select {} from memories where id=?", cols.join(", ")), params![mid])?;
    Some(Value::Object(cols.into_iter().zip(r).collect()))
}

struct Run {
    id: i64,
    seen: HashSet<i64>,
    changed: usize,
}

impl Run {
    fn start() -> Run {
        let id = db::scalar_i64("select coalesce(max(run), 0) + 1 from memories_history", []);
        Run { id, seen: HashSet::new(), changed: 0 }
    }

    /// 改、删之前存一份原样（同一次里只存第一次的）
    fn before(&mut self, mid: i64) {
        if !self.seen.insert(mid) {
            return;
        }
        if let Some(r) = row_json(mid) {
            db::exec(
                "insert into memories_history(run, memory_id, row, ts) values(?,?,?,?)",
                params![self.id, mid, py::dumps_u(&r), py::now()],
            );
        }
    }

    /// 新加的一行：撤销时删掉（created 用来认出还是不是它）
    fn added(&mut self, mid: i64) {
        self.seen.insert(mid);
        let created = db::scalar("select created from memories where id=?", params![mid]);
        db::exec(
            "insert into memories_history(run, memory_id, row, created, ts) values(?,?,null,?,?)",
            params![self.id, mid, created.as_f64(), py::now()],
        );
    }

    fn delete(&mut self, mid: i64) {
        self.before(mid);
        memories::delete(mid);
    }

    fn finish(self) -> (i64, usize) {
        db::exec(
            "insert into memories_history(run, memory_id, row, ts, changed) values(?,null,null,?,?)",
            params![self.id, py::now(), self.changed as i64],
        );
        db::exec(
            "delete from memories_history where run not in
             (select distinct run from memories_history order by run desc limit ?)",
            params![KEEP_RUNS],
        );
        (self.id, self.changed)
    }
}

/// 把一行放回去。原来的编号已经被别的记忆占了（created 不一样），就换个新编号放
fn put_back(row: &Value) {
    let Some(obj) = row.as_object() else { return };
    let cols: Vec<String> = columns().into_iter().filter(|c| obj.contains_key(c)).collect();
    let mid = obj.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
    let now_created = db::scalar("select created from memories where id=?", params![mid]);
    let same = now_created.is_null() || now_created.as_f64() == obj.get("created").and_then(|v| v.as_f64());
    let cols: Vec<&String> = cols.iter().filter(|c| same || c.as_str() != "id").collect();
    if same {
        memories::delete(mid);
    }
    let vals: Vec<Value> = cols.iter().map(|c| obj[c.as_str()].clone()).collect();
    let marks = vec!["?"; cols.len()].join(",");
    db::exec(
        &format!("insert into memories({}) values({marks})", cols.iter().map(|c| c.as_str()).collect::<Vec<_>>().join(", ")),
        rusqlite::params_from_iter(vals.iter().map(db::J)),
    );
}

/// 撤销最近一次（还没撤销过的）整理。返回 (那次的编号, 放回了几条)
pub async fn undo() -> Result<(i64, usize), String> {
    let _g = LOCK.try_lock().map_err(|_| t!("正在整理记忆，等它做完再撤销"))?;
    let run = db::scalar("select max(run) from memories_history where undone=0", []);
    let Some(run) = run.as_i64() else {
        return Err(t!("没有可以撤销的整理"));
    };
    let rows = db::rows(
        "select memory_id, row, created from memories_history where run=? and memory_id is not null order by id desc",
        params![run],
    );
    for r in &rows {
        let mid = r.i(0);
        if r[1].is_null() {
            let c = db::scalar("select created from memories where id=?", params![mid]);
            if !c.is_null() && c.as_f64() == r[2].as_f64() {
                memories::delete(mid);
            }
        } else if let Ok(v) = serde_json::from_str::<Value>(&r.s(1)) {
            put_back(&v);
        }
    }
    db::exec("update memories_history set undone=1 where run=?", params![run]);
    server::broadcast(json!({"type": "memories_changed"}));
    Ok((run, rows.len()))
}

// ---------- 整理 ----------

/// 一组：一个 Bot 自己的、共享的（两个都是 project_id 为空），或者一个项目的
#[derive(Clone, Copy, Debug)]
enum Group {
    Bot(i64),
    Shared,
    Project(i64),
}

fn notes(g: Group, pinned: bool) -> Vec<Value> {
    let (wh, id) = match g {
        Group::Bot(b) => ("bot_id=? and project_id is null", Some(b)),
        Group::Shared => ("bot_id is ? and project_id is null", None),
        Group::Project(p) => ("project_id=?", Some(p)),
    };
    db::rows(
        &format!("select id, text, updated, until from memories where {wh} and kind='note' and pinned=? and {} order by id", active_sql()),
        params![id, pinned as i64],
    )
    .iter()
    .map(|r| json!({"id": r[0], "text": r[1], "updated": r[2], "until": r[3]}))
    .collect()
}

fn listing(items: &[Value]) -> String {
    items
        .iter()
        .map(|m| {
            let until = m["until"].as_f64().map(|u| format!("，有效到 {}", memories::until_day(u))).unwrap_or_default();
            format!(
                "- #{} {}（更新于 {}{until}）",
                py::str_of(&m["id"]),
                py::str_of(&m["text"]),
                py::strftime_at(m["updated"].as_f64().unwrap_or(0.0), "%Y-%m-%d")
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// 模型的回答 → 操作列表。不是 {"ops": [...]} 就算解析不了
pub fn parse_ops(content: &str) -> Option<Vec<Value>> {
    let s = content.trim();
    let s = s.strip_prefix("```json").or_else(|| s.strip_prefix("```")).unwrap_or(s);
    let s = s.strip_suffix("```").unwrap_or(s).trim();
    let v: Value = serde_json::from_str(s).ok()?;
    let ops = v.get("ops")?.as_array()?;
    if ops.iter().any(|o| !o.is_object() || !o["op"].is_string()) {
        return None;
    }
    Some(ops.clone())
}

fn id_of(v: &Value) -> Option<i64> {
    py::as_int(v)
}

/// 照操作列表改这一组，返回改了几条。只认这一组里没置顶的编号；一条只做第一次提到它的操作
fn apply(run: &mut Run, group: &[Value], ops: &[Value]) -> usize {
    let ids: HashSet<i64> = group.iter().filter_map(|m| m["id"].as_i64()).collect();
    let deletes: HashSet<i64> =
        ops.iter().filter(|o| o["op"] == "delete").filter_map(|o| id_of(&o["id"])).filter(|i| ids.contains(i)).collect();
    if deletes.len() as f64 > ids.len() as f64 * MAX_DELETE_SHARE {
        tracing::warn!("整理记忆：模型要删 {}/{} 条，太多了，这一组不动", deletes.len(), ids.len());
        return 0;
    }
    let mut used: HashSet<i64> = HashSet::new();
    let mut changed = 0;
    let now = py::now();
    for o in ops {
        match o["op"].as_str().unwrap_or("") {
            "rewrite" => {
                let Some(id) = id_of(&o["id"]).filter(|i| ids.contains(i) && !used.contains(i)) else { continue };
                let Ok(text) = memories::clean(&py::str_of(&o["text"])) else { continue };
                used.insert(id);
                let old = group.iter().find(|m| m["id"].as_i64() == Some(id)).map(|m| py::str_of(&m["text"]));
                if old.as_deref() == Some(text.as_str()) {
                    continue;
                }
                run.before(id);
                db::exec("update memories set text=?, updated=? where id=?", params![text, now, id]);
                changed += 1;
            }
            "merge" => {
                let mut list: Vec<i64> = o["ids"].as_array().map(|a| a.iter().filter_map(id_of).collect()).unwrap_or_default();
                list.retain(|i| ids.contains(i) && !used.contains(i));
                list.sort();
                list.dedup();
                if list.is_empty() {
                    continue;
                }
                let Ok(text) = memories::clean(&py::str_of(&o["text"])) else { continue };
                let keep = list[0];
                let members: Vec<&Value> = group.iter().filter(|m| m["id"].as_i64().is_some_and(|i| list.contains(&i))).collect();
                // 合并后的截止日：有一条不过期就不过期，否则取最晚的
                let until = if members.iter().any(|m| m["until"].is_null()) {
                    None
                } else {
                    members.iter().filter_map(|m| m["until"].as_f64()).reduce(f64::max)
                };
                for i in &list {
                    used.insert(*i);
                    run.before(*i);
                }
                db::exec("update memories set text=?, updated=?, until=? where id=?", params![text, now, until, keep]);
                for i in &list[1..] {
                    memories::delete(*i);
                }
                changed += list.len();
            }
            "delete" => {
                let Some(id) = id_of(&o["id"]).filter(|i| ids.contains(i) && !used.contains(i)) else { continue };
                used.insert(id);
                run.delete(id);
                changed += 1;
            }
            _ => {
                if let Some(id) = id_of(&o["id"]) {
                    used.insert(id);
                }
            }
        }
    }
    changed
}

/// 一组（一个 Bot 的、共享的，或者一个项目的）：问一次模型，照着改
async fn group(run: &mut Run, g: Group) -> usize {
    let items = notes(g, false);
    if items.len() < MIN_NOTES {
        return 0;
    }
    let pinned = notes(g, true);
    let whose = match g {
        Group::Shared => "所有 Bot 共享的用户事实".to_string(),
        Group::Bot(b) => format!("Bot「{}」自己的记忆", bots::get(b).map(|x| py::str_of(&x["name"])).unwrap_or_default()),
        Group::Project(p) => format!("项目 {}的记忆", crate::board::project_label(p).unwrap_or_default()),
    };
    let bot_id = match g {
        Group::Bot(b) => Some(b),
        _ => None,
    };
    let mut user = format!("今天是 {}。下面是{whose}：\n{}", py::strftime("%Y-%m-%d"), listing(&items));
    if !pinned.is_empty() {
        user.push_str(&format!("\n\n置顶的（不要动，只供参考）：\n{}", listing(&pinned)));
    }
    let msgs = json!([{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]);
    let who = memory::Who { bot_id, ..Default::default() };
    let resp = memory::WHO.scope(who, providers::complete_role("filter", &msgs, Some(json!({"type": "json_object"})))).await;
    let content = match resp {
        Ok(r) => r.content,
        Err(e) => {
            tracing::warn!("整理记忆：{whose} 调模型失败：{}", providers::brief(&e));
            return 0;
        }
    };
    let Some(ops) = parse_ops(&content) else {
        tracing::warn!("整理记忆：{whose} 模型的回答解析不了，这一组不动");
        return 0;
    };
    apply(run, &items, &ops)
}

fn status_label(s: &str) -> String {
    match s {
        "done" => t!("完成"),
        "failed" => t!("失败"),
        "cancelled" => t!("已取消"),
        "interrupted" => t!("已中断"),
        "running" => t!("还在做"),
        other => other.to_string(),
    }
}

/// 每日摘要：上一条摘要以来（最多一天）做过的事和结束的任务。没做事返回 None
fn digest_text(since: f64, now: f64) -> Option<String> {
    static TASK_NO: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"#(\d+)").unwrap());
    let mut items: Vec<(f64, String)> = vec![];
    let mut seen: HashSet<i64> = HashSet::new();
    let results = db::rows(
        "select m.ts, json_extract(m.meta, '$.task_id'), json_extract(m.meta, '$.status'), d.content, coalesce(d.bot_id, m.bot_id)
         from messages m left join messages d on d.id = json_extract(m.meta, '$.task_id')
         where m.role='task' and json_extract(m.meta, '$.kind')='result' and m.ts > ? and m.ts <= ? order by m.id",
        params![since, now],
    );
    for r in &results {
        let tid = r[1].as_i64().unwrap_or(0);
        if !seen.insert(tid) {
            continue;
        }
        let goal = r.s(3);
        let goal = goal.strip_prefix("[任务已派发]").unwrap_or(&goal);
        let goal = py::head(goal.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or(""), 60);
        if goal.is_empty() {
            continue;
        }
        let bot = r[4].as_i64().and_then(bots::get).map(|b| py::str_of(&b["name"])).unwrap_or_default();
        let st = r[2].as_str().map(status_label).unwrap_or_else(|| status_label("done"));
        items.push((r.f(0), format!("- {bot}：#{tid} {goal}（{st}）")));
    }
    // kind=work 是做完的任务自动记的一笔；上面已经有的（同一个任务编号）不再列
    for r in db::rows(
        "select created, text, bot_id from memories where kind='work' and created > ? and created <= ? order by id",
        params![since, now],
    ) {
        let text = r.s(1);
        if let Some(c) = TASK_NO.captures(&text)
            && let Ok(n) = c[1].parse::<i64>()
            && !seen.insert(n)
        {
            continue;
        }
        let bot = r[2].as_i64().and_then(bots::get).map(|b| py::str_of(&b["name"])).unwrap_or_default();
        items.push((r.f(0), format!("- {bot}：{}", py::head(&text, 80))));
    }
    if items.is_empty() {
        return None;
    }
    items.sort_by(|a, b| a.0.total_cmp(&b.0));
    let (first, last) = (py::strftime_at(items[0].0, "%m-%d"), py::strftime_at(items[items.len() - 1].0, "%m-%d"));
    let date = if first == last { first } else { format!("{first} – {last}") };
    let n = items.len();
    let mut lines: Vec<String> = items.into_iter().take(DIGEST_ITEMS).map(|(_, l)| l).collect();
    if n > DIGEST_ITEMS {
        lines.push(t!("……还有 {n} 件", n = n - DIGEST_ITEMS));
    }
    Some(format!("{}\n{}", t!("{date} 做过的事（{n} 件）：", date = date, n = n), lines.join("\n")))
}

fn write_digest(run: &mut Run, now: f64) -> bool {
    let last = db::scalar("select max(created) from memories where bot_id is null and kind='digest'", []);
    let since = last.as_f64().unwrap_or(0.0).max(now - 86400.0);
    let Some(text) = digest_text(since, now) else { return false };
    let mid = db::insert(
        "insert into memories(bot_id, kind, text, source, created, updated) values(null, 'digest', ?, 'digest', ?, ?)",
        params![text, now, now],
    );
    run.added(mid);
    for r in db::rows(
        "select id from memories where bot_id is null and kind='digest' and id not in
         (select id from memories where bot_id is null and kind='digest' order by id desc limit ?)",
        params![KEEP_DIGESTS],
    ) {
        run.delete(r.i(0));
    }
    true
}

fn progress(done: usize, total: usize) {
    *PROGRESS.lock() = json!({"running": true, "done": done, "total": total});
}

/// 整理一次。返回 (这次的编号, 改了几条, 写没写每日摘要)
pub async fn run_once() -> Result<(i64, usize, bool), String> {
    let _g = LOCK.try_lock().map_err(|_| t!("正在整理记忆，稍等"))?;
    let now = py::now();
    let mut run = Run::start();
    let mut groups: Vec<Group> = db::rows(
        &format!(
            "select distinct bot_id from memories where kind='note' and bot_id is not null and project_id is null and pinned=0 and {} order by bot_id",
            active_sql()
        ),
        [],
    )
    .iter()
    .filter_map(|r| r[0].as_i64())
    .filter(|b| bots::get(*b).is_some())
    .map(Group::Bot)
    .chain(std::iter::once(Group::Shared))
    .collect();
    // 每个项目的记忆单独一组
    groups.extend(
        db::rows(
            &format!("select distinct project_id from memories where kind='note' and project_id is not null and pinned=0 and {} order by project_id", active_sql()),
            [],
        )
        .iter()
        .filter_map(|r| r[0].as_i64())
        .filter(|p| crate::board::project_label(*p).is_some())
        .map(Group::Project),
    );
    let total = groups.len() + 2;
    progress(0, total);
    // 过了截止日的（置顶的不算）
    for r in db::rows("select id from memories where kind='note' and pinned=0 and until is not null and until <= ?", params![now]) {
        run.delete(r.i(0));
        run.changed += 1;
    }
    progress(1, total);
    for (i, b) in groups.iter().enumerate() {
        run.changed += group(&mut run, *b).await;
        progress(i + 2, total);
    }
    let digest = write_digest(&mut run, now);
    *PROGRESS.lock() = json!({"running": false});
    let (id, changed) = run.finish();
    server::broadcast(json!({"type": "memories_changed"}));
    Ok((id, changed, digest))
}

/// 最近一次整理：{run, ts, changed, undone}（撤销过的 undone 是 true，面板上就不再给“撤销”）
fn last_run() -> Value {
    db::row("select run, ts, changed, undone from memories_history where memory_id is null order by run desc limit 1", [])
        .map(|r| json!({"run": r[0], "ts": r[1], "changed": r[2], "undone": py::truthy(&r[3])}))
        .unwrap_or(Value::Null)
}

// ---------- 每天定时 ----------

/// 今天几点整理（本地时间的时间戳）。没设或设成空就不整理
fn scheduled_today() -> Option<f64> {
    use chrono::TimeZone;
    let at = match config::get("memory.consolidate_at") {
        Value::Null => "03:00".to_string(),
        v => py::str_of(&v).trim().to_string(),
    };
    if at.is_empty() {
        return None;
    }
    let t = chrono::NaiveTime::parse_from_str(&at, "%H:%M").ok()?;
    let dt = chrono::Local::now().date_naive().and_time(t);
    chrono::Local.from_local_datetime(&dt).earliest().map(|x| x.timestamp() as f64)
}

/// 该不该跑：上一次检查到现在跨过了整理时间；或者以前整理过、错过了今天的（服务没开着），在 CATCH_UP 以内补上
pub fn due(sched: f64, now: f64, last_check: f64, last_run: Option<f64>) -> bool {
    if now < sched {
        return false;
    }
    if last_run.is_some_and(|r| r >= sched) {
        return false;
    }
    last_check < sched || (last_run.is_some() && now - sched < CATCH_UP)
}

pub async fn schedule_loop() {
    let mut last_check = py::now();
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
        let now = py::now();
        if let Some(sched) = scheduled_today() {
            let last = db::scalar("select max(ts) from memories_history where memory_id is null", []).as_f64();
            if due(sched, now, last_check, last) {
                match run_once().await {
                    Ok((run, n, _)) => tracing::info!("整理记忆：第 {run} 次，改了 {n} 条"),
                    Err(e) => tracing::warn!("整理记忆没跑：{e}"),
                }
            }
        }
        last_check = now;
    }
}

// ---------- 接口 ----------

async fn api_run() -> Res {
    let (run, changed, digest) = run_once().await?;
    ok(json!({"ok": true, "changed": changed, "run": run, "digest": digest}))
}

async fn api_undo() -> Res {
    let (run, restored) = undo().await?;
    ok(json!({"ok": true, "run": run, "restored": restored}))
}

async fn api_status() -> Res {
    let mut p = PROGRESS.lock().clone();
    p["last"] = last_run();
    ok(p)
}

pub fn routes() -> axum::Router {
    axum::Router::new()
        .route("/api/memories/consolidate", post(api_run).get(api_status))
        .route("/api/memories/consolidate/undo", post(api_undo))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn due_rules() {
        let s = 1000.0;
        assert!(!due(s, 999.0, 900.0, None)); // 还没到
        assert!(due(s, 1001.0, 990.0, None)); // 跨过了
        assert!(!due(s, 5000.0, 4000.0, None)); // 新装的、开起来时已经过了：等明天
        assert!(due(s, 5000.0, 4000.0, Some(10.0))); // 以前整理过：补上
        assert!(!due(s, 5000.0, 990.0, Some(1500.0))); // 今天已经整理过
        assert!(!due(s, s + CATCH_UP + 1.0, s + CATCH_UP, Some(10.0))); // 错过太久
    }

    #[test]
    fn ops_parse() {
        assert!(parse_ops("不是 JSON").is_none());
        assert!(parse_ops(r#"{"items": []}"#).is_none());
        assert!(parse_ops(r#"{"ops": [1]}"#).is_none());
        assert_eq!(parse_ops("```json\n{\"ops\": [{\"op\": \"keep\", \"id\": 1}]}\n```").unwrap().len(), 1);
    }
}
