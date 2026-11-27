//! 按意思找记忆和资料库页面：关键词（FTS5，中文两个字一组）+ 可选的向量，按排名融合（RRF）。对应 osa/search.py。

use crate::db::{self, RowExt};
use crate::registry::{Call, Out};
use crate::web::{Q, Res, ok, qi, qs};
use crate::{config, library, providers, py};
use axum::routing::get;
use indexmap::IndexMap;
use parking_lot::Mutex;
use regex::Regex;
use rusqlite::params_from_iter;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;
use std::time::Duration;

pub const CHUNK: usize = 600;
pub const MAX_CHUNKS: usize = 200;
pub const BATCH: i64 = 32;

static CJK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\x{3400}-\x{4dbf}\x{4e00}-\x{9fff}\x{f900}-\x{faff}]+|[a-z0-9_]+").unwrap());
static WAKE: LazyLock<tokio::sync::Notify> = LazyLock::new(tokio::sync::Notify::new);

/// 叫醒后台算向量的循环（换了 embedding 模型时）
pub fn wake() {
    WAKE.notify_one();
}

struct Status {
    error: String,
    last: f64,
    failed: f64,
}

static STATUS: Mutex<Status> = Mutex::new(Status { error: String::new(), last: 0.0, failed: 0.0 });

pub fn migrate() {
    db::batch(
        "create table if not exists search_docs(
          id integer primary key, kind text, ref integer, chunk integer, bot_id integer, sub text default '',
          src_updated real, title text default '', text text, model text default '', vec blob);
         create index if not exists search_docs_ref on search_docs(kind, ref);
         create virtual table if not exists search_fts using fts5(tokens);",
    );
}

// ---------- 分词 ----------
pub fn tokens(text: &str) -> Vec<String> {
    let low = text.to_lowercase();
    let mut out = vec![];
    for m in CJK.find_iter(&low) {
        let s: Vec<char> = m.as_str().chars().collect();
        if s[0] > '\u{3000}' {
            if s.len() < 2 {
                out.push(m.as_str().to_string());
            } else {
                for i in 0..s.len() - 1 {
                    out.push(s[i..i + 2].iter().collect());
                }
            }
        } else {
            out.push(m.as_str().to_string());
        }
    }
    out
}

fn dedup(v: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    v.into_iter().filter(|t| seen.insert(t.clone())).collect()
}

fn match_query(q: &str) -> String {
    dedup(tokens(q)).iter().map(|t| format!("\"{t}\"")).collect::<Vec<_>>().join(" OR ")
}

// ---------- 切块 ----------
pub fn plain(content: &str, fmt: &str) -> String {
    if fmt != "html" {
        return content.to_string();
    }
    static SCRIPT: LazyLock<fancy_regex::Regex> = LazyLock::new(|| fancy_regex::Regex::new(r"(?is)<(script|style)\b.*?</\1>").unwrap());
    static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").unwrap());
    static NB: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"&nbsp;|&#160;").unwrap());
    let c = SCRIPT.replace_all(content, " ");
    let c = TAG.replace_all(&c, " ");
    NB.replace_all(&c, " ").into_owned()
}

pub fn chunks(text: &str) -> Vec<String> {
    static PARA: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\n\s*\n").unwrap());
    let mut out: Vec<String> = vec![];
    let mut cur = String::new();
    for para in PARA.split(text) {
        let mut para: Vec<char> = para.split_whitespace().collect::<Vec<_>>().join(" ").chars().collect();
        if para.is_empty() {
            continue;
        }
        while para.len() > CHUNK * 2 {
            out.push(para[..CHUNK].iter().collect());
            para = para[CHUNK..].to_vec();
        }
        let ps: String = para.iter().collect();
        if !cur.is_empty() && py::char_len(&cur) + para.len() > CHUNK {
            out.push(std::mem::take(&mut cur));
        }
        cur = format!("{cur} {ps}").trim().to_string();
        if out.len() >= MAX_CHUNKS {
            break;
        }
    }
    if !cur.is_empty() && out.len() < MAX_CHUNKS {
        out.push(cur);
    }
    out.truncate(MAX_CHUNKS);
    out
}

// ---------- 同步索引 ----------
fn drop_ids(ids: &[i64]) {
    for i in ids {
        db::exec("delete from search_fts where rowid=?", [i]);
        db::exec("delete from search_docs where id=?", [i]);
    }
}

fn add(kind: &str, r#ref: i64, bot_id: &Value, sub: &str, updated: &Value, title: &str, parts: &[String]) {
    for (n, text) in parts.iter().enumerate() {
        let did = db::insert(
            "insert into search_docs(kind, ref, chunk, bot_id, sub, src_updated, title, text) values(?,?,?,?,?,?,?,?)",
            rusqlite::params![kind, r#ref, n as i64, db::J(bot_id), sub, db::J(updated), title, text],
        );
        db::exec(
            "insert into search_fts(rowid, tokens) values(?, ?)",
            rusqlite::params![did, tokens(&format!("{title} {text}")).join(" ")],
        );
    }
}

fn same(a: &Value, b: &Value) -> bool {
    match (a.as_f64(), b.as_f64()) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

/// 让索引跟上记忆和页面：返回改动了多少条
pub fn sync() -> usize {
    let _c = db::conn();
    let mut have: IndexMap<(String, i64), (Value, Vec<i64>)> = IndexMap::new();
    for r in db::rows("select kind, ref, src_updated, id from search_docs", []) {
        let e = have.entry((r.s(0), r.i(1))).or_insert((r[2].clone(), vec![]));
        e.1.push(r.i(3));
    }
    let mut src: IndexMap<(String, i64), Value> = IndexMap::new();
    // 项目记忆只给那个项目的任务看、过了截止日的不再用：都不进索引（已经在索引里的，下面会当成“源没了”清掉）
    let live = "select id, updated from memories where project_id is null and (pinned = 1 or until is null or until > ?)";
    for r in db::rows(live, rusqlite::params![py::now()]) {
        src.insert(("memory".into(), r.i(0)), r[1].clone());
    }
    for r in db::rows("select id, updated from pages", []) {
        src.insert(("page".into(), r.i(0)), r[1].clone());
    }
    let mut changed = 0;
    for (key, (u, ids)) in &have {
        if src.get(key).is_none_or(|s| !same(s, u)) {
            drop_ids(ids);
            changed += 1;
        }
    }
    for ((kind, r#ref), u) in &src {
        if have.get(&(kind.clone(), *r#ref)).is_some_and(|(h, _)| same(h, u)) {
            continue;
        }
        if kind == "memory" {
            if let Some(r) = db::row("select bot_id, kind, text from memories where id=?", [r#ref]) {
                add(kind, *r#ref, &r[0], &r.s(1), u, "", &[r.s(2)]);
            }
        } else if let Some(r) = db::row("select bot_id, title, format, content from pages where id=?", [r#ref]) {
            let mut parts = chunks(&plain(&r.s(3), &r.s(2)));
            if parts.is_empty() {
                parts = vec![r.s(1)];
            }
            add(kind, *r#ref, &r[0], "", u, &r.s(1), &parts);
        }
        changed += 1;
    }
    if changed > 0 {
        WAKE.notify_one();
    }
    changed
}

// ---------- 向量 ----------
pub fn embed_config() -> Option<Value> {
    let e = config::get("embedding");
    let p = e["provider"].as_str().unwrap_or("");
    let m = e["model"].as_str().unwrap_or("").trim().to_string();
    if !p.is_empty() && providers::exists(p) && !m.is_empty() {
        return Some(json!({"provider": p, "model": m}));
    }
    None
}

pub async fn embed(texts: &[String]) -> anyhow::Result<Vec<Vec<f32>>> {
    let e = embed_config().ok_or_else(|| anyhow::anyhow!("没配 embedding"))?;
    let mut c = providers::Client::for_provider(e["provider"].as_str().unwrap_or(""))?;
    c.timeout = Some(Duration::from_secs(60));
    let input: Vec<String> = texts
        .iter()
        .map(|t| {
            let s = py::head(t, 8000);
            if s.is_empty() { " ".into() } else { s }
        })
        .collect();
    let r = c.post_json("embeddings", &json!({"model": e["model"], "input": input})).await?;
    let mut data: Vec<Value> = r["data"].as_array().cloned().unwrap_or_default();
    data.sort_by_key(|d| d["index"].as_i64().unwrap_or(0));
    Ok(data
        .iter()
        .map(|d| {
            let v: Vec<f64> = d["embedding"].as_array().map(|a| a.iter().filter_map(|x| x.as_f64()).collect()).unwrap_or_default();
            let n = v.iter().map(|x| x * x).sum::<f64>().sqrt();
            let n = if n == 0.0 { 1.0 } else { n };
            v.iter().map(|x| (x / n) as f32).collect()
        })
        .collect())
}

fn pack(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

fn unpack(b: &[u8]) -> Vec<f32> {
    b.as_chunks::<4>().0.iter().map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()
}

fn vec_rows(sql: &str, args: Vec<Value>) -> Vec<(i64, Vec<u8>)> {
    let c = db::conn();
    let Ok(mut st) = c.prepare(sql) else { return vec![] };
    st.query_map(params_from_iter(args.iter().map(db::J)), |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1).unwrap_or_default())))
        .map(|it| it.filter_map(|x| x.ok()).collect())
        .unwrap_or_default()
}

/// 后台给还没有向量（或者换了模型）的条目算向量。没配 embedding 时什么都不做
pub async fn embed_loop() {
    loop {
        let _ = tokio::time::timeout(Duration::from_secs(300), WAKE.notified()).await;
        while let Some(e) = embed_config() {
            let model = e["model"].as_str().unwrap_or("").to_string();
            let rows = db::rows(
                "select id, title, text from search_docs where model != ? or vec is null limit ?",
                rusqlite::params![model, BATCH],
            );
            if rows.is_empty() {
                break;
            }
            let texts: Vec<String> = rows.iter().map(|r| format!("{} {}", r.s(1), r.s(2)).trim().to_string()).collect();
            match embed(&texts).await {
                Ok(vecs) => {
                    for (r, v) in rows.iter().zip(vecs) {
                        db::exec("update search_docs set vec=?, model=? where id=?", rusqlite::params![pack(&v), model, r.i(0)]);
                    }
                    let mut s = STATUS.lock();
                    s.error.clear();
                    s.last = py::now();
                    s.failed = 0.0;
                }
                Err(err) => {
                    {
                        let mut s = STATUS.lock();
                        s.error = err_text(&err);
                        s.failed = py::now();
                    }
                    tokio::time::sleep(Duration::from_secs(60)).await;
                    break;
                }
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }
}

fn err_text(e: &anyhow::Error) -> String {
    let name = if e.downcast_ref::<providers::ApiError>().is_some() { "APIError" } else { "Error" };
    format!("{name}: {}", py::head(&e.to_string(), 200))
}

/// 启动时补上旧数据的索引，然后在后台算向量
pub async fn startup() {
    sync();
    embed_loop().await;
}

pub fn info() -> Value {
    let e = embed_config();
    let total = db::scalar_i64("select count(*) from search_docs", []);
    let done = match &e {
        Some(e) => {
            db::scalar_i64("select count(*) from search_docs where vec is not null and model=?", [e["model"].as_str().unwrap_or("")])
        }
        None => 0,
    };
    let error = if e.is_some() { STATUS.lock().error.clone() } else { String::new() };
    json!({"embedding": e, "chunks": total, "embedded": done, "error": error})
}

// ---------- 检索 ----------
fn visible(kinds: &[&str], bot_id: Option<i64>) -> (String, Vec<Value>) {
    let mut wh = vec![format!("d.kind in ({})", vec!["?"; kinds.len()].join(","))];
    let mut args: Vec<Value> = kinds.iter().map(|k| json!(k)).collect();
    if let Some(b) = bot_id
        && kinds.contains(&"memory")
    {
        wh.push("(d.kind != 'memory' or d.bot_id is null or d.bot_id = ?)".into());
        args.push(json!(b));
    }
    (wh.join(" and "), args)
}

/// 返回 [{kind, ref, title, snippet, bot_id, sub, score}]，同一个页面只出一次。subs 为空是不限
pub async fn search(q: &str, bot_id: Option<i64>, kinds: &[&str], limit: usize, subs: &[&str]) -> Vec<Value> {
    let q = q.trim();
    if q.is_empty() {
        return vec![];
    }
    sync();
    let (mut wh, mut args) = visible(kinds, bot_id);
    if !subs.is_empty() {
        wh += &format!(" and (d.kind != 'memory' or d.sub in ({}))", vec!["?"; subs.len()].join(","));
        args.extend(subs.iter().map(|s| json!(s)));
    }
    let mut ranks: IndexMap<i64, f64> = IndexMap::new();
    let m = match_query(q);
    if !m.is_empty() {
        let mut a = vec![json!(m)];
        a.extend(args.clone());
        let rows = db::rows(
            &format!(
                "select d.id from search_fts f join search_docs d on d.id = f.rowid where search_fts match ? and {wh} order by bm25(search_fts) limit 60"
            ),
            params_from_iter(a.iter().map(db::J)),
        );
        for (i, r) in rows.iter().enumerate() {
            *ranks.entry(r.i(0)).or_insert(0.0) += 1.0 / (60 + i) as f64;
        }
    }
    if let Some(e) = embed_config() {
        let model = e["model"].as_str().unwrap_or("").to_string();
        let ok_time = py::now() - STATUS.lock().failed > 60.0;
        if ok_time && db::exists("select 1 from search_docs where vec is not null and model=? limit 1", [&model]) {
            match embed(&[q.to_string()]).await {
                Ok(v) => {
                    let qv = v.into_iter().next().unwrap_or_default();
                    let mut a = vec![json!(model)];
                    a.extend(args.clone());
                    let rows =
                        vec_rows(&format!("select d.id, d.vec from search_docs d where d.vec is not null and d.model = ? and {wh}"), a);
                    let mut scored: Vec<(f64, i64)> = rows
                        .iter()
                        .map(|(did, b)| (qv.iter().zip(unpack(b)).map(|(x, y)| (*x as f64) * (y as f64)).sum::<f64>(), *did))
                        .collect();
                    scored.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
                    for (i, (s, did)) in scored.into_iter().take(60).enumerate() {
                        if s > 0.2 {
                            *ranks.entry(did).or_insert(0.0) += 1.0 / (60 + i) as f64;
                        }
                    }
                }
                Err(err) => {
                    let mut s = STATUS.lock();
                    s.error = err_text(&err);
                    s.failed = py::now();
                }
            }
        }
    }
    if ranks.is_empty() {
        return vec![];
    }
    let mut ids: Vec<i64> = ranks.keys().copied().collect();
    ids.sort_by(|a, b| ranks[b].partial_cmp(&ranks[a]).unwrap_or(std::cmp::Ordering::Equal));
    let docs: HashMap<i64, Vec<Value>> = db::rows(
        &format!("select id, kind, ref, title, text, bot_id, sub from search_docs where id in ({})", vec!["?"; ids.len()].join(",")),
        params_from_iter(ids.iter()),
    )
    .into_iter()
    .map(|r| (r.i(0), r))
    .collect();
    let mut out = vec![];
    let mut seen = HashSet::new();
    for did in ids {
        let Some(d) = docs.get(&did) else { continue };
        let (kind, r#ref) = (d.s(1), d.i(2));
        if !seen.insert((kind.clone(), r#ref)) {
            continue;
        }
        let text = d.s(4);
        out.push(
            json!({"kind": kind, "ref": r#ref, "title": d[3], "snippet": if kind == "memory" { text } else { snippet(&text, q, 120) },
                        "bot_id": d[5], "sub": d[6], "score": py::round_n(ranks[&did], 5)}),
        );
        if out.len() >= limit {
            break;
        }
    }
    out
}

/// 命中词附近的一段
pub fn snippet(text: &str, q: &str, width: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    let low: Vec<char> = text.to_lowercase().chars().collect();
    let pos = tokens(q)
        .iter()
        .filter_map(|t| {
            let tc: Vec<char> = t.chars().collect();
            (0..=low.len().saturating_sub(tc.len())).find(|i| low.len() >= tc.len() && low[*i..*i + tc.len()] == tc[..])
        })
        .min();
    let n = chars.len();
    match pos {
        Some(pos) if n > width => {
            let start = pos.saturating_sub(width / 3);
            let end = (start + width).min(n);
            format!(
                "{}{}{}",
                if start > 0 { "…" } else { "" },
                chars[start.min(n)..end].iter().collect::<String>(),
                if start + width < n { "…" } else { "" }
            )
        }
        _ => format!("{}{}", chars.iter().take(width).collect::<String>(), if n > width { "…" } else { "" }),
    }
}

// ---------- 给 Bot 用的工具 ----------
pub async fn tool_search_memory(c: Call) -> Out {
    let scope = if py::truthy(&c.args["scope"]) { py::str_of(&c.args["scope"]) } else { "all".into() };
    let kinds: &[&str] = match scope.as_str() {
        "memories" => &["memory"],
        "pages" => &["page"],
        _ => &["memory", "page"],
    };
    let q = if py::truthy(&c.args["query"]) { py::str_of(&c.args["query"]) } else { String::new() };
    let hits = search(&q, Some(c.bot_id()), kinds, 8, &[]).await;
    if hits.is_empty() {
        return "没找到相关的记忆或页面".into();
    }
    hits.iter()
        .map(|h| {
            if h["kind"] == "memory" {
                let who = if h["bot_id"].is_null() {
                    "所有 Bot 共享"
                } else if h["sub"] == "work" {
                    "做过的事"
                } else {
                    "你的记忆"
                };
                format!("- 记忆 #{}（{who}）：{}", py::str_of(&h["ref"]), py::str_of(&h["snippet"]))
            } else {
                format!(
                    "- 页面 #{}《{}》{}：{}",
                    py::str_of(&h["ref"]),
                    py::str_of(&h["title"]),
                    library::link(h["ref"].as_i64().unwrap_or(0)),
                    py::str_of(&h["snippet"])
                )
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        .into()
}

async fn api_search(q: Q) -> Res {
    let kinds: &[&str] = match qs(&q, "kind").unwrap_or("all") {
        "page" => &["page"],
        "memory" => &["memory"],
        _ => &["memory", "page"],
    };
    let hits = search(qs(&q, "q").unwrap_or(""), qi(&q, "bot_id"), kinds, 20, &[]).await;
    ok(json!({"hits": hits, "info": info()}))
}

async fn api_info() -> Res {
    sync();
    ok(info())
}

/// /api/search、/api/search/info
pub fn routes() -> axum::Router {
    axum::Router::new().route("/api/search", get(api_search)).route("/api/search/info", get(api_info))
}
