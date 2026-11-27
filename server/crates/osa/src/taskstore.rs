//! 任务的过程存下来（task_runs）：做完的任务能接着说、失败的能重试，服务重启后接着做。
//! 也管：用户发给任务的话（收件箱、讨论串里回复任务）、Worker 的 ask_user、停服务时让任务停在步与步之间、
//! 重启时正在执行、说不准有没有做成的那一步（doubt）。Python 版没有这些（只有 Rust 版有）。

use crate::db::{self, RowExt};
use crate::registry::{Call, Context, Out, Tool, WORKER};
use crate::task::{Handoff, NewTask, Note, Reply, Task};
use crate::web::{ApiError, Body, Res, ok};
use crate::{approvals, bots, config, memory, py, router, server, tasklog};
use axum::extract::Path;
use axum::routing::post;
use futures::future::BoxFuture;
use parking_lot::Mutex;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock};

/// 重启后恢复：最近多久动过的任务才接着做
pub const RESUME_WITHIN_S: f64 = 6.0 * 3600.0;
/// 状态里列出的、还能接着说的已结束任务：最近多久的、最多几个
const RECENT_S: f64 = 24.0 * 3600.0;
const RECENT_N: i64 = 5;
/// 给 Router 看的：这个任务已经结束，但还能接着说
pub const CAN_TELL: &str = "（已结束，用户接着说它的事时用 tell_task）";

pub fn migrate() {
    db::batch(
        "create table if not exists task_runs(task_id integer primary key, bot_id integer, chat_id integer, status text,
            round integer default 1, msgs text, args text, updated real)",
    );
    // 重启时说不准有没有做成的写操作（Doubt 的 JSON 数组）：再重启一次、接着做、重试都还在
    db::add_column("task_runs", "doubts", "text");
    // 任务停下（取消、失败）时还开着的工具步：重试、接着做时照“说不准”处理（见 Resume）
    db::add_column("task_runs", "open_steps", "text");
}

// ---------- 存、取 ----------
fn reply_json(r: &Option<Reply>) -> Value {
    match r {
        None => Value::Null,
        Some(r) => json!({"bot": r.bot, "chat": r.chat, "depth": r.depth, "then": reply_json(&r.then.as_ref().map(|b| (**b).clone()))}),
    }
}

fn reply_of(v: &Value) -> Option<Reply> {
    if !v.is_object() {
        return None;
    }
    Some(Reply {
        bot: v["bot"].as_i64().unwrap_or(0),
        chat: v["chat"].as_i64().unwrap_or(0),
        depth: v["depth"].as_i64().unwrap_or(0),
        then: reply_of(&v["then"]).map(Box::new),
    })
}

/// 派发时登记（Heavy 也登记，只是不存过程：msgs 留空）
pub fn create(task: &Task, mac: Option<&Value>) {
    let (handoff, reply) = {
        let s = task.st.lock();
        (s.handoff.as_ref().map(|h| json!({"to": h.to, "note": h.note})).unwrap_or(Value::Null), reply_json(&s.reply))
    };
    let args = json!({"goal": task.goal, "mode": task.mode, "notes": task.notes, "background": task.background,
        "approval_chat_id": task.approval_chat_id, "depth": task.depth, "mac": mac.cloned().unwrap_or(Value::Null),
        "handoff": handoff, "reply": reply});
    // 重试从头做时也走这里：说不准的写操作（doubts）留着
    db::exec(
        "insert into task_runs(task_id, bot_id, chat_id, status, round, msgs, args, updated) values(?,?,?,'running',?,NULL,?,?)
        on conflict(task_id) do update set bot_id=excluded.bot_id, chat_id=excluded.chat_id, status='running', round=excluded.round,
            msgs=NULL, args=excluded.args, updated=excluded.updated",
        params![task.id, task.bot_id, task.chat_id, task.round, py::dumps_u(&args), py::now()],
    );
}

/// 截图这类大图换成一句话再存
fn slim(msgs: &[Value]) -> Vec<Value> {
    msgs.iter()
        .map(|m| {
            if m["content"].as_array().is_some_and(|a| a.iter().any(|p| p["type"] == "image_url")) {
                let mut m = m.clone();
                m["content"] = json!("（截图没有存下来，要看就重新截图）");
                m
            } else {
                m.clone()
            }
        })
        .collect()
}

/// Worker 每走一步存一次
pub fn save(task: &Task, msgs: &[Value]) {
    if !task.store {
        return;
    }
    db::exec("update task_runs set msgs=?, updated=? where task_id=?", params![py::dumps_u(&json!(slim(msgs))), py::now(), task.id]);
}

/// 一个工具调用做完就把结果接在存下的过程后面（整轮做完前重启，做完的那几个不用重做）
pub fn save_result(task: &Task, call_id: &Value, content: &str) {
    if !task.store {
        return;
    }
    let m = json!({"role": "tool", "tool_call_id": call_id, "content": content});
    db::exec(
        "update task_runs set msgs=json_insert(msgs, '$[#]', json(?)), updated=? where task_id=? and msgs is not null",
        params![py::dumps_u(&m), py::now(), task.id],
    );
}

pub fn finished(task: &Task, status: &str) {
    db::exec("update task_runs set status=?, updated=? where task_id=?", params![status, py::now(), task.id]);
}

pub struct Run {
    pub bot_id: i64,
    pub chat_id: i64,
    pub round: i64,
    pub msgs: Option<Vec<Value>>,
    pub args: Value,
}

pub fn load(task_id: i64) -> Option<Run> {
    let r = db::row("select bot_id, chat_id, round, msgs, args from task_runs where task_id=?", params![task_id])?;
    let msgs = if r[3].is_null() { None } else { serde_json::from_str::<Vec<Value>>(&r.s(3)).ok() };
    Some(Run { bot_id: r.i(0), chat_id: r.i(1), round: r.oi(2).unwrap_or(1).max(1), msgs, args: r.j(4) })
}

fn can_continue(task_id: i64) -> bool {
    db::exists("select 1 from task_runs where task_id=? and msgs is not null", params![task_id])
}

struct Dispatched {
    bot_id: i64,
    chat_id: i64,
    content: String,
    meta: Value,
}

fn dispatched(task_id: i64) -> Option<Dispatched> {
    let r =
        db::row(&format!("select bot_id, chat_id, content, meta from messages where id=? and {}", tasklog::DISPATCH), params![task_id])?;
    Some(Dispatched { bot_id: r.i(0), chat_id: r.i(1), content: r.s(2), meta: r.j(3) })
}

fn why_not(d: &Dispatched) -> String {
    if d.meta["mode"] == "heavy" {
        t!("Heavy 模式的任务没有存下过程，不能接着做；可以重新派一个任务")
    } else {
        t!("这个任务没有存下过程（升级前派的），不能接着做；可以重新派一个任务")
    }
}

// ---------- 接着做 ----------
/// 结束了的任务从存下的过程接着做一轮。resume：服务重启后恢复同一轮（不加轮次，末尾那一轮照第 2 点处理）
fn continue_round(
    task_id: i64,
    run: Run,
    notes: Vec<Note>,
    thread: Option<i64>,
    resume: Option<Vec<StepInfo>>,
) -> Result<Arc<Task>, ApiError> {
    let bot = bots::get(run.bot_id).ok_or_else(|| ApiError::bad(t!("这个任务的 Bot 已经删除了")))?;
    let msgs = run.msgs.unwrap_or_default();
    let again = resume.is_some();
    // 末尾那一轮没拿全结果的 tool_calls：重启恢复的重新执行（只读的）或记成说不准；
    // 重试、接着做的不重新执行，给个“没有执行 / 没做完”的结果让模型重新决定，写操作同样记成说不准
    let plan = match resume {
        Some(steps) => Resume { steps, why: "restart", rerun: true },
        None => {
            let status = db::scalar("select status from task_runs where task_id=?", params![task_id]);
            let (steps, why) = stopped_steps(task_id, status.as_str() == Some("interrupted"));
            Resume { steps, why, rerun: false }
        }
    };
    let a = &run.args;
    let round = if again { run.round } else { run.round + 1 };
    let result_chat = if again { a["result_chat"].as_i64() } else { thread };
    let mac = &a["mac"];
    let task = Task::new(NewTask {
        id: task_id,
        goal: a["goal"].as_str().unwrap_or("").to_string(),
        mode: "single".into(),
        bot_id: run.bot_id,
        chat_id: run.chat_id,
        approval_chat_id: a["approval_chat_id"].as_i64().unwrap_or_else(|| bot["chat_id"].as_i64().unwrap_or(0)),
        background: py::truthy(&a["background"]),
        handoff: if again && a["handoff"].is_object() {
            Some(Handoff { to: a["handoff"]["to"].as_i64().unwrap_or(0), note: py::str_of(&a["handoff"]["note"]) })
        } else {
            None
        },
        reply: if again { reply_of(&a["reply"]) } else { None },
        depth: a["depth"].as_i64().unwrap_or(0),
        notes: a["notes"].as_str().unwrap_or("").to_string(),
        mac: mac["id"].as_i64().unwrap_or(0),
        mac_name: if mac.is_object() { py::str_of(&mac["name"]) } else { String::new() },
        round,
        store: true,
        seed: Some(msgs),
        result_chat,
        ..Default::default()
    });
    if let Some(h) = &task.st.lock().handoff {
        crate::groups::expect(task.chat_id, h.to);
    }
    *task.resume_steps.lock() = Some(plan);
    db::exec("update task_runs set open_steps=null where task_id=?", params![task_id]);
    // 上一轮（或重启前）还没了结的说不准的写操作带过来：那一步到底做没做成仍然没人确认过
    *task.doubts.lock() = load_doubts(task_id);
    task.inbox.lock().extend(notes);
    db::exec(
        "update task_runs set round=?, status='running', updated=?, args=json_set(coalesce(args, '{}'), '$.result_chat', ?) where task_id=?",
        params![round, py::now(), result_chat, task_id],
    );
    memory::update_meta(task_id, &json!({"status": "running"}));
    router::runtime(run.bot_id).start(task.clone());
    Ok(task)
}

/// 没存过程的任务（Heavy、升级前的）重试：从头做
fn fresh(task_id: i64, d: &Dispatched) -> Result<Arc<Task>, ApiError> {
    let bot = bots::get(d.bot_id).ok_or_else(|| ApiError::bad(t!("这个任务的 Bot 已经删除了")))?;
    let mode = d.meta["mode"].as_str().unwrap_or("single").to_string();
    let goal = d.content.strip_prefix("[任务已派发]").unwrap_or(&d.content).trim().to_string();
    let round = load(task_id).map(|r| r.round + 1).unwrap_or(2);
    let task = Task::new(NewTask {
        id: task_id,
        goal,
        mode: mode.clone(),
        bot_id: d.bot_id,
        chat_id: d.chat_id,
        approval_chat_id: bot["chat_id"].as_i64().unwrap_or(0),
        round,
        store: mode == "single",
        ..Default::default()
    });
    create(&task, None);
    *task.doubts.lock() = load_doubts(task_id);
    memory::update_meta(task_id, &json!({"status": "running"}));
    router::runtime(d.bot_id).start(task.clone());
    Ok(task)
}

/// 一轮做完（或被取消）：收件箱里还有话（Worker 交结果时才到的），就带着它们接着做
pub fn after_round(task: &Arc<Task>) {
    let notes = {
        let mut ib = task.inbox.lock();
        task.closed.store(true, Ordering::SeqCst);
        std::mem::take(&mut *ib)
    };
    if notes.is_empty() || !task.store || task.status() == "cancelled" {
        return;
    }
    let thread = task.st.lock().result_chat;
    if let Some(run) = load(task.id).filter(|r| r.msgs.is_some())
        && let Err(e) = continue_round(task.id, run, notes, thread, None)
    {
        tracing::warn!("任务 #{} 没能接着做：{:?}", task.id, e.detail);
    }
}

/// 话是怎么交给任务的
#[derive(PartialEq)]
enum How {
    Inbox,
    Answer,
    Continue,
}

fn post_followup(bot_id: i64, chat_id: i64, task_id: i64, text: &str) {
    router::runtime(bot_id).add(
        "task",
        &format!("[给任务 #{task_id} 的话]\n{text}"),
        Some(chat_id),
        json!({"kind": "followup", "task_id": task_id}),
    );
}

/// 用户发给任务的一句话：在做的下一步之前交给它，在等回答的就是回答，结束了的接着做一轮
/// thread：在讨论串里回复的（之后的结果也发进讨论串）；post：给任务的话存在哪个对话（没给是任务的对话）
fn deliver_in(task_id: i64, text: &str, thread: Option<i64>, post: Option<i64>) -> Result<(Arc<Task>, How), ApiError> {
    let text = text.trim();
    if text.is_empty() {
        return Err(ApiError::bad(t!("要说的话不能为空")));
    }
    let d = dispatched(task_id).ok_or_else(|| ApiError::not_found(t!("没有这个任务")))?;
    if let Some(t) = router::task(task_id).filter(|t| !t.closed.load(Ordering::SeqCst)) {
        let question = t.waiting_kind().as_deref() == Some("question") && t.status() == "running";
        let aid = t.st.lock().waiting_approval;
        if question && let Some(aid) = aid {
            post_followup(t.bot_id, thread.or(post).unwrap_or(t.chat_id), task_id, text);
            if let Some(th) = thread {
                t.st.lock().result_chat = Some(th);
            }
            approvals::resolve(aid, true, text, false, &Value::Null, None, None);
            return Ok((t, How::Answer));
        }
        let queued = {
            let mut ib = t.inbox.lock();
            if t.closed.load(Ordering::SeqCst) {
                false
            } else {
                ib.push(Note::user(text));
                true
            }
        };
        if queued {
            post_followup(t.bot_id, thread.or(post).unwrap_or(t.chat_id), task_id, text);
            if let Some(th) = thread {
                t.st.lock().result_chat = Some(th);
            }
            return Ok((t, How::Inbox));
        }
    }
    if HELD.lock().contains_key(&task_id) {
        return Err(ApiError::bad(t!("服务刚重启，这个任务正在恢复，稍后再说")));
    }
    let run = load(task_id).filter(|r| r.msgs.is_some()).ok_or_else(|| ApiError::bad(why_not(&d)))?;
    post_followup(run.bot_id, thread.or(post).unwrap_or(run.chat_id), task_id, text);
    let t = continue_round(task_id, run, vec![Note::user(text)], thread, None)?;
    Ok((t, How::Continue))
}

fn deliver(task_id: i64, text: &str, thread: Option<i64>) -> Result<(Arc<Task>, How), ApiError> {
    deliver_in(task_id, text, thread, None)
}

/// 消息以卡片编号开头、那张卡正好一个任务在做（server::safe_handle）：交给那个任务，Bot 不被叫醒。
/// 给任务的话存在用户发消息的那个对话里。交出去了返回 true
pub fn tell_from(task_id: i64, text: &str, chat_id: i64) -> bool {
    match deliver_in(task_id, text, None, Some(chat_id)) {
        Ok(_) => true,
        Err(e) => {
            tracing::warn!("消息交给任务 #{task_id} 没成：{:?}", e.detail);
            false
        }
    }
}

pub fn message(task_id: i64, text: &str) -> Result<Value, ApiError> {
    let (t, _) = deliver(task_id, text, None)?;
    Ok(t.public())
}

/// 失败、取消、中断的任务从停下的地方接着做；没存过程的从头做
pub fn retry(task_id: i64) -> Result<Value, ApiError> {
    let d = dispatched(task_id).ok_or_else(|| ApiError::not_found(t!("没有这个任务")))?;
    if router::task(task_id).is_some_and(|t| !t.closed.load(Ordering::SeqCst)) || HELD.lock().contains_key(&task_id) {
        return Err(ApiError::bad(t!("任务还在做，不用重试")));
    }
    let status = d.meta["status"].as_str().unwrap_or("done").to_string();
    if status == "done" {
        return Err(ApiError::bad(t!("任务已经做完了；要接着做，给它发一句话")));
    }
    let t = match load(task_id).filter(|r| r.msgs.is_some()) {
        Some(run) => continue_round(
            task_id,
            run,
            vec![Note::system(
                &format!("上次没做完（{status}），用户让你从停下的地方接着做。先看看做到哪了，做过的不要重复。"),
                &t!("从停下的地方重试"),
            )],
            None,
            None,
        )?,
        None => fresh(task_id, &d)?,
    };
    Ok(t.public())
}

/// Router 的 tell_task：把用户接着说的话转给它派过的任务
pub async fn tell(bot_id: i64, task_id: i64, text: &str) -> String {
    match dispatched(task_id) {
        None => return format!("没转：没有任务 #{task_id}"),
        Some(d) if d.bot_id != bot_id => return format!("没转：任务 #{task_id} 不是你派的"),
        _ => {}
    }
    match deliver(task_id, text, None) {
        Ok((_, How::Inbox)) => format!("已转给任务 #{task_id}，它做下一步之前会看到"),
        Ok((_, How::Answer)) => format!("任务 #{task_id} 正在等用户回答问题，已把这句话作为回答交给它"),
        Ok((t, How::Continue)) => {
            format!("任务 #{task_id} 已经结束，已让它带着这句话接着做（第 {} 轮），做完结果会自动发到对话里", t.round)
        }
        Err(e) => format!("没转：{}。要做的话用 dispatch 派一个新任务", e.detail.as_str().unwrap_or("")),
    }
}

/// 讨论串里的消息：根消息是任务的派发或结果消息、而且这个任务还能接着说时，交给任务（不叫醒 Router），返回 true
pub fn thread_send(chat: &Value, text: &str) -> bool {
    // 讨论串的 chat 带着上一级的 kind（direct / group），认 parent 和 chats 表
    if !py::truthy(&chat["parent"]) {
        return false;
    }
    let cid = chat["id"].as_i64().unwrap_or(0);
    let Some((_, root)) = memory::thread_of(cid) else { return false };
    let Some(r) = db::row("select role, meta from messages where id=?", params![root]) else { return false };
    if r.s(0) != "task" {
        return false;
    }
    let meta = r.j(1);
    let tid = match meta["kind"].as_str() {
        Some("dispatch") => root,
        Some("result") => meta["task_id"].as_i64().unwrap_or(0),
        _ => return false,
    };
    let live = router::task(tid).is_some_and(|t| !t.closed.load(Ordering::SeqCst));
    if !live && !can_continue(tid) {
        return false;
    }
    if let Err(e) = deliver(tid, text, Some(cid)) {
        server::broadcast(json!({"type": "error", "chat_id": cid, "message": e.detail}));
    }
    true
}

/// 状态里列出的、最近结束还能接着说的任务（不在 skip 里的）
pub fn recent_lines(bot_id: i64, skip: &[i64]) -> Vec<String> {
    db::rows(
        &format!(
            "select id, content, meta from messages where bot_id=? and {} and json_extract(meta, '$.status')!='running'
            and id in (select task_id from task_runs where msgs is not null and updated>?) order by id desc limit ?",
            tasklog::DISPATCH
        ),
        params![bot_id, py::now() - RECENT_S, RECENT_N],
    )
    .into_iter()
    .filter(|r| !skip.contains(&r.i(0)))
    .map(|r| {
        let m = r.j(2);
        let c = r.s(1);
        let goal = c.strip_prefix("[任务已派发]").unwrap_or(&c).trim().to_string();
        format!(
            "#{} [{}] single 步数={} {}{CAN_TELL}",
            r.i(0),
            m["status"].as_str().unwrap_or("done"),
            py::str_of(&m["steps"]),
            py::head(&goal, 50)
        )
    })
    .collect()
}

// ---------- 重启 ----------
/// 重启时还开着的一步（工具调用开始了、没有结果）
#[derive(Clone, Debug)]
pub struct StepInfo {
    pub name: String,
    pub detail: String,
    pub ts: f64,
    pub status: String,
}

impl StepInfo {
    fn to_json(&self) -> Value {
        json!({"name": self.name, "detail": self.detail, "ts": self.ts, "status": self.status})
    }
    fn of(v: &Value) -> StepInfo {
        StepInfo {
            name: py::str_of(&v["name"]),
            detail: py::str_of(&v["detail"]),
            ts: v["ts"].as_f64().unwrap_or(0.0),
            status: py::str_of(&v["status"]),
        }
    }
}

/// 末尾那一轮没拿全结果的 tool_calls 怎么处理（worker::resume_tail，三条路共用）。
/// why：stall（卡住超时）、stopped（任务被停下）、restart（服务重启）；rerun：没开始的、在等确认的、只读的重新执行，
/// 不然给个“没有执行 / 没做完”的结果让模型重新决定。开始了、没有结果的写操作一律记成说不准
#[derive(Clone, Debug)]
pub struct Resume {
    pub steps: Vec<StepInfo>,
    pub why: &'static str,
    pub rerun: bool,
}

/// 这个任务现在还开着的工具步（开始了、没有结果；在等确认的状态是 waiting）
pub fn open_steps(task_id: i64) -> Vec<StepInfo> {
    db::rows(
        "select name, detail, ts, status from task_steps where task_id=? and kind='tool' and status in ('running', 'waiting') order by id",
        params![task_id],
    )
    .into_iter()
    .map(|r| StepInfo { name: r.s(0), detail: r.s(1), ts: r.f(2), status: r.s(3) })
    .collect()
}

/// 任务停下（取消、失败）时、开着的步结束之前调：存下还开着的工具步，重试、接着做时用；
/// 正在执行的写操作在过程里注明一句（没人重试的话就到此为止）
pub fn keep_open_steps(task: &Task) {
    if !task.store {
        return;
    }
    let steps = open_steps(task.id);
    let js: Vec<Value> = steps.iter().map(StepInfo::to_json).collect();
    db::exec("update task_runs set open_steps=? where task_id=?", params![py::dumps_u(&json!(js)), task.id]);
    for s in steps.iter().filter(|s| s.status == "running" && crate::worker::is_write(&s.name, &s.detail)) {
        tasklog::Log::new(task, "")
            .note(&t!("任务停下时 {tool} 正在执行，说不准有没有做成；重试或接着做时会先让 Bot 查，再做要先问你", tool = s.name), "error");
    }
}

/// 重试、接着做：上次停下时存下的开着的步；没存（升级前、服务重启后没接着做被标成中断的）就从过程里找那时还开着、后来被标成中断的
fn stopped_steps(task_id: i64, interrupted: bool) -> (Vec<StepInfo>, &'static str) {
    let saved = db::scalar("select open_steps from task_runs where task_id=?", params![task_id]);
    if let Some(v) = saved.as_str().and_then(|s| serde_json::from_str::<Value>(s).ok()) {
        return (v.as_array().map(|a| a.iter().map(StepInfo::of).collect()).unwrap_or_default(), "stopped");
    }
    if interrupted {
        let steps = db::rows(
            "select name, detail, ts, 'running' from task_steps where task_id=? and kind='tool' and status='interrupted' order by id",
            params![task_id],
        )
        .into_iter()
        .map(|r| StepInfo { name: r.s(0), detail: r.s(1), ts: r.f(2), status: r.s(3) })
        .collect();
        return (steps, "restart");
    }
    (vec![], "stopped")
}

/// 卡住时取消了这一步：已经做完的调用的结果（save_result 一个个存下的）
pub fn saved_results(task: &Task, calls: &[Value]) -> Vec<Value> {
    let ids: Vec<&Value> = calls.iter().map(|c| &c["id"]).collect();
    let Some(msgs) = load(task.id).and_then(|r| r.msgs) else { return vec![] };
    let Some(ai) = msgs.iter().rposition(|m| m["role"] == "assistant") else { return vec![] };
    msgs[ai + 1..].iter().filter(|m| m["role"] == "tool" && ids.contains(&&m["tool_call_id"])).cloned().collect()
}

/// 启动时挑出来、等着恢复的任务，和它们还开着的工具步
static HELD: LazyLock<Mutex<HashMap<i64, Vec<StepInfo>>>> = LazyLock::new(Default::default);

/// 重启前正在做、存着过程、最近动过的任务（tasklog::startup 在把别的标成中断之前调）
pub fn resumable() -> Vec<i64> {
    let ids: Vec<i64> = db::rows(
        &format!(
            "select id from messages where {} and json_extract(meta, '$.status')='running'
            and id in (select task_id from task_runs where msgs is not null and updated>?) order by id",
            tasklog::DISPATCH
        ),
        params![py::now() - RESUME_WITHIN_S],
    )
    .into_iter()
    .map(|r| r.i(0))
    .collect();
    let mut held = HELD.lock();
    for id in &ids {
        held.insert(*id, open_steps(*id));
    }
    ids
}

pub fn hold(ids: Vec<i64>) {
    HELD.lock().retain(|k, _| ids.contains(k));
}

/// 应用、聊天软件都起好以后（lib.rs 的 background）把挑出来的任务接着做
pub async fn resume() {
    if HELD.lock().is_empty() {
        return;
    }
    wait_apps().await;
    let held: Vec<(i64, Vec<StepInfo>)> = {
        let h = HELD.lock();
        let mut v: Vec<_> = h.iter().map(|(k, s)| (*k, s.clone())).collect();
        v.sort_by_key(|x| x.0);
        v
    };
    for (id, steps) in held {
        let r = match load(id).filter(|r| r.msgs.is_some()) {
            Some(run) => continue_round(
                id,
                run,
                vec![Note::system("服务重启过，接着做。做过的事不要重复。", &t!("服务重启过，接着做"))],
                None,
                Some(steps),
            )
            .map(|_| ()),
            None => Err(ApiError::bad("")),
        };
        HELD.lock().remove(&id);
        if r.is_err() {
            memory::update_meta(id, &json!({"status": "interrupted"}));
            finished_id(id, "interrupted");
        }
    }
}

/// 应用是后台连的：等启用了的都连上（或者连不上），最多 20 秒，不然恢复的任务一上来调应用的工具就是“未知工具”
async fn wait_apps() {
    let end = py::now() + 20.0;
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    while py::now() < end {
        let busy = crate::mcp_hub::public().iter().any(|a| a["enabled"] == true && a["state"] == "connecting");
        if !busy {
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    }
}

fn finished_id(task_id: i64, status: &str) {
    db::exec("update task_runs set status=?, updated=? where task_id=?", params![status, py::now(), task_id]);
}

/// 重启时说不准有没有做成的一次写操作。存在 task_runs.doubts 里，每次变化都存
#[derive(Clone, Debug)]
pub struct Doubt {
    pub tool: String,
    pub args: String,
    pub started: f64,
    /// 怎么了结的：redo（再做了一次）、done（之前已经做成）、stop（任务停下）、skip（不做了）；None 还没了结
    pub settled: Option<String>,
    /// 了结以后已经在某一轮的结果里注明过（之后不再带）
    pub noted: bool,
    /// 为什么说不准：stall（卡住超时）、stopped（任务被停下）、restart（服务重启）
    pub why: String,
}

impl Doubt {
    fn to_json(&self) -> Value {
        json!({"tool": self.tool, "args": self.args, "started": self.started, "settled": self.settled, "noted": self.noted, "why": self.why})
    }

    fn of(v: &Value) -> Option<Doubt> {
        Some(Doubt {
            tool: v["tool"].as_str()?.to_string(),
            args: v["args"].as_str().unwrap_or("").to_string(),
            started: v["started"].as_f64().unwrap_or(0.0),
            settled: v["settled"].as_str().map(String::from),
            noted: v["noted"] == true,
            why: v["why"].as_str().unwrap_or("restart").to_string(),
        })
    }
}

/// 存下任务现在的说不准清单（记下、了结、注明以后都调）
pub fn save_doubts(task: &Task) {
    let ds: Vec<Value> = task.doubts.lock().iter().map(Doubt::to_json).collect();
    db::exec("update task_runs set doubts=? where task_id=?", params![py::dumps_u(&json!(ds)), task.id]);
}

/// 读回存下的清单：了结并且注明过的不再带
pub fn load_doubts(task_id: i64) -> Vec<Doubt> {
    let Some(r) = db::row("select doubts from task_runs where task_id=?", params![task_id]) else { return vec![] };
    if r[0].is_null() {
        return vec![];
    }
    let v: Value = serde_json::from_str(&r.s(0)).unwrap_or(json!([]));
    v.as_array().map(|a| a.iter().filter_map(Doubt::of).filter(|d| !d.noted).collect()).unwrap_or_default()
}

/// 说不准的那一步给模型的工具结果
pub fn doubt_text(why: &str, tool: &str, args: &str, started: f64) -> String {
    let when = match why {
        "stall" => "这一步卡住超时、没有做完就被打断了",
        "stopped" => "这一步没有做完任务就被停下了",
        _ => "这一步在服务重启时正在执行",
    };
    format!(
        "{when}，不知道有没有做成：工具 {tool}，参数 {args}，{} 开始执行。\
先用只读的办法查一下它有没有做成（比如已发送的邮件、文件在不在、页面现在的样子、记录有没有多一条），查到了就照查到的接着做；\
查不出来、确实要再做一次的，照常调用这个工具，会先请用户确认。",
        py::strftime_at(started, "%H:%M:%S")
    )
}

/// 任务结果后面注明：有一步当时说不准，最后是怎么处理的
/// 已经了结的标成注明过；还没了结的这一轮也注明一句，下一轮（接着做、重试）照样带着
pub fn doubt_note(task: &Task) -> String {
    let ds: Vec<Doubt> = {
        let mut ds = task.doubts.lock();
        let out: Vec<Doubt> = ds.iter().filter(|d| !d.noted).cloned().collect();
        for d in ds.iter_mut() {
            if d.settled.is_some() {
                d.noted = true;
            }
        }
        out
    };
    if ds.is_empty() {
        return String::new();
    }
    save_doubts(task);
    ds.iter()
        .map(|d| {
            let how = match d.settled.as_deref() {
                Some("redo") => t!("用户确认后又做了一次"),
                Some("done") => t!("用户确认之前已经做成，没有再做"),
                Some("stop") => t!("用户让任务停下"),
                Some(_) => t!("用户决定不再做"),
                None => t!("之后没有再做这一步"),
            };
            let note = match d.why.as_str() {
                "stall" => t!("（注：{tool} 这一步卡住超时被打断，说不准有没有做成；{how}。）", tool = d.tool, how = how),
                "stopped" => t!("（注：任务被停下时 {tool} 这一步正在执行，说不准有没有做成；{how}。）", tool = d.tool, how = how),
                _ => t!("（注：服务重启时 {tool} 这一步正在执行，说不准有没有做成；{how}。）", tool = d.tool, how = how),
            };
            format!("\n\n{note}")
        })
        .collect()
}

// ---------- 停服务 ----------
static STOPPING: AtomicBool = AtomicBool::new(false);

/// 正在停服务：Worker 不再开始新的一步
pub fn stopping() -> bool {
    STOPPING.load(Ordering::SeqCst)
}

pub fn begin_stop() {
    STOPPING.store(true, Ordering::SeqCst);
}

/// Worker 在停服务时停在步与步之间：任务状态留着 running，启动后恢复
#[derive(Debug)]
pub struct Parked;

impl std::fmt::Display for Parked {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "服务正在停止，任务停在步与步之间")
    }
}

impl std::error::Error for Parked {}

/// 停服务：等存着过程的任务都停到步与步之间（在等确认的不用等），最多 limits.shutdown_wait_s 秒
pub async fn stop_tasks() {
    begin_stop();
    let wait = config::get_f64("limits.shutdown_wait_s", 30.0).max(0.0);
    let end = py::now() + wait;
    loop {
        let busy = router::tasks().into_iter().any(|t| {
            t.store
                && !t.parked.load(Ordering::SeqCst)
                && !t.closed.load(Ordering::SeqCst)
                && t.status() == "running"
                && t.st.lock().waiting_approval.is_none()
        });
        if !busy || py::now() >= end {
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
}

// ---------- ask_user ----------
fn ask_schema() -> Value {
    json!({"type": "function", "function": {"name": "ask_user",
        "description": "问用户一个问题，等用户回答后接着做（回答前任务暂停）。只在缺少必须由用户决定的信息（选哪个、要不要、具体的偏好），自己查不到、也不该替用户猜时用；能自己查证或按常理判断的不要问。给几个候选答案时填 options，用户可以直接点选，也可以自己写。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "要问的问题，写清楚为什么要问、问的是什么"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "候选答案（可选，最多 8 个）"}},
            "required": ["question"]}}})
}

/// 正式的 Worker 才能问（Heavy 的草稿不问）
fn ask_when(env: &Value) -> bool {
    py::truthy(&env["pages"])
}

fn ask_progress(a: &Value) -> String {
    t!("问你：{question}", question = py::head(a["question"].as_str().unwrap_or(""), 60))
}

pub fn register(ctx: &Context) {
    let run: crate::registry::RunFn = Arc::new(|c: Call| -> BoxFuture<'static, Out> { Box::pin(tool_ask_user(c)) });
    let mut tool = Tool::new("ask_user", ask_schema(), run);
    tool.where_ = vec![WORKER];
    // Router 靠这句知道 Worker 能在任务中途问用户（列在“后台 Worker 当前能用的工具”里）
    tool.describe = "任务中途向用户提一个问题（在对话里出一张卡片，可以带选项），等用户回答后接着做；要用户拿主意的事不用事先问清，写进 goal 让任务到时候问".into();
    tool.when = Some(ask_when);
    tool.progress = Some(Arc::new(ask_progress));
    if let Err(e) = ctx.tool(tool) {
        tracing::error!("{e}");
    }
}

async fn tool_ask_user(c: Call) -> Out {
    let Some(task) = c.task.clone() else { return "ask_user 只能在任务里用".into() };
    let q = c.args["question"].as_str().unwrap_or("").trim().to_string();
    if q.is_empty() {
        return "没问：question 是空的".into();
    }
    let opts: Vec<String> = c.args["options"]
        .as_array()
        .map(|a| a.iter().map(py::str_of).map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).take(8).collect())
        .unwrap_or_default();
    let (ok, note, _) = approvals::request_with(
        &task,
        "ask_user",
        &json!({"问题": q}),
        approvals::Req { kind: "question", ..Default::default() },
        &json!({"question": q, "options": opts}),
    )
    .await;
    let note = note.trim().to_string();
    if ok && !note.is_empty() {
        format!("用户回答：{note}").into()
    } else if ok {
        "用户看过了，但没有写回答。按你能确定的接着做。".into()
    } else {
        format!("用户没有回答（{note}）。按你能确定的接着做，做不了的在结果里说明还缺什么。").into()
    }
}

// ---------- 接口 ----------
async fn api_message(Path(task_id): Path<i64>, Body(d): Body) -> Res {
    let t = message(task_id, d["text"].as_str().unwrap_or(""))?;
    ok(json!({"ok": true, "task": t}))
}

async fn api_retry(Path(task_id): Path<i64>) -> Res {
    let t = retry(task_id)?;
    ok(json!({"ok": true, "task": t}))
}

pub fn routes() -> axum::Router {
    axum::Router::new().route("/api/tasks/{task_id}/message", post(api_message)).route("/api/tasks/{task_id}/retry", post(api_retry))
}
