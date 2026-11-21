//! 对话里生成视频：Router 的 generate_video 工具。各家接口怎么调按配方（generation.yaml），这里管流程。
//! 对应 osa/videos.py（流程见那里的说明）。

use crate::generation::{self, Poll, Ref};
use crate::registry::{Call, Out, PostFn};
use crate::task::{NewTask, Task};
use crate::web::{Res, ok};
use crate::{approvals, bots, config, db, images, media, memory, py, router, server};
use axum::routing::get;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};
use std::sync::LazyLock;
use std::time::Duration;

pub const TOOL: &str = "generate_video";
pub const WAIT_S: f64 = 40.0 * 60.0;
pub const POLL_S: [u64; 3] = [5, 10, 20];
pub const MAX_POLL_ERRORS: usize = 20;
pub const MAX_REFS: usize = 10;

pub const NO_MODEL: &str = "没生成：还没有能生成视频的模型。照实告诉用户：去 设置 → 模型 添加有视频模型的服务（硅基流动的 Wan、阿里云百炼的万相、智谱的 CogVideoX、火山方舟的 Seedance），再在“生成视频”那一行选模型。不要假装生成了，也不要派任务去做。";

pub fn choice() -> Value {
    let c = memory::setting("video_model").filter(|v| v.is_object()).unwrap_or(json!({}));
    json!({"provider": c["provider"].as_str().unwrap_or(""), "model": c["model"].as_str().unwrap_or("")})
}

pub fn choose(provider: &str, model: &str) {
    memory::set_setting("video_model", &json!({"provider": provider, "model": model}));
}

/// 能生成视频的模型；default 是现在用的（只收文字时）
pub async fn models() -> Value {
    let mut out = generation::catalog("video").await;
    out.sort_by_key(|x| {
        (generation::rank("video", x["model"].as_str().unwrap_or("")), py::str_of(&x["provider"]), py::str_of(&x["model"]))
    });
    let c = choice();
    let plain: Vec<&Value> = out.iter().filter(|x| x["needs_ref"] != true).collect();
    let chosen = out.iter().find(|x| x["provider"] == c["provider"] && x["model"] == c["model"]);
    let default = chosen.cloned().or_else(|| plain.first().map(|x| (*x).clone())).or_else(|| out.first().cloned()).unwrap_or(Value::Null);
    json!({"models": out, "default": default})
}

fn fits(m: &Value, refs: &[Ref]) -> bool {
    let (p, md) = (py::str_of(&m["provider"]), py::str_of(&m["model"]));
    generation::check(&generation::caps("video", &p, &md), refs, &md).is_empty()
}

pub async fn pick(refs: &[Ref]) -> Option<Value> {
    let d = models().await;
    let ok: Vec<Value> = d["models"].as_array().cloned().unwrap_or_default().into_iter().filter(|x| fits(x, refs)).collect();
    let c = choice();
    ok.iter().find(|x| x["provider"] == c["provider"] && x["model"] == c["model"]).cloned().or_else(|| ok.first().cloned())
}

static MID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9a-f]{24}$").unwrap());

fn find_video(x: &str) -> Result<Option<Value>, String> {
    if MID.is_match(x) {
        return media::info(&json!(x)).map(Some).ok_or_else(|| format!("对话里没有编号是 {x} 的视频"));
    }
    Ok(images::by_path(x, true))
}

pub async fn find_videos(items: &Value) -> Result<Vec<Value>, String> {
    let list: Vec<Value> = match items {
        Value::Array(a) => a.clone(),
        x if py::truthy(x) => vec![x.clone()],
        _ => vec![],
    };
    let mut out: Vec<Value> = vec![];
    for x in list.into_iter().take(3) {
        let x = if py::truthy(&x) { py::str_of(&x) } else { String::new() };
        let x = x.trim();
        let x = x.strip_prefix("[视频]").unwrap_or(x);
        let x = x.strip_prefix("[附件]").unwrap_or(x).trim().to_string();
        if x.is_empty() {
            continue;
        }
        let got = match find_video(&x)? {
            Some(g) => g,
            None => {
                let att = media::from_computer(&x, true).await?;
                if !py::truthy(&att["media"]) || !py::str_of(&att["mime"]).starts_with("video/") {
                    return Err(format!("{} 不是视频（只认 MP4、MOV、WebM，最大 {} MB）", py::str_of(&att["name"]), media::MAX_VIDEO_REF >> 20));
                }
                media::info(&att["media"]).ok_or_else(|| format!("电脑里没有这个文件：{x}"))?
            }
        };
        if !got["mime"].as_str().unwrap_or("").starts_with("video/") {
            return Err(format!("{} 不是视频", py::str_of(&got["name"])));
        }
        if out.iter().all(|r| r["id"] != got["id"]) {
            out.push(got);
        }
    }
    Ok(out)
}

fn refs_of(items: &[Value], need_url: bool) -> Result<Vec<Ref>, String> {
    let mut out = vec![];
    for it in items {
        let role = py::str_of(&it["role"]);
        let mid = py::str_of(&it["media"]);
        let got = if role != "video" { media::get(&mid) } else { None };
        let url = if role == "video" || need_url { generation::public_link(&mid) } else { String::new() };
        if (role == "video" || need_url) && url.is_empty() {
            return Err(t!("参考视频要给视频服务一个能从外网打开的地址，这台服务器的网页地址外面打不开"));
        }
        if role != "video" && got.is_none() {
            return Err(t!("参考图已经被清理掉了：{name}", name = py::str_of(&it["name"])));
        }
        let (mime, data) = match got {
            Some((m, d, _)) => (m, d),
            None => (it["mime"].as_str().unwrap_or("").to_string(), vec![]),
        };
        out.push(Ref {
            data,
            mime,
            name: py::str_of(&it["name"]),
            url,
            role,
            w: it["w"].as_i64().unwrap_or(0),
            h: it["h"].as_i64().unwrap_or(0),
        });
    }
    Ok(out)
}

fn shape(items: &[Value]) -> Vec<Ref> {
    items
        .iter()
        .map(|x| Ref {
            name: py::str_of(&x["name"]),
            role: py::str_of(&x["role"]),
            w: x["w"].as_i64().unwrap_or(0),
            h: x["h"].as_i64().unwrap_or(0),
            ..Default::default()
        })
        .collect()
}

fn seconds_for(m: &Value, want: &Value) -> Option<i64> {
    let secs: Vec<i64> = m["seconds"].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default();
    if secs.is_empty() {
        return None;
    }
    let w = if py::truthy(want) { py::to_int(want).unwrap_or(5) } else { 5 };
    generation::nearest_int(w, &secs)
}

pub fn price_text(price: Option<f64>) -> String {
    match price {
        None => t!("价格未知"),
        Some(p) if p == 0.0 => t!("免费"),
        Some(p) => t!("{price} / 段", price = format!("{}{}", config::get_str("pricing.currency"), crate::websearch::fmt_g(p))),
    }
}

/// generate_video 工具
pub async fn generate(bot: &Value, chat_id: i64, private_chat_id: i64, args: &Value, post: PostFn) -> String {
    let prompt = py::head(crate::builtin::arg_s(args, "prompt").trim(), 1000);
    if prompt.is_empty() {
        return "没生成：描述是空的".into();
    }
    let (pics, vids) =
        match async { Ok::<_, String>((images::find_refs(&args["images"]).await?, find_videos(&args["videos"]).await?)) }.await {
            Ok(x) => x,
            Err(e) => return format!("没生成：参考素材有问题：{e}"),
        };
    let use_ = args["image_use"].as_str().filter(|u| *u == "frames" || *u == "reference").unwrap_or("");
    let vid_items: Vec<Value> =
        vids.iter().map(|v| json!({"media": v["id"], "name": v["name"], "mime": v["mime"], "role": "video"})).collect();
    let items = |role: &str| -> Vec<Value> {
        let mut v: Vec<Value> = pics
            .iter()
            .map(|p| json!({"media": p["id"], "name": p["name"], "mime": p["mime"], "w": p["w"], "h": p["h"], "role": role}))
            .collect();
        v.extend(vid_items.clone());
        v
    };
    let tries: Vec<&str> = if !use_.is_empty() {
        vec![use_]
    } else if !pics.is_empty() && vids.is_empty() {
        vec!["frames", "reference"]
    } else if !pics.is_empty() {
        vec!["reference"]
    } else {
        vec!["frames"]
    };
    let mut m = None;
    let mut chosen = vec![];
    for tr in tries {
        chosen = items(if tr == "frames" { "frame" } else { "image" });
        m = pick(&shape(&chosen)).await;
        if m.is_some() {
            break;
        }
    }
    let Some(m) = m else {
        let d = models().await;
        let all = d["models"].as_array().cloned().unwrap_or_default();
        if all.is_empty() {
            return NO_MODEL.into();
        }
        let what: Vec<String> = all
            .iter()
            .take(8)
            .map(|x| {
                format!(
                    "{}（首尾帧 {}、参考图 {}、参考视频 {}）",
                    py::str_of(&x["model"]),
                    py::str_of(&x["frames"]),
                    py::str_of(&x["refs"]),
                    py::str_of(&x["videos"])
                )
            })
            .collect();
        return format!(
            "没生成：现在的视频模型都收不了这些参考素材。能用的模型和它们收什么：{}。照实告诉用户，或者少给几个、换一种用法再试。",
            what.join("、")
        );
    };
    if chosen.iter().any(|x| x["role"] == "video") && generation::public_base().is_empty() {
        return "没生成：参考视频要给视频服务一个能从外网打开的地址，这台服务器的网页地址外面打不开（比如在自己电脑上用的桌面 App），或者还没填（设置 → 聊天软件 → 网页地址）。照实告诉用户：可以不带参考视频，或者填好外面打得开的网页地址再试。".into();
    }
    let (provider, model) = (py::str_of(&m["provider"]), py::str_of(&m["model"]));
    let seconds = seconds_for(&m, &args["seconds"]);
    let price = generation::video_price(&provider, &model, seconds);
    let ratio = py::head(&crate::builtin::arg_s(args, "ratio"), 10);
    let plan = json!({"prompt": prompt, "provider": provider, "model": model, "title": m["title"], "seconds": seconds, "ratio": ratio, "refs": chosen, "price": price});
    let bot_id = bot["id"].as_i64().unwrap_or(0);
    if !approvals::decide(TOOL, None, &Value::Null).0 {
        let mid = start(bot_id, chat_id, &plan, &post).await;
        return if mid != 0 {
            format!(
                "开始生成了（模型 {model}{}），要几分钟，好了会自动显示在对话里，你不用再回来。简短告诉用户等一会儿。",
                seconds.map(|s| format!("，{s} 秒")).unwrap_or_default()
            )
        } else {
            "没生成：出错了".into()
        };
    }
    let fake = Task::new(NewTask {
        fake_id: Some(format!("对话·{}", py::str_of(&bot["name"]))),
        goal: t!("在对话里生成视频"),
        bot_id,
        chat_id,
        approval_chat_id: private_chat_id,
        ..Default::default()
    });
    let names = |role: &str| chosen.iter().filter(|x| x["role"] == role).map(|x| py::str_of(&x["name"])).collect::<Vec<_>>().join("、");
    let frames = chosen.iter().filter(|x| x["role"] == "frame").count();
    let mut shown = json!({"描述": prompt});
    if frames > 0 {
        shown[if frames == 1 { "开头画面" } else { "开头和结尾画面" }] = json!(names("frame"));
    }
    if !names("image").is_empty() {
        shown["参考图"] = json!(names("image"));
    }
    if !names("video").is_empty() {
        shown["参考视频"] = json!(names("video"));
    }
    shown["模型"] = json!(format!("{model} · {}", py::str_of(&m["title"])));
    if let Some(s) = seconds {
        shown["时长"] = json!(format!("{s} 秒"));
    }
    shown["价格"] = json!(price_text(price));
    tokio::spawn(async move {
        let (ok, _, _) = approvals::request(&fake, TOOL, &shown, approvals::Req::default()).await;
        if ok {
            start(bot_id, chat_id, &plan, &post).await;
        }
    });
    format!(
        "已弹出确认卡片（模型 {model}，{}），用户同意后会开始生成，要几分钟，好了自动显示在对话里，你不用再回来。简短告诉用户确认一下就行，不要说已经生成好了。",
        price_text(price)
    )
}

/// 在对话里放一条“正在生成”，后台提交、等结果。返回那条消息的 id
pub async fn start(bot_id: i64, chat_id: i64, plan: &Value, post: &PostFn) -> i64 {
    let mut meta = json!({});
    for k in ["prompt", "provider", "model", "seconds", "ratio", "price"] {
        meta[k] = plan[k].clone();
    }
    meta["refs"] = json!(
        plan["refs"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .map(|x| {
                let mut o = serde_json::Map::new();
                for k in ["media", "name", "role", "mime", "w", "h"] {
                    o.insert(k.into(), x.get(k).cloned().unwrap_or(Value::Null));
                }
                Value::Object(o)
            })
            .collect::<Vec<_>>()
    );
    meta["status"] = json!("running");
    meta["started"] = json!(py::now());
    let mut full = json!({"kind": "video_job"});
    for (k, v) in meta.as_object().unwrap() {
        full[k] = v.clone();
    }
    let mid = post("task".into(), format!("[生成视频] {}", py::str_of(&plan["prompt"])), chat_id, full.clone()).await;
    spawn(mid, chat_id, Some(bot_id), full);
    mid
}

fn spawn(mid: i64, chat_id: i64, bot_id: Option<i64>, meta: Value) {
    tokio::spawn(run(mid, chat_id, bot_id, meta));
}

fn update(mid: i64, chat_id: i64, meta: &mut Value, fields: Value) {
    for (k, v) in fields.as_object().cloned().unwrap_or_default() {
        meta[k] = v;
    }
    memory::update_meta(mid, &fields);
    server::broadcast(json!({"type": "message_meta", "chat_id": chat_id, "id": mid, "meta": meta}));
}

fn post_as(bot_id: Option<i64>, role: &str, content: &str, chat_id: i64, meta: Value) -> i64 {
    match bot_id.filter(|b| bots::get(*b).is_some()) {
        Some(b) => router::runtime(b).add(role, content, Some(chat_id), meta),
        None => memory::add(role, content, chat_id, bot_id, &meta),
    }
}

fn file_name(prompt: &str, data: &[u8]) -> String {
    static R: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"[\\/:*?"<>|\s]+"#).unwrap());
    let ext = match media::sniff(data) {
        Some("video/webm") => "webm",
        Some("video/quicktime") => "mov",
        _ => "mp4",
    };
    let w = R.replace_all(prompt.trim(), "-").into_owned();
    let w = py::head(&w, 24).trim_matches('-').to_string();
    format!("{}.{ext}", if w.is_empty() { "video".into() } else { w })
}

async fn run(mid: i64, chat_id: i64, bot_id: Option<i64>, mut meta: Value) {
    let started = meta["started"].as_f64().unwrap_or_else(py::now);
    let r: Result<(), String> = async {
        let mut job = meta["job"].clone();
        let (provider, model) = (py::str_of(&meta["provider"]), py::str_of(&meta["model"]));
        if !py::truthy(&job) {
            let rc = generation::recipe("video", &provider, &model).unwrap_or(json!({}));
            let refs = refs_of(&meta["refs"].as_array().cloned().unwrap_or_default(), rc["image_url"] == "url")?;
            let secs = if py::truthy(&meta["seconds"]) { py::to_int(&meta["seconds"]) } else { None };
            let ratio = meta["ratio"].as_str().map(String::from);
            job = generation::submit(&provider, &model, &py::str_of(&meta["prompt"]), &refs, secs, ratio.as_deref()).await?;
            let s = if py::truthy(&job["seconds"]) { job["seconds"].clone() } else { meta["seconds"].clone() };
            update(mid, chat_id, &mut meta, json!({"job": job, "seconds": s}));
        }
        let mut errors = 0;
        loop {
            let waited = py::now() - started;
            if waited > WAIT_S {
                return Err(t!("等了 {n} 分钟还没好", n = (WAIT_S / 60.0) as i64));
            }
            let s = if waited < 60.0 {
                POLL_S[0]
            } else if waited < 600.0 {
                POLL_S[1]
            } else {
                POLL_S[2]
            };
            tokio::time::sleep(Duration::from_secs(s)).await;
            match generation::poll(&job).await {
                Poll::Running(got) => {
                    errors = if got.is_some() { errors + 1 } else { 0 };
                    if errors >= MAX_POLL_ERRORS {
                        return Err(t!("查不到进度：{e}", e = got.unwrap_or_default()));
                    }
                }
                Poll::Failed(why) => return Err(if why.is_empty() { t!("服务说失败了") } else { why }),
                Poll::Done(data) => {
                    let item = media::save(&data, &file_name(&py::str_of(&meta["prompt"]), &data), generation::MAX_VIDEO, true)?;
                    if !item["mime"].as_str().unwrap_or("").starts_with("video/") {
                        return Err(t!("服务没有返回视频"));
                    }
                    let secs = meta["seconds"].as_i64();
                    let price =
                        if job["magpie"] == true { meta["price"].as_f64() } else { generation::video_price(&provider, &model, secs) };
                    generation::record("video", &provider, &model, price);
                    let att = json!({"name": item["name"], "size": item["size"], "media": item["id"], "mime": item["mime"]});
                    let mut m = json!({"kind": "video", "prompt": meta["prompt"], "model": model, "provider": provider, "price": price,
                                       "seconds": meta["seconds"], "attachments": [att]});
                    if py::truthy(&meta["refs"]) {
                        m["refs"] = meta["refs"].clone();
                    }
                    post_as(bot_id, "task", &format!("[生成视频] {}", py::str_of(&meta["prompt"])), chat_id, m);
                    update(
                        mid,
                        chat_id,
                        &mut meta,
                        json!({"status": "done", "media": item["id"], "elapsed": py::round0(py::now() - started)}),
                    );
                    return Ok(());
                }
            }
        }
    }
    .await;
    if let Err(e) = r {
        update(
            mid,
            chat_id,
            &mut meta,
            json!({"status": "failed", "error": py::head(&e, 300), "elapsed": py::round0(py::now() - started)}),
        );
        post_as(bot_id, "task", &t!("[没生成视频：{error}]", error = e), chat_id, json!({"kind": "note"}));
    }
}

/// 服务重启：没做完的接着查；还没提交上的算失败（不重新提交，免得花两份钱）
pub fn resume() {
    let rows = db::rows(
        "select id, chat_id, bot_id, meta from messages where json_extract(meta, '$.kind') = 'video_job' and json_extract(meta, '$.status') = 'running'",
        [],
    );
    for r in rows {
        use crate::db::RowExt;
        let meta = r.j(3);
        if py::truthy(&meta["job"]) {
            spawn(r.i(0), r.i(1), r.oi(2), meta);
        } else {
            memory::update_meta(r.i(0), &json!({"status": "failed", "error": t!("服务重启了，还没提交上；可以再说一次")}));
        }
    }
    let _ = params![0];
}

pub async fn tool_generate_video(c: Call) -> Out {
    let Some(post) = c.post.clone() else { return Out::Text("没生成：这里不能生成视频".into()) };
    Out::Text(generate(&c.bot, c.chat_id, c.private_chat_id, &c.args, post).await)
}

async fn video_models() -> Res {
    ok(models().await)
}

pub fn routes() -> axum::Router {
    axum::Router::new().route("/api/videos/models", get(video_models))
}
