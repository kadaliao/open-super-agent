//! 写操作前的人工确认，以及确认规则。对应 osa/approvals.py（规则的写法见那里的说明）。

use crate::db::{self, RowExt};
use crate::task::Task;
use crate::util::{fnmatch, normpath};
use crate::{config, i18n, memory, py, server};
use parking_lot::Mutex;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::{Arc, LazyLock};
use tokio::sync::oneshot;

pub fn migrate() {
    db::batch("create table if not exists rules(id integer primary key, pattern text, action text, note text default '', created real)");
}

/// 卡片的编号。存在 settings 里（approval_seq），重启后接着往下数：聊天软件里还留着重启前的旧卡片，钉钉是打字回“允许一次 #编号”，
/// 编号要是从 1 重新数，点旧卡片就可能批准到重启后另一件同号的事（重启后任务会接着做、重新弹卡片）。
fn next_id() -> i64 {
    static LOCK: Mutex<i64> = Mutex::new(0);
    let mut last = LOCK.lock();
    if *last == 0 {
        *last = memory::setting_raw("approval_seq").and_then(|v| v.trim().parse::<i64>().ok()).unwrap_or(0).max(0);
    }
    *last += 1;
    memory::set_setting_raw("approval_seq", &last.to_string());
    *last
}

pub struct Pending {
    pub item: Value,
    pub tx: Option<oneshot::Sender<(bool, String)>>,
    pub args: Value,
}

pub static PENDING: LazyLock<Mutex<BTreeMap<i64, Pending>>> = LazyLock::new(Default::default);

struct Grant {
    pattern: String,
    task_id: Option<Value>,
    until: Option<f64>,
}

static GRANTS: LazyLock<Mutex<Vec<Grant>>> = LazyLock::new(Default::default);
pub static LAST_DONE: Mutex<f64> = Mutex::new(0.0);

pub const DEFAULT_WORDS: [&str; 25] = [
    "create", "update", "delete", "remove", "archive", "append", "patch", "post", "write", "edit", "move", "send", "insert", "upload",
    "publish", "comment", "set_", "add_", "forward", "reply", "trash", "share", "cancel", "rename", "transfer",
];

/// 应用的默认策略（confirm: auto / all / none / 词列表）。read_only_hint：服务端在工具上标的 readOnlyHint
pub fn default_confirm(rule: &Value, tool_name: &str, read_only_hint: Option<bool>) -> bool {
    match rule.as_str() {
        Some("none") => return false,
        Some("all") => return true,
        Some("auto") if read_only_hint == Some(true) => return false,
        _ => {}
    }
    let words: Vec<String> = match rule {
        Value::String(s) if s == "auto" => DEFAULT_WORDS.iter().map(|s| s.to_string()).collect(),
        Value::Array(a) => a.iter().map(py::str_of).collect(),
        Value::String(s) => s.chars().map(|c| c.to_string()).collect(),
        _ => DEFAULT_WORDS.iter().map(|s| s.to_string()).collect(),
    };
    let name = tool_name.to_lowercase();
    words.iter().any(|w| name.contains(&w.to_lowercase()))
}

const READ_VERBS: [&str; 12] = ["get", "list", "search", "read", "fetch", "find", "lookup", "view", "show", "describe", "count", "ask"];
const NOT_READ: [&str; 26] = [
    "and", "or", "then", "mark", "save", "replace", "clear", "reset", "run", "execute", "exec", "apply", "merge", "toggle", "sync",
    "submit", "lock", "unlock", "upsert", "put", "modify", "change", "trigger", "sql", "", "",
];

/// 应用的这个工具是不是只读
pub fn read_only(tool_name: &str, read_only_hint: Option<bool>, app: &str) -> bool {
    if let Some(ro) = read_only_hint {
        return ro;
    }
    static CAMEL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"([a-z0-9])([A-Z])").unwrap());
    static SPLIT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());
    let name = CAMEL.replace_all(tool_name, "${1}_${2}").to_lowercase();
    let mut words: Vec<&str> = SPLIT.split(&name).filter(|w| !w.is_empty()).collect();
    if !words.is_empty() && words[0] == app.to_lowercase() {
        words.remove(0);
    }
    !words.is_empty()
        && READ_VERBS.contains(&words[0])
        && !words.iter().any(|w| !w.is_empty() && NOT_READ.contains(w))
        && !DEFAULT_WORDS.iter().any(|w| name.contains(w))
}

// ---------- 规则 ----------
pub fn rules() -> Vec<Value> {
    db::rows("select id, pattern, action, note, created from rules order by id", [])
        .into_iter()
        .map(|r| {
            let n = r.s(3);
            json!({"id": r[0], "pattern": r[1], "action": r[2], "note": if n.is_empty() { n } else { t!(&n) }, "created": r[4]})
        })
        .collect()
}

pub fn add_rule(pattern: &str, action: &str, note: &str) -> Result<Value, String> {
    let pattern = pattern.trim();
    if pattern.is_empty() {
        return Err(t!("规则不能为空"));
    }
    if action != "allow" && action != "ask" {
        return Err(t!("action 只能是 allow 或 ask"));
    }
    let id = match db::row("select id from rules where pattern=? and action=?", params![pattern, action]) {
        Some(r) => r.i(0),
        None => db::insert("insert into rules(pattern, action, note, created) values(?,?,?,?)", params![pattern, action, note, py::now()]),
    };
    Ok(rules().into_iter().find(|r| r["id"].as_i64() == Some(id)).unwrap_or(Value::Null))
}

pub fn delete_rule(rid: i64) {
    db::exec("delete from rules where id=?", params![rid]);
}

pub const PC_ASK: [&str; 5] = ["browser_upload", "computer_write_file", "browser_submit", "desktop", "desktop_submit"];
pub const ASK: [&str; 3] = ["generate_image", "generate_video", "use_mac"];
pub const HOSTS: [&str; 2] = ["host", "mac"];
pub const SHELL: &str = "computer_shell";
pub const MAX_MINUTES: i64 = 24 * 60;
static UNSAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[;&|<>`$()\n\r]").unwrap());
static PARTS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[;&|\n\r]+|\$\(|[`()]").unwrap());

fn pc_tools(t: &str) -> bool {
    PC_ASK.contains(&t) || t == SHELL
}

fn params_of(tool: &str) -> Vec<String> {
    let schema = crate::registry::get(tool).map(|t| t.schema.clone()).or_else(|| {
        if tool.starts_with("computer_") || tool.starts_with("browser_") || tool.starts_with("desktop_") {
            crate::assets::computer_schema(tool)
        } else {
            None
        }
    });
    schema.and_then(|s| s["function"]["parameters"]["properties"].as_object().map(|o| o.keys().cloned().collect())).unwrap_or_default()
}

/// 规则冒号后面比的值
pub fn subject(tool: &str, args: &Value) -> Option<String> {
    let a = args.as_object()?;
    if tool == SHELL {
        let v = a.get("cmd").or_else(|| a.get("命令"))?;
        return v.as_str().map(|s| s.trim().to_string());
    }
    let order: Vec<String> = params_of(tool).into_iter().filter(|k| a.contains_key(k)).collect();
    let rest: Vec<String> = a.keys().filter(|k| !order.contains(k)).cloned().collect();
    for k in order.iter().chain(rest.iter()) {
        if let Some(Value::String(v)) = a.get(k) {
            return Some(if v.starts_with('/') { normpath(v) } else { v.clone() });
        }
    }
    None
}

fn hit(pattern: &str, tool: &str, value: Option<&str>, strict: bool) -> bool {
    if pattern == tool {
        return true;
    }
    let Some((name, arg)) = pattern.split_once(':') else {
        return fnmatch(tool, pattern);
    };
    let Some(value) = value else { return false };
    if !fnmatch(tool, name) {
        return false;
    }
    if strict && tool == SHELL && UNSAFE.is_match(value) {
        return false;
    }
    fnmatch(value, arg) || (arg.ends_with(" *") && value == &arg[..arg.len() - 2])
}

fn asks(pattern: &str, tool: &str, value: Option<&str>) -> bool {
    if hit(pattern, tool, value, false) {
        return true;
    }
    if let Some(v) = value.filter(|v| tool == SHELL && !v.is_empty() && pattern.contains(':')) {
        return PARTS.split(v).map(|p| p.trim()).filter(|p| !p.is_empty()).any(|p| hit(pattern, tool, Some(p), false));
    }
    false
}

/// 返回 (是否要确认, 原因)
pub fn decide(tool: &str, backend: Option<&str>, args: &Value) -> (bool, String) {
    let value = subject(tool, args);
    let v = value.as_deref();
    let host_shell = tool == SHELL && backend.is_some_and(|b| HOSTS.contains(&b));
    let rs = rules();
    for r in &rs {
        let p = r["pattern"].as_str().unwrap_or("");
        if r["action"] == "ask" && asks(p, tool, v) {
            return (true, format!("规则「{p}」要求先问"));
        }
    }
    for r in &rs {
        let p = r["pattern"].as_str().unwrap_or("");
        if r["action"] == "allow" && (p.contains(':') || !host_shell) && hit(p, tool, v, true) {
            return (false, format!("规则「{p}」自动允许"));
        }
    }
    if host_shell {
        return (true, "本机电脑的终端每条命令都要确认".into());
    }
    if pc_tools(tool) {
        return (backend.is_some_and(|b| HOSTS.contains(&b)) && PC_ASK.contains(&tool), "默认策略".into());
    }
    if ASK.contains(&tool) {
        return (true, "默认策略".into());
    }
    (crate::mcp_hub::needs_confirm(tool), "默认策略".into())
}

// ---------- 临时允许 ----------
pub fn granted(task_id: &Value, tool: &str, value: Option<&str>) -> bool {
    let now = py::now();
    let mut g = GRANTS.lock();
    g.retain(|x| x.until.is_none_or(|u| u > now));
    g.iter().any(|x| (x.task_id.is_none() || x.task_id.as_ref() == Some(task_id)) && hit(&x.pattern, tool, value, true))
}

pub fn end_task(task_id: i64) {
    GRANTS.lock().retain(|g| g.task_id.is_none() || g.task_id != Some(json!(task_id)));
}

pub fn first_word(cmd: Option<&str>) -> String {
    match cmd.unwrap_or("").split_whitespace().next() {
        Some(w) if prefix_ok(w) => w.to_string(),
        _ => String::new(),
    }
}

fn prefix_ok(p: &str) -> bool {
    !p.is_empty() && !"*?[".contains(p.chars().next().unwrap_or(' ')) && !UNSAFE.is_match(p)
}

fn scope_of(item: &Value, ok: bool, scope: &str) -> (String, i64) {
    static MIN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^minutes:(\d{1,6})$").unwrap());
    let mut s = scope.trim().to_lowercase();
    if s.is_empty() {
        s = "once".into();
    }
    let mut minutes = 0;
    if let Some(c) = MIN.captures(&s) {
        minutes = c[1].parse::<i64>().unwrap_or(0).min(MAX_MINUTES);
        s = "minutes".into();
    }
    let allowed: Vec<String> = item["scopes"]
        .as_array()
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_else(|| vec!["once".into()]);
    if s == "minutes" {
        s = if minutes > 0 && allowed.iter().any(|x| x == "always") { "minutes".into() } else { "once".into() };
    } else if !allowed.contains(&s) {
        s = "once".into();
    }
    if ok && s != "once" { (s, minutes) } else { ("once".into(), 0) }
}

fn allow_pattern(item: &Value, prefix: Option<&str>) -> (String, String) {
    let tool = item["tool"].as_str().unwrap_or("");
    if tool != SHELL {
        return (tool.to_string(), String::new());
    }
    let mut p = prefix.map(|p| p.split_whitespace().collect::<Vec<_>>().join(" ")).unwrap_or_default();
    if !prefix_ok(&p) {
        p = item["prefix"].as_str().unwrap_or("").to_string();
    }
    if p.is_empty() {
        return (String::new(), String::new());
    }
    let pat = if p.ends_with('*') { format!("{SHELL}:{p}") } else { format!("{SHELL}:{p} *") };
    (pat, p.trim_end_matches('*').trim().to_string())
}

/// 确认请求的参数
#[derive(Default)]
pub struct Req<'a> {
    pub kind: &'a str,
    pub once: bool,
    pub limit: usize,
    pub editable: &'a [&'a str],
}

/// 卡片还开着时的收尾：被取消（future 被丢掉）也要清掉
struct Guard {
    aid: i64,
    task: Arc<Task>,
    t0: f64,
}

impl Drop for Guard {
    fn drop(&mut self) {
        *LAST_DONE.lock() = py::now();
        PENDING.lock().remove(&self.aid);
        let mut s = self.task.st.lock();
        s.waiting_approval = None;
        s.last_progress = py::now();
        s.paused_s += py::now() - self.t0;
    }
}

/// 创建确认请求并等待用户决定。返回 (是否允许, 用户附言, 改过以后的参数)
pub async fn request(task: &Arc<Task>, tool: &str, args: &Value, req: Req<'_>) -> (bool, String, Value) {
    request_with(task, tool, args, req, &Value::Null).await
}

/// 同上，卡片上多带几个字段（任务提问的 question、options），原样放进公开字段
pub async fn request_with(task: &Arc<Task>, tool: &str, args: &Value, req: Req<'_>, extra: &Value) -> (bool, String, Value) {
    let kind = if req.kind.is_empty() { "approval" } else { req.kind };
    let limit = if req.limit == 0 { 3000 } else { req.limit };
    let value = subject(tool, args);
    if kind == "approval" && !req.once && granted(&task.id_value(), tool, value.as_deref()) {
        return (true, String::new(), args.clone());
    }
    let mut scopes = vec!["once"];
    if kind == "approval" && !req.once {
        if task.fake_id.is_none() {
            scopes.push("task");
        }
        if tool != SHELL || !first_word(value.as_deref()).is_empty() {
            scopes.push("always");
        }
    }
    let aid = next_id();
    let (tx, rx) = oneshot::channel();
    let timeout = if task.background { Some(config::get_i64("limits.approval_timeout_s", 1800)) } else { None };
    let chat_id = if task.approval_chat_id != 0 { task.approval_chat_id } else { task.chat_id };
    let th = memory::thread_of(task.chat_id);
    let now = py::now();
    let mut item = json!({"id": aid, "kind": kind, "task_id": task.id_value(), "bot_id": task.bot_id, "chat_id": chat_id,
        "origin_chat_id": th.map(|t| t.0).unwrap_or(task.chat_id)});
    if th.is_some() {
        item["origin_thread"] = json!(task.chat_id);
    }
    item["tool"] = json!(tool);
    if task.mac != 0 {
        item["mac"] = json!(task.mac);
        item["mac_name"] = json!(task.mac_name);
    }
    item["goal"] = json!(py::head(&task.goal, 80));
    item["args"] = json!(py::head(&py::dumps_indent(args, 1, false), limit));
    item["ts"] = json!(now);
    item["expires"] = match timeout {
        Some(t) => json!(now + t as f64),
        None => Value::Null,
    };
    if req.once {
        item["once"] = json!(true);
    }
    if kind == "approval" {
        item["scopes"] = json!(scopes);
    }
    if tool == SHELL {
        item["prefix"] = json!(first_word(value.as_deref()));
        if UNSAFE.is_match(value.as_deref().unwrap_or("")) {
            item["compound"] = json!(true);
        }
    }
    if !req.editable.is_empty() {
        item["editable"] = json!(req.editable.iter().filter(|k| args[**k].is_string()).collect::<Vec<_>>());
    }
    if let Some(extra) = extra.as_object() {
        for (k, v) in extra {
            item[k] = v.clone();
        }
    }
    PENDING.lock().insert(aid, Pending { item: item.clone(), tx: Some(tx), args: args.clone() });
    task.st.lock().waiting_approval = Some(aid);
    let guard = Guard { aid, task: task.clone(), t0: py::now() };
    let mut ev = public(&item);
    ev["type"] = json!("approval");
    ev["status"] = json!("pending");
    server::broadcast(ev);
    let got = match timeout {
        Some(t) => match tokio::time::timeout(std::time::Duration::from_secs(t.max(0) as u64), rx).await {
            Ok(r) => r.ok(),
            Err(_) => {
                server::broadcast(json!({"type": "approval", "id": aid, "status": "expired", "chat_id": chat_id}));
                Some((false, format!("{} 分钟内没有确认，按拒绝处理", t / 60)))
            }
        },
        None => rx.await.ok(),
    };
    let args_after = PENDING.lock().get(&aid).map(|p| p.args.clone()).unwrap_or_else(|| args.clone());
    drop(guard);
    let (ok, note) = got.unwrap_or((false, "任务已取消".into()));
    (ok, note, args_after)
}

pub fn resolve(aid: i64, ok: bool, note: &str, always: bool, edits: &Value, scope: Option<&str>, prefix: Option<&str>) -> String {
    let mut pend = PENDING.lock();
    let Some(p) = pend.get_mut(&aid).filter(|p| p.tx.is_some()) else {
        return t!("没有待确认的请求 #{id}", id = aid);
    };
    let item = p.item.clone();
    let sc = scope.map(String::from).unwrap_or_else(|| if always { "always".into() } else { "once".into() });
    let (mut scope, minutes) = scope_of(&item, ok, &sc);
    let (pattern, shown) =
        if scope == "always" || scope == "minutes" { allow_pattern(&item, prefix) } else { (String::new(), String::new()) };
    if (scope == "always" || scope == "minutes") && pattern.is_empty() {
        scope = "once".into();
    }
    let tool = item["tool"].as_str().unwrap_or("").to_string();
    if scope == "always" {
        let note =
            if tool == SHELL { "在确认卡片上点了总是允许这个命令前缀" } else { "在确认卡片上点了总是允许" };
        let _ = add_rule(&pattern, "allow", note);
    } else if scope == "minutes" {
        GRANTS.lock().push(Grant { pattern: pattern.clone(), task_id: None, until: Some(py::now() + minutes as f64 * 60.0) });
    } else if scope == "task" {
        GRANTS.lock().push(Grant { pattern: tool.clone(), task_id: Some(item["task_id"].clone()), until: None });
    }
    let mut edited: Vec<String> = vec![];
    if ok && let Some(e) = edits.as_object() {
        let allowed: Vec<String> =
            item["editable"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
        for (k, v) in e {
            let Some(v) = v.as_str().map(|s| s.trim().to_string()) else { continue };
            if allowed.contains(k) && !v.is_empty() && p.args[k].as_str().map(|s| s.trim()) != Some(v.as_str()) {
                p.args[k] = json!(v);
                edited.push(k.clone());
            }
        }
    }
    if let Some(tx) = p.tx.take() {
        let _ = tx.send((ok, note.to_string()));
    }
    drop(pend);
    let mut ev = json!({"type": "approval", "id": aid, "chat_id": item["chat_id"], "status": if ok { "approved" } else { "rejected" },
                        "always": scope == "always", "scope": scope});
    if scope == "always" {
        ev["rule"] = json!(pattern);
    }
    ev["edited"] = json!(edited);
    // 提问的回答、说不准那一步选了什么（done / stop）：带上，聊天软件和别的窗口才写得出“已回答：……”“已经做成了”
    if item["kind"] == "question" || !item["doubt"].is_null() {
        ev["note"] = json!(note);
    }
    tokio::spawn(async move { server::broadcast(ev) });
    let mut out = if ok { t!("已允许请求 #{id}", id = aid) } else { t!("已拒绝请求 #{id}", id = aid) };
    if scope == "always" {
        out += &if !shown.is_empty() {
            t!("，以后「{prefix}」开头的命令自动允许", prefix = shown)
        } else {
            t!("，以后同类操作自动允许")
        };
    } else if scope == "minutes" {
        out += &t!("，{n} 分钟内同类操作不再问", n = minutes);
    } else if scope == "task" {
        out += &t!("，这个任务里同类操作不再问");
    }
    out
}

fn cancel_where(pred: impl Fn(&Value) -> bool, why: &str) {
    let mut evs = vec![];
    {
        let mut pend = PENDING.lock();
        for (aid, p) in pend.iter_mut() {
            if pred(&p.item)
                && let Some(tx) = p.tx.take()
            {
                let _ = tx.send((false, why.to_string()));
                evs.push(json!({"type": "approval", "id": aid, "status": "cancelled", "chat_id": p.item["chat_id"]}));
            }
        }
    }
    for e in evs {
        tokio::spawn(async move { server::broadcast(e) });
    }
}

pub fn cancel_for_task(task_id: i64) {
    end_task(task_id);
    cancel_where(|i| i["task_id"].as_i64() == Some(task_id), "任务已取消");
}

pub fn cancel_for_bot(bot_id: i64) {
    cancel_where(|i| i["bot_id"].as_i64() == Some(bot_id), "Bot 已删除");
}

pub fn public(item: &Value) -> Value {
    let mut o = item.clone();
    if let Some(m) = o.as_object_mut() {
        m.retain(|k, _| !k.starts_with('_'));
    }
    o
}

pub fn pending_public() -> Vec<Value> {
    PENDING.lock().values().map(|p| public(&p.item)).collect()
}

pub fn pending_count() -> usize {
    PENDING.lock().len()
}

pub fn summary(bot_id: Option<i64>) -> String {
    let s: Vec<String> = PENDING
        .lock()
        .values()
        .filter(|p| bot_id.is_none() || p.item["bot_id"].as_i64() == bot_id)
        .map(|p| {
            let a = &p.item;
            format!(
                "#{}（任务 #{}）{} 参数：{}",
                py::str_of(&a["id"]),
                py::str_of(&a["task_id"]),
                py::str_of(&a["tool"]),
                py::head(a["args"].as_str().unwrap_or(""), 200)
            )
        })
        .collect();
    if s.is_empty() { "无".into() } else { s.join("\n") }
}

#[allow(dead_code)]
fn _i(_: &i18n::Later) {}
