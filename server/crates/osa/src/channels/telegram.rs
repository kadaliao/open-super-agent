//! Telegram：Bot API 的长轮询（getUpdates）收消息。对应 osa/channels/telegram.py（说明见那里）。
//! OSA_TELEGRAM_API 可以换成假的服务器（测试用）。

use super::{Card, Chan, Platform, err_name, fmt};
use crate::i18n::Later;
use crate::py;
use async_trait::async_trait;
use regex::Regex;
use serde_json::{Map, Value, json};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const MAX_FILE: i64 = 20 << 20;
const COMMANDS: [(&str, &str); 3] = [("new", "开始新话题"), ("stop", "停下正在做的任务"), ("help", "怎么用")];

fn api() -> String {
    std::env::var("OSA_TELEGRAM_API")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "https://api.telegram.org".into())
        .trim_end_matches('/')
        .to_string()
}

#[derive(Debug)]
pub enum TgErr {
    Tg { code: i64, desc: String, retry: u64 },
    Http(&'static str),
}

impl std::fmt::Display for TgErr {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TgErr::Tg { code, desc, .. } => write!(f, "{code} {desc}"),
            TgErr::Http(n) => f.write_str(n),
        }
    }
}

impl std::error::Error for TgErr {}

async fn call(c: &reqwest::Client, token: &str, method: &str, timeout: u64, params: Value) -> Result<Value, TgErr> {
    let body: Map<String, Value> = params.as_object().cloned().unwrap_or_default().into_iter().filter(|(_, v)| !v.is_null()).collect();
    let r = c
        .post(format!("{}/bot{token}/{method}", api()))
        .json(&body)
        .timeout(Duration::from_secs(timeout))
        .send()
        .await
        .map_err(|e| TgErr::Http(err_name(&e)))?;
    let status = r.status().as_u16() as i64;
    let text = r.text().await.map_err(|e| TgErr::Http(err_name(&e)))?;
    let Ok(d) = serde_json::from_str::<Value>(&text) else {
        return Err(TgErr::Tg { code: status, desc: py::head(&text, 200), retry: 0 });
    };
    if d["ok"] != true {
        return Err(TgErr::Tg {
            code: d["error_code"].as_i64().unwrap_or(status),
            desc: d["description"].as_str().unwrap_or("").to_string(),
            retry: d["parameters"]["retry_after"].as_u64().unwrap_or(0),
        });
    }
    Ok(d["result"].clone())
}

pub async fn check(fields: &Value) -> Result<Value, String> {
    static TOKEN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+:[\w-]{20,}$").unwrap());
    let token = fields["token"].as_str().unwrap_or("").trim().to_string();
    if !TOKEN.is_match(&token) {
        return Err(t!("token 格式不对：应该像 123456789:AAH…，在 Telegram 的 @BotFather 里拿"));
    }
    let c = super::http(20);
    let me = match call(&c, &token, "getMe", 30, json!({})).await {
        Ok(m) => m,
        Err(TgErr::Tg { code, desc, .. }) => {
            return Err(if code == 401 || code == 404 {
                t!("token 不对，或者这个机器人已经被删除")
            } else {
                t!("Telegram 返回错误：{error}", error = desc)
            });
        }
        Err(TgErr::Http(n)) => {
            return Err(t!("连不上 Telegram（{error}）。服务器在国内的话，要给 open-super-agent 设置 HTTPS_PROXY", error = n));
        }
    };
    let user = py::str_of(&me["username"]);
    Ok(json!({"name": format!("@{user}"), "config": {"username": user, "bot_id": me["id"]}, "secret": token}))
}

pub struct Telegram {
    http: reqwest::Client,
}

impl Telegram {
    pub fn new() -> Telegram {
        Telegram { http: super::http(30) }
    }

    async fn call(&self, ch: &Chan, method: &str, timeout: u64, params: Value) -> Result<Value, TgErr> {
        call(&self.http, &ch.secret, method, timeout, params).await
    }

    async fn on_update(&self, ch: &Arc<Chan>, u: &Value) -> anyhow::Result<()> {
        if let Some(cq) = u.get("callback_query").filter(|v| v.is_object()) {
            let peer = py::str_of(&cq["message"]["chat"]["id"]);
            let peer = if cq["message"]["chat"]["id"].is_null() { String::new() } else { peer };
            let toast = ch.press(&peer, cq["data"].as_str().unwrap_or("")).await;
            self.call(ch, "answerCallbackQuery", 30, json!({"callback_query_id": cq["id"], "text": py::head(&toast, 190)})).await?;
            return Ok(());
        }
        let Some(m) = u.get("message").filter(|v| v.is_object()) else { return Ok(()) };
        if m["chat"]["type"] != "private" {
            return Ok(());
        }
        let peer = py::str_of(&m["chat"]["id"]);
        let f = &m["from"];
        let who: Vec<String> = [&f["first_name"], &f["last_name"]].iter().filter(|x| py::truthy(x)).map(|x| py::str_of(x)).collect();
        let who = if who.is_empty() { f["username"].as_str().unwrap_or("").to_string() } else { who.join(" ") };
        let text = [&m["text"], &m["caption"]].into_iter().find(|v| py::truthy(v)).map(py::str_of).unwrap_or_default();
        let n = py::str_of(&m["message_id"]);
        let mut files: Vec<(String, Value)> = vec![];
        if let Some(d) = m.get("document").filter(|v| py::truthy(v)) {
            let name = if py::truthy(&d["file_name"]) { py::str_of(&d["file_name"]) } else { format!("file_{n}") };
            files.push((name, d["file_id"].clone()));
        }
        if let Some(p) = m["photo"].as_array().filter(|a| !a.is_empty()) {
            files.push((format!("photo_{n}.jpg"), p[p.len() - 1]["file_id"].clone()));
        }
        for (k, ext) in [("video", ".mp4"), ("audio", ".mp3")] {
            if let Some(v) = m.get(k).filter(|v| py::truthy(v)) {
                let name = if py::truthy(&v["file_name"]) { py::str_of(&v["file_name"]) } else { format!("{k}_{n}{ext}") };
                files.push((name, v["file_id"].clone()));
            }
        }
        if (py::truthy(&m["voice"]) || py::truthy(&m["video_note"])) && text.is_empty() && files.is_empty() {
            if ch.peer() == peer {
                ch.say(&t!("暂时听不了语音消息，请打字（可以用输入法的语音转文字）。"), None);
            }
            return Ok(());
        }
        // 引用回复的那条消息（引用提问卡片回复就是在回答它）
        let quoted: Vec<String> = Some(&m["reply_to_message"]["message_id"]).filter(|v| !v.is_null()).map(py::str_of).into_iter().collect();
        ch.inbound(&peer, &who, &text, files, &quoted).await;
        Ok(())
    }

    fn card(card: &Card) -> (String, Value) {
        let mut text = format!(
            "<b>{}</b>\n\n{}",
            fmt::escape(card["title"].as_str().unwrap_or(""), true),
            fmt::telegram_html(card["body"].as_str().unwrap_or(""))
        );
        if let Some(done) = card["done"].as_str().filter(|s| !s.is_empty()) {
            text += &format!("\n\n<b>{}</b>", fmt::escape(done, true));
        }
        let mut rows = vec![];
        let id = py::str_of(&card["id"]);
        let btns: Vec<Value> = card["buttons"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .map(|b| json!({"text": b[0], "callback_data": format!("ap:{id}:{}", py::str_of(&b[1]))}))
            .collect();
        // 提问的选项、说不准那一步的三个选择字多：一个按钮一行
        if matches!(card["kind"].as_str(), Some("question" | "doubt")) {
            rows.extend(btns.into_iter().map(|b| json!([b])));
        } else if !btns.is_empty() {
            rows.push(json!(btns));
        }
        if let Some(l) = card["link"].as_array()
            && l.get(1).and_then(|u| u.as_str()).is_some_and(|u| u.starts_with("https://"))
        {
            rows.push(json!([{"text": l[0], "url": l[1]}]));
        }
        (text, json!({"inline_keyboard": rows}))
    }
}

impl Default for Telegram {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Platform for Telegram {
    fn max_len(&self) -> usize {
        3800
    }

    async fn run(&self, ch: Arc<Chan>) -> anyhow::Result<()> {
        let mut offset: Value = Value::Null;
        let mut ready = false;
        let mut backoff = 3u64;
        loop {
            let r: Result<(), TgErr> = async {
                if !ready {
                    self.call(&ch, "deleteWebhook", 30, json!({})).await?;
                    let cmds: Vec<Value> = COMMANDS.iter().map(|(c, d)| json!({"command": c, "description": t!(d)})).collect();
                    self.call(&ch, "setMyCommands", 30, json!({"commands": cmds})).await?;
                    ready = true;
                    ch.set_state("connected", None);
                }
                let ups = self
                    .call(&ch, "getUpdates", 70, json!({"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]}))
                    .await?;
                ch.set_state("connected", None);
                backoff = 3;
                for u in ups.as_array().cloned().unwrap_or_default() {
                    offset = json!(u["update_id"].as_i64().unwrap_or(0) + 1);
                    let me = ch.clone();
                    let tg = Telegram { http: self.http.clone() };
                    tokio::spawn(async move {
                        if let Err(e) = tg.on_update(&me, &u).await {
                            eprintln!("[聊天软件] 处理消息出错：{e}");
                        }
                    });
                }
                Ok(())
            }
            .await;
            match r {
                Ok(()) => {}
                Err(TgErr::Tg { code, desc, retry }) => {
                    if code == 401 || code == 404 {
                        ch.set_state("error", Some(Later::plain("token 失效了（机器人被删除或 token 被重置），删掉这个连接重新添加")));
                        return Ok(());
                    }
                    if code == 409 {
                        ch.set_state(
                            "error",
                            Some(Later::plain(
                                "这个机器人在别处也在收消息（比如另一台服务器上的 open-super-agent），同一个 token 只能一处用",
                            )),
                        );
                        tokio::time::sleep(Duration::from_secs(30)).await;
                        continue;
                    }
                    ch.set_state("error", Some(Later::new("Telegram 返回错误：{error}", &[("error", json!(desc))])));
                    tokio::time::sleep(Duration::from_secs(if retry > 0 { retry } else { backoff })).await;
                    backoff = (backoff * 2).min(60);
                }
                Err(TgErr::Http(n)) => {
                    ch.set_state("error", Some(Later::new("连不上 Telegram（{error}），稍后自动重试", &[("error", json!(n))])));
                    tokio::time::sleep(Duration::from_secs(backoff)).await;
                    backoff = (backoff * 2).min(60);
                }
            }
        }
    }

    async fn download(&self, ch: &Arc<Chan>, fref: &Value) -> anyhow::Result<(Vec<u8>, String)> {
        let f = self.call(ch, "getFile", 30, json!({"file_id": fref})).await?;
        if f["file_size"].as_i64().unwrap_or(0) > MAX_FILE {
            anyhow::bail!(t!("Telegram 机器人只能接收 20 MB 以内的文件"));
        }
        let r = self
            .http
            .get(format!("{}/file/bot{}/{}", api(), ch.secret, py::str_of(&f["file_path"])))
            .timeout(Duration::from_secs(120))
            .send()
            .await?
            .error_for_status()?;
        Ok((r.bytes().await?.to_vec(), String::new()))
    }

    async fn send_text(&self, ch: &Arc<Chan>, peer: &str, md: &str) -> anyhow::Result<()> {
        let r = self
            .call(ch, "sendMessage", 30, json!({"chat_id": peer, "text": fmt::telegram_html(md), "parse_mode": "HTML", "link_preview_options": {"is_disabled": true}}))
            .await;
        match r {
            Ok(_) => Ok(()),
            Err(TgErr::Tg { code: 400, .. }) => {
                self.call(
                    ch,
                    "sendMessage",
                    30,
                    json!({"chat_id": peer, "text": py::head(&fmt::plain(md), 4096), "link_preview_options": {"is_disabled": true}}),
                )
                .await?;
                Ok(())
            }
            Err(e) => Err(e.into()),
        }
    }

    async fn send_card(&self, ch: &Arc<Chan>, peer: &str, card: &Card) -> anyhow::Result<String> {
        let (text, kb) = Self::card(card);
        let r = match self
            .call(ch, "sendMessage", 30, json!({"chat_id": peer, "text": text, "parse_mode": "HTML", "reply_markup": kb}))
            .await
        {
            Ok(r) => r,
            Err(TgErr::Tg { code: 400, .. }) => {
                let first: Vec<Value> = kb["inline_keyboard"].as_array().cloned().unwrap_or_default().into_iter().take(1).collect();
                self.call(ch, "sendMessage", 30, json!({"chat_id": peer, "text": format!("{}\n\n{}", py::str_of(&card["title"]), fmt::plain(card["body"].as_str().unwrap_or(""))),
                    "reply_markup": {"inline_keyboard": first}})).await?
            }
            Err(e) => return Err(e.into()),
        };
        Ok(py::str_of(&r["message_id"]))
    }

    async fn edit_card(&self, ch: &Arc<Chan>, peer: &str, msg_id: &str, card: &Card) -> anyhow::Result<()> {
        let (text, kb) = Self::card(card);
        let mid: i64 = msg_id.parse().unwrap_or(0);
        match self
            .call(
                ch,
                "editMessageText",
                30,
                json!({"chat_id": peer, "message_id": mid, "text": text, "parse_mode": "HTML", "reply_markup": kb}),
            )
            .await
        {
            Ok(_) => Ok(()),
            Err(TgErr::Tg { desc, .. }) if desc.contains("not modified") => Ok(()),
            Err(TgErr::Tg { code: 400, .. }) => {
                self.call(
                    ch,
                    "editMessageText",
                    30,
                    json!({"chat_id": peer, "message_id": mid,
                    "text": format!("{}\n\n{}", py::str_of(&card["title"]), py::str_of(&card["done"])), "reply_markup": kb}),
                )
                .await?;
                Ok(())
            }
            Err(e) => Err(e.into()),
        }
    }

    async fn typing(&self, ch: &Arc<Chan>, peer: &str) -> anyhow::Result<()> {
        self.call(ch, "sendChatAction", 30, json!({"chat_id": peer, "action": "typing"})).await?;
        Ok(())
    }
}
