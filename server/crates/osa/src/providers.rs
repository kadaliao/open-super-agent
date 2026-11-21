//! 模型服务：一个兼容 OpenAI 的接口地址，加一个密钥。对应 osa/providers.py。
//!
//! config.yaml 的 providers 是默认值；界面里添加或改过的存在 settings 表的 providers 一行，密钥存进保险库（LLM_KEY_<名字>）。
//! 调用自己发 HTTP：流式按 SSE 一块块解析。重试和 openai SDK 一样：连不上、408/409/429/5xx 最多再试 5 次，
//! 0.5、1、2、4、8 秒退避（有 Retry-After 按它）。

use crate::{assets, config, db, i18n, memory, py, vault};
use futures::StreamExt;
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

pub const MODELS_TTL: f64 = 600.0;
pub const RETRIES: u32 = 5;

static BASE: LazyLock<Value> = LazyLock::new(|| config::ORIGINAL.get("providers").cloned().filter(|v| v.is_object()).unwrap_or(json!({})));
static MODELS: LazyLock<Mutex<HashMap<String, Value>>> = LazyLock::new(Default::default);

pub fn presets_raw() -> &'static Vec<Value> {
    static P: LazyLock<Vec<Value>> = LazyLock::new(|| assets::schemas()["provider_presets"].as_array().cloned().unwrap_or_default());
    &P
}

fn preset(id: &str) -> Option<&'static Value> {
    presets_raw().iter().find(|p| p["id"] == id)
}

fn preset_titles() -> Vec<String> {
    presets_raw().iter().filter_map(|p| p["title"].as_str().map(String::from)).collect()
}

/// 给界面的服务商列表：名字、说明按界面语言翻好
pub fn presets() -> Vec<Value> {
    presets_raw()
        .iter()
        .map(|p| {
            let mut p = p.clone();
            for k in ["title", "desc", "note"] {
                if let Some(s) = p[k].as_str().filter(|s| !s.is_empty()).map(String::from) {
                    p[k] = json!(t!(&s));
                }
            }
            p
        })
        .collect()
}

fn title_of(t: &str) -> String {
    if preset_titles().iter().any(|x| x == t) { t!(t) } else { t.to_string() }
}

fn stored_title(t: &str) -> String {
    preset_titles().into_iter().find(|zh| t == i18n::t_in(zh, Some("en"), &[])).unwrap_or_else(|| t.to_string())
}

fn load() -> Map<String, Value> {
    memory::setting("providers").and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

fn store(d: &Map<String, Value>) {
    memory::set_setting("providers", &Value::Object(d.clone()));
}

/// config.yaml 里的加上界面里添加、改过的，放进 CFG.providers
pub fn apply() {
    let mut merged = Map::new();
    for (n, p) in BASE.as_object().cloned().unwrap_or_default() {
        let mut p = p.as_object().cloned().unwrap_or_default();
        p.insert("source".into(), json!("config"));
        merged.insert(n, Value::Object(p));
    }
    for (n, p) in load() {
        if let Some(m) = merged.get_mut(&n).and_then(|m| m.as_object_mut()) {
            for k in ["title", "base_url", "preset"] {
                if let Some(v) = p.get(k) {
                    m.insert(k.into(), v.clone());
                }
            }
        } else {
            let mut p = p.as_object().cloned().unwrap_or_default();
            p.insert("source".into(), json!("ui"));
            merged.insert(n, Value::Object(p));
        }
    }
    config::with_mut(|c| c["providers"] = Value::Object(merged));
}

fn providers() -> Map<String, Value> {
    config::get("providers").as_object().cloned().unwrap_or_default()
}

pub fn provider(name: &str) -> Option<Value> {
    providers().get(name).cloned()
}

// ---------- 密钥 ----------
pub fn key_ref(name: &str) -> String {
    static W: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\W").unwrap());
    format!("LLM_KEY_{}", W.replace_all(&name.to_uppercase(), "_"))
}

pub fn api_key(name: &str) -> String {
    if let Some(v) = vault::get(&key_ref(name)).filter(|v| !v.is_empty()) {
        return v;
    }
    let env = provider(name).and_then(|p| p["api_key_env"].as_str().map(String::from)).unwrap_or_default();
    if env.is_empty() { String::new() } else { std::env::var(env).unwrap_or_default() }
}

fn host_of(url: &str) -> String {
    reqwest::Url::parse(url).ok().and_then(|u| u.host_str().map(|h| h.trim_matches(['[', ']']).to_lowercase())).unwrap_or_default()
}

pub fn is_local(url: &str) -> bool {
    let host = host_of(url);
    if host == "localhost" || host == "host.docker.internal" || host.ends_with(".local") {
        return true;
    }
    match host.parse::<std::net::IpAddr>() {
        Ok(std::net::IpAddr::V4(ip)) => ip.is_private() || ip.is_loopback(),
        Ok(std::net::IpAddr::V6(ip)) => ip.is_loopback() || (ip.segments()[0] & 0xfe00) == 0xfc00,
        Err(_) => false,
    }
}

pub fn needs_key(name: &str) -> bool {
    let p = provider(name).unwrap_or(json!({}));
    let no_key = preset(p["preset"].as_str().unwrap_or("")).is_some_and(|x| py::truthy(&x["no_key"]));
    !(no_key || is_local(p["base_url"].as_str().unwrap_or("")))
}

pub fn exists(name: &str) -> bool {
    providers().contains_key(name)
}

pub fn usable(name: &str) -> bool {
    exists(name) && (!api_key(name).is_empty() || !needs_key(name))
}

fn hint(k: &str) -> String {
    if k.chars().count() >= 12 { format!("••••{}", py::tail(k, 4)) } else { "••••".into() }
}

pub fn describe(name: &str) -> Value {
    let p = provider(name).unwrap_or(json!({}));
    let in_vault = vault::get(&key_ref(name)).is_some_and(|v| !v.is_empty());
    let env = p["api_key_env"].as_str().unwrap_or("").to_string();
    let from_env = !in_vault && !env.is_empty() && std::env::var(&env).is_ok_and(|v| !v.is_empty());
    let k = api_key(name);
    let m = MODELS.lock().get(name).cloned().unwrap_or(json!({}));
    let pre = p["preset"].as_str().filter(|s| !s.is_empty()).unwrap_or(name);
    let title = p["title"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(String::from)
        .or_else(|| preset(pre).and_then(|x| x["title"].as_str().map(String::from)))
        .unwrap_or_else(|| name.to_string());
    json!({
        "name": name, "title": title_of(&title),
        "preset": p["preset"].as_str().filter(|s| !s.is_empty()).map(String::from).unwrap_or_else(|| if preset(name).is_some() { name.into() } else { "custom".into() }),
        "base_url": p["base_url"], "source": p["source"].as_str().unwrap_or("config"), "key_env": env,
        "key_source": if in_vault { "vault" } else if from_env { "env" } else { "" },
        "key_hint": if k.is_empty() { String::new() } else { hint(&k) }, "needs_key": needs_key(name),
        "models": m["models"].as_array().map(|a| a.len()).unwrap_or(0), "error": m["error"].as_str().unwrap_or(""),
        "checked": m.get("ts").cloned().unwrap_or(json!(0)),
    })
}

pub fn public() -> Vec<Value> {
    providers().keys().map(|n| describe(n)).collect()
}

// ---------- 调用 ----------
/// 调模型出的错（Python 版是 openai 的异常）
#[derive(Debug, Clone)]
pub struct ApiError {
    pub status: Option<u16>,
    pub message: String,
    pub timeout: bool,
    pub origin: String,
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.status {
            Some(s) => write!(f, "Error code: {s} - {}", self.message),
            None if self.timeout => write!(f, "Request timed out."),
            None => write!(f, "Connection error."),
        }
    }
}

impl std::error::Error for ApiError {}

/// 把接口报错翻成一句人话
pub fn explain(e: &ApiError, base_url: &str) -> String {
    let detail = py::head(&e.message, 200);
    match e.status {
        Some(401) => t!("密钥不对，或者已经失效（401）"),
        Some(403) => t!("没有权限（403）：可能没开通这个模型，或者账户欠费。{detail}", detail = detail),
        Some(404) => t!("找不到（404）：接口地址或模型名字不对。{detail}", detail = detail),
        Some(429) => t!("请求太多或余额不足（429）。{detail}", detail = detail),
        Some(c) if c >= 500 => t!("模型服务那边临时出了故障（{code}），一般过一会儿就好，稍后再试一次", code = c),
        Some(c) => t!("出错了（{code}）：{detail}", code = c, detail = detail),
        None if e.timeout => t!("连接超时：{url}", url = base_url),
        None => t!("连不上 {url}", url = base_url),
    }
}

/// 对话和任务里报错时用：调模型出的错翻成人话，别的错误原样
pub fn brief(e: &anyhow::Error) -> String {
    match e.downcast_ref::<ApiError>() {
        Some(a) => explain(a, &a.origin),
        None => e.to_string(),
    }
}

fn origin_of(url: &str) -> String {
    reqwest::Url::parse(url).map(|u| format!("{}://{}", u.scheme(), u.host_str().unwrap_or(""))).unwrap_or_default()
}

static HTTP: LazyLock<reqwest::Client> = LazyLock::new(|| {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(60))
        .pool_idle_timeout(Duration::from_secs(90))
        .build()
        .expect("http client")
});

pub fn http() -> &'static reqwest::Client {
    &HTTP
}

/// 一个服务的地址和密钥
#[derive(Clone, Debug)]
pub struct Client {
    pub base_url: String,
    pub key: String,
    pub timeout: Option<Duration>,
    pub retries: u32,
}

impl Client {
    pub fn for_provider(name: &str) -> anyhow::Result<Client> {
        let p = provider(name).ok_or_else(|| anyhow::anyhow!(t!("没有这个模型服务：{name}", name = name)))?;
        let k = api_key(name);
        Ok(Client {
            base_url: p["base_url"].as_str().unwrap_or("").to_string(),
            key: if k.is_empty() { "none".into() } else { k },
            timeout: None,
            retries: RETRIES,
        })
    }

    pub fn url(&self, path: &str) -> String {
        format!("{}/{}", self.base_url.trim_end_matches('/'), path.trim_start_matches('/'))
    }

    fn err(&self, status: Option<u16>, message: String, timeout: bool) -> ApiError {
        ApiError { status, message, timeout, origin: origin_of(&self.base_url) }
    }

    /// 发一次请求（失败按规则重试），返回成功的响应
    pub async fn send(&self, method: reqwest::Method, path: &str, body: Option<&Value>) -> Result<reqwest::Response, ApiError> {
        let mut attempt = 0;
        loop {
            let mut rb = HTTP
                .request(method.clone(), self.url(path))
                .bearer_auth(&self.key)
                .header("user-agent", "OpenAI/Python 2.0 (osa)")
                .header("x-stainless-retry-count", attempt.to_string());
            rb = rb.timeout(self.timeout.unwrap_or(Duration::from_secs(600)));
            if method == reqwest::Method::GET && path == "models" {
                rb = rb.header("X-Magpie-Drawers", "1").header("X-Magpie-Videomakers", "1");
            }
            if let Some(b) = body {
                rb = rb.json(b);
            }
            let (retry, wait, err) = match rb.send().await {
                Ok(r) if r.status().is_success() => return Ok(r),
                Ok(r) => {
                    let s = r.status().as_u16();
                    let after = r
                        .headers()
                        .get("retry-after-ms")
                        .and_then(|v| v.to_str().ok())
                        .and_then(|v| v.parse::<f64>().ok())
                        .map(|ms| ms / 1000.0)
                        .or_else(|| r.headers().get("retry-after").and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<f64>().ok()));
                    let should = r.headers().get("x-should-retry").and_then(|v| v.to_str().ok()).map(|v| v == "true");
                    let text = r.text().await.unwrap_or_default();
                    let msg = serde_json::from_str::<Value>(&text)
                        .ok()
                        .and_then(|v| {
                            let e = if v["error"].is_object() { v["error"].clone() } else { v.clone() };
                            e["message"].as_str().map(String::from)
                        })
                        .unwrap_or(text);
                    let retry = should.unwrap_or(matches!(s, 408 | 409 | 429) || s >= 500);
                    (retry, after, self.err(Some(s), msg, false))
                }
                Err(e) => (true, None, self.err(None, e.to_string(), e.is_timeout())),
            };
            if !retry || attempt >= self.retries {
                return Err(err);
            }
            let backoff = match wait {
                Some(w) if (0.0..=60.0).contains(&w) => w,
                _ => {
                    let base = (0.5 * 2f64.powi(attempt as i32)).min(8.0);
                    base * (1.0 - 0.25 * rand::random::<f64>())
                }
            };
            tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
            attempt += 1;
        }
    }

    pub async fn post_json(&self, path: &str, body: &Value) -> Result<Value, ApiError> {
        let r = self.send(reqwest::Method::POST, path, Some(body)).await?;
        r.json::<Value>().await.map_err(|e| self.err(None, e.to_string(), e.is_timeout()))
    }

    pub async fn get_json(&self, path: &str) -> Result<Value, ApiError> {
        let r = self.send(reqwest::Method::GET, path, None).await?;
        r.json::<Value>().await.map_err(|e| self.err(None, e.to_string(), e.is_timeout()))
    }

    /// 流式的 chat/completions：每块是解析好的 JSON（choices[0].delta …）
    pub async fn stream(&self, mut body: Value) -> Result<ChunkStream, ApiError> {
        body["stream"] = json!(true);
        let r = self.send(reqwest::Method::POST, "chat/completions", Some(&body)).await?;
        Ok(ChunkStream { inner: Box::pin(r.bytes_stream()), buf: Vec::new(), done: false, origin: origin_of(&self.base_url) })
    }
}

/// SSE 解析成一块块 JSON
pub struct ChunkStream {
    inner: std::pin::Pin<Box<dyn futures::Stream<Item = reqwest::Result<bytes::Bytes>> + Send>>,
    buf: Vec<u8>,
    done: bool,
    origin: String,
}

impl ChunkStream {
    pub async fn next(&mut self) -> Option<Result<Value, ApiError>> {
        loop {
            if self.done {
                return None;
            }
            // 一个事件以空行结束
            if let Some(pos) = find_event_end(&self.buf) {
                let ev: Vec<u8> = self.buf.drain(..pos.0).collect();
                self.buf.drain(..pos.1);
                let text = String::from_utf8_lossy(&ev);
                let mut data = String::new();
                for line in text.lines() {
                    if let Some(d) = line.strip_prefix("data:") {
                        if !data.is_empty() {
                            data.push('\n');
                        }
                        data.push_str(d.strip_prefix(' ').unwrap_or(d));
                    }
                }
                if data.is_empty() {
                    continue;
                }
                if data.trim() == "[DONE]" {
                    self.done = true;
                    return None;
                }
                match serde_json::from_str::<Value>(&data) {
                    Ok(v) => {
                        if v.get("error").is_some_and(|e| !e.is_null()) {
                            let msg = v["error"]["message"].as_str().map(String::from).unwrap_or_else(|| v["error"].to_string());
                            self.done = true;
                            return Some(Err(ApiError { status: Some(500), message: msg, timeout: false, origin: self.origin.clone() }));
                        }
                        return Some(Ok(v));
                    }
                    Err(_) => continue,
                }
            }
            match self.inner.next().await {
                Some(Ok(b)) => self.buf.extend_from_slice(&b),
                Some(Err(e)) => {
                    self.done = true;
                    return Some(Err(ApiError {
                        status: None,
                        message: e.to_string(),
                        timeout: e.is_timeout(),
                        origin: self.origin.clone(),
                    }));
                }
                None => {
                    self.done = true;
                    if !self.buf.is_empty() {
                        self.buf.extend_from_slice(b"\n\n");
                        self.done = false;
                        let rest = std::mem::take(&mut self.buf);
                        self.buf = rest;
                        // 再走一遍解析：最后一个事件后面可能没有空行
                        if let Some(pos) = find_event_end(&self.buf) {
                            let ev: Vec<u8> = self.buf.drain(..pos.0).collect();
                            self.buf.clear();
                            self.done = true;
                            let text = String::from_utf8_lossy(&ev);
                            let data: String = text.lines().filter_map(|l| l.strip_prefix("data:")).map(|d| d.trim_start()).collect();
                            if data.trim() != "[DONE]"
                                && let Ok(v) = serde_json::from_str::<Value>(&data)
                            {
                                return Some(Ok(v));
                            }
                        }
                    }
                    return None;
                }
            }
        }
    }
}

fn find_event_end(b: &[u8]) -> Option<(usize, usize)> {
    for i in 0..b.len() {
        if b[i..].starts_with(b"\r\n\r\n") {
            return Some((i, 4));
        }
        if b[i..].starts_with(b"\n\n") {
            return Some((i, 2));
        }
    }
    None
}

/// 某个角色的客户端和模型名
pub fn client_for(role: &str) -> anyhow::Result<(Client, String)> {
    let (p, m) = config::role(role);
    Ok((Client::for_provider(&p)?, m))
}

/// 不流式的一次调用的结果
pub struct Completion {
    pub content: String,
    pub reasoning: String,
    pub usage: Option<Value>,
    pub raw: Value,
}

/// 按角色调一次（不流式），记用量。extra 是 response_format 之类
pub async fn complete_role(role: &str, msgs: &Value, response_format: Option<Value>) -> anyhow::Result<Completion> {
    let (c, model) = client_for(role)?;
    let mut body = json!({"model": model, "messages": msgs});
    if let Some(rf) = response_format {
        body["response_format"] = rf;
    }
    for (k, v) in config::call_kwargs(role) {
        body[k] = v;
    }
    let t0 = Instant::now();
    match c.post_json("chat/completions", &body).await {
        Ok(v) => {
            memory::record_usage(role, &model, v.get("usage").filter(|u| !u.is_null()), memory::UsageRec::default());
            let msg = &v["choices"][0]["message"];
            Ok(Completion {
                content: msg["content"].as_str().unwrap_or("").to_string(),
                reasoning: msg["reasoning_content"].as_str().unwrap_or("").to_string(),
                usage: v.get("usage").cloned(),
                raw: v.clone(),
            })
        }
        Err(e) => {
            memory::record_error(
                role,
                &model,
                &explain(&e, &e.origin),
                memory::UsageRec { ms: Some(t0.elapsed().as_secs_f64() * 1000.0), ..Default::default() },
            );
            Err(e.into())
        }
    }
}

// ---------- 模型列表 ----------
async fn list_models(base_url: &str, key: &str) -> Result<Value, ApiError> {
    let c = Client {
        base_url: base_url.into(),
        key: if key.is_empty() { "none".into() } else { key.into() },
        timeout: Some(Duration::from_secs(15)),
        retries: 0,
    };
    let v = c.get_json("models").await?;
    let mut ids = Vec::new();
    let mut media = json!({"image": {}, "video": {}});
    let mut magpie = false;
    for row in v["data"].as_array().into_iter().flatten() {
        let Some(id) = row["id"].as_str().filter(|id| !id.is_empty()) else { continue };
        if row["magpie_label"].is_string() {
            magpie = true;
            if let Some(kind @ ("image" | "video")) = row["kind"].as_str() {
                let edit =
                    kind == "image" && row["supported_endpoint_types"].as_array().is_some_and(|a| a.iter().any(|x| x == "image-edit"));
                media[kind][id] = json!(if edit { "edit" } else { kind });
                continue;
            }
        }
        ids.push(id);
    }
    ids.sort();
    ids.dedup();
    let mut out = json!({"ok": true, "models": ids, "error": "", "note": ""});
    if magpie {
        out["magpie_media"] = media;
    } else if ids.is_empty() {
        out["note"] = json!(t!("连上了，但是没有列出模型，选模型时直接填名字"));
    }
    Ok(out)
}

pub async fn check(base_url: &str, key: &str) -> Value {
    match list_models(base_url, key).await {
        Ok(m) => m,
        Err(e) if e.status == Some(404) => {
            json!({"ok": true, "models": [], "error": "", "note": t!("这个服务不提供模型列表，选模型时直接填名字")})
        }
        Err(e) if e.status == Some(401) => {
            json!({"ok": false, "models": [], "error": if key.is_empty() { t!("要填 API Key 才能用") } else { explain(&e, "") }, "note": ""})
        }
        Err(e) => json!({"ok": false, "models": [], "error": explain(&e, base_url), "note": ""}),
    }
}

pub async fn inventory(name: &str, refresh: bool) -> Result<Value, String> {
    let p = provider(name).ok_or_else(|| t!("没有这个模型服务：{name}", name = name))?;
    let cached = MODELS.lock().get(name).cloned();
    let m = match cached {
        Some(m) if !refresh && py::now() - m["ts"].as_f64().unwrap_or(0.0) <= MODELS_TTL => m,
        _ => {
            let mut m = if needs_key(name) && api_key(name).is_empty() {
                json!({"ok": false, "models": [], "error": t!("还没填 API Key"), "note": ""})
            } else {
                check(p["base_url"].as_str().unwrap_or(""), &api_key(name)).await
            };
            if m["ok"] == true && m["magpie_media"].is_null() && m["models"].as_array().is_none_or(|a| a.is_empty()) {
                let known = crate::prices::models_of(name);
                if !known.is_empty() {
                    m["models"] = json!(known);
                    m["note"] = json!(t!("这个服务没有列出模型，下面是 models.dev 收录的，也可以直接填名字"));
                }
            }
            m["ts"] = json!(py::now());
            MODELS.lock().insert(name.to_string(), m.clone());
            m
        }
    };
    Ok(m)
}

/// None 是普通服务；空串是 Magpie 没列出这个用途。
pub fn magpie_recipe(name: &str, kind: &str, model: &str) -> Option<String> {
    let models = MODELS.lock();
    let media = models.get(name)?.get("magpie_media")?;
    Some(media[kind][model].as_str().unwrap_or("").to_string())
}

pub async fn models(name: &str, refresh: bool) -> Result<Value, String> {
    let mut out = inventory(name, refresh).await?;
    let list: Vec<String> =
        out["models"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
    out["name"] = json!(name);
    out["items"] = json!(crate::prices::annotate(name, &list));
    Ok(out)
}

// ---------- 增删改 ----------
pub fn clean_url(url: &str) -> Result<String, String> {
    static R: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^https?://[^\s/]+").unwrap());
    let u = url.trim().trim_end_matches('/').to_string();
    if !R.is_match(&u) {
        return Err(t!("接口地址要以 http:// 或 https:// 开头"));
    }
    Ok(u)
}

fn new_name(base: &str) -> String {
    let mut b: String = base.to_lowercase().chars().filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '_').collect();
    if b.is_empty() {
        b = "custom".into();
    }
    if !b.chars().next().is_some_and(|c| c.is_alphabetic()) {
        b = format!("p{b}");
    }
    let b: String = b.chars().take(28).collect();
    let all = providers();
    let (mut name, mut n) = (b.clone(), 2);
    while all.contains_key(&name) {
        name = format!("{b}_{n}");
        n += 1;
    }
    name
}

pub fn same_origin(a: &str, b: &str) -> bool {
    fn origin(u: &str) -> Option<(String, String, u16)> {
        let p = reqwest::Url::parse(u).ok()?;
        let port = p.port().unwrap_or(if p.scheme() == "https" { 443 } else { 80 });
        Some((p.scheme().to_string(), p.host_str().unwrap_or("").to_lowercase(), port))
    }
    origin(a).is_some() && origin(a) == origin(b)
}

pub fn save(name: Option<&str>, data: &Value) -> Result<String, String> {
    let mut d = load();
    let title = stored_title(&py::head(data["title"].as_str().unwrap_or("").trim(), 40));
    let url = clean_url(data["base_url"].as_str().unwrap_or(""))?;
    let pre = data["preset"].as_str().filter(|p| preset(p).is_some()).map(String::from);
    let key = data["api_key"].as_str().unwrap_or("").trim().to_string();
    if let Some(n) = name
        && let Some(p) = provider(n)
        && key.is_empty()
        && !api_key(n).is_empty()
        && !same_origin(&url, p["base_url"].as_str().unwrap_or(""))
    {
        return Err(t!("换了接口地址要重新填 API Key（原来的密钥不会发给新地址）"));
    }
    let name = match name {
        None => {
            let base = match &pre {
                Some(p) if p != "custom" => p.clone(),
                _ => {
                    if title.is_empty() {
                        "custom".into()
                    } else {
                        title.clone()
                    }
                }
            };
            let n = new_name(&base);
            let t = if !title.is_empty() {
                title.clone()
            } else {
                pre.as_deref().and_then(preset).and_then(|p| p["title"].as_str().map(String::from)).unwrap_or_else(|| n.clone())
            };
            d.insert(n.clone(), json!({"title": t, "base_url": url, "preset": pre.clone().unwrap_or_else(|| "custom".into())}));
            n
        }
        Some(n) => {
            if !exists(n) {
                return Err(t!("没有这个模型服务：{name}", name = n));
            }
            let e = d.entry(n.to_string()).or_insert(json!({}));
            e["base_url"] = json!(url);
            if !title.is_empty() {
                e["title"] = json!(title);
            }
            n.to_string()
        }
    };
    if !key.is_empty() {
        vault::put(&key_ref(&name), &key).map_err(|e| e.to_string())?;
    } else if py::truthy(&data["clear_key"]) {
        vault::delete(&key_ref(&name));
    }
    store(&d);
    apply();
    MODELS.lock().remove(&name);
    Ok(name)
}

pub fn users(name: &str) -> Vec<String> {
    let mut out: Vec<String> = config::get("roles")
        .as_object()
        .map(|o| o.iter().filter(|(_, v)| v["provider"] == name).map(|(k, _)| k.clone()).collect())
        .unwrap_or_default();
    if config::get_str("embedding.provider") == name {
        out.push("embedding".into());
    }
    out
}

pub fn delete(name: &str, role_names: &HashMap<&str, &str>) -> Result<(), String> {
    let p = provider(name).ok_or_else(|| t!("没有这个模型服务：{name}", name = name))?;
    if p["source"] == "config" {
        return Err(t!("这个是 config.yaml 里写的，要在那里删"));
    }
    let u = users(name);
    if !u.is_empty() {
        let roles: Vec<String> = u.iter().map(|x| t!(role_names.get(x.as_str()).copied().unwrap_or(x.as_str()))).collect();
        return Err(t!("{roles}还在用它，先换成别的模型", roles = roles.join(&t!("、"))));
    }
    let mut d = load();
    d.remove(name);
    store(&d);
    vault::delete(&key_ref(name));
    MODELS.lock().remove(name);
    apply();
    Ok(())
}

// ---------- 测试一个模型 ----------
fn png(w: u32, h: u32, rgb: [u8; 3]) -> Vec<u8> {
    let mut raw = vec![];
    for _ in 0..h {
        raw.push(0);
        for _ in 0..w {
            raw.extend_from_slice(&rgb);
        }
    }
    fn chunk(t: &[u8], data: &[u8]) -> Vec<u8> {
        let mut out = (data.len() as u32).to_be_bytes().to_vec();
        out.extend_from_slice(t);
        out.extend_from_slice(data);
        let mut c = t.to_vec();
        c.extend_from_slice(data);
        out.extend_from_slice(&crc32(&c).to_be_bytes());
        out
    }
    let mut ihdr = w.to_be_bytes().to_vec();
    ihdr.extend_from_slice(&h.to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]);
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    out.extend(chunk(b"IHDR", &ihdr));
    out.extend(chunk(b"IDAT", &crate::util::zlib_compress(&raw)));
    out.extend(chunk(b"IEND", b""));
    out
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

pub async fn test(provider_name: &str, model: &str, kind: &str, role: &str) -> Result<Value, String> {
    let p = provider(provider_name).ok_or_else(|| t!("没有这个模型服务：{name}", name = provider_name))?;
    if model.trim().is_empty() {
        return Err(t!("先选一个模型"));
    }
    let base = p["base_url"].as_str().unwrap_or("").to_string();
    let k = api_key(provider_name);
    let c = Client {
        base_url: base.clone(),
        key: if k.is_empty() { "none".into() } else { k },
        timeout: Some(Duration::from_secs(60)),
        retries: 0,
    };
    let mut t0 = Instant::now();
    let ms = |t0: Instant| t0.elapsed().as_millis() as i64;
    if kind == "embedding" {
        return Ok(match c.post_json("embeddings", &json!({"model": model, "input": ["测试一下"]})).await {
            Ok(r) => json!({"ok": true, "dim": r["data"][0]["embedding"].as_array().map(|a| a.len()).unwrap_or(0), "ms": ms(t0)}),
            Err(e) => json!({"ok": false, "error": explain(&e, &base), "ms": ms(t0)}),
        });
    }
    let roles = config::get("roles");
    let kw = if roles.get(role).is_some() { config::call_kwargs(role) } else { Map::new() };
    let ask = |content: Value| {
        let c = c.clone();
        let kw = kw.clone();
        let model = model.to_string();
        let pn = provider_name.to_string();
        async move {
            let mut body = json!({"model": model, "max_tokens": 512, "messages": [{"role": "user", "content": content}]});
            for (k, v) in kw {
                body[k] = v;
            }
            let r = c.post_json("chat/completions", &body).await?;
            memory::record_usage(
                "test",
                &model,
                r.get("usage").filter(|u| !u.is_null()),
                memory::UsageRec { provider: Some(pn), ..Default::default() },
            );
            let msg = &r["choices"][0]["message"];
            let s = msg["content"].as_str().filter(|s| !s.is_empty()).or(msg["reasoning_content"].as_str()).unwrap_or("");
            Ok::<String, ApiError>(s.trim().to_string())
        }
    };
    let red = format!("data:image/png;base64,{}", crate::util::b64(&png(64, 64, [220, 30, 30])));
    let img_err =
        match ask(json!([{"type": "text", "text": "这张图片是什么颜色？只回答颜色。"}, {"type": "image_url", "image_url": {"url": red}}]))
            .await
        {
            Ok(reply) => {
                let seen = if reply.contains('红') || reply.to_lowercase().contains("red") {
                    "yes".to_string()
                } else {
                    match crate::prices::lookup(provider_name, model).get("vision") {
                        Some(Value::Bool(false)) => "no".into(),
                        _ => "unsure".into(),
                    }
                };
                crate::media::remember_test(provider_name, model, &seen);
                return Ok(json!({"ok": true, "vision": seen, "reply": py::head(&reply, 80), "ms": ms(t0)}));
            }
            Err(e) if matches!(e.status, Some(401) | Some(403) | Some(429)) || (e.status.is_none() && !e.timeout) => {
                return Ok(json!({"ok": false, "error": explain(&e, &base), "ms": ms(t0)}));
            }
            Err(e) => explain(&e, &base),
        };
    t0 = Instant::now();
    Ok(match ask(json!("回复“好”这一个字。")).await {
        Ok(reply) => {
            crate::media::remember_test(provider_name, model, "no");
            json!({"ok": true, "vision": "no", "vision_error": img_err, "reply": py::head(&reply, 80), "ms": ms(t0)})
        }
        Err(e) => json!({"ok": false, "error": explain(&e, &base), "ms": ms(t0)}),
    })
}

#[allow(dead_code)]
fn _unused() {
    let _ = db::conn;
}
