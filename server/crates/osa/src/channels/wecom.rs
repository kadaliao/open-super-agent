//! 企业微信：智能机器人的长连接（API 模式选“长连接”）。对应 osa/channels/wecom.py（协议说明见那里）。
//! OSA_WECOM_WS 可以换成假的服务器（测试用）。

use super::{Card, Chan, Platform, fmt, reply_hints};
use crate::i18n::Later;
use crate::{i18n, py};
use async_trait::async_trait;
use futures::{SinkExt, StreamExt};
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, LazyLock};
use std::time::Duration;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::Message;

pub const PING_EVERY: u64 = 30;
pub const CARD_TEXT: usize = 110;

fn ws_url() -> String {
    std::env::var("OSA_WECOM_WS").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "wss://openws.work.weixin.qq.com".into())
}

#[derive(Debug)]
pub struct WecomError {
    pub code: Value,
    pub msg: String,
}

impl std::fmt::Display for WecomError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {}", py::str_of(&self.code), self.msg)
    }
}

impl std::error::Error for WecomError {}

fn req_id(prefix: &str) -> String {
    format!("{prefix}_{}_{}", (py::now() * 1000.0) as i64, crate::util::token_hex(4))
}

/// AES-256-CBC，IV 是密钥的前 16 字节，PKCS#7 按 32 字节补齐
pub fn decrypt(data: &[u8], aeskey: &str) -> anyhow::Result<Vec<u8>> {
    use aes::cipher::{BlockDecryptMut, KeyIvInit};
    let pad = (4 - aeskey.len() % 4) % 4;
    let key = crate::util::b64_decode(&format!("{aeskey}{}", "=".repeat(pad))).ok_or_else(|| anyhow::anyhow!(t!("文件解密失败")))?;
    if key.len() != 32 || !data.len().is_multiple_of(16) {
        anyhow::bail!(t!("文件解密失败"));
    }
    let mut buf = data.to_vec();
    let out = cbc::Decryptor::<aes::Aes256>::new_from_slices(&key, &key[..16])
        .map_err(|_| anyhow::anyhow!(t!("文件解密失败")))?
        .decrypt_padded_mut::<aes::cipher::block_padding::NoPadding>(&mut buf)
        .map_err(|_| anyhow::anyhow!(t!("文件解密失败")))?
        .to_vec();
    let p = *out.last().unwrap_or(&0) as usize;
    if !(1..=32).contains(&p) || p > out.len() || out[out.len() - p..].iter().any(|b| *b as usize != p) {
        anyhow::bail!(t!("文件解密失败"));
    }
    Ok(out[..out.len() - p].to_vec())
}

fn unquote(s: &str) -> String {
    url::form_urlencoded::parse(format!("x={}", s.replace('+', "%2B")).as_bytes()).next().map(|(_, v)| v.into_owned()).unwrap_or_default()
}

fn filename(disposition: &str) -> String {
    static A: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)filename\*=UTF-8''([^;\s]+)").unwrap());
    static B: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?i)filename="?([^";]+)"?"#).unwrap());
    if let Some(c) = A.captures(disposition) {
        return unquote(&c[1]);
    }
    if let Some(c) = B.captures(disposition) {
        return unquote(&c[1]).trim().to_string();
    }
    String::new()
}

type Sink =
    futures::stream::SplitSink<tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>, Message>;
type Stream = futures::stream::SplitStream<tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>>;

async fn subscribe(sink: &mut Sink, stream: &mut Stream, bot_id: &str, secret: &str, timeout: f64) -> anyhow::Result<()> {
    let rid = req_id("aibot_subscribe");
    sink.send(Message::Text(
        py::dumps(&json!({"cmd": "aibot_subscribe", "headers": {"req_id": rid}, "body": {"bot_id": bot_id, "secret": secret}}), true)
            .into(),
    ))
    .await?;
    let end = std::time::Instant::now() + Duration::from_secs_f64(timeout);
    loop {
        let left = end.saturating_duration_since(std::time::Instant::now()).max(Duration::from_millis(100));
        let m = tokio::time::timeout(left, stream.next()).await.map_err(|_| anyhow::anyhow!("TimeoutError"))?;
        let Some(Ok(m)) = m else { anyhow::bail!("ConnectionClosed") };
        let Ok(text) = m.to_text() else { continue };
        let Ok(f) = serde_json::from_str::<Value>(text) else { continue };
        if f["headers"]["req_id"] == rid.as_str() {
            if py::truthy(&f["errcode"]) {
                return Err(WecomError { code: f["errcode"].clone(), msg: f["errmsg"].as_str().unwrap_or("").to_string() }.into());
            }
            return Ok(());
        }
    }
}

async fn connect() -> anyhow::Result<(Sink, Stream)> {
    let (ws, _) = tokio::time::timeout(Duration::from_secs(15), tokio_tungstenite::connect_async(ws_url()))
        .await
        .map_err(|_| anyhow::anyhow!("TimeoutError"))??;
    Ok(ws.split())
}

pub async fn check(fields: &Value) -> Result<Value, String> {
    let bot_id = fields["bot_id"].as_str().unwrap_or("").trim().to_string();
    let secret = fields["secret"].as_str().unwrap_or("").trim().to_string();
    if bot_id.is_empty() || secret.is_empty() {
        return Err(t!("请填 Bot ID 和 Secret，在智能机器人的配置页里，API 模式选“长连接”后能看到"));
    }
    let r = async {
        let (mut sink, mut stream) = connect().await?;
        subscribe(&mut sink, &mut stream, &bot_id, &secret, 10.0).await?;
        let _ = sink.close().await;
        anyhow::Ok(())
    }
    .await;
    if let Err(e) = r {
        if let Some(w) = e.downcast_ref::<WecomError>() {
            return Err(t!(
                "Bot ID 或 Secret 不对，或者机器人的 API 模式没选“长连接”（{error}）",
                error = if w.msg.is_empty() { py::str_of(&w.code) } else { w.msg.clone() }
            ));
        }
        return Err(t!(
            "连不上企业微信（{error}）",
            error = if e.to_string() == "TimeoutError" { "TimeoutError".to_string() } else { "WebSocketException".to_string() }
        ));
    }
    let name = py::head(fields["name"].as_str().unwrap_or("").trim(), 40);
    Ok(json!({"name": if name.is_empty() { bot_id.clone() } else { name }, "config": {"bot_id": bot_id}, "secret": secret}))
}

#[derive(Clone)]
pub struct WeCom {
    http: reqwest::Client,
    ws: Arc<Mutex<Option<Arc<tokio::sync::Mutex<Sink>>>>>,
    acks: Arc<Mutex<HashMap<String, oneshot::Sender<Value>>>>,
    seen: Arc<Mutex<VecDeque<String>>>,
    events: Arc<Mutex<HashMap<String, (String, f64)>>>,
    missed: Arc<AtomicU32>,
}

impl WeCom {
    pub fn new() -> WeCom {
        WeCom {
            http: super::http(120),
            ws: Default::default(),
            acks: Default::default(),
            seen: Default::default(),
            events: Default::default(),
            missed: Default::default(),
        }
    }

    /// 发一帧并等回执
    async fn request(&self, cmd: &str, body: Value, rid: Option<String>) -> anyhow::Result<Value> {
        let ws = self.ws.lock().clone().ok_or_else(|| anyhow::anyhow!("还没连上企业微信"))?;
        let rid = rid.unwrap_or_else(|| req_id(cmd));
        let (tx, rx) = oneshot::channel();
        self.acks.lock().insert(rid.clone(), tx);
        let sent =
            ws.lock().await.send(Message::Text(py::dumps_u(&json!({"cmd": cmd, "headers": {"req_id": rid}, "body": body})).into())).await;
        let r = match sent {
            Ok(()) => tokio::time::timeout(Duration::from_secs(10), rx).await,
            Err(e) => {
                self.acks.lock().remove(&rid);
                return Err(e.into());
            }
        };
        self.acks.lock().remove(&rid);
        let f = r.map_err(|_| anyhow::anyhow!("TimeoutError"))?.map_err(|_| anyhow::anyhow!("长连接断了"))?;
        if py::truthy(&f["errcode"]) {
            return Err(WecomError { code: f["errcode"].clone(), msg: f["errmsg"].as_str().unwrap_or("").to_string() }.into());
        }
        Ok(f)
    }

    async fn session(&self, ch: &Arc<Chan>) -> anyhow::Result<bool> {
        let bot_id = py::str_of(&ch.config("bot_id"));
        let (mut sink, mut stream) = connect().await?;
        subscribe(&mut sink, &mut stream, &bot_id, &ch.secret, 10.0).await?;
        let sink = Arc::new(tokio::sync::Mutex::new(sink));
        *self.ws.lock() = Some(sink.clone());
        self.missed.store(0, Ordering::Relaxed);
        ch.set_state("connected", None);
        let me = self.clone();
        let s2 = sink.clone();
        let pinger = tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(PING_EVERY)).await;
                if me.missed.load(Ordering::Relaxed) >= 2 {
                    let _ = s2.lock().await.close().await;
                    return;
                }
                me.missed.fetch_add(1, Ordering::Relaxed);
                let _ = s2
                    .lock()
                    .await
                    .send(Message::Text(py::dumps(&json!({"cmd": "ping", "headers": {"req_id": req_id("ping")}}), true).into()))
                    .await;
            }
        });
        let mut kicked = false;
        while let Some(m) = stream.next().await {
            let text = match m {
                Ok(Message::Text(t)) => t.to_string(),
                Ok(Message::Close(_)) | Err(_) => break,
                _ => continue,
            };
            let Ok(f) = serde_json::from_str::<Value>(&text) else { continue };
            if self.frame(ch, &f) {
                kicked = true;
                break;
            }
        }
        pinger.abort();
        *self.ws.lock() = None;
        self.acks.lock().clear();
        Ok(kicked)
    }

    /// 处理一帧；被别处挤掉了返回 true
    fn frame(&self, ch: &Arc<Chan>, f: &Value) -> bool {
        let rid = f["headers"]["req_id"].as_str().unwrap_or("").to_string();
        let Some(cmd) = f["cmd"].as_str().filter(|s| !s.is_empty()) else {
            if rid.starts_with("ping") {
                self.missed.store(0, Ordering::Relaxed);
            } else if let Some(tx) = self.acks.lock().remove(&rid) {
                let _ = tx.send(f.clone());
            }
            return false;
        };
        let body = f["body"].clone();
        if let Some(mid) = body["msgid"].as_str().filter(|s| !s.is_empty()) {
            let mut seen = self.seen.lock();
            if seen.iter().any(|x| x == mid) {
                return false;
            }
            seen.push_back(mid.to_string());
            if seen.len() > 500 {
                seen.pop_front();
            }
        }
        if cmd == "aibot_msg_callback" {
            let (me, c) = (self.clone(), ch.clone());
            tokio::spawn(async move { me.on_message(&c, &body).await });
        } else if cmd == "aibot_event_callback" {
            let ev = body["event"].clone();
            match ev["eventtype"].as_str().unwrap_or("") {
                "disconnected_event" => return true,
                "template_card_event" => {
                    let inner = if ev["template_card_event"].is_object() { ev["template_card_event"].clone() } else { ev.clone() };
                    let (me, c) = (self.clone(), ch.clone());
                    tokio::spawn(async move { me.on_card(&c, &rid, &body, &inner).await });
                }
                "enter_chat" if ch.peer().is_empty() => {
                    let me = self.clone();
                    tokio::spawn(async move {
                        let _ = me
                            .request("aibot_respond_welcome_msg", json!({"msgtype": "text", "text": {"content": t!("你好！把 open-super-agent 网页上 设置 → 聊天软件 里的 6 位配对码发给我，就能开始用了。")}}), Some(rid))
                            .await;
                    });
                }
                _ => {}
            }
        }
        false
    }

    async fn on_message(&self, ch: &Arc<Chan>, b: &Value) {
        if b["chattype"] != "single" {
            return;
        }
        let peer = b["from"]["userid"].as_str().unwrap_or("").to_string();
        let n = py::tail(b["msgid"].as_str().unwrap_or(""), 10);
        let (mut text, mut files): (String, Vec<(String, Value)>) = (String::new(), vec![]);
        let mt = b["msgtype"].as_str().unwrap_or("");
        match mt {
            "text" => text = b["text"]["content"].as_str().unwrap_or("").to_string(),
            "voice" => {
                text = b["voice"]["content"].as_str().unwrap_or("").to_string();
                if text.is_empty() {
                    if peer == ch.peer() {
                        ch.say(&t!("没听清这条语音，请再说一遍或者打字。"), None);
                    }
                    return;
                }
            }
            "mixed" => {
                let mut parts = vec![];
                for (i, it) in b["mixed"]["msg_item"].as_array().cloned().unwrap_or_default().iter().enumerate() {
                    if it["msgtype"] == "text" {
                        parts.push(it["text"]["content"].as_str().unwrap_or("").to_string());
                    } else if it["msgtype"] == "image" && py::truthy(&it["image"]["url"]) {
                        files.push((format!("image_{n}_{i}.jpg"), it["image"].clone()));
                    }
                }
                text = parts.into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n");
            }
            "image" | "file" | "video" => {
                let item = &b[mt];
                if py::truthy(&item["url"]) {
                    let name = match mt {
                        "image" => format!("image_{n}.jpg"),
                        "video" => format!("video_{n}.mp4"),
                        _ => format!("file_{n}"),
                    };
                    files.push((name, item.clone()));
                }
            }
            _ => {
                if peer == ch.peer() {
                    ch.say(&t!("暂时只能收文字、语音、图片和文件。"), None);
                }
                return;
            }
        }
        ch.inbound(&peer, "", &text, files).await;
    }

    async fn on_card(&self, ch: &Arc<Chan>, rid: &str, b: &Value, ev: &Value) {
        let task_id = ev["task_id"].as_str().unwrap_or("").to_string();
        {
            let mut e = self.events.lock();
            e.insert(task_id.clone(), (rid.to_string(), py::now()));
            let cut = py::now() - 60.0;
            e.retain(|_, (_, t)| *t >= cut);
        }
        let (okd, toast) = ch.press_inner(b["from"]["userid"].as_str().unwrap_or(""), ev["event_key"].as_str().unwrap_or("")).await;
        if !okd {
            self.events.lock().remove(&task_id);
            ch.say(&toast, None);
        }
    }

    fn card(card: &Card, task_id: &str) -> Value {
        static FENCE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"```\w*\n?|`").unwrap());
        static NL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*\n\s*").unwrap());
        static LEAD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\W+").unwrap());
        let desc = fmt::plain(card["body"].as_str().unwrap_or(""));
        let desc = FENCE.replace_all(&desc, "").into_owned();
        let desc = NL.replace_all(&desc, " ｜ ").into_owned();
        let mut desc = desc.trim_matches(|c| c == ' ' || c == '｜').to_string();
        let more = t!("…（详情见上条）");
        if py::char_len(&desc) > CARD_TEXT {
            desc = py::head(&desc, CARD_TEXT.saturating_sub(py::char_len(&more))) + &more;
        }
        let zh = i18n::lang() == "zh";
        let title = py::head(&LEAD.replace(card["title"].as_str().unwrap_or(""), ""), if zh { 26 } else { 52 });
        let btn = if zh { 10 } else { 24 };
        let id = py::str_of(&card["id"]);
        let done = card["done"].as_str().unwrap_or("");
        let buttons: Vec<Value> = if !done.is_empty() {
            vec![json!({"text": py::head(done.trim_start_matches(['✓', '✕', ' ']), btn), "style": 2, "key": format!("ap:{id}:over")})]
        } else {
            card["buttons"].as_array().cloned().unwrap_or_default().iter()
                .map(|b| json!({"text": py::head(b[0].as_str().unwrap_or(""), btn), "style": if b[2] == "primary" { 1 } else { 2 }, "key": format!("ap:{id}:{}", py::str_of(&b[1]))}))
                .collect()
        };
        let mut out = json!({"card_type": "button_interaction", "main_title": {"title": title, "desc": format!("#{id}")},
                             "sub_title_text": desc, "button_list": buttons, "task_id": task_id});
        if let Some(l) = card["link"].as_array().filter(|_| done.is_empty()) {
            out["card_action"] = json!({"type": 1, "url": l[1]});
        }
        out
    }
}

impl Default for WeCom {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Platform for WeCom {
    fn max_len(&self) -> usize {
        6000
    }

    async fn run(&self, ch: Arc<Chan>) -> anyhow::Result<()> {
        let mut backoff = 5.0f64;
        loop {
            let mut pause = 0.0;
            match self.session(&ch).await {
                Ok(true) => {
                    pause = 120.0;
                    ch.set_state(
                        "error",
                        Some(Later::plain(
                            "这个机器人在别处也连上了（比如另一台服务器上的 open-super-agent），同一个机器人只能一处用，2 分钟后重试",
                        )),
                    );
                }
                Ok(false) => ch.set_state("error", Some(Later::plain("长连接断了，正在重连"))),
                Err(e) => {
                    if let Some(w) = e.downcast_ref::<WecomError>() {
                        let err = if w.msg.is_empty() { w.code.clone() } else { json!(w.msg) };
                        ch.set_state(
                            "error",
                            Some(Later::new(
                                "企业微信拒绝了连接（{error}）。Secret 重置过或者机器人改成了回调模式的话，删掉这个连接重新添加",
                                &[("error", err)],
                            )),
                        );
                        pause = 300.0;
                    } else {
                        let n = if e.to_string() == "TimeoutError" { "TimeoutError" } else { "OSError" };
                        ch.set_state("error", Some(Later::new("连接企业微信失败（{error}），稍后自动重试", &[("error", json!(n))])));
                    }
                }
            }
            let wait = if pause > 0.0 { pause } else { backoff + rand::random::<f64>() * 5.0 };
            tokio::time::sleep(Duration::from_secs_f64(wait)).await;
            backoff = (backoff * 2.0).min(120.0);
        }
    }

    async fn download(&self, _ch: &Arc<Chan>, item: &Value) -> anyhow::Result<(Vec<u8>, String)> {
        let r = self.http.get(py::str_of(&item["url"])).send().await?.error_for_status()?;
        let disp = r.headers().get("content-disposition").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let raw = r.bytes().await?.to_vec();
        let data = match item["aeskey"].as_str().filter(|s| !s.is_empty()) {
            Some(k) => decrypt(&raw, k)?,
            None => raw,
        };
        Ok((data, filename(&disp)))
    }

    async fn send_text(&self, _ch: &Arc<Chan>, peer: &str, md: &str) -> anyhow::Result<()> {
        self.request("aibot_send_msg", json!({"chatid": peer, "chat_type": 1, "msgtype": "markdown", "markdown": {"content": md}}), None)
            .await?;
        Ok(())
    }

    async fn send_card(&self, ch: &Arc<Chan>, peer: &str, card: &Card) -> anyhow::Result<String> {
        let body = card["body"].as_str().unwrap_or("");
        if py::char_len(&fmt::plain(body)) > CARD_TEXT {
            self.send_text(ch, peer, &format!("**{}**\n\n{body}", py::str_of(&card["title"]))).await?;
        }
        let task_id = format!("osa{}_{}_{}", ch.id, py::str_of(&card["id"]), crate::util::token_hex(4));
        match self
            .request(
                "aibot_send_msg",
                json!({"chatid": peer, "chat_type": 1, "msgtype": "template_card", "template_card": Self::card(card, &task_id)}),
                None,
            )
            .await
        {
            Ok(_) => return Ok(task_id),
            Err(e) if e.downcast_ref::<WecomError>().is_some() => eprintln!("[聊天软件 {} 企业微信] 卡片发送失败，改发文字：{e}", ch.id),
            Err(e) => return Err(e),
        }
        let say = reply_hints(card).join(&t!("、"));
        let link = card["link"].as_array().map(|l| format!("\n\n[{}]({})", py::str_of(&l[0]), py::str_of(&l[1]))).unwrap_or_default();
        self.send_text(ch, peer, &format!("**{}**\n\n{body}{link}\n\n{}", py::str_of(&card["title"]), t!("回复{say}。", say = say)))
            .await?;
        Ok(String::new())
    }

    async fn edit_card(&self, ch: &Arc<Chan>, peer: &str, msg_id: &str, card: &Card) -> anyhow::Result<()> {
        let (rid, t0) = self.events.lock().remove(msg_id).unwrap_or_default();
        if !rid.is_empty()
            && py::now() - t0 < 4.5
            && self
                .request(
                    "aibot_respond_update_msg",
                    json!({"response_type": "update_template_card", "template_card": Self::card(card, msg_id)}),
                    Some(rid),
                )
                .await
                .is_ok()
        {
            return Ok(());
        }
        let what = fmt::title_line(card["body"].as_str().unwrap_or(""), 40);
        self.send_text(
            ch,
            peer,
            &format!("**{}**{}", py::str_of(&card["done"]), t!("（#{id} {what}）", id = card["id"].clone(), what = what)),
        )
        .await
    }
}
