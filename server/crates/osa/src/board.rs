//! 需求看板和项目：你要的每件事一张卡（需求），归在项目下、带编号（`OPC-12`）；卡在哪一列由它背后的任务算出来，不存。
//! 约定见 docs/plans/tasks-board.md 的“看板和项目”。
//!
//! 第一波不改 router.rs / worker.rs / task.rs：任务的情况从现成的地方读（内存里的任务、`messages` 里派发那条的
//! `meta.status`、待确认列表）；任务状态变了靠 registry 的 `task_done` 和一个每秒一次的轻量检查发 `board` 事件。

use crate::db::{self, J, RowExt};
use crate::router::{self, Dispatch};
use crate::task::Task;
use crate::tasklog::DISPATCH;
use crate::web::{ApiError, Body, Q, Res, ok, qi, qs};
use crate::{approvals, bots, providers, py, registry, server};
use axum::extract::Path;
use axum::routing::{get, post};
use parking_lot::Mutex;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, LazyLock};

pub const COLUMNS: [&str; 5] = ["todo", "doing", "waiting", "done", "cancelled"];
/// 结束的卡片（完成、取消）除了归档的只留最近这么多张
pub const KEEP_ENDED: usize = 500;
pub const TITLE_MAX: usize = 200;
pub const BODY_MAX: usize = 20_000;

pub fn migrate() {
    db::batch(
        "create table if not exists projects(id integer primary key, key text unique, name text, brief text default '',
           dir text default '', repo text default '', next_no integer default 1, created real);
         create table if not exists needs(id integer primary key, project_id integer, no integer, title text,
           auto_title integer default 0, body text default '', source_text text default '', status text default '',
           assignee_bot_id integer, after text default '[]', origin_chat integer, origin_msg integer,
           created real, updated real, archived integer default 0, started real, start_msg integer);
         create index if not exists needs_project on needs(project_id, no);
         create table if not exists need_tasks(need_id integer, task_id integer, primary key(need_id, task_id));
         create index if not exists need_tasks_task on need_tasks(task_id);",
    );
}

// ---------- 事件 ----------

/// 正在被检查的卡：need_id → 上次发事件时的样子（列、任务状态）
static WATCH: LazyLock<Mutex<HashMap<i64, String>>> = LazyLock::new(Default::default);
/// 交给主 Bot、它这一轮还没说完的卡
static TURNS: LazyLock<Mutex<HashSet<i64>>> = LazyLock::new(Default::default);

fn changed(need_id: i64) {
    server::broadcast(json!({"type": "board", "need_id": need_id}));
}

/// 项目变了（卡片的编号可能跟着变）：need_id 是 null，带上 project_id
fn projects_changed(project_id: i64) {
    server::broadcast(json!({"type": "board", "need_id": null, "project_id": project_id}));
}

fn sig(c: &Value) -> String {
    let ts: Vec<String> =
        c["tasks"].as_array().into_iter().flatten().map(|t| format!("{}:{}:{}", t["id"], t["status"], t["waiting_kind"])).collect();
    format!("{}|{}", c["column"], ts.join(","))
}

fn watch(need_id: i64) {
    let s = card(need_id).map(|c| sig(&c)).unwrap_or_default();
    WATCH.lock().insert(need_id, s);
}

/// 看一遍正在检查的卡，变了就发事件；没有在做的任务了就不再看
fn check_watched() {
    let ids: Vec<(i64, String)> = WATCH.lock().iter().map(|(k, v)| (*k, v.clone())).collect();
    for (id, old) in ids {
        let Some(c) = card(id) else {
            WATCH.lock().remove(&id);
            continue;
        };
        let s = sig(&c);
        if s != old {
            changed(id);
        }
        let live = c["tasks"].as_array().into_iter().flatten().any(|t| t["status"] == "running") || TURNS.lock().contains(&id);
        let mut w = WATCH.lock();
        if live {
            w.insert(id, s);
        } else {
            w.remove(&id);
        }
    }
}

async fn on_task_done(kw: Value) {
    let Some(tid) = kw["task"]["id"].as_i64() else { return };
    for nid in needs_of(tid) {
        changed(nid);
        watch(nid);
    }
}

/// 后台：任务结束（registry 的 task_done）马上发事件；等确认、被取消这些没有钩子的，每秒看一次
pub async fn watcher() {
    let ctx = registry::Context::new("board");
    ctx.on("task_done", Arc::new(|kw| Box::pin(on_task_done(kw))));
    std::mem::forget(ctx);
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        check_watched();
    }
}

// ---------- 任务 ----------

/// 任务在等你什么：`approval`（确认）、`help`（接手电脑）、`question`（回答），没在等是 None。
/// task-core 的 `waiting_kind` 还没合进来，现在只按 `waiting_approval` 算；收拢时换成任务自己的字段。
fn waiting_kind(t: &Task) -> Option<&'static str> {
    t.st.lock().waiting_approval.map(|_| "approval")
}

/// 卡片上的一个任务：{id, bot_id, chat_id, status, waiting_kind, goal, last_action, elapsed}。消息被删了的是 None
fn task_info(tid: i64) -> Option<Value> {
    let r = db::row(&format!("select id, content, meta, bot_id, chat_id from messages where id=? and {DISPATCH}"), params![tid])?;
    let m = r.j(2);
    let c = r.s(1);
    let goal = c.strip_prefix("[任务已派发]").unwrap_or(&c).trim().to_string();
    let mut o = json!({"id": tid, "bot_id": r[3], "chat_id": r[4], "status": m.get("status").cloned().unwrap_or(json!("done")),
        "waiting_kind": null, "goal": goal, "last_action": "", "elapsed": m.get("elapsed").cloned().unwrap_or(Value::Null)});
    if let Some(t) = router::task(tid) {
        let status = t.status();
        if status == "running" {
            let p = t.public();
            o["waiting_kind"] = json!(waiting_kind(&t));
            o["goal"] = json!(t.goal);
            o["last_action"] = p["last_action"].clone();
            o["elapsed"] = p["elapsed"].clone();
        }
        o["status"] = json!(status);
    }
    Some(o)
}

fn task_ids(need_id: i64) -> Vec<i64> {
    db::rows("select task_id from need_tasks where need_id=? order by task_id", params![need_id]).iter().map(|r| r.i(0)).collect()
}

fn needs_of(task_id: i64) -> Vec<i64> {
    db::rows("select need_id from need_tasks where task_id=? order by need_id", params![task_id]).iter().map(|r| r.i(0)).collect()
}

/// 任务的结果（结果那条消息去掉开头的标记）
fn result_of(t: &Value) -> Option<String> {
    static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\[任务 #\d+ 结果\]\n?").unwrap());
    let r = db::row(
        "select content from messages where chat_id=? and id>? and role='task'
         and json_extract(meta, '$.task_id')=? and json_extract(meta, '$.kind')='result' order by id desc limit 1",
        params![t["chat_id"].as_i64(), t["id"].as_i64(), t["id"].as_i64()],
    )?;
    Some(RE.replace(&r.s(0), "").into_owned())
}

/// 把任务归到卡上。新归上的返回 true
fn link(need_id: i64, task_id: i64) -> bool {
    let n = db::exec("insert or ignore into need_tasks(need_id, task_id) values(?, ?)", params![need_id, task_id]);
    if n > 0 {
        db::exec("update needs set updated=? where id=?", params![py::now(), need_id]);
        watch(need_id);
        changed(need_id);
    }
    n > 0
}

// ---------- 卡片 ----------

const COLS: &str = "n.id, n.project_id, n.no, n.title, n.auto_title, n.body, n.source_text, n.status, n.assignee_bot_id, n.after,
    n.origin_chat, n.origin_msg, n.created, n.updated, n.archived, n.started, n.start_msg, p.key, p.name";

/// needs 表的一行
#[derive(Clone, Debug, Default)]
struct Need {
    id: i64,
    project_id: Option<i64>,
    no: Option<i64>,
    title: String,
    auto_title: bool,
    body: String,
    source_text: String,
    status: String,
    assignee: Option<i64>,
    after: Vec<i64>,
    origin_chat: Option<i64>,
    origin_msg: Option<i64>,
    created: f64,
    updated: f64,
    archived: bool,
    started: Option<f64>,
    start_msg: Option<i64>,
    pkey: Option<String>,
}

impl Need {
    fn from_row(r: &Vec<Value>) -> Need {
        Need {
            id: r.i(0),
            project_id: r.oi(1),
            no: r.oi(2),
            title: r.s(3),
            auto_title: r.i(4) != 0,
            body: r.s(5),
            source_text: r.s(6),
            status: r.s(7),
            assignee: r.oi(8),
            after: parse_ids(&r.s(9)),
            origin_chat: r.oi(10),
            origin_msg: r.oi(11),
            created: r.f(12),
            updated: r.f(13),
            archived: r.i(14) != 0,
            started: r.get(15).and_then(|v| v.as_f64()),
            start_msg: r.oi(16),
            pkey: r.get(17).and_then(|v| v.as_str()).map(String::from),
        }
    }

    fn key(&self) -> Option<String> {
        match (&self.pkey, self.no) {
            (Some(k), Some(n)) => Some(format!("{k}-{n}")),
            _ => None,
        }
    }

    /// 给人、给模型看的称呼：有编号用编号，没有用标题
    fn label(&self) -> String {
        match self.key() {
            Some(k) => format!("{k} {}", self.title),
            None => self.title.clone(),
        }
    }
}

fn parse_ids(s: &str) -> Vec<i64> {
    serde_json::from_str::<Value>(s).ok().and_then(|v| v.as_array().map(|a| a.iter().filter_map(py::as_int).collect())).unwrap_or_default()
}

fn load(id: i64) -> Option<Need> {
    db::row(&format!("select {COLS} from needs n left join projects p on p.id=n.project_id where n.id=?"), params![id])
        .map(|r| Need::from_row(&r))
}

fn need_or_404(id: i64) -> Result<Need, ApiError> {
    load(id).ok_or_else(|| ApiError::not_found(t!("没有这张卡片")))
}

/// 卡在哪一列。`status` 是手动标的（done / cancelled 优先）；`in_turn`：交给主 Bot、它这一轮还没说完
pub fn column(status: &str, started: bool, in_turn: bool, tasks: &[Value]) -> &'static str {
    match status {
        "done" => return "done",
        "cancelled" => return "cancelled",
        _ => {}
    }
    let running = |t: &&Value| t["status"] == "running";
    if tasks.iter().filter(running).any(|t| !t["waiting_kind"].is_null()) {
        return "waiting";
    }
    if tasks.iter().any(|t| running(&t)) || in_turn {
        return "doing";
    }
    match tasks.last() {
        Some(t) if t["status"] == "done" => "done",
        Some(_) => "waiting",
        // 交给主 Bot 以后它没派任务、直接回了话：等你看过以后自己标完成
        None if started => "waiting",
        None => "todo",
    }
}

fn first_line(s: &str, n: usize) -> String {
    py::head(s.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or(""), n)
}

/// 交给主 Bot 以后它的第一句回复
fn reply_after(n: &Need) -> Option<String> {
    let (Some(cid), Some(mid)) = (n.origin_chat_or_start(), n.start_msg) else { return None };
    db::row("select content from messages where chat_id=? and id>? and role='assistant' order by id limit 1", params![cid, mid])
        .map(|r| r.s(0))
}

impl Need {
    /// 开始时发消息的那个对话（start_msg 所在的）
    fn origin_chat_or_start(&self) -> Option<i64> {
        self.start_msg.and_then(|m| db::scalar("select chat_id from messages where id=?", params![m]).as_i64())
    }
}

fn latest(n: &Need, tasks: &[Value]) -> String {
    if let Some(t) = tasks.iter().rev().find(|t| t["status"] == "running" && py::truthy(&t["last_action"])) {
        return first_line(&py::str_of(&t["last_action"]), 100);
    }
    if let Some(t) = tasks.last() {
        if let Some(r) = result_of(t).filter(|r| !r.trim().is_empty()) {
            return first_line(&r, 100);
        }
        return first_line(&py::str_of(&t["goal"]), 100);
    }
    reply_after(n).map(|r| first_line(&r, 100)).unwrap_or_default()
}

/// 只算列（blocked_by 用，不展开别的卡）
fn column_of(n: &Need) -> &'static str {
    let tasks: Vec<Value> = task_ids(n.id).into_iter().filter_map(task_info).collect();
    column(&n.status, n.started.is_some(), TURNS.lock().contains(&n.id), &tasks)
}

fn card_of(n: &Need) -> Value {
    let tasks: Vec<Value> = task_ids(n.id).into_iter().filter_map(task_info).collect();
    let col = column(&n.status, n.started.is_some(), TURNS.lock().contains(&n.id), &tasks);
    let blocked: Vec<Value> = n
        .after
        .iter()
        .filter_map(|a| load(*a))
        .filter_map(|b| {
            let c = column_of(&b);
            (c != "done").then(|| json!({"id": b.id, "key": b.key(), "title": b.title, "column": c}))
        })
        .collect();
    let done = tasks.iter().filter(|t| t["status"] == "done").count();
    let origin = match (n.origin_chat, n.origin_msg) {
        (Some(c), Some(m)) => json!({"chat_id": c, "message_id": m}),
        _ => Value::Null,
    };
    json!({"id": n.id, "key": n.key(), "no": n.no, "project_id": n.project_id, "title": n.title, "auto_title": n.auto_title,
        "body": n.body, "source_text": n.source_text, "column": col, "assignee_bot_id": n.assignee, "after": n.after,
        "blocked_by": blocked, "progress": {"done": done, "total": tasks.len()}, "latest": latest(n, &tasks),
        "tasks": tasks, "created": n.created, "updated": n.updated, "archived": n.archived, "origin": origin})
}

pub fn card(id: i64) -> Option<Value> {
    load(id).map(|n| card_of(&n))
}

/// 卡片页：另带每个任务的结果和在等你的确认卡片
fn detail(id: i64) -> Option<Value> {
    let mut c = card(id)?;
    let tasks = c["tasks"].as_array().cloned().unwrap_or_default();
    let results: Vec<Value> = tasks.iter().filter_map(|t| result_of(t).map(|r| json!({"task_id": t["id"], "text": r}))).collect();
    let ids: HashSet<i64> = tasks.iter().filter_map(|t| t["id"].as_i64()).collect();
    let questions: Vec<Value> =
        approvals::pending_public().into_iter().filter(|a| a["task_id"].as_i64().is_some_and(|t| ids.contains(&t))).collect();
    c["results"] = json!(results);
    c["questions"] = json!(questions);
    Some(c)
}

// ---------- 改卡片 ----------

fn clean_title(v: &Value) -> Result<String, ApiError> {
    let t = py::str_of(v).split_whitespace().collect::<Vec<_>>().join(" ");
    if t.is_empty() || v.is_null() {
        return Err(ApiError::bad(t!("标题不能为空")));
    }
    if py::char_len(&t) > TITLE_MAX {
        return Err(ApiError::bad(t!("标题太长（最多 {n} 字）", n = TITLE_MAX)));
    }
    Ok(t)
}

fn clean_body(v: &Value) -> Result<String, ApiError> {
    let b = if v.is_null() { String::new() } else { py::str_of(v).trim().to_string() };
    if py::char_len(&b) > BODY_MAX {
        return Err(ApiError::bad(t!("描述太长（最多 {n} 字）", n = BODY_MAX)));
    }
    Ok(b)
}

fn check_project(v: &Value) -> Result<Option<i64>, ApiError> {
    if v.is_null() {
        return Ok(None);
    }
    let id = crate::web::int(v)?;
    if !db::exists("select 1 from projects where id=?", params![id]) {
        return Err(ApiError::bad(t!("没有这个项目")));
    }
    Ok(Some(id))
}

fn check_bot(v: &Value) -> Result<Option<i64>, ApiError> {
    if v.is_null() {
        return Ok(None);
    }
    let id = crate::web::int(v)?;
    if bots::get(id).is_none() {
        return Err(ApiError::bad(t!("没有这个 Bot")));
    }
    Ok(Some(id))
}

/// 要等的卡：都得在，不能等自己，不能互相等
fn check_after(me: Option<i64>, v: &Value) -> Result<Vec<i64>, ApiError> {
    if v.is_null() {
        return Ok(vec![]);
    }
    let Some(a) = v.as_array() else { return Err(ApiError::bad(t!("after 要是卡片 id 的列表"))) };
    let mut out: Vec<i64> = vec![];
    for x in a {
        let id = crate::web::int(x)?;
        if Some(id) == me {
            return Err(ApiError::bad(t!("卡片不能等它自己")));
        }
        if !db::exists("select 1 from needs where id=?", params![id]) {
            return Err(ApiError::bad(t!("没有卡片 #{id}", id = id)));
        }
        if !out.contains(&id) {
            out.push(id);
        }
    }
    if let Some(me) = me
        && waits_on(&out, me)
    {
        return Err(ApiError::bad(t!("这样几张卡会互相等，谁也开始不了")));
    }
    Ok(out)
}

/// 从 start 这些卡顺着 after 往前找，会不会找到 target
fn waits_on(start: &[i64], target: i64) -> bool {
    let mut seen: HashSet<i64> = HashSet::new();
    let mut stack: Vec<i64> = start.to_vec();
    while let Some(id) = stack.pop() {
        if id == target {
            return true;
        }
        if !seen.insert(id) {
            continue;
        }
        let a = db::scalar("select after from needs where id=?", params![id]);
        stack.extend(parse_ids(a.as_str().unwrap_or("[]")));
    }
    false
}

/// 在项目里取下一个编号（不回收）
fn next_no(project_id: i64) -> i64 {
    let _g = db::conn();
    let n = db::scalar_i64("select next_no from projects where id=?", params![project_id]).max(1);
    db::exec("update projects set next_no=? where id=?", params![n + 1, project_id]);
    n
}

#[derive(Default)]
struct NewNeed {
    project_id: Option<i64>,
    title: String,
    auto_title: bool,
    body: String,
    source_text: String,
    assignee: Option<i64>,
    after: Vec<i64>,
    origin: Option<(i64, i64)>,
    started: bool,
}

fn insert(n: NewNeed) -> i64 {
    let now = py::now();
    let no = n.project_id.map(next_no);
    db::insert(
        "insert into needs(project_id, no, title, auto_title, body, source_text, status, assignee_bot_id, after,
           origin_chat, origin_msg, created, updated, archived, started) values(?,?,?,?,?,?,'',?,?,?,?,?,?,0,?)",
        params![
            n.project_id,
            no,
            n.title,
            n.auto_title as i64,
            n.body,
            n.source_text,
            n.assignee,
            py::dumps_u(&json!(n.after)),
            n.origin.map(|o| o.0),
            n.origin.map(|o| o.1),
            now,
            now,
            n.started.then_some(now)
        ],
    )
}

fn set(id: i64, col: &str, v: &Value) {
    db::exec(&format!("update needs set {col}=?, updated=? where id=?"), params![J(v), py::now(), id]);
}

fn running_tasks(id: i64) -> Vec<i64> {
    task_ids(id).into_iter().filter(|t| router::task(*t).is_some_and(|x| x.status() == "running")).collect()
}

/// 开始一张卡：交给主 Bot 的当成你发给它的一条消息，交给别的 Bot 的直接派一个任务
fn start(id: i64) -> Result<Value, ApiError> {
    let n = need_or_404(id)?;
    if n.archived {
        return Err(ApiError::bad(t!("卡片已归档，先恢复")));
    }
    if !running_tasks(id).is_empty() || TURNS.lock().contains(&id) {
        return Err(ApiError::bad(t!("这张卡已经在做了")));
    }
    let bot = match n.assignee {
        Some(b) => bots::get(b).ok_or_else(|| ApiError::bad(t!("交给的 Bot 已删除")))?,
        None => bots::primary().ok_or_else(|| ApiError::bad(t!("没有主 Bot")))?,
    };
    let bot_id = bot["id"].as_i64().unwrap_or(0);
    let now = py::now();
    db::exec("update needs set status='', started=?, start_msg=null, updated=? where id=?", params![now, now, id]);
    // 任务归上来之前也算在做
    TURNS.lock().insert(id);
    watch(id);
    if bot["is_primary"] == true {
        let cid = bot["chat_id"].as_i64().filter(|c| *c != 0).ok_or_else(|| ApiError::bad(t!("主 Bot 的对话不在了")))?;
        let text = start_text(&n, true);
        tokio::spawn(tell_primary(id, bot_id, cid, text));
    } else {
        let goal = start_text(&n, false);
        let rt = router::runtime(bot_id);
        tokio::spawn(async move {
            let t = rt.dispatch(Dispatch { goal, mode: "single".into(), ..Default::default() }).await;
            link(id, t.id);
            TURNS.lock().remove(&id);
            watch(id);
        });
    }
    changed(id);
    Ok(card(id).unwrap_or(Value::Null))
}

/// 发给 Bot 的话：交给主 Bot 的开头带编号（没有编号带标题），派给别的 Bot 的是标题加描述；
/// 要等的卡都做完了的，附上它们的结果
fn start_text(n: &Need, with_key: bool) -> String {
    let head = if with_key { n.label() } else { n.title.clone() };
    let mut s = if n.body.is_empty() { head } else { format!("{head}\n\n{}", n.body) };
    let before: Vec<String> = n
        .after
        .iter()
        .filter_map(|a| load(*a))
        .filter_map(|b| {
            let r: Vec<String> = task_ids(b.id).into_iter().filter_map(task_info).filter_map(|t| result_of(&t)).collect();
            r.last().map(|r| format!("【{}】\n{}", b.label(), py::head(r, 3000)))
        })
        .collect();
    if !before.is_empty() {
        s.push_str(&format!("\n\n前面几张卡的结果：\n{}", before.join("\n\n")));
    }
    s
}

/// 像你发了一条消息那样交给主 Bot；这一轮里它派出的任务归到这张卡上
async fn tell_primary(id: i64, bot_id: i64, cid: i64, text: String) {
    let before = db::scalar_i64("select coalesce(max(id), 0) from messages", []);
    let rt = router::runtime(bot_id);
    let fut = rt.handle_user(&text, "", Some(cid), &[], None);
    tokio::pin!(fut);
    let r = loop {
        tokio::select! {
            r = &mut fut => break r,
            _ = tokio::time::sleep(std::time::Duration::from_millis(300)) => link_turn(id, bot_id, cid, before, &text),
        }
    };
    if let Err(e) = r {
        tracing::error!("看板卡片 #{id} 交给主 Bot 出错：{e:#}");
        server::broadcast(json!({"type": "error", "chat_id": cid, "message": t!("出错了：{e}", e = providers::brief(&e))}));
    }
    link_turn(id, bot_id, cid, before, &text);
    TURNS.lock().remove(&id);
    changed(id);
    watch(id);
}

/// 找到替你发的那条消息，把它之后这个 Bot 在这个对话里派的任务归到卡上。
/// 第一波的简单办法：同一个对话里同时还有上一轮没说完的话，那一轮派的任务也可能被算进来（第二波在 router.rs 里正式接）
fn link_turn(id: i64, bot_id: i64, cid: i64, before: i64, text: &str) {
    let mid = match load(id).and_then(|n| n.start_msg) {
        Some(m) => m,
        None => {
            let Some(m) = db::row(
                "select id from messages where chat_id=? and id>? and role='user' and content=? order by id limit 1",
                params![cid, before, text],
            ) else {
                return;
            };
            let m = m.i(0);
            db::exec("update needs set start_msg=? where id=?", params![m, id]);
            m
        }
    };
    let ids = db::rows(
        &format!("select id from messages where chat_id=? and id>? and bot_id=? and {DISPATCH} order by id"),
        params![cid, mid, bot_id],
    );
    for r in ids {
        link(id, r.i(0));
    }
}

/// 手动标完成、取消：停掉它下面还在做的任务
fn finish(id: i64, status: &str) -> Result<Value, ApiError> {
    need_or_404(id)?;
    let reason = if status == "done" { t!("用户在看板上把卡片标成了完成") } else { t!("用户在看板上取消了卡片") };
    for tid in running_tasks(id) {
        router::cancel(tid, &reason);
    }
    set(id, "status", &json!(status));
    changed(id);
    Ok(card(id).unwrap_or(Value::Null))
}

// ---------- 接口：卡片 ----------

async fn create_need(Body(d): Body) -> Res {
    let title = clean_title(&d["title"])?;
    let body = clean_body(&d["body"])?;
    let project_id = check_project(&d["project_id"])?;
    let assignee = check_bot(&d["assignee_bot_id"])?;
    let after = check_after(None, &d["after"])?;
    let id = insert(NewNeed { project_id, title, body, assignee, after, ..Default::default() });
    changed(id);
    if py::truthy(&d["start"]) {
        return ok(start(id)?);
    }
    ok(card(id).unwrap_or(Value::Null))
}

async fn get_need(Path(id): Path<i64>) -> Res {
    ok(detail(id).ok_or_else(|| ApiError::not_found(t!("没有这张卡片")))?)
}

async fn patch_need(Path(id): Path<i64>, Body(d): Body) -> Res {
    let n = need_or_404(id)?;
    // 先都检查一遍，再一起改
    let title = d.get("title").map(clean_title).transpose()?;
    let body = d.get("body").map(clean_body).transpose()?;
    let project = d.get("project_id").map(check_project).transpose()?;
    let assignee = d.get("assignee_bot_id").map(check_bot).transpose()?;
    let after = d.get("after").map(|v| check_after(Some(id), v)).transpose()?;
    if let Some(t) = title {
        if t != n.title {
            set(id, "title", &json!(t));
            set(id, "auto_title", &json!(0));
        }
    }
    if let Some(b) = body {
        set(id, "body", &json!(b));
    }
    if let Some(p) = project
        && p != n.project_id
    {
        // 移到别的项目：在新项目里重新编号，旧编号不回收
        let no = p.map(next_no);
        db::exec("update needs set project_id=?, no=?, updated=? where id=?", params![p, no, py::now(), id]);
    }
    if let Some(a) = assignee {
        set(id, "assignee_bot_id", &json!(a));
    }
    if let Some(a) = after {
        set(id, "after", &json!(py::dumps_u(&json!(a))));
    }
    changed(id);
    ok(card(id).unwrap_or(Value::Null))
}

async fn need_action(Path((id, action)): Path<(i64, String)>) -> Res {
    match action.as_str() {
        "start" => ok(start(id)?),
        "done" => ok(finish(id, "done")?),
        "cancel" => ok(finish(id, "cancelled")?),
        "archive" | "restore" => {
            need_or_404(id)?;
            set(id, "archived", &json!(if action == "archive" { 1 } else { 0 }));
            changed(id);
            ok(card(id).unwrap_or(Value::Null))
        }
        _ => Err(ApiError::not_found("Not Found")),
    }
}

/// GET /api/board：五列卡片、项目列表
async fn board(q: Q) -> Res {
    let archived = qs(&q, "archived") == Some("1");
    let mut wh: Vec<String> = vec!["n.archived=?".into()];
    let mut args: Vec<Value> = vec![json!(archived as i64)];
    match qs(&q, "project").map(str::trim) {
        Some("none") => wh.push("n.project_id is null".into()),
        Some(p) if !p.is_empty() => {
            wh.push("n.project_id=?".into());
            args.push(json!(p.parse::<i64>().map_err(|_| ApiError::bad(t!("没有这个项目")))?));
        }
        _ => {}
    }
    if let Some(s) = qs(&q, "q").map(|s| s.trim().to_lowercase()).filter(|s| !s.is_empty()) {
        wh.push(
            "(instr(lower(n.title), ?) > 0 or instr(lower(n.body), ?) > 0 or instr(lower(coalesce(p.key || '-' || n.no, '')), ?) > 0)"
                .into(),
        );
        args.extend([json!(s), json!(s), json!(s)]);
    }
    if let Some(f) = qs(&q, "from").and_then(|v| v.trim().parse::<f64>().ok()) {
        wh.push("n.created >= ?".into());
        args.push(json!(f));
    }
    if let Some(t) = qs(&q, "to").and_then(|v| v.trim().parse::<f64>().ok()) {
        wh.push("n.created <= ?".into());
        args.push(json!(t));
    }
    let limit = qi(&q, "limit").unwrap_or(200).clamp(1, 1000) as usize;
    let before = qi(&q, "before").filter(|b| *b > 0);
    let sql = format!("select {COLS} from needs n left join projects p on p.id=n.project_id where {} order by n.id desc", wh.join(" and "));
    let needs: Vec<Need> = db::rows(&sql, rusqlite::params_from_iter(args.iter().map(J))).iter().map(Need::from_row).collect();
    let mut cards: Vec<Value> = needs.iter().map(card_of).collect();
    if !archived {
        // 结束的卡片只留最近 KEEP_ENDED 张
        let mut ended: Vec<(f64, i64)> = cards
            .iter()
            .filter(|c| c["column"] == "done" || c["column"] == "cancelled")
            .map(|c| (c["updated"].as_f64().unwrap_or(0.0), c["id"].as_i64().unwrap_or(0)))
            .collect();
        ended.sort_by(|a, b| b.0.total_cmp(&a.0).then(b.1.cmp(&a.1)));
        let drop: HashSet<i64> = ended.iter().skip(KEEP_ENDED).map(|e| e.1).collect();
        cards.retain(|c| !drop.contains(&c["id"].as_i64().unwrap_or(0)));
    }
    if let Some(b) = before {
        cards.retain(|c| c["id"].as_i64().is_some_and(|i| i < b));
    }
    let more = cards.len() > limit;
    cards.truncate(limit);
    let mut cols: serde_json::Map<String, Value> = COLUMNS.iter().map(|c| (c.to_string(), json!([]))).collect();
    cards.sort_by(|a, b| b["updated"].as_f64().unwrap_or(0.0).total_cmp(&a["updated"].as_f64().unwrap_or(0.0)));
    for c in cards {
        let col = py::str_of(&c["column"]);
        if let Some(a) = cols.get_mut(&col).and_then(|v| v.as_array_mut()) {
            a.push(c);
        }
    }
    ok(json!({"columns": cols, "projects": list_projects(), "more": more}))
}

// ---------- 项目 ----------

static KEY_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Z0-9]{2,8}$").unwrap());

/// 项目编号：2–8 个大写字母数字（小写的转成大写）
pub fn clean_key(v: &Value) -> Result<String, String> {
    let k = py::str_of(v).trim().to_uppercase();
    if v.is_null() || !KEY_RE.is_match(&k) {
        return Err(t!("项目编号要是 2–8 个大写字母或数字"));
    }
    Ok(k)
}

const PCOLS: &str = "select p.id, p.key, p.name, p.brief, p.dir, p.repo, p.created,
    (select count(*) from needs n where n.project_id=p.id and n.archived=0) from projects p";

fn project_out(r: &Vec<Value>) -> Value {
    json!({"id": r[0], "key": r[1], "name": r[2], "brief": r.s(3), "dir": r.s(4), "repo": r.s(5), "count": r.i(7), "created": r[6]})
}

fn list_projects() -> Vec<Value> {
    db::rows(&format!("{PCOLS} order by p.key"), []).iter().map(project_out).collect()
}

fn project(id: i64) -> Option<Value> {
    db::row(&format!("{PCOLS} where p.id=?"), params![id]).map(|r| project_out(&r))
}

fn project_by_key(key: &str) -> Option<Value> {
    let k = key.trim();
    if k.is_empty() {
        return None;
    }
    db::row(&format!("{PCOLS} where upper(p.key)=upper(?) or p.name=?"), params![k, k]).map(|r| project_out(&r))
}

fn clean_name(v: &Value) -> Result<String, ApiError> {
    let n = if v.is_null() { String::new() } else { py::str_of(v).trim().to_string() };
    if n.is_empty() {
        return Err(ApiError::bad(t!("项目名字不能为空")));
    }
    if py::char_len(&n) > 100 {
        return Err(ApiError::bad(t!("项目名字太长（最多 {n} 字）", n = 100)));
    }
    Ok(n)
}

fn text_of(v: &Value) -> String {
    if v.is_null() { String::new() } else { py::str_of(v).trim().to_string() }
}

fn key_taken(key: &str, except: Option<i64>) -> bool {
    db::exists("select 1 from projects where upper(key)=? and id != ?", params![key, except.unwrap_or(0)])
}

async fn get_projects() -> Res {
    ok(json!(list_projects()))
}

async fn create_project(Body(d): Body) -> Res {
    let key = clean_key(&d["key"])?;
    let name = clean_name(&d["name"])?;
    if key_taken(&key, None) {
        return Err(ApiError::bad(t!("项目编号 {key} 已经有了", key = key)));
    }
    let id = db::insert(
        "insert into projects(key, name, brief, dir, repo, next_no, created) values(?,?,?,?,?,1,?)",
        params![key, name, text_of(&d["brief"]), text_of(&d["dir"]), text_of(&d["repo"]), py::now()],
    );
    projects_changed(id);
    ok(project(id).unwrap_or(Value::Null))
}

async fn patch_project(Path(id): Path<i64>, Body(d): Body) -> Res {
    project(id).ok_or_else(|| ApiError::not_found(t!("没有这个项目")))?;
    let key = d.get("key").map(clean_key).transpose()?;
    if let Some(k) = &key
        && key_taken(k, Some(id))
    {
        return Err(ApiError::bad(t!("项目编号 {key} 已经有了", key = k)));
    }
    let name = d.get("name").map(clean_name).transpose()?;
    if let Some(k) = key {
        db::exec("update projects set key=? where id=?", params![k, id]);
    }
    if let Some(n) = name {
        db::exec("update projects set name=? where id=?", params![n, id]);
    }
    for col in ["brief", "dir", "repo"] {
        if let Some(v) = d.get(col) {
            db::exec(&format!("update projects set {col}=? where id=?"), params![text_of(v), id]);
        }
    }
    projects_changed(id);
    ok(project(id).unwrap_or(Value::Null))
}

async fn delete_project(Path(id): Path<i64>) -> Res {
    project(id).ok_or_else(|| ApiError::not_found(t!("没有这个项目")))?;
    // 卡片留下，变成不属于项目
    db::exec("update needs set project_id=null, no=null, updated=? where project_id=?", params![py::now(), id]);
    db::exec("delete from projects where id=?", params![id]);
    projects_changed(id);
    ok(json!({"ok": true}))
}

pub fn routes() -> axum::Router {
    axum::Router::new()
        .route("/api/board", get(board))
        .route("/api/needs", post(create_need))
        .route("/api/needs/{id}", get(get_need).patch(patch_need))
        .route("/api/needs/{id}/{action}", post(need_action))
        .route("/api/projects", get(get_projects).post(create_project))
        .route("/api/projects/{id}", axum::routing::patch(patch_project).delete(delete_project))
}

// ---------- 给第二波留的（第一波还没人调） ----------

/// 派任务时归到卡上：给了 need_id 而且卡在就归到它上面；没有就新建一张（标题用 `need`，没写就取你的原话）。
/// `project` 是项目编号或名字。都没有（不是你提的事）返回 None
#[allow(dead_code)]
pub fn attach(task_id: i64, bot_id: i64, chat_id: i64, need: &str, need_id: Option<i64>, project: &str, user_text: &str) -> Option<i64> {
    if let Some(n) = need_id.and_then(load) {
        link(n.id, task_id);
        return Some(n.id);
    }
    let need = need.trim();
    let user_text = user_text.trim();
    if need.is_empty() && user_text.is_empty() {
        return None;
    }
    let (title, auto) = if need.is_empty() { (first_line(user_text, TITLE_MAX), true) } else { (py::head(need, TITLE_MAX), false) };
    let primary = bots::get(bot_id).is_some_and(|b| b["is_primary"] == true);
    let msg = db::scalar("select max(id) from messages where chat_id=? and role='user'", params![chat_id]).as_i64();
    let id = insert(NewNeed {
        project_id: project_by_key(project).and_then(|p| p["id"].as_i64()),
        title,
        auto_title: auto,
        source_text: user_text.to_string(),
        assignee: (!primary).then_some(bot_id),
        origin: msg.map(|m| (chat_id, m)),
        started: true,
        ..Default::default()
    });
    changed(id);
    link(id, task_id);
    Some(id)
}

fn column_zh(c: &str) -> &'static str {
    match c {
        "todo" => "待办",
        "doing" => "进行中",
        "waiting" => "等用户处理",
        "done" => "已完成",
        _ => "已取消",
    }
}

/// 给 Router 看的项目列表和进行中的卡（主 Bot 看全部，别的 Bot 只看交给它的）。都没有是空的
#[allow(dead_code)]
pub fn state_block(bot_id: i64) -> String {
    let primary = bots::get(bot_id).is_some_and(|b| b["is_primary"] == true);
    let mut out: Vec<String> = vec![];
    let ps = list_projects();
    if primary && !ps.is_empty() {
        let lines: Vec<String> = ps
            .iter()
            .map(|p| {
                let brief = first_line(&py::str_of(&p["brief"]), 80);
                format!(
                    "- {}「{}」{}",
                    py::str_of(&p["key"]),
                    py::str_of(&p["name"]),
                    if brief.is_empty() { String::new() } else { format!("：{brief}") }
                )
            })
            .collect();
        out.push(format!("项目：\n{}", lines.join("\n")));
    }
    let rows = db::rows(
        &format!(
            "select {COLS} from needs n left join projects p on p.id=n.project_id where n.archived=0 and n.status=''
          and (? or n.assignee_bot_id=?) order by n.updated desc limit 100"
        ),
        params![primary, bot_id],
    );
    let mut cards: Vec<String> = vec![];
    for n in rows.iter().map(Need::from_row) {
        let col = column_of(&n);
        if col == "done" || col == "cancelled" {
            continue;
        }
        let ts: Vec<String> = running_tasks(n.id).iter().map(|t| format!("#{t}")).collect();
        let tasks = if ts.is_empty() { String::new() } else { format!("（任务 {}）", ts.join("、")) };
        cards.push(format!("- {} [{}] {}{tasks}", n.key().unwrap_or_else(|| format!("卡片 #{}", n.id)), column_zh(col), n.title));
        if cards.len() >= 20 {
            break;
        }
    }
    if !cards.is_empty() {
        out.push(format!("进行中的卡：\n{}", cards.join("\n")));
    }
    out.join("\n")
}

static MENTION: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*([A-Za-z0-9]{2,8})-(\d+)(?:$|[\s，,：:。.])").unwrap());

/// 消息开头的卡片编号（`OPC-12 加上英国`）→ (卡, 它正在做的任务)
#[allow(dead_code)]
pub fn mention(text: &str) -> Option<(i64, Vec<i64>)> {
    let c = MENTION.captures(text)?;
    let no: i64 = c[2].parse().ok()?;
    let r = db::row(
        "select n.id from needs n join projects p on p.id=n.project_id where upper(p.key)=upper(?) and n.no=?",
        params![&c[1], no],
    )?;
    let id = r.i(0);
    Some((id, running_tasks(id)))
}

/// 前面的卡都完成了，这张还没开始过：可以自动开始。有取消、失败（不在“已完成”）的不开始
fn ready(n: &Need) -> bool {
    !n.archived
        && n.status.is_empty()
        && n.started.is_none()
        && !n.after.is_empty()
        && n.after.iter().all(|a| load(*a).is_some_and(|b| column_of(&b) == "done"))
}

/// 任务状态变了：重新算列、发事件；卡完成了，让等它的卡开始
#[allow(dead_code)]
pub fn task_changed(task_id: i64) {
    for nid in needs_of(task_id) {
        changed(nid);
        watch(nid);
        let Some(n) = load(nid) else { continue };
        if column_of(&n) != "done" {
            continue;
        }
        let waiting = db::rows(
            "select id from needs where archived=0 and status='' and started is null and after like ?",
            params![format!("%{nid}%")],
        );
        for w in waiting.iter().filter_map(|r| load(r.i(0))) {
            if w.after.contains(&nid) && ready(&w) {
                let _ = start(w.id);
            }
        }
    }
}

/// 任务所属项目的简介和目录，接在任务交代后面。不属于项目是空的
#[allow(dead_code)]
pub fn notes_for(task_id: i64) -> String {
    let Some(p) = needs_of(task_id).into_iter().filter_map(load).find_map(|n| n.project_id).and_then(project) else {
        return String::new();
    };
    let mut s = format!("\n\n这个任务属于项目「{}」（{}）。", py::str_of(&p["name"]), py::str_of(&p["key"]));
    let brief = py::str_of(&p["brief"]);
    if !brief.is_empty() {
        s.push_str(&format!("\n项目简介：{brief}"));
    }
    let dir = py::str_of(&p["dir"]);
    if !dir.is_empty() {
        s.push_str(&format!("\n项目目录：{dir}"));
    }
    let repo = py::str_of(&p["repo"]);
    if !repo.is_empty() {
        s.push_str(&format!("\n代码仓库：{repo}"));
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::memory;
    use std::sync::Once;

    /// 临时数据目录和配置（不碰 data/）
    fn setup() {
        static ONCE: Once = Once::new();
        ONCE.call_once(|| {
            let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
            let tmp = std::env::temp_dir().join("osa-board-test");
            let _ = std::fs::remove_dir_all(&tmp);
            std::fs::create_dir_all(tmp.join("data")).unwrap();
            let cfg = std::fs::read_to_string(root.join("config.yaml")).unwrap();
            std::fs::write(tmp.join("config.yaml"), cfg).unwrap();
            // SAFETY: 测试开头、还没有别的线程读环境变量
            unsafe {
                std::env::set_var("OSA_ROOT", &root);
                std::env::set_var("OSA_DATA_DIR", tmp.join("data"));
                std::env::set_var("OSA_CONFIG", tmp.join("config.yaml"));
            }
            assert!(crate::config::DB_PATH.starts_with(&tmp));
            memory::migrate();
            bots::migrate();
            migrate();
            bot("主", true);
        });
    }

    fn bot(name: &str, primary: bool) -> i64 {
        let id = db::insert(
            "insert into bots(name, avatar, instructions, is_primary, created) values(?, '{}', '', ?, 0)",
            params![name, primary as i64],
        );
        db::insert("insert into chats(kind, title, bot_ids, updated) values('direct', ?, ?, 0)", params![name, format!("[{id}]")]);
        id
    }

    fn primary() -> i64 {
        db::scalar_i64("select id from bots where is_primary=1", [])
    }

    fn proj(key: &str) -> i64 {
        db::scalar("select id from projects where key=?", params![key]).as_i64().unwrap_or_else(|| {
            db::insert(
                "insert into projects(key, name, brief, dir, repo, next_no, created) values(?, ?, '做官网', '/srv/site', '', 1, 0)",
                params![key, format!("{key} 项目")],
            )
        })
    }

    /// 造一条派发消息（冒充任务，不在内存里）
    fn task(chat: i64, bot: i64, status: &str) -> i64 {
        memory::add("task", "[任务已派发] 查东西", chat, Some(bot), &json!({"kind": "dispatch", "status": status}))
    }

    #[test]
    fn board_column_rules() {
        let t = |s: &str, w: Option<&str>| json!({"status": s, "waiting_kind": w});
        assert_eq!(column("", false, false, &[]), "todo");
        assert_eq!(column("", true, true, &[]), "doing");
        assert_eq!(column("", true, false, &[]), "waiting");
        assert_eq!(column("", true, false, &[t("running", None)]), "doing");
        assert_eq!(column("", true, false, &[t("done", None), t("running", Some("approval"))]), "waiting");
        assert_eq!(column("", true, false, &[t("done", None)]), "done");
        assert_eq!(column("", true, false, &[t("done", None), t("failed", None)]), "waiting");
        assert_eq!(column("", true, false, &[t("failed", None), t("done", None)]), "done");
        assert_eq!(column("", true, false, &[t("interrupted", None)]), "waiting");
        assert_eq!(column("", true, false, &[t("cancelled", None)]), "waiting");
        assert_eq!(column("done", true, false, &[t("running", None)]), "done");
        assert_eq!(column("cancelled", false, false, &[]), "cancelled");
    }

    #[test]
    fn board_key_rules() {
        setup(); // 报错的文字要读语言设置
        assert_eq!(clean_key(&json!("opc")).unwrap(), "OPC");
        assert_eq!(clean_key(&json!(" AB12 ")).unwrap(), "AB12");
        for bad in [json!("A"), json!("ABCDEFGHI"), json!("A-B"), json!("中文"), json!(""), Value::Null] {
            assert!(clean_key(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn board_attach_mention_notes_state() {
        setup();
        let p = primary();
        let other = bot("研究员", false);
        let pid = proj("TST");
        let chat = 9001;
        memory::add("user", "帮我写一份竞品报告", chat, Some(p), &json!({}));
        let t1 = task(chat, p, "failed");
        assert_eq!(attach(t1, p, chat, "", None, "", ""), None);
        let n = attach(t1, p, chat, "一份竞品报告", None, "tst", "帮我写一份竞品报告").unwrap();
        let c = card(n).unwrap();
        assert_eq!(c["title"], "一份竞品报告");
        assert_eq!(c["auto_title"], false);
        assert_eq!(c["source_text"], "帮我写一份竞品报告");
        assert_eq!(c["project_id"], pid);
        assert!(c["key"].as_str().unwrap().starts_with("TST-"));
        assert_eq!(c["assignee_bot_id"], Value::Null);
        assert_eq!(c["origin"]["chat_id"], chat);
        assert_eq!(c["tasks"][0]["id"], t1);
        assert_eq!(c["column"], "waiting"); // 最近一个任务失败了
        // 归到已有的卡
        let t2 = task(chat, p, "done");
        assert_eq!(attach(t2, p, chat, "", Some(n), "", "再查一下"), Some(n));
        let c = card(n).unwrap();
        assert_eq!(c["progress"], json!({"done": 1, "total": 2}));
        assert_eq!(c["column"], "done");
        // 没写 need：标题取原话
        let t3 = task(chat, other, "done");
        let m = attach(t3, other, chat, "", None, "", "  查一下天气\n明天的 ").unwrap();
        let c = card(m).unwrap();
        assert_eq!(c["title"], "查一下天气");
        assert_eq!(c["auto_title"], true);
        assert_eq!(c["key"], Value::Null);
        assert_eq!(c["assignee_bot_id"], other);

        // mention
        let key = card(n).unwrap()["key"].as_str().unwrap().to_string();
        let (id, running) = mention(&format!("{key} 加上英国")).unwrap();
        assert_eq!(id, n);
        assert!(running.is_empty());
        assert_eq!(mention(&format!("{} 小写也认", key.to_lowercase())).map(|x| x.0), Some(n));
        assert!(mention("TST-99999 没有这张").is_none());
        assert!(mention("随便说说 TST-1").is_none());
        assert!(mention(&format!("{key}0")).is_none_or(|x| x.0 != n));

        // notes_for
        let notes = notes_for(t1);
        assert!(notes.starts_with("\n\n这个任务属于项目「TST 项目」（TST）。"), "{notes}");
        assert!(notes.contains("项目目录：/srv/site") && notes.contains("项目简介：做官网"));
        assert_eq!(notes_for(t3), "");

        // state_block：主 Bot 看到项目；结束的卡不列
        let s = state_block(p);
        assert!(s.contains("- TST「TST 项目」：做官网"), "{s}");
        assert!(!s.contains(&key), "{s}");
        let t4 = task(chat, p, "failed");
        let k = attach(t4, p, chat, "修一下首页", None, "TST", "首页坏了").unwrap();
        let s = state_block(p);
        assert!(s.contains(&format!("{} [等用户处理] 修一下首页", card(k).unwrap()["key"].as_str().unwrap())), "{s}");
        // 别的 Bot 只看交给它的，不看项目列表
        let s = state_block(other);
        assert!(!s.contains("修一下首页") && !s.contains("项目："), "{s}");
    }

    #[test]
    fn board_after_and_task_changed() {
        setup();
        let p = primary();
        let chat = 9002;
        let a = insert(NewNeed { title: "前面的".into(), ..Default::default() });
        let b = insert(NewNeed { title: "后面的".into(), after: vec![a], ..Default::default() });
        assert!(check_after(Some(a), &json!([b])).is_err()); // 互相等
        assert!(check_after(Some(a), &json!([a])).is_err());
        assert!(check_after(Some(b), &json!([a])).is_ok());
        let cb = card(b).unwrap();
        assert_eq!(cb["blocked_by"][0]["id"], a);
        assert_eq!(cb["blocked_by"][0]["column"], "todo");
        // 前面的失败了：后面的不开始
        let t = task(chat, p, "failed");
        link(a, t);
        task_changed(t);
        assert!(load(b).unwrap().started.is_none());
        assert!(!ready(&load(b).unwrap()));
        // 前面的做完了：后面的可以开始
        let t2 = task(chat, p, "done");
        link(a, t2);
        assert!(ready(&load(b).unwrap()));
        assert_eq!(card(b).unwrap()["blocked_by"], json!([]));
    }
}
