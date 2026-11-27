//! Discord：Gateway 长连接收私信。对应 osa/channels/discord.py（协议说明见那里）。
//! OSA_DISCORD_API 可以换成假的服务器（测试用）。

use super::{Card, Chan, Platform, err_name, fmt};
use crate::i18n::Later;
use crate::py;
use async_trait::async_trait;
use futures::{SinkExt, StreamExt};
use parking_lot::Mutex;
use serde_json::{Value, json};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;

pub const INTENTS: u64 = 1 << 12;
const ORANGE: u32 = 0xF59E0B;
const GREY: u32 = 0x9CA3AF;

fn fatal(code: u16) -> Option<&'static str> {
    Some(match code {
        4004 => "token 失效了（被重置或机器人被删除），删掉这个连接重新添加",
        4010 => "分片参数不对",
        4011 => "需要分片",
        4012 => "Gateway 版本不对",
        4013 => "意图参数不对",
        4014 => "没有开通需要的意图",
        _ => return None,
    })
}

fn api() -> String {
    std::env::var("OSA_DISCORD_API")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "https://discord.com/api/v10".into())
        .trim_end_matches('/')
        .to_string()
}

#[derive(Debug)]
pub struct DiscordError {
    pub status: u16,
    pub msg: String,
}

impl std::fmt::Display for DiscordError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {}", self.status, self.msg)
    }
}

impl std::error::Error for DiscordError {}

async fn call(c: &reqwest::Client, token: &str, method: reqwest::Method, path: &str, body: Option<&Value>) -> anyhow::Result<Value> {
    for attempt in 0..3 {
        let mut rb = c.request(method.clone(), format!("{}{path}", api())).header("Authorization", format!("Bot {token}"));
        if let Some(b) = body {
            rb = rb.json(b);
        }
        let r = rb.send().await?;
        let status = r.status().as_u16();
        let text = r.text().await.unwrap_or_default();
        if status == 429 && attempt < 2 {
            let wait =
                serde_json::from_str::<Value>(&text).ok().and_then(|d| d["retry_after"].as_f64()).filter(|w| *w > 0.0).unwrap_or(1.0);
            tokio::time::sleep(Duration::from_secs_f64(wait.min(30.0))).await;
            continue;
        }
        if status == 204 {
            return Ok(json!({}));
        }
        let d: Value = serde_json::from_str(&text).unwrap_or(json!({}));
        if status >= 400 {
            return Err(
                DiscordError { status, msg: d["message"].as_str().map(String::from).unwrap_or_else(|| py::head(&text, 200)) }.into()
            );
        }
        return Ok(d);
    }
    Ok(json!({}))
}

pub async fn check(fields: &Value) -> Result<Value, String> {
    let mut token = fields["token"].as_str().unwrap_or("").trim().to_string();
    if token.to_lowercase().starts_with("bot ") {
        token = token[4..].trim().to_string();
    }
    if token.chars().count() < 50 || token.matches('.').count() != 2 {
        return Err(t!("token 格式不对：在 Developer Portal 应用的 Bot 页面点 Reset Token 拿到（三段，用点隔开）"));
    }
    let c = super::http(20);
    let r = async {
        let me = call(&c, &token, reqwest::Method::GET, "/users/@me", None).await?;
        let app = call(&c, &token, reqwest::Method::GET, "/oauth2/applications/@me", None).await?;
        anyhow::Ok((me, app))
    }
    .await;
    let (me, app) = match r {
        Ok(x) => x,
        Err(e) => {
            if let Some(d) = e.downcast_ref::<DiscordError>() {
                return Err(if d.status == 401 {
                    t!("token 不对，或者已经被重置")
                } else {
                    t!("Discord 返回错误：{error}", error = d.msg.clone())
                });
            }
            let n = e.downcast_ref::<reqwest::Error>().map(err_name).unwrap_or("HTTPError");
            return Err(t!("连不上 Discord（{error}）。服务器在国内的话，要给 open-super-agent 设置 HTTPS_PROXY", error = n));
        }
    };
    let name = [&me["username"], &app["name"]].into_iter().find(|v| py::truthy(v)).map(py::str_of).unwrap_or_else(|| t!("Discord 机器人"));
    let app_id = if py::truthy(&app["id"]) { app["id"].clone() } else { me["id"].clone() };
    Ok(
        json!({"name": name, "config": {"bot_id": me["id"], "app_id": app_id, "username": me["username"].as_str().unwrap_or("")}, "secret": token}),
    )
}

#[derive(Clone)]
pub struct Discord {
    http: reqwest::Client,
    session: Arc<Mutex<(String, String, Value)>>,
    acked: Arc<AtomicBool>,
}

impl Discord {
    pub fn new() -> Discord {
        Discord {
            http: super::http(30),
            session: Arc::new(Mutex::new((String::new(), String::new(), Value::Null))),
            acked: Arc::new(AtomicBool::new(true)),
        }
    }

    async fn call(&self, ch: &Chan, method: reqwest::Method, path: &str, body: Option<&Value>) -> anyhow::Result<Value> {
        call(&self.http, &ch.secret, method, path, body).await
    }

    /// 一次连接；断开时返回关闭码（0 是没有）
    async fn session_loop(&self, ch: &Arc<Chan>, url: &str) -> anyhow::Result<u16> {
        let (ws, _) = tokio_tungstenite::connect_async(url).await?;
        let (sink, mut stream) = ws.split();
        let sink = Arc::new(tokio::sync::Mutex::new(sink));
        let hello = tokio::time::timeout(Duration::from_secs(30), stream.next()).await.map_err(|_| anyhow::anyhow!("TimeoutError"))?;
        let hello: Value = match hello {
            Some(Ok(m)) => serde_json::from_str(m.to_text().unwrap_or("{}"))?,
            _ => return Ok(0),
        };
        let interval = hello["d"]["heartbeat_interval"].as_f64().unwrap_or(41250.0) / 1000.0;
        self.acked.store(true, Ordering::Relaxed);
        let me = self.clone();
        let s2 = sink.clone();
        let beat = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_secs_f64(interval * rand::random::<f64>())).await;
            loop {
                if !me.acked.load(Ordering::Relaxed) {
                    let _ = s2.lock().await.send(Message::Close(Some(CloseFrame { code: CloseCode::from(4000), reason: "".into() }))).await;
                    return;
                }
                me.acked.store(false, Ordering::Relaxed);
                let seq = me.session.lock().2.clone();
                let _ = s2.lock().await.send(Message::Text(py::dumps(&json!({"op": 1, "d": seq}), true).into())).await;
                tokio::time::sleep(Duration::from_secs_f64(interval)).await;
            }
        });
        let (sid, _, seq) = self.session.lock().clone();
        let first = if !sid.is_empty() {
            json!({"op": 6, "d": {"token": ch.secret, "session_id": sid, "seq": seq}})
        } else {
            json!({"op": 2, "d": {"token": ch.secret, "intents": INTENTS, "properties": {"os": "linux", "browser": "open-super-agent", "device": "open-super-agent"}}})
        };
        sink.lock().await.send(Message::Text(py::dumps(&first, true).into())).await?;
        let mut code = 0u16;
        while let Some(m) = stream.next().await {
            let raw = match m {
                Ok(Message::Text(t)) => t.to_string(),
                Ok(Message::Close(f)) => {
                    code = f.map(|f| u16::from(f.code)).unwrap_or(0);
                    break;
                }
                Err(_) => break,
                _ => continue,
            };
            let p: Value = serde_json::from_str(&raw).unwrap_or(json!({}));
            if !p["s"].is_null() && p.get("s").is_some() {
                self.session.lock().2 = p["s"].clone();
            }
            match p["op"].as_i64().unwrap_or(-1) {
                0 => {
                    let d = p["d"].clone();
                    match p["t"].as_str().unwrap_or("") {
                        "READY" => {
                            let mut s = self.session.lock();
                            s.0 = py::str_of(&d["session_id"]);
                            s.1 = d["resume_gateway_url"].as_str().unwrap_or("").to_string();
                            drop(s);
                            ch.set_state("connected", None);
                        }
                        "RESUMED" => ch.set_state("connected", None),
                        "MESSAGE_CREATE" => {
                            let (me, c) = (self.clone(), ch.clone());
                            tokio::spawn(async move { me.on_message(&c, &d).await });
                        }
                        "INTERACTION_CREATE" => {
                            let (me, c) = (self.clone(), ch.clone());
                            tokio::spawn(async move {
                                if let Err(e) = me.on_interaction(&c, &d).await {
                                    eprintln!("[聊天软件] 处理消息出错：{e}");
                                }
                            });
                        }
                        _ => {}
                    }
                }
                1 => {
                    let seq = self.session.lock().2.clone();
                    let _ = sink.lock().await.send(Message::Text(py::dumps(&json!({"op": 1, "d": seq}), true).into())).await;
                }
                11 => self.acked.store(true, Ordering::Relaxed),
                7 => {
                    let _ = sink.lock().await.close().await;
                    break;
                }
                9 => {
                    if !py::truthy(&p["d"]) {
                        let mut s = self.session.lock();
                        s.0.clear();
                        s.2 = Value::Null;
                    }
                    tokio::time::sleep(Duration::from_secs_f64(1.0 + rand::random::<f64>() * 4.0)).await;
                    let _ = sink.lock().await.close().await;
                    break;
                }
                _ => {}
            }
        }
        beat.abort();
        Ok(code)
    }

    async fn on_message(&self, ch: &Arc<Chan>, m: &Value) {
        let author = &m["author"];
        if py::truthy(&m["guild_id"]) || py::truthy(&author["bot"]) || author["id"] == ch.config("bot_id") {
            return;
        }
        let peer = m["channel_id"].as_str().unwrap_or("").to_string();
        let who = [&author["global_name"], &author["username"]].into_iter().find(|v| py::truthy(v)).map(py::str_of).unwrap_or_default();
        let text = m["content"].as_str().unwrap_or("").to_string();
        let files: Vec<(String, Value)> = m["attachments"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter(|a| py::truthy(&a["url"]))
            .map(|a| {
                (
                    if py::truthy(&a["filename"]) { py::str_of(&a["filename"]) } else { format!("file_{}", py::str_of(&a["id"])) },
                    a["url"].clone(),
                )
            })
            .collect();
        if m["flags"].as_i64().unwrap_or(0) & 8192 != 0 {
            if peer == ch.peer() {
                ch.say(&t!("暂时听不了语音消息，请打字。"), None);
            }
            return;
        }
        // 引用回复的那条消息（引用提问卡片回复就是在回答它）
        let quoted: Vec<String> = m["message_reference"]["message_id"].as_str().map(String::from).into_iter().collect();
        ch.inbound(&peer, &who, &text, files, &quoted).await;
    }

    async fn on_interaction(&self, ch: &Arc<Chan>, d: &Value) -> anyhow::Result<()> {
        if d["type"] != 3 {
            return Ok(());
        }
        let peer = if py::truthy(&d["guild_id"]) {
            String::new()
        } else {
            [&d["channel_id"], &d["channel"]["id"]].into_iter().find(|v| py::truthy(v)).map(py::str_of).unwrap_or_default()
        };
        let data = d["data"]["custom_id"].as_str().unwrap_or("").to_string();
        let c = ch.clone();
        let mut job = tokio::spawn(async move { c.press(&peer, &data).await });
        let toast = match tokio::time::timeout(Duration::from_secs_f64(2.5), &mut job).await {
            Ok(Ok(t)) => t,
            _ => t!("收到，正在处理"),
        };
        self.call(
            ch,
            reqwest::Method::POST,
            &format!("/interactions/{}/{}/callback", py::str_of(&d["id"]), py::str_of(&d["token"])),
            Some(&json!({"type": 4, "data": {"content": py::head(&toast, 2000), "flags": 64}})),
        )
        .await?;
        Ok(())
    }

    fn card(card: &Card) -> Value {
        let mut body = fmt::discord_md(card["body"].as_str().unwrap_or(""));
        let done = card["done"].as_str().unwrap_or("");
        if !done.is_empty() {
            body += &format!("\n\n**{done}**");
        }
        let id = py::str_of(&card["id"]);
        let mut btns: Vec<Value> = card["buttons"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .map(|b| {
                let style = match b[2].as_str() {
                    Some("primary") => 1,
                    Some("danger") => 4,
                    _ => 2,
                };
                json!({"type": 2, "style": style, "label": b[0], "custom_id": format!("ap:{id}:{}", py::str_of(&b[1]))})
            })
            .collect();
        if let Some(l) = card["link"].as_array()
            && l[1].as_str().is_some_and(|u| u.starts_with("https://") || u.starts_with("http://"))
        {
            btns.push(json!({"type": 2, "style": 5, "label": l[0], "url": l[1]}));
        }
        let mut rows = vec![];
        if !btns.is_empty() {
            btns.truncate(5);
            rows.push(json!({"type": 1, "components": btns}));
        }
        json!({"embeds": [{"title": py::head(card["title"].as_str().unwrap_or(""), 256), "description": py::head(&body, 4096), "color": if done.is_empty() { ORANGE } else { GREY }}],
               "components": rows, "allowed_mentions": {"parse": []}})
    }
}

impl Default for Discord {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Platform for Discord {
    fn max_len(&self) -> usize {
        1900
    }

    async fn run(&self, ch: Arc<Chan>) -> anyhow::Result<()> {
        let mut backoff = 3.0f64;
        loop {
            let r = async {
                let (sid, resume, _) = self.session.lock().clone();
                let mut url = if sid.is_empty() {
                    self.session.lock().1.clear();
                    py::str_of(&self.call(&ch, reqwest::Method::GET, "/gateway/bot", None).await?["url"])
                } else {
                    resume
                };
                url += if url.contains('?') { "&" } else { "?" };
                url += "v=10&encoding=json";
                self.session_loop(&ch, &url).await
            }
            .await;
            match r {
                Ok(code) => {
                    if let Some(f) = fatal(code) {
                        ch.set_state("error", Some(Later::new("Discord 断开了连接：{error}", &[("error", json!(t!(f)))])));
                        return Ok(());
                    }
                    if code == 4007 || code == 4009 {
                        self.session.lock().0.clear();
                    }
                    backoff = 3.0;
                    ch.set_state("error", Some(Later::plain("长连接断了，正在重连")));
                }
                Err(e) => {
                    if let Some(d) = e.downcast_ref::<DiscordError>() {
                        if d.status == 401 {
                            ch.set_state("error", Some(Later::plain(fatal(4004).unwrap_or(""))));
                            return Ok(());
                        }
                        ch.set_state("error", Some(Later::new("Discord 返回错误：{error}，稍后自动重试", &[("error", json!(d.msg))])));
                    } else {
                        let n = e.downcast_ref::<reqwest::Error>().map(err_name).unwrap_or("OSError");
                        ch.set_state(
                            "error",
                            Some(Later::new(
                                "连不上 Discord（{error}），稍后自动重试。服务器在国内的话，要设置 HTTPS_PROXY",
                                &[("error", json!(n))],
                            )),
                        );
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs_f64(backoff + rand::random::<f64>() * 2.0)).await;
            backoff = (backoff * 2.0).min(60.0);
        }
    }

    async fn download(&self, _ch: &Arc<Chan>, url: &Value) -> anyhow::Result<(Vec<u8>, String)> {
        let r = self.http.get(py::str_of(url)).timeout(Duration::from_secs(120)).send().await?.error_for_status()?;
        Ok((r.bytes().await?.to_vec(), String::new()))
    }

    async fn send_text(&self, ch: &Arc<Chan>, peer: &str, md: &str) -> anyhow::Result<()> {
        self.call(
            ch,
            reqwest::Method::POST,
            &format!("/channels/{peer}/messages"),
            Some(&json!({"content": py::head(&fmt::discord_md(md), 2000), "allowed_mentions": {"parse": []}})),
        )
        .await?;
        Ok(())
    }

    async fn send_card(&self, ch: &Arc<Chan>, peer: &str, card: &Card) -> anyhow::Result<String> {
        Ok(self.call(ch, reqwest::Method::POST, &format!("/channels/{peer}/messages"), Some(&Self::card(card))).await?["id"]
            .as_str()
            .unwrap_or("")
            .to_string())
    }

    async fn edit_card(&self, ch: &Arc<Chan>, peer: &str, msg_id: &str, card: &Card) -> anyhow::Result<()> {
        if !msg_id.is_empty() {
            self.call(ch, reqwest::Method::PATCH, &format!("/channels/{peer}/messages/{msg_id}"), Some(&Self::card(card))).await?;
        }
        Ok(())
    }

    async fn typing(&self, ch: &Arc<Chan>, peer: &str) -> anyhow::Result<()> {
        self.call(ch, reqwest::Method::POST, &format!("/channels/{peer}/typing"), None).await?;
        Ok(())
    }
}
