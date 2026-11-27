//! Router：常驻会话，负责和用户对话、派发任务。自己不执行工具（对应 DeepSeek 设计里 Router 无 shell）。
//! 一个 Bot 一个 Router，既在私聊里说话，也在它所在的群聊里说话。对应 osa/router.py。

use crate::computer::tools as ctools;
use crate::dm::{self, Turn};
use crate::registry::{self, Call, Out, PostFn, ROUTER};
use crate::task::{Handoff, NewTask, Task};
use crate::{
    approvals, assets, botadmin, bots, catalog, config, dsml, groups, i18n, mcp_hub, media, memories, memory, providers, py, routines,
    security, server, skills, tasklog, taskstore, worker,
};
use futures::future::BoxFuture;
use parking_lot::Mutex;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, LazyLock};
use std::time::Instant;

pub const ROUTER_PROMPT: &str = include_str!("prompts/router.txt");
pub const ATTACH_BLIND: &str = "- 消息里的“[附件] 路径”是用户上传到电脑里的文件；要读或处理它就派任务，goal 里写上完整路径。";
pub const ATTACH_SEE: &str = "- 消息里的“[附件] 路径”是用户上传到电脑里的文件。用户发的图片会直接附在消息里给你看（只带最近几张，更早的只剩路径）：看图就能回答的直接回答，不用派任务；要处理文件（读文档、裁剪图片、转格式、传到网页上）才派任务，goal 里写上完整路径。";
pub const STATE_PROMPT: &str = include_str!("prompts/state.txt");
pub const GROUP_PROMPT: &str = include_str!("prompts/group.txt");
pub const DM_TURN: &str = include_str!("prompts/dm_turn.txt");
pub const DOC_COMMENT_TURN: &str = include_str!("prompts/doc_comment_turn.txt");
pub const PRIMARY_RULE: &str = include_str!("prompts/primary_rule.txt");
pub const OTHER_RULE: &str = "- 管理 Bot 的事归主 Bot「{primary}」：用户要新建、删除 Bot，或者要改别的 Bot，或者你觉得该有个专门的 Bot 时，用 message_bot 私信它（它就在这个群里的话用 handoff），把要做什么写完整，由它请用户确认后去做。改你自己（名字、职责、头像、技能）用 update_bot，同样会弹卡片请用户确认。不要让用户自己去设置里做。定时任务和事件你只能管自己的，要动别的 Bot 的也找主 Bot（它直接改，不用确认）。\n";
pub const REPLY_TURN: &str = "\n\n这一轮是因为 {name} 回了你的私信（见最近的消息）。";
pub const REPLY_SHOWN: &str = "回信已经显示在对话里了，不用整段复述：说清结论、要用户做什么，或者接下来怎么办。回信说任务被取消或失败了，照实告诉用户就行，不要自己重派、拆开重做或换个 Bot 再做，等用户决定。";
pub const REPLY_OWED: &str =
    "你这一轮的回复会自动作为回信发给 {owed}，它看不到 {name} 的回信，把它要的内容写全；还需要别人帮忙可以继续派任务或私信。";
pub const BOTADMIN_TURN: &str = include_str!("prompts/botadmin_turn.txt");
pub const BOTADMIN_OWED: &str = "\n这是 {owed} 私信请你做的：你这一轮的回复会自动作为回信发给它，把结果写清楚，它看不到这里的系统通知。";
pub const BOTADMIN_EDITED: &str = "\n用户在卡片上改过{fields}才同意的，以改过的为准：要知道现在是怎么写的，用 get_bot 看。";

pub const DIRECT_PC: [&str; 13] = [
    "computer_screenshot",
    "computer_click",
    "computer_type",
    "computer_key",
    "computer_scroll",
    "browser_open",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_key",
    "browser_scroll",
    "browser_back",
    "browser_read",
];

fn router_env() -> Value {
    json!({"scope": "router"})
}

pub static TASKS: LazyLock<Mutex<BTreeMap<i64, Arc<Task>>>> = LazyLock::new(Default::default);
static RUNTIMES: LazyLock<Mutex<HashMap<i64, Arc<Router>>>> = LazyLock::new(Default::default);

pub fn task(id: i64) -> Option<Arc<Task>> {
    TASKS.lock().get(&id).cloned()
}

pub fn tasks() -> Vec<Arc<Task>> {
    TASKS.lock().values().cloned().collect()
}

/// 任务卡片上的进度（Worker 每做一个动作推一次）
pub fn progress(task: &Task) {
    server::broadcast(json!({"type": "task", "chat_id": task.chat_id, "bot_id": task.bot_id, "task": task.public()}));
}

/// 把这一轮的状态接在最新一条用户消息后面
pub fn with_state(mut history: Vec<Value>, state: &str) -> Vec<Value> {
    if let Some(last) = history.last_mut().filter(|m| m["role"] == "user") {
        if let Some(s) = last["content"].as_str() {
            last["content"] = json!(format!("{s}{state}"));
            return history;
        }
        if let Some(a) = last["content"].as_array_mut() {
            a.push(json!({"type": "text", "text": state.trim_start()}));
            return history;
        }
    }
    history.push(json!({"role": "user", "content": state.trim_start()}));
    history
}

pub fn runtime(bot_id: i64) -> Arc<Router> {
    RUNTIMES.lock().entry(bot_id).or_insert_with(|| Arc::new(Router { bot_id, lock: tokio::sync::Mutex::new(()) })).clone()
}

pub fn forget_runtime(bot_id: i64) {
    RUNTIMES.lock().remove(&bot_id);
}

pub fn cancel(tid: i64, reason: &str) -> String {
    match task(tid) {
        Some(t) => runtime(t.bot_id).cancel(tid, reason),
        None => t!("没有任务 #{id}", id = tid),
    }
}

pub struct Router {
    pub bot_id: i64,
    lock: tokio::sync::Mutex<()>,
}

/// 一轮里的状态（Python 版放在 self 上；一个 Bot 一次只说一轮，这里单独传）
pub struct Ctx {
    pub chat: Value,
    pub turn: Turn,
    pub turn_tasks: Vec<i64>,
    pub turn_dms: i64,
    pub turn_waits: i64,
    pub files: Arc<Mutex<Vec<Value>>>,
}

pub fn fmt(template: &str, kv: &[(&str, &str)]) -> String {
    // str.format：只替换给了的 {名字}
    let mut out = String::with_capacity(template.len() + 256);
    let mut rest = template;
    while let Some(i) = rest.find('{') {
        out.push_str(&rest[..i]);
        let after = &rest[i + 1..];
        match after.find('}') {
            Some(j) if kv.iter().any(|(k, _)| *k == &after[..j]) => {
                let key = &after[..j];
                out.push_str(kv.iter().find(|(k, _)| *k == key).map(|(_, v)| *v).unwrap_or(""));
                rest = &after[j + 1..];
            }
            _ => {
                out.push('{');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

impl Router {
    pub fn bot(&self) -> Value {
        bots::get(self.bot_id).unwrap_or(Value::Null)
    }

    pub fn chat_id(&self) -> i64 {
        self.bot()["chat_id"].as_i64().unwrap_or(0)
    }

    pub fn tasks(&self) -> Vec<Arc<Task>> {
        TASKS.lock().values().filter(|t| t.bot_id == self.bot_id).cloned().collect()
    }

    pub fn emit(&self, mut event: Value, chat_id: Option<i64>) {
        let mut base = json!({"chat_id": chat_id.unwrap_or_else(|| self.chat_id()), "bot_id": self.bot_id});
        if let (Some(b), Some(e)) = (base.as_object_mut(), event.as_object_mut()) {
            for (k, v) in std::mem::take(e) {
                b.insert(k, v);
            }
        }
        server::broadcast(base);
    }

    pub fn add(&self, role: &str, content: &str, chat_id: Option<i64>, meta: Value) -> i64 {
        let cid = chat_id.unwrap_or_else(|| self.chat_id());
        let meta = if meta.is_object() { meta } else { json!({}) };
        let mid = memory::add(role, content, cid, Some(self.bot_id), &meta);
        self.emit(json!({"type": "message", "message": {"id": mid, "ts": py::now(), "role": role, "content": content, "meta": meta, "bot_id": self.bot_id}}), Some(cid));
        mid
    }

    fn post_fn(self: &Arc<Self>) -> PostFn {
        let me = self.clone();
        Arc::new(move |role: String, content: String, chat_id: i64, meta: Value| -> BoxFuture<'static, i64> {
            let me = me.clone();
            Box::pin(async move { me.add(&role, &content, Some(chat_id), meta) })
        })
    }

    fn over_budget(&self) -> bool {
        memory::tokens_today() > config::get_i64("limits.daily_token_budget", 2_000_000)
    }

    /// 用户在私聊（或私聊里的讨论串）里发了一条消息
    pub async fn handle_user(
        self: &Arc<Self>,
        text: &str,
        via: &str,
        chat_id: Option<i64>,
        attachments: &[Value],
        applet: Option<Value>,
    ) -> anyhow::Result<()> {
        let cid = chat_id.unwrap_or_else(|| self.chat_id());
        let mut meta = json!({});
        if !via.is_empty() {
            meta["via"] = json!(via);
        }
        if !attachments.is_empty() {
            meta["attachments"] = json!(attachments);
        }
        if let Some(a) = applet {
            meta["applet"] = a;
        }
        if let Some((skill, rest)) = skills::parse(self.bot_id, text) {
            meta["skill"] = skill["name"].clone();
            meta["skill_prompt"] = json!(skills::expand(&skill, &rest));
        }
        self.add("user", text, Some(cid), meta);
        self.respond(cid, Turn::default()).await
    }

    /// 在某个对话里说一轮话
    pub fn respond(self: &Arc<Self>, chat_id: i64, turn: Turn) -> BoxFuture<'static, anyhow::Result<()>> {
        let me = self.clone();
        Box::pin(async move { me.respond_inner(chat_id, turn).await })
    }

    async fn respond_inner(self: Arc<Self>, chat_id: i64, turn: Turn) -> anyhow::Result<()> {
        let _l = self.lock.lock().await;
        let owed = turn.reply.clone();
        let who = memory::Who { bot_id: Some(self.bot_id), chat_id: Some(chat_id), task_id: None };
        let me = self.clone();
        let r = memory::WHO
            .scope(who, async move {
                let Some(chat) = bots::chat(chat_id) else { return Ok(()) };
                let mut ctx = Ctx { chat, turn, turn_tasks: vec![], turn_dms: 0, turn_waits: 0, files: Arc::new(Mutex::new(vec![])) };
                match me.think(&mut ctx).await {
                    Ok(shown) => {
                        if let Some(o) = &owed
                            && ctx.turn_tasks.is_empty()
                            && ctx.turn_dms == 0
                            && ctx.turn_waits == 0
                        {
                            let text = if shown.is_empty() { t!("（没有回复内容）") } else { shown };
                            dm::answer(me.bot_id, o, &text, chat_id, json!({})).await;
                        }
                        Ok(())
                    }
                    Err(e) => {
                        if let Some(o) = &owed {
                            let name = me.bot()["name"].as_str().unwrap_or("Bot").to_string();
                            dm::answer(
                                me.bot_id,
                                o,
                                &t!("（{name} 处理时出错：{error}）", name = name, error = providers::brief(&e)),
                                chat_id,
                                json!({}),
                            )
                            .await;
                        }
                        Err(e)
                    }
                }
            })
            .await;
        self.release_desk().await;
        r
    }

    async fn release_desk(&self) {
        crate::computer::desk_release(&format!("chat-{}", self.bot_id)).await;
    }

    async fn stream(
        &self,
        ctx: &Ctx,
        client: &providers::Client,
        model: &str,
        msgs: &mut Vec<Value>,
        sid: &str,
        use_tools: bool,
    ) -> anyhow::Result<(String, String, Vec<(String, String, String)>)> {
        let tools = self.tools(ctx);
        let cid = ctx.chat["id"].as_i64();
        let make = |msgs: &[Value]| {
            let mut body = json!({"model": model, "messages": msgs, "stream": true, "stream_options": {"include_usage": true}});
            if use_tools {
                body["tools"] = json!(tools);
            }
            for (k, v) in config::call_kwargs("router") {
                body[k] = v;
            }
            body
        };
        let t0 = Instant::now();
        let err_rec = |t0: Instant| memory::UsageRec { ms: Some(t0.elapsed().as_secs_f64() * 1000.0), ..Default::default() };
        let mut stream = match client.stream(make(msgs)).await {
            Ok(s) => s,
            Err(e) if e.status == Some(400) && media::strip_images(msgs) => {
                let (p, m) = config::role("router");
                media::remember_test(&p, &m, "no");
                match client.stream(make(msgs)).await {
                    Ok(s) => s,
                    Err(e) => {
                        memory::record_error("router", model, &providers::explain(&e, &e.origin), err_rec(t0));
                        return Err(e.into());
                    }
                }
            }
            Err(e) => {
                memory::record_error("router", model, &providers::explain(&e, &e.origin), err_rec(t0));
                return Err(e.into());
            }
        };
        let (mut content, mut reasoning) = (String::new(), String::new());
        let mut calls: BTreeMap<i64, (String, String, String)> = BTreeMap::new();
        let mut usage: Option<Value> = None;
        let mut first: Option<Instant> = None;
        let mut sent = 0usize;
        loop {
            let chunk = match stream.next().await {
                None => break,
                Some(Ok(c)) => c,
                Some(Err(e)) => {
                    memory::record_error("router", model, &providers::explain(&e, &e.origin), err_rec(t0));
                    return Err(e.into());
                }
            };
            if chunk.get("usage").is_some_and(|u| !u.is_null()) {
                usage = Some(chunk["usage"].clone());
            }
            let Some(d) = chunk["choices"].as_array().and_then(|c| c.first()).map(|c| c["delta"].clone()) else { continue };
            let rc = d["reasoning_content"].as_str().unwrap_or("");
            let dc = d["content"].as_str().unwrap_or("");
            let tcs = d["tool_calls"].as_array().cloned().unwrap_or_default();
            if first.is_none() && (!dc.is_empty() || !rc.is_empty() || !tcs.is_empty()) {
                first = Some(Instant::now());
            }
            if !rc.is_empty() {
                reasoning.push_str(rc);
                self.emit(json!({"type": "stream", "sid": sid, "thinking": rc}), cid);
            }
            if !dc.is_empty() {
                content.push_str(dc);
                let n = dsml::visible(&content);
                if n > sent {
                    self.emit(json!({"type": "stream", "sid": sid, "delta": &content[sent..n]}), cid);
                    sent = n;
                }
            }
            for tc in tcs {
                let c = calls.entry(tc["index"].as_i64().unwrap_or(0)).or_default();
                if let Some(id) = tc["id"].as_str().filter(|s| !s.is_empty()) {
                    c.0 = id.to_string();
                }
                if tc["function"].is_object() {
                    c.1.push_str(tc["function"]["name"].as_str().unwrap_or(""));
                    c.2.push_str(tc["function"]["arguments"].as_str().unwrap_or(""));
                }
            }
        }
        memory::record_usage(
            "router",
            model,
            usage.as_ref(),
            memory::UsageRec {
                ms: Some(t0.elapsed().as_secs_f64() * 1000.0),
                ttft: first.map(|f| (f - t0).as_secs_f64() * 1000.0),
                ..Default::default()
            },
        );
        let (content, leaked) = dsml::split(&content);
        if content.len() > sent {
            self.emit(json!({"type": "stream", "sid": sid, "delta": &content[sent..]}), cid);
        }
        let calls: Vec<(String, String, String)> = calls.into_values().collect();
        let out = if !calls.is_empty() {
            calls
        } else if use_tools {
            leaked.into_iter().map(|l| (l.id, l.name, l.args)).collect()
        } else {
            vec![]
        };
        Ok((content, reasoning, out))
    }

    pub fn tools(&self, ctx: &Ctx) -> Vec<Value> {
        let mut out = assets::router_tools();
        out.extend(registry::schemas(ROUTER, &router_env()));
        out.extend(ctools::desk_schemas());
        if ctx.chat["kind"] == "group" {
            out.push(assets::schemas()["handoff"].clone());
        }
        if bots::list_bots().len() > 1 {
            out.push(assets::schemas()["message_bot"].clone());
        }
        out.extend(botadmin::tools_for(&self.bot()));
        out
    }

    fn group_block(&self, chat: &Value) -> String {
        if chat["kind"] != "group" {
            return String::new();
        }
        let lead_id = chat["lead"].as_i64();
        let mut lines = vec![];
        for i in bots::ids_of(chat) {
            if let Some(m) = bots::get(i) {
                let ins = m["instructions"].as_str().unwrap_or("");
                let role = py::head(&(if ins.is_empty() { "职责没写明，看群里的对话" } else { ins }).replace('\n', " "), 120);
                lines.push(format!(
                    "- {}{}{}：{role}",
                    py::str_of(&m["name"]),
                    if i == self.bot_id { "（你）" } else { "" },
                    if Some(i) == lead_id { "（协调者）" } else { "" }
                ));
            }
        }
        let lead = if lead_id == Some(self.bot_id) {
            "你是这个群的协调者：用户没有 @ 任何人的消息由你接。能自己答就答；适合别人做的，用 handoff 分给对应的 Bot（可以分给几个），再简短告诉用户谁在做什么。"
        } else {
            "用户 @ 你、或者别的 Bot 把工作交给你时才轮到你。"
        };
        fmt(GROUP_PROMPT, &[("title", chat["title"].as_str().unwrap_or("")), ("members", &lines.join("\n")), ("lead", lead)])
    }

    fn turn_block(&self, turn: &Turn) -> String {
        let Some(woke) = &turn.woke else { return String::new() };
        if woke.kind == "botadmin" {
            let o = turn.reply.as_ref().and_then(|r| bots::get(r.bot));
            let mut s = BOTADMIN_TURN.to_string();
            if !woke.edited.is_empty() {
                s += &fmt(BOTADMIN_EDITED, &[("fields", &woke.edited.join("、"))]);
            }
            if let Some(o) = o {
                s += &fmt(BOTADMIN_OWED, &[("owed", o["name"].as_str().unwrap_or(""))]);
            }
            return s;
        }
        if woke.kind == "doc_comment" {
            return fmt(DOC_COMMENT_TURN, &[("prompt", &woke.text)]);
        }
        let name = bots::get(woke.from).map(|b| py::str_of(&b["name"])).unwrap_or_else(|| "已删除的 Bot".into());
        if woke.kind == "dm" {
            return fmt(DM_TURN, &[("name", &name)]);
        }
        if let Some(owed) = &turn.reply {
            let o = bots::get(owed.bot).map(|b| py::str_of(&b["name"])).unwrap_or_else(|| "等回信的 Bot".into());
            return fmt(REPLY_TURN, &[("name", &name)]) + &fmt(REPLY_OWED, &[("owed", &o), ("name", &name)]);
        }
        fmt(REPLY_TURN, &[("name", &name)]) + REPLY_SHOWN
    }

    fn others(&self) -> String {
        let rows: Vec<Value> = bots::list_bots().into_iter().filter(|b| b["id"].as_i64() != Some(self.bot_id)).collect();
        let mut lines: Vec<String> = rows
            .iter()
            .take(30)
            .map(|b| {
                let ins = b["instructions"].as_str().unwrap_or("");
                format!(
                    "- {}：{}",
                    py::str_of(&b["name"]),
                    py::head(&(if ins.is_empty() { "职责没写明" } else { ins }).replace('\n', " "), 80)
                )
            })
            .collect();
        if rows.len() > 30 {
            lines.push(format!("- ……还有 {} 个", rows.len() - 30));
        }
        if lines.is_empty() { "暂无（只有你一个 Bot）".into() } else { lines.join("\n") }
    }

    async fn think(self: &Arc<Self>, ctx: &mut Ctx) -> anyhow::Result<String> {
        let (client, model) = providers::client_for("router")?;
        let b = self.bot();
        let cid = ctx.chat["id"].as_i64().unwrap_or(0);
        let last = crate::db::scalar(
            "select content from messages where chat_id=? and role='user' order by id desc limit 1",
            rusqlite::params![cid],
        );
        let probe = match &ctx.turn.woke {
            Some(w) => w.text.clone(),
            None => last.as_str().unwrap_or("").to_string(),
        };
        let related = memories::related(self.bot_id, &probe).await;
        if let Some(started) = &ctx.turn.started {
            started();
        }
        let see = media::can_see("", "");
        let ins = b["instructions"].as_str().unwrap_or("");
        let primary = bots::primary_name();
        let bot_rule = if b["is_primary"] == true {
            PRIMARY_RULE.to_string()
        } else if !primary.is_empty() {
            fmt(OTHER_RULE, &[("primary", &primary)])
        } else {
            String::new()
        };
        let system = fmt(
            ROUTER_PROMPT,
            &[
                ("name", b["name"].as_str().unwrap_or("")),
                ("instructions", if ins.is_empty() { "你的具体职责以用户在对话里交代的为准。" } else { ins }),
                ("date", &py::strftime("%Y-%m-%d")),
                ("skills", &skills::summary(self.bot_id)),
                ("routines", &self.routine_list()),
                ("capabilities", &self.capabilities()),
                ("memories", &memories::prompt_block(self.bot_id)),
                ("group", &self.group_block(&ctx.chat)),
                ("others", &self.others()),
                ("bot_rule", &bot_rule),
                ("attach_rule", if see { ATTACH_SEE } else { ATTACH_BLIND }),
            ],
        ) + i18n::model_hint();
        let recent = memories::turn_block(self.bot_id, &related);
        let tool_names: Vec<String> = self.tools(ctx).iter().map(|t| py::str_of(&t["function"]["name"])).collect();
        let state = fmt(
            STATE_PROMPT,
            &[
                ("tasks", &self.task_list()),
                ("approvals", &approvals::summary(Some(self.bot_id))),
                ("tools", &tool_names.join("、")),
                ("memories", &if recent.is_empty() { String::new() } else { format!("\n\n{recent}") }),
                ("turn", &self.turn_block(&ctx.turn)),
            ],
        );
        let viewer = if ctx.chat["kind"] == "group" { Some(self.bot_id) } else { None };
        let mut msgs = vec![json!({"role": "system", "content": system})];
        msgs.extend(with_state(memory::context(cid, viewer, Some(self.bot_id), if see { media::MODEL_IMAGES } else { 0 }), &state));
        let sid = format!("s{}", py::now_ns());
        let started = py::now();
        let (mut shown, mut thinking, mut reasoning) = (String::new(), String::new(), String::new());
        let mut shots: Vec<(Vec<u8>, Option<String>, bool)> = vec![];
        self.emit(json!({"type": "stream_start", "sid": sid}), Some(cid));
        let body = async {
            for _ in 0..10 {
                let (content, r, calls) = self.stream(ctx, &client, &model, &mut msgs, &sid, true).await?;
                reasoning = r.clone();
                shown.push_str(&content);
                thinking.push_str(&r);
                if calls.is_empty() {
                    break;
                }
                msgs.push(json!({"role": "assistant", "content": content, "reasoning_content": r,
                    "tool_calls": calls.iter().map(|(id, name, args)| json!({"id": id, "type": "function",
                        "function": {"name": name, "arguments": if args.is_empty() { "{}" } else { args }}})).collect::<Vec<_>>()}));
                for (id, name, args) in &calls {
                    let a: Value = serde_json::from_str(if args.is_empty() { "{}" } else { args }).unwrap_or(json!({}));
                    let a = if a.is_object() { a } else { json!({}) };
                    let out = self.call_tool(ctx, name, a).await;
                    let text = match out {
                        Out::Text(s) => s,
                        Out::Shot(s, png) => {
                            if let Some(p) = png {
                                let whole = name == "look_at_screen";
                                shots.push((p, if whole { Some("电脑的整个屏幕".to_string()) } else { None }, !whole));
                            }
                            s
                        }
                        Out::Pic(s, png, cap) => {
                            shots.push((png, Some(cap), true));
                            s
                        }
                    };
                    msgs.push(json!({"role": "tool", "tool_call_id": id, "content": text}));
                }
                if let Some((png, cap, coords)) = shots.last() {
                    msgs.push(ctools::image_message(png, cap.as_deref(), *coords));
                    shots.clear();
                }
                if !shown.is_empty() && !shown.ends_with('\n') {
                    shown.push_str("\n\n");
                    self.emit(json!({"type": "stream", "sid": sid, "delta": "\n\n"}), Some(cid));
                }
            }
            if shown.trim().is_empty() {
                msgs.push(json!({"role": "user", "content": "（系统：这一轮能调用工具的次数用完了。现在不要再调用任何工具，直接用文字告诉用户做到哪一步了；没做完的，告诉用户下一条消息里你会派任务接着做。）"}));
                let (content, r, _) = self.stream(ctx, &client, &model, &mut msgs, &sid, false).await?;
                shown = content;
                thinking.push_str(&r);
                reasoning = r;
            }
            anyhow::Ok(())
        }
        .await;
        self.emit(json!({"type": "stream_end", "sid": sid}), Some(cid));
        body?;
        let shown = shown.trim().to_string();
        let mut meta = json!({"reasoning": reasoning, "thinking": thinking, "thought_s": py::round_n(py::now() - started, 1), "sid": sid, "tasks": ctx.turn_tasks});
        let files = ctx.files.lock().clone();
        if !files.is_empty() {
            meta["attachments"] = json!(files);
        }
        self.add("assistant", &shown, Some(cid), meta);
        Ok(shown)
    }

    fn member(&self, ctx: &Ctx, name: &str) -> Option<Value> {
        let key = name.trim().trim_start_matches('@').to_lowercase();
        bots::ids_of(&ctx.chat).into_iter().filter_map(bots::get).find(|m| m["name"].as_str().unwrap_or("").to_lowercase() == key)
    }

    async fn call_tool(self: &Arc<Self>, ctx: &mut Ctx, name: &str, args: Value) -> Out {
        let s = |k: &str| -> String {
            args[k].as_str().map(String::from).unwrap_or_else(|| if args[k].is_null() { String::new() } else { py::str_of(&args[k]) })
        };
        let chat_id = ctx.chat["id"].as_i64().unwrap_or(0);
        match name {
            "dispatch" => {
                if self.over_budget() {
                    return "今日 token 预算已用完，没有派发。请告诉用户。".into();
                }
                let mut handoff = None;
                let mut to_name = String::new();
                if py::truthy(&args["handoff_to"]) && ctx.chat["kind"] == "group" {
                    match self.member(ctx, &s("handoff_to")) {
                        Some(to) if to["id"].as_i64() != Some(self.bot_id) => {
                            to_name = py::str_of(&to["name"]);
                            handoff = Some(Handoff { to: to["id"].as_i64().unwrap_or(0), note: s("handoff_note") });
                        }
                        _ => return format!("没派发：群里没有叫 {} 的其他成员", s("handoff_to")).into(),
                    }
                }
                let mut mac = None;
                if !s("mac").trim().is_empty() {
                    match pick_mac(&s("mac")) {
                        (Some(m), _) => mac = Some(m),
                        (None, why) => return why.into(),
                    }
                }
                if let Some(h) = &handoff {
                    groups::expect(chat_id, h.to);
                }
                let mode = if s("mode").is_empty() { "single".to_string() } else { s("mode") };
                let task = self
                    .dispatch(Dispatch {
                        goal: s("goal"),
                        mode,
                        chat_id: Some(chat_id),
                        handoff: handoff.clone(),
                        reply: ctx.turn.reply.clone(),
                        depth: ctx.turn.depth(),
                        mac: mac.clone(),
                        ..Default::default()
                    })
                    .await;
                ctx.turn_tasks.push(task.id);
                if handoff.is_some() {
                    return format!("已派发任务 #{}，做完后结果会自动交给 {to_name}", task.id).into();
                }
                if let Some(r) = task.st.lock().reply.clone() {
                    return format!("已派发任务 #{}，做完后结果会自动发给 {}", task.id, dm::name_of(Some(r.bot))).into();
                }
                if let Some(m) = mac {
                    return format!("已派发任务 #{}，在用户的 Mac「{}」上做（开始前会先请用户确认）", task.id, py::str_of(&m["name"]))
                        .into();
                }
                format!("已派发任务 #{}", task.id).into()
            }
            "message_bot" => {
                let to = bots::by_name(&s("to"));
                if to.as_ref().is_some_and(|t| t["id"].as_i64() == Some(self.bot_id)) {
                    return "没发：这是你自己".into();
                }
                let Some(to) = to else {
                    let others: Vec<String> = bots::list_bots()
                        .iter()
                        .filter(|b| b["id"].as_i64() != Some(self.bot_id))
                        .map(|b| py::str_of(&b["name"]))
                        .collect();
                    let fix = if self.bot()["is_primary"] == true {
                        "要用它就先用 create_bot 新建（用户在卡片上确认后建好），不要让用户自己去设置里建".to_string()
                    } else {
                        format!("需要这样一个 Bot 的话，私信主 Bot「{}」请它新建", bots::primary_name())
                    };
                    let o = others.join("、");
                    return format!(
                        "没发：没有叫 {} 的其他 Bot（没建成或已删除，记忆里说有也不算）。现有：{}。{fix}",
                        py::str_of(&args["to"]),
                        if o.is_empty() { "无" } else { &o }
                    )
                    .into();
                };
                if ctx.chat["kind"] == "group" && bots::ids_of(&ctx.chat).contains(&to["id"].as_i64().unwrap_or(0)) {
                    return format!("没发：{} 就在这个群里，用 handoff 交给它", py::str_of(&to["name"])).into();
                }
                let out = dm::send(self.bot_id, &to, &s("text"), chat_id, ctx.turn.reply.clone(), ctx.turn.depth()).await;
                if out.starts_with("已私信") {
                    ctx.turn_dms += 1;
                }
                out.into()
            }
            "handoff" => {
                if ctx.chat["kind"] != "group" {
                    return "handoff 只能在群聊里用".into();
                }
                match self.member(ctx, &s("to")) {
                    Some(to) if to["id"].as_i64() != Some(self.bot_id) => {
                        groups::handoff(chat_id, self.bot_id, to["id"].as_i64().unwrap_or(0), &s("request"));
                        format!("已交给 {}，你这轮说完它就接着做", py::str_of(&to["name"])).into()
                    }
                    _ => format!("群里没有叫 {} 的其他成员", py::str_of(&args["to"])).into(),
                }
            }
            "save_skill" => match skills::save(self.bot_id, &s("name"), &s("body"), &s("description"), "chat") {
                Ok(sk) => {
                    self.emit(json!({"type": "skills_changed"}), None);
                    format!("已保存技能 /{}", py::str_of(&sk["name"])).into()
                }
                Err(e) => format!("没保存：{e}").into(),
            },
            "remember" => self.remember(&args).into(),
            "forget" => {
                let mid = &args["id"];
                match mid.as_i64().filter(|m| mid.is_i64() && memories::visible(*m, self.bot_id)) {
                    Some(m) => {
                        memories::delete(m);
                        self.emit(json!({"type": "memories_changed"}), None);
                        format!("已删除记忆 #{m}").into()
                    }
                    None => format!("没有 #{} 这条记忆", py::str_of(mid)).into(),
                }
            }
            "list_tasks" => self.task_list().into(),
            "tell_task" => taskstore::tell(self.bot_id, py::to_int(&args["task_id"]).unwrap_or(0), &s("text")).await.into(),
            "cancel_task" => {
                let tid = py::to_int(&args["task_id"]).unwrap_or(0);
                let Some(woke) = &ctx.turn.woke else {
                    return self.cancel(tid, &t!("用户要求取消")).into();
                };
                if let Some(t) = self.tasks().into_iter().find(|t| t.id == tid) {
                    let mut st = t.st.lock();
                    if st.reply.is_some() && ctx.turn.reply.is_some() && dm::same(&st.reply, &ctx.turn.reply) {
                        st.reply = None;
                    }
                }
                let reason = t!(
                    "{name} 收到 {sender} 的新消息后自己取消的",
                    name = self.bot()["name"].as_str().unwrap_or(""),
                    sender = dm::name_of(Some(woke.from))
                );
                self.cancel(tid, &reason).into()
            }
            "resolve_approval" => approvals::resolve(
                py::to_int(&args["id"]).unwrap_or(0),
                py::truthy(&args["approve"]),
                &s("note"),
                py::truthy(&args["always"]),
                &Value::Null,
                args["scope"].as_str(),
                None,
            )
            .into(),
            "recall" => memory::recall(py::to_int(&args["id"]).unwrap_or(0), chat_id).into(),
            n if botadmin::TOOLS.contains(&n) => {
                let out =
                    botadmin::call(self, n, &self.bot(), chat_id, self.chat_id(), &args, ctx.turn.reply.clone(), ctx.turn.depth()).await;
                if out.starts_with("已弹出") {
                    ctx.turn_waits += 1;
                }
                out.into()
            }
            "look_at_screen" => crate::computer::look_for_router().await,
            n if DIRECT_PC.contains(&n) || ctools::is_desktop(n) => self.direct(n, &args).await,
            n => match registry::available(n, ROUTER, &router_env()) {
                Some(tool) => {
                    let call = Call {
                        scope: ROUTER,
                        bot: self.bot(),
                        chat_id,
                        private_chat_id: self.chat_id(),
                        args,
                        task: None,
                        post: Some(self.post_fn()),
                        files: ctx.files.clone(),
                        apps: Arc::new(Mutex::new(vec![])),
                        env: router_env(),
                    };
                    use futures::FutureExt;
                    match std::panic::AssertUnwindSafe((tool.run)(call)).catch_unwind().await {
                        Ok(o) => o,
                        Err(_) => "工具出错：内部错误".into(),
                    }
                }
                None => "未知工具".into(),
            },
        }
    }

    fn remember(&self, args: &Value) -> String {
        let owner = if py::truthy(&args["shared"]) { None } else { Some(self.bot_id) };
        let rid = &args["replace_id"];
        let text = args["text"].as_str().unwrap_or("");
        let until = match memories::parse_until(args.get("until")) {
            Ok(u) => u,
            Err(e) => return format!("没记住：{e}"),
        };
        let r = match rid.as_i64().filter(|r| rid.is_i64() && memories::visible(*r, self.bot_id)) {
            Some(r) => {
                let keep = memories::get(r).map(|m| m["bot_id"].as_i64()).unwrap_or(None);
                memories::update(r, Some(text), Some(if args.get("shared").is_some() { owner } else { keep })).map(|m| match until {
                    Some(u) => {
                        memories::set_until(r, u);
                        memories::get(r).unwrap_or(m)
                    }
                    None => m,
                })
            }
            None => memories::add_until(owner, text, "chat", until.flatten()),
        };
        match r {
            Ok(m) => {
                self.emit(json!({"type": "memories_changed"}), None);
                let until = m["until"].as_f64().map(|u| format!("，到 {} 为止", memories::until_day(u))).unwrap_or_default();
                format!(
                    "已记住 #{}（{}{until}）：{}",
                    py::str_of(&m["id"]),
                    if m["shared"] == true { "所有 Bot 共享" } else { "只有你知道" },
                    py::str_of(&m["text"])
                )
            }
            Err(e) => format!("没记住：{e}"),
        }
    }

    async fn direct(&self, name: &str, args: &Value) -> Out {
        crate::computer::direct(self.bot_id, &py::str_of(&self.bot()["name"]), self.chat_id(), name, args).await
    }

    fn capabilities(&self) -> String {
        let mut lines: Vec<String> = registry::all()
            .iter()
            .filter(|t| !t.describe.is_empty() && t.group.is_empty())
            .map(|t| format!("- {}：{}", t.name, t.describe))
            .collect();
        if config::get_bool("computer.enabled", true) {
            lines.push(format!(
                "- 电脑：{}遇到登录、验证码、付款这类必须用户本人做的步骤，Worker 会在任务中途暂停，在对话里请用户「接手电脑」，用户交还后同一个任务接着做完。所以这类事情派一个任务就够，不要拆成两个任务，也不要让用户做完后再来找你。提交、付款、发送、删除这类有后果的操作，Worker 会先用 ask_approval 弹确认卡片，用户同意才做；用户要求“先问我再做”时，把这个要求写进 goal 就行。你自己也能用 look_at_screen 直接看屏幕：用户问屏幕上是什么、任务做到哪了，先看再答，不要说你看不见。",
                ctools::capability()
            ));
        }
        let macs = if security::demo() { vec![] } else { crate::computer::link_rows() };
        if !macs.is_empty() {
            let names: Vec<String> = macs
                .iter()
                .map(|r| format!("「{}」（{}）", py::str_of(&r["name"]), if r["connected"] == true { "在线" } else { "没连上" }))
                .collect();
            lines.push(format!("- 用户的 Mac：{}。有的事只有用户自己的 Mac 才做得了（要用那台 Mac 上登录着的 App 或网站、Mac 上的文件、只装在 Mac 上的软件）：dispatch 时 mac 写那台的名字，任务就在那台 Mac 上做，开始前会请用户确认。别的事照常在你自己的电脑上做，不要写 mac。", names.join("、")));
        }
        let groups: Vec<(String, Vec<String>)> = registry::by_group()
            .into_iter()
            .map(|(srv, ts)| {
                (
                    srv,
                    ts.iter()
                        .filter(|t| t.where_.contains(&registry::WORKER))
                        .map(|t| t.name.split_once("__").map(|x| x.1.to_string()).unwrap_or_default())
                        .collect(),
                )
            })
            .collect();
        let label = |n: &str| -> String {
            let t = catalog::title(n);
            if t == format!("@{n}") { format!("@{n}") } else { format!("@{n}（{t}）") }
        };
        for (srv, ts) in &groups {
            if !ts.is_empty() {
                lines.push(format!("- 应用 {}：{}", label(srv), ts.join(", ")));
            }
        }
        let off: Vec<String> = mcp_hub::all_apps().into_iter().filter(|n| !groups.iter().any(|(g, _)| g == n)).collect();
        if !off.is_empty() {
            lines.push(format!(
                "- 未连接的应用（现在用不了，请用户到「应用」页面重新连接）：{}",
                off.iter().map(|n| label(n)).collect::<Vec<_>>().join(", ")
            ));
        }
        lines.join("\n")
    }

    pub async fn dispatch(self: &Arc<Self>, d: Dispatch) -> Arc<Task> {
        let cid = d.chat_id.unwrap_or_else(|| self.chat_id());
        let mut meta = json!({"kind": "dispatch", "mode": d.mode, "status": "running"});
        if let Some(h) = &d.handoff {
            meta["handoff_to"] = json!(h.to);
        }
        if let Some(r) = &d.reply {
            meta["reply_to"] = json!(r.bot);
        }
        if let Some(m) = &d.mac {
            meta["mac"] = m["name"].clone();
        }
        let label = if d.label.is_empty() { String::new() } else { format!("{} ", d.label) };
        let tid = self.add("task", &format!("[任务已派发] {label}{}", d.goal), Some(cid), meta);
        let store = d.mode.is_empty() || d.mode == "single";
        let task = Task::new(NewTask {
            id: tid,
            goal: d.goal,
            mode: d.mode,
            bot_id: self.bot_id,
            chat_id: cid,
            approval_chat_id: self.chat_id(),
            background: d.background,
            handoff: d.handoff,
            reply: d.reply,
            depth: d.depth,
            quiet_ok: d.quiet_ok,
            notes: d.notes,
            mac: d.mac.as_ref().and_then(|m| m["id"].as_i64()).unwrap_or(0),
            mac_name: d.mac.as_ref().map(|m| py::str_of(&m["name"])).unwrap_or_default(),
            round: 1,
            store,
            ..Default::default()
        });
        if let Some(f) = d.on_done {
            *task.on_done.lock() = Some(f);
        }
        taskstore::create(&task, d.mac.as_ref());
        self.start(task.clone());
        task
    }

    /// 让一个建好的任务跑起来（新派的、接着做的、重启后恢复的）
    pub fn start(self: &Arc<Self>, task: Arc<Task>) {
        TASKS.lock().insert(task.id, task.clone());
        let me = self.clone();
        let t2 = task.clone();
        let fut: BoxFuture<'static, ()> = Box::pin(async move { me.run(t2).await });
        tokio::spawn(fut);
        self.emit(json!({"type": "task", "task": task.public()}), Some(task.chat_id));
    }

    fn routine_list(&self) -> String {
        let rs: Vec<String> = routines::list_for(Some(self.bot_id))
            .iter()
            .map(|r| {
                format!(
                    "- #{} {}「{}」（{}{}）",
                    py::str_of(&r["id"]),
                    routines::noun(r),
                    py::str_of(&r["name"]),
                    routines::describe(&r["trigger"], Some("zh")),
                    if py::truthy(&r["enabled"]) { "" } else { "，已暂停" }
                )
            })
            .collect();
        if rs.is_empty() { "暂无".into() } else { rs.join("\n") }
    }

    fn task_list(&self) -> String {
        let live = self.tasks();
        let mut rs: Vec<String> = live
            .iter()
            .map(|t| {
                let waiting = t.waiting_kind();
                let s = t.st.lock();
                let state = match waiting.as_deref() {
                    Some("question") => "等用户回答".to_string(),
                    Some(_) => "等待确认".to_string(),
                    None => s.status.clone(),
                };
                let more = if s.status != "running" && t.store { taskstore::CAN_TELL } else { "" };
                format!("#{} [{state}] {} 步数={} {}{more}", t.id, t.mode, s.steps, py::head(&t.goal, 50))
            })
            .collect();
        let ids: Vec<i64> = live.iter().map(|t| t.id).collect();
        rs.extend(taskstore::recent_lines(self.bot_id, &ids));
        if rs.is_empty() { "暂无任务".into() } else { rs.join("\n") }
    }

    async fn use_mac(&self, task: &Arc<Task>) -> String {
        crate::computer::use_mac(task).await
    }

    async fn run(self: Arc<Self>, task: Arc<Task>) {
        let token = task.cancel.clone();
        let t2 = task.clone();
        let me = self.clone();
        let work = async move {
            if t2.mac != 0 {
                let why = me.use_mac(&t2).await;
                if !why.is_empty() {
                    return Ok::<Option<String>, anyhow::Error>(Some(why));
                }
            }
            let goal = format!("{}{}", t2.goal, t2.notes);
            let r = if t2.mode == "heavy" {
                worker::run_heavy(t2.clone()).await
            } else {
                worker::run_worker(t2.clone(), goal, String::new()).await
            };
            r.map(|s| {
                t2.st.lock().result = s;
                None
            })
        };
        let work = crate::computer::with_task_pc(work);
        let outcome = tokio::select! {
            r = work => Some(r),
            _ = token.cancelled() => None,
        };
        match outcome {
            None => {
                // 取消了：cancel() 已经标好状态、记好消息
                let asked = task.status() == "cancelled";
                task.st.lock().status = "cancelled".into();
                let (h, reply) = {
                    let s = task.st.lock();
                    (s.handoff.clone(), s.reply.clone())
                };
                if let Some(h) = h {
                    groups::unexpect(task.chat_id, h.to);
                }
                if reply.is_some() && asked {
                    tokio::spawn(dm::after_task(task.clone()));
                }
                taskstore::finished(&task, "cancelled");
                self.done(&task).await;
                taskstore::after_round(&task);
                return;
            }
            Some(Ok(Some(why))) => {
                let mut s = task.st.lock();
                s.status = "failed".into();
                s.result = why;
            }
            Some(Ok(None)) => {
                task.st.lock().status = "done".into();
                if !task.background {
                    memories::log_work(self.bot_id, task.id, &task.goal);
                }
            }
            Some(Err(e)) if e.downcast_ref::<taskstore::Parked>().is_some() => {
                // 停服务时停在了步与步之间：状态留着 running，启动后接着做
                task.parked.store(true, std::sync::atomic::Ordering::SeqCst);
                return;
            }
            Some(Err(e)) => {
                let mut s = task.st.lock();
                s.status = "failed".into();
                s.result = t!("任务失败：{error}", error = providers::brief(&e));
            }
        }
        let note = taskstore::doubt_note(&task);
        if !note.is_empty() {
            task.st.lock().result.push_str(&note);
        }
        let (status, result, started, steps) = {
            let s = task.st.lock();
            (s.status.clone(), s.result.clone(), s.started, s.steps)
        };
        tasklog::close(&task, &status, if status == "failed" { &result } else { "" });
        let elapsed = py::round0(py::now() - started);
        if status == "done" && task.quiet_ok && routines::is_quiet(&result) {
            task.st.lock().quiet = true;
            memory::update_meta(task.id, &json!({"status": "done", "quiet": true, "elapsed": elapsed, "steps": steps}));
            self.emit(json!({"type": "task", "task": task.public()}), Some(task.chat_id));
            taskstore::finished(&task, "done");
            self.done(&task).await;
            taskstore::after_round(&task);
            return;
        }
        memory::update_meta(task.id, &json!({"status": status, "elapsed": elapsed, "steps": steps}));
        let mut meta = json!({"kind": "result", "task_id": task.id, "status": status, "elapsed": py::round0(py::now() - started), "round": task.round});
        let files = task.files.lock().clone();
        if !files.is_empty() {
            meta["attachments"] = json!(files);
        }
        let apps = task.apps.lock().clone();
        if !apps.is_empty() {
            meta["apps"] = json!(apps);
        }
        // 在讨论串里回复过这个任务的，结果发进讨论串
        let result_chat = task.st.lock().result_chat.unwrap_or(task.chat_id);
        self.add("task", &format!("[任务 #{} 结果]\n{result}", task.id), Some(result_chat), meta);
        self.emit(json!({"type": "task", "task": task.public()}), Some(task.chat_id));
        registry::fire("task_done", json!({"task": task.public(), "bot": self.bot(), "result": result}));
        let (h, reply) = {
            let s = task.st.lock();
            (s.handoff.is_some(), s.reply.is_some())
        };
        if h {
            tokio::spawn(groups::after_task(task.clone()));
        }
        if reply {
            tokio::spawn(dm::after_task(task.clone()));
        }
        taskstore::finished(&task, &status);
        self.done(&task).await;
        taskstore::after_round(&task);
    }

    async fn done(&self, task: &Arc<Task>) {
        approvals::end_task(task.id);
        let f = task.on_done.lock().take();
        if let Some(f) = f {
            f(task.clone()).await;
        }
    }

    pub fn cancel(&self, tid: i64, reason: &str) -> String {
        let Some(t) = self.tasks().into_iter().find(|t| t.id == tid).filter(|t| t.status() == "running") else {
            return t!("任务 #{id} 不在运行中", id = tid);
        };
        approvals::cancel_for_task(tid);
        let result = t!("已取消：{reason}", reason = reason);
        let (started, steps) = {
            let mut s = t.st.lock();
            s.status = "cancelled".into();
            s.result = result.clone();
            (s.started, s.steps)
        };
        t.cancel.cancel();
        memory::update_meta(tid, &json!({"status": "cancelled", "elapsed": py::round0(py::now() - started), "steps": steps}));
        tasklog::close(&t, "cancelled", &result);
        self.add(
            "task",
            &t!("[任务 #{id} 已取消：{reason}]", id = tid, reason = reason),
            Some(t.chat_id),
            json!({"kind": "cancel", "task_id": tid}),
        );
        self.emit(json!({"type": "task", "task": t.public()}), Some(t.chat_id));
        t!("已取消 #{id}", id = tid)
    }
}

#[derive(Default)]
pub struct Dispatch {
    pub goal: String,
    pub mode: String,
    pub background: bool,
    pub on_done: Option<crate::task::OnDone>,
    pub label: String,
    pub chat_id: Option<i64>,
    pub handoff: Option<Handoff>,
    pub reply: Option<crate::task::Reply>,
    pub depth: i64,
    pub quiet_ok: bool,
    pub notes: String,
    pub mac: Option<Value>,
}

/// dispatch 的 mac：按名字找用户登记过的 Mac
fn pick_mac(name: &str) -> (Option<Value>, String) {
    if security::demo() {
        return (None, "没派发：演示服务器上不能把任务派到用户的 Mac".into());
    }
    crate::computer::pick_mac(name)
}
