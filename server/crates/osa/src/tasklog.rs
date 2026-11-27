//! 任务过程：后台任务每一步想了什么、调了什么工具、拿到了什么。边做边推给前端，做完存着。对应 osa/tasklog.py。

use crate::db::{self, RowExt};
use crate::task::Task;
use crate::{config, py, server};
use parking_lot::Mutex;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::{Arc, LazyLock};

pub const KEYS: [&str; 10] = ["id", "task_id", "ts", "worker", "kind", "title", "body", "status", "dur", "detail"];
pub const FLUSH_S: f64 = 0.3;

fn limit(kind: &str) -> usize {
    match kind {
        "think" => 20000,
        "say" => 4000,
        "tool" => 6000,
        "note" => 2000,
        _ => 60000,
    }
}

fn open(status: &str) -> bool {
    status == "running" || status == "waiting"
}

/// 一步
#[derive(Clone, Debug)]
pub struct Step {
    pub id: i64,
    pub task_id: i64,
    pub ts: f64,
    pub worker: String,
    pub kind: String,
    pub title: String,
    pub body: String,
    pub status: String,
    pub dur: f64,
    pub detail: String,
    pub sent: usize,
    pub t: f64,
    pub chat_id: i64,
    pub bot_id: i64,
}

pub type StepRef = Arc<Mutex<Step>>;

pub static LIVE: LazyLock<Mutex<BTreeMap<i64, StepRef>>> = LazyLock::new(Default::default);

pub fn public(s: &Step) -> Value {
    let body = if open(&s.status) { s.body[..s.sent.min(s.body.len())].to_string() } else { s.body.clone() };
    json!({"id": s.id, "task_id": s.task_id, "ts": s.ts, "worker": s.worker, "kind": s.kind, "title": s.title,
           "body": body, "status": s.status, "dur": s.dur, "detail": s.detail})
}

fn save(s: &Step) {
    db::exec(
        "update task_steps set kind=?, title=?, body=?, status=?, dur=? where id=?",
        params![s.kind, s.title, s.body, s.status, s.dur, s.id],
    );
}

/// 一个 Worker 的过程记录
#[derive(Clone)]
pub struct Log {
    pub task_id: i64,
    pub chat_id: i64,
    pub bot_id: i64,
    pub worker: String,
}

impl Log {
    pub fn new(task: &Task, worker: &str) -> Log {
        Log { task_id: task.id, chat_id: task.chat_id, bot_id: task.bot_id, worker: worker.into() }
    }

    fn emit(&self, mut ev: Value) {
        let mut base = json!({"type": "task_step", "chat_id": self.chat_id, "bot_id": self.bot_id, "task_id": self.task_id});
        if let (Some(b), Some(e)) = (base.as_object_mut(), ev.as_object_mut()) {
            for (k, v) in std::mem::take(e) {
                b.insert(k, v);
            }
        }
        server::broadcast(base);
    }

    pub fn start(&self, kind: &str, title: &str, detail: &str, status: &str) -> StepRef {
        let now = py::now();
        let sid = db::insert(
            "insert into task_steps(task_id, ts, worker, kind, title, body, status, dur, detail, name) values(?,?,?,?,?,?,?,?,?,?)",
            params![self.task_id, now, self.worker, kind, title, "", status, 0, detail, if kind == "tool" { title } else { "" }],
        );
        let s = Step {
            id: sid,
            task_id: self.task_id,
            ts: now,
            worker: self.worker.clone(),
            kind: kind.into(),
            title: title.into(),
            body: String::new(),
            status: status.into(),
            dur: 0.0,
            detail: detail.into(),
            sent: 0,
            t: now,
            chat_id: self.chat_id,
            bot_id: self.bot_id,
        };
        let p = public(&s);
        let r = Arc::new(Mutex::new(s));
        if open(status) {
            LIVE.lock().insert(sid, r.clone());
        }
        self.emit(json!({"step": p}));
        r
    }

    pub fn note(&self, text: &str, status: &str) -> StepRef {
        self.start("note", &py::head(text, limit("note")), "", status)
    }

    /// 改标题、状态
    pub fn update(&self, s: &StepRef, title: Option<&str>, status: Option<&str>) {
        let p = {
            let mut s = s.lock();
            if let Some(t) = title {
                s.title = t.into();
            }
            if let Some(st) = status {
                s.status = st.into();
            }
            db::exec("update task_steps set title=?, status=? where id=?", params![s.title, s.status, s.id]);
            public(&s)
        };
        self.emit(json!({"step": p}));
    }

    pub fn append(&self, s: &StepRef, text: &str) {
        let ev = {
            let mut s = s.lock();
            s.body.push_str(text);
            let now = py::now();
            if now - s.t >= FLUSH_S {
                let at = s.sent;
                s.sent = s.body.len();
                s.t = now;
                // at 是字数（和 Python 版一样按字符算）
                Some(json!({"id": s.id, "at": s.body[..at].chars().count(), "append": s.body[at..]}))
            } else {
                None
            }
        };
        if let Some(e) = ev {
            self.emit(e);
        }
    }

    pub fn finish(&self, s: &StepRef, status: &str, body: Option<&str>, kind: Option<&str>) {
        let p = {
            let mut s = s.lock();
            if !open(&s.status) {
                return;
            }
            if let Some(b) = body {
                s.body = b.into();
            }
            if let Some(k) = kind {
                s.kind = k.into();
            }
            s.status = status.into();
            s.dur = py::round_n(py::now() - s.ts, 1);
            let lim = limit(&s.kind);
            if py::char_len(&s.body) > lim {
                s.body = py::head(&s.body, lim);
            }
            save(&s);
            LIVE.lock().remove(&s.id);
            public(&s)
        };
        self.emit(json!({"step": p}));
    }
}

pub fn utf16_len(s: &str) -> usize {
    s.encode_utf16().count()
}

/// 任务结束：还没做完的步骤跟着结束；失败、取消的话记一笔原因
pub fn close(task: &Task, status: &str, text: &str) {
    let open_steps: Vec<StepRef> = LIVE.lock().values().filter(|s| s.lock().task_id == task.id).cloned().collect();
    for s in open_steps {
        let w = s.lock().worker.clone();
        Log::new(task, &w).finish(&s, if status == "cancelled" { "cancelled" } else { "error" }, None, None);
    }
    if !text.is_empty() {
        Log::new(task, "").note(text, if status == "failed" { "error" } else { status });
    }
}

/// 还开着的步骤（想到一半、工具调到一半）都结束掉：重试这一步、从上一步重来之前用
pub fn end_open(task: &Task, status: &str) {
    let open_steps: Vec<StepRef> = LIVE.lock().values().filter(|s| s.lock().task_id == task.id).cloned().collect();
    for s in open_steps {
        let w = s.lock().worker.clone();
        Log::new(task, &w).finish(&s, status, None, None);
    }
}

pub fn steps(task_id: i64) -> Vec<Value> {
    let live = LIVE.lock().clone();
    db::rows(&format!("select {} from task_steps where task_id=? order by id", KEYS.join(", ")), params![task_id])
        .into_iter()
        .map(|r| match live.get(&r.i(0)) {
            Some(s) => public(&s.lock()),
            None => {
                let mut o = serde_json::Map::new();
                for (k, v) in KEYS.iter().zip(r) {
                    o.insert(k.to_string(), v);
                }
                Value::Object(o)
            }
        })
        .collect()
}

// ---------- 任务列表 ----------
/// 列表、详情共用的列：messages 的 id, ts, content, meta, chat_id, bot_id，再加 task_runs 的轮次、存没存过程
const COLS: &str = "id, ts, content, meta, chat_id, bot_id, (select round from task_runs where task_id=messages.id),
    (select msgs is not null from task_runs where task_id=messages.id)";

fn task_out(r: &[Value]) -> Value {
    let r = r.to_vec();
    let m = r.j(3);
    let c = r.s(2);
    let goal = c.strip_prefix("[任务已派发]").unwrap_or(&c).trim().to_string();
    let status = m.get("status").cloned().unwrap_or(json!("done"));
    let waiting = if status == "running" { r[0].as_i64().and_then(crate::router::task).and_then(|t| t.waiting_kind()) } else { None };
    json!({"id": r[0], "ts": r[1], "goal": goal, "mode": m.get("mode").cloned().unwrap_or(json!("single")),
           "status": status, "elapsed": m.get("elapsed").cloned().unwrap_or(Value::Null),
           "steps": m.get("steps").cloned().unwrap_or(Value::Null), "chat_id": r[4], "bot_id": r[5],
           "handoff_to": m.get("handoff_to").cloned().unwrap_or(Value::Null),
           "waiting_kind": waiting, "can_continue": r.get(7).is_some_and(py::truthy),
           "round": r.get(6).and_then(|v| v.as_i64()).unwrap_or(1), "need": r[0].as_i64().map(crate::board::need_of_task).unwrap_or(Value::Null)})
}

pub const DISPATCH: &str = "role='task' and (json_extract(meta, '$.kind')='dispatch' or content like '[任务已派发]%')";

pub fn list_tasks(bot_id: Option<i64>, chat_id: Option<i64>, before: Option<i64>, limit: i64) -> Vec<Value> {
    let mut wh = vec![DISPATCH.to_string(), "id < ?".into()];
    let mut args: Vec<Value> = vec![json!(before.filter(|b| *b != 0).unwrap_or(crate::memory::BIG))];
    if let Some(b) = bot_id.filter(|b| *b != 0) {
        wh.push("bot_id=?".into());
        args.push(json!(b));
    }
    if let Some(c) = chat_id.filter(|c| *c != 0) {
        wh.push("chat_id in (select ? union select id from chats where kind='thread' and parent=?)".into());
        args.push(json!(c));
        args.push(json!(c));
    }
    args.push(json!(limit));
    let sql = format!("select {COLS} from messages where {} order by id desc limit ?", wh.join(" and "));
    db::rows(&sql, rusqlite::params_from_iter(args.iter().map(db::J))).iter().map(|r| task_out(r)).collect()
}

pub fn detail(task_id: i64) -> Option<Value> {
    let r = db::row(&format!("select {COLS} from messages where id=? and {DISPATCH}"), params![task_id])?;
    let mut t = task_out(&r);
    // 接着做过的任务有几条结果，取最新的；在讨论串里回复过的，结果在讨论串里
    let res = db::row(
        "select content, meta, ts from messages where id>? and role='task'
        and json_extract(meta, '$.task_id')=? and json_extract(meta, '$.kind')='result' order by id desc limit 1",
        params![task_id, task_id],
    );
    if let Some(res) = res {
        static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\[任务 #\d+ 结果\]\n?").unwrap());
        t["result"] = json!(RE.replace(&res.s(0), "").into_owned());
        t["finished"] = res[2].clone();
        if !py::truthy(&t["elapsed"]) {
            t["elapsed"] = res.j(1).get("elapsed").cloned().unwrap_or(Value::Null);
        }
    }
    let c = db::row(
        "select coalesce(sum(cost), 0), coalesce(sum(cache_hit+cache_miss+output), 0) from llm_calls where task_id=?",
        params![task_id],
    )
    .unwrap_or_default();
    t["cost"] = json!(py::round_n(c.f(0), 4));
    t["tokens"] = c[1].clone();
    t["currency"] = config::get("pricing.currency");
    Some(t)
}

pub fn startup() {
    // 重启前在做、存着过程、最近动过的任务等应用和聊天软件都起好以后接着做（taskstore::resume），别的标成中断
    let keep = crate::taskstore::resumable();
    db::exec("update task_steps set status='interrupted' where status in ('running', 'waiting')", []);
    db::exec(
        &format!(
            "update messages set meta=json_set(meta, '$.status', 'interrupted') where {DISPATCH} and json_extract(meta, '$.status')='running'
            and id not in (select value from json_each(?))"
        ),
        params![py::dumps_u(&json!(keep))],
    );
    crate::taskstore::hold(keep);
    prune();
}

pub fn prune() {
    let days = match config::get("memory.task_log_days") {
        Value::Null => 90.0,
        v => v.as_f64().unwrap_or(90.0),
    };
    if days != 0.0 {
        db::exec("delete from task_steps where ts < ?", params![py::now() - days * 86400.0]);
    }
}

pub async fn prune_loop() {
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(86400)).await;
        prune();
    }
}
