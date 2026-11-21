//! 界面上可修改的设置，存在数据库里（settings 表的 overrides），覆盖 config.yaml 的同名项。对应 osa/settings.py。
//! 模型服务（地址、密钥）在 providers.rs；这里还有模型服务、单价的接口。

use crate::web::{ApiError, Body, Res, ok};
use crate::{config, i18n, media, memory, prices, providers, py, security};
use axum::extract::Path;
use axum::routing::{get, post, put};
use serde_json::{Map, Value, json};
use std::collections::HashMap;

pub const EDITABLE_LIMITS: [&str; 4] = ["daily_token_budget", "worker_max_steps", "task_timeout_s", "approval_timeout_s"];

pub fn role_names() -> HashMap<&'static str, &'static str> {
    HashMap::from([
        ("router", "对话"),
        ("worker", "后台任务"),
        ("reviewer", "Heavy 汇总"),
        ("filter", "整理和过滤"),
        ("embedding", "按意思检索"),
    ])
}

/// config.yaml 里原来的电脑设置：界面上选回“自动”时用
fn orig_pc() -> Value {
    let p = &config::ORIGINAL["computer"];
    if p.is_object() { p.clone() } else { json!({}) }
}

fn load() -> Map<String, Value> {
    memory::setting("overrides").and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

// 电脑：用哪种、能用哪几种（computer::pc）
pub use crate::computer::pc;

pub fn apply() {
    providers::apply();
    let o = load();
    let orig = orig_pc();
    config::with_mut(|c| {
        if let Some(roles) = o.get("roles").and_then(|r| r.as_object()) {
            for (role, v) in roles {
                if let Some(r) = c["roles"].get_mut(role).and_then(|r| r.as_object_mut()) {
                    for k in ["provider", "model", "thinking", "reasoning_effort"] {
                        if let Some(x) = v.get(k) {
                            r.insert(k.into(), x.clone());
                        }
                    }
                }
            }
        }
        if let Some(l) = o.get("limits").and_then(|l| l.as_object()) {
            for (k, v) in l {
                if EDITABLE_LIMITS.contains(&k.as_str()) {
                    c["limits"][k] = v.clone();
                }
            }
        }
        if let Some(h) = o.get("heavy_workers") {
            c["heavy"]["workers"] = h.clone();
        }
        if !c["computer"].is_object() {
            c["computer"] = json!({});
        }
        if let Some(p) = o.get("computer_parallel") {
            c["computer"]["parallel"] = p.clone();
        }
        let b =
            o.get("computer_backend").and_then(|b| b.as_str()).filter(|b| pc::BACKENDS.contains(b) && (*b != "host" || pc::host_allowed()));
        c["computer"]["backend"] = match b {
            Some(b) => json!(b),
            None => orig.get("backend").cloned().unwrap_or(json!("auto")),
        };
        c["computer"]["host_shell"] = match o.get("computer_host_shell") {
            Some(v) => v.clone(),
            None => json!(py::truthy(&orig["host_shell"])),
        };
        if let Some(e) = o.get("embedding") {
            c["embedding"] = e.clone();
        }
    });
}

/// 对话或后台任务用的模型服务没法用时，说一句为什么；能用返回空
pub fn problem() -> String {
    let names = role_names();
    for role in ["router", "worker"] {
        let name = config::get_str(&format!("roles.{role}.provider"));
        if !providers::exists(&name) {
            return crate::t!("{role}用的模型服务 {name} 不存在了", role = i18n::t0(names[role]), name = name);
        }
        if !providers::usable(&name) {
            return crate::t!("{name} 还没填 API Key", name = providers::describe(&name)["title"].clone());
        }
    }
    String::new()
}

pub fn onboarding() -> bool {
    if crate::db::exists("select 1 from settings where key='onboarding'", []) {
        return false;
    }
    !crate::db::exists("select 1 from messages where role='user' limit 1", [])
}

pub fn finish_onboarding() {
    memory::set_setting("onboarding", &json!({"done": py::now()}));
}

fn info(provider: &str, model: &str) -> Value {
    let mut out = prices::lookup(provider, model);
    if let Some(seen) = media::tested(provider, model) {
        out["vision"] = json!(seen);
    }
    out
}

/// 存在 settings 表里的 {provider, model}（画图、视频用哪个模型；avatars.choice / videos.choice）
pub fn public() -> Value {
    let emb = config::get("embedding");
    let s = |v: &Value| if py::truthy(v) { py::str_of(v) } else { String::new() };
    let mut used: Vec<(String, String)> = config::get("roles")
        .as_object()
        .map(|o| o.values().map(|v| (py::str_of(&v["provider"]), py::str_of(&v["model"]))).collect())
        .unwrap_or_default();
    if py::truthy(&emb["provider"]) && py::truthy(&emb["model"]) {
        used.push((s(&emb["provider"]), s(&emb["model"])));
    }
    let mut model_info = Map::new();
    for (p, m) in used {
        model_info.insert(format!("{p}/{m}"), info(&p, &m));
    }
    let limits: Map<String, Value> = EDITABLE_LIMITS.iter().map(|k| (k.to_string(), config::get(&format!("limits.{k}")))).collect();
    let pc_cfg = config::get("computer");
    json!({
        "providers": providers::public(),
        "presets": providers::presets(),
        "roles": config::get("roles"),
        "limits": limits,
        "heavy_workers": config::get("heavy.workers"),
        "computer_parallel": pc_cfg.get("parallel").cloned().unwrap_or(json!(3)),
        "computer_backend": pc_cfg.get("backend").cloned().unwrap_or(json!("auto")),
        "computer_backends": pc::available(),
        "computer_current": pc::backend_name(),
        "computer_choices": pc::choices(),
        "computer_on_desktop": std::env::var("OSA_DESKTOP").is_ok_and(|v| !v.is_empty()),
        "computer_host_shell": py::truthy(&pc_cfg["host_shell"]),
        "embedding": {"provider": s(&emb["provider"]), "model": s(&emb["model"])},
        "image_model": crate::avatars::choice(false),
        "image_edit_model": crate::avatars::choice(true),
        "video_model": crate::videos::choice(),
        "model_info": model_info,
        "prices": prices::info(),
        "problem": problem(),
        "language": i18n::lang(),
        "security": security::public(),
    })
}

fn int_of(v: &Value) -> Result<i64, String> {
    py::to_int(v).ok_or_else(|| format!("invalid literal for int() with base 10: {}", py::repr(v)))
}

fn pm(e: &Value) -> (String, String) {
    let s = |v: &Value| if py::truthy(v) { py::str_of(v) } else { String::new() };
    (s(&e["provider"]).trim().to_string(), py::head(s(&e["model"]).trim(), 200))
}

pub fn save(data: &Value) -> Result<Value, String> {
    let has = |k: &str| data.get(k).is_some();
    if security::demo() && (has("limits") || has("heavy_workers") || has("computer_parallel")) {
        return Err(crate::t!("演示服务器上不能改用量上限"));
    }
    if !security::local_exec() && (data["computer_backend"] == "host" || py::truthy(&data["computer_host_shell"])) {
        return Err(security::no_local_exec());
    }
    if data["computer_backend"] == "host" && !pc::host_allowed() {
        return Err(crate::t!("服务器上不能用服务器自己当电脑，用 Docker 沙盒或云电脑"));
    }
    if has("language") {
        i18n::set_lang(&py::str_of(&data["language"]))?;
    }
    let mut o = load();
    o.remove("computer_linked_shell");
    if o.get("computer_backend").is_some_and(|b| b == "linked") {
        o.remove("computer_backend");
    }
    if has("roles") {
        let roles_cfg = config::get("roles");
        let entry = o.entry("roles").or_insert(json!({}));
        for (role, v) in data["roles"].as_object().cloned().unwrap_or_default() {
            if roles_cfg.get(&role).is_none() {
                continue;
            }
            let mut v: Map<String, Value> = ["provider", "model", "thinking", "reasoning_effort"]
                .iter()
                .filter_map(|k| v.get(*k).map(|x| (k.to_string(), x.clone())))
                .collect();
            if let Some(p) = v.get("provider") {
                let p = py::str_of(p);
                if !providers::exists(&p) {
                    return Err(crate::t!("没有这个模型服务：{name}", name = p));
                }
            }
            if let Some(m) = v.get("model") {
                let m = py::head(if py::truthy(m) { py::str_of(m) } else { String::new() }.trim(), 200);
                if m.is_empty() {
                    return Err(crate::t!("模型名字不能为空"));
                }
                v.insert("model".into(), json!(m));
            }
            let r = entry.as_object_mut().unwrap().entry(role).or_insert(json!({}));
            for (k, x) in v {
                r[k] = x;
            }
        }
    }
    if has("limits") {
        let mut l = Map::new();
        for (k, v) in data["limits"].as_object().cloned().unwrap_or_default() {
            if EDITABLE_LIMITS.contains(&k.as_str()) {
                l.insert(k, json!(int_of(&v)?));
            }
        }
        let e = o.entry("limits").or_insert(json!({}));
        for (k, v) in l {
            e[k] = v;
        }
    }
    if has("heavy_workers") {
        o.insert("heavy_workers".into(), json!(int_of(&data["heavy_workers"])?.clamp(1, 8)));
    }
    if has("computer_parallel") {
        o.insert("computer_parallel".into(), json!(int_of(&data["computer_parallel"])?.clamp(1, 8)));
    }
    if has("computer_backend") {
        let b = if py::truthy(&data["computer_backend"]) { py::str_of(&data["computer_backend"]) } else { "auto".into() };
        if b != "auto" && !pc::BACKENDS.contains(&b.as_str()) {
            return Err(crate::t!("没有这种电脑：{name}", name = b));
        }
        let cur = config::get_str("computer.backend");
        if pc::BACKENDS.contains(&b.as_str()) && !pc::choices().contains(&b.as_str()) && b != cur {
            return Err(crate::t!("这台服务器上不能选这种电脑（config.yaml 的 computer.choices）"));
        }
        let okm: HashMap<String, bool> = pc::available().iter().map(|x| (py::str_of(&x["id"]), x["ok"] == true)).collect();
        if b == "instabox" && !okm.get("instabox").copied().unwrap_or(false) {
            return Err(crate::t!("还没填 Instabox 的 API Key"));
        }
        if b == "local" {
            let why = pc::docker_problem();
            if !why.is_empty() {
                return Err(why);
            }
        }
        let c = crate::computer::get(0);
        let busy = {
            let st = c.st.lock();
            !st.users.is_empty() || !st.exclusive.is_empty()
        };
        if busy && b != config::get_str("computer.backend") {
            return Err(crate::t!("电脑正在被用着（{who}），等用完再换", who = c.busy()));
        }
        if b == "auto" {
            o.remove("computer_backend");
        } else {
            o.insert("computer_backend".into(), json!(b));
        }
    }
    if has("computer_host_shell") {
        o.insert("computer_host_shell".into(), json!(py::truthy(&data["computer_host_shell"])));
    }
    if has("embedding") {
        let (provider, model) = pm(&data["embedding"]);
        if !provider.is_empty() && !providers::exists(&provider) {
            return Err(crate::t!("没有这个模型服务：{name}", name = provider));
        }
        if !provider.is_empty() && model.is_empty() {
            return Err(crate::t!("选了服务就要填 embedding 模型的名字"));
        }
        o.insert("embedding".into(), json!({"provider": provider, "model": model}));
    }
    for k in ["image_model", "image_edit_model"] {
        if !has(k) {
            continue;
        }
        let (provider, model) = pm(&data[k]);
        if !provider.is_empty() && !providers::exists(&provider) {
            return Err(crate::t!("没有这个模型服务：{name}", name = provider));
        }
        if !provider.is_empty() && crate::generation::recipe("image", &provider, &model).is_none() {
            return Err(crate::t!("{model} 不是画图的模型", model = model));
        }
        if !provider.is_empty() && k == "image_edit_model" && crate::generation::caps("image", &provider, &model).images == 0 {
            return Err(crate::t!("{model} 不能照着图片画", model = model));
        }
        crate::avatars::choose(&provider, &model, k == "image_edit_model");
    }
    if has("video_model") {
        let (provider, model) = pm(&data["video_model"]);
        if !provider.is_empty() && !providers::exists(&provider) {
            return Err(crate::t!("没有这个模型服务：{name}", name = provider));
        }
        if !provider.is_empty() && crate::generation::recipe("video", &provider, &model).is_none() {
            return Err(crate::t!("{model} 不是生成视频的模型", model = model));
        }
        crate::videos::choose(&provider, &model);
    }
    memory::set_setting("overrides", &Value::Object(o));
    apply();
    if has("embedding") {
        crate::search::wake(); // 换了模型：后台重新算向量
    }
    Ok(public())
}

pub async fn update(d: &Value) -> Result<Value, String> {
    for field in ["image_model", "image_edit_model", "video_model"] {
        if let Some(name) = d[field]["provider"].as_str().map(str::trim).filter(|s| !s.is_empty()) {
            providers::inventory(name, false).await?;
        }
    }
    save(d)
}

// ---------- 接口 ----------
pub fn routes() -> axum::Router {
    axum::Router::new()
        .route("/api/settings", get(get_settings).put(put_settings))
        .route("/api/onboarding/done", post(onboarding_done))
        .route("/api/providers/{name}/models", get(provider_models))
        .route("/api/providers/check", post(check_provider))
        .route("/api/providers", post(add_provider))
        .route("/api/providers/{name}", put(update_provider).delete(delete_provider))
        .route("/api/models/test", post(test_model))
        .route("/api/prices", get(price_info))
        .route("/api/prices/refresh", post(refresh_prices))
}

async fn get_settings() -> Res {
    ok(public())
}

async fn put_settings(Body(d): Body) -> Res {
    if !d.is_object() {
        return Err(ApiError::new(422, "Input should be a valid dictionary"));
    }
    let out = update(&d).await?;
    if d.get("computer_parallel").is_some() {
        crate::computer::get(0).recheck();
    }
    if ["computer_parallel", "computer_backend", "computer_host_shell"].iter().any(|k| d.get(*k).is_some()) {
        crate::server::broadcast(json!({"type": "computer", "computer": crate::computer::public_state(0).await}));
    }
    ok(out)
}

async fn onboarding_done() -> Res {
    finish_onboarding();
    ok(json!({"ok": true}))
}

async fn provider_models(Path(name): Path<String>, q: crate::web::Q) -> Res {
    let refresh = q.get("refresh").is_some_and(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "on" | "y" | "t"));
    ok(providers::models(&name, refresh).await?)
}

async fn check_provider(Body(d): Body) -> Res {
    let url = providers::clean_url(d["base_url"].as_str().unwrap_or(""))?;
    security::check_provider_url(&url).await?;
    let mut key = d["api_key"].as_str().unwrap_or("").trim().to_string();
    let name = if py::truthy(&d["name"]) { py::str_of(&d["name"]) } else { String::new() };
    if key.is_empty()
        && providers::exists(&name)
        && providers::same_origin(&url, providers::describe(&name)["base_url"].as_str().unwrap_or(""))
    {
        key = providers::api_key(&name);
    }
    ok(providers::check(&url, &key).await)
}

async fn add_provider(Body(d): Body) -> Res {
    security::check_provider_url(d["base_url"].as_str().unwrap_or("")).await?;
    let name = providers::save(None, &d)?;
    ok(json!({"name": name, "settings": public()}))
}

async fn update_provider(Path(name): Path<String>, Body(d): Body) -> Res {
    security::check_provider_url(d["base_url"].as_str().unwrap_or("")).await?;
    providers::save(Some(&name), &d)?;
    ok(json!({"name": name, "settings": public()}))
}

async fn delete_provider(Path(name): Path<String>) -> Res {
    providers::delete(&name, &role_names())?;
    ok(json!({"settings": public()}))
}

async fn test_model(Body(d): Body) -> Res {
    let s = |k: &str, dflt: &str| d[k].as_str().map(String::from).unwrap_or_else(|| dflt.to_string());
    ok(providers::test(&s("provider", ""), &s("model", ""), &s("kind", "chat"), &s("role", "")).await?)
}

async fn price_info() -> Res {
    ok(prices::info())
}

async fn refresh_prices() -> Res {
    let mut r = prices::refresh().await;
    r["settings"] = public();
    ok(r)
}
