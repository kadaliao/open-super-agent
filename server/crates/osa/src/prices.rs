//! 模型单价和能力：自动从 models.dev 获取，每天更新一次，存一份在数据目录（离线时用上一次的）。对应 osa/prices.py。
//!
//! 单价是美元 / 百万 token，按 pricing.usd_rate 换成人民币。config.yaml 的 pricing.models 里写了的模型以它为准
//! （DeepSeek 分高峰、空闲两档）。本机和内网的模型不收钱，按 0 计。

use crate::{config, providers, py};
use parking_lot::{Mutex, RwLock};
use regex::Regex;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::LazyLock;

pub const TTL: f64 = 86400.0;
const ALIAS: [(&str, &str); 7] = [
    ("openai", "openai"),
    ("deepseek", "deepseek"),
    ("openrouter", "openrouter"),
    ("dashscope", "alibaba-cn"),
    ("zhipu", "zhipuai"),
    ("moonshot", "moonshotai-cn"),
    ("siliconflow", "siliconflow-cn"),
];
const PREFER: [&str; 8] = ["openai", "deepseek", "alibaba-cn", "zhipuai", "moonshotai-cn", "siliconflow-cn", "volcengine", "openrouter"];

pub fn source() -> String {
    let s = config::ORIGINAL["pricing"]["source"].as_str().unwrap_or("").to_string();
    if s.is_empty() { "https://models.dev/api.json".into() } else { s }
}

fn file() -> PathBuf {
    config::DB_PATH.parent().map(|d| d.join("model-prices.json")).unwrap_or_else(|| "model-prices.json".into())
}

static DATA: LazyLock<RwLock<Value>> = LazyLock::new(|| {
    let v =
        std::fs::read_to_string(file()).ok().and_then(|s| serde_json::from_str::<Value>(&s).ok()).filter(|v| v["providers"].is_object());
    RwLock::new(v.unwrap_or(json!({"ts": 0, "providers": {}})))
});
static STATUS: Mutex<(String, f64)> = Mutex::new((String::new(), 0.0));
static MATCHED: LazyLock<Mutex<HashMap<(String, String, String), Option<String>>>> = LazyLock::new(Default::default);

/// 1 美元换多少设置的货币（Python 版的 prices._rate）
pub fn usd_rate() -> f64 {
    rate()
}

fn rate() -> f64 {
    let p = config::get("pricing");
    match p["usd_rate"].as_f64().filter(|r| *r != 0.0) {
        Some(r) => r,
        None => {
            if matches!(p["currency"].as_str(), Some("$") | Some("USD")) {
                1.0
            } else {
                7.1
            }
        }
    }
}

fn norm(url: &str) -> String {
    let u = url.to_lowercase();
    let u = u.split_once("://").map(|x| x.1.to_string()).unwrap_or(u);
    let u = u.trim_end_matches('/');
    u.strip_suffix("/v1").unwrap_or(u).to_string()
}

fn compact(raw: &Value) -> Value {
    let mut out = Map::new();
    for (pid, p) in raw.as_object().cloned().unwrap_or_default() {
        let mut ms = Map::new();
        for (mid, m) in p["models"].as_object().cloned().unwrap_or_default() {
            let c = &m["cost"];
            if c["input"].is_null() && m["type"] != "embedding" {
                continue;
            }
            let vision = m["modalities"]["input"].as_array().is_some_and(|a| a.iter().any(|x| x == "image"));
            ms.insert(
                mid,
                json!({"i": c["input"], "o": c["output"], "c": c["cache_read"], "v": vision as i64,
                                  "t": m["type"].as_str().unwrap_or(""), "n": m["name"].as_str().unwrap_or("")}),
            );
        }
        let api = p["api"].as_str().unwrap_or("").to_string();
        let name = p["name"].as_str().filter(|s| !s.is_empty()).map(String::from).unwrap_or_else(|| pid.clone());
        out.insert(pid, json!({"api": api, "name": name, "models": ms}));
    }
    Value::Object(out)
}

pub async fn refresh() -> Value {
    let r: Result<Value, String> = async {
        let c = reqwest::Client::builder().timeout(std::time::Duration::from_secs(60)).build().map_err(|e| e.to_string())?;
        let r = c.get(source()).send().await.map_err(|e| format!("ConnectError: {}", py::head(&e.to_string(), 160)))?;
        if !r.status().is_success() {
            return Err(format!("HTTPStatusError: {}", r.status()));
        }
        let raw: Value = r.json().await.map_err(|e| format!("JSONDecodeError: {}", py::head(&e.to_string(), 160)))?;
        Ok(json!({"ts": py::now(), "providers": compact(&raw)}))
    }
    .await;
    match r {
        Ok(data) => {
            let f = file();
            let tmp = f.with_extension("tmp");
            if std::fs::write(&tmp, serde_json::to_string(&data).unwrap_or_default()).is_ok() {
                let _ = std::fs::rename(&tmp, &f);
            }
            *DATA.write() = data;
            *STATUS.lock() = (String::new(), py::now());
        }
        Err(e) => *STATUS.lock() = (e, py::now()),
    }
    info()
}

pub async fn price_loop() {
    loop {
        let ts = DATA.read()["ts"].as_f64().unwrap_or(0.0);
        if py::now() - ts > TTL {
            refresh().await;
        }
        let err = !STATUS.lock().0.is_empty();
        tokio::time::sleep(std::time::Duration::from_secs(if err { 600 } else { 3600 })).await;
    }
}

pub fn info() -> Value {
    let d = DATA.read();
    let n: usize =
        d["providers"].as_object().map(|o| o.values().map(|p| p["models"].as_object().map(|m| m.len()).unwrap_or(0)).sum()).unwrap_or(0);
    json!({"source": source(), "updated": d.get("ts").cloned().unwrap_or(json!(0)), "error": STATUS.lock().0,
           "rate": rate(), "currency": config::get("pricing.currency"), "models": n})
}

// ---------- 对上服务商和模型 ----------
fn provider_cfg(provider: &str) -> Value {
    providers::provider(provider).unwrap_or(json!({}))
}

fn is_local(p: &Value) -> bool {
    providers::is_local(p["base_url"].as_str().unwrap_or(""))
}

/// 我们的模型服务在 models.dev 里是哪一家
pub fn matched(provider: &str) -> Option<String> {
    let p = provider_cfg(provider);
    if p.as_object().is_none_or(|o| o.is_empty()) || is_local(&p) {
        return None;
    }
    let base = p["base_url"].as_str().unwrap_or("").to_string();
    let preset = p["preset"].as_str().filter(|s| !s.is_empty()).unwrap_or(provider).to_string();
    let d = DATA.read();
    let key = (base.clone(), preset.clone(), d["ts"].to_string());
    if let Some(v) = MATCHED.lock().get(&key) {
        return v.clone();
    }
    let url = norm(&base);
    let ps = d["providers"].as_object().cloned().unwrap_or_default();
    let mut pid = ps.iter().find(|(_, x)| x["api"].as_str().is_some_and(|a| !a.is_empty() && norm(a) == url)).map(|(k, _)| k.clone());
    if pid.is_none() && url.contains("api.openai.com") && ps.contains_key("openai") {
        pid = Some("openai".into());
    }
    if pid.is_none() {
        pid = ALIAS.iter().find(|(k, _)| *k == preset).map(|(_, v)| v.to_string()).filter(|v| ps.contains_key(v));
    }
    MATCHED.lock().insert(key, pid.clone());
    pid
}

fn find(provider: &str, model: &str) -> Option<(Value, String)> {
    let pid = matched(provider);
    let d = DATA.read();
    let ps = &d["providers"];
    if let Some(pid) = pid {
        let m = &ps[&pid]["models"][model];
        return if m.is_object() { Some((m.clone(), pid)) } else { None };
    }
    let mut names = vec![model.to_string()];
    if let Some((_, rest)) = model.split_once('/') {
        names.push(rest.to_string());
    }
    let mut order: Vec<String> = PREFER.iter().map(|s| s.to_string()).collect();
    if let Some(o) = ps.as_object() {
        order.extend(o.keys().filter(|k| !PREFER.contains(&k.as_str())).cloned());
    }
    for name in &names {
        for p in &order {
            let m = &ps[p]["models"][name];
            if m.is_object() && !m["i"].is_null() {
                return Some((m.clone(), p.clone()));
            }
        }
    }
    None
}

fn manual(model: &str) -> Option<Value> {
    let m = config::get("pricing.models")[model].clone();
    if m.is_object() { Some(m) } else { None }
}

/// [高峰, 空闲] 里的一个（写成一个数的两档一样）
fn tier(v: &Value, k: usize) -> f64 {
    match v {
        Value::Array(a) => a.get(k).or(a.first()).and_then(|x| x.as_f64()).unwrap_or(0.0),
        v => v.as_f64().unwrap_or(0.0),
    }
}

fn tier_v(v: &Value, k: usize) -> Value {
    match v {
        Value::Array(a) => a.get(k).or(a.first()).cloned().unwrap_or(json!(0)),
        v => v.clone(),
    }
}

fn unit_price(table: &str, provider: &str, model: &str) -> Option<Value> {
    let base = provider_cfg(provider)["base_url"].as_str().unwrap_or("").to_lowercase();
    let kind = if table == "images" { "image" } else { "video" };
    if providers::is_local(&base) && providers::magpie_recipe(provider, kind, model).is_none() {
        return Some(json!(0.0));
    }
    let tail = model.rsplit('/').next().unwrap_or(model).to_lowercase();
    let tables = config::get(&format!("pricing.{table}"));
    for (host, t) in tables.as_object().cloned().unwrap_or_default() {
        if !base.contains(&host.to_lowercase()) && host != provider {
            continue;
        }
        for (name, price) in t.as_object().cloned().unwrap_or_default() {
            let n = name.to_lowercase();
            if n == model.to_lowercase() || (!n.contains('/') && n == tail) {
                return Some(price);
            }
        }
    }
    None
}

/// 画一张图多少钱（pricing.images）；本机的算 0，没写的返回 None
pub fn image_price(provider: &str, model: &str) -> Option<f64> {
    unit_price("images", provider, model).map(|p| p.as_f64().unwrap_or(0.0))
}

/// 生成一段视频多少钱（pricing.videos）：数字是每段，{per_second: x} 乘时长（不知道按 5 秒）
pub fn video_price(provider: &str, model: &str, seconds: Option<i64>) -> Option<f64> {
    let p = unit_price("videos", provider, model)?;
    if p.is_object() {
        let per = p["per_second"].as_f64().unwrap_or(0.0);
        return Some(per * seconds.filter(|s| *s != 0).unwrap_or(5) as f64);
    }
    Some(p.as_f64().unwrap_or(0.0))
}

/// 界面上显示用：单价（元 / 百万 token）、能不能看图、是不是 embedding、单价从哪来
pub fn lookup(provider: &str, model: &str) -> Value {
    let p = provider_cfg(provider);
    let mut out = json!({"source": ""});
    if let Some((m, pid)) = find(provider, model) {
        let r = rate();
        out["vision"] = json!(py::truthy(&m["v"]));
        out["kind"] = json!(m["t"].as_str().filter(|s| !s.is_empty()).unwrap_or("chat"));
        out["name"] = m["n"].clone();
        if let Some(i) = m["i"].as_f64() {
            out["input"] = json!(py::round_n(i * r, 4));
            out["output"] = json!(py::round_n(m["o"].as_f64().unwrap_or(0.0) * r, 4));
            out["cache"] = match m["c"].as_f64() {
                Some(c) => json!(py::round_n(c * r, 4)),
                None => Value::Null,
            };
            out["source"] = json!("models.dev");
            out["via"] = if matched(provider).is_some() { json!("") } else { DATA.read()["providers"][&pid]["name"].clone() };
        }
    }
    if let Some(man) = manual(model) {
        out["input"] = tier_v(&man["input"], 0);
        out["output"] = tier_v(&man["output"], 0);
        out["cache"] = tier_v(&man["cache_hit"], 0);
        out["offpeak"] = json!([tier_v(&man["input"], 1), tier_v(&man["output"], 1)]);
        out["source"] = json!("config");
    }
    if p.as_object().is_some_and(|o| !o.is_empty()) && is_local(&p) {
        out["input"] = json!(0);
        out["output"] = json!(0);
        out["cache"] = json!(0);
        out["source"] = json!("local");
    }
    out
}

fn guess_kind(mid: &str) -> &'static str {
    static EMB: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"embed|bge-|/bge|^bge|m3e|gte-|\be5-").unwrap());
    static OTHER: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"tts|whisper|dall-e|gpt-image|sora|wanx|cogview|seedream|seedance|kolors|flux|stable-diffusion|asr|speech|audio")
            .unwrap()
    });
    let x = mid.to_lowercase();
    if EMB.is_match(&x) {
        "embedding"
    } else if x.contains("rerank") {
        "reranking"
    } else if OTHER.is_match(&x) {
        "other"
    } else {
        "chat"
    }
}

/// 模型列表加上单价、能不能看图、是对话还是 embedding
pub fn annotate(provider: &str, ids: &[String]) -> Vec<Value> {
    let local = is_local(&provider_cfg(provider));
    let r = rate();
    let mut out = vec![];
    for mid in ids {
        let mut item = json!({"id": mid, "kind": guess_kind(mid)});
        if let Some((m, _)) = if local { None } else { find(provider, mid) } {
            let t = m["t"].as_str().unwrap_or("");
            item["vision"] = json!(py::truthy(&m["v"]));
            item["kind"] = json!(if ["embedding", "chat", "reranking"].contains(&t) {
                t
            } else if !t.is_empty() {
                "other"
            } else {
                "chat"
            });
            if let Some(i) = m["i"].as_f64() {
                item["input"] = json!(py::round_n(i * r, 4));
                item["output"] = json!(py::round_n(m["o"].as_f64().unwrap_or(0.0) * r, 4));
            }
        }
        if let Some(man) = manual(mid) {
            item["input"] = tier_v(&man["input"], 0);
            item["output"] = tier_v(&man["output"], 0);
        }
        if local {
            item["input"] = json!(0);
            item["output"] = json!(0);
        }
        out.push(item);
    }
    out
}

/// 服务本身不列模型时，用 models.dev 里这一家的模型列表顶上
pub fn models_of(provider: &str) -> Vec<String> {
    let Some(pid) = matched(provider) else { return vec![] };
    let mut v: Vec<String> = DATA.read()["providers"][&pid]["models"].as_object().map(|m| m.keys().cloned().collect()).unwrap_or_default();
    v.sort();
    v
}

pub fn cost(provider: &str, model: &str, hit: i64, miss: i64, out: i64, peak: bool) -> f64 {
    if let Some(man) = manual(model) {
        let k = if peak { 0 } else { 1 };
        return (hit as f64 * tier(&man["cache_hit"], k) + miss as f64 * tier(&man["input"], k) + out as f64 * tier(&man["output"], k))
            / 1e6;
    }
    let p = provider_cfg(provider);
    if p.as_object().is_some_and(|o| !o.is_empty()) && is_local(&p) {
        return 0.0;
    }
    let Some((m, _)) = find(provider, model) else { return 0.0 };
    let Some(i) = m["i"].as_f64() else { return 0.0 };
    let cache = m["c"].as_f64().unwrap_or(i);
    (hit as f64 * cache + miss as f64 * i + out as f64 * m["o"].as_f64().unwrap_or(0.0)) / 1e6 * rate()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn kinds() {
        assert_eq!(guess_kind("BAAI/bge-m3"), "embedding");
        assert_eq!(guess_kind("gpt-image-2"), "other");
        assert_eq!(guess_kind("deepseek-chat"), "chat");
        assert_eq!(norm("https://api.deepseek.com/v1/"), "api.deepseek.com");
    }
}
