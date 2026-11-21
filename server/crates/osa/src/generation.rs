//! 画图、生成视频：按“配方”调各家的接口。代码里不写模型名和参数，都在配方里（osa/generation.yaml，config.yaml 的 generation 可以加、改）。
//! 对应 osa/generation.py（配方的写法见那里的说明）。

use crate::{config, memory, providers, py};
use hmac::{Hmac, Mac};
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::Sha256;
use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::Duration;

pub const MAX_IMAGE: usize = 20 << 20;
pub const MAX_VIDEO: usize = 200 << 20;
pub const LIST_S: f64 = 600.0;
pub const LINK_S: i64 = 6 * 3600;

static DEFAULTS: LazyLock<Value> = LazyLock::new(|| {
    let y: serde_yaml::Value = serde_yaml::from_str(include_str!("../../../../osa/generation.yaml")).expect("generation.yaml");
    config::yaml_to_json(y)
});

pub fn defaults() -> &'static Value {
    &DEFAULTS
}

/// config.yaml 的 generation.recipes 排在默认的前面；同名的默认配方不要了
fn recipes(magpie: Option<&str>) -> Vec<Value> {
    let mine: Vec<Value> = config::get("generation.recipes").as_array().cloned().unwrap_or_default();
    let names: Vec<Value> = mine.iter().map(|r| r["name"].clone()).collect();
    let mut out = mine;
    let defaults = match magpie {
        Some(key) => DEFAULTS["magpie"].get(key).into_iter().cloned().collect(),
        None => DEFAULTS["recipes"].as_array().cloned().unwrap_or_default(),
    };
    for r in defaults {
        if !names.contains(&r["name"]) {
            out.push(r);
        }
    }
    out
}

pub fn prefer(kind: &str) -> Vec<String> {
    let mine = config::get(&format!("generation.prefer.{kind}"));
    let v = if py::truthy(&mine) { mine } else { DEFAULTS["prefer"][kind].clone() };
    v.as_array().map(|a| a.iter().map(py::str_of).collect()).unwrap_or_default()
}

pub fn rank(kind: &str, model: &str) -> usize {
    let x = model.to_lowercase();
    let order = prefer(kind);
    order.iter().position(|k| x.contains(&k.to_lowercase())).unwrap_or(order.len())
}

pub fn base(provider: &str) -> String {
    providers::provider(provider)
        .and_then(|p| p["base_url"].as_str().map(String::from))
        .unwrap_or_default()
        .trim_end_matches('/')
        .to_string()
}

fn re_i(pat: &str, text: &str) -> bool {
    Regex::new(&format!("(?i){pat}")).is_ok_and(|r| r.is_match(text))
}

fn host_ok(r: &Value, provider: &str, base: &str) -> bool {
    match r["host"].as_str().filter(|h| !h.is_empty()) {
        Some(h) => h == provider || re_i(h, base),
        None => true,
    }
}

/// 这个服务的这个模型按哪条配方调
pub fn recipe(kind: &str, provider: &str, model: &str) -> Option<Value> {
    let b = base(provider).to_lowercase();
    for r in recipes(providers::magpie_recipe(provider, kind, model).as_deref()) {
        if r["kind"] != kind {
            continue;
        }
        if !host_ok(&r, provider, &b) {
            continue;
        }
        if re_i(r["model"].as_str().unwrap_or(""), model) {
            return if py::truthy(&r["skip"]) { None } else { Some(r) };
        }
    }
    None
}

// ---------- 单价：在 prices 模块（和 Python 版一样，这里只转一下） ----------
pub fn image_price(provider: &str, model: &str) -> Option<f64> {
    crate::prices::image_price(provider, model)
}

pub fn video_price(provider: &str, model: &str, seconds: Option<i64>) -> Option<f64> {
    crate::prices::video_price(provider, model, seconds)
}

pub fn price(kind: &str, provider: &str, model: &str) -> Option<f64> {
    if kind == "image" { image_price(provider, model) } else { video_price(provider, model, None) }
}

/// 美元换成设置的货币（prices._rate）
pub fn usd_rate() -> f64 {
    crate::prices::usd_rate()
}

// ---------- 能用的模型 ----------
static LISTS: LazyLock<Mutex<HashMap<(String, String), (f64, Vec<String>)>>> = LazyLock::new(Default::default);

fn list_paths(kind: &str, provider: &str) -> Vec<String> {
    let b = base(provider).to_lowercase();
    let mut out: Vec<String> = vec![];
    for r in recipes(None) {
        if r["kind"] == kind && py::truthy(&r["list"]) && py::truthy(&r["host"]) && host_ok(&r, provider, &b) {
            let l = py::str_of(&r["list"]);
            if !out.contains(&l) {
                out.push(l);
            }
        }
    }
    out
}

async fn listed(provider: &str, path: &str) -> Vec<String> {
    let k = (provider.to_string(), path.to_string());
    if let Some(hit) = LISTS.lock().get(&k).filter(|h| py::now() - h.0 < LIST_S) {
        return hit.1.clone();
    }
    let key = providers::api_key(provider);
    let mut rb = client(20.0).get(format!("{}{path}", base(provider)));
    if !key.is_empty() {
        rb = rb.header("Authorization", format!("Bearer {key}"));
    }
    let ids: Vec<String> = match rb.send().await {
        Ok(r) if r.status().as_u16() < 400 => r
            .json::<Value>()
            .await
            .ok()
            .and_then(|d| d["data"].as_array().map(|a| a.iter().filter_map(|m| m["id"].as_str().map(String::from)).collect()))
            .unwrap_or_default(),
        _ => vec![],
    };
    LISTS.lock().insert(k, (py::now(), ids.clone()));
    ids
}

pub fn offers(kind: &str, provider: &str) -> Vec<String> {
    let b = base(provider).to_lowercase();
    let mut out: Vec<String> = vec![];
    for r in recipes(None) {
        if r["kind"] == kind && py::truthy(&r["offer"]) && py::truthy(&r["host"]) && host_ok(&r, provider, &b) {
            for m in r["offer"].as_array().cloned().unwrap_or_default() {
                let m = py::str_of(&m);
                if !out.contains(&m) {
                    out.push(m);
                }
            }
        }
    }
    out
}

/// 已经配好的模型服务里这一类的模型，带价格和能收什么
pub async fn catalog(kind: &str) -> Vec<Value> {
    let mut out = vec![];
    let names: Vec<String> = config::get("providers").as_object().map(|o| o.keys().cloned().collect()).unwrap_or_default();
    for name in names {
        if !providers::usable(&name) {
            continue;
        }
        let Ok(m) = providers::models(&name, false).await else { continue };
        let title = py::str_of(&providers::describe(&name)["title"]);
        let mut all: Vec<String> = m["models"].as_array().map(|a| a.iter().map(py::str_of).collect()).unwrap_or_default();
        if let Some(media) = m["magpie_media"][kind].as_object() {
            all.extend(media.keys().cloned());
        }
        for p in list_paths(kind, &name) {
            all.extend(listed(&name, &p).await);
        }
        all.extend(offers(kind, &name));
        let mut seen = std::collections::HashSet::new();
        for model in all {
            if !seen.insert(model.clone()) || recipe(kind, &name, &model).is_none() {
                continue;
            }
            let c = caps(kind, &name, &model);
            out.push(json!({"provider": name, "title": title, "model": model, "price": price(kind, &name, &model),
                "refs": c.images, "needs_ref": c.needs_ref(), "frames": c.frames, "videos": c.videos,
                "seconds": c.seconds, "ratios": c.ratios}));
        }
    }
    out
}

#[derive(Clone, Debug, Default)]
pub struct Caps {
    pub images: i64,
    pub frames: i64,
    pub videos: i64,
    pub total: i64,
    pub need: Map<String, Value>,
    pub seconds: Vec<i64>,
    pub ratios: Vec<String>,
}

impl Caps {
    pub fn needs_ref(&self) -> bool {
        !self.need.is_empty()
    }
}

pub fn seconds_of(v: &Value) -> Vec<i64> {
    static R: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+-\d+$").unwrap());
    if let Some(s) = v.as_str().filter(|s| R.is_match(s)) {
        let (a, b) = s.split_once('-').unwrap();
        let (a, b): (i64, i64) = (a.parse().unwrap_or(0), b.parse().unwrap_or(0));
        return (a..=b).collect();
    }
    match v {
        Value::Array(a) => a.iter().filter_map(py::to_int).collect(),
        v if py::truthy(v) => py::to_int(v).into_iter().collect(),
        _ => vec![],
    }
}

fn int_of(v: &Value) -> i64 {
    if py::truthy(v) { py::to_int(v).unwrap_or(0) } else { 0 }
}

pub fn caps(kind: &str, provider: &str, model: &str) -> Caps {
    let r = recipe(kind, provider, model).unwrap_or(json!({}));
    let ratios: Vec<String> = match r["ratios"].as_array().filter(|a| !a.is_empty()) {
        Some(a) => a.iter().map(py::str_of).collect(),
        None => r["sizes"].as_object().map(|o| o.keys().cloned().collect()).unwrap_or_default(),
    };
    Caps {
        images: int_of(&r["images"]),
        frames: int_of(&r["frames"]),
        videos: int_of(&r["videos"]),
        total: int_of(&r["total"]),
        need: r["need"].as_object().cloned().unwrap_or_default(),
        seconds: seconds_of(&r["seconds"]),
        ratios,
    }
}

/// 一个参考素材：data 是内容（参考图），url 是能从外面打开的地址
#[derive(Clone, Debug, Default)]
pub struct Ref {
    pub data: Vec<u8>,
    pub mime: String,
    pub name: String,
    pub url: String,
    pub role: String,
    pub w: i64,
    pub h: i64,
}

impl Ref {
    pub fn data_url(&self) -> String {
        format!("data:{};base64,{}", self.mime, crate::util::b64(&self.data))
    }
    pub fn kind(&self) -> &'static str {
        if self.role == "video" { "video" } else { "image" }
    }
}

/// 参考素材合不合这个模型：合适返回空，不合适返回原因
pub fn check(c: &Caps, refs: &[Ref], what: &str) -> String {
    let count = |role: &str| refs.iter().filter(|x| x.role == role).count() as i64;
    let n: [(&str, i64, i64); 3] =
        [("frames", count("frame"), c.frames), ("images", count("image"), c.images), ("videos", count("video"), c.videos)];
    let label = |k: &str| match k {
        "frames" => t!("首尾帧"),
        "images" => t!("参考图"),
        "videos" => t!("参考视频"),
        "refs" => t!("参考图或参考视频"),
        other => other.to_string(),
    };
    for (k, got, most) in n {
        if got > most {
            return if most == 0 {
                t!("{model} 不收{what}", model = what, what = label(k))
            } else {
                t!("{model} 最多收 {n} 个{what}", model = what, what = label(k), n = most)
            };
        }
    }
    let (nf, ni, nv) = (n[0].1, n[1].1, n[2].1);
    if c.total != 0 && nf + ni + nv > c.total {
        return t!("{model} 的参考素材加起来最多 {n} 个", model = what, n = c.total);
    }
    if nf > 0 && (ni > 0 || nv > 0) {
        return t!("首尾帧和参考素材不能一起用");
    }
    for (k, least) in &c.need {
        let have = match k.as_str() {
            "refs" => ni + nv,
            "frames" => nf,
            "images" => ni,
            "videos" => nv,
            _ => 0,
        };
        if have < py::to_int(least).unwrap_or(0) {
            return t!("{model} 只能照着原图生成，要给它{what}", model = what, what = label(k));
        }
    }
    String::new()
}

// ---------- 模板 ----------
static VAR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\$\{(\w+)\}").unwrap());
static WHOLE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\$\w+$").unwrap());

pub fn render_str(s: &str, v: &Map<String, Value>) -> String {
    VAR.replace_all(s, |c: &regex::Captures| match v.get(&c[1]) {
        None | Some(Value::Null) => String::new(),
        Some(x) => py::str_of(x),
    })
    .into_owned()
}

/// 把模板里的 $名字、${名字} 换成变量；值为 null 的键、列表项去掉（返回 Null 表示去掉）
pub fn render(obj: &Value, v: &Map<String, Value>) -> Value {
    match obj {
        Value::String(s) => {
            if WHOLE.is_match(s) {
                return v.get(&s[1..]).cloned().unwrap_or(Value::Null);
            }
            Value::String(render_str(s, v))
        }
        Value::Object(o) => {
            let mut out = Map::new();
            for (k, x) in o {
                let val = render(x, v);
                if !val.is_null() {
                    out.insert(render_str(k, v), val);
                }
            }
            Value::Object(out)
        }
        Value::Array(a) => {
            let mut out = vec![];
            for x in a {
                if let Some(each) = x.get("$each").filter(|_| x.is_object()) {
                    let items = v.get(each.as_str().unwrap_or("")).and_then(|i| i.as_array()).cloned().unwrap_or_default();
                    for item in items {
                        let mut vv = v.clone();
                        if let Some(io) = item.as_object() {
                            for (k, x) in io {
                                vv.insert(k.clone(), x.clone());
                            }
                        }
                        let y = render(&x["as"], &vv);
                        if !y.is_null() {
                            out.push(y);
                        }
                    }
                } else {
                    let y = render(x, v);
                    if !y.is_null() {
                        out.push(y);
                    }
                }
            }
            Value::Array(out)
        }
        other => other.clone(),
    }
}

/// 回包里 a.b.0.c 这样的路径，取不到返回 Null
pub fn pick(d: &Value, path: &str) -> Value {
    let mut cur = d;
    for k in path.split('.') {
        match cur {
            Value::Array(a) if !k.is_empty() && k.chars().all(|c| c.is_ascii_digit()) && k.parse::<usize>().is_ok_and(|i| i < a.len()) => {
                cur = &a[k.parse::<usize>().unwrap()]
            }
            Value::Object(o) if o.contains_key(k) => cur = &o[k],
            _ => return Value::Null,
        }
    }
    cur.clone()
}

fn empty(v: &Value) -> bool {
    v.is_null() || v.as_str() == Some("")
}

/// 写几个路径时第一个有值的
pub fn first(d: &Value, paths: &Value) -> Value {
    let list: Vec<Value> = match paths {
        Value::Array(a) => a.clone(),
        p => vec![p.clone()],
    };
    for p in list {
        if let Some(p) = p.as_str() {
            let x = pick(d, p);
            if !empty(&x) {
                return x;
            }
        }
    }
    Value::Null
}

pub fn nearest_int(want: i64, allowed: &[i64]) -> Option<i64> {
    if allowed.is_empty() {
        return None;
    }
    if allowed.contains(&want) {
        return Some(want);
    }
    allowed.iter().copied().min_by_key(|a| ((a - want).abs(), *a))
}

fn nearest_str(want: Option<&str>, allowed: &[String], default: Option<&str>) -> Option<String> {
    if allowed.is_empty() {
        return None;
    }
    if let Some(w) = want
        && allowed.iter().any(|a| a == w)
    {
        return Some(w.to_string());
    }
    match default {
        Some(d) if allowed.iter().any(|a| a == d) => Some(d.to_string()),
        _ => Some(allowed[0].clone()),
    }
}

fn like(frame: &Ref, allowed: &[String]) -> Option<String> {
    if allowed.iter().any(|a| a == "adaptive") {
        return Some("adaptive".into());
    }
    static R: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+:\d+$").unwrap());
    let nums: Vec<&String> = allowed.iter().filter(|a| R.is_match(a)).collect();
    if frame.w == 0 || frame.h == 0 || nums.is_empty() {
        return None;
    }
    let want = frame.w as f64 / frame.h as f64;
    let ratio = |a: &str| {
        let (x, y) = a.split_once(':').unwrap();
        x.parse::<f64>().unwrap_or(1.0) / y.parse::<f64>().unwrap_or(1.0)
    };
    nums.into_iter()
        .min_by(|a, b| (ratio(a) - want).abs().partial_cmp(&(ratio(b) - want).abs()).unwrap_or(std::cmp::Ordering::Equal))
        .cloned()
}

pub fn origin_of(url: &str) -> String {
    match reqwest::Url::parse(url) {
        Ok(u) => {
            let host = u.host_str().unwrap_or("");
            match u.port() {
                Some(p) => format!("{}://{host}:{p}", u.scheme()),
                None => format!("{}://{host}", u.scheme()),
            }
        }
        Err(_) => "://".into(),
    }
}

pub fn variables(
    r: &Value,
    provider: &str,
    model: &str,
    prompt: &str,
    refs: &[Ref],
    seconds: Option<i64>,
    ratio: Option<&str>,
) -> Map<String, Value> {
    let sizes = r["sizes"].as_object().cloned().unwrap_or_default();
    let allowed: Vec<String> = match r["ratios"].as_array().filter(|a| !a.is_empty()) {
        Some(a) => a.iter().map(py::str_of).collect(),
        None => sizes.keys().cloned().collect(),
    };
    let frames: Vec<&Ref> = refs.iter().filter(|x| x.role == "frame").collect();
    let mut ratio = ratio.filter(|s| !s.is_empty()).map(String::from);
    if ratio.is_none() && !frames.is_empty() {
        ratio = like(frames[0], &allowed);
    }
    let ratio = nearest_str(ratio.as_deref(), &allowed, r["ratio"].as_str());
    let secs = seconds_of(&r["seconds"]);
    let roles = r["roles"].as_object().cloned().unwrap_or_default();
    let as_url = r["image_url"] == "url";
    let imgs: Vec<&Ref> = refs.iter().filter(|x| x.kind() == "image").collect();
    let mut v = Map::new();
    v.insert("model".into(), json!(model));
    v.insert("prompt".into(), json!(prompt));
    v.insert("origin".into(), json!(origin_of(&base(provider))));
    v.insert("ratio".into(), ratio.clone().map(Value::String).unwrap_or(Value::Null));
    v.insert(
        "size".into(),
        match &ratio {
            Some(r) => sizes.get(r).cloned().unwrap_or(Value::Null),
            None => r.get("size").cloned().unwrap_or(Value::Null),
        },
    );
    v.insert(
        "seconds".into(),
        if secs.is_empty() {
            Value::Null
        } else {
            let want = seconds.filter(|s| *s != 0).unwrap_or_else(|| {
                let d = int_of(&r["default_seconds"]);
                if d != 0 { d } else { 5 }
            });
            json!(nearest_int(want, &secs))
        },
    );
    let images: Vec<Value> = imgs.iter().map(|x| json!(if as_url { x.url.clone() } else { x.data_url() })).collect();
    for (i, x) in images.iter().enumerate() {
        v.insert(format!("image{}", i + 1), x.clone());
    }
    v.insert("images".into(), if images.is_empty() { Value::Null } else { json!(images) });
    let mut media = vec![];
    for x in refs {
        let mut role = x.role.clone();
        if role == "frame" {
            role = if std::ptr::eq(x, frames[0]) { "first".into() } else { "last".into() };
        }
        let url = if x.kind() == "video" || as_url { x.url.clone() } else { x.data_url() };
        let r2 = roles.get(&role).map(py::str_of).unwrap_or(role);
        media.push(json!({"url": url, "type": x.kind(), "role": r2}));
    }
    v.insert("media".into(), if media.is_empty() { Value::Null } else { json!(media) });
    v
}

// ---------- 调接口 ----------
pub fn client(timeout: f64) -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs_f64(timeout))
        .redirect(reqwest::redirect::Policy::limited(20))
        .build()
        .unwrap_or_default()
}

/// 接口报错 → “HTTP 400：……”
pub fn http_error(status: u16, text: &str, where_: &str) -> String {
    let d: Option<Value> = serde_json::from_str(text).ok();
    let mut msg: Option<String> = None;
    match &d {
        Some(d @ Value::Object(_)) => {
            let e = &d["error"];
            let cand = [
                if e.is_object() { e["message"].clone() } else { e.clone() },
                d["message"].clone(),
                d["detail"].clone(),
                if where_.is_empty() { Value::Null } else { pick(d, where_) },
                pick(d, "output.message"),
            ];
            msg = cand.into_iter().find(py::truthy).map(|v| py::str_of(&v));
        }
        _ if !text.is_empty() => msg = Some(py::head(text, 200)),
        _ => {}
    }
    format!("HTTP {status}{}", msg.map(|m| format!("：{m}")).unwrap_or_default())
}

async fn call(c: &reqwest::Client, provider: &str, req: &Value, v: &Map<String, Value>, refs: &[Ref]) -> Result<Value, String> {
    let b = base(provider);
    let key = providers::api_key(provider);
    let path = py::str_of(&render(&req["path"].clone().as_str().map(|s| json!(s)).unwrap_or(json!("")), v));
    let url = if path.starts_with("http://") || path.starts_with("https://") { path } else { format!("{b}{path}") };
    let method = req["method"].as_str().filter(|s| !s.is_empty()).unwrap_or("POST").to_uppercase();
    let mut rb = c.request(reqwest::Method::from_bytes(method.as_bytes()).unwrap_or(reqwest::Method::POST), &url);
    if !key.is_empty() {
        rb = rb.header("Authorization", format!("Bearer {key}"));
    }
    for (k, x) in render(&req.get("headers").cloned().unwrap_or(json!({})), v).as_object().cloned().unwrap_or_default() {
        rb = rb.header(k, py::str_of(&x));
    }
    if req.get("form").is_some() {
        let mut form = reqwest::multipart::Form::new();
        for (k, x) in render(&req["form"], v).as_object().cloned().unwrap_or_default() {
            form = form.text(k, py::str_of(&x));
        }
        for (fname, which) in req["files"].as_object().cloned().unwrap_or_default() {
            if which != "images" {
                continue;
            }
            for (i, x) in refs.iter().filter(|y| y.kind() == "image").enumerate() {
                let ext = x.mime.split('/').nth(1).unwrap_or("png").replace("jpeg", "jpg");
                let part = reqwest::multipart::Part::bytes(x.data.clone())
                    .file_name(format!("image{}.{ext}", i + 1))
                    .mime_str(&x.mime)
                    .unwrap_or_else(|_| reqwest::multipart::Part::bytes(x.data.clone()));
                form = form.part(fname.clone(), part);
            }
        }
        rb = rb.multipart(form);
    } else if req.get("json").is_some() {
        rb = rb.json(&render(&req["json"], v));
    }
    let r = rb
        .send()
        .await
        .map_err(|e| if e.is_timeout() { t!("连接超时：{url}", url = b.clone()) } else { t!("连不上 {url}", url = b.clone()) })?;
    let status = r.status().as_u16();
    let text = r.text().await.unwrap_or_default();
    if status >= 400 {
        return Err(http_error(status, &text, ""));
    }
    serde_json::from_str(&text).map_err(|_| t!("返回的不是 JSON：{text}", text = py::head(&text, 120)))
}

async fn fetch_result(c: &reqwest::Client, found: &Value, limit: usize, key: &str) -> Result<Vec<u8>, String> {
    let b = match found.as_str() {
        Some(s) if s.starts_with("http://") || s.starts_with("https://") => {
            let mut request = c.get(s);
            if !key.is_empty() {
                request = request.bearer_auth(key);
            }
            let mut got = request.send().await.map_err(|e| e.to_string())?;
            if got.status().as_u16() >= 400 {
                return Err(format!("HTTP {}", got.status().as_u16()));
            }
            let mut buf = Vec::new();
            while let Some(chunk) = got.chunk().await.map_err(|e| e.to_string())? {
                if buf.len() + chunk.len() > limit {
                    return Err(t!("服务没有返回结果"));
                }
                buf.extend_from_slice(&chunk);
            }
            buf
        }
        _ => {
            let s = if found.is_null() { String::new() } else { py::str_of(found) };
            base64_loose(&s)
        }
    };
    if b.is_empty() || b.len() > limit {
        return Err(t!("服务没有返回结果"));
    }
    Ok(b)
}

/// base64.b64decode(validate=False)：不是 base64 字符的丢掉
fn base64_loose(s: &str) -> Vec<u8> {
    let clean: String = s.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '+' || *c == '/' || *c == '=').collect();
    let clean = clean.trim_end_matches('=');
    let pad = (4 - clean.len() % 4) % 4;
    crate::util::b64_decode(&format!("{clean}{}", "=".repeat(pad))).unwrap_or_default()
}

fn usable(provider: &str) -> Result<(), String> {
    if !providers::exists(provider) || !providers::usable(provider) {
        return Err(t!("没有这个模型服务：{name}", name = provider));
    }
    Ok(())
}

/// 画一张，返回 (原图, mime)
pub async fn draw(prompt: &str, provider: &str, model: &str, refs: &[Ref]) -> Result<(Vec<u8>, String), String> {
    let prompt = py::head(prompt.trim(), 1000);
    let refs: Vec<Ref> =
        refs.iter().map(|x| if x.role.is_empty() { Ref { role: "image".into(), ..x.clone() } } else { x.clone() }).collect();
    if prompt.is_empty() {
        return Err(t!("写一句想要画什么"));
    }
    usable(provider)?;
    providers::inventory(provider, false).await?;
    let r = recipe("image", provider, model).ok_or_else(|| t!("{model} 不是画图的模型", model = model))?;
    let why = check(&caps("image", provider, model), &refs, model);
    if !why.is_empty() {
        return Err(why);
    }
    let req = if !refs.is_empty() && py::truthy(&r["edit"]) { r["edit"].clone() } else { r["request"].clone() };
    let c = client(180.0);
    let got = async {
        let d = call(&c, provider, &req, &variables(&r, provider, model, &prompt, &refs, None, None), &refs).await?;
        let paths = if py::truthy(&req["result"]) {
            req["result"].clone()
        } else if py::truthy(&r["result"]) {
            r["result"].clone()
        } else {
            json!([])
        };
        fetch_result(&c, &first(&d, &paths), MAX_IMAGE, "").await
    }
    .await
    .map_err(|e| t!("画图失败：{e}", e = e))?;
    let mime = crate::media::sniff(&got).filter(|m| m.starts_with("image/")).ok_or_else(|| t!("画图服务没有返回图片"))?;
    record("image", provider, model, image_price(provider, model));
    Ok((got, mime.to_string()))
}

/// 提交一个视频任务，返回 job
pub async fn submit(
    provider: &str,
    model: &str,
    prompt: &str,
    refs: &[Ref],
    seconds: Option<i64>,
    ratio: Option<&str>,
) -> Result<Value, String> {
    usable(provider)?;
    providers::inventory(provider, false).await?;
    let r = recipe("video", provider, model).ok_or_else(|| t!("{model} 不是生成视频的模型", model = model))?;
    let why = check(&caps("video", provider, model), refs, model);
    if !why.is_empty() {
        return Err(why);
    }
    let v = variables(&r, provider, model, prompt, refs, seconds, ratio);
    let d = call(&client(60.0), provider, &r["request"], &v, refs).await?;
    let tid = pick(&d, r["task"].as_str().filter(|s| !s.is_empty()).unwrap_or("id"));
    if !py::truthy(&tid) {
        return Err(t!("服务没有返回任务编号"));
    }
    let mut job =
        json!({"provider": provider, "model": model, "id": py::str_of(&tid), "seconds": v.get("seconds"), "ratio": v.get("ratio")});
    if r == DEFAULTS["magpie"]["video"] {
        job["magpie"] = json!(true);
    }
    Ok(job)
}

pub enum Poll {
    Running(Option<String>),
    Done(Vec<u8>),
    Failed(String),
}

/// 查一次视频任务
pub async fn poll(job: &Value) -> Poll {
    let provider = job["provider"].as_str().unwrap_or("");
    let model = job["model"].as_str().unwrap_or("");
    let rc = if job["magpie"] == true { Some(DEFAULTS["magpie"]["video"].clone()) } else { recipe("video", provider, model) };
    let Some(r) = rc else {
        return Poll::Failed(t!("找不到 {model} 的配方", model = model));
    };
    let p = &r["poll"];
    let c = client(60.0);
    let mut v = Map::new();
    v.insert("id".into(), job["id"].clone());
    v.insert("model".into(), json!(model));
    v.insert("origin".into(), json!(origin_of(&base(provider))));
    let d = match call(&c, provider, &p["request"], &v, &[]).await {
        Ok(d) => d,
        Err(e) => return Poll::Running(Some(e)),
    };
    let status = py::str_of(&pick(&d, p["status"].as_str().filter(|s| !s.is_empty()).unwrap_or("status")));
    let status =
        if pick(&d, p["status"].as_str().filter(|s| !s.is_empty()).unwrap_or("status")).is_null() { String::new() } else { status };
    let list = |k: &str| -> Vec<String> { p[k].as_array().map(|a| a.iter().map(py::str_of).collect()).unwrap_or_default() };
    if list("failed").contains(&status) {
        let e = first(&d, &p.get("error").cloned().unwrap_or(json!([])));
        return Poll::Failed(if py::truthy(&e) { py::str_of(&e) } else { status });
    }
    if !list("done").contains(&status) {
        return Poll::Running(None);
    }
    let (found, key) = if let Some(path) = r["content_path"].as_str() {
        let path = py::str_of(&render(&json!(path), &v));
        (json!(format!("{}/{}", base(provider), path.trim_start_matches('/'))), providers::api_key(provider))
    } else {
        (first(&d, &r["result"]), String::new())
    };
    match fetch_result(&c, &found, MAX_VIDEO, &key).await {
        Ok(b) => Poll::Done(b),
        Err(e) => Poll::Failed(e),
    }
}

/// 画了一张、生成了一段：费用统计里记一行（0 token）
pub fn record(kind: &str, provider: &str, model: &str, cost: Option<f64>) {
    memory::add_usage(
        kind,
        model,
        memory::UsageRec { provider: Some(provider.to_string()), cost: cost.unwrap_or(0.0), ..Default::default() },
    );
}

// ---------- 临时公网地址 ----------
fn secret() -> Vec<u8> {
    if memory::setting_raw("media_link_secret").is_none() {
        crate::db::exec("insert or ignore into settings values('media_link_secret', ?)", rusqlite::params![crate::util::token_hex(32)]);
    }
    let s = memory::setting_raw("media_link_secret").unwrap_or_default();
    (0..s.len() / 2).filter_map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).ok()).collect()
}

pub fn sign(mid: &str, exp: i64) -> String {
    let mut m = Hmac::<Sha256>::new_from_slice(&secret()).expect("hmac");
    m.update(format!("{mid}:{exp}").as_bytes());
    m.finalize().into_bytes().iter().map(|b| format!("{b:02x}")).collect::<String>()[..32].to_string()
}

pub fn verify(mid: &str, exp: i64, sig: &str) -> bool {
    let want = sign(mid, exp);
    exp as f64 >= py::now() && want.len() == sig.len() && want.bytes().zip(sig.bytes()).fold(0u8, |a, (x, y)| a | (x ^ y)) == 0
}

/// 网页的公网地址（设置 → 聊天软件 → 网页地址）；外面打不开的返回空
pub fn public_base() -> String {
    let u = crate::channels::public_url();
    let host =
        reqwest::Url::parse(&u).ok().and_then(|x| x.host_str().map(|h| h.trim_matches(['[', ']']).to_lowercase())).unwrap_or_default();
    if host.is_empty() || host == "localhost" || host.ends_with(".local") {
        return String::new();
    }
    if let Ok(ip) = host.parse::<std::net::IpAddr>() {
        let bad = match ip {
            std::net::IpAddr::V4(v) => v.is_private() || v.is_loopback() || v.is_link_local(),
            std::net::IpAddr::V6(v) => v.is_loopback() || (v.segments()[0] & 0xfe00) == 0xfc00 || (v.segments()[0] & 0xffc0) == 0xfe80,
        };
        if bad {
            return String::new();
        }
    }
    u.trim_end_matches('/').to_string()
}

/// 给视频服务用的临时地址（6 小时内有效），不用登录
pub fn public_link(mid: &str) -> String {
    let b = public_base();
    if b.is_empty() {
        return String::new();
    }
    let exp = py::now() as i64 + LINK_S;
    format!("{b}/pub/media/{mid}?exp={exp}&sig={}", sign(mid, exp))
}
