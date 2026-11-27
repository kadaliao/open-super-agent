//! 钉钉：企业内部应用的机器人 + Stream 模式收消息。对应 osa/channels/dingtalk.py（协议说明见那里）。
//! 确认请求是一条 markdown，“按钮”是 dtmd:// 链接，点一下就替用户发出“允许一次 #12”。OSA_DINGTALK_API 可以换成假的服务器。

use super::{Card, Chan, Platform, err_name, fmt, reply_hints, word_command};
use crate::i18n::Later;
use crate::py;
use async_trait::async_trait;
use futures::{SinkExt, StreamExt};
use parking_lot::Mutex;
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::Duration;
use tokio_tungstenite::tungstenite::Message;

pub const TOPIC_MSG: &str = "/v1.0/im/bot/messages/get";

fn api() -> String {
    std::env::var("OSA_DINGTALK_API")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "https://api.dingtalk.com".into())
        .trim_end_matches('/')
        .to_string()
}

#[derive(Debug)]
pub struct DingError {
    pub status: u16,
    pub code: String,
    pub msg: String,
}

impl std::fmt::Display for DingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {} {}", self.status, self.code, self.msg)
    }
}

impl std::error::Error for DingError {}

async fn post(c: &reqwest::Client, path: &str, body: &Value, token: &str) -> anyhow::Result<Value> {
    let mut rb = c.post(format!("{}{path}", api())).json(body);
    if !token.is_empty() {
        rb = rb.header("x-acs-dingtalk-access-token", token);
    }
    let r = rb.send().await?;
    let status = r.status().as_u16();
    let text = r.text().await?;
    let d: Value = serde_json::from_str(&text).unwrap_or(json!({}));
    if status >= 400 {
        return Err(DingError {
            status,
            code: d["code"].as_str().unwrap_or("").to_string(),
            msg: d["message"].as_str().map(String::from).unwrap_or_else(|| py::head(&text, 200)),
        }
        .into());
    }
    Ok(d)
}

async fn token(c: &reqwest::Client, app_key: &str, secret: &str) -> anyhow::Result<(String, u64)> {
    let d = post(c, "/v1.0/oauth2/accessToken", &json!({"appKey": app_key, "appSecret": secret}), "").await?;
    let t = d["accessToken"].as_str().ok_or_else(|| anyhow::anyhow!("KeyError"))?.to_string();
    Ok((t, d["expireIn"].as_u64().unwrap_or(7200)))
}

fn quote(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn link(label: &str, text: &str) -> String {
    format!("[{label}](dtmd://dingtalkclient/sendMessage?content={})", quote(text))
}

pub async fn check(fields: &Value) -> Result<Value, String> {
    let app_key = fields["app_key"].as_str().unwrap_or("").trim().to_string();
    let secret = fields["app_secret"].as_str().unwrap_or("").trim().to_string();
    if app_key.is_empty() || secret.is_empty() {
        return Err(t!("请填 Client ID（AppKey）和 Client Secret（AppSecret），在开发者后台应用的“凭证与基础信息”里"));
    }
    let c = super::http(20);
    if let Err(e) = token(&c, &app_key, &secret).await {
        if let Some(d) = e.downcast_ref::<DingError>() {
            return Err(t!(
                "Client ID 或 Client Secret 不对（{error}）",
                error = if d.msg.is_empty() { d.code.clone() } else { d.msg.clone() }
            ));
        }
        let n = e.downcast_ref::<reqwest::Error>().map(err_name).unwrap_or("KeyError");
        return Err(t!("连不上钉钉（{error}）", error = n));
    }
    let name = py::head(fields["name"].as_str().unwrap_or("").trim(), 40);
    Ok(json!({"name": if name.is_empty() { app_key.clone() } else { name }, "config": {"app_key": app_key}, "secret": secret}))
}

#[derive(Clone)]
pub struct DingTalk {
    http: reqwest::Client,
    app_key: String,
    token: Arc<Mutex<(String, f64)>>,
    seen: Arc<Mutex<VecDeque<String>>>,
    webhooks: Arc<Mutex<HashMap<String, (String, f64)>>>,
    robot_code: Arc<Mutex<String>>,
}

impl DingTalk {
    pub fn new(r: &Value) -> DingTalk {
        let k = py::str_of(&r["config"]["app_key"]);
        DingTalk {
            http: super::http(30),
            robot_code: Arc::new(Mutex::new(k.clone())),
            app_key: k,
            token: Arc::new(Mutex::new((String::new(), 0.0))),
            seen: Arc::new(Mutex::new(VecDeque::new())),
            webhooks: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    async fn token(&self, ch: &Chan) -> anyhow::Result<String> {
        let (t, exp) = self.token.lock().clone();
        if !t.is_empty() && exp >= py::now() + 300.0 {
            return Ok(t);
        }
        let (t, ttl) = token(&self.http, &self.app_key, &ch.secret).await?;
        *self.token.lock() = (t.clone(), py::now() + ttl as f64);
        Ok(t)
    }

    async fn api(&self, ch: &Chan, path: &str, body: &Value) -> anyhow::Result<Value> {
        match post(&self.http, path, body, &self.token(ch).await?).await {
            Err(e) if e.downcast_ref::<DingError>().is_some_and(|d| d.status == 401) => {
                self.token.lock().0.clear();
                post(&self.http, path, body, &self.token(ch).await?).await
            }
            r => r,
        }
    }

    async fn session(&self, ch: &Arc<Chan>) -> anyhow::Result<()> {
        let d = post(
            &self.http,
            "/v1.0/gateway/connections/open",
            &json!({
            "clientId": self.app_key, "clientSecret": ch.secret,
            "subscriptions": [{"type": "CALLBACK", "topic": TOPIC_MSG}], "ua": "open-super-agent", "localIp": ""}),
            "",
        )
        .await?;
        let endpoint = d["endpoint"].as_str().ok_or_else(|| anyhow::anyhow!("KeyError"))?;
        let ticket: String = url::form_urlencoded::byte_serialize(d["ticket"].as_str().unwrap_or("").as_bytes()).collect();
        let (ws, _) = tokio_tungstenite::connect_async(format!("{endpoint}?ticket={ticket}")).await?;
        ch.set_state("connected", None);
        let (sink, mut stream) = ws.split();
        let sink = Arc::new(tokio::sync::Mutex::new(sink));
        while let Some(m) = stream.next().await {
            let raw = match m {
                Ok(Message::Text(t)) => t.to_string(),
                Ok(Message::Binary(b)) => String::from_utf8_lossy(&b).into_owned(),
                Ok(Message::Close(_)) | Err(_) => break,
                _ => continue,
            };
            let me = ch.clone();
            let sk = sink.clone();
            let this = self.clone();
            tokio::spawn(async move {
                if let Err(e) = this.frame(&me, &sk, &raw).await {
                    eprintln!("[聊天软件] 处理消息出错：{e}");
                }
            });
        }
        Ok(())
    }
}

impl DingTalk {
    async fn frame(
        &self,
        ch: &Arc<Chan>,
        sink: &Arc<tokio::sync::Mutex<impl futures::Sink<Message> + Unpin>>,
        raw: &str,
    ) -> anyhow::Result<()> {
        let f: Value = serde_json::from_str(raw)?;
        let h = &f["headers"];
        let mut ack = json!({"code": 200, "headers": {"contentType": "application/json", "messageId": h["messageId"].as_str().unwrap_or("")},
                             "message": "OK", "data": f["data"].as_str().unwrap_or("")});
        if f["type"] == "SYSTEM" {
            if h["topic"] == "ping" {
                let _ = sink.lock().await.send(Message::Text(py::dumps(&ack, true).into())).await;
            }
            return Ok(());
        }
        if f["type"] != "CALLBACK" {
            return Ok(());
        }
        ack["data"] = json!(py::dumps(&json!({"response": null}), true));
        let _ = sink.lock().await.send(Message::Text(py::dumps(&ack, true).into())).await;
        if h["topic"] == TOPIC_MSG {
            let m: Value = serde_json::from_str(f["data"].as_str().filter(|s| !s.is_empty()).unwrap_or("{}"))?;
            self.on_message(ch, &m).await;
        }
        Ok(())
    }

    async fn on_message(&self, ch: &Arc<Chan>, m: &Value) {
        if let Some(id) = m["msgId"].as_str().filter(|s| !s.is_empty()) {
            let mut seen = self.seen.lock();
            if seen.iter().any(|x| x == id) {
                return;
            }
            seen.push_back(id.to_string());
            if seen.len() > 500 {
                seen.pop_front();
            }
        }
        if py::str_of(&m["conversationType"]) != "1" {
            return;
        }
        let peer = [&m["senderStaffId"], &m["senderId"]].into_iter().find(|v| py::truthy(v)).map(py::str_of).unwrap_or_default();
        if !peer.is_empty() && py::truthy(&m["sessionWebhook"]) {
            let mut w = self.webhooks.lock();
            if w.len() > 50 {
                w.clear();
            }
            w.insert(peer.clone(), (py::str_of(&m["sessionWebhook"]), m["sessionWebhookExpiredTime"].as_f64().unwrap_or(0.0) / 1000.0));
        }
        if let Some(rc) = m["robotCode"].as_str().filter(|s| !s.is_empty()) {
            *self.robot_code.lock() = rc.to_string();
        }
        let content = &m["content"];
        let n = py::tail(
            &m["msgId"].as_str().filter(|s| !s.is_empty()).map(String::from).unwrap_or_else(|| (py::now() as i64).to_string()),
            10,
        );
        let (mut text, mut files): (String, Vec<(String, Value)>) = (String::new(), vec![]);
        let code_of =
            |v: &Value| [&v["downloadCode"], &v["pictureDownloadCode"]].into_iter().find(|x| py::truthy(x)).cloned().unwrap_or(Value::Null);
        match m["msgtype"].as_str().unwrap_or("") {
            "text" => text = m["text"]["content"].as_str().unwrap_or("").to_string(),
            "richText" => {
                for (i, el) in content["richText"].as_array().cloned().unwrap_or_default().iter().enumerate() {
                    if py::truthy(&el["text"]) {
                        text += &py::str_of(&el["text"]);
                    } else if py::truthy(&code_of(el)) {
                        files.push((format!("image_{n}_{i}.png"), code_of(el)));
                    }
                }
            }
            "picture" => files.push((format!("image_{n}.png"), code_of(content))),
            "file" => files.push((
                content["fileName"].as_str().filter(|s| !s.is_empty()).map(String::from).unwrap_or_else(|| format!("file_{n}")),
                content["downloadCode"].clone(),
            )),
            "video" => files.push((format!("video_{n}.mp4"), content["downloadCode"].clone())),
            "audio" => {
                text = content["recognition"].as_str().unwrap_or("").to_string();
                if text.is_empty() {
                    if peer == ch.peer() {
                        ch.say(&t!("没听清这条语音，请再说一遍或者打字。"), None);
                    }
                    return;
                }
            }
            _ => {
                if peer == ch.peer() {
                    ch.say(&t!("暂时只能收文字、语音、图片和文件。"), None);
                }
                return;
            }
        }
        let files: Vec<(String, Value)> = files.into_iter().filter(|f| py::truthy(&f.1)).collect();
        ch.inbound(&peer, m["senderNick"].as_str().unwrap_or(""), &text, files, &[]).await;
    }

    async fn markdown(&self, ch: &Chan, peer: &str, title: &str, text: &str) -> anyhow::Result<()> {
        let (url, exp) = self.webhooks.lock().get(peer).cloned().unwrap_or_default();
        if !url.is_empty() && exp > py::now() + 30.0 {
            if let Ok(r) =
                self.http.post(&url).json(&json!({"msgtype": "markdown", "markdown": {"title": title, "text": text}})).send().await
                && r.status().as_u16() == 200
                && r.json::<Value>().await.is_ok_and(|d| d["errcode"].as_i64().unwrap_or(0) == 0)
            {
                return Ok(());
            }
            self.webhooks.lock().remove(peer);
        }
        let robot = self.robot_code.lock().clone();
        let d = match self
            .api(
                ch,
                "/v1.0/robot/oToMessages/batchSend",
                &json!({"robotCode": robot, "userIds": [peer], "msgKey": "sampleMarkdown",
                "msgParam": py::dumps_u(&json!({"title": title, "text": text}))}),
            )
            .await
        {
            Ok(d) => d,
            Err(e) => {
                if let Some(de) = e.downcast_ref::<DingError>()
                    && (de.status == 403
                        || format!("{}{}", de.code, de.msg).to_lowercase().contains("permission")
                        || de.msg.contains("权限"))
                {
                    ch.set_state(
                        "error",
                        Some(Later::plain("发不出消息：应用还没开通“企业内机器人发送消息”权限（开发者后台 → 权限管理），开通后重新发布")),
                    );
                }
                return Err(e);
            }
        };
        if py::truthy(&d["invalidStaffIdList"]) || py::truthy(&d["flowControlledStaffIdList"]) {
            return Err(DingError { status: 200, code: String::new(), msg: format!("钉钉没发出去：{d}") }.into());
        }
        Ok(())
    }
}

#[async_trait]
impl Platform for DingTalk {
    fn max_len(&self) -> usize {
        3000
    }

    async fn run(&self, ch: Arc<Chan>) -> anyhow::Result<()> {
        let mut backoff = 5.0f64;
        loop {
            match self.session(&ch).await {
                Ok(()) => ch.set_state("error", Some(Later::plain("长连接断了，正在重连"))),
                Err(e) => {
                    if let Some(d) = e.downcast_ref::<DingError>() {
                        let err = if d.msg.is_empty() { json!(d.code) } else { json!(d.msg) };
                        if [400, 401, 403].contains(&d.status) {
                            ch.set_state("error", Some(Later::new("钉钉拒绝了连接（{error}）。确认应用开了机器人能力、消息接收模式选的是 Stream 模式；Client Secret 重置过的话，删掉这个连接重新添加", &[("error", err)])));
                        } else {
                            let err = if d.msg.is_empty() { json!(d.status) } else { json!(d.msg) };
                            ch.set_state("error", Some(Later::new("钉钉返回错误（{error}），稍后自动重试", &[("error", err)])));
                        }
                    } else {
                        let n = e.downcast_ref::<reqwest::Error>().map(err_name).unwrap_or("OSError");
                        ch.set_state("error", Some(Later::new("连接钉钉失败（{error}），稍后自动重试", &[("error", json!(n))])));
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs_f64(backoff + rand::random::<f64>() * 5.0)).await;
            backoff = (backoff * 2.0).min(120.0);
        }
    }

    async fn download(&self, ch: &Arc<Chan>, fref: &Value) -> anyhow::Result<(Vec<u8>, String)> {
        let robot = self.robot_code.lock().clone();
        let d = match self.api(ch, "/v1.0/robot/messageFiles/download", &json!({"downloadCode": fref, "robotCode": robot})).await {
            Ok(d) => d,
            Err(e) => {
                let msg = e
                    .downcast_ref::<DingError>()
                    .map(|d| if d.msg.is_empty() { d.code.clone() } else { d.msg.clone() })
                    .unwrap_or_else(|| e.to_string());
                anyhow::bail!(t!("钉钉不让下载（{error}）", error = msg));
            }
        };
        let r = self.http.get(py::str_of(&d["downloadUrl"])).timeout(Duration::from_secs(120)).send().await?.error_for_status()?;
        Ok((r.bytes().await?.to_vec(), String::new()))
    }

    async fn send_text(&self, ch: &Arc<Chan>, peer: &str, md: &str) -> anyhow::Result<()> {
        self.markdown(ch, peer, &fmt::title_line(md, 30), &fmt::dingtalk_md(md)).await
    }

    async fn send_card(&self, ch: &Arc<Chan>, peer: &str, card: &Card) -> anyhow::Result<String> {
        let btns = card["buttons"].as_array().cloned().unwrap_or_default();
        let mut links: Vec<String> =
            btns.iter().map(|b| link(b[0].as_str().unwrap_or(""), &word_command(card, b[1].as_str().unwrap_or("")))).collect();
        if let Some(l) = card["link"].as_array() {
            links.push(format!("[{}]({})", py::str_of(&l[0]), py::str_of(&l[1])));
        }
        // 提问卡片：“回答 #12 你的回答”“不回答 #12”；说不准那一步：三种回复都写出来；别的写前两个
        let says = match card["kind"].as_str() {
            Some("question" | "doubt") => reply_hints(card),
            _ => reply_hints(card).into_iter().take(2).collect(),
        };
        let title = py::str_of(&card["title"]);
        let text = format!(
            "### {title}\n\n{}\n\n{}\n\n{}",
            py::str_of(&card["body"]),
            links.join(" ｜ "),
            t!("点上面的链接，或者直接回复{say}。", say = says.join(&t!("、")))
        );
        self.markdown(ch, peer, &title, &fmt::dingtalk_md(&text)).await?;
        Ok(String::new())
    }

    async fn edit_card(&self, ch: &Arc<Chan>, peer: &str, _msg_id: &str, card: &Card) -> anyhow::Result<()> {
        let what = fmt::title_line(card["body"].as_str().unwrap_or(""), 40);
        let done = py::str_of(&card["done"]);
        self.markdown(ch, peer, &done, &format!("**{done}**{}", t!("（#{id} {what}）", id = card["id"].clone(), what = what))).await
    }
}
