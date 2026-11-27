//! Bot 和对话的增删改查。每个 Bot 有一个私聊对话；群聊的 bot_ids 有 2–6 个，lead 是协调者。
//! 讨论串（thread）是挂在私聊或群聊某条消息下面的子对话（kind='thread'），chat() 按上级对话的样子返回。对应 osa/bots.py。

use crate::db::{self, RowExt};
use crate::memory;
use crate::py::{self, dumps_u};
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::HashMap;

pub const SHAPES: [&str; 8] = ["square", "sparkle", "clover", "flower", "heart", "house", "star", "circle"];
pub const COLORS: [&str; 8] = ["#7c9cff", "#ff8a65", "#4fc3a1", "#f6c453", "#c792ea", "#ff6b9a", "#5cc8ff", "#a3a3a3"];
pub const MAX_BOTS: i64 = 50;
pub const GROUP_MIN: usize = 2;
pub const GROUP_MAX: usize = 6;
pub const MAX_GROUPS: i64 = 50;

pub fn migrate() {
    let cols = db::columns("chats");
    let has = |c: &str| cols.iter().any(|x| x == c);
    if !has("lead") {
        db::batch("alter table chats add column lead integer");
    }
    if !has("parent") {
        db::batch("alter table chats add column parent integer; alter table chats add column root integer;");
    }
    if !has("image") {
        db::batch("alter table chats add column image text");
    }
    if !has("seen") {
        db::batch(
            "alter table chats add column seen integer;
             update chats set seen=(select coalesce(max(id), 0) from messages where chat_id=chats.id) where kind='thread';",
        );
    }
    db::batch("create index if not exists chats_thread on chats(parent, root)");
    if !db::exists("select 1 from settings where key='chat_seen_init'", []) {
        db::exec(
            "update chats set seen=(select coalesce(max(id), 0) from messages where chat_id=chats.id) where kind != 'thread' and seen is null",
            [],
        );
        memory::set_setting_raw("chat_seen_init", "1");
    }
}

fn ids_json(ids: &[i64]) -> String {
    dumps_u(&json!(ids))
}

fn bot_out(r: &[Value]) -> Value {
    let r = r.to_vec();
    let i = r.i(0);
    let chat = db::row("select id from chats where kind='direct' and bot_ids=?", params![ids_json(&[i])]);
    json!({"id": i, "name": r[1], "avatar": r.j(2), "instructions": r.s(3), "is_primary": r.i(4) != 0,
           "created": r[5], "chat_id": chat.map(|c| c[0].clone()).unwrap_or(Value::Null)})
}

const BOT_COLS: &str = "select id, name, avatar, instructions, is_primary, created from bots";

pub fn list_bots() -> Vec<Value> {
    db::rows(&format!("{BOT_COLS} order by is_primary desc, id"), []).iter().map(|r| bot_out(r)).collect()
}

pub fn get(bot_id: i64) -> Option<Value> {
    db::row(&format!("{BOT_COLS} where id=?"), params![bot_id]).map(|r| bot_out(&r))
}

/// 按名字找 Bot（不区分大小写，去掉开头的 @）
pub fn by_name(name: &str) -> Option<Value> {
    let key = name.trim().trim_start_matches('@').trim().to_lowercase();
    if key.is_empty() {
        return None;
    }
    list_bots().into_iter().find(|b| b["name"].as_str().unwrap_or("").to_lowercase() == key)
}

pub fn primary() -> Option<Value> {
    let id = db::scalar("select id from bots where is_primary=1", []);
    id.as_i64().and_then(get)
}

pub fn primary_name() -> String {
    db::scalar("select name from bots where is_primary=1", []).as_str().unwrap_or("").to_string()
}

fn valid_color(s: &str) -> bool {
    s.len() == 7 && s.starts_with('#') && s[1..].chars().all(|c| c.is_ascii_hexdigit())
}

/// 头像：形状加颜色，可以再加一张图片
pub fn clean_avatar(avatar: &Value, n: i64) -> Result<Value, String> {
    let a = if avatar.is_object() { avatar.clone() } else { json!({}) };
    let shape = a["shape"]
        .as_str()
        .filter(|s| SHAPES.contains(s))
        .map(String::from)
        .unwrap_or_else(|| SHAPES[(n.rem_euclid(SHAPES.len() as i64)) as usize].to_string());
    let color = a["color"]
        .as_str()
        .filter(|s| valid_color(s))
        .map(String::from)
        .unwrap_or_else(|| COLORS[(n.rem_euclid(COLORS.len() as i64)) as usize].to_string());
    let mut out = json!({"shape": shape, "color": color});
    if py::truthy(&a["image"]) {
        let img = py::str_of(&a["image"]);
        if !crate::avatars::valid(&img) {
            return Err(t!("头像图片不存在，重新上传一次"));
        }
        out["image"] = Value::String(img);
    }
    Ok(out)
}

fn next_id() -> i64 {
    let hw = memory::setting_raw("bot_id_hw").and_then(|s| s.trim().parse::<i64>().ok()).unwrap_or(0);
    let used = db::scalar_i64("select max(coalesce((select max(id) from bots), 0), coalesce((select max(bot_id) from messages), 0))", []);
    let bid = hw.max(used) + 1;
    memory::set_setting_raw("bot_id_hw", &bid.to_string());
    bid
}

/// 文档里的 Agent 跟着 Bot 建、改名、撤（没配文档、或不在 tokio 里时不做，下次启动 docs::sync 补上）
fn docs_later(f: impl std::future::Future<Output = ()> + Send + 'static) {
    if crate::docs::configured()
        && let Ok(h) = tokio::runtime::Handle::try_current()
    {
        h.spawn(f);
    }
}

fn clip_name(s: &str, n: usize) -> String {
    py::head(s.trim(), n)
}

pub fn create(name: &str, avatar: &Value, instructions: &str, is_primary: bool) -> Result<Value, String> {
    let n = db::scalar_i64("select count(*) from bots", []);
    if n >= MAX_BOTS {
        return Err(t!("最多 {n} 个 Bot", n = MAX_BOTS));
    }
    let avatar = clean_avatar(avatar, n)?;
    let now = py::now();
    let bid = next_id();
    let nm = clip_name(name, 40);
    let nm = if nm.is_empty() { t!("未命名") } else { nm };
    db::exec(
        "insert into bots(id, name, avatar, instructions, is_primary, created) values(?,?,?,?,?,?)",
        params![bid, nm, py::dumps(&avatar, true), instructions, is_primary as i64, now],
    );
    db::exec("insert into chats(kind, title, bot_ids, updated) values('direct', ?, ?, ?)", params![name, ids_json(&[bid]), now]);
    docs_later(crate::docs::on_bot_created(bid));
    Ok(get(bid).unwrap_or(Value::Null))
}

/// 改名字、职责、头像（没给或给 null 的不改）
pub fn update(bot_id: i64, fields: &Value) -> Result<Value, String> {
    if !fields["name"].is_null() && fields.get("name").is_some() {
        let nm = clip_name(&py::str_of(&fields["name"]), 40);
        let nm = if nm.is_empty() { t!("未命名") } else { nm };
        db::exec("update bots set name=? where id=?", params![nm, bot_id]);
        db::exec("update chats set title=? where kind='direct' and bot_ids=?", params![nm, ids_json(&[bot_id])]);
        let an = crate::docs::agent_name(&nm, bot_id);
        docs_later(async move { crate::docs::rename(bot_id, &an).await });
    }
    if fields.get("instructions").is_some_and(|v| !v.is_null()) {
        db::exec("update bots set instructions=? where id=?", params![py::head(&py::str_of(&fields["instructions"]), 4000), bot_id]);
    }
    if py::truthy(&fields["avatar"]) {
        let a = clean_avatar(&fields["avatar"], bot_id)?;
        db::exec("update bots set avatar=? where id=?", params![py::dumps(&a, true), bot_id]);
    }
    crate::avatars::gc();
    Ok(get(bot_id).unwrap_or(Value::Null))
}

pub fn set_primary(bot_id: i64) {
    db::exec("update bots set is_primary = (id = ?)", params![bot_id]);
}

pub fn delete(bot_id: i64) -> Result<(), String> {
    let Some(b) = get(bot_id) else { return Ok(()) };
    if b["is_primary"] == true {
        return Err(t!("主 Bot 不能删除，先把别的 Bot 设为主 Bot"));
    }
    if let Some(c) = b["chat_id"].as_i64() {
        delete_chat(c);
    }
    db::exec("delete from bots where id=?", params![bot_id]);
    docs_later(crate::docs::forget(bot_id));
    for g in list_chats().into_iter().filter(|c| c["kind"] == "group" && ids_of(c).contains(&bot_id)) {
        let ids: Vec<i64> = ids_of(&g).into_iter().filter(|i| *i != bot_id).collect();
        let lead = g["lead"].as_i64().filter(|l| ids.contains(l)).or(ids.first().copied());
        db::exec("update chats set bot_ids=?, lead=? where id=?", params![ids_json(&ids), lead, g["id"].as_i64()]);
    }
    crate::avatars::gc();
    crate::media::gc();
    Ok(())
}

pub fn ids_of(chat: &Value) -> Vec<i64> {
    chat["bot_ids"].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default()
}

pub fn thread_ids(chat_id: i64) -> Vec<i64> {
    db::rows("select id from chats where kind='thread' and parent=?", params![chat_id]).iter().map(|r| r.i(0)).collect()
}

fn delete_chat(chat_id: i64) {
    let mut all = vec![chat_id];
    all.extend(thread_ids(chat_id));
    for i in all {
        db::exec("delete from task_steps where task_id in (select id from messages where chat_id=?)", params![i]);
        db::exec("delete from messages where chat_id=?", params![i]);
        db::exec("delete from chats where id=?", params![i]);
    }
}

const CHAT_COLS: &str = "id, kind, title, bot_ids, pinned, updated, lead, parent, root, image";

fn chat_out(r: &[Value]) -> Value {
    let r = r.to_vec();
    let i = r.i(0);
    let kind = r.s(1);
    let last = db::row("select content, role from messages where chat_id=? order by id desc limit 1", params![i]);
    let threads =
        if kind == "thread" { 0 } else { db::scalar_i64("select count(*) from chats where kind='thread' and parent=?", params![i]) };
    let ids: Value = serde_json::from_str(&if r.s(3).is_empty() { "[]".into() } else { r.s(3) }).unwrap_or(json!([]));
    json!({"id": i, "kind": kind, "title": r[2], "bot_ids": ids, "pinned": if py::truthy(&r[4]) { r[4].clone() } else { json!(0) },
           "updated": r[5], "preview": last.map(|l| py::head(&l.s(0), 80)).unwrap_or_default(), "lead": r[6], "image": r[9],
           "threads": threads, "unread": unread(i)})
}

pub fn list_chats() -> Vec<Value> {
    db::rows(&format!("select {CHAT_COLS} from chats where kind != 'thread' order by updated desc"), [])
        .iter()
        .map(|r| chat_out(r))
        .collect()
}

pub fn set_pinned(chat_id: i64, pinned: bool) -> Result<Value, String> {
    let c = chat(chat_id).filter(|c| !py::truthy(&c["parent"])).ok_or_else(|| t!("没有这个对话"))?;
    if py::truthy(&c["pinned"]) != pinned {
        db::exec("update chats set pinned=? where id=?", params![if pinned { json!(py::now()) } else { json!(0) }.as_f64(), chat_id]);
    }
    Ok(chat(chat_id).unwrap_or(Value::Null))
}

/// 一个对话。讨论串按上级对话的样子返回，另外带上 parent、root、root_bot
pub fn chat(chat_id: i64) -> Option<Value> {
    let r = db::row(&format!("select {CHAT_COLS} from chats where id=?"), params![chat_id])?;
    let c = chat_out(&r);
    if c["kind"] != "thread" {
        return Some(c);
    }
    let up = chat(r.i(7))?;
    let root = r.i(8);
    let author = db::row("select bot_id from messages where id=?", params![root]).and_then(|a| a.oi(0));
    let mut out = up.clone();
    for k in ["id", "updated", "preview", "unread"] {
        out[k] = c[k].clone();
    }
    out["pinned"] = json!(0);
    out["parent"] = up["id"].clone();
    out["root"] = json!(root);
    out["root_bot"] = match author {
        Some(a) if ids_of(&up).contains(&a) => json!(a),
        _ => Value::Null,
    };
    Some(out)
}

// ---------- 讨论串 ----------
pub fn find_thread(parent: i64, root: i64) -> Option<i64> {
    db::row("select id from chats where kind='thread' and parent=? and root=?", params![parent, root]).map(|r| r.i(0))
}

pub fn thread(parent: i64, root: i64) -> Result<Value, String> {
    let up = chat(parent);
    if up.as_ref().is_none_or(|u| py::truthy(&u["parent"])) {
        return Err(t!("只能回复私聊或群聊里的消息"));
    }
    if !db::exists("select 1 from messages where id=? and chat_id=?", params![root, parent]) {
        return Err(t!("这个对话里没有这条消息"));
    }
    let tid = match find_thread(parent, root) {
        Some(t) => t,
        None => db::insert(
            "insert into chats(kind, title, bot_ids, updated, parent, root) values('thread', '', '[]', ?, ?, ?)",
            params![py::now(), parent, root],
        ),
    };
    Ok(chat(tid).unwrap_or(Value::Null))
}

const REPLY: &str = "(m.role = 'user' or (m.role = 'assistant' and m.content != '') \
    or json_extract(m.meta, '$.kind') in ('result', 'dm_reply', 'image', 'followup'))";
/// 不是用户自己说的：用户发的消息、用户发给任务的话（followup，role 是 task）都不算
const THEIRS: &str = "(m.role != 'user' and coalesce(json_extract(m.meta, '$.kind'), '') != 'followup')";

pub fn unread(chat_id: i64) -> i64 {
    db::scalar_i64(
        &format!(
            "select count(*) from messages m where m.chat_id=? and {THEIRS} and {REPLY} \
             and m.id > (select coalesce(seen, 0) from chats where id=?)"
        ),
        params![chat_id, chat_id],
    )
}

pub fn thread_summaries(parent: i64, roots: &[i64]) -> HashMap<i64, Value> {
    if roots.is_empty() {
        return HashMap::new();
    }
    let marks = vec!["?"; roots.len()].join(",");
    let mut p: Vec<i64> = vec![parent];
    p.extend_from_slice(roots);
    db::rows(
        &format!(
            "select c.id, c.root, count(m.id), max(m.ts),
        group_concat(distinct case when {THEIRS} then m.bot_id end),
        sum(case when {THEIRS} and m.id > coalesce(c.seen, 0) then 1 else 0 end)
        from chats c left join messages m on m.chat_id = c.id and {REPLY}
        where c.kind = 'thread' and c.parent = ? and c.root in ({marks}) group by c.id"
        ),
        rusqlite::params_from_iter(p),
    )
    .into_iter()
    .map(|r| {
        let bots: Vec<i64> = r.s(4).split(',').filter(|x| !x.is_empty()).filter_map(|x| x.parse().ok()).collect();
        (r.i(1), json!({"id": r[0], "count": r[2], "last": r[3], "bots": bots, "unread": r.oi(5).unwrap_or(0)}))
    })
    .collect()
}

pub fn thread_list(parent: Option<i64>, unread_only: bool) -> Vec<Value> {
    let rows = match parent.filter(|p| *p != 0) {
        Some(p) => db::rows("select parent, root from chats where kind='thread' and parent=?", params![p]),
        None => db::rows("select parent, root from chats where kind='thread'", []),
    };
    let mut roots: Vec<(i64, Vec<i64>)> = vec![];
    for r in rows {
        let (p, root) = (r.i(0), r.i(1));
        match roots.iter_mut().find(|(x, _)| *x == p) {
            Some((_, v)) => v.push(root),
            None => roots.push((p, vec![root])),
        }
    }
    let mut out = vec![];
    for (p, rs) in roots {
        let sums = thread_summaries(p, &rs);
        // Python 的 dict 按查询结果的顺序（group by c.id）
        let mut items: Vec<(i64, Value)> = sums.into_iter().collect();
        items.sort_by_key(|(_, s)| s["id"].as_i64().unwrap_or(0));
        for (root, s) in items {
            if !py::truthy(&s["count"]) || (unread_only && !py::truthy(&s["unread"])) {
                continue;
            }
            let head = db::row("select role, content, bot_id from messages where id=?", params![root]);
            let tail = db::row(
                &format!("select m.content, m.bot_id from messages m where m.chat_id=? and {REPLY} order by m.id desc limit 1"),
                params![s["id"].as_i64()],
            );
            let mut o = s.clone();
            o["parent"] = json!(p);
            o["root"] = json!(root);
            o["root_role"] = json!(head.as_ref().map(|h| h.s(0)).unwrap_or_default());
            o["root_text"] = json!(head.as_ref().map(|h| py::head(&h.s(1), 200)).unwrap_or_default());
            o["root_bot"] = head.as_ref().map(|h| h[2].clone()).unwrap_or(Value::Null);
            o["preview"] = json!(tail.as_ref().map(|t| py::head(&t.s(0), 200)).unwrap_or_default());
            o["preview_bot"] = tail.as_ref().map(|t| t[1].clone()).unwrap_or(Value::Null);
            out.push(o);
        }
    }
    out.sort_by(|a, b| {
        b["last"].as_f64().unwrap_or(0.0).partial_cmp(&a["last"].as_f64().unwrap_or(0.0)).unwrap_or(std::cmp::Ordering::Equal)
    });
    out
}

pub fn mark_seen(thread_id: i64, upto: Option<i64>) -> Option<Value> {
    if see(thread_id, upto, "kind = 'thread'") { thread_event(thread_id) } else { None }
}

pub fn mark_chat_seen(chat_id: i64, upto: Option<i64>) -> Option<i64> {
    if see(chat_id, upto, "kind != 'thread'") { Some(unread(chat_id)) } else { None }
}

fn see(chat_id: i64, upto: Option<i64>, kind: &str) -> bool {
    let top = db::scalar_i64("select coalesce(max(id), 0) from messages where chat_id=?", params![chat_id]);
    let upto = match upto.filter(|u| *u != 0) {
        Some(u) => u.min(top),
        None => top,
    };
    db::exec(&format!("update chats set seen=max(coalesce(seen, 0), ?) where id=? and {kind}"), params![upto, chat_id]) > 0
}

pub fn thread_event(chat_id: i64) -> Option<Value> {
    let (p, root) = memory::thread_of(chat_id)?;
    let s = thread_summaries(p, &[root]).remove(&root)?;
    let mut o = json!({"parent": p, "root": root});
    for (k, v) in s.as_object()? {
        o[k] = v.clone();
    }
    Some(o)
}

// ---------- 群聊 ----------
fn members(bot_ids: &Value) -> Result<Vec<i64>, String> {
    let mut ids = vec![];
    for x in bot_ids.as_array().cloned().unwrap_or_default() {
        let i = py::to_int(&x).ok_or_else(|| "invalid literal for int()".to_string())?;
        if !ids.contains(&i) {
            if get(i).is_none() {
                return Err(t!("没有 Bot #{id}", id = i));
            }
            ids.push(i);
        }
    }
    if !(GROUP_MIN..=GROUP_MAX).contains(&ids.len()) {
        return Err(t!("群聊要有 {min}–{max} 个 Bot", min = GROUP_MIN, max = GROUP_MAX));
    }
    Ok(ids)
}

pub fn auto_title(ids: &[i64]) -> String {
    let names: Vec<String> = ids.iter().filter_map(|i| get(*i)).map(|b| py::str_of(&b["name"])).collect();
    let head = names.iter().take(3).cloned().collect::<Vec<_>>().join(&t!("、"));
    if names.len() > 3 { head + &t!(" 等 {n} 个", n = names.len()) } else { head }
}

fn lead_of(ids: &[i64], lead: &Value) -> i64 {
    if let Some(l) = py::to_int(lead).filter(|l| !lead.is_null() && ids.contains(l)) {
        return l;
    }
    let p = db::scalar("select id from bots where is_primary=1", []).as_i64();
    match p {
        Some(p) if ids.contains(&p) => p,
        _ => ids[0],
    }
}

pub fn create_group(bot_ids: &Value, title: &str, lead: &Value) -> Result<Value, String> {
    let ids = members(bot_ids)?;
    if db::scalar_i64("select count(*) from chats where kind='group'", []) >= MAX_GROUPS {
        return Err(t!("最多 {n} 个群聊", n = MAX_GROUPS));
    }
    let t = py::head(title.trim(), 60);
    let t = if t.is_empty() { auto_title(&ids) } else { t };
    let id = db::insert(
        "insert into chats(kind, title, bot_ids, updated, lead) values('group', ?, ?, ?, ?)",
        params![t, ids_json(&ids), py::now(), lead_of(&ids, lead)],
    );
    Ok(chat(id).unwrap_or(Value::Null))
}

pub fn update_group(chat_id: i64, title: Option<&str>, bot_ids: &Value, lead: &Value, image: Option<&str>) -> Result<Value, String> {
    let g = chat(chat_id).filter(|g| g["kind"] == "group" && !py::truthy(&g["parent"])).ok_or_else(|| t!("没有这个群聊"))?;
    let ids = if bot_ids.is_null() { ids_of(&g) } else { members(bot_ids)? };
    if let Some(title) = title {
        let t = py::head(title.trim(), 60);
        db::exec("update chats set title=? where id=?", params![if t.is_empty() { auto_title(&ids) } else { t }, chat_id]);
    }
    if let Some(image) = image {
        if !image.is_empty() && !crate::avatars::valid(image) {
            return Err(t!("头像图片不存在，重新上传一次"));
        }
        db::exec("update chats set image=? where id=?", params![if image.is_empty() { None } else { Some(image) }, chat_id]);
    }
    let l = if lead.is_null() { g["lead"].clone() } else { lead.clone() };
    db::exec("update chats set bot_ids=?, lead=? where id=?", params![ids_json(&ids), lead_of(&ids, &l), chat_id]);
    if image.is_some() {
        crate::avatars::gc();
    }
    Ok(chat(chat_id).unwrap_or(Value::Null))
}

pub fn delete_group(chat_id: i64) {
    if db::exists("select 1 from chats where id=? and kind='group'", params![chat_id]) {
        delete_chat(chat_id);
        crate::avatars::gc();
        crate::media::gc();
    }
}

/// 首次启动：没有 Bot 时创建主 Bot，并把没归属的旧消息迁到它的对话里
pub fn ensure_default() {
    if !db::exists("select 1 from bots limit 1", []) {
        let _ = create("Nova", &json!({"shape": "sparkle", "color": "#7c9cff"}), "", true);
    }
    if let Some(p) = primary() {
        db::exec("update messages set chat_id=?, bot_id=? where chat_id is null", params![p["chat_id"].as_i64(), p["id"].as_i64()]);
    }
}
