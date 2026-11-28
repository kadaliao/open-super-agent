//! 文档：交给 Open Agent Docs（OAD）。设计见 docs/plans/oad.md。
//!
//! - OAD 是单独的进程（服务器上是 compose 里的 oad 服务，桌面 App 自己带一个），只听内网；地址和 header 登录的密钥从
//!   OSA_DOCS_URL、OSA_DOCS_SECRET 来，没设就没有文档功能。
//! - 浏览器只连 osa-server：`/api/v1/*`（含协同编辑的 WebSocket）先过 OSA 的登录，再带上密钥和用户转给 OAD；`/s/*` 是分享页，不用登录。
//! - 每个 Bot 在 OAD 里是一个 Agent（token 加密存在 docs_agents），用自己的 token 连 OAD 的 /mcp，工具注册成 docs__<工具>。
//! - OSA 自己有一个 admin 的 Agent（bot_id=0），用它的通知流收整个工作区的评论通知，叫醒对应的 Bot。

use crate::db::{self, RowExt};
use crate::dm::{Turn, Woke};
use crate::mcp_client::{Session, Transport};
use crate::registry::{self, Call, Context, Out, ROUTER, Tool, WORKER};
use crate::{bots, memory, py, server, vault};
use axum::body::Body;
use axum::extract::ws::{Message as AxMsg, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Request};
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get};
use base64::Engine;
use futures::{SinkExt, StreamExt};
use open_agent_docs as oad;
use parking_lot::Mutex;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

/// OSA 的用户在 OAD 里的编号（OSA 只有一个用户；第一个出现的用户自动是工作区所有者）
pub const OWNER: &str = "owner";
/// OSA 自己那个 admin 的 Agent 在 docs_agents 里的 bot_id
const SELF_ID: i64 = 0;
/// 对话里（Router）也给的工具：读、找、按块改、建页面、回评论（被评论叫醒时多半就是改几句）。别的只给 Worker
const ROUTER_TOOLS: [&str; 7] = ["search_pages", "list_pages", "read_page", "edit_page", "create_page", "list_comments", "comment"];
/// 不注册的工具：通知由 OSA 收
const SKIP_TOOLS: [&str; 1] = ["my_notifications"];

pub struct Conf {
    pub url: String,
    pub secret: String,
}

/// 文档服务在哪：OSA_DOCS_URL + OSA_DOCS_SECRET 是连别处已经在跑的；没设时找到 open-agent-docs 程序就自己起一个（见 spawn）
static CONF: std::sync::OnceLock<Conf> = std::sync::OnceLock::new();

fn conf() -> Option<&'static Conf> {
    CONF.get()
}

pub fn configured() -> bool {
    CONF.get().is_some()
}

/// 连上了、工具也注册好了
pub fn ready() -> bool {
    *READY.lock()
}

static READY: LazyLock<Mutex<bool>> = LazyLock::new(Default::default);

pub fn migrate() {
    db::batch(
        "create table if not exists docs_agents(bot_id integer primary key, agent_id text, token text, name text, created real);
         create table if not exists docs_legacy(page_id integer primary key, doc_id text);",
    );
}

// ---------- 客户端 ----------
static HTTP: LazyLock<reqwest::Client> = LazyLock::new(|| {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("reqwest")
});

fn builder() -> oad::ClientBuilder {
    let c = conf().expect("docs 没配置");
    oad::Client::builder().base_url(&c.url).http_client(HTTP.clone()).locale(if crate::i18n::lang() == "en" { "en" } else { "zh" })
}

/// 以 OSA 的用户（工作区所有者）的身份
fn owner() -> oad::Result<oad::Client> {
    let c = conf().expect("docs 没配置");
    builder().header_secret(&c.secret).build()?.with_user(oad::User::new(OWNER).name(user_name()))
}

fn user_name() -> String {
    t!("用户")
}

fn as_agent(token: &str) -> oad::Result<oad::Client> {
    builder().token(token).build()
}

// ---------- Bot 的 Agent ----------
fn enc(token: &str) -> String {
    base64::engine::general_purpose::STANDARD.encode(vault::encrypt_raw(token))
}

fn dec(s: &str) -> Option<String> {
    vault::decrypt_raw(&base64::engine::general_purpose::STANDARD.decode(s).ok()?)
}

// ---------- 备份与迁移（backup.rs 用） ----------
/// 自己起的文档服务的数据目录（连的是别处的 OAD、或者还没用过文档时是 None）
pub fn local_dir() -> Option<std::path::PathBuf> {
    let d = crate::config::DATA.join("docs");
    d.join("oad.db").exists().then_some(d)
}

/// OAD 数据库里有几篇文档（不算回收站和数据库的行）
pub fn page_count(oad_db: &std::path::Path) -> Option<i64> {
    let c = rusqlite::Connection::open_with_flags(oad_db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).ok()?;
    c.query_row("select count(*) from pages where deleted_at is null and kind<>'row'", [], |r| r.get(0)).ok()
}

const PLAIN: &str = "plain:";

/// 导出前：Agent 的 token 是用这台的保险库主密钥加密的，换了机器解不开。备份文件整个是加密的，里面放明文（和 secrets.json 一样）
pub fn tokens_for_backup(db: &rusqlite::Connection) {
    let rows: Vec<(i64, String)> = db
        .prepare("select bot_id, token from docs_agents")
        .and_then(|mut s| s.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).map(|m| m.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    for (id, tok) in rows {
        match dec(&tok) {
            Some(t) => db.execute("update docs_agents set token=? where bot_id=?", params![format!("{PLAIN}{t}"), id]),
            None => db.execute("delete from docs_agents where bot_id=?", params![id]),
        }
        .ok();
    }
}

/// 恢复时：备份里带着文档就用这台的主密钥重新加密 token；没带文档，这些 Agent 在新的文档服务里没有，清掉（启动时重新建）
pub fn tokens_from_backup(db: &rusqlite::Connection, with_docs: bool) {
    let rows: Vec<(i64, String)> = db
        .prepare("select bot_id, token from docs_agents")
        .and_then(|mut s| s.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).map(|m| m.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    for (id, tok) in rows {
        match tok.strip_prefix(PLAIN).filter(|_| with_docs) {
            Some(t) => db.execute("update docs_agents set token=? where bot_id=?", params![enc(t), id]),
            None => db.execute("delete from docs_agents where bot_id=?", params![id]),
        }
        .ok();
    }
    if !with_docs {
        db.execute("delete from docs_legacy", []).ok();
    }
}

struct AgentRow {
    agent_id: String,
    token: String,
    name: String,
}

fn row_of(bot_id: i64) -> Option<AgentRow> {
    let r = db::row("select agent_id, token, name from docs_agents where bot_id=?", params![bot_id])?;
    Some(AgentRow { agent_id: r.s(0), token: dec(&r.s(1))?, name: r.s(2) })
}

/// Bot 在 OAD 里的名字：1 到 40 个字，不能有空白和 @ [ ] ( ) < > 引号 逗号
pub fn agent_name(name: &str, bot_id: i64) -> String {
    let bad = |c: char| c.is_whitespace() || "@[]()<>\"'`,，“”‘’".contains(c);
    let s: String = name.chars().map(|c| if bad(c) { '-' } else { c }).collect();
    let s = s.trim_matches('-').chars().take(40).collect::<String>();
    if s.is_empty() { format!("bot-{bot_id}") } else { s }
}

async fn create_agent(ws: &str, bot_id: i64, name: &str, scope: &str, about: &str) -> oad::Result<AgentRow> {
    let o = owner()?;
    let body = |n: &str| oad::types::CreateAgent {
        name: n.to_string(),
        scope: scope.to_string(),
        about: oad::Nullable::Value(about.to_string()),
        ..Default::default()
    };
    // 名字重了（比如以前留下的同名 Agent）就带上编号再建一次
    let made = match o.agents().create(ws, body(name)).await {
        Err(e) if e.is_conflict() || e.is_bad_request() => {
            let n2: String = format!("{}-{bot_id}", name.chars().take(32).collect::<String>());
            o.agents().create(ws, body(&n2)).await?
        }
        r => r?,
    };
    db::exec(
        "insert or replace into docs_agents(bot_id, agent_id, token, name, created) values(?,?,?,?,?)",
        params![bot_id, made.agent.id, enc(&made.token), made.agent.name, py::now()],
    );
    Ok(AgentRow { agent_id: made.agent.id, token: made.token, name: made.agent.name })
}

/// 让 OAD 里的 Agent 和现在的 Bot 对上：缺的建、名字变了的改、Bot 删了的撤掉。返回 OSA 自己那个 admin Agent 的 token
async fn sync() -> oad::Result<String> {
    sync_once(true).await
}

async fn sync_once(retry: bool) -> oad::Result<String> {
    let o = owner()?;
    let ws = o.default_workspace().await?;
    let me = match row_of(SELF_ID) {
        Some(r) => r,
        None => create_agent(&ws, SELF_ID, "OSA", "admin", "Open Super Agent：替各个 Bot 收通知").await?,
    };
    let alive: Vec<String> = o.agents().list(&ws).await?.into_iter().filter(|a| a.revoked_at.is_none()).map(|a| a.id).collect();
    if !alive.contains(&me.agent_id) {
        // OAD 的数据换过了（或 Agent 被人在别处撤了）：这边记的全作废，重新建（只重来一次）
        db::exec("delete from docs_agents", []);
        if retry {
            return Box::pin(sync_once(false)).await;
        }
        return Err(oad::Error::Config("建好的 Agent 在列表里找不到".into()));
    }
    let bots = bots::list_bots();
    for b in &bots {
        let id = b["id"].as_i64().unwrap_or(0);
        let want = agent_name(b["name"].as_str().unwrap_or(""), id);
        match row_of(id) {
            Some(r) if alive.contains(&r.agent_id) => {
                if r.name != want && !r.name.starts_with(&format!("{}-", want.chars().take(32).collect::<String>())) {
                    rename(id, &want).await;
                }
            }
            _ => {
                if let Err(e) = create_agent(&ws, id, &want, "write", "").await {
                    tracing::warn!("文档：给 Bot {id} 建 Agent 失败：{e}");
                }
            }
        }
    }
    let ids: Vec<i64> = bots.iter().filter_map(|b| b["id"].as_i64()).collect();
    for r in db::rows("select bot_id from docs_agents where bot_id<>0", []) {
        if !ids.contains(&r.i(0)) {
            forget(r.i(0)).await;
        }
    }
    Ok(me.token)
}

/// Bot 改名后跟着改 Agent 的名字（失败了不要紧：下次启动 sync 再改）
pub async fn rename(bot_id: i64, name: &str) {
    let (Some(r), Ok(o)) = (row_of(bot_id), owner()) else { return };
    let body = oad::types::PatchAgent { name: oad::Nullable::Value(name.to_string()), ..Default::default() };
    match o.agents().update(&r.agent_id, body).await {
        Ok(a) => {
            db::exec("update docs_agents set name=? where bot_id=?", params![a.name, bot_id]);
        }
        Err(e) => tracing::warn!("文档：Bot {bot_id} 的 Agent 改名失败：{e}"),
    }
}

/// Bot 删了：撤掉它的 Agent（写过的内容留着，署名还是它）
pub async fn forget(bot_id: i64) {
    SESSIONS.lock().remove(&bot_id);
    if let (Some(r), Ok(o)) = (row_of(bot_id), owner())
        && let Err(e) = o.agents().revoke(&r.agent_id).await
        && !e.is_not_found()
    {
        tracing::warn!("文档：撤掉 Bot {bot_id} 的 Agent 失败：{e}");
        return;
    }
    db::exec("delete from docs_agents where bot_id=?", params![bot_id]);
}

/// Bot 新建了：马上给它建 Agent（不等下次启动）
pub async fn on_bot_created(bot_id: i64) {
    if !ready() {
        return;
    }
    let Some(b) = bots::get(bot_id) else { return };
    let Ok(o) = owner() else { return };
    let Ok(ws) = o.default_workspace().await else { return };
    if let Err(e) = create_agent(&ws, bot_id, &agent_name(b["name"].as_str().unwrap_or(""), bot_id), "write", "").await {
        tracing::warn!("文档：给 Bot {bot_id} 建 Agent 失败：{e}");
    }
}

async fn token_of(bot_id: i64) -> Result<String, String> {
    if let Some(r) = row_of(bot_id) {
        return Ok(r.token);
    }
    on_bot_created(bot_id).await;
    row_of(bot_id).map(|r| r.token).ok_or_else(|| "文档服务现在连不上".to_string())
}

// ---------- 工具（OAD 的 MCP） ----------
static SESSIONS: LazyLock<Mutex<HashMap<i64, Arc<Session>>>> = LazyLock::new(Default::default);
static CTX: LazyLock<Mutex<Option<Arc<Context>>>> = LazyLock::new(Default::default);

async fn connect_mcp(token: &str) -> Result<Arc<Session>, String> {
    let c = conf().ok_or("文档没有配置")?;
    let headers = HashMap::from([("Authorization".to_string(), format!("Bearer {token}"))]);
    let tr = Transport::Http { url: format!("{}/mcp", c.url), headers, auth: None };
    Session::connect(tr, json!({}), Duration::from_secs(20)).await.map_err(|e| e.to_string())
}

async fn session_of(bot_id: i64) -> Result<Arc<Session>, String> {
    if let Some(s) = SESSIONS.lock().get(&bot_id).cloned() {
        return Ok(s);
    }
    let s = connect_mcp(&token_of(bot_id).await?).await?;
    SESSIONS.lock().insert(bot_id, s.clone());
    Ok(s)
}

fn run_of(tool: String) -> registry::RunFn {
    Arc::new(move |c: Call| {
        let tool = tool.clone();
        Box::pin(async move {
            let bot_id = c.bot_id();
            let mut again = false;
            loop {
                let s = match session_of(bot_id).await {
                    Ok(s) => s,
                    Err(e) => return Out::Text(format!("工具出错：{e}")),
                };
                match s.call_tool(&tool, &c.args).await {
                    Ok(r) => {
                        if tool == "comment" {
                            replied(bot_id, &c.args);
                        }
                        return Out::Text(crate::mcp_client::text_of(&r));
                    }
                    // 连接断了（OAD 重启过）：重连一次
                    Err(e) if e.lost() && !again => {
                        SESSIONS.lock().remove(&bot_id);
                        again = true;
                    }
                    Err(e) => return Out::Text(format!("工具出错：{e}")),
                }
            }
        })
    })
}

async fn register_tools(admin_token: &str) -> Result<usize, String> {
    let s = connect_mcp(admin_token).await?;
    let tools = s.list_tools().await.map_err(|e| e.to_string())?;
    s.shutdown().await;
    let ctx = Arc::new(Context::new("docs"));
    let mut n = 0;
    for tl in &tools {
        let name = tl["name"].as_str().unwrap_or("").to_string();
        if name.is_empty() || SKIP_TOOLS.contains(&name.as_str()) {
            continue;
        }
        let full = format!("docs__{name}");
        let params = tl.get("inputSchema").filter(|v| v.is_object()).cloned().unwrap_or(json!({"type": "object", "properties": {}}));
        let desc = tl["description"].as_str().unwrap_or("").to_string();
        let mut t = Tool::new(
            &full,
            json!({"type": "function", "function": {"name": full, "description": py::head(&desc, 1500), "parameters": params}}),
            run_of(name.clone()),
        );
        t.where_ = if ROUTER_TOOLS.contains(&name.as_str()) { vec![ROUTER, WORKER] } else { vec![WORKER] };
        t.describe = desc;
        let shown = name.clone();
        t.progress = Some(Arc::new(move |a: &Value| progress(&shown, a)));
        if ctx.tool(t).is_ok() {
            n += 1;
        }
    }
    if let Some(old) = CTX.lock().replace(ctx) {
        old.dispose();
    }
    Ok(n)
}

fn progress(tool: &str, a: &Value) -> String {
    let s = |k: &str| py::head(&py::str_of(&a[k]), 40);
    match tool {
        "search_pages" => t!("搜文档：{q}", q = s("query")),
        "read_page" => t!("读文档"),
        "create_page" => t!("新建文档：{title}", title = s("title")),
        "edit_page" | "rewrite_page" | "update_page" => t!("改文档"),
        "comment" => t!("回复文档评论"),
        _ => t!("文档：{tool}", tool = tool),
    }
}

// ---------- 评论叫醒 Bot ----------
/// 这一轮里 Bot 自己回过的评论串：(bot_id, thread_id)
static REPLIED: LazyLock<Mutex<Vec<(i64, String)>>> = LazyLock::new(Default::default);
/// 叫醒的那一轮里派出去的任务：task_id → (bot_id, thread_id)，任务做完把结果发到评论串里
static PENDING: LazyLock<Mutex<HashMap<i64, (i64, String)>>> = LazyLock::new(Default::default);

fn replied(bot_id: i64, args: &Value) {
    for k in ["thread_id", "thread", "reply_to"] {
        if let Some(t) = args[k].as_str() {
            REPLIED.lock().push((bot_id, t.to_string()));
        }
    }
}

fn take_replied(bot_id: i64, thread: &str) -> bool {
    let mut r = REPLIED.lock();
    let had = r.iter().any(|(b, t)| *b == bot_id && t == thread);
    r.retain(|(b, t)| !(*b == bot_id && t == thread));
    had
}

/// 用 Bot 的身份回复评论串
async fn post_reply(bot_id: i64, thread: &str, text: &str) {
    let text = text.trim();
    if text.is_empty() {
        return;
    }
    let Ok(tok) = token_of(bot_id).await else { return };
    let Ok(c) = as_agent(&tok) else { return };
    let body = oad::types::Reply { body: py::head(text, 8000), ..Default::default() };
    if let Err(e) = c.threads().reply(thread, body).await {
        tracing::warn!("文档：Bot {bot_id} 回复评论失败：{e}");
    }
}

fn bot_of_agent(agent_id: &str) -> Option<i64> {
    db::row("select bot_id from docs_agents where agent_id=? and bot_id<>0", params![agent_id]).map(|r| r.i(0))
}

/// 没设对外地址时，OAD 给的链接是它自己的 127.0.0.1 地址：换成站内的相对地址（/p/…）
fn local_links(text: &str) -> String {
    match conf() {
        Some(c) => text.replace(&c.url, ""),
        None => text.to_string(),
    }
}

async fn handle(note: oad::types::AgentNotification) {
    let Some(bot_id) = bot_of_agent(&note.agent.id) else { return };
    let Some(b) = bots::get(bot_id) else { return };
    let Some(chat_id) = b["chat_id"].as_i64() else { return };
    let who = note.comment.author["name"].as_str().unwrap_or("").to_string();
    let shown =
        t!("[文档评论]《{title}》{who}：{body}", title = note.page.title.clone(), who = who, body = py::head(&note.comment.body, 500));
    let meta = json!({"kind": "doc_comment", "page_id": note.page.id, "url": link(&note.page.id), "thread_id": note.thread_id});
    let mid = memory::add("task", &shown, chat_id, Some(bot_id), &meta);
    server::broadcast(json!({"type": "message", "chat_id": chat_id, "bot_id": bot_id, "message": {
        "id": mid, "ts": py::now(), "role": "task", "content": shown, "meta": meta, "bot_id": bot_id}}));
    let since = py::now();
    REPLIED.lock().retain(|(b, _)| *b != bot_id); // 以前留下的记号不算
    let turn = Turn {
        woke: Some(Woke { kind: "doc_comment".into(), from: 0, text: local_links(&note.prompt), edited: vec![] }),
        ..Turn::with_depth(0)
    };
    crate::dm::wake(bot_id, chat_id, turn).await;
    // 这一轮派出去的任务：做完后结果发到评论串
    let started: Vec<i64> = crate::router::TASKS
        .lock()
        .values()
        .filter(|t| t.bot_id == bot_id && t.chat_id == chat_id && t.fake_id.is_none() && t.st.lock().started >= since)
        .map(|t| t.id)
        .collect();
    for id in started {
        PENDING.lock().insert(id, (bot_id, note.thread_id.clone()));
    }
    if !take_replied(bot_id, &note.thread_id) {
        let last = db::scalar(
            "select content from messages where chat_id=? and bot_id=? and role='assistant' and ts>=? order by id desc limit 1",
            params![chat_id, bot_id, since],
        );
        post_reply(bot_id, &note.thread_id, last.as_str().unwrap_or("")).await;
    }
}

async fn on_task_done(kw: Value) {
    let Some(id) = kw["task"]["id"].as_i64() else { return };
    let Some((bot_id, thread)) = PENDING.lock().remove(&id) else { return };
    if take_replied(bot_id, &thread) {
        return;
    }
    let status = kw["task"]["status"].as_str().unwrap_or("");
    let result = py::str_of(&kw["result"]);
    let text = if status == "done" {
        result
    } else {
        t!("（任务没做完：{result}）", result = if result.is_empty() { status.to_string() } else { result })
    };
    post_reply(bot_id, &thread, &text).await;
}

/// 每个 Bot 一个队列，按顺序处理它的通知（同一个 Bot 同时只叫醒一轮）
type NoteTx = tokio::sync::mpsc::UnboundedSender<(oad::types::AgentNotification, oad::Client)>;
static QUEUES: LazyLock<Mutex<HashMap<i64, NoteTx>>> = LazyLock::new(Default::default);

fn enqueue(note: oad::types::AgentNotification, admin: oad::Client) {
    let bot = bot_of_agent(&note.agent.id).unwrap_or(-1);
    let mut q = QUEUES.lock();
    let tx = q.entry(bot).or_insert_with(|| {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<(oad::types::AgentNotification, oad::Client)>();
        tokio::spawn(async move {
            while let Some((note, admin)) = rx.recv().await {
                let id = note.id;
                handle(note).await;
                let ack = oad::types::Ack { ids: Some(vec![id]), ..Default::default() };
                if let Err(e) = admin.agent().notifications().ack(ack).await {
                    tracing::warn!("文档：确认通知 {id} 失败：{e}");
                }
            }
        });
        tx
    });
    let _ = tx.send((note, admin));
}

async fn listen(admin_token: String) -> oad::Result<()> {
    let admin = as_agent(&admin_token)?;
    let mut events = admin.agent().notifications().stream().await?;
    while let Some(ev) = events.next().await {
        let ev = ev?;
        enqueue(ev.data, admin.clone());
    }
    Ok(())
}

// ---------- 启动 ----------
/// 自带的 open-agent-docs 程序：OSA_DOCS_BIN，或者和 osa-server 放在一起的（桌面 App 的 Contents/MacOS、Docker 镜像的 /usr/local/bin），或者 PATH 上的
fn find_bin() -> Option<std::path::PathBuf> {
    let name = "open-agent-docs";
    if let Some(p) = std::env::var_os("OSA_DOCS_BIN").filter(|p| !p.is_empty()) {
        return Some(p.into());
    }
    let next = std::env::current_exe().ok().and_then(|e| e.parent().map(|d| d.join(name)));
    if let Some(p) = next.filter(|p| p.is_file()) {
        return Some(p);
    }
    std::env::var_os("PATH").and_then(|path| std::env::split_paths(&path).map(|d| d.join(name)).find(|p| p.is_file()))
}

/// 自己起的文档服务的 header 密钥：第一次生成，存在数据目录里（只有自己能读）
fn local_secret(dir: &std::path::Path) -> String {
    let f = dir.join(".osa-header-secret");
    if let Ok(s) = std::fs::read_to_string(&f)
        && s.trim().len() >= 32
    {
        return s.trim().to_string();
    }
    let s: String = {
        use rand::Rng;
        let mut r = rand::thread_rng();
        (0..48).map(|_| char::from(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[r.gen_range(0..62)])).collect()
    };
    let _ = std::fs::write(&f, &s);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o600));
    }
    s
}

static CHILD: LazyLock<tokio::sync::Mutex<Option<tokio::process::Child>>> = LazyLock::new(Default::default);
static STOPPING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn command(bin: &std::path::Path, dir: &std::path::Path, port: u16, secret: &str, public: &str) -> tokio::process::Command {
    let mut cmd = tokio::process::Command::new(bin);
    cmd.arg("serve")
        .env("OAD_AUTH", "header")
        .env("OAD_HEADER_SECRET", secret)
        .env("OAD_HOST", "127.0.0.1")
        .env("OAD_PORT", port.to_string())
        .env("OAD_DATA_DIR", dir)
        .env_remove("OAD_DATABASE_URL")
        // 它的日志和 osa 的混在一起：只留启动、警告和错误
        .env("RUST_LOG", "warn,oad_bin=info")
        .kill_on_drop(true);
    if public.is_empty() {
        cmd.env_remove("OAD_PUBLIC_URL");
    } else {
        // 分享、通知里的链接用 OSA 对外的地址（设置里的对外地址；改了以后重启生效）
        cmd.env("OAD_PUBLIC_URL", public);
    }
    cmd
}

/// 起自带的文档服务（只听 127.0.0.1，数据在 <数据目录>/docs），退出了就重起
async fn spawn(bin: std::path::PathBuf) -> Option<Conf> {
    let dir = crate::config::DATA.join("docs");
    if let Err(e) = std::fs::create_dir_all(&dir) {
        tracing::warn!("文档：建不了数据目录 {}：{e}", dir.display());
        return None;
    }
    let secret = local_secret(&dir);
    let port = std::net::TcpListener::bind("127.0.0.1:0").ok()?.local_addr().ok()?.port();
    let public = crate::channels::public_url();
    let cmd = || command(&bin, &dir, port, &secret, &public);
    let child = match cmd().spawn() {
        Ok(c) => c,
        Err(e) => {
            tracing::warn!("文档：起不来 {}：{e}", bin.display());
            return None;
        }
    };
    *CHILD.lock().await = Some(child);
    let (bin2, dir2, secret2, public2) = (bin.clone(), dir.clone(), secret.clone(), public.clone());
    tokio::spawn(async move {
        let cmd = || command(&bin2, &dir2, port, &secret2, &public2);
        loop {
            let status = {
                let mut g = CHILD.lock().await;
                match g.as_mut() {
                    Some(c) => match c.try_wait() {
                        Ok(Some(s)) => Some(s),
                        _ => None,
                    },
                    None => return,
                }
            };
            if let Some(s) = status {
                if STOPPING.load(std::sync::atomic::Ordering::Relaxed) {
                    return;
                }
                tracing::warn!("文档：文档服务退出了（{s}），3 秒后重起");
                tokio::time::sleep(Duration::from_secs(3)).await;
                match cmd().spawn() {
                    Ok(c) => *CHILD.lock().await = Some(c),
                    Err(e) => {
                        tracing::warn!("文档：重起失败：{e}");
                        return;
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    });
    Some(Conf { url: format!("http://127.0.0.1:{port}"), secret })
}

/// 退出时停掉自己起的文档服务（等它把页面存好）
pub async fn stop() {
    STOPPING.store(true, std::sync::atomic::Ordering::Relaxed);
    if let Some(mut c) = CHILD.lock().await.take() {
        #[cfg(unix)]
        if let Some(pid) = c.id() {
            unsafe { libc::kill(pid as i32, libc::SIGTERM) };
            if tokio::time::timeout(Duration::from_secs(10), c.wait()).await.is_ok() {
                return;
            }
        }
        let _ = c.kill().await;
    }
}

pub async fn start() {
    let env = |k: &str| std::env::var(k).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
    let c = match (env("OSA_DOCS_URL"), env("OSA_DOCS_SECRET")) {
        (Some(url), Some(secret)) => Some(Conf { url: url.trim_end_matches('/').to_string(), secret }),
        _ if env("OSA_DOCS") == Some("off".into()) => None,
        _ => match find_bin() {
            Some(bin) => spawn(bin).await,
            None => {
                tracing::info!("文档：没找到 open-agent-docs 程序，资料库里没有文档（见 docs/plans/oad.md）");
                None
            }
        },
    };
    let Some(c) = c else { return };
    let _ = CONF.set(c);
    let ctx = Context::new("docs-events");
    ctx.on("task_done", Arc::new(|kw| Box::pin(on_task_done(kw))));
    std::mem::forget(ctx);
    tokio::spawn(async {
        let mut wait = 1u64;
        loop {
            match connect_once().await {
                Ok(()) => wait = 1,
                Err(e) => {
                    *READY.lock() = false;
                    // 自己起的服务刚启动时连不上很正常，前几次不记
                    if wait >= 4 {
                        tracing::warn!("文档：连不上 Open Agent Docs（{e}），{wait} 秒后再试");
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs(wait)).await;
            wait = (wait * 2).min(30);
        }
    });
}

/// 旧资料库里的 Markdown 页面搬进文档（一次性）：用原来那个 Bot 的身份建，Bot 删了的用用户的身份；搬好一篇删一篇
async fn import_legacy() {
    let rows =
        db::rows("select id, bot_id, title, content from pages where coalesce(format,'markdown')<>'html' and app is null order by id", []);
    if rows.is_empty() {
        return;
    }
    let mut n = 0;
    for r in &rows {
        let (pid, bot, title, content) = (r.i(0), r.oi(1), r.s(2), r.s(3));
        let client = match bot.filter(|b| bots::get(*b).is_some()) {
            Some(b) => token_of(b).await.and_then(|t| as_agent(&t).map_err(|e| e.to_string())),
            None => owner().map_err(|e| e.to_string()),
        };
        let Ok(c) = client else { return };
        let made: oad::Result<oad::types::Page> = async {
            let ws = c.default_workspace().await?;
            let body = oad::types::CreatePage { title: oad::Nullable::Value(title.clone()), ..Default::default() };
            c.pages().create_with_markdown(&ws, body, content).await
        }
        .await;
        match made {
            Ok(p) => {
                db::exec("insert or replace into docs_legacy(page_id, doc_id) values(?,?)", params![pid, p.id]);
                db::exec("delete from pages where id=?", params![pid]);
                n += 1;
            }
            Err(e) => {
                tracing::warn!("文档：旧页面 #{pid} 搬不过去：{e}");
                return;
            }
        }
    }
    tracing::info!("文档：旧资料库的 {n} 篇 Markdown 页面已搬进文档");
    server::broadcast(json!({"type": "pages_changed"}));
}

/// 旧资料库页面（/library/pages/N）搬进文档后的新编号
pub fn legacy(page_id: i64) -> Option<String> {
    db::row("select doc_id from docs_legacy where page_id=?", params![page_id]).map(|r| r.s(0))
}

async fn connect_once() -> Result<(), String> {
    let token = sync().await.map_err(|e| e.to_string())?;
    import_legacy().await;
    let n = register_tools(&token).await?;
    *READY.lock() = true;
    tracing::info!("文档：已连上 Open Agent Docs，{n} 个工具");
    server::broadcast(json!({"type": "settings_changed"}));
    listen(token).await.map_err(|e| e.to_string())
}

// ---------- 转发 ----------
const HOP: [&str; 9] =
    ["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer", "host", "content-length"];

/// 浏览器发来的请求头里哪些转给 OAD：OSA 的 cookie、Authorization、Origin（OSA 已经查过同源）、伪造的 X-OAD-* 都不转
fn forward_headers(src: &HeaderMap, user: bool) -> HeaderMap {
    let mut h = HeaderMap::new();
    for (k, v) in src {
        let n = k.as_str();
        if HOP.contains(&n)
            || n.starts_with("x-oad-")
            || n.starts_with("sec-websocket")
            || ["cookie", "authorization", "origin", "referer", "accept-encoding"].contains(&n)
        {
            continue;
        }
        h.append(k.clone(), v.clone());
    }
    if let Some(host) = src.get(header::HOST) {
        h.insert(HeaderName::from_static("x-forwarded-host"), host.clone());
    }
    if user && let Some(c) = conf() {
        let pct = |s: &str| url::form_urlencoded::byte_serialize(s.as_bytes()).collect::<String>().replace('+', "%20");
        if let Ok(v) = HeaderValue::from_str(&c.secret) {
            h.insert(HeaderName::from_static("x-oad-secret"), v);
        }
        h.insert(HeaderName::from_static("x-oad-user-id"), HeaderValue::from_static(OWNER));
        if let Ok(v) = HeaderValue::from_str(&pct(&user_name())) {
            h.insert(HeaderName::from_static("x-oad-user-name"), v);
        }
    }
    h
}

fn unavailable() -> Response {
    (StatusCode::SERVICE_UNAVAILABLE, axum::Json(json!({"error": {"code": "unavailable", "message": t!("文档服务没有启动")}})))
        .into_response()
}

async fn proxy(req: Request) -> Response {
    // 资料库网页（HTML、小应用）以前的分享链接也是 /s/<token>：是它们的就照旧由网页显示
    if let Some(tok) = req.uri().path().strip_prefix("/s/")
        && !tok.contains('/')
        && crate::library::by_token(tok).is_some()
    {
        return server::fallback(req).await;
    }
    let Some(c) = conf() else { return unavailable() };
    let (parts, body) = req.into_parts();
    let pq = parts.uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let user = pq.starts_with("/api/");
    let url = format!("{}{pq}", c.url);
    let stream = body.into_data_stream();
    let r = HTTP
        .request(parts.method.clone(), &url)
        .headers(forward_headers(&parts.headers, user))
        .body(reqwest::Body::wrap_stream(stream))
        .send()
        .await;
    let r = match r {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!("文档：转发 {pq} 失败：{e}");
            return unavailable();
        }
    };
    let mut out = Response::builder().status(r.status().as_u16());
    for (k, v) in r.headers() {
        if !HOP.contains(&k.as_str()) && k != header::SET_COOKIE {
            out = out.header(k, v);
        }
    }
    out.body(Body::from_stream(r.bytes_stream())).unwrap_or_else(|_| unavailable())
}

/// 协同编辑的 WebSocket：/api/v1/sync/{page}
async fn sync_ws(Path(page): Path<String>, ws: WebSocketUpgrade, req: Request) -> Response {
    let Some(c) = conf() else { return unavailable() };
    let q = req.uri().query().map(|q| format!("?{q}")).unwrap_or_default();
    let url = format!("{}/api/v1/sync/{page}{q}", c.url.replacen("http", "ws", 1));
    let mut up = match tokio_tungstenite::tungstenite::client::IntoClientRequest::into_client_request(url.as_str()) {
        Ok(r) => r,
        Err(_) => return unavailable(),
    };
    for (k, v) in forward_headers(req.headers(), true) {
        if let Some(k) = k {
            up.headers_mut().insert(k, v);
        }
    }
    let upstream = match tokio_tungstenite::connect_async(up).await {
        Ok((s, _)) => s,
        Err(e) => {
            tracing::warn!("文档：连协同编辑失败：{e}");
            return unavailable();
        }
    };
    ws.on_upgrade(move |client| pump(client, upstream))
}

async fn pump(client: WebSocket, upstream: tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>) {
    use tokio_tungstenite::tungstenite::Message as TMsg;
    let (mut cw, mut cr) = client.split();
    let (mut uw, mut ur) = upstream.split();
    let a = async {
        while let Some(Ok(m)) = cr.next().await {
            let m = match m {
                AxMsg::Binary(b) => TMsg::Binary(b),
                AxMsg::Text(t) => TMsg::Text(t.as_str().into()),
                AxMsg::Ping(p) => TMsg::Ping(p),
                AxMsg::Pong(p) => TMsg::Pong(p),
                AxMsg::Close(_) => break,
            };
            if uw.send(m).await.is_err() {
                break;
            }
        }
        let _ = uw.close().await;
    };
    let b = async {
        while let Some(Ok(m)) = ur.next().await {
            let m = match m {
                TMsg::Binary(b) => AxMsg::Binary(b),
                TMsg::Text(t) => AxMsg::Text(t.as_str().into()),
                TMsg::Ping(p) => AxMsg::Ping(p),
                TMsg::Pong(p) => AxMsg::Pong(p),
                TMsg::Close(_) => break,
                TMsg::Frame(_) => continue,
            };
            if cw.send(m).await.is_err() {
                break;
            }
        }
        let _ = cw.close().await;
    };
    tokio::select! { _ = a => {}, _ = b => {} }
}

pub fn routes() -> axum::Router {
    axum::Router::new().route("/api/v1/sync/{page}", get(sync_ws)).route("/api/v1/{*p}", any(proxy)).route("/s/{*p}", any(proxy))
}

// ---------- save_page / read_page 的文档部分 ----------
/// 对话里引用文档用的地址（OAD 的页面地址，前端在资料库里打开）
pub fn link(id: &str) -> String {
    format!("/p/{id}")
}

/// 文档的编号是 OAD 的字符串 id；资料库网页、小应用是数字
pub fn is_doc_id(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty() && s.parse::<i64>().is_err())
}

/// 正文开头的一级标题就是文档的标题（或者是它的前半截）时去掉这一行：文档页最上面已经用大字显示标题，
/// 正文里紧接着再来一遍，看着像写重了。一级标题比文档标题多出内容的（“周报：第 48 周”对“周报”）不动
fn drop_title_heading<'a>(title: &str, content: &'a str) -> &'a str {
    let Some(rest) = content.trim_start().strip_prefix("# ") else { return content };
    let (line, after) = rest.split_once('\n').unwrap_or((rest, ""));
    let norm = |s: &str| s.chars().filter(|c| !c.is_whitespace() && !"#*_`".contains(*c)).collect::<String>().to_lowercase();
    let (h, t) = (norm(line), norm(title));
    let after = after.trim_start_matches(['\n', '\r']);
    if h.chars().count() < 2 || !t.starts_with(&h) || after.trim().is_empty() {
        return content;
    }
    after
}

/// save_page 写 Markdown：用 Bot 自己的身份建文档或整篇换掉（给了 page_id 就是更新那篇）
pub async fn save_markdown(bot_id: i64, title: &str, content: &str, page_id: Option<&str>) -> String {
    if !ready() {
        return "没保存：文档服务现在连不上，稍后再试".into();
    }
    let c = match token_of(bot_id).await.and_then(|t| as_agent(&t).map_err(|e| e.to_string())) {
        Ok(c) => c,
        Err(e) => return format!("没保存：{e}"),
    };
    let title = py::head(title.trim(), 200);
    let r: oad::Result<(String, String)> = async {
        match page_id {
            Some(id) => {
                if !title.is_empty() {
                    let patch = oad::types::PatchPage { title: oad::Nullable::Value(title.clone()), ..Default::default() };
                    c.pages().update(id, patch).await?;
                }
                // 没给新标题时，拿这篇现在的标题来比
                let now = if title.is_empty() && content.trim_start().starts_with("# ") { c.pages().get(id).await?.title } else { title.clone() };
                let put = oad::types::PutContent { markdown: oad::Nullable::Value(drop_title_heading(&now, content).to_string()), ..Default::default() };
                c.pages().content().replace(id, put).await?;
                let p = c.pages().get(id).await?;
                Ok((p.id, p.title))
            }
            None => {
                let ws = c.default_workspace().await?;
                let body = oad::types::CreatePage { title: oad::Nullable::Value(title.clone()), ..Default::default() };
                let p = c.pages().create_with_markdown(&ws, body, drop_title_heading(&title, content)).await?;
                Ok((p.id, p.title))
            }
        }
    }
    .await;
    match r {
        Ok((id, t)) => {
            server::broadcast(json!({"type": "docs_changed"}));
            format!(
                "已保存文档「{t}」（page_id=\"{id}\"），链接 {}（用户在资料库的文档里也能看到）。回复里用 Markdown 链接附上它。以后要局部修改，用 docs__edit_page 按块改。",
                link(&id)
            )
        }
        Err(e) if e.is_not_found() => format!("没保存：没有文档 {}", page_id.unwrap_or("")),
        Err(e) => format!("没保存：{e}"),
    }
}

/// read_page 读文档：Markdown 全文
pub async fn read_markdown(bot_id: i64, id: &str, limit: usize) -> String {
    let c = match token_of(bot_id).await.and_then(|t| as_agent(&t).map_err(|e| e.to_string())) {
        Ok(c) => c,
        Err(e) => return format!("没读到：{e}"),
    };
    let r: oad::Result<(oad::types::Page, String)> = async {
        let p = c.pages().get(id).await?;
        let body = c.pages().content().get(id).await?;
        Ok((p, body.markdown.unwrap_or_default()))
    }
    .await;
    match r {
        Ok((p, md)) => {
            let n = py::char_len(&md);
            let more = if n > limit {
                format!("\n…（还有 {} 字没显示，用 docs__read_page 分段读）", n - limit)
            } else {
                String::new()
            };
            format!("《{}》{}\n\n{}{more}", p.title, link(&p.id), py::head(&md, limit))
        }
        Err(e) if e.is_not_found() => format!("没有文档 {id}"),
        Err(e) => format!("没读到：{e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::drop_title_heading;

    #[test]
    fn drops_leading_heading_that_repeats_the_title() {
        // 一样的、是标题前半截的：去掉这一行和后面的空行
        assert_eq!(drop_title_heading("猫的报告", "# 猫的报告\n\n橘猫特别能吃。"), "橘猫特别能吃。");
        assert_eq!(drop_title_heading("近期动态：双链笔记 App（2026-11-14–2026-11-28）", "\n# 近期动态：双链笔记 App\n\n**时间范围**"), "**时间范围**");
        assert_eq!(drop_title_heading("Weekly Report", "# **weekly report**\r\n\r\nbody"), "body");
        // 一级标题比文档标题多出内容、只有一个字、不是一级标题、去掉以后什么都不剩：原样
        for (t, c) in [
            ("周报", "# 周报：第 48 周\n\n正文"),
            ("猫的报告", "# 猫\n\n橘猫特别能吃。"),
            ("猫的报告", "## 猫的报告\n\n正文"),
            ("猫的报告", "正文里提到 # 猫的报告"),
            ("猫的报告", "# 猫的报告\n\n"),
            ("", "# 猫的报告\n\n正文"),
        ] {
            assert_eq!(drop_title_heading(t, c), c);
        }
    }
}
