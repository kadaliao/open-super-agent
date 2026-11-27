//! 接口、WebSocket、广播、前端静态文件、启动和停止。对应 osa/server.py。

use crate::web::{ApiError, Body, Q, Res, int, ok, qi, todo};
use crate::{
    approvals, auth, bots, config, groups, i18n, mcp_hub, media, memories, memory, providers, py, router, routines, settings, tasklog,
};
use axum::Router;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Request};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, patch, post};
use parking_lot::Mutex;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::LazyLock;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::mpsc;

// ---------- 广播 ----------
struct Client {
    tx: mpsc::UnboundedSender<String>,
    watching: bool,
}

static CLIENTS: LazyLock<Mutex<HashMap<u64, Client>>> = LazyLock::new(Default::default);
static NEXT_CLIENT: AtomicU64 = AtomicU64::new(1);

/// 发给所有打开着的网页（和聊天软件、手机推送）。不等：消息按顺序进每个网页的发送队列
pub fn broadcast(event: Value) {
    let mut event = event;
    if event["type"] == "message"
        && let Some(cid) = event["chat_id"].as_i64().filter(|c| *c != 0)
    {
        match bots::thread_event(cid) {
            Some(th) => event["thread"] = th,
            None => event["unread"] = json!(bots::unread(cid)),
        }
    }
    crate::channels::on_event(&event);
    crate::board::on_event(&event); // 确认卡片弹出、关掉：卡片的列跟着变
    let text = py::dumps_u(&event);
    let nobody = {
        let mut c = CLIENTS.lock();
        c.retain(|_, cl| cl.tx.send(text.clone()).is_ok());
        !c.values().any(|cl| cl.watching)
    };
    if nobody
        && crate::push::count() > 0
        && let Some(msg) = crate::push::message_for(&event)
    {
        let urgency = if event["type"] == "approval" { "high" } else { "normal" };
        let badge = approvals::pending_count();
        tokio::spawn(async move { crate::push::send(msg, urgency, badge).await });
    }
}

pub fn client_count() -> usize {
    CLIENTS.lock().len()
}

// ---------- 用户发来的消息 ----------
pub async fn safe_handle(chat: Value, text: String, via: String, attachments: Vec<Value>) {
    let text = media::with_marks(&text, &attachments);
    let cid = chat["id"].as_i64().unwrap_or(0);
    // 消息以卡片编号开头（OPC-12 加上英国）、那张卡正好一个任务在做：交给那个任务，Bot 不被叫醒（网页和聊天软件都走这里）
    if let Some((_, running)) = crate::board::mention(&text)
        && running.len() == 1
        && crate::taskstore::tell_from(running[0], &text, cid)
    {
        return;
    }
    let r = if chat["kind"] == "group" {
        groups::on_user(&chat, &text, &via, &attachments).await;
        Ok(())
    } else {
        let bot = bots::ids_of(&chat).first().copied().unwrap_or(0);
        router::runtime(bot).handle_user(&text, &via, Some(cid), &attachments, None).await
    };
    if let Err(e) = r {
        tracing::error!("对话 {cid} 出错：{e:#}");
        broadcast(json!({"type": "error", "chat_id": cid, "message": t!("出错了：{e}", e = providers::brief(&e))}));
    }
}

async fn ws_handler(h: HeaderMap, up: WebSocketUpgrade) -> Response {
    if h.get("origin").and_then(|v| v.to_str().ok()) == Some("null") {
        return StatusCode::FORBIDDEN.into_response();
    }
    up.max_message_size(64 << 20).on_upgrade(ws_run)
}

async fn ws_run(sock: WebSocket) {
    use futures::{SinkExt, StreamExt};
    let (mut sink, mut stream) = sock.split();
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let id = NEXT_CLIENT.fetch_add(1, Ordering::Relaxed);
    CLIENTS.lock().insert(id, Client { tx, watching: true });
    let writer = tokio::spawn(async move {
        while let Some(m) = rx.recv().await {
            if sink.send(Message::Text(m.into())).await.is_err() {
                break;
            }
        }
    });
    while let Some(Ok(msg)) = stream.next().await {
        let text = match msg {
            Message::Text(t) => t.to_string(),
            Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
            Message::Close(_) => break,
            _ => continue,
        };
        let Ok(data) = serde_json::from_str::<Value>(&text) else { break };
        match data["type"].as_str() {
            Some("approve") => {
                approvals::resolve(
                    py::to_int(&data["id"]).unwrap_or(0),
                    py::truthy(&data["ok"]),
                    data["note"].as_str().unwrap_or(""),
                    py::truthy(&data["always"]),
                    &data["edits"],
                    data["scope"].as_str(),
                    data["prefix"].as_str(),
                );
            }
            Some("visibility") => {
                if let Some(c) = CLIENTS.lock().get_mut(&id) {
                    c.watching = py::truthy(&data["visible"]);
                }
            }
            Some("send") => {
                let text = data["text"].as_str().unwrap_or("").trim().to_string();
                let chat = py::to_int(&data["chat_id"]).and_then(bots::chat);
                let atts = media::clean_attachments(&data["attachments"]);
                if let Some(chat) = chat.filter(|_| !text.is_empty() || !atts.is_empty()) {
                    // 任务消息下面的讨论串：交给那个任务，不叫醒 Bot（taskstore.rs）
                    if crate::taskstore::thread_send(&chat, &media::with_marks(&text, &atts)) {
                        continue;
                    }
                    tokio::spawn(safe_handle(chat, text, String::new(), atts));
                }
            }
            _ => {}
        }
    }
    CLIENTS.lock().remove(&id);
    writer.abort();
}

// ---------- Bot 与对话 ----------
pub fn can_sign_out() -> bool {
    auth::enabled() && std::env::var("OSA_DESKTOP").map_or(true, |v| v.is_empty())
}

async fn bootstrap(h: HeaderMap) -> Res {
    if i18n::chosen().is_empty() {
        let l =
            if settings::onboarding() { i18n::from_header(h.get("accept-language").and_then(|v| v.to_str().ok())) } else { "zh".into() };
        let _ = i18n::set_lang(&l);
    }
    let tasks: Vec<Value> = router::tasks().iter().filter(|t| t.status() == "running").map(|t| t.public()).collect();
    let mut out = json!({"bots": bots::list_bots(), "chats": bots::list_chats(), "settings": settings::public(),
        "shapes": bots::SHAPES, "colors": bots::COLORS, "tasks": tasks, "approvals": approvals::pending_public(),
        "threads": bots::thread_list(None, true)});
    for (k, v) in apps_out(&h) {
        out[k] = v;
    }
    out["teach"] = crate::teach::public();
    out["auth"] = json!(can_sign_out());
    out["onboarding"] = json!(settings::onboarding());
    out["build"] = json!(web_build());
    ok(out)
}

pub fn apps_out(h: &HeaderMap) -> Vec<(&'static str, Value)> {
    let host = h.get("host").and_then(|v| v.to_str().ok()).unwrap_or("127.0.0.1");
    let scheme = if auth::https(h) { "https" } else { "http" };
    vec![
        ("apps", json!(mcp_hub::public())),
        ("catalog", json!(crate::catalog::public())),
        ("categories", json!(crate::catalog::categories())),
        ("oauth_callback", json!(format!("{scheme}://{host}/oauth/callback"))),
    ]
}

async fn create_bot(Body(d): Body) -> Res {
    let b = bots::create(d["name"].as_str().unwrap_or(""), &d["avatar"], d["instructions"].as_str().unwrap_or(""), false)?;
    broadcast(json!({"type": "bots_changed"}));
    ok(b)
}

async fn update_bot(Path(bot_id): Path<i64>, Body(d): Body) -> Res {
    if bots::get(bot_id).is_none() {
        return Err(ApiError::not_found(t!("没有这个 Bot")));
    }
    let b = bots::update(bot_id, &d)?;
    broadcast(json!({"type": "bots_changed"}));
    ok(b)
}

async fn make_primary(Path(bot_id): Path<i64>) -> Res {
    bots::set_primary(bot_id);
    broadcast(json!({"type": "bots_changed"}));
    ok(json!({"ok": true}))
}

async fn delete_bot(Path(bot_id): Path<i64>) -> Res {
    crate::botadmin::remove(bot_id).await?;
    broadcast(json!({"type": "bots_changed"}));
    ok(json!({"ok": true}))
}

async fn messages(Path(chat_id): Path<i64>, q: Q) -> Res {
    let limit = qi(&q, "limit").unwrap_or(60).min(200);
    ok(json!(memory::page(chat_id, qi(&q, "before"), limit)))
}

async fn chat_seen(Path(chat_id): Path<i64>, Body(d): Body) -> Res {
    let upto = if d["id"].is_null() { None } else { Some(int(&d["id"])?) };
    let n = bots::mark_chat_seen(chat_id, upto).ok_or_else(|| ApiError::not_found(t!("没有这个对话")))?;
    broadcast(json!({"type": "chat_seen", "chat_id": chat_id, "unread": n}));
    ok(json!({"unread": n}))
}

fn root_of(chat_id: i64, root: i64) -> Result<Value, ApiError> {
    let r = crate::db::row(
        "select id, ts, role, content, meta, bot_id from messages where id=? and chat_id=?",
        rusqlite::params![root, chat_id],
    )
    .ok_or_else(|| ApiError::not_found(t!("这个对话里没有这条消息")))?;
    Ok(memory::message_out(&r))
}

async fn list_threads(Path(chat_id): Path<i64>) -> Res {
    ok(json!(bots::thread_list(Some(chat_id), false)))
}

async fn get_thread_by_id(Path(tid): Path<i64>) -> Res {
    bots::thread_event(tid).map(axum::Json).ok_or_else(|| ApiError::not_found(t!("没有这个对话")))
}

async fn thread_seen(Path(tid): Path<i64>, Body(d): Body) -> Res {
    let upto = if d["id"].is_null() { None } else { Some(int(&d["id"])?) };
    let th = bots::mark_seen(tid, upto).ok_or_else(|| ApiError::not_found(t!("没有这个对话")))?;
    broadcast(json!({"type": "thread_seen", "chat_id": th["parent"], "thread": th}));
    ok(th)
}

async fn get_thread(Path((chat_id, root)): Path<(i64, i64)>) -> Res {
    ok(json!({"root": root_of(chat_id, root)?, "id": bots::find_thread(chat_id, root)}))
}

async fn open_thread(Path(chat_id): Path<i64>, Body(d): Body) -> Res {
    let root = if py::truthy(&d["root"]) { int(&d["root"])? } else { 0 };
    let th = bots::thread(chat_id, root)?;
    ok(json!({"root": root_of(chat_id, th["root"].as_i64().unwrap_or(0))?, "id": th["id"]}))
}

async fn create_group(Body(d): Body) -> Res {
    let ids = if py::truthy(&d["bot_ids"]) { d["bot_ids"].clone() } else { json!([]) };
    let g = bots::create_group(&ids, d["title"].as_str().unwrap_or(""), &d["lead"])?;
    broadcast(json!({"type": "bots_changed"}));
    ok(g)
}

async fn update_group(Path(chat_id): Path<i64>, Body(d): Body) -> Res {
    let g = bots::update_group(chat_id, d["title"].as_str(), &d["bot_ids"], &d["lead"], d["image"].as_str())?;
    broadcast(json!({"type": "bots_changed"}));
    ok(g)
}

async fn pin_chat(Path(chat_id): Path<i64>, Body(d): Body) -> Res {
    let c = bots::set_pinned(chat_id, py::truthy(&d["pinned"]))?;
    broadcast(json!({"type": "bots_changed"}));
    ok(c)
}

async fn delete_group(Path(chat_id): Path<i64>) -> Res {
    let chat = bots::chat(chat_id);
    if chat.as_ref().is_none_or(|c| c["kind"] != "group" || py::truthy(&c["parent"])) {
        return Err(ApiError::bad(t!("只能删除群聊；Bot 的私聊随 Bot 一起删除")));
    }
    let mut inside = vec![chat_id];
    inside.extend(bots::thread_ids(chat_id));
    for tk in router::tasks() {
        if inside.contains(&tk.chat_id) && tk.status() == "running" {
            router::cancel(tk.id, &t!("群聊已删除"));
        }
    }
    bots::delete_group(chat_id);
    for i in &inside {
        groups::forget(*i);
    }
    crate::channels::chat_removed(chat_id);
    broadcast(json!({"type": "bots_changed"}));
    ok(json!({"ok": true}))
}

async fn new_topic(Path(chat_id): Path<i64>) -> Res {
    let chat = bots::chat(chat_id).ok_or_else(|| ApiError::not_found(t!("没有这个对话")))?;
    if py::truthy(&chat["parent"]) {
        return Err(ApiError::bad(t!("讨论串里不能开新话题")));
    }
    ok(json!({"id": reset_chat(&chat)}))
}

pub fn reset_chat(chat: &Value) -> i64 {
    let chat_id = chat["id"].as_i64().unwrap_or(0);
    let bot_id = if chat["kind"] == "direct" { bots::ids_of(chat).first().copied() } else { None };
    let mid = memory::add("task", "[新话题]", chat_id, bot_id, &json!({"kind": "reset"}));
    broadcast(json!({"type": "message", "chat_id": chat_id, "bot_id": bot_id, "message": {
        "id": mid, "ts": py::now(), "role": "task", "content": "[新话题]", "meta": {"kind": "reset"}, "bot_id": bot_id}}));
    mid
}

const LIVE_KEYS: [&str; 6] = ["status", "steps", "started", "elapsed", "waiting_approval", "last_action"];

fn live(mut t: Value) -> Value {
    if let Some(l) = t["id"].as_i64().and_then(router::task).filter(|l| l.status() == "running") {
        let p = l.public();
        for k in LIVE_KEYS {
            t[k] = p[k].clone();
        }
    }
    t
}

async fn list_tasks(q: Q) -> Res {
    let limit = qi(&q, "limit").unwrap_or(30).clamp(1, 100);
    ok(json!(tasklog::list_tasks(qi(&q, "bot_id"), qi(&q, "chat_id"), qi(&q, "before"), limit).into_iter().map(live).collect::<Vec<_>>()))
}

async fn task_detail(Path(task_id): Path<i64>) -> Res {
    let t = tasklog::detail(task_id).ok_or_else(|| ApiError::not_found(t!("没有这个任务")))?;
    ok(json!({"task": live(t), "steps": tasklog::steps(task_id)}))
}

async fn cancel_task(Path(task_id): Path<i64>) -> Res {
    ok(json!({"result": router::cancel(task_id, &t!("用户在界面上取消"))}))
}

// ---------- 记忆 ----------
pub fn bot_or_404(bot_id: i64) -> Result<Value, ApiError> {
    bots::get(bot_id).ok_or_else(|| ApiError::not_found(t!("没有这个 Bot")))
}

async fn list_memories(Path(bot_id): Path<i64>) -> Res {
    bot_or_404(bot_id)?;
    ok(memories::list_for(bot_id))
}

async fn add_memory(Path(bot_id): Path<i64>, Body(d): Body) -> Res {
    bot_or_404(bot_id)?;
    let owner = if py::truthy(&d["shared"]) { None } else { Some(bot_id) };
    let m = memories::add(owner, d["text"].as_str().unwrap_or(""), "manual")?;
    broadcast(json!({"type": "memories_changed"}));
    ok(m)
}

async fn import_memories(Path(bot_id): Path<i64>, Body(d): Body) -> Res {
    bot_or_404(bot_id)?;
    let saved = memories::import_text(bot_id, d["text"].as_str().unwrap_or("")).await?;
    broadcast(json!({"type": "memories_changed"}));
    ok(json!(saved))
}

async fn update_memory(Path(mid): Path<i64>, Body(d): Body) -> Res {
    let m = memories::get(mid).ok_or_else(|| ApiError::not_found(t!("没有这条记忆")))?;
    let mut owner = m["bot_id"].as_i64();
    if d.get("shared").is_some() {
        owner = if py::truthy(&d["shared"]) {
            None
        } else {
            m["bot_id"].as_i64().or_else(|| d["bot_id"].as_i64().filter(|_| d["bot_id"].is_i64()))
        };
        if !py::truthy(&d["shared"]) && !owner.is_some_and(|o| bots::get(o).is_some()) {
            return Err(ApiError::bad(t!("改成只给某个 Bot 时要说明是哪个 Bot")));
        }
    }
    let until = memories::parse_until(d.get("until"))?;
    let r = memories::update(mid, d["text"].as_str(), Some(owner))?;
    if let Some(u) = until {
        memories::set_until(mid, u);
    }
    if let Some(p) = d.get("pinned").filter(|p| !p.is_null()) {
        memories::set_pinned(mid, py::truthy(p));
    }
    broadcast(json!({"type": "memories_changed"}));
    ok(if until.is_some() || d.get("pinned").is_some() { memories::get(mid).unwrap_or(r) } else { r })
}

async fn delete_memory(Path(mid): Path<i64>) -> Res {
    memories::delete(mid);
    broadcast(json!({"type": "memories_changed"}));
    ok(json!({"ok": true}))
}

// ---------- 前端 ----------
pub fn dist() -> PathBuf {
    match std::env::var_os("OSA_WEB_DIST") {
        Some(d) => PathBuf::from(d),
        None => config::ROOT.join("osa").join("static").join("dist"),
    }
}

static BUILD: LazyLock<Mutex<(Option<(u64, i128, u64)>, String)>> = LazyLock::new(|| Mutex::new((None, String::new())));

pub fn web_build() -> String {
    let f = dist().join("index.html");
    let Ok(md) = std::fs::metadata(&f) else { return String::new() };
    #[cfg(unix)]
    let key = {
        use std::os::unix::fs::MetadataExt;
        (md.ino(), md.mtime() as i128 * 1_000_000_000 + md.mtime_nsec() as i128, md.size())
    };
    #[cfg(not(unix))]
    let key = (
        0u64,
        md.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_nanos() as i128).unwrap_or(0),
        md.len(),
    );
    let mut b = BUILD.lock();
    if b.0 != Some(key) {
        let text = std::fs::read_to_string(&f).unwrap_or_default();
        static RE: LazyLock<regex::Regex> = LazyLock::new(|| regex::Regex::new(r#"<meta name="osa-build" content="([^"]+)""#).unwrap());
        *b = (Some(key), RE.captures(&text).map(|c| c[1].to_string()).unwrap_or_default());
    }
    b.1.clone()
}

async fn current_build() -> Res {
    ok(json!({"build": web_build()}))
}

fn file_response(path: &std::path::Path, no_cache: bool, immutable: bool) -> Response {
    let Ok(bytes) = std::fs::read(path) else { return StatusCode::NOT_FOUND.into_response() };
    let mime = mime_guess::from_path(path).first_or_octet_stream();
    let ct = if mime.type_() == "text" || mime.essence_str() == "application/javascript" {
        format!("{}; charset=utf-8", mime.essence_str())
    } else {
        mime.essence_str().to_string()
    };
    let mut r = Response::new(axum::body::Body::from(bytes));
    r.headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_str(&ct).unwrap_or(HeaderValue::from_static("application/octet-stream")));
    if no_cache {
        r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    } else if immutable {
        r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("public, max-age=31536000, immutable"));
    }
    r
}

async fn assets(Path(p): Path<String>) -> Response {
    let base = dist().join("assets");
    let f = base.join(&p);
    match (std::fs::canonicalize(&f), std::fs::canonicalize(&base)) {
        (Ok(f), Ok(b)) if f.starts_with(&b) && f.is_file() => file_response(&f, false, true),
        _ => (StatusCode::NOT_FOUND, axum::Json(json!({"detail": "Not Found"}))).into_response(),
    }
}

pub async fn fallback(req: Request) -> Response {
    let path = req.uri().path().to_string();
    if path.starts_with("/api/") || path.starts_with("/hooks/") || path.starts_with("/oauth/") {
        return todo(&format!("{} {path}", req.method())).into_response();
    }
    if req.method() != "GET" && req.method() != "HEAD" {
        return (StatusCode::METHOD_NOT_ALLOWED, axum::Json(json!({"detail": "Method Not Allowed"}))).into_response();
    }
    let rel = path.trim_start_matches('/');
    let d = dist();
    if !rel.is_empty() {
        let f = d.join(rel);
        if let (Ok(f), Ok(base)) = (std::fs::canonicalize(&f), std::fs::canonicalize(&d))
            && f.starts_with(&base)
            && f.is_file()
        {
            let name = f.file_name().and_then(|n| n.to_str()).unwrap_or("");
            return file_response(&f, name == "index.html" || name == "sw.js", false);
        }
    }
    let index = d.join("index.html");
    if !index.exists() {
        return file_response(&config::ROOT.join("osa").join("static").join("index.html"), true, false);
    }
    file_response(&index, true, false)
}

/// 压缩哪些响应：默认的规则（够大、不是图片和事件流）之外，视频和分段（206 / 带 Content-Range）的不压，
/// 不然 Accept-Ranges 会被去掉、分段播放就坏了
fn compressible() -> impl tower_http::compression::Predicate {
    use tower_http::compression::predicate::{DefaultPredicate, NotForContentType, Predicate};
    DefaultPredicate::new().and(NotForContentType::const_new("video/")).and(
        |status: StatusCode, _: axum::http::Version, h: &HeaderMap, _: &axum::http::Extensions| {
            status != StatusCode::PARTIAL_CONTENT && !h.contains_key(header::CONTENT_RANGE)
        },
    )
}

/// 小的请求体先整个读进来，再交给处理函数。有的接口不看请求体（撤销整理、取消任务、标记已读……），而请求体没读完的话，
/// hyper 回完这次的话就把连接关掉，客户端紧接着在同一条连接上发的下一个请求会落空（“服务端没回应就断开”，接口测试在 Linux 上撞到过）。
/// 大的（上传文件）、不知道多长的照旧边收边处理。
async fn read_small_body(req: Request, next: axum::middleware::Next) -> Response {
    const MAX: usize = 256 * 1024;
    let len = req.headers().get(header::CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<usize>().ok());
    match len {
        Some(n) if n > 0 && n <= MAX => {
            let (parts, body) = req.into_parts();
            match axum::body::to_bytes(body, MAX).await {
                Ok(bytes) => next.run(Request::from_parts(parts, axum::body::Body::from(bytes))).await,
                Err(_) => StatusCode::BAD_REQUEST.into_response(),
            }
        }
        _ => next.run(req).await,
    }
}

pub fn app() -> Router {
    use tower_http::compression::CompressionLayer;
    Router::new()
        .route("/login", get(auth::login_page).post(auth::login))
        .route("/login/ticket", get(auth::use_ticket).post(auth::login_ticket))
        .route("/api/pair", post(auth::pair))
        .route("/logout", post(auth::logout))
        .route("/api/me", get(auth::me))
        .route("/api/bootstrap", get(bootstrap))
        .route("/api/bots", post(create_bot))
        .route("/api/bots/{bot_id}", patch(update_bot).delete(delete_bot))
        .route("/api/bots/{bot_id}/primary", post(make_primary))
        .route("/api/chats/{chat_id}/messages", get(messages))
        .route("/api/chats/{chat_id}/seen", post(chat_seen))
        .route("/api/chats/{chat_id}/threads", get(list_threads).post(open_thread))
        .route("/api/threads/{tid}", get(get_thread_by_id))
        .route("/api/threads/{tid}/seen", post(thread_seen))
        .route("/api/chats/{chat_id}/threads/{root}", get(get_thread))
        .route("/api/groups", post(create_group))
        .route("/api/chats/{chat_id}", patch(update_group).delete(delete_group))
        .route("/api/chats/{chat_id}/pin", post(pin_chat))
        .route("/api/chats/{chat_id}/reset", post(new_topic))
        .route("/api/tasks", get(list_tasks))
        .route("/api/tasks/{task_id}", get(task_detail))
        .route("/api/tasks/{task_id}/cancel", post(cancel_task))
        .route("/api/bots/{bot_id}/memories", get(list_memories).post(add_memory))
        .route("/api/bots/{bot_id}/memories/import", post(import_memories))
        .route("/api/memories/{mid}", patch(update_memory).delete(delete_memory))
        .route("/api/build", get(current_build))
        .route("/ws", get(ws_handler))
        .route("/assets/{*p}", get(assets))
        .merge(crate::routes::extra())
        .fallback(fallback)
        .layer(axum::middleware::from_fn(auth::middleware))
        .layer(axum::middleware::from_fn(read_small_body))
        .layer(CompressionLayer::new().br(true).gzip(true).compress_when(compressible()))
}

// ---------- 启动和停止 ----------
pub fn migrate() {
    crate::backup::apply_pending();
    memory::migrate();
    bots::migrate();
    crate::vault::migrate();
    approvals::migrate();
    memories::migrate();
    crate::modules_migrate();
}

pub async fn startup() {
    if !auth::enabled() {
        println!("未设置 OSA_PASSWORD：网页不用登录就能访问，只能在本机或内网使用");
    }
    bots::ensure_default();
    tasklog::startup();
    settings::apply();
    crate::builtin::load(); // 内置的事件来源要先登记好，导入旧版触发器时要用
    if let Some(p) = bots::primary() {
        routines::import_legacy(p["id"].as_i64().unwrap_or(0));
    }
    mcp_hub::start().await;
    crate::docs::start().await;
    crate::channels::start().await;
    tokio::spawn(crate::watchdog::watchdog());
    tokio::spawn(routines::scheduler());
    tokio::spawn(tasklog::prune_loop());
    crate::background();
    if let Some(pid) = std::env::var("OSA_PARENT_PID").ok().and_then(|p| p.parse::<i32>().ok()) {
        tokio::spawn(parent_watch(pid));
    }
}

async fn parent_watch(pid: i32) {
    #[cfg(unix)]
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        // kill(pid, 0)：进程不在了就退出
        let alive = unsafe { libc::kill(pid, 0) } == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM);
        if !alive {
            println!("启动服务的 App 已经退出，服务跟着停止");
            let _ = SHUTDOWN.0.send(true);
            return;
        }
    }
}

pub static SHUTDOWN: LazyLock<(tokio::sync::watch::Sender<bool>, tokio::sync::watch::Receiver<bool>)> =
    LazyLock::new(|| tokio::sync::watch::channel(false));

pub async fn shutdown() {
    crate::taskstore::stop_tasks().await; // 等正在执行的那一步做完、存好（最多 limits.shutdown_wait_s）
    crate::channels::stop().await;
    mcp_hub::stop().await;
    crate::docs::stop().await;
    crate::computer::shutdown().await;
}
