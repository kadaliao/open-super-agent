//! 手机推送（Web Push）：没人开着页面时，需要你处理的事推送到已订阅的设备。对应 osa/push.py。
//!
//! VAPID 私钥（PEM，PKCS8）存在 settings 表的 vapid 一行（原样的文字，和 Python 版同一把）；
//! 消息按 RFC 8291（aes128gcm）加密，VAPID 按 RFC 8292 签名。推送服务返回 404 / 410 的订阅直接删掉。
//! 推送服务是自签证书时（测试、内网），SSL_CERT_FILE 指向的证书也信任（和 Python 的 httpx 一样）。

use crate::db::{self, RowExt};
use crate::web::{ApiError, Body, Res, ok};
use crate::{config, memory, py, util};
use aes_gcm::aead::{Aead, KeyInit};
use axum::http::HeaderMap;
use axum::routing::{get, post};
use p256::ecdsa::{Signature, SigningKey, signature::Signer};
use p256::elliptic_curve::sec1::ToEncodedPoint;
use p256::pkcs8::{DecodePrivateKey, EncodePrivateKey, LineEnding};
use p256::{PublicKey, SecretKey};
use parking_lot::Mutex;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::LazyLock;
use std::time::Duration;

pub fn migrate() {
    db::batch("create table if not exists push_subs(endpoint text primary key, p256dh text, auth text, ua text default '', created real)");
}

fn subject() -> String {
    let s = config::get_str("push.subject");
    if s.is_empty() { "https://github.com/kadaliao/open-super-agent".into() } else { s }
}

pub fn b64u(b: &[u8]) -> String {
    util::b64url(b)
}

pub fn unb64u(s: &str) -> Option<Vec<u8>> {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(s.trim_end_matches('=')).ok()
}

static KEY: Mutex<Option<SecretKey>> = Mutex::new(None);

pub fn vapid_key() -> SecretKey {
    let mut k = KEY.lock();
    if let Some(k) = k.as_ref() {
        return k.clone();
    }
    let key = match memory::setting_raw("vapid").and_then(|pem| SecretKey::from_pkcs8_pem(&pem).ok()) {
        Some(key) => key,
        None => {
            let key = SecretKey::random(&mut rand::rngs::OsRng);
            if let Ok(pem) = key.to_pkcs8_pem(LineEnding::LF) {
                memory::set_setting_raw("vapid", &pem);
            }
            key
        }
    };
    *k = Some(key.clone());
    key
}

fn point(p: &PublicKey) -> Vec<u8> {
    p.to_encoded_point(false).as_bytes().to_vec()
}

/// 给浏览器 pushManager.subscribe 的 applicationServerKey
pub fn public_key() -> String {
    b64u(&point(&vapid_key().public_key()))
}

/// ES256 的 JWT：header.claims.签名（r||s）
pub fn jwt(key: &SecretKey, head: &Value, claims: &Value) -> String {
    let h = b64u(py::dumps(head, true).as_bytes());
    let c = b64u(py::dumps(claims, true).as_bytes());
    let sig: Signature = SigningKey::from(key).sign(format!("{h}.{c}").as_bytes());
    format!("{h}.{c}.{}", b64u(&sig.to_bytes()))
}

pub fn vapid_header(endpoint: &str) -> String {
    let aud = reqwest::Url::parse(endpoint)
        .map(|u| {
            format!(
                "{}://{}",
                u.scheme(),
                u.host_str()
                    .map(|h| match u.port() {
                        Some(p) => format!("{h}:{p}"),
                        None => h.to_string(),
                    })
                    .unwrap_or_default()
            )
        })
        .unwrap_or_default();
    let t = jwt(
        &vapid_key(),
        &json!({"typ": "JWT", "alg": "ES256"}),
        &json!({"aud": aud, "exp": py::now() as i64 + 12 * 3600, "sub": subject()}),
    );
    format!("vapid t={t}, k={}", public_key())
}

fn hkdf(salt: &[u8], ikm: &[u8], info: &[u8], n: usize) -> Vec<u8> {
    let mut out = vec![0u8; n];
    let _ = hkdf::Hkdf::<Sha256>::new(Some(salt), ikm).expand(info, &mut out);
    out
}

/// RFC 8291：用订阅的公钥和 auth 加密，返回 aes128gcm 格式的请求体（单条记录）
pub fn encrypt(payload: &[u8], p256dh: &str, auth: &str) -> Result<Vec<u8>, String> {
    let ua_pub_bytes = unb64u(p256dh).ok_or("bad p256dh")?;
    let ua_pub = PublicKey::from_sec1_bytes(&ua_pub_bytes).map_err(|e| e.to_string())?;
    let eph = p256::ecdh::EphemeralSecret::random(&mut rand::rngs::OsRng);
    let as_pub = point(&eph.public_key());
    let secret = eph.diffie_hellman(&ua_pub);
    let mut info = b"WebPush: info\x00".to_vec();
    info.extend_from_slice(&ua_pub_bytes);
    info.extend_from_slice(&as_pub);
    let ikm = hkdf(&unb64u(auth).ok_or("bad auth")?, secret.raw_secret_bytes(), &info, 32);
    let salt: [u8; 16] = rand::random();
    let cek = hkdf(&salt, &ikm, b"Content-Encoding: aes128gcm\x00", 16);
    let nonce = hkdf(&salt, &ikm, b"Content-Encoding: nonce\x00", 12);
    let mut plain = payload.to_vec();
    plain.push(2);
    let body = aes_gcm::Aes128Gcm::new_from_slice(&cek)
        .map_err(|e| e.to_string())?
        .encrypt(aes_gcm::Nonce::from_slice(&nonce), plain.as_slice())
        .map_err(|e| e.to_string())?;
    let mut out = salt.to_vec();
    out.extend_from_slice(&4096u32.to_be_bytes());
    out.push(as_pub.len() as u8);
    out.extend_from_slice(&as_pub);
    out.extend_from_slice(&body);
    Ok(out)
}

// ---------- 订阅 ----------
pub fn subscribe(sub: &Value, ua: &str) -> Result<i64, String> {
    let ep = sub["endpoint"].as_str().unwrap_or("");
    let keys = &sub["keys"];
    let (p, a) = (keys["p256dh"].as_str().unwrap_or(""), keys["auth"].as_str().unwrap_or(""));
    if !ep.starts_with("https://") || p.is_empty() || a.is_empty() {
        return Err(t!("订阅信息不完整"));
    }
    db::exec("insert or replace into push_subs values(?,?,?,?,?)", params![ep, p, a, py::head(ua, 200), py::now()]);
    Ok(count() as i64)
}

pub fn unsubscribe(endpoint: &str) {
    db::exec("delete from push_subs where endpoint=?", params![endpoint]);
}

pub fn count() -> usize {
    db::scalar_i64("select count(*) from push_subs", []) as usize
}

/// RFC 8030 的 Topic 头
pub fn topic(tag: &str) -> String {
    py::head(&b64u(&Sha256::digest(tag.as_bytes())), 32)
}

static HTTP: LazyLock<reqwest::Client> = LazyLock::new(|| {
    let mut b = reqwest::Client::builder().timeout(Duration::from_secs(15));
    if let Some(f) = std::env::var_os("SSL_CERT_FILE")
        && let Ok(pem) = std::fs::read(f)
        && let Ok(certs) = reqwest::Certificate::from_pem_bundle(&pem)
    {
        for c in certs {
            b = b.add_root_certificate(c);
        }
    }
    b.build().expect("push client")
});

async fn send_one(ep: &str, p256dh: &str, auth: &str, data: &[u8], urgency: &str, tag: &str) -> String {
    let body = match encrypt(data, p256dh, auth) {
        Ok(b) => b,
        Err(e) => return t!("发送失败：{e}", e = e),
    };
    let mut rb = HTTP
        .post(ep)
        .header("Authorization", vapid_header(ep))
        .header("Content-Encoding", "aes128gcm")
        .header("Content-Type", "application/octet-stream")
        .header("TTL", "86400")
        .header("Urgency", urgency)
        .body(body);
    if !tag.is_empty() {
        rb = rb.header("Topic", topic(tag));
    }
    match rb.send().await {
        Err(e) => t!("发送失败：{e}", e = e.to_string()),
        Ok(r) => {
            let s = r.status().as_u16();
            if s == 404 || s == 410 {
                unsubscribe(ep);
                return t!("订阅已失效，已删除");
            }
            if s < 300 {
                "ok".into()
            } else {
                let text = r.text().await.unwrap_or_default();
                t!("推送服务返回 {code}：{text}", code = s, text = py::head(&text, 200))
            }
        }
    }
}

/// 发给所有订阅了的设备。badge：现在有几个操作等你确认
pub async fn send_parts(title: &str, body: &str, url: &str, tag: &str, urgency: &str, badge: Option<usize>) -> Vec<String> {
    let subs = db::rows("select endpoint, p256dh, auth from push_subs", []);
    if subs.is_empty() {
        return vec![];
    }
    let mut payload = json!({"title": py::head(title, 80), "body": py::head(body, 200), "url": url, "tag": tag});
    if let Some(b) = badge {
        payload["badge"] = json!(b);
    }
    let data = py::dumps_u(&payload).into_bytes();
    let futs = subs.iter().map(|r| {
        let (ep, k, a) = (r.s(0), r.s(1), r.s(2));
        let data = data.clone();
        async move { send_one(&ep, &k, &a, &data, urgency, tag).await }
    });
    futures::future::join_all(futs).await
}

/// server::broadcast 用：msg 是 message_for 给的 [标题, 正文, 地址, tag]
pub async fn send(msg: Value, urgency: &str, badge: usize) {
    let s = |i: usize| msg[i].as_str().unwrap_or("").to_string();
    send_parts(&s(0), &s(1), &s(2), &s(3), urgency, Some(badge)).await;
}

// ---------- 什么时候推 ----------
fn bot_name(bot_id: &Value) -> String {
    match bot_id.as_i64() {
        Some(id) if id != 0 => {
            db::scalar("select name from bots where id=?", params![id]).as_str().map(String::from).unwrap_or_else(|| "Bot".into())
        }
        _ => "Bot".into(),
    }
}

static HEAD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\[[^\]]*\]\s*").unwrap());
static MARKS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[#*`>|_~]+").unwrap());

fn plain(text: &str) -> String {
    let t = HEAD.replace(text, "");
    MARKS.replace_all(&t, "").replace('\n', " ").trim().to_string()
}

/// 一个界面事件 → [标题, 正文, 打开的地址, 合并用的 tag]；不需要推的返回 None
pub fn message_for(ev: &Value) -> Option<Value> {
    let t = ev["type"].as_str().unwrap_or("");
    let mut cid = ev["chat_id"].clone();
    let mut url = if py::truthy(&cid) { format!("/?chat={}", py::str_of(&cid)) } else { "/".into() };
    let th = &ev["thread"];
    if py::truthy(th) {
        cid = th["parent"].clone();
        url = format!("/?chat={}&thread={}", py::str_of(&th["parent"]), py::str_of(&th["root"]));
    }
    let out = |a: String, b: String, c: String, d: String| Some(json!([a, b, c, d]));
    if t == "approval" && ev["status"] == "pending" {
        let who = bot_name(&ev["bot_id"]);
        let tag = format!("approval-{}", py::str_of(&ev["id"]));
        if ev["kind"] == "help" {
            return out(t!("{who} 需要你帮忙", who = who), t!("任务暂停了，等你接手电脑处理"), url, tag);
        }
        // 任务问你一个问题：正文是问题原文
        if ev["kind"] == "question" {
            return out(t!("{who} 等你回答", who = who), py::head(&plain(ev["question"].as_str().unwrap_or("")), 120), url, tag);
        }
        // 服务重启时说不准做没做成的那一步：是哪一步
        if ev["doubt"].is_object() {
            let tool = ev["doubt"]["tool"].as_str().filter(|s| !s.is_empty()).or(ev["tool"].as_str()).unwrap_or("").to_string();
            return out(
                t!("{who} 有一步要你定", who = who),
                match ev["doubt"]["why"].as_str().unwrap_or("") {
                    "stall" => t!("{tool} 这一步卡住超时被打断了，说不准有没有做成：再做一次，还是算已经做成？", tool = tool),
                    "stopped" => t!("任务被停下时 {tool} 这一步正在做，说不准有没有做成：再做一次，还是算已经做成？", tool = tool),
                    _ => t!("服务重启时 {tool} 这一步正在做，说不准有没有做成：再做一次，还是算已经做成？", tool = tool),
                },
                url,
                tag,
            );
        }
        let tool = ev["tool"].as_str().unwrap_or("");
        if ["create_bot", "update_bot", "delete_bot"].contains(&tool) {
            let d: Value = serde_json::from_str(ev["args"].as_str().filter(|s| !s.is_empty()).unwrap_or("{}")).unwrap_or(json!({}));
            let title = match tool {
                "create_bot" => t!("{who} 想新建一个 Bot", who = who),
                "update_bot" => t!("{who} 想修改一个 Bot", who = who),
                _ => t!("{who} 想删除一个 Bot", who = who),
            };
            let body = [&d["名字"], &d["Bot"]].into_iter().find(|v| py::truthy(v)).map(py::str_of).unwrap_or_default();
            return out(title, body, url, tag);
        }
        let tool_s = if ev.get("tool").is_some() { py::str_of(&ev["tool"]) } else { String::new() };
        let goal_s = if ev.get("goal").is_some() { py::str_of(&ev["goal"]) } else { String::new() };
        return out(t!("{who} 需要你确认", who = who), t!("{tool}：{goal}", tool = tool_s, goal = goal_s), url, tag);
    }
    if t == "message" {
        let m = &ev["message"];
        let kind = m["meta"]["kind"].as_str().unwrap_or("");
        let tag = format!("chat-{}", py::str_of(&cid));
        let content = m["content"].as_str().unwrap_or("");
        let who = || bot_name(&m["bot_id"]);
        let prompt = || py::head(m["meta"]["prompt"].as_str().unwrap_or(""), 120);
        if m["role"] == "assistant" && !content.is_empty() {
            return out(who(), py::head(&plain(content), 120), url, tag);
        }
        if m["role"] == "task" && kind == "image" {
            return out(t!("{who} 画好了一张图", who = who()), prompt(), url, tag);
        }
        if m["role"] == "task" && kind == "video" {
            return out(t!("{who} 生成好了一段视频", who = who()), prompt(), url, tag);
        }
        if m["role"] == "task" && kind == "result" {
            let ok = m["meta"]["status"] == "done";
            let w = who();
            return out(
                if ok { t!("{who} 做完了任务", who = w) } else { t!("{who} 的任务失败了", who = w) },
                py::head(&plain(content), 120),
                url,
                tag,
            );
        }
    }
    if t == "teach" && py::truthy(&ev["draft"]) {
        return out(
            t!("演示录制已结束"),
            t!("技能草稿「{name}」等你审核", name = py::str_of(&ev["draft"]["name"])),
            "/".into(),
            "teach".into(),
        );
    }
    None
}

// ---------- 接口 ----------
async fn push_info() -> Res {
    ok(json!({"key": public_key(), "count": count()}))
}

async fn push_subscribe(h: HeaderMap, Body(d): Body) -> Res {
    let ua = h.get("user-agent").and_then(|v| v.to_str().ok()).unwrap_or("");
    let sub = if d["subscription"].is_object() { d["subscription"].clone() } else { json!({}) };
    let n = subscribe(&sub, ua)?;
    ok(json!({"count": n}))
}

async fn push_unsubscribe(Body(d): Body) -> Res {
    unsubscribe(d["endpoint"].as_str().unwrap_or(""));
    ok(json!({"count": count()}))
}

async fn push_test() -> Res {
    if count() == 0 {
        return Err(ApiError::bad(t!("还没有设备开启推送")));
    }
    ok(json!({"results": send_parts(&t!("推送测试"), &t!("能收到这条，说明这台设备的推送是通的。"), "/", "test", "normal", None).await}))
}

pub fn routes() -> axum::Router {
    axum::Router::new()
        .route("/api/push", get(push_info))
        .route("/api/push/subscribe", post(push_subscribe))
        .route("/api/push/unsubscribe", post(push_unsubscribe))
        .route("/api/push/test", post(push_test))
}
