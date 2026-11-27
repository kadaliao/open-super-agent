//! 聊天软件接入：在 Telegram、飞书、钉钉、企业微信、Discord 里和 Bot 对话。对应 osa/channels/__init__.py（规则见那里的说明）。
//!
//! 一个连接 = 聊天软件里的一个机器人账号，绑定这边的一个对话。只和配对过的那个人说话。
//! 出去的消息按顺序排队（outbox）；确认请求变成带按钮的卡片，在哪边处理的都会把卡片改成结果。

pub mod dingtalk;
pub mod discord;
pub mod feishu;
pub mod fmt;
pub mod telegram;
pub mod wecom;

use crate::db::{self, RowExt};
use crate::i18n::Later;
use crate::web::{ApiError, Body, Res, int, ok};
use crate::{approvals, bots, i18n, media, py, router, server, vault};
use async_trait::async_trait;
use axum::extract::Path;
use axum::routing::{get, patch, post, put};
use futures::future::BoxFuture;
use parking_lot::Mutex;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, LazyLock};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

pub const PAIR_TTL: f64 = 30.0 * 60.0;
pub const PAIR_MAX_FAILS: i64 = 10;
pub const HELP: &str = "直接发消息就是在和「{name}」说话，和网页上是同一个对话。
- 发文件或图片：存到电脑的 uploads 目录，Bot 能接着处理
- /new 开始新话题
- /stop 停下这个对话里正在做的任务
- 需要你确认的操作会发来一张卡片，点上面的按钮，或者回复“允许 #编号”“拒绝 #编号”";

pub fn migrate() {
    db::batch(
        "create table if not exists channels(
          id integer primary key, kind text, name text default '', config text default '{}', chat_id integer,
          peer text default '', peer_name text default '', pair_code text default '', pair_expires real default 0,
          pair_fails integer default 0, enabled integer default 1, created real)",
    );
}

const COLS: &str = "id, kind, name, config, chat_id, peer, peer_name, pair_code, pair_expires, pair_fails, enabled, created";

fn row_of(r: &[Value]) -> Value {
    let keys: Vec<&str> = COLS.split(", ").collect();
    let mut o = serde_json::Map::new();
    for (k, v) in keys.iter().zip(r) {
        o.insert(k.to_string(), v.clone());
    }
    let cfg = r.to_vec().j(3);
    o.insert("config".into(), cfg);
    Value::Object(o)
}

pub fn rows() -> Vec<Value> {
    db::rows(&format!("select {COLS} from channels order by id"), []).iter().map(|r| row_of(r)).collect()
}

pub fn row(cid: i64) -> Option<Value> {
    db::row(&format!("select {COLS} from channels where id=?"), params![cid]).map(|r| row_of(&r))
}

pub fn secret_name(cid: i64) -> String {
    format!("channel_{cid}")
}

pub fn public_url() -> String {
    crate::memory::setting_raw("public_url").unwrap_or_default()
}

pub fn set_public_url(url: &str) -> Result<(), String> {
    static R: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^https?://[^\s/]+(/[^\s]*)?$").unwrap());
    let url = url.trim().trim_end_matches('/');
    if !url.is_empty() && !R.is_match(url) {
        return Err(t!("地址要以 http:// 或 https:// 开头"));
    }
    crate::memory::set_setting_raw("public_url", url);
    Ok(())
}

// ---------- 平台 ----------
/// 卡片（各平台自己画）：{id, kind, title, body, buttons: [[文字, 动作, 样式]], link: [文字, 地址] | null, done}
pub type Card = Value;

#[async_trait]
pub trait Platform: Send + Sync {
    fn max_len(&self) -> usize;
    async fn run(&self, ch: Arc<Chan>) -> anyhow::Result<()>;
    async fn send_text(&self, ch: &Arc<Chan>, peer: &str, md: &str) -> anyhow::Result<()>;
    async fn send_card(&self, ch: &Arc<Chan>, peer: &str, card: &Card) -> anyhow::Result<String>;
    async fn edit_card(&self, ch: &Arc<Chan>, peer: &str, msg_id: &str, card: &Card) -> anyhow::Result<()>;
    async fn typing(&self, _ch: &Arc<Chan>, _peer: &str) -> anyhow::Result<()> {
        Ok(())
    }
    /// 取附件：(内容, 下载时才知道的文件名，没有是空)
    async fn download(&self, _ch: &Arc<Chan>, _fref: &Value) -> anyhow::Result<(Vec<u8>, String)> {
        anyhow::bail!(t!("这个聊天软件还不支持收文件"))
    }
    /// 自己处理“正在输入”（飞书用表情）：处理了返回 true
    fn start_typing(&self, _ch: &Arc<Chan>) -> bool {
        false
    }
    fn stop_typing(&self, _ch: &Arc<Chan>) -> bool {
        false
    }
}

pub fn title_of(kind: &str) -> &'static str {
    match kind {
        "telegram" => "Telegram",
        "feishu" => "飞书",
        "dingtalk" => "钉钉",
        "wecom" => "企业微信",
        "discord" => "Discord",
        _ => "",
    }
}

fn known(kind: &str) -> bool {
    !title_of(kind).is_empty()
}

/// 验证凭据，返回 {"name", "config", "secret"}；不对就报写给用户看的话
async fn check(kind: &str, fields: &Value) -> Result<Value, String> {
    match kind {
        "telegram" => telegram::check(fields).await,
        "feishu" => feishu::check(fields).await,
        "dingtalk" => dingtalk::check(fields).await,
        "wecom" => wecom::check(fields).await,
        "discord" => discord::check(fields).await,
        _ => Err(t!("不支持的聊天软件：{kind}", kind = kind)),
    }
}

fn same(kind: &str, r: &Value, info: &Value) -> bool {
    let key = match kind {
        "telegram" | "wecom" | "discord" => "bot_id",
        "feishu" => "app_id",
        "dingtalk" => "app_key",
        _ => return false,
    };
    r["config"].get(key).is_some_and(|v| *v == info["config"][key])
}

fn pair_link(r: &Value) -> String {
    match r["kind"].as_str().unwrap_or("") {
        "telegram" => format!("https://t.me/{}?start={}", py::str_of(&r["config"]["username"]), py::str_of(&r["pair_code"])),
        "feishu" => feishu::pair_link(r),
        "discord" => format!("https://discord.com/users/{}", py::str_of(&r["config"]["bot_id"])),
        _ => String::new(),
    }
}

fn setup_link(r: &Value) -> String {
    match r["kind"].as_str().unwrap_or("") {
        "discord" => {
            format!("https://discord.com/oauth2/authorize?client_id={}&scope=bot&permissions=0", py::str_of(&r["config"]["app_id"]))
        }
        _ => String::new(),
    }
}

fn make(kind: &str, r: &Value) -> Arc<dyn Platform> {
    match kind {
        "telegram" => Arc::new(telegram::Telegram::new()),
        "feishu" => Arc::new(feishu::Feishu::new(r)),
        "dingtalk" => Arc::new(dingtalk::DingTalk::new(r)),
        "wecom" => Arc::new(wecom::WeCom::new()),
        _ => Arc::new(discord::Discord::new()),
    }
}

/// 聊天软件用的 HTTP 客户端：HTTPS_PROXY 有的话走它（NO_PROXY 照样认）
pub fn http(timeout: u64) -> reqwest::Client {
    let mut b = reqwest::Client::builder().timeout(std::time::Duration::from_secs(timeout));
    for k in ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"] {
        if let Some(p) = std::env::var(k).ok().filter(|v| !v.is_empty()).and_then(|v| reqwest::Proxy::https(&v).ok()) {
            b = b.proxy(p.no_proxy(reqwest::NoProxy::from_env()));
            break;
        }
    }
    b.build().unwrap_or_default()
}

/// httpx 异常的类名（报给用户看的“连不上 Telegram（ConnectError）”）
pub fn err_name(e: &reqwest::Error) -> &'static str {
    if e.is_timeout() {
        "ReadTimeout"
    } else if e.is_connect() {
        "ConnectError"
    } else if e.is_decode() {
        "DecodingError"
    } else if e.is_status() {
        "HTTPStatusError"
    } else {
        "HTTPError"
    }
}

// ---------- 一个连接 ----------
type Job = BoxFuture<'static, anyhow::Result<()>>;

pub struct Chan {
    pub id: i64,
    pub kind: String,
    pub secret: String,
    pub row: Mutex<Value>,
    state: Mutex<(String, Option<Later>)>,
    tx: mpsc::UnboundedSender<Job>,
    rx: Mutex<Option<mpsc::UnboundedReceiver<Job>>>,
    cards: Mutex<HashMap<i64, (String, Card)>>,
    pub carding: Mutex<HashSet<i64>>,
    /// 在这边处理的卡片，处理完卡片上写什么（“已回答：……”“已经做成了”）：确认结果的事件里没有这些
    marks: Mutex<HashMap<i64, String>>,
    jobs: Mutex<Vec<JoinHandle<()>>>,
    typer: Mutex<Option<JoinHandle<()>>>,
    pub platform: Arc<dyn Platform>,
}

pub static RUNNING: LazyLock<Mutex<BTreeMap<i64, Arc<Chan>>>> = LazyLock::new(Default::default);

pub fn changed() {
    tokio::spawn(async { server::broadcast(json!({"type": "channels_changed"})) });
}

fn refresh(cid: i64) {
    let ch = RUNNING.lock().get(&cid).cloned();
    if let (Some(ch), Some(r)) = (ch, row(cid)) {
        *ch.row.lock() = r;
    }
}

impl Chan {
    fn new(r: Value, secret: String) -> Arc<Chan> {
        let (tx, rx) = mpsc::unbounded_channel();
        let kind = r["kind"].as_str().unwrap_or("").to_string();
        Arc::new(Chan {
            id: r["id"].as_i64().unwrap_or(0),
            platform: make(&kind, &r),
            kind,
            secret,
            row: Mutex::new(r),
            state: Mutex::new(("connecting".into(), None)),
            tx,
            rx: Mutex::new(Some(rx)),
            cards: Mutex::new(HashMap::new()),
            carding: Mutex::new(HashSet::new()),
            marks: Mutex::new(HashMap::new()),
            jobs: Mutex::new(vec![]),
            typer: Mutex::new(None),
        })
    }

    pub fn peer(&self) -> String {
        self.row.lock()["peer"].as_str().unwrap_or("").to_string()
    }

    pub fn config(&self, k: &str) -> Value {
        self.row.lock()["config"][k].clone()
    }

    pub fn state(&self) -> String {
        self.state.lock().0.clone()
    }

    pub fn set_state(&self, state: &str, error: Option<Later>) {
        let mut s = self.state.lock();
        if s.0 != state || s.1 != error {
            *s = (state.to_string(), error);
            drop(s);
            changed();
        }
    }

    fn error_text(&self) -> String {
        self.state.lock().1.as_ref().map(|l| l.text()).unwrap_or_default()
    }

    fn launch(self: &Arc<Self>) {
        let me = self.clone();
        let guard = tokio::spawn(async move {
            let p = me.platform.clone();
            if let Err(e) = p.run(me.clone()).await {
                me.set_state("error", Some(Later::new("停止了：{error}", &[("error", json!(e.to_string()))])));
            }
        });
        let me = self.clone();
        let mut rx = self.rx.lock().take();
        let sender = tokio::spawn(async move {
            let Some(rx) = rx.as_mut() else { return };
            while let Some(job) = rx.recv().await {
                if let Err(e) = job.await {
                    eprintln!("[聊天软件 {} {}] 发送失败：{e}", me.id, me.kind);
                }
            }
        });
        *self.jobs.lock() = vec![guard, sender];
    }

    fn close(&self) {
        for j in self.jobs.lock().drain(..) {
            j.abort();
        }
        if let Some(t) = self.typer.lock().take() {
            t.abort();
        }
    }

    pub fn queue(&self, job: Job) {
        let _ = self.tx.send(job);
    }

    /// 发一段 Markdown 给 peer（默认配对过的人）
    pub fn say(self: &Arc<Self>, md: &str, peer: Option<&str>) {
        let peer = peer.map(String::from).unwrap_or_else(|| self.peer());
        if peer.is_empty() || md.trim().is_empty() {
            return;
        }
        let me = self.clone();
        let md = md.to_string();
        self.queue(Box::pin(async move {
            for part in fmt::split(&absolute(&md), me.platform.max_len()) {
                me.platform.send_text(&me, &peer, &part).await?;
            }
            Ok(())
        }));
    }

    fn send_card(self: &Arc<Self>, peer: String, card: Card) {
        let me = self.clone();
        self.queue(Box::pin(async move {
            let mut card = card;
            card["body"] = json!(absolute(card["body"].as_str().unwrap_or("")));
            let id = card["id"].as_i64().unwrap_or(0);
            let mid = me.platform.send_card(&me, &peer, &card).await?;
            me.cards.lock().insert(id, (mid, card));
            Ok(())
        }));
    }

    fn finish_card(self: &Arc<Self>, peer: String, aid: i64, status: String, note: Option<String>) {
        let me = self.clone();
        self.queue(Box::pin(async move {
            let hit = me.cards.lock().remove(&aid);
            me.carding.lock().remove(&aid);
            let mark = me.marks.lock().remove(&aid);
            let Some((mid, card)) = hit else { return Ok(()) };
            let kind = card["kind"].as_str().unwrap_or("");
            let done = match (mark, note) {
                (Some(m), _) => m,
                (None, Some(n)) if kind == "question" && status == "approved" => answered(&n),
                _ => done_text(kind, &status),
            };
            let mut c = card.clone();
            c["buttons"] = json!([]);
            c["link"] = Value::Null;
            c["done"] = json!(done);
            me.platform.edit_card(&me, &peer, &mid, &c).await
        }));
    }

    pub fn consider(self: &Arc<Self>, ev: &Value) {
        let (peer, chat_id) = {
            let r = self.row.lock();
            (r["peer"].as_str().unwrap_or("").to_string(), r["chat_id"].clone())
        };
        if peer.is_empty() {
            return;
        }
        let t = ev["type"].as_str().unwrap_or("");
        let mine = ev["chat_id"] == chat_id && !chat_id.is_null();
        if t == "message" && mine {
            let chat = ev["chat_id"].as_i64().and_then(bots::chat);
            if let Some(md) = render(&ev["message"], chat.is_some_and(|c| c["kind"] == "group")) {
                self.say(&md, None);
            }
        } else if t == "approval" {
            let aid = ev["id"].as_i64().unwrap_or(0);
            let status = ev["status"].as_str().unwrap_or("");
            if status == "pending" && (mine || ev["origin_chat_id"] == chat_id) {
                self.carding.lock().insert(aid);
                self.send_card(peer, card_for(ev));
            } else if status != "pending" && self.carding.lock().contains(&aid) {
                let st = if py::truthy(&ev["always"]) {
                    "always"
                } else if ev["scope"] == "task" {
                    "task"
                } else {
                    status
                };
                // 网页上回答的问题：事件里带了回答（note）就写出来
                let note = ev["note"].as_str().map(str::trim).filter(|n| !n.is_empty()).map(String::from);
                self.finish_card(peer, aid, st.to_string(), note);
            }
        } else if t == "stream_start" && mine {
            self.start_typing();
        } else if t == "stream_end" && mine {
            self.stop_typing();
        } else if t == "error" && mine {
            self.say(&format!("⚠️ {}", py::str_of(&ev["message"])), None);
        }
    }

    fn start_typing(self: &Arc<Self>) {
        if self.platform.start_typing(self) {
            return;
        }
        let mut t = self.typer.lock();
        if t.as_ref().is_some_and(|h| !h.is_finished()) {
            return;
        }
        let me = self.clone();
        let peer = self.peer();
        *t = Some(tokio::spawn(async move {
            for _ in 0..45 {
                if me.platform.typing(&me, &peer).await.is_err() {
                    return;
                }
                tokio::time::sleep(std::time::Duration::from_secs(4)).await;
            }
        }));
    }

    fn stop_typing(self: &Arc<Self>) {
        if self.platform.stop_typing(self) {
            return;
        }
        if let Some(t) = self.typer.lock().take() {
            t.abort();
        }
    }

    /// 收到一条私聊消息。files：[(文件名, 平台取文件用的引用)]
    pub async fn inbound(self: &Arc<Self>, peer: &str, who: &str, text: &str, files: Vec<(String, Value)>) {
        let Some(r) = row(self.id) else { return };
        *self.row.lock() = r.clone();
        let text = text.trim().to_string();
        let owner = r["peer"].as_str().unwrap_or("").to_string();
        if owner.is_empty() {
            return self.pair(peer, who, &text);
        }
        if peer != owner {
            return;
        }
        let chat_id = r["chat_id"].as_i64().unwrap_or(0);
        let cmd = if text.starts_with('/') {
            text.split_whitespace().next().unwrap_or("").split('@').next().unwrap_or("").to_lowercase()
        } else {
            String::new()
        };
        if cmd == "/start" || cmd == "/help" {
            return self.say(&t!(HELP, name = chat_title(Some(chat_id))), None);
        }
        if cmd == "/new" {
            if let Some(c) = bots::chat(chat_id) {
                server::reset_chat(&c);
            }
            return self.say(&t!("已开始新话题，之前的对话不再带进上下文（长期记忆还在）。"), None);
        }
        if let Some((act, aid)) = word_cmd(&text.replace('’', "'"))
            && self.carding.lock().contains(&aid)
        {
            let (okd, toast) = self.press_inner(peer, &format!("ap:{aid}:{act}")).await;
            if !okd {
                self.say(&toast, None);
            }
            return;
        }
        // “回答 #12 北京”：回答指定的那个问题
        if let Some((aid, said)) = answer_cmd(&text)
            && self.carding.lock().contains(&aid)
        {
            let (okd, toast) = self.answer(aid, &said);
            if !okd {
                self.say(&toast, None);
            }
            return;
        }
        if cmd == "/stop" {
            let done: Vec<String> = router::tasks()
                .into_iter()
                .filter(|tk| tk.chat_id == chat_id && tk.status() == "running")
                .map(|tk| router::cancel(tk.id, &t!("用户在聊天软件里停止")))
                .collect();
            return self.say(
                &if done.is_empty() {
                    t!("现在没有正在做的任务。")
                } else {
                    t!("已停止：{tasks}", tasks = done.join(&t!("、")))
                },
                None,
            );
        }
        // 这个对话里正好有一个问题在等：直接回的这句话就是回答，不交给 Bot（有几个在等、或者带了文件时照常交给 Bot）
        if files.is_empty()
            && !text.is_empty()
            && !text.starts_with('/')
            && let [aid] = self.questions()[..]
        {
            let (okd, toast) = self.answer(aid, &text);
            if !okd {
                self.say(&toast, None);
            }
            return;
        }
        let Some(chat) = bots::chat(chat_id) else {
            return self.say(&t!("绑定的对话不存在了，请在网页的 设置 → 聊天软件 里重新选择。"), None);
        };
        let mut atts = vec![];
        for (name, fref) in files {
            let (data, name) = match self.platform.download(self, &fref).await {
                Ok((d, n)) => (d, if n.is_empty() { name } else { n }),
                Err(e) => {
                    self.say(&t!("附件「{name}」没存上：{error}", name = name, error = e.to_string()), None);
                    continue;
                }
            };
            let mut att = json!({"name": name, "size": data.len()});
            if media::sniff(&data).is_some()
                && data.len() <= media::MAX_BYTES
                && let Ok(m) = media::save(&data, &name, media::MAX_BYTES, false)
            {
                att["media"] = m["id"].clone();
            }
            match save_upload(&name, &data).await {
                Ok(p) => att["path"] = json!(p),
                Err(e) => {
                    if att.get("media").is_none() {
                        self.say(&t!("附件「{name}」没存上：{error}", name = name, error = e), None);
                        continue;
                    }
                }
            }
            atts.push(att);
        }
        let atts = media::clean_attachments(&json!(atts));
        if !text.is_empty() || !atts.is_empty() {
            server::safe_handle(chat, text, self.kind.clone(), atts).await;
        }
    }

    fn pair(self: &Arc<Self>, peer: &str, who: &str, text: &str) {
        static CODE: LazyLock<fancy_regex::Regex> = LazyLock::new(|| fancy_regex::Regex::new(r"(?<!\d)(\d{6})(?!\d)").unwrap());
        let r = self.row.lock().clone();
        let code = CODE.captures(text).ok().flatten().and_then(|c| c.get(1).map(|m| m.as_str().to_string()));
        let pc = r["pair_code"].as_str().unwrap_or("").to_string();
        let valid = !pc.is_empty()
            && r["pair_expires"].as_f64().unwrap_or(0.0) > py::now()
            && r["pair_fails"].as_i64().unwrap_or(0) < PAIR_MAX_FAILS;
        if valid && code.as_deref().is_some_and(|c| eq(c.as_bytes(), pc.as_bytes())) {
            db::exec(
                "update channels set peer=?, peer_name=?, pair_code='', pair_fails=0 where id=?",
                params![peer, py::head(who, 80), self.id],
            );
            if let Some(nr) = row(self.id) {
                *self.row.lock() = nr;
            }
            changed();
            let msg = format!("{}\n\n{}", t!("配对成功 ✓"), t!(HELP, name = chat_title(r["chat_id"].as_i64())));
            return self.say(&msg, Some(peer));
        }
        if code.is_some() && valid {
            db::exec("update channels set pair_fails=pair_fails+1 where id=?", params![self.id]);
            if let Some(nr) = row(self.id) {
                let fails = nr["pair_fails"].as_i64().unwrap_or(0);
                *self.row.lock() = nr;
                if fails >= PAIR_MAX_FAILS {
                    changed();
                }
            }
            return self.say(&t!("配对码不对。"), Some(peer));
        }
        self.say(&t!("这个机器人还没配对。在 open-super-agent 网页的 设置 → 聊天软件 里找到它的 6 位配对码，发给我。"), Some(peer));
    }

    /// 卡片上的按钮被点了。返回给用户的一句提示
    pub async fn press(self: &Arc<Self>, peer: &str, data: &str) -> String {
        self.press_inner(peer, data).await.1
    }

    /// (处理了没有, 提示)
    pub async fn press_inner(self: &Arc<Self>, peer: &str, data: &str) -> (bool, String) {
        static R: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^ap:(\d+):(\w+)$").unwrap());
        if peer.is_empty() || peer != self.peer() {
            return (false, t!("没有权限"));
        }
        let Some(c) = R.captures(data) else { return (false, t!("这个按钮已经失效了")) };
        let aid: i64 = c[1].parse().unwrap_or(0);
        let act = c[2].to_string();
        if !self.carding.lock().contains(&aid) {
            return (false, t!("这个按钮已经失效了"));
        }
        let item = approvals::PENDING.lock().get(&aid).filter(|p| p.tx.is_some()).map(|p| p.item.clone());
        let Some(item) = item else { return (false, t!("已经处理过了")) };
        let opt = option_index(&act);
        if act_word(&act).is_none() && opt.is_none() {
            return (false, t!("这个按钮已经失效了"));
        }
        let kind = card_kind(&item);
        let fits = match kind {
            "help" => ["done", "fail"].contains(&act.as_str()),
            "question" => act == "skip" || opt.is_some(),
            "doubt" => ["redo", "made", "stop"].contains(&act.as_str()),
            _ => ["ok", "no", "always", "task"].contains(&act.as_str()),
        };
        if !fits {
            return (false, t!("这个请求不能这样处理"));
        }
        if act == "done" {
            handback(item["bot_id"].as_i64().unwrap_or(0));
            return (true, t!("已交还电脑，Bot 继续"));
        }
        if act == "fail" {
            approvals::resolve(aid, false, "用户表示处理不了", false, &Value::Null, None, None);
            return (true, t!("已告诉 Bot"));
        }
        if let Some(i) = opt {
            let Some(choice) = item["options"].get(i).and_then(|v| v.as_str()).map(String::from) else {
                return (false, t!("这个按钮已经失效了"));
            };
            self.marks.lock().insert(aid, answered(&choice));
            approvals::resolve(aid, true, &choice, false, &Value::Null, None, None);
            return (true, t!("已回答：{answer}", answer = py::head(&choice, 100)));
        }
        // 不回答；说不准那一步：再做一次（ok）、已经做成了（note done）、不做了停下任务（note stop）
        let (ok, note, mark, toast) = match act.as_str() {
            "skip" => (false, "", t!("✕ 没有回答"), t!("已告诉 Bot 你不回答")),
            "redo" => (true, "", t!("✓ 再做一次"), t!("好的，再做一次")),
            "made" => (false, "done", t!("✓ 已经做成了，不再做"), t!("好的，不再做这一步")),
            "stop" => (false, "stop", t!("⏹ 不做了，任务停下"), t!("好的，任务停下")),
            _ => {
                let scope = match act.as_str() {
                    "always" => Some("always"),
                    "task" => Some("task"),
                    _ => None,
                };
                let ok = ["ok", "always", "task"].contains(&act.as_str());
                return (true, approvals::resolve(aid, ok, "", false, &Value::Null, scope, None));
            }
        };
        self.marks.lock().insert(aid, mark);
        approvals::resolve(aid, ok, note, false, &Value::Null, None, None);
        (true, toast)
    }

    /// 这个连接上还在等回答的问题（卡片发到了这边的）
    fn questions(&self) -> Vec<i64> {
        let mine: HashSet<i64> = self.carding.lock().clone();
        approvals::PENDING
            .lock()
            .iter()
            .filter(|(id, p)| mine.contains(id) && p.tx.is_some() && p.item["kind"] == "question")
            .map(|(id, _)| *id)
            .collect()
    }

    /// 用一句话回答一个问题：和网页上回复任务一样交给任务（对话里留一条“给任务的话”），交不过去的直接了结卡片
    fn answer(&self, aid: i64, text: &str) -> (bool, String) {
        let text = text.trim();
        let item = approvals::PENDING.lock().get(&aid).filter(|p| p.tx.is_some()).map(|p| p.item.clone());
        let Some(item) = item else { return (false, t!("已经处理过了")) };
        if card_kind(&item) != "question" {
            return (false, t!("这个请求不能这样处理"));
        }
        if text.is_empty() {
            return (false, t!("回答不能为空"));
        }
        self.marks.lock().insert(aid, answered(text));
        if let Some(tid) = item["task_id"].as_i64() {
            let _ = crate::taskstore::message(tid, text);
        }
        if approvals::PENDING.lock().get(&aid).is_some_and(|p| p.tx.is_some()) {
            approvals::resolve(aid, true, text, false, &Value::Null, None, None);
        }
        (true, t!("已回答：{answer}", answer = py::head(text, 100)))
    }
}

fn eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// 交还电脑（“请你帮忙”处理完了）
fn handback(bot_id: i64) {
    crate::computer::get(bot_id).handback();
    let ids: Vec<i64> = approvals::PENDING
        .lock()
        .values()
        .filter(|p| p.item["bot_id"].as_i64() == Some(bot_id) && p.item["kind"] == "help")
        .filter_map(|p| p.item["id"].as_i64())
        .collect();
    for a in ids {
        approvals::resolve(a, true, "", false, &Value::Null, None, None);
    }
    tokio::spawn(async move {
        crate::server::broadcast(json!({"type": "computer", "computer": crate::computer::public_state(bot_id).await}));
    });
}

/// 聊天软件发来的文件存到电脑的 uploads 目录（和网页上传附件一样）
async fn save_upload(name: &str, data: &[u8]) -> Result<String, String> {
    let c = crate::computer::get(0);
    if c.state().await != "running" {
        c.start().await.map_err(|e| e.0)?;
    }
    let name: String = name.chars().map(|ch| if ch == '/' || ch == '\\' || ch == '\0' { '_' } else { ch }).collect();
    let name = name.trim();
    let name = if name.is_empty() { "file".to_string() } else { crate::py::head(name, 200) };
    let r = crate::library::put_file(&c, "uploads", &name, data).await?;
    Ok(crate::py::str_of(&r["path"]))
}

// ---------- 增删改 ----------
pub fn public(r: &Value) -> Value {
    let id = r["id"].as_i64().unwrap_or(0);
    let ch = RUNNING.lock().get(&id).cloned();
    let kind = r["kind"].as_str().unwrap_or("");
    let enabled = py::truthy(&r["enabled"]);
    let state = if enabled { ch.as_ref().map(|c| c.state()).unwrap_or_else(|| "connecting".into()) } else { "off".into() };
    let mut out = json!({"id": r["id"], "kind": kind, "title": t!(title_of(kind)), "name": r["name"], "chat_id": r["chat_id"],
        "enabled": enabled, "state": state, "error": if enabled { ch.as_ref().map(|c| c.error_text()).unwrap_or_default() } else { String::new() },
        "paired": py::truthy(&r["peer"]), "peer_name": r["peer_name"], "created": r["created"]});
    if !py::truthy(&r["peer"]) {
        let okc = py::truthy(&r["pair_code"])
            && r["pair_expires"].as_f64().unwrap_or(0.0) > py::now()
            && r["pair_fails"].as_i64().unwrap_or(0) < PAIR_MAX_FAILS;
        out["pair_code"] = if okc { r["pair_code"].clone() } else { json!("") };
        out["pair_expires"] = if okc { r["pair_expires"].clone() } else { json!(0) };
        out["pair_link"] = json!(if okc { pair_link(r) } else { String::new() });
        out["setup_link"] = json!(if okc { setup_link(r) } else { String::new() });
    }
    out
}

pub fn list_public() -> Vec<Value> {
    rows().iter().map(public).collect()
}

pub async fn create(kind: &str, fields: &Value, chat_id: i64) -> Result<Value, String> {
    if !known(kind) {
        return Err(t!("不支持的聊天软件：{kind}", kind = kind));
    }
    if bots::chat(chat_id).is_none() {
        return Err(t!("要绑定的对话不存在"));
    }
    let info = check(kind, fields).await?;
    for r in rows() {
        if r["kind"] == kind && same(kind, &r, &info) {
            return Err(t!("这个账号已经连接过了（{name}）", name = r["name"].clone()));
        }
    }
    let cid = db::insert(
        "insert into channels(kind, name, config, chat_id, created) values(?,?,?,?,?)",
        params![kind, py::str_of(&info["name"]), py::dumps_u(&info["config"]), chat_id, py::now()],
    );
    vault::put(&secret_name(cid), info["secret"].as_str().unwrap_or(""))?;
    new_pair_code(cid);
    if let Some(r) = row(cid) {
        launch(&r);
    }
    changed();
    Ok(row(cid).map(|r| public(&r)).unwrap_or(Value::Null))
}

pub fn new_pair_code(cid: i64) {
    let code = format!("{:06}", rand::random::<u32>() % 1_000_000);
    db::exec(
        "update channels set peer='', peer_name='', pair_code=?, pair_expires=?, pair_fails=0 where id=?",
        params![code, py::now() + PAIR_TTL, cid],
    );
    refresh(cid);
}

pub async fn update(cid: i64, data: &Value) -> Result<Value, ApiError> {
    if row(cid).is_none() {
        return Err(ApiError::bad(t!("没有这个连接")));
    }
    if data.get("chat_id").is_some() {
        let c = int(&data["chat_id"])?;
        if bots::chat(c).is_none() {
            return Err(ApiError::bad(t!("要绑定的对话不存在")));
        }
        db::exec("update channels set chat_id=? where id=?", params![c, cid]);
    }
    if let Some(e) = data.get("enabled") {
        db::exec("update channels set enabled=? where id=?", params![py::truthy(e) as i64, cid]);
    }
    let r = row(cid).unwrap_or(Value::Null);
    if data.get("enabled").is_some() {
        if py::truthy(&r["enabled"]) {
            launch(&r);
        } else {
            stop_one(cid);
        }
    }
    refresh(cid);
    if data.get("chat_id").is_some()
        && py::truthy(&r["peer"])
        && let Some(ch) = RUNNING.lock().get(&cid).cloned()
    {
        ch.say(&t!("已改成和「{title}」对话。", title = chat_title(r["chat_id"].as_i64())), None);
    }
    changed();
    Ok(public(&r))
}

pub fn delete(cid: i64) {
    stop_one(cid);
    vault::delete(&secret_name(cid));
    db::exec("delete from channels where id=?", params![cid]);
    changed();
}

/// 绑定的 Bot 或群聊被删了：改成和主 Bot 对话
pub fn chat_removed(chat_id: i64) {
    let bound: Vec<Value> = rows().into_iter().filter(|r| r["chat_id"].as_i64() == Some(chat_id)).collect();
    if bound.is_empty() {
        return;
    }
    let to = bots::primary().and_then(|p| p["chat_id"].as_i64());
    for r in bound {
        let id = r["id"].as_i64().unwrap_or(0);
        db::exec("update channels set chat_id=? where id=?", params![to, id]);
        refresh(id);
        if py::truthy(&r["peer"])
            && let Some(ch) = RUNNING.lock().get(&id).cloned()
        {
            ch.say(&t!("原来绑定的对话被删除了，改成和「{title}」对话。", title = chat_title(to)), None);
        }
    }
    changed();
}

// ---------- 启停 ----------
fn launch(r: &Value) {
    let id = r["id"].as_i64().unwrap_or(0);
    stop_one(id);
    let ch = Chan::new(r.clone(), vault::get(&secret_name(id)).unwrap_or_default());
    RUNNING.lock().insert(id, ch.clone());
    ch.launch();
}

fn stop_one(cid: i64) -> Option<Arc<Chan>> {
    let ch = RUNNING.lock().remove(&cid);
    if let Some(c) = &ch {
        c.close();
    }
    ch
}

pub async fn start() {
    for r in rows() {
        if py::truthy(&r["enabled"]) {
            launch(&r);
        }
    }
}

pub async fn stop() {
    let ids: Vec<i64> = RUNNING.lock().keys().copied().collect();
    for i in ids {
        stop_one(i);
    }
}

// ---------- 界面事件 → 聊天软件 ----------
/// server::broadcast 每个事件都会调用这里；只排队，不在这里等网络
pub fn on_event(event: &Value) {
    let all: Vec<Arc<Chan>> = RUNNING.lock().values().cloned().collect();
    for ch in all {
        ch.consider(event);
    }
}

fn bot_name(bot_id: &Value) -> String {
    bot_id.as_i64().filter(|b| *b != 0).and_then(bots::get).map(|b| py::str_of(&b["name"])).unwrap_or_else(|| "Bot".into())
}

fn chat_title(chat_id: Option<i64>) -> String {
    let t = chat_id.filter(|c| *c != 0).and_then(bots::chat).map(|c| py::str_of(&c["title"])).unwrap_or_default();
    if t.is_empty() || t == "None" { t!("已删除的对话") } else { t }
}

fn duration(s: &Value) -> String {
    let s = py::to_int(s).unwrap_or(0);
    if s < 60 {
        return t!("{s} 秒", s = s);
    }
    if s < 3600 {
        return t!("{m} 分 {s} 秒", m = s / 60, s = s % 60);
    }
    t!("{h} 小时 {m} 分", h = s / 3600, m = s % 3600 / 60)
}

/// 把站内链接补成完整地址
pub fn absolute(md: &str) -> String {
    static A: LazyLock<fancy_regex::Regex> = LazyLock::new(|| fancy_regex::Regex::new(r"\]\((/(?!/)[^)\s]*)\)").unwrap());
    static B: LazyLock<fancy_regex::Regex> =
        LazyLock::new(|| fancy_regex::Regex::new(r"(?<![\w\]\(/.:])(/(?:library/pages|s)/[\w-]+)").unwrap());
    let base = public_url();
    if base.is_empty() {
        return md.to_string();
    }
    let md = fmt::sub(&A, md, |m| format!("]({base}{})", m.get(1).map(|x| x.as_str()).unwrap_or("")));
    fmt::sub(&B, &md, |m| format!("{base}{}", m.get(1).map(|x| x.as_str()).unwrap_or("")))
}

fn files(atts: &Value) -> String {
    let Some(a) = atts.as_array().filter(|a| !a.is_empty()) else { return String::new() };
    let lines: Vec<String> = a
        .iter()
        .map(|x| format!("📎 [{}]({})", if py::truthy(&x["name"]) { py::str_of(&x["name"]) } else { "file".into() }, media::link(x)))
        .collect();
    format!("\n\n{}", lines.join("\n"))
}

fn strip_brackets(s: &str) -> &str {
    s.trim_matches(|c| c == '[' || c == ']')
}

/// 一条界面消息在聊天软件里怎么说；不用发的返回 None
pub fn render(m: &Value, group: bool) -> Option<String> {
    static RES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\[任务 #\d+ 结果\]\s*").unwrap());
    static ROUT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\[任务已派发\] \[定时任务 ([^\]]+)\]").unwrap());
    let meta = if m["meta"].is_object() { m["meta"].clone() } else { json!({}) };
    let role = m["role"].as_str().unwrap_or("");
    let content = m["content"].as_str().unwrap_or("").trim().to_string();
    let name = bot_name(&m["bot_id"]);
    let fl = files(&meta["attachments"]);
    let ms = |k: &str| if py::truthy(&meta[k]) { py::str_of(&meta[k]) } else { String::new() };
    if role == "assistant" {
        if content.is_empty() && fl.is_empty() {
            return None;
        }
        return Some(format!("{}{content}{fl}", if group { format!("**{name}**\n") } else { String::new() }));
    }
    if role != "task" {
        return None;
    }
    match meta["kind"].as_str().unwrap_or("") {
        "result" => {
            let tid = meta["task_id"].clone();
            let status = meta["status"].as_str().unwrap_or("");
            let body = RES.replace(&content, "").into_owned();
            let d = tid.as_i64().and_then(|t| db::row("select content from messages where id=?", params![t])).map(|r| r.s(0));
            let routine = d.as_deref().and_then(|d| ROUT.captures(d).map(|c| c[1].to_string()));
            let mut head = match (&routine, status) {
                (Some(n), "done") => t!("✅ 定时任务「{name}」完成", name = n),
                (Some(n), "failed") => t!("❌ 定时任务「{name}」失败", name = n),
                (Some(n), _) => t!("⏹ 定时任务「{name}」没做完", name = n),
                (None, "done") => t!("✅ 任务 #{id} 完成", id = tid),
                (None, "failed") => t!("❌ 任务 #{id} 失败", id = tid),
                (None, _) => t!("⏹ 任务 #{id} 没做完", id = tid),
            };
            if py::truthy(&meta["elapsed"]) {
                head += &t!("（用时 {d}）", d = duration(&meta["elapsed"]));
            }
            Some(format!("**{head}**{}\n\n{body}{fl}", if group { format!(" · {name}") } else { String::new() }))
        }
        "image" => Some(t!("🎨 {name} 画好了一张图：{prompt}", name = name, prompt = ms("prompt")) + &fl),
        "video" => Some(t!("🎬 {name} 生成好了一段视频：{prompt}", name = name, prompt = ms("prompt")) + &fl),
        "cancel" => Some(format!("⏹ {}", strip_brackets(&content))),
        "handoff" => Some(t!(
            "{name} → {to}：{note}",
            name = name,
            to = bot_name(&meta["to"]),
            note = if py::truthy(&meta["note"]) { py::str_of(&meta["note"]) } else { content.clone() }
        )),
        "dm" => Some(t!("✉️ {name} 私信 {to}：{text}", name = name, to = bot_name(&meta["to"]), text = content)),
        "dm_reply" => {
            if py::truthy(&meta["sent"]) {
                None
            } else {
                Some(format!("{}\n\n{content}", t!("**↩️ {name} 回信**", name = name)))
            }
        }
        "note" => Some(format!("ℹ️ {}", strip_brackets(&content))),
        "bot_created" | "bot_updated" | "bot_deleted" => Some(format!("🤖 {}", strip_brackets(&content))),
        _ => None,
    }
}

fn bot_card(tool: &str, d: &Value, who: &str, args: &str) -> (String, String) {
    let sep = t!("、");
    let s = |k: &str| if py::truthy(&d[k]) { py::str_of(&d[k]) } else { String::new() };
    let list = |k: &str| -> Vec<String> { d[k].as_array().map(|a| a.iter().map(py::str_of).collect()).unwrap_or_default() };
    if tool == "create_bot" {
        let skills = list("技能").join("\n");
        let duty = if s("职责").is_empty() { args.to_string() } else { s("职责") };
        return (
            t!("🤖 {name} 想新建一个 Bot", name = who),
            format!(
                "**{}**\n{}{}",
                s("名字"),
                py::head(&duty, 600),
                if skills.is_empty() { String::new() } else { format!("\n\n{}", py::head(&skills, 400)) }
            ),
        );
    }
    let bot = s("Bot");
    if tool == "update_bot" {
        let mut lines = vec![];
        if !s("新名字").is_empty() {
            lines.push(t!("名字改成：{name}", name = s("新名字")));
        }
        if !s("职责").is_empty() {
            lines.push(format!("{}\n{}", t!("职责改成："), py::head(&s("职责"), 600)));
        }
        if py::truthy(&d["头像"]) {
            lines.push(t!("换头像"));
        }
        for (key, label) in [("新技能", t!("新技能：")), ("改技能", t!("改技能：")), ("删技能", t!("删技能："))] {
            let l = list(key);
            if !l.is_empty() {
                lines.push(format!(
                    "{label} {}",
                    l.iter().map(|x| x.split('：').next().unwrap_or("").to_string()).collect::<Vec<_>>().join(" ")
                ));
            }
        }
        let body = if lines.is_empty() { args.to_string() } else { lines.join("\n\n") };
        return (t!("✏️ {name} 想修改 Bot「{bot}」", name = who, bot = bot), body);
    }
    let n = &d["会删掉"];
    let lost: Vec<String> =
        [("消息", "私聊里的 {n} 条消息"), ("技能", "{n} 个技能"), ("定时任务", "{n} 个定时任务"), ("记忆", "它自己的 {n} 条记忆")]
            .iter()
            .filter(|(k, _)| py::truthy(&n[*k]))
            .map(|(k, zh)| i18n::t(zh, &[("n", n[*k].clone())]))
            .collect();
    let mut lines = if py::truthy(&d["原因"]) { vec![s("原因")] } else { vec![] };
    lines.push(if lost.is_empty() {
        t!("它还没有聊天记录、技能、定时任务和记忆")
    } else {
        t!("会一起删掉：{what}", what = lost.join(&sep))
    });
    let groups = list("群聊");
    if !groups.is_empty() {
        lines.push(t!("会退出群聊：{groups}", groups = groups.iter().map(|g| format!("「{g}」")).collect::<Vec<_>>().join(&sep)));
    }
    if py::truthy(&d["在做的任务"]) {
        lines.push(t!("正在做的 {n} 个任务会停掉", n = d["在做的任务"].clone()));
    }
    lines.push(t!("删了不能恢复。"));
    (t!("🗑 {name} 想删除 Bot「{bot}」", name = who, bot = bot), lines.join("\n"))
}

fn btn(label: String, act: &str, style: &str) -> Value {
    json!([label, act, style])
}

/// 确认请求 → 卡片
pub fn card_for(ev: &Value) -> Card {
    static WS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());
    let who = bot_name(&ev["bot_id"]);
    let mut origin = String::new();
    if py::truthy(&ev["origin_chat_id"]) && ev["origin_chat_id"] != ev["chat_id"] {
        origin = t!("（在群聊「{title}」里派的）", title = chat_title(ev["origin_chat_id"].as_i64()));
    }
    let url = public_url();
    let args_full = ev["args"].as_str().unwrap_or("").to_string();
    let parse = |s: &str| -> Option<Value> { serde_json::from_str::<Value>(if s.is_empty() { "{}" } else { s }).ok() };
    if ev["kind"] == "help" {
        let reason = match parse(&args_full) {
            Some(v) if v.is_object() => {
                let r = &v["需要你做的事"];
                if py::truthy(r) { py::str_of(r) } else { args_full.clone() }
            }
            _ => args_full.clone(),
        };
        return json!({"id": ev["id"], "kind": "help", "title": t!("🙋 {name} 需要你帮忙", name = who),
            "body": format!("{reason}\n\n{}", t!("任务 #{id} 已暂停{origin}。要在网页上接手电脑处理，处理完点“处理完了”，Bot 会接着做。", id = ev["task_id"].clone(), origin = origin)),
            "buttons": [btn(t!("处理完了"), "done", "primary"), btn(t!("处理不了"), "fail", "")],
            "link": if url.is_empty() { Value::Null } else { json!([t!("去网页接手"), format!("{url}/?chat={}", py::str_of(&ev["chat_id"]))]) }, "done": ""});
    }
    let tool = ev["tool"].as_str().unwrap_or("").to_string();
    let args = py::head(&args_full, 800);
    let goal = WS.replace_all(ev["goal"].as_str().unwrap_or(""), " ").into_owned();
    let task_head = t!("任务 #{id}：{goal}", id = ev["task_id"].clone(), goal = goal.clone()) + &origin;
    if ev["kind"] == "question" {
        return question_card(ev, &who, &task_head);
    }
    if ev["doubt"].is_object() {
        return doubt_card(ev, &who, &task_head);
    }
    let mut when = String::new();
    if let Some(e) = ev["expires"].as_f64() {
        when = format!("\n{}", t!("后台任务，{time} 前没确认按拒绝处理。", time = py::strftime_at(e, "%H:%M")));
    }
    let mut buttons = vec![btn(t!("允许一次"), "ok", "primary"), btn(t!("拒绝"), "no", "danger")];
    let scopes: Vec<String> = match ev["scopes"].as_array().filter(|a| !a.is_empty()) {
        Some(a) => a.iter().filter_map(|x| x.as_str().map(String::from)).collect(),
        None => {
            if py::truthy(&ev["once"]) {
                vec![]
            } else {
                vec!["always".into()]
            }
        }
    };
    let has = |s: &str| scopes.iter().any(|x| x == s);
    if has("task") {
        buttons.push(btn(t!("本次任务内允许"), "task", ""));
    }
    let task_line = t!("任务 #{id}：{goal}", id = ev["task_id"].clone(), goal = goal) + &origin + &when;
    if tool == "generate_image" {
        let d = parse(&args_full).filter(|v| v.is_object()).unwrap_or(json!({}));
        let desc = if py::truthy(&d["描述"]) { py::str_of(&d["描述"]) } else { args.clone() };
        let model = if d.get("模型").is_some() { py::str_of(&d["模型"]) } else { String::new() };
        let price = if d.get("价格").is_some() { py::str_of(&d["价格"]) } else { String::new() };
        let body = format!("{desc}\n\n{} · {}{origin}", t!("模型：{model}", model = model), t!("价格：{price}", price = price));
        if has("always") {
            buttons.push(btn(t!("总是允许"), "always", ""));
        }
        return json!({"id": ev["id"], "kind": "approval", "title": t!("🎨 {name} 想画一张图", name = who), "body": body, "buttons": buttons, "link": null, "done": ""});
    }
    if ["create_bot", "update_bot", "delete_bot"].contains(&tool.as_str()) {
        let d = parse(&args_full).filter(|v| v.is_object()).unwrap_or(json!({}));
        let (title, mut body) = bot_card(&tool, &d, &who, &args);
        if py::truthy(&d["请求方"]) {
            body += &format!("\n\n{}", t!("（{name} 私信请它做的）", name = d["请求方"].clone()));
        }
        return json!({"id": ev["id"], "kind": "approval", "title": title, "body": body + &origin, "buttons": buttons, "link": null, "done": ""});
    }
    let body;
    if tool == "ask_approval" {
        let what = match parse(&args) {
            Some(v) if v.is_object() && py::truthy(&v["操作"]) => py::str_of(&v["操作"]),
            _ => args.clone(),
        };
        body = format!("{what}\n\n{task_line}");
    } else {
        let (app, name) = match tool.rfind("__") {
            Some(i) => (tool[..i].to_string(), tool[i + 2..].to_string()),
            None => (String::new(), tool.clone()),
        };
        let head = if app.is_empty() { format!("**{name}**") } else { t!("应用 @{app} 的 **{tool}**", app = app, tool = name) };
        body = format!("{head}\n{task_line}\n```\n{args}\n```");
        let prefix = ev["prefix"].as_str().unwrap_or("");
        if has("always") && !prefix.is_empty() {
            buttons.push(btn(t!("总是允许 {prefix} 开头的", prefix = py::head(prefix, 20)), "always", ""));
        } else if has("always") {
            buttons.push(btn(t!("总是允许"), "always", ""));
        }
    }
    json!({"id": ev["id"], "kind": "approval", "title": t!("⚠️ {name} 需要你确认", name = who), "body": body, "buttons": buttons, "link": null, "done": ""})
}

/// 提问卡片上最多放几个选项按钮（Discord 一行最多 5 个按钮，还要留一个给“不回答”），其余写在正文里
const MAX_OPTION_BUTTONS: usize = 4;

/// “应用 @mail 的 **send**”/“**send**”
fn tool_head(tool: &str) -> String {
    match tool.rfind("__") {
        Some(i) => t!("应用 @{app} 的 **{tool}**", app = tool[..i].to_string(), tool = tool[i + 2..].to_string()),
        None => format!("**{tool}**"),
    }
}

/// 任务问用户一个问题（kind: question）：选项是按钮，另有“不回答”；也可以直接回一句话
fn question_card(ev: &Value, who: &str, task_head: &str) -> Card {
    let question = ev["question"].as_str().unwrap_or("").trim().to_string();
    let options: Vec<String> =
        ev["options"].as_array().map(|a| a.iter().map(py::str_of).filter(|s| !s.trim().is_empty()).collect()).unwrap_or_default();
    let mut buttons: Vec<Value> =
        options.iter().take(MAX_OPTION_BUTTONS).enumerate().map(|(i, o)| btn(py::head(o.trim(), 40), &format!("a{i}"), "")).collect();
    buttons.push(btn(t!("不回答"), "skip", "danger"));
    let mut body = question;
    if options.len() > MAX_OPTION_BUTTONS {
        body += &format!("\n\n{}", t!("其他候选：{options}", options = options[MAX_OPTION_BUTTONS..].join(&t!("、"))));
    }
    body += &format!("\n\n{task_head}");
    if let Some(e) = ev["expires"].as_f64() {
        body += &format!("\n{}", t!("后台任务，{time} 前没回答就不等了。", time = py::strftime_at(e, "%H:%M")));
    }
    body += &format!(
        "\n\n{}",
        t!("点一个选项，或者直接回一句话作答；同时有几个问题在等时，回复“回答 #{id} 你的回答”。", id = ev["id"].clone())
    );
    json!({"id": ev["id"], "kind": "question", "title": t!("❓ {name} 有个问题要你回答", name = who), "body": body,
           "buttons": buttons, "options": options, "link": null, "done": ""})
}

/// 服务重启时正在执行、说不准有没有做成的那一步又要做（带 doubt）：再做一次 / 已经做成了 / 不做了停下任务
fn doubt_card(ev: &Value, who: &str, task_head: &str) -> Card {
    let d = &ev["doubt"];
    let tool = d["tool"].as_str().filter(|s| !s.is_empty()).or(ev["tool"].as_str()).unwrap_or("").to_string();
    let pretty = |s: &str| match serde_json::from_str::<Value>(s) {
        Ok(v) if v.is_object() || v.is_array() => py::dumps_indent(&v, 1, false),
        _ => s.to_string(),
    };
    let before = d["args"].as_str().unwrap_or("").trim().to_string();
    let now = ev["args"].as_str().unwrap_or("").trim().to_string();
    let same = match (serde_json::from_str::<Value>(&before), serde_json::from_str::<Value>(&now)) {
        (Ok(a), Ok(b)) => a == b,
        _ => before == now,
    };
    // 为什么说不准：卡住超时被打断（stall）、任务被停下（stopped）、服务重启（restart，没写也按它）
    let mut body = match d["why"].as_str().unwrap_or("") {
        "stall" => t!("这一步卡住超时、没做完就被打断了，说不准有没有做成。"),
        "stopped" => t!("任务被停下时这一步正在执行，说不准有没有做成。"),
        _ => t!("服务重启时这一步正在执行，说不准有没有做成。"),
    };
    let mut head = tool_head(&tool);
    if let Some(s) = d["started"].as_f64().filter(|s| *s > 0.0) {
        head += &t!("（{time} 开始）", time = py::strftime_at(s, "%H:%M:%S"));
    }
    body += &format!("\n\n{head}\n```\n{}\n```", py::head(&pretty(&before), 600));
    if !same && !now.is_empty() {
        body += &format!("\n{}\n```\n{}\n```", t!("再做一次的话用这些参数："), py::head(&pretty(&now), 600));
    }
    let checked = d["checked"].as_str().unwrap_or("").trim().to_string();
    let checked = if checked.is_empty() { t!("没查出来") } else { py::head(&checked, 600) };
    body += &format!("\n\n{}\n\n{task_head}", t!("Bot 查过：{checked}", checked = checked));
    let buttons =
        vec![btn(t!("再做一次"), "redo", "primary"), btn(t!("已经做成了"), "made", ""), btn(t!("不做了，停下任务"), "stop", "danger")];
    json!({"id": ev["id"], "kind": "doubt", "title": t!("⚠️ {name} 有一步要你定", name = who), "body": body,
           "buttons": buttons, "link": null, "done": ""})
}

/// 按钮动作 → 打字时说的那个词（钉钉、企业微信发不了卡片时让用户回复的就是它）
const ACTS: [(&str, &str); 10] = [
    ("ok", "允许一次"),
    ("no", "拒绝"),
    ("always", "总是允许"),
    ("task", "本次任务内允许"),
    ("done", "处理完了"),
    ("fail", "处理不了"),
    ("skip", "不回答"),
    ("redo", "再做一次"),
    ("made", "已经做成"),
    ("stop", "停下任务"),
];

fn act_word(act: &str) -> Option<&'static str> {
    ACTS.iter().find(|(a, _)| *a == act).map(|(_, w)| *w)
}

/// 提问卡片上第几个选项的按钮（a0、a1……）
fn option_index(act: &str) -> Option<usize> {
    act.strip_prefix('a').filter(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit())).and_then(|n| n.parse().ok())
}

/// 卡片是哪一种：help（请你帮忙）、question（任务提问）、doubt（重启时说不准做没做成的那一步）、approval（普通确认）
pub fn card_kind(item: &Value) -> &'static str {
    match item["kind"].as_str() {
        Some("help") => "help",
        Some("question") => "question",
        _ if item["doubt"].is_object() => "doubt",
        _ => "approval",
    }
}

fn answered(text: &str) -> String {
    t!("✓ 已回答：{answer}", answer = py::head(text.trim(), 200))
}

/// 点了等于按按钮的那句话（用界面的语言）。提问卡片的选项是“回答 #12 北京”
pub fn word_command(card: &Card, act: &str) -> String {
    let aid = py::str_of(&card["id"]);
    if let Some(i) = option_index(act) {
        let choice = card["options"].get(i).map(py::str_of).unwrap_or_default();
        return format!("{} #{aid} {choice}", t!("回答"));
    }
    format!("{} #{aid}", act_say(act))
}

/// 打字说的那个词，用界面的语言（新加的几个写成字面量，i18n 检查才找得到）
fn act_say(act: &str) -> String {
    match act {
        "skip" => t!("不回答"),
        "redo" => t!("再做一次"),
        "made" => t!("已经做成"),
        "stop" => t!("停下任务"),
        _ => t!(act_word(act).unwrap_or("")),
    }
}

/// 没有按钮的地方（钉钉、企业微信发不了卡片时）告诉用户可以回复什么
pub fn reply_hints(card: &Card) -> Vec<String> {
    let btns = card["buttons"].as_array().cloned().unwrap_or_default();
    let acts: Vec<String> = btns.iter().map(|b| py::str_of(&b[1])).collect();
    if card["kind"] == "question" {
        let mut out = vec![t!("“回答 #{id} 你的回答”", id = card["id"].clone())];
        out.extend(acts.iter().filter(|a| *a == "skip").map(|a| t!("“{text}”", text = word_command(card, a))));
        return out;
    }
    acts.iter().map(|a| t!("“{text}”", text = word_command(card, a))).collect()
}

static WORDS: LazyLock<Vec<(String, &'static str)>> = LazyLock::new(|| {
    let aliases: [(&str, &[&str]); 10] = [
        ("ok", &["允许一次", "允许", "同意"]),
        ("no", &["拒绝"]),
        ("always", &["总是允许"]),
        ("task", &["本次任务内允许", "本次任务"]),
        ("done", &["处理完了"]),
        ("fail", &["处理不了"]),
        ("skip", &["不回答"]),
        ("redo", &["再做一次", "再做"]),
        ("made", &["已经做成", "已经做成了", "做成了"]),
        ("stop", &["停下任务", "不做了，停下任务", "停下", "不做了"]),
    ];
    let mut out: Vec<(String, &'static str)> = vec![];
    let mut seen = HashSet::new();
    for (act, zhs) in aliases {
        for zh in zhs {
            for w in i18n::both(zh) {
                let w = w.to_lowercase();
                // Python 的 dict：后来的同名键覆盖前面的
                if !seen.insert(w.clone()) {
                    out.retain(|(x, _)| *x != w);
                }
                out.push((w, act));
            }
        }
    }
    out
});

static WORD_CMD: LazyLock<Regex> = LazyLock::new(|| {
    let mut ws: Vec<&String> = WORDS.iter().map(|(w, _)| w).collect();
    ws.sort_by_key(|w| std::cmp::Reverse(w.chars().count()));
    let alt = ws.iter().map(|w| regex::escape(w)).collect::<Vec<_>>().join("|");
    Regex::new(&format!(r"(?i)^({alt})\s*[#＃]?\s*(\d+)\s*[。.!！]?$")).unwrap()
});

/// “允许 #12”“拒绝 12” → (动作, 确认请求 id)
fn word_cmd(text: &str) -> Option<(&'static str, i64)> {
    let c = WORD_CMD.captures(text)?;
    let w = c[1].to_lowercase();
    let act = WORDS.iter().find(|(x, _)| *x == w).map(|(_, a)| *a)?;
    Some((act, c[2].parse().ok()?))
}

/// “回答 #12 北京”“Answer #12: Beijing” → (确认请求 id, 回答)
fn answer_cmd(text: &str) -> Option<(i64, String)> {
    static R: LazyLock<Regex> = LazyLock::new(|| {
        let alt = i18n::both("回答").iter().map(|w| regex::escape(w)).collect::<Vec<_>>().join("|");
        Regex::new(&format!(r"(?is)^(?:{alt})\s*[#＃]?\s*(\d+)[\s:：,，]+(\S.*)$")).unwrap()
    });
    let c = R.captures(text.trim())?;
    Some((c[1].parse().ok()?, c[2].trim().to_string()))
}

fn done_text(kind: &str, status: &str) -> String {
    match (kind, status) {
        ("help", "approved") => t!("✓ 已处理完，Bot 继续"),
        ("help", "rejected") => t!("✕ 没有处理"),
        ("help", "expired") => t!("已过期"),
        ("question", "approved") => t!("✓ 已回答"),
        ("question", "rejected") => t!("✕ 没有回答"),
        ("question", "expired") => t!("已过期，没有回答"),
        ("doubt", "approved") => t!("✓ 再做一次"),
        ("doubt", "rejected") => t!("✕ 没有再做"),
        ("doubt", "expired") => t!("已过期，没有再做"),
        (_, "approved") => t!("✓ 已允许"),
        (_, "always") => t!("✓ 已允许，以后自动允许"),
        (_, "task") => t!("✓ 已允许，这个任务里不再问"),
        (_, "rejected") => t!("✕ 已拒绝"),
        (_, "expired") => t!("已过期，按拒绝处理"),
        (_, "cancelled") => t!("任务已取消"),
        _ => String::new(),
    }
}

// ---------- 接口 ----------
async fn list_channels() -> Res {
    ok(json!({"channels": list_public(), "public_url": public_url()}))
}

async fn add_channel(Body(d): Body) -> Res {
    if py::truthy(&d["public_url"]) && public_url().is_empty() {
        set_public_url(&py::str_of(&d["public_url"]))?;
    }
    let chat_id = if py::truthy(&d["chat_id"]) { int(&d["chat_id"])? } else { 0 };
    let fields = if d["fields"].is_object() { d["fields"].clone() } else { json!({}) };
    ok(create(d["kind"].as_str().unwrap_or(""), &fields, chat_id).await?)
}

async fn channel_settings(Body(d): Body) -> Res {
    set_public_url(d["public_url"].as_str().unwrap_or(""))?;
    ok(json!({"public_url": public_url()}))
}

async fn update_channel(Path(cid): Path<i64>, Body(d): Body) -> Res {
    ok(update(cid, &d).await?)
}

async fn repair_channel(Path(cid): Path<i64>) -> Res {
    if row(cid).is_none() {
        return Err(ApiError::not_found(t!("没有这个连接")));
    }
    new_pair_code(cid);
    changed();
    ok(row(cid).map(|r| public(&r)).unwrap_or(Value::Null))
}

async fn delete_channel(Path(cid): Path<i64>) -> Res {
    delete(cid);
    ok(json!({"ok": true}))
}

pub fn routes() -> axum::Router {
    axum::Router::new()
        .route("/api/channels", get(list_channels).post(add_channel))
        .route("/api/channels/settings", put(channel_settings))
        .route("/api/channels/{cid}", patch(update_channel).delete(delete_channel))
        .route("/api/channels/{cid}/pair", post(repair_channel))
}
