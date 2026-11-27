//! 会话记忆：SQLite 持久化。对应 osa/memory.py。
//!
//! Inline & Spill：最近 N 条消息直接放进上下文，更早的只留 ID + 摘要行；Chain of Recall：模型可用 recall(id) 按需取回原文。

use crate::db::{self, J, RowExt};
use crate::py::{self, dumps_u};
use regex::Regex;
use rusqlite::params;
use serde_json::{Map, Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

pub const BIG: i64 = 1 << 62;

pub fn migrate() {
    db::batch(
        "create table if not exists messages(id integer primary key, ts real, role text, content text, meta text);
         create table if not exists bots(id integer primary key, name text, avatar text, instructions text default '', is_primary integer default 0, created real);
         create table if not exists chats(id integer primary key, kind text, title text, bot_ids text, pinned integer default 0, updated real);
         create table if not exists settings(key text primary key, value text);",
    );
    if !db::columns("messages").iter().any(|c| c == "chat_id") {
        db::batch("alter table messages add column chat_id integer; alter table messages add column bot_id integer;");
    }
    db::batch(
        "create index if not exists messages_chat on messages(chat_id, id);
         create table if not exists llm_usage(ts real, role text, model text, task_id integer,
           cache_hit integer, cache_miss integer, output integer, cost real);
         create index if not exists llm_usage_task on llm_usage(task_id);
         create table if not exists llm_calls(id integer primary key, ts real, role text, model text, task_id integer,
           cache_hit integer default 0, cache_miss integer default 0, output integer default 0, cost real default 0,
           bot_id integer, chat_id integer, provider text, ms integer, ttft integer, err text);
         create index if not exists llm_calls_ts on llm_calls(ts);
         create index if not exists llm_calls_task on llm_calls(task_id);",
    );
    move_legacy_usage();
    db::batch(
        "create table if not exists task_steps(
           id integer primary key, task_id integer, ts real, worker text default '', kind text, title text default '',
           body text default '', status text default 'done', dur real default 0, detail text default '');
         create index if not exists task_steps_task on task_steps(task_id, id);",
    );
    if db::add_column("task_steps", "name", "text default ''") {
        db::exec("update task_steps set name=title where kind='tool'", []);
    }
}

fn move_legacy_usage() {
    let done = setting("llm_usage_moved").and_then(|v| v.as_f64()).unwrap_or(-1.0);
    let last = db::scalar("select max(ts) from llm_usage where ts > ?", params![done]);
    let Some(last) = last.as_f64() else { return };
    db::exec(
        "insert into llm_calls(ts, role, model, task_id, cache_hit, cache_miss, output, cost, bot_id, chat_id)
        select u.ts, u.role, u.model, u.task_id, u.cache_hit, u.cache_miss, u.output, u.cost, m.bot_id, m.chat_id
        from llm_usage u left join messages m on m.id = u.task_id where u.ts > ? and u.ts <= ? order by u.ts",
        params![done, last],
    );
    set_setting("llm_usage_moved", &json!(last));
}

// ---------- settings 表（值存成 JSON 文字） ----------
pub fn setting(key: &str) -> Option<Value> {
    let r = db::row("select value from settings where key=?", params![key])?;
    match r.first() {
        Some(Value::String(s)) => Some(serde_json::from_str(s).unwrap_or(Value::String(s.clone()))),
        Some(Value::Null) | None => Some(Value::Null),
        Some(v) => Some(v.clone()),
    }
}

/// settings 表里原样的文字（不当 JSON 解析）
pub fn setting_raw(key: &str) -> Option<String> {
    db::row("select value from settings where key=?", params![key]).map(|r| r.s(0))
}

pub fn set_setting(key: &str, value: &Value) {
    db::exec("insert or replace into settings values(?, ?)", params![key, py::dumps(value, true)]);
}

pub fn set_setting_raw(key: &str, value: &str) {
    db::exec("insert or replace into settings(key, value) values(?, ?)", params![key, value]);
}

pub fn del_setting(key: &str) {
    db::exec("delete from settings where key=?", params![key]);
}

// ---------- 消息 ----------
pub fn add(role: &str, content: &str, chat_id: i64, bot_id: Option<i64>, meta: &Value) -> i64 {
    let now = py::now();
    let meta = if meta.is_object() { meta.clone() } else { json!({}) };
    let id = db::insert(
        "insert into messages(ts, role, content, meta, chat_id, bot_id) values(?,?,?,?,?,?)",
        params![now, role, content, dumps_u(&meta), chat_id, bot_id],
    );
    db::exec("update chats set updated=? where id=?", params![now, chat_id]);
    id
}

pub fn update_meta(msg_id: i64, fields: &Value) {
    if let Some(r) = db::row("select meta from messages where id=?", params![msg_id]) {
        let meta = py::merge(&r.j(0), fields);
        db::exec("update messages set meta=? where id=?", params![dumps_u(&meta), msg_id]);
    }
}

pub fn message_out(r: &[Value]) -> Value {
    // id, ts, role, content, meta, bot_id
    let r = r.to_vec();
    json!({"id": r[0], "ts": r[1], "role": r[2], "content": r[3], "meta": r.j(4), "bot_id": r[5]})
}

/// 前端翻页用：某个对话里 id < before 的最近 limit 条消息，按时间正序返回
pub fn page(chat_id: i64, before: Option<i64>, limit: i64) -> Vec<Value> {
    let rows = db::rows(
        "select id, ts, role, content, meta, bot_id from messages where chat_id=? and id < ? order by id desc limit ?",
        params![chat_id, before.filter(|b| *b != 0).unwrap_or(BIG), limit],
    );
    let ids: Vec<i64> = rows.iter().map(|r| r.i(0)).collect();
    let threads = crate::bots::thread_summaries(chat_id, &ids);
    rows.iter()
        .rev()
        .map(|r| {
            let mut m = message_out(r);
            if let Some(t) = threads.get(&r.i(0)) {
                m["thread"] = t.clone();
            }
            m
        })
        .collect()
}

/// 讨论串的 (上级对话, 回复的那条消息)
pub fn thread_of(chat_id: i64) -> Option<(i64, i64)> {
    db::row("select parent, root from chats where id=? and kind='thread'", params![chat_id]).map(|r| (r.i(0), r.i(1)))
}

pub fn recall(msg_id: i64, chat_id: i64) -> String {
    let mut row = db::row("select role, content from messages where id=? and chat_id=?", params![msg_id, chat_id]);
    if row.is_none()
        && let Some((parent, root)) = thread_of(chat_id)
        && msg_id <= root
    {
        row = db::row("select role, content from messages where id=? and chat_id=?", params![msg_id, parent]);
    }
    match row {
        Some(r) => format!("[{}] {}", r.s(0), r.s(1)),
        None => format!("没有 id={msg_id} 的消息"),
    }
}

/// 最后一次“新话题”的消息 id
pub fn topic_start(chat_id: i64, before: Option<i64>) -> i64 {
    db::scalar_i64(
        "select max(id) from messages where chat_id=? and role='task' and id < ? and json_extract(meta, '$.kind')='reset'",
        params![chat_id, before.unwrap_or(BIG)],
    )
}

pub const THREAD_NOTE: &str = "（以下是讨论串：用户在针对上面 #{root} 这条消息回复，回答时围绕它。主对话里在它之后的消息这里看不到）";

type Row = (i64, String, String, String, Option<i64>);

fn rows_for(chat_id: i64, after: i64, upto: i64) -> Vec<Row> {
    db::rows(
        "select id, role, content, meta, bot_id from messages where chat_id=? and id > ? and id <= ? \
         and json_extract(meta, '$.quiet') is null order by id",
        params![chat_id, after, upto],
    )
    .into_iter()
    .map(|r| (r.i(0), r.s(1), r.s(2), r.s(3), r.oi(4)))
    .collect()
}

fn context_rows(chat_id: i64) -> (Vec<Row>, Option<usize>, Option<i64>) {
    match thread_of(chat_id) {
        None => (rows_for(chat_id, topic_start(chat_id, None), BIG), None, None),
        Some((parent, root)) => {
            let mut before = rows_for(parent, topic_start(parent, Some(root)), root);
            let n = before.len();
            before.extend(rows_for(chat_id, 0, BIG));
            (before, Some(n), Some(root))
        }
    }
}

pub fn via_name(v: &str) -> String {
    match v {
        "telegram" => " Telegram".into(),
        "feishu" => "飞书".into(),
        "dingtalk" => "钉钉".into(),
        "wecom" => "企业微信".into(),
        "discord" => " Discord".into(),
        other => other.into(),
    }
}

static MARKS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"系统(通知|附上的当前状态|附图)").unwrap());

/// 别的 Bot 的发言、私信、交接备注、任务结果里出现的系统标记改成“转述的……”
pub fn defang(text: &str) -> String {
    MARKS.replace_all(text, "转述的“系统$1”").into_owned()
}

fn pick_images(recent: &[Row], limit: usize) -> HashSet<(i64, String)> {
    let mut out = HashSet::new();
    for (mid, r, _, meta, _) in recent.iter().rev() {
        if out.len() >= limit {
            break;
        }
        if r != "user" || !meta.contains("\"media\"") {
            continue;
        }
        let m: Value = serde_json::from_str(meta).unwrap_or(json!({}));
        if let Some(atts) = m["attachments"].as_array() {
            for a in atts.iter().rev() {
                let media = a["media"].as_str().unwrap_or("");
                let mime = a["mime"].as_str().unwrap_or("");
                if !media.is_empty() && crate::media::MODEL_MIMES.contains(&mime) && out.len() < limit {
                    out.insert((*mid, media.to_string()));
                }
            }
        }
    }
    out
}

/// 组装给 Router 的历史消息（见 memory.py 的 context）
pub fn context(chat_id: i64, viewer: Option<i64>, me: Option<i64>, images: usize) -> Vec<Value> {
    let n = crate::config::get_i64("memory.inline_messages", 20).max(1) as usize;
    let (rows, split, root) = context_rows(chat_id);
    let names: HashMap<i64, String> = db::rows("select id, name from bots", []).into_iter().map(|r| (r.i(0), r.s(1))).collect();
    let me = if viewer.is_some() { viewer } else { me };
    let name = |b: Option<i64>| -> String { b.and_then(|b| names.get(&b).cloned()).unwrap_or_else(|| "已删除的 Bot".into()) };
    let cut = if rows.len() > n { (rows.len() - n) / n * n } else { 0 };
    let (old, recent) = rows.split_at(cut);
    let mut msgs = vec![];
    if !old.is_empty() {
        let who = |r: &str, b: Option<i64>| -> String {
            if viewer.is_some() && r == "assistant" && b != viewer { name(b) } else { r.to_string() }
        };
        let start = old.len().saturating_sub(200);
        let index: Vec<String> =
            old[start..].iter().map(|(i, r, c, _, b)| format!("#{i} [{}] {}", who(r, *b), py::head(c, 60).replace('\n', " "))).collect();
        msgs.push(
            json!({"role": "system", "content": format!("更早的对话已移出上下文，需要时用 recall(id) 取回原文：\n{}", index.join("\n"))}),
        );
    }
    let at: isize = match split {
        Some(s) => s.max(cut) as isize,
        None => -1,
    };
    let shown = if images > 0 { pick_images(recent, images) } else { HashSet::new() };
    for (k, (mid, r, c, meta, b)) in recent.iter().enumerate() {
        let k = (k + cut) as isize;
        let meta: Value = serde_json::from_str(if meta.is_empty() { "{}" } else { meta }).unwrap_or(json!({}));
        let mut c = c.clone();
        if k == at {
            let note = THREAD_NOTE.replace("{root}", &root.unwrap_or(0).to_string());
            if r == "user" {
                c = note + &c;
            } else {
                msgs.push(json!({"role": "user", "content": note}));
            }
        }
        let others = viewer.is_some() && *b != viewer;
        let kind = meta["kind"].as_str().unwrap_or("");
        if r == "assistant" || kind == "result" {
            c = no_leak(&c);
        }
        let mstr = |k: &str| -> String { meta[k].as_str().map(String::from).unwrap_or_default() };
        let note_or = |c: &str| -> String {
            let n = mstr("note");
            defang(if n.is_empty() { c } else { &n })
        };
        let prompt_or = |c: &str| -> String {
            let p = mstr("prompt");
            defang(if p.is_empty() { c } else { &p })
        };
        if r == "task" && kind == "handoff" && viewer.is_some() {
            let to = meta["to"].as_i64();
            let text = if to == viewer {
                format!("（{} 把工作交给了你，现在轮到你）{}", name(*b), note_or(&c))
            } else if *b == viewer {
                format!("（系统通知：你把工作交给了 {}）{}", name(to), note_or(&c))
            } else {
                format!("（系统通知：{} 把工作交给了 {}）{}", name(*b), name(to), note_or(&c))
            };
            msgs.push(json!({"role": "user", "content": text}));
        } else if r == "task" && kind == "image" {
            let whose = if others && b.is_some() { name(*b) } else { "你".into() };
            let refs: Vec<String> = meta["refs"]
                .as_array()
                .map(|a| a.iter().map(|x| x["name"].as_str().unwrap_or("").to_string()).collect())
                .unwrap_or_default();
            let refs = refs.join("、");
            msgs.push(json!({"role": "user", "content": format!(
                "（系统通知：{whose}{}画好了一张图，已经显示给用户。描述：{}）{}",
                if refs.is_empty() { String::new() } else { format!("照着 {refs} ") }, prompt_or(&c), image_ids(&meta, true))}));
        } else if r == "task" && kind == "video" {
            let whose = if others && b.is_some() { name(*b) } else { "你".into() };
            let ids: String = meta["attachments"]
                .as_array()
                .map(|a| a.iter().filter_map(|x| x["media"].as_str().filter(|s| !s.is_empty()).map(|m| format!("\n[视频] {m}"))).collect())
                .unwrap_or_default();
            msgs.push(json!({"role": "user", "content": format!("（系统通知：{whose}生成好了一段视频，已经显示给用户。描述：{}）{ids}", prompt_or(&c))}));
        } else if r == "task" && kind == "video_job" {
            if meta["status"] == "running" {
                let whose = if others && b.is_some() { name(*b) } else { "你".into() };
                msgs.push(json!({"role": "user", "content": format!("（系统通知：{whose}提交的视频还在生成，好了会自动显示。描述：{}）", prompt_or(&c))}));
            }
        } else if r == "task" && (kind == "dm" || kind == "dm_reply") {
            msgs.push(json!({"role": "user", "content": dm_line(&meta, &defang(&c), *b, me, &name)}));
        } else if r == "task" {
            let whose = if others && b.is_some() { format!("{} 的", name(*b)) } else { String::new() };
            let note = if kind == "result" {
                format!("（系统通知，非用户发言；这是{whose}任务结果，已作为卡片直接展示给用户，不用复述，除非用户问起）")
            } else if kind == "followup" {
                // 用户直接对任务说的话（任务详情里、讨论串里、tell_task 转的）：是用户说的，但已经交给任务了
                format!("（系统通知：下面是用户对{whose}任务说的话，已经交给那个任务了，不用你再转）")
            } else {
                format!("（系统通知，非用户发言{}）", if whose.is_empty() { String::new() } else { format!("；{whose}任务") })
            };
            msgs.push(json!({"role": "user", "content": note + &defang(&c)}));
        } else if r == "assistant" && others {
            msgs.push(json!({"role": "user", "content": format!("【{}】{}", name(*b), defang(&c))}));
        } else if r == "assistant" && meta.get("reasoning").is_some() {
            let mut m = json!({"role": "assistant", "content": c});
            if !meta["reasoning"].is_null() {
                m["reasoning_content"] = meta["reasoning"].clone();
            }
            msgs.push(m);
        } else if r == "assistant" {
            msgs.push(json!({"role": "user", "content": format!("（系统通知：这是你之前给用户的回复）{c}")}));
        } else {
            if let Some(v) = meta["via"].as_str().filter(|v| !v.is_empty()) {
                c = format!("（来自{}）{c}", via_name(v));
            }
            let ap = &meta["applet"];
            if py::truthy(ap) && py::truthy(&ap["app"]) {
                c = format!("（来自应用 @{} 的界面）{c}", py::str_of(&ap["app"]));
            } else if py::truthy(ap) {
                c = format!("（来自小应用「{}」）{c}", ap["title"].as_str().unwrap_or(""));
            }
            if let Some(sp) = meta["skill_prompt"].as_str().filter(|s| !s.is_empty()) {
                c = format!("{c}\n\n{sp}");
            }
            c += &image_ids(&meta, false);
            let mut parts = vec![];
            if !shown.is_empty()
                && let Some(atts) = meta["attachments"].as_array()
            {
                for a in atts {
                    let media = a["media"].as_str().unwrap_or("");
                    if shown.contains(&(*mid, media.to_string()))
                        && let Some(p) = crate::media::image_part(media)
                    {
                        parts.push(p);
                    }
                }
            }
            if parts.is_empty() {
                msgs.push(json!({"role": "user", "content": c}));
            } else {
                let mut content = vec![json!({"type": "text", "text": c})];
                content.extend(parts);
                msgs.push(json!({"role": "user", "content": content}));
            }
        }
    }
    msgs
}

fn image_ids(meta: &Value, all: bool) -> String {
    meta["attachments"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter(|x| py::truthy(&x["media"]) && (all || !py::truthy(&x["path"])))
                .map(|x| format!("\n[图片] {}", py::str_of(&x["media"])))
                .collect()
        })
        .unwrap_or_default()
}

fn no_leak(c: &str) -> String {
    let (text, calls) = crate::dsml::split(c);
    if calls.is_empty() {
        return text;
    }
    if text.is_empty() {
        format!("（这里想调用 {}，但写成了文字，没有执行）", calls.iter().map(|x| x.name.as_str()).collect::<Vec<_>>().join("、"))
    } else {
        text
    }
}

fn dm_line(meta: &Value, c: &str, sender: Option<i64>, me: Option<i64>, name: &dyn Fn(Option<i64>) -> String) -> String {
    let to = meta["to"].as_i64();
    if meta["kind"] == "dm" {
        if to == me {
            return format!("（{} 私信了你，不是用户说的话）{c}", name(sender));
        }
        if sender == me {
            return format!("（系统通知：你私信了 {}，等它回信）{c}", name(to));
        }
        return format!("（系统通知：{} 私信了 {}）{c}", name(sender), name(to));
    }
    if py::truthy(&meta["sent"]) {
        let who = if sender == me { "你".to_string() } else { name(sender) };
        return format!("（系统通知：{who}已把回信发给 {}）", name(to));
    }
    let mut c = c.to_string();
    if py::truthy(&meta["task_id"]) && meta["status"] == "done" {
        c = format!("任务 #{} 的结果：\n{c}", py::str_of(&meta["task_id"]));
    }
    if to == me {
        return format!("（{} 回了你的私信，不是用户说的话）{c}", name(sender));
    }
    format!("（系统通知：{} 回了 {} 的私信）{c}", name(sender), name(to))
}

// ---------- 模型调用的记录 ----------
fn is_peak(ts: f64) -> bool {
    use chrono::{Datelike, TimeZone};
    let Some(t) = chrono::Local.timestamp_opt(ts as i64, 0).single() else { return false };
    if t.weekday().num_days_from_monday() >= 5 {
        return false;
    }
    let hm = t.format("%H:%M").to_string();
    crate::config::get("pricing.peak_hours")
        .as_array()
        .map(|a| {
            a.iter().filter_map(|r| r.as_str()).any(|r| match r.split_once('-') {
                Some((a, b)) => a <= hm.as_str() && hm.as_str() < b,
                None => false,
            })
        })
        .unwrap_or(false)
}

/// 这次模型调用是谁、在哪个对话里发起的（Python 的 WHO ContextVar）
#[derive(Clone, Copy, Debug, Default)]
pub struct Who {
    pub bot_id: Option<i64>,
    pub chat_id: Option<i64>,
    pub task_id: Option<i64>,
}

tokio::task_local! {
    pub static WHO: Who;
}

pub fn who() -> Who {
    WHO.try_with(|w| *w).unwrap_or_default()
}

#[derive(Default, Clone, Debug)]
pub struct UsageRec {
    pub task_id: Option<i64>,
    pub provider: Option<String>,
    pub bot_id: Option<i64>,
    pub chat_id: Option<i64>,
    pub hit: i64,
    pub miss: i64,
    pub out: i64,
    pub cost: f64,
    pub ms: Option<f64>,
    pub ttft: Option<f64>,
    pub err: String,
}

pub fn add_usage(role: &str, model: &str, u: UsageRec) {
    let w = who();
    let task_id = u.task_id.or(w.task_id);
    let mut bot_id = u.bot_id.or(w.bot_id);
    let mut chat_id = u.chat_id.or(w.chat_id);
    if bot_id.is_none()
        && let Some(t) = task_id
        && let Some(r) = db::row("select bot_id, chat_id from messages where id=?", params![t])
    {
        bot_id = r.oi(0);
        chat_id = chat_id.or(r.oi(1));
    }
    db::exec(
        "insert into llm_calls(ts, role, model, task_id, cache_hit, cache_miss, output, cost,
        bot_id, chat_id, provider, ms, ttft, err) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![
            py::now(),
            role,
            model,
            task_id,
            u.hit,
            u.miss,
            u.out,
            u.cost,
            bot_id,
            chat_id,
            u.provider,
            u.ms.map(|x| x as i64),
            u.ttft.map(|x| x as i64),
            if u.err.is_empty() { None } else { Some(u.err) }
        ],
    );
}

/// 记录一次模型调用的 token 和费用。usage 是 OpenAI 格式的 usage（JSON）
pub fn record_usage(role: &str, model: &str, usage: Option<&Value>, mut rec: UsageRec) {
    if usage.is_none() && rec.ms.is_none() {
        return;
    }
    let u = usage.cloned().unwrap_or(Value::Null);
    let prompt = u["prompt_tokens"].as_i64().unwrap_or(0);
    let hit = match u.get("prompt_cache_hit_tokens").and_then(|v| v.as_i64()) {
        Some(h) => h,
        None => u["prompt_tokens_details"]["cached_tokens"].as_i64().unwrap_or(0),
    };
    let out = u["completion_tokens"].as_i64().unwrap_or(0);
    let provider = rec.provider.clone().unwrap_or_else(|| crate::config::get_str(&format!("roles.{role}.provider")));
    rec.cost = crate::prices::cost(&provider, model, hit, prompt - hit, out, is_peak(py::now()));
    rec.provider = Some(provider);
    rec.hit = hit;
    rec.miss = prompt - hit;
    rec.out = out;
    add_usage(role, model, rec);
}

pub fn record_error(role: &str, model: &str, error: &str, mut rec: UsageRec) {
    if rec.provider.is_none() {
        rec.provider = Some(crate::config::get_str(&format!("roles.{role}.provider")));
    }
    rec.err = py::head(if error.is_empty() { "error" } else { error }, 300);
    add_usage(role, model, rec);
}

pub fn tokens_today() -> i64 {
    db::scalar_i64("select coalesce(sum(cache_hit+cache_miss+output),0) from llm_calls where ts > ?", params![py::now() - 86400.0])
}

pub fn usage_stats() -> Value {
    let now = py::now();
    let day0 = {
        use chrono::TimeZone;
        let d = chrono::Local::now().date_naive().and_hms_opt(0, 0, 0).unwrap();
        chrono::Local.from_local_datetime(&d).earliest().map(|t| t.timestamp() as f64).unwrap_or(now)
    };
    let total = |since: f64| -> Value {
        let r = db::row(
            "select coalesce(sum(cost),0), coalesce(sum(cache_hit),0), coalesce(sum(cache_miss),0),
            coalesce(sum(output),0), count(*) from llm_calls where ts >= ? and err is null",
            params![since],
        )
        .unwrap_or_default();
        let (h, m) = (r.i(1), r.i(2));
        json!({"cost": py::round_n(r.f(0), 4), "cache_hit": h, "cache_miss": m, "output": r.i(3), "calls": r.i(4),
               "cache_rate": if h + m > 0 { py::round_n(h as f64 / (h + m) as f64, 3) } else { json!(0).as_f64().unwrap() }})
    };
    let by_role: Vec<Value> = db::rows(
        "select role, model, sum(cost), sum(cache_hit+cache_miss+output), count(*) from llm_calls
        where ts >= ? and err is null group by role, model order by 3 desc",
        params![now - 30.0 * 86400.0],
    )
    .into_iter()
    .map(|r| json!({"role": r[0], "model": r[1], "cost": py::round_n(r.f(2), 4), "tokens": r[3], "calls": r[4]}))
    .collect();
    let by_task: Vec<Value> = db::rows(
        "select u.task_id, sum(u.cost), sum(u.cache_hit+u.cache_miss+u.output), m.content
        from llm_calls u left join messages m on m.id = u.task_id where u.task_id is not null
        group by u.task_id order by u.task_id desc limit 20",
        [],
    )
    .into_iter()
    .map(|r| {
        let g = r.s(3);
        let g = g.strip_prefix("[任务已派发] ").unwrap_or(&g).to_string();
        json!({"id": r[0], "cost": py::round_n(r.f(1), 4), "tokens": r[2], "goal": py::head(&g, 60)})
    })
    .collect();
    let by_day: Vec<Value> = db::rows(
        "select date(ts, 'unixepoch', 'localtime') d, sum(cost) from llm_calls where ts >= ? group by d order by d",
        params![now - 30.0 * 86400.0],
    )
    .into_iter()
    .map(|r| json!({"day": r[0], "cost": py::round_n(r.f(1), 4)}))
    .collect();
    json!({
        "currency": crate::config::get("pricing.currency"),
        "today": total(day0), "week": total(now - 7.0 * 86400.0), "month": total(now - 30.0 * 86400.0),
        "by_role": by_role, "by_task": by_task, "by_day": by_day,
    })
}

/// meta 参数的简写：meta!{"kind": "dispatch"}
pub fn meta_of(pairs: &[(&str, Value)]) -> Value {
    let mut m = Map::new();
    for (k, v) in pairs {
        m.insert(k.to_string(), v.clone());
    }
    Value::Object(m)
}

#[allow(dead_code)]
fn _unused(_: J) {}
