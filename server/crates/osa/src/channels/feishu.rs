//! 飞书（和海外版 Lark）：企业自建应用 + 长连接收事件。对应 osa/channels/feishu.py（协议说明见那里）。
//! OSA_FEISHU_API 可以换成假的服务器（测试用）。

use super::{Card, Chan, Platform, err_name, fmt};
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

fn domain_base(d: &str) -> &'static str {
    if d == "lark" { "https://open.larksuite.com" } else { "https://open.feishu.cn" }
}

fn applink(d: &str) -> &'static str {
    if d == "lark" { "https://applink.larksuite.com" } else { "https://applink.feishu.cn" }
}

pub fn base(domain: &str) -> String {
    match std::env::var("OSA_FEISHU_API").ok().filter(|s| !s.is_empty()) {
        Some(o) => o.trim_end_matches('/').to_string(),
        None => domain_base(domain).into(),
    }
}

#[derive(Debug)]
pub struct FeishuError {
    pub code: Value,
    pub msg: String,
}

impl std::fmt::Display for FeishuError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {}", py::str_of(&self.code), self.msg)
    }
}

impl std::error::Error for FeishuError {}

// ---------- pbbp2.Frame 的编解码 ----------
fn varint(mut n: u64, out: &mut Vec<u8>) {
    loop {
        let b = (n & 0x7f) as u8;
        n >>= 7;
        if n != 0 {
            out.push(b | 0x80);
        } else {
            out.push(b);
            return;
        }
    }
}

fn read_varint(b: &[u8], i: &mut usize) -> Option<u64> {
    let (mut n, mut shift) = (0u64, 0);
    loop {
        let x = *b.get(*i)?;
        *i += 1;
        n |= ((x & 0x7f) as u64) << shift;
        shift += 7;
        if x & 0x80 == 0 {
            return Some(n);
        }
        if shift > 63 {
            return None;
        }
    }
}

enum F<'a> {
    Num(u64),
    Bytes(&'a [u8]),
}

fn fields(b: &[u8]) -> Option<Vec<(u64, F<'_>)>> {
    let mut out = vec![];
    let mut i = 0;
    while i < b.len() {
        let key = read_varint(b, &mut i)?;
        let (num, wt) = (key >> 3, key & 7);
        match wt {
            0 => out.push((num, F::Num(read_varint(b, &mut i)?))),
            2 => {
                let n = read_varint(b, &mut i)? as usize;
                out.push((num, F::Bytes(b.get(i..i + n)?)));
                i += n;
            }
            1 => {
                out.push((num, F::Bytes(b.get(i..i + 8)?)));
                i += 8;
            }
            5 => {
                out.push((num, F::Bytes(b.get(i..i + 4)?)));
                i += 4;
            }
            _ => return None,
        }
    }
    Some(out)
}

#[derive(Default, Clone, Debug)]
pub struct Frame {
    pub seq: u64,
    pub log: u64,
    pub service: u64,
    pub method: u64,
    pub headers: Vec<(String, String)>,
    pub payload_encoding: String,
    pub payload_type: String,
    pub payload: Option<Vec<u8>>,
    pub log_new: String,
}

fn num_field(num: u64, v: u64, out: &mut Vec<u8>) {
    varint(num << 3, out);
    varint(v, out);
}

fn bytes_field(num: u64, v: &[u8], out: &mut Vec<u8>) {
    varint(num << 3 | 2, out);
    varint(v.len() as u64, out);
    out.extend_from_slice(v);
}

pub fn encode_frame(f: &Frame) -> Vec<u8> {
    let mut out = vec![];
    num_field(1, f.seq, &mut out);
    num_field(2, f.log, &mut out);
    num_field(3, f.service, &mut out);
    num_field(4, f.method, &mut out);
    for (k, v) in &f.headers {
        let mut h = vec![];
        bytes_field(1, k.as_bytes(), &mut h);
        bytes_field(2, v.as_bytes(), &mut h);
        bytes_field(5, &h, &mut out);
    }
    if !f.payload_encoding.is_empty() {
        bytes_field(6, f.payload_encoding.as_bytes(), &mut out);
    }
    if !f.payload_type.is_empty() {
        bytes_field(7, f.payload_type.as_bytes(), &mut out);
    }
    if let Some(p) = &f.payload {
        bytes_field(8, p, &mut out);
    }
    if !f.log_new.is_empty() {
        bytes_field(9, f.log_new.as_bytes(), &mut out);
    }
    out
}

pub fn decode_frame(b: &[u8]) -> Option<Frame> {
    let mut f = Frame::default();
    let s = |v: &[u8]| String::from_utf8_lossy(v).into_owned();
    for (num, v) in fields(b)? {
        match (num, v) {
            (1, F::Num(n)) => f.seq = n,
            (2, F::Num(n)) => f.log = n,
            (3, F::Num(n)) => f.service = n,
            (4, F::Num(n)) => f.method = n,
            (5, F::Bytes(h)) => {
                let (mut k, mut val) = (String::new(), String::new());
                for (hn, hv) in fields(h)? {
                    if let F::Bytes(x) = hv {
                        if hn == 1 {
                            k = s(x);
                        } else if hn == 2 {
                            val = s(x);
                        }
                    }
                }
                f.headers.push((k, val));
            }
            (6, F::Bytes(x)) => f.payload_encoding = s(x),
            (7, F::Bytes(x)) => f.payload_type = s(x),
            (8, F::Bytes(x)) => f.payload = Some(x.to_vec()),
            (9, F::Bytes(x)) => f.log_new = s(x),
            _ => {}
        }
    }
    Some(f)
}

// ---------- 接口 ----------
async fn tenant_token(c: &reqwest::Client, domain: &str, app_id: &str, secret: &str) -> anyhow::Result<(String, u64)> {
    let r = c
        .post(format!("{}/open-apis/auth/v3/tenant_access_token/internal", base(domain)))
        .json(&json!({"app_id": app_id, "app_secret": secret}))
        .send()
        .await?;
    let text = r.text().await?;
    let d: Value = serde_json::from_str(&text)?;
    if d["code"] != 0 {
        return Err(FeishuError {
            code: d["code"].clone(),
            msg: d["msg"].as_str().map(String::from).unwrap_or_else(|| py::head(&text, 200)),
        }
        .into());
    }
    Ok((py::str_of(&d["tenant_access_token"]), d["expire"].as_u64().unwrap_or(7200)))
}

/// 富文本消息（post）→ 纯文字 + 里面的图片
fn post_text(content: &Value) -> (String, Vec<String>) {
    let body = match content["content"].as_array() {
        Some(a) if !a.is_empty() => a.clone(),
        _ => content
            .as_object()
            .and_then(|o| o.values().find_map(|v| v["content"].as_array().filter(|a| !a.is_empty()).cloned()))
            .unwrap_or_default(),
    };
    let mut lines = vec![];
    let mut images = vec![];
    for para in body {
        let mut parts = String::new();
        for el in para.as_array().cloned().unwrap_or_default() {
            let s = |k: &str| el[k].as_str().unwrap_or("").to_string();
            match el["tag"].as_str().unwrap_or("") {
                "text" | "md" => parts += &s("text"),
                "a" => parts += &format!("{}（{}）", s("text"), s("href")),
                "code_block" => parts += &format!("```\n{}\n```", s("text")),
                "img" if !s("image_key").is_empty() => images.push(s("image_key")),
                _ => {}
            }
        }
        lines.push(parts);
    }
    let title = content["title"].as_str().unwrap_or("");
    let mut all = vec![];
    if !title.is_empty() {
        all.push(title.to_string());
    }
    all.extend(lines);
    (all.join("\n").trim().to_string(), images)
}

pub async fn check(fields: &Value) -> Result<Value, String> {
    let app_id = fields["app_id"].as_str().unwrap_or("").trim().to_string();
    let secret = fields["app_secret"].as_str().unwrap_or("").trim().to_string();
    let domain = match fields["domain"].as_str() {
        Some(d @ ("feishu" | "lark")) => d.to_string(),
        _ => "feishu".into(),
    };
    if !app_id.starts_with("cli_") {
        return Err(t!("App ID 应该以 cli_ 开头，在开发者后台应用的“凭证与基础信息”里"));
    }
    if secret.is_empty() {
        return Err(t!("请填 App Secret"));
    }
    let c = super::http(20);
    let unreachable = |n: &str| {
        if domain == "feishu" { t!("连不上飞书（{error}）", error = n) } else { t!("连不上 Lark（{error}）", error = n) }
    };
    let tok = match tenant_token(&c, &domain, &app_id, &secret).await {
        Ok((t, _)) => t,
        Err(e) => {
            if let Some(fe) = e.downcast_ref::<FeishuError>() {
                return Err(t!("App ID 或 App Secret 不对（{error}）", error = fe.msg.clone()));
            }
            let n = e.downcast_ref::<reqwest::Error>().map(err_name).unwrap_or("ValueError");
            return Err(unreachable(n));
        }
    };
    let d: Value = match c.get(format!("{}/open-apis/bot/v3/info", base(&domain))).bearer_auth(&tok).send().await {
        Ok(r) => r.json().await.map_err(|_| unreachable("ValueError"))?,
        Err(e) => return Err(unreachable(err_name(&e))),
    };
    if d["code"] != 0 {
        return Err(t!(
            "应用还没开启机器人能力：在开发者后台“添加应用能力”里添加“机器人”，再发布一个版本（{error}）",
            error = d["msg"].clone()
        ));
    }
    let bot = &d["bot"];
    let name = bot["app_name"].as_str().filter(|s| !s.is_empty()).map(String::from).unwrap_or_else(|| app_id.clone());
    Ok(
        json!({"name": name, "config": {"app_id": app_id, "domain": domain, "open_id": bot["open_id"].as_str().unwrap_or("")}, "secret": secret}),
    )
}

pub fn pair_link(r: &Value) -> String {
    let d = r["config"]["domain"].as_str().filter(|s| !s.is_empty()).unwrap_or("feishu");
    format!("{}/client/bot/open?appId={}", applink(d), py::str_of(&r["config"]["app_id"]))
}

type Ws = futures::stream::SplitSink<tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>, Message>;

pub struct Feishu {
    http: reqwest::Client,
    domain: String,
    app_id: String,
    token: Mutex<(String, f64)>,
    seen: Mutex<VecDeque<String>>,
    parts: Mutex<HashMap<String, Vec<Vec<u8>>>>,
    last_msg: Mutex<String>,
    reaction: Arc<Mutex<Option<(String, String)>>>,
}

impl Feishu {
    pub fn new(r: &Value) -> Feishu {
        Feishu {
            http: super::http(30),
            domain: r["config"]["domain"].as_str().filter(|s| !s.is_empty()).unwrap_or("feishu").to_string(),
            app_id: py::str_of(&r["config"]["app_id"]),
            token: Mutex::new((String::new(), 0.0)),
            seen: Mutex::new(VecDeque::new()),
            parts: Mutex::new(HashMap::new()),
            last_msg: Mutex::new(String::new()),
            reaction: Arc::new(Mutex::new(None)),
        }
    }

    async fn token(&self, ch: &Chan) -> anyhow::Result<String> {
        let (t, exp) = self.token.lock().clone();
        if !t.is_empty() && exp >= py::now() + 120.0 {
            return Ok(t);
        }
        let (t, ttl) = tenant_token(&self.http, &self.domain, &self.app_id, &ch.secret).await?;
        *self.token.lock() = (t.clone(), py::now() + ttl as f64);
        Ok(t)
    }

    async fn api(
        &self,
        ch: &Chan,
        method: reqwest::Method,
        path: &str,
        query: &[(&str, &str)],
        body: Option<Value>,
    ) -> anyhow::Result<Value> {
        for attempt in 0..2 {
            let tok = self.token(ch).await?;
            let mut rb = self.http.request(method.clone(), format!("{}{path}", base(&self.domain))).bearer_auth(tok).query(query);
            if let Some(b) = &body {
                rb = rb.json(b);
            }
            let text = rb.send().await?.text().await?;
            let d: Value = serde_json::from_str(&text)?;
            let code = d["code"].as_i64().unwrap_or(-1);
            if [99991661, 99991663, 99991668].contains(&code) && attempt == 0 {
                self.token.lock().0.clear();
                continue;
            }
            if code != 0 {
                return Err(FeishuError {
                    code: d["code"].clone(),
                    msg: d["msg"].as_str().map(String::from).unwrap_or_else(|| py::head(&text, 200)),
                }
                .into());
            }
            return Ok(d.get("data").cloned().filter(|v| !v.is_null()).unwrap_or(json!({})));
        }
        Ok(json!({}))
    }

    async fn endpoint(&self, ch: &Chan) -> anyhow::Result<(String, Value)> {
        let r = self
            .http
            .post(format!("{}/callback/ws/endpoint", base(&self.domain)))
            .header("locale", "zh")
            .json(&json!({"AppID": self.app_id, "AppSecret": ch.secret}))
            .send()
            .await?;
        let d: Value = r.json().await?;
        if d["code"] != 0 {
            return Err(FeishuError {
                code: d["code"].clone(),
                msg: d["msg"].as_str().map(String::from).unwrap_or_else(|| t!("拿不到长连接地址")),
            }
            .into());
        }
        Ok((py::str_of(&d["data"]["URL"]), d["data"].get("ClientConfig").cloned().filter(|v| v.is_object()).unwrap_or(json!({}))))
    }

    async fn session(&self, ch: &Arc<Chan>) -> anyhow::Result<()> {
        let (url, conf) = self.endpoint(ch).await?;
        let service: u64 = reqwest::Url::parse(&url)
            .ok()
            .and_then(|u| u.query_pairs().find(|(k, _)| k == "service_id").and_then(|(_, v)| v.parse().ok()))
            .unwrap_or(0);
        let (ws, _) = tokio_tungstenite::connect_async(&url).await?;
        ch.set_state("connected", None);
        let (sink, mut stream) = ws.split();
        let sink = Arc::new(tokio::sync::Mutex::new(sink));
        let s2 = sink.clone();
        let every = conf["PingInterval"].as_u64().filter(|n| *n > 0).unwrap_or(120);
        let pinger = tokio::spawn(async move {
            loop {
                let f = Frame { service, method: 0, headers: vec![("type".into(), "ping".into())], ..Default::default() };
                if s2.lock().await.send(Message::Binary(encode_frame(&f).into())).await.is_err() {
                    return;
                }
                tokio::time::sleep(Duration::from_secs(every)).await;
            }
        });
        while let Some(m) = stream.next().await {
            match m {
                Ok(Message::Binary(b)) => self.frame(ch, &sink, &b).await,
                Ok(Message::Close(_)) | Err(_) => break,
                _ => {}
            }
        }
        pinger.abort();
        Ok(())
    }

    async fn frame(&self, ch: &Arc<Chan>, sink: &Arc<tokio::sync::Mutex<Ws>>, raw: &[u8]) {
        let Some(mut f) = decode_frame(raw) else { return };
        let hs: HashMap<String, String> = f.headers.iter().cloned().collect();
        if f.method != 1 || hs.get("type").map(|s| s.as_str()) != Some("event") {
            return;
        }
        let mut payload = f.payload.clone().unwrap_or_default();
        let total: usize = hs.get("sum").and_then(|s| s.parse().ok()).unwrap_or(1);
        let seq: usize = hs.get("seq").and_then(|s| s.parse().ok()).unwrap_or(0);
        let mid = hs.get("message_id").cloned().unwrap_or_default();
        if total > 1 {
            let mut parts = self.parts.lock();
            let buf = parts.entry(mid.clone()).or_insert_with(|| vec![vec![]; total]);
            if seq < buf.len() {
                buf[seq] = payload.clone();
            }
            if buf.iter().any(|p| p.is_empty()) {
                return;
            }
            payload = parts.remove(&mid).unwrap_or_default().concat();
        }
        let t0 = std::time::Instant::now();
        let mut resp = json!({"code": 200});
        match serde_json::from_slice::<Value>(&payload) {
            Ok(p) => {
                if let Some(result) = self.event(ch, &p).await {
                    resp["data"] = json!(crate::util::b64(py::dumps_u(&result).as_bytes()));
                }
            }
            Err(e) => {
                eprintln!("[聊天软件 {} 飞书] 处理事件出错：{e}", ch.id);
                resp = json!({"code": 500});
            }
        }
        f.headers.push(("biz_rt".into(), t0.elapsed().as_millis().to_string()));
        f.payload = Some(py::dumps(&resp, true).into_bytes());
        let _ = sink.lock().await.send(Message::Binary(encode_frame(&f).into())).await;
    }

    async fn event(&self, ch: &Arc<Chan>, p: &Value) -> Option<Value> {
        let (h, ev) = (&p["header"], &p["event"]);
        if let Some(eid) = h["event_id"].as_str().filter(|s| !s.is_empty()) {
            let mut seen = self.seen.lock();
            if seen.iter().any(|x| x == eid) {
                return None;
            }
            seen.push_back(eid.to_string());
            if seen.len() > 500 {
                seen.pop_front();
            }
        }
        match h["event_type"].as_str().unwrap_or("") {
            "im.message.receive_v1" => {
                let (msg, sender) = (&ev["message"], &ev["sender"]);
                let st = &sender["sender_type"];
                if msg["chat_type"] != "p2p" || !(st.is_null() || st == "user") {
                    return None;
                }
                let peer = msg["chat_id"].as_str().unwrap_or("").to_string();
                let mid = msg["message_id"].as_str().unwrap_or("").to_string();
                let content: Value =
                    serde_json::from_str(msg["content"].as_str().filter(|s| !s.is_empty()).unwrap_or("{}")).unwrap_or(json!({}));
                let tail = |s: &str| py::tail(s, 10);
                let (mut text, mut files) = (String::new(), vec![]);
                match msg["message_type"].as_str().unwrap_or("") {
                    "text" => text = content["text"].as_str().unwrap_or("").to_string(),
                    "post" => {
                        let (t, images) = post_text(&content);
                        text = t;
                        files = images.iter().map(|k| (format!("image_{}.png", tail(k)), json!([mid, k, "image"]))).collect();
                    }
                    "image" => {
                        files =
                            vec![(format!("image_{}.png", tail(&mid)), json!([mid, content["image_key"].as_str().unwrap_or(""), "image"]))]
                    }
                    "file" | "media" => {
                        let name = content["file_name"]
                            .as_str()
                            .filter(|s| !s.is_empty())
                            .map(String::from)
                            .unwrap_or_else(|| format!("file_{}", tail(&mid)));
                        files = vec![(name, json!([mid, content["file_key"].as_str().unwrap_or(""), "file"]))];
                    }
                    _ => {
                        if peer == ch.peer() {
                            ch.say(&t!("暂时只能收文字、图片和文件。语音请用输入法的语音转文字。"), None);
                        }
                        return None;
                    }
                }
                if peer == ch.peer() {
                    *self.last_msg.lock() = mid;
                }
                // 引用回复的那条消息（parent_id；在话题里回复时还有话题的第一条 root_id）：引用提问卡片回复就是在回答它
                let quoted: Vec<String> =
                    ["parent_id", "root_id"].iter().filter_map(|k| msg[*k].as_str().filter(|s| !s.is_empty()).map(String::from)).collect();
                let me = ch.clone();
                tokio::spawn(async move { me.inbound(&peer, "", &text, files, &quoted).await });
                None
            }
            "card.action.trigger" => {
                let value = &ev["action"]["value"];
                let data = if value.is_object() { value["osa"].as_str().unwrap_or("").to_string() } else { String::new() };
                let toast = ch.press(ev["context"]["open_chat_id"].as_str().unwrap_or(""), &data).await;
                Some(json!({"toast": {"type": "info", "content": toast}}))
            }
            _ => None,
        }
    }

    async fn create(&self, ch: &Chan, peer: &str, msg_type: &str, content: &Value) -> anyhow::Result<Value> {
        self.api(
            ch,
            reqwest::Method::POST,
            "/open-apis/im/v1/messages",
            &[("receive_id_type", "chat_id")],
            Some(json!({"receive_id": peer, "msg_type": msg_type, "content": py::dumps_u(content)})),
        )
        .await
    }

    fn card(card: &Card) -> Value {
        let mut els = vec![json!({"tag": "markdown", "content": card["body"]})];
        let done = card["done"].as_str().unwrap_or("");
        if !done.is_empty() {
            els.push(json!({"tag": "markdown", "content": format!("**{done}**")}));
        }
        let id = py::str_of(&card["id"]);
        let mut acts: Vec<Value> = card["buttons"].as_array().cloned().unwrap_or_default().iter().map(|b| {
            let style = match b[2].as_str() { Some("primary") => "primary", Some("danger") => "danger", _ => "default" };
            json!({"tag": "button", "text": {"tag": "plain_text", "content": b[0]}, "type": style, "value": {"osa": format!("ap:{id}:{}", py::str_of(&b[1]))}})
        }).collect();
        if let Some(l) = card["link"].as_array() {
            acts.push(json!({"tag": "button", "text": {"tag": "plain_text", "content": l[0]}, "type": "default", "url": l[1]}));
        }
        if !acts.is_empty() {
            els.push(json!({"tag": "action", "actions": acts}));
        }
        json!({"config": {"wide_screen_mode": true, "update_multi": true},
               "header": {"title": {"tag": "plain_text", "content": card["title"]}, "template": if done.is_empty() { "orange" } else { "grey" }},
               "elements": els})
    }
}

#[async_trait]
impl Platform for Feishu {
    fn max_len(&self) -> usize {
        6000
    }

    async fn run(&self, ch: Arc<Chan>) -> anyhow::Result<()> {
        let mut backoff = 5.0f64;
        loop {
            match self.session(&ch).await {
                Ok(()) => ch.set_state("error", Some(Later::plain("长连接断了，正在重连"))),
                Err(e) => {
                    if let Some(fe) = e.downcast_ref::<FeishuError>() {
                        ch.set_state(
                            "error",
                            Some(Later::new(
                                "飞书拒绝了连接（{error}），稍后自动重试。App Secret 重置过的话，删掉这个连接重新添加",
                                &[("error", json!(fe.msg))],
                            )),
                        );
                    } else {
                        let n = e.downcast_ref::<reqwest::Error>().map(err_name).unwrap_or("OSError");
                        ch.set_state("error", Some(Later::new("连接飞书失败（{error}），稍后自动重试", &[("error", json!(n))])));
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs_f64(backoff + rand::random::<f64>() * 5.0)).await;
            backoff = (backoff * 2.0).min(120.0);
        }
    }

    async fn download(&self, ch: &Arc<Chan>, fref: &Value) -> anyhow::Result<(Vec<u8>, String)> {
        let (mid, key, kind) = (py::str_of(&fref[0]), py::str_of(&fref[1]), py::str_of(&fref[2]));
        let r = self
            .http
            .get(format!("{}/open-apis/im/v1/messages/{mid}/resources/{key}", base(&self.domain)))
            .query(&[("type", kind.as_str())])
            .bearer_auth(self.token(ch).await?)
            .timeout(Duration::from_secs(120))
            .send()
            .await?;
        if r.headers().get("content-type").and_then(|v| v.to_str().ok()).unwrap_or("").contains("application/json") {
            let d: Value = r.json().await.unwrap_or(json!({}));
            anyhow::bail!(t!("飞书不让下载（{error}），检查应用有没有“获取与上传图片或文件资源”权限", error = d["msg"].clone()));
        }
        Ok((r.error_for_status()?.bytes().await?.to_vec(), String::new()))
    }

    async fn send_text(&self, ch: &Arc<Chan>, peer: &str, md: &str) -> anyhow::Result<()> {
        let rows: Vec<Value> =
            fmt::fence_segments(&fmt::feishu_md(md)).into_iter().map(|seg| json!([{"tag": "md", "text": seg}])).collect();
        if let Err(e) = self.create(ch, peer, "post", &json!({"zh_cn": {"title": "", "content": rows}})).await {
            if e.downcast_ref::<FeishuError>().is_none() {
                return Err(e);
            }
            self.create(ch, peer, "text", &json!({"text": fmt::plain(md)})).await?;
        }
        Ok(())
    }

    async fn send_card(&self, ch: &Arc<Chan>, peer: &str, card: &Card) -> anyhow::Result<String> {
        Ok(self.create(ch, peer, "interactive", &Self::card(card)).await?["message_id"].as_str().unwrap_or("").to_string())
    }

    async fn edit_card(&self, ch: &Arc<Chan>, _peer: &str, msg_id: &str, card: &Card) -> anyhow::Result<()> {
        if !msg_id.is_empty() {
            self.api(
                ch,
                reqwest::Method::PATCH,
                &format!("/open-apis/im/v1/messages/{msg_id}"),
                &[],
                Some(json!({"content": py::dumps_u(&Self::card(card))})),
            )
            .await?;
        }
        Ok(())
    }

    fn start_typing(&self, ch: &Arc<Chan>) -> bool {
        let mid = self.last_msg.lock().clone();
        let mut r = self.reaction.lock();
        if !mid.is_empty() && r.is_none() {
            *r = Some((mid.clone(), String::new()));
            let me = Feishu {
                http: self.http.clone(),
                domain: self.domain.clone(),
                app_id: self.app_id.clone(),
                token: Mutex::new(self.token.lock().clone()),
                seen: Mutex::new(VecDeque::new()),
                parts: Mutex::new(HashMap::new()),
                last_msg: Mutex::new(String::new()),
                reaction: self.reaction.clone(),
            };
            let c = ch.clone();
            ch.queue(Box::pin(async move {
                match me
                    .api(
                        &c,
                        reqwest::Method::POST,
                        &format!("/open-apis/im/v1/messages/{mid}/reactions"),
                        &[],
                        Some(json!({"reaction_type": {"emoji_type": "Typing"}})),
                    )
                    .await
                {
                    Ok(d) => *me.reaction.lock() = Some((mid, d["reaction_id"].as_str().unwrap_or("").to_string())),
                    Err(_) => *me.reaction.lock() = None,
                }
                Ok(())
            }));
        }
        true
    }

    fn stop_typing(&self, ch: &Arc<Chan>) -> bool {
        if self.reaction.lock().is_some() {
            let me = Feishu {
                http: self.http.clone(),
                domain: self.domain.clone(),
                app_id: self.app_id.clone(),
                token: Mutex::new(self.token.lock().clone()),
                seen: Mutex::new(VecDeque::new()),
                parts: Mutex::new(HashMap::new()),
                last_msg: Mutex::new(String::new()),
                reaction: self.reaction.clone(),
            };
            let c = ch.clone();
            ch.queue(Box::pin(async move {
                let hit = me.reaction.lock().take();
                if let Some((m, rid)) = hit.filter(|h| !h.1.is_empty()) {
                    let _ = me.api(&c, reqwest::Method::DELETE, &format!("/open-apis/im/v1/messages/{m}/reactions/{rid}"), &[], None).await;
                }
                Ok(())
            }));
        }
        true
    }
}
