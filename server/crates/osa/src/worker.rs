//! Worker：隔离的 ReAct 循环，带工具，单独执行一个任务。模型流式输出，每一步都记进任务过程（tasklog）。
//! 对应 osa/worker.py。

use crate::computer::tools as ctools;
use crate::registry::{self, Call, Out, WORKER};
use crate::task::Task;
use crate::tasklog::{self, Log, StepRef};
use crate::{approvals, bots, config, dsml, i18n, memories, memory, providers, py, router, taskstore, watchdog};
use futures::future::join_all;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::Semaphore;

pub const WORKER_PROMPT: &str = "你是执行任务的 Worker。用工具查证事实，给出结论时附上来源链接。
需要读写用户的外部服务（应用）时，使用名字带“应用名__”前缀的工具；任务里写了“@应用名”就必须用那个应用。
任务里写了按技能 /名字 做、却没附上做法或要用技能带的文件时，先用 use_skill 取。
写操作会先请用户确认；被拒绝时不要换个工具绕过去，按用户的意见调整或说明无法完成。
算数、日期、单位换算、统计和处理数据，有 run_code 时用它在沙箱里跑代码算，不要心算。
完成后直接输出最终答案（{lang}，简洁、结构清晰）。今天是 {date}。";

fn answer_lang() -> &'static str {
    if i18n::lang() == "en" { "默认用英文，任务是用别的语言写的就用那种语言" } else { "中文" }
}

pub const APPLET_GUIDE: &str = "做小应用（可交互的界面）时 format=html 并填 app，页面里直接用 window.osa（服务器会自动注入，不用引脚本）：
  osa.call(工具名, 参数对象) → 文字结果（只能用 app.tools 里列的工具；要确认的会弹卡片）；
  osa.send(文字) 替用户给你发一条消息（要 app.chat=true），你会像收到用户消息一样接着处理；
  osa.store.get(键) / set(键, 值) / all() / remove(键) 读写这个小应用的数据，你用 page_data 工具也能读写同一份数据，
  osa.on('data', fn) 在数据被改时收到通知。页面高度会自动适应内容，不要写死 100vh；不要用 fetch，外网和接口都连不上。
用户在对话里会直接看到小应用并能操作，所以回复里附上链接就行，不用复述界面内容。";

fn page_hint() -> String {
    format!("任务要求做成页面或报告时，用 save_page 保存，最终答案里附上返回的链接。\n{APPLET_GUIDE}")
}

pub const FILE_HINT: &str =
    "要把电脑里的文件（截图、画好的图、做好的表格和文档）交给用户时用 send_file，它们会显示在任务结果下面；不要自己拼下载链接。";

pub async fn run_worker(task: Arc<Task>, goal: String, label: String) -> anyhow::Result<String> {
    let claim = format!("{}:{label}", task.id);
    let who = memory::Who { bot_id: Some(task.bot_id), chat_id: Some(task.chat_id), task_id: Some(task.id) };
    let t2 = task.clone();
    let fut = async move {
        let r = run_inner(t2.clone(), &goal, &label).await;
        ctools::release().await;
        r
    };
    ctools::CLAIM.scope(claim, memory::WHO.scope(who, fut)).await
}

/// 流式调用一次模型，思考和正文边收边写进任务过程。返回这一轮的 assistant 消息
pub async fn reply(
    task: &Arc<Task>,
    log: &Log,
    role: &str,
    client: &providers::Client,
    model: &str,
    msgs: &[Value],
    tools: Option<&[Value]>,
) -> anyhow::Result<Value> {
    let t0 = Instant::now();
    let mut body = json!({"model": model, "messages": msgs, "stream": true, "stream_options": {"include_usage": true}});
    if let Some(ts) = tools.filter(|t| !t.is_empty()) {
        body["tools"] = json!(ts);
    }
    for (k, v) in config::call_kwargs(role) {
        body[k] = v;
    }
    let rec = || memory::UsageRec {
        task_id: Some(task.id),
        bot_id: Some(task.bot_id),
        chat_id: Some(task.chat_id),
        ms: Some(t0.elapsed().as_secs_f64() * 1000.0),
        ..Default::default()
    };
    let mut stream = match client.stream(body).await {
        Ok(s) => s,
        Err(e) => {
            memory::record_error(role, model, &providers::explain(&e, &e.origin), rec());
            return Err(e.into());
        }
    };
    let (mut content, mut reasoning) = (String::new(), String::new());
    let mut calls: BTreeMap<i64, (String, String, String)> = BTreeMap::new();
    let mut usage: Option<Value> = None;
    let mut first: Option<Instant> = None;
    let mut think: Option<StepRef> = None;
    let mut text: Option<StepRef> = None;
    let mut sent = 0usize;
    let say = |piece: &str, think: &Option<StepRef>, text: &mut Option<StepRef>| {
        if let Some(th) = think {
            log.finish(th, "done", None, None);
        }
        if text.as_ref().is_none_or(|t| t.lock().status != "running") {
            *text = Some(log.start("text", "", "", "running"));
        }
        log.append(text.as_ref().unwrap(), piece);
    };
    loop {
        let chunk = match stream.next().await {
            None => break,
            Some(Ok(c)) => c,
            Some(Err(e)) => {
                memory::record_error(role, model, &providers::explain(&e, &e.origin), rec());
                return Err(e.into());
            }
        };
        task.touch();
        if chunk.get("usage").is_some_and(|u| !u.is_null()) {
            usage = Some(chunk["usage"].clone());
        }
        let Some(d) = chunk["choices"].as_array().and_then(|c| c.first()).map(|c| c["delta"].clone()) else { continue };
        let rc = d["reasoning_content"].as_str().unwrap_or("");
        let dc = d["content"].as_str().unwrap_or("");
        let tcs = d["tool_calls"].as_array().cloned().unwrap_or_default();
        if first.is_none() && (!dc.is_empty() || !rc.is_empty() || !tcs.is_empty()) {
            first = Some(Instant::now());
        }
        if !rc.is_empty() {
            reasoning.push_str(rc);
            if think.as_ref().is_none_or(|t| t.lock().status != "running") {
                think = Some(log.start("think", "", "", "running"));
            }
            log.append(think.as_ref().unwrap(), rc);
        }
        if !dc.is_empty() {
            content.push_str(dc);
            let n = dsml::visible(&content);
            if n > sent {
                let piece = content[sent..n].to_string();
                say(&piece, &think, &mut text);
                sent = n;
            }
        }
        for tc in tcs {
            let idx = tc["index"].as_i64().unwrap_or(0);
            let c = calls.entry(idx).or_default();
            if let Some(id) = tc["id"].as_str().filter(|s| !s.is_empty()) {
                c.0 = id.to_string();
            }
            if tc["function"].is_object() {
                c.1.push_str(tc["function"]["name"].as_str().unwrap_or(""));
                c.2.push_str(tc["function"]["arguments"].as_str().unwrap_or(""));
            }
        }
    }
    memory::record_usage(
        role,
        model,
        usage.as_ref(),
        memory::UsageRec {
            task_id: Some(task.id),
            bot_id: Some(task.bot_id),
            chat_id: Some(task.chat_id),
            ms: Some(t0.elapsed().as_secs_f64() * 1000.0),
            ttft: first.map(|f| (f - t0).as_secs_f64() * 1000.0),
            ..Default::default()
        },
    );
    let (content, leaked) = dsml::split(&content);
    if content.len() > sent {
        let piece = content[sent..].to_string();
        say(&piece, &think, &mut text);
    }
    let has_tools = tools.is_some_and(|t| !t.is_empty());
    if calls.is_empty() && has_tools {
        for (k, l) in leaked.into_iter().enumerate() {
            calls.insert(k as i64, (l.id, l.name, l.args));
        }
    }
    if let Some(th) = &think {
        log.finish(th, "done", None, None);
    }
    if let Some(tx) = &text {
        log.finish(tx, "done", None, Some(if calls.is_empty() { "answer" } else { "say" }));
    }
    let mut msg = json!({"role": "assistant"});
    if !content.is_empty() {
        msg["content"] = json!(content);
    }
    if !reasoning.is_empty() {
        msg["reasoning_content"] = json!(reasoning);
    }
    if !calls.is_empty() {
        msg["tool_calls"] = json!(
            calls
                .iter()
                .map(|(k, (id, name, args))| json!({
            "id": if id.is_empty() { format!("call_{k}") } else { id.clone() }, "type": "function",
            "function": {"name": name, "arguments": if args.is_empty() { "{}" } else { args }}}))
                .collect::<Vec<_>>()
        );
    }
    Ok(msg)
}

fn progress_text(tool: &registry::Tool, name: &str, args: &Value) -> String {
    match &tool.progress {
        Some(p) => p(args),
        None => {
            let q = if py::truthy(&args["query"]) { &args["query"] } else { &args["url"] };
            let q = if q.is_null() { String::new() } else { py::str_of(q) };
            format!("{name} {}", py::head(&q, 60))
        }
    }
}

/// 执行一个工具调用，返回 (给模型的结果, 这一步的状态)
async fn call_tool(
    task: &Arc<Task>,
    log: &Log,
    step: &StepRef,
    name: &str,
    raw: &str,
    env: &Value,
    shots: &mut Vec<ctools::Shot>,
) -> (String, String) {
    let args: Value = match serde_json::from_str(if raw.is_empty() { "{}" } else { raw }) {
        Ok(v) => v,
        Err(e) => return (format!("工具出错：{e}"), "error".into()),
    };
    let progress = |text: String| {
        task.st.lock().last_action = text.clone();
        log.update(step, Some(&text), None);
        router::progress(task);
    };
    // 重启时说不准有没有做成的写操作又要做一次：一律先问（“总是允许”的规则这次不算）
    let open = task.doubts.lock().iter().position(|d| d.settled.is_none() && d.tool == name);
    if let Some(i) = open {
        let d = task.doubts.lock()[i].clone();
        let checked = task.last_say.lock().clone();
        let extra = json!({"doubt": {"started": d.started, "tool": d.tool, "args": d.args, "checked": checked, "why": d.why}});
        log.update(step, None, Some("waiting"));
        let (ok, note, after) =
            approvals::request_with(task, name, &args, approvals::Req { once: true, ..Default::default() }, &extra).await;
        log.update(step, None, Some("running"));
        let how = match (ok, note.trim()) {
            (true, _) => "redo",
            (false, "done") => "done",
            (false, "stop") => "stop",
            _ => "skip",
        };
        if let Some(x) = task.doubts.lock().get_mut(i) {
            x.settled = Some(how.to_string());
        }
        taskstore::save_doubts(task);
        match how {
            "redo" => {
                return run_tool(task, log, step, name, after, env, shots, &progress, true).await;
            }
            "done" => return ("用户确认这一步之前已经做成，不用再做。".into(), "done".into()),
            "stop" => {
                router::cancel(task.id, &t!("用户在确认卡片上让任务停下"));
                return ("用户让任务停下。".into(), "rejected".into());
            }
            _ => return (format!("用户拒绝了这个操作，没有执行。{note}").trim().to_string(), "rejected".into()),
        }
    }
    run_tool(task, log, step, name, args, env, shots, &progress, false).await
}

/// 执行工具（confirmed：已经确认过，不再按规则问）
#[allow(clippy::too_many_arguments)]
async fn run_tool(
    task: &Arc<Task>,
    log: &Log,
    step: &StepRef,
    name: &str,
    args: Value,
    env: &Value,
    shots: &mut Vec<ctools::Shot>,
    progress: &(dyn Fn(String) + Sync),
    confirmed: bool,
) -> (String, String) {
    if ctools::is_name(name) {
        let (out, shot) = ctools::call(task, name, &args, progress, None).await;
        if let Some(s) = shot {
            shots.push(s);
        }
        return (out, "done".into());
    }
    let Some(tool) = registry::available(name, WORKER, env) else {
        return ("未知工具".into(), "error".into());
    };
    progress(progress_text(&tool, name, &args));
    let mut args = args;
    if !confirmed && tool.gated && approvals::decide(name, None, &args).0 {
        log.update(step, None, Some("waiting"));
        let (ok, note, after) = approvals::request(task, name, &args, approvals::Req::default()).await;
        if !ok {
            return (format!("用户拒绝了这个操作，没有执行。{note}").trim().to_string(), "rejected".into());
        }
        args = after;
        log.update(step, None, Some("running"));
    }
    let bot = bots::get(task.bot_id).unwrap_or(json!({"id": task.bot_id}));
    let call = Call {
        scope: WORKER,
        bot,
        chat_id: task.chat_id,
        private_chat_id: if task.approval_chat_id != 0 { task.approval_chat_id } else { task.chat_id },
        args,
        task: Some(task.clone()),
        post: None,
        files: task.files.clone(),
        apps: task.apps.clone(),
        env: env.clone(),
    };
    let run = tool.run.clone();
    // 工具 panic 了也当成工具出错，别把任务弄挂
    use futures::FutureExt;
    match std::panic::AssertUnwindSafe(watchdog::TASK.scope(task.clone(), run(call))).catch_unwind().await {
        Ok(Out::Text(s)) => (s, "done".into()),
        Ok(Out::Pic(s, png, cap)) => {
            shots.push(ctools::Shot { png, caption: Some(cap) });
            (s, "done".into())
        }
        Ok(Out::Shot(s, png)) => {
            if let Some(p) = png {
                shots.push(ctools::Shot::page(p));
            }
            (s, "done".into())
        }
        Err(_) => ("工具出错：内部错误".into(), "error".into()),
    }
}

/// 工具自己报了超时、连接断了这类错（应用那头可能其实做成了）
fn maybe_done(out: &str) -> bool {
    let head = py::head(out, 400).to_lowercase();
    (head.contains("出错") || head.contains("error") || head.contains("失败") || head.contains("failed"))
        && ["timeout", "timed out", "超时", "connection", "连接", "连不上", "broken pipe", "reset by peer", "eof"]
            .iter()
            .any(|k| head.contains(k))
}

/// 这个调用能不能和相邻的只读调用一起跑：能就返回 (组, 同组同时最多几个)
fn lane(call: &Value, env: &Value) -> Option<(String, usize)> {
    let name = call["function"]["name"].as_str().unwrap_or("");
    if ctools::is_name(name) {
        return None;
    }
    let tool = registry::available(name, WORKER, env)?;
    if tool.parallel < 1 {
        return None;
    }
    let raw = call["function"]["arguments"].as_str().unwrap_or("");
    let args: Value = serde_json::from_str(if raw.is_empty() { "{}" } else { raw }).ok()?;
    if !args.is_object() || (tool.gated && approvals::decide(name, None, &args).0) {
        return None;
    }
    Some((if tool.group.is_empty() { tool.name.clone() } else { tool.group.clone() }, tool.parallel))
}

async fn run_calls(task: &Arc<Task>, log: &Log, calls: &[Value], env: &Value) -> Vec<(String, Vec<ctools::Shot>)> {
    let mut results = vec![];
    let mut i = 0;
    while i < calls.len() {
        let mut lanes = vec![lane(&calls[i], env)];
        while lanes[0].is_some() && i + lanes.len() < calls.len() {
            match lane(&calls[i + lanes.len()], env) {
                Some(l) => lanes.push(Some(l)),
                None => break,
            }
        }
        let group = &calls[i..i + lanes.len()];
        let mut steps = vec![];
        for call in group {
            let name = call["function"]["name"].as_str().unwrap_or("").to_string();
            let raw = call["function"]["arguments"].as_str().unwrap_or("").to_string();
            {
                let mut s = task.st.lock();
                s.recent_calls.push(format!("{name}:{raw}"));
                let n = s.recent_calls.len();
                if n > 6 {
                    s.recent_calls.drain(..n - 6);
                }
            }
            steps.push(log.start("tool", &name, &py::head(&raw, 2000), "running"));
        }
        let sems: HashMap<String, Arc<Semaphore>> = if group.len() > 1 {
            lanes.iter().flatten().map(|(g, n)| (g.clone(), Arc::new(Semaphore::new(*n)))).collect()
        } else {
            HashMap::new()
        };
        let futs = group.iter().zip(steps.iter()).zip(lanes.iter()).map(|((call, step), ln)| {
            let sem = ln.as_ref().and_then(|(g, _)| sems.get(g).cloned());
            let task = task.clone();
            let log = log.clone();
            let step = step.clone();
            let env = env.clone();
            let name = call["function"]["name"].as_str().unwrap_or("").to_string();
            let raw = call["function"]["arguments"].as_str().unwrap_or("").to_string();
            let call_id = call["id"].clone();
            async move {
                let mut shots = vec![];
                let _permit = match &sem {
                    Some(s) => Some(s.acquire().await),
                    None => None,
                };
                let (mut out, status) = call_tool(&task, &log, &step, &name, &raw, &env, &mut shots).await;
                drop(_permit);
                if maybe_done(&out) && is_write(&name, &raw) {
                    out.push_str("\n（写操作超时或连接断了：这一步可能已经做成了。重做之前先用只读的办法查一下有没有做成。）");
                }
                log.finish(&step, &status, Some(&out), None);
                // 做完一个存一个：整轮做完前重启的话，做完的不用重做
                if env["pages"] == true {
                    taskstore::save_result(&task, &call_id, &py::head(&out, 8000));
                }
                task.touch();
                (out, shots)
            }
        });
        results.extend(join_all(futs).await);
        i += group.len();
    }
    results
}

/// 模型服务临时出错（408、429、5xx、连接断了、超时）：等一会儿重试
fn transient(e: &anyhow::Error) -> bool {
    match e.downcast_ref::<providers::ApiError>() {
        Some(a) => match a.status {
            None => true,
            Some(s) => s == 408 || s == 429 || s >= 500,
        },
        None => false,
    }
}

/// 临时出错后等几秒再试：limits.task_retry_s，默认 15、30、60 秒，第四次还不行才算失败
fn retry_waits() -> Vec<f64> {
    match config::get("limits.task_retry_s") {
        Value::Array(a) => a.iter().filter_map(|v| v.as_f64()).collect(),
        Value::Null => vec![15.0, 30.0, 60.0],
        v => v.as_f64().map(|w| vec![w]).unwrap_or_default(),
    }
}

/// 新的一步：Watchdog 觉得卡住了就取消它
fn new_step(task: &Task) -> tokio_util::sync::CancellationToken {
    let t = tokio_util::sync::CancellationToken::new();
    *task.step.lock() = t.clone();
    t
}

/// 调一次模型，临时出错按 retry_waits 重试。返回 None：这一步卡住了，被 Watchdog 叫停，要从上一步重试
async fn model_step(
    task: &Arc<Task>,
    log: &Log,
    client: &providers::Client,
    model: &str,
    msgs: &[Value],
    tools: Option<&[Value]>,
) -> anyhow::Result<Option<Value>> {
    let waits = retry_waits();
    let mut tries = 0;
    loop {
        // 停服务时存着过程的任务停在这里（Heavy 不存过程，照常做完）
        if task.store && taskstore::stopping() {
            return Err(taskstore::Parked.into());
        }
        let token = new_step(task);
        let r = tokio::select! {
            r = reply(task, log, "worker", client, model, msgs, tools) => r,
            _ = token.cancelled() => return Ok(None),
        };
        match r {
            Ok(m) => return Ok(Some(m)),
            Err(e) if transient(&e) && tries < waits.len() => {
                let wait = waits[tries];
                tries += 1;
                tasklog::end_open(task, "error");
                log.note(
                    &t!(
                        "模型服务临时出错：{error}。{n} 秒后重试（第 {k} 次）",
                        error = providers::brief(&e),
                        n = py::round_n(wait, 1),
                        k = tries
                    ),
                    "error",
                );
                task.touch();
                let end = py::now() + wait.max(0.0);
                while py::now() < end && !token.is_cancelled() && !(task.store && taskstore::stopping()) {
                    tokio::time::sleep(std::time::Duration::from_secs_f64((end - py::now()).clamp(0.0, 0.2))).await;
                }
                task.touch();
            }
            Err(e) => return Err(e),
        }
    }
}

/// 把收件箱里的话（用户补充的、系统的提醒）接进对话，并记进任务过程
fn take_inbox(task: &Arc<Task>, log: &Log, msgs: &mut Vec<Value>) -> bool {
    let got: Vec<crate::task::Note> = std::mem::take(&mut *task.inbox.lock());
    for n in &got {
        msgs.push(json!({"role": "user", "content": n.model}));
        if n.kind == "user" {
            let s = log.start("user", "", "", "running");
            log.finish(&s, "done", Some(&n.shown), None);
        } else {
            log.note(&n.shown, "done");
        }
    }
    !got.is_empty()
}

async fn run_inner(task: Arc<Task>, goal: &str, label: &str) -> anyhow::Result<String> {
    let (client, model) = providers::client_for("worker")?;
    let use_pc = config::get_bool("computer.enabled", true);
    let pages = label.is_empty();
    let seed = if pages { task.seed.lock().take() } else { None };
    let mut msgs = match seed {
        Some(m) => m,
        None => {
            let mut system = WORKER_PROMPT.replace("{date}", &py::strftime("%Y-%m-%d")).replace("{lang}", answer_lang());
            if pages {
                system += &format!("\n{}", page_hint());
            }
            if pages && use_pc {
                system += &format!("\n{FILE_HINT}");
            }
            if use_pc {
                system += &format!("\n\n{}", ctools::system_hint());
            }
            let known = memories::worker_block(task.bot_id);
            if !known.is_empty() {
                system += &format!("\n\n关于用户（长期记忆，做事时遵守其中的偏好）：\n{known}");
            }
            // 任务所属项目的记忆：只有这个项目的任务读到
            if let Some(p) = crate::board::project_of_task(task.id) {
                let pm = memories::project_lines(p);
                if !pm.is_empty() {
                    let name = crate::board::project_label(p).unwrap_or_default();
                    system += &format!("\n\n这个任务属于项目 {name}，项目的记忆（做事时遵守）：\n{pm}");
                }
            }
            vec![json!({"role": "system", "content": system}), json!({"role": "user", "content": goal})]
        }
    };
    let env = json!({"scope": WORKER, "pages": pages, "use_pc": use_pc});
    let mut schemas = registry::schemas(WORKER, &env);
    if use_pc {
        schemas.extend(ctools::schemas());
    }
    let log = Log::new(&task, label);
    // 只有正式的 Worker（不是 Heavy 的草稿）存过程
    let store = pages && task.store;
    let max = config::get_i64("limits.worker_max_steps", 30);
    let resume = task.resume_steps.lock().take().filter(|_| store);
    if let Some(plan) = resume {
        resume_tail(&task, &log, &mut msgs, plan, &env).await;
    }
    for _ in 0..max {
        take_inbox(&task, &log, &mut msgs);
        if store {
            taskstore::save(&task, &msgs);
        }
        // 正在停服务：过程存好了，停在这里（任务留着 running，启动后接着做）
        if store && taskstore::stopping() {
            return Err(taskstore::Parked.into());
        }
        let mark = msgs.len();
        ctools::prune_images(&mut msgs, 2);
        let Some(m) = model_step(&task, &log, &client, &model, &msgs, Some(&schemas)).await? else {
            stalled(&task, &log, &mut msgs, mark);
            continue;
        };
        {
            let mut s = task.st.lock();
            s.steps += 1;
            s.last_progress = py::now();
        }
        let Some(calls) = m["tool_calls"].as_array().cloned().filter(|c| !c.is_empty()) else {
            task.st.lock().stall_retried = false;
            let answer = m["content"].as_str().unwrap_or("").to_string();
            msgs.push(m);
            // 回答的时候用户又说了话：接着做，不急着交
            if !task.inbox.lock().is_empty() {
                continue;
            }
            if store {
                taskstore::save(&task, &msgs);
            }
            return Ok(answer);
        };
        *task.last_say.lock() = m["content"].as_str().unwrap_or("").to_string();
        msgs.push(m);
        if store {
            taskstore::save(&task, &msgs);
        }
        let token = new_step(&task);
        let results = tokio::select! {
            r = run_calls(&task, &log, &calls, &env) => Some(r),
            _ = token.cancelled() => None,
        };
        let Some(results) = results else {
            if store {
                // 卡住时取消了这一步：做完的调用结果留着，只读的重新执行，开始了、没有结果的写操作记成说不准
                // （这时不算“重试这一步”：模型拿到说不准的结果接着往下走）
                let open = taskstore::open_steps(task.id);
                msgs.extend(taskstore::saved_results(&task, &calls));
                tasklog::end_open(&task, "error");
                log.note(&t!("这一步一直没有进展：做完的留着，没做完的重新处理"), "error");
                task.touch();
                let plan = taskstore::Resume { steps: open, why: "stall", rerun: true };
                if resume_tail(&task, &log, &mut msgs, plan, &env).await {
                    task.st.lock().stall_retried = false;
                }
                continue;
            }
            stalled(&task, &log, &mut msgs, mark);
            continue;
        };
        task.st.lock().stall_retried = false;
        let mut shots = vec![];
        for (call, (out, got)) in calls.iter().zip(results) {
            msgs.push(json!({"role": "tool", "tool_call_id": call["id"], "content": py::head(&out, 8000)}));
            shots.extend(got);
        }
        if let Some(s) = shots.last() {
            msgs.push(ctools::image_message(&s.png, s.caption.as_deref(), true));
        }
    }
    log.note(&t!("步数用完了，根据已有信息写结果"), "done");
    take_inbox(&task, &log, &mut msgs);
    msgs.push(json!({"role": "user", "content": "步数用完了，请根据已有信息给出最终答案。"}));
    let m = loop {
        match model_step(&task, &log, &client, &model, &msgs, None).await? {
            Some(m) => break m,
            None => {
                let n = msgs.len();
                stalled(&task, &log, &mut msgs, n);
            }
        }
    };
    let answer = m["content"].as_str().unwrap_or("").to_string();
    msgs.push(m);
    if store {
        taskstore::save(&task, &msgs);
    }
    Ok(answer)
}

/// 存下的过程末尾那一轮 tool_calls 还没全部拿到结果（服务重启后恢复、卡住时取消了这一步、重试和接着做，三条路共用）。
/// 已有结果的照旧；没开始的、在等确认的、只读的：plan.rerun 时重新执行，不然给一句“没有执行 / 没做完”让模型重新决定；
/// 开始了、没有结果的写操作不重做，结果写成“不知道有没有做成”，记进任务的 doubts（存数据库），之后再调同一个工具时一律先问。
/// 返回有没有记下说不准的写操作
async fn resume_tail(task: &Arc<Task>, log: &Log, msgs: &mut Vec<Value>, plan: taskstore::Resume, env: &Value) -> bool {
    let Some(ai) = msgs.iter().rposition(|m| m["role"] == "assistant") else { return false };
    let Some(calls) = msgs[ai]["tool_calls"].as_array().cloned().filter(|c| !c.is_empty()) else { return false };
    let mut have: HashMap<String, Value> = HashMap::new();
    for m in &msgs[ai + 1..] {
        if m["role"] == "tool" {
            have.insert(py::str_of(&m["tool_call_id"]), m.clone());
        }
    }
    if calls.iter().all(|c| have.contains_key(&py::str_of(&c["id"]))) && msgs.len() == ai + 1 + have.len() {
        return false; // 都有结果，原样
    }
    msgs.truncate(ai + 1);
    let mut steps = plan.steps;
    let mut rerun = vec![];
    let mut fixed: HashMap<String, String> = HashMap::new();
    let mut doubted = false;
    for c in &calls {
        let id = py::str_of(&c["id"]);
        if have.contains_key(&id) {
            continue;
        }
        let name = c["function"]["name"].as_str().unwrap_or("").to_string();
        let raw = c["function"]["arguments"].as_str().unwrap_or("").to_string();
        let detail = py::head(&raw, 2000);
        let step = steps.iter().position(|s| s.name == name && s.detail == detail).map(|i| steps.remove(i));
        let started = step.as_ref().filter(|s| s.status != "waiting");
        match started {
            Some(s) if !redo_safe(&name, c, env) => {
                let args = py::head(&raw, 1000);
                fixed.insert(id, taskstore::doubt_text(plan.why, &name, &args, s.ts));
                task.doubts.lock().push(taskstore::Doubt {
                    tool: name.clone(),
                    args,
                    started: s.ts,
                    settled: None,
                    noted: false,
                    why: plan.why.to_string(),
                });
                doubted = true;
                let note = match plan.why {
                    "stall" => t!("{tool} 这一步卡住超时被打断，说不准有没有做成：先让 Bot 查一下，要再做会先问你", tool = name),
                    "stopped" => t!("任务被停下时 {tool} 这一步正在执行，说不准有没有做成：先让 Bot 查一下，要再做会先问你", tool = name),
                    _ => t!("服务重启时 {tool} 这一步正在执行，说不准有没有做成：先让 Bot 查一下，要再做会先问你", tool = name),
                };
                log.note(&note, "done");
            }
            _ if plan.rerun => rerun.push(c.clone()),
            Some(_) => {
                fixed.insert(id, "这一步没有做完任务就停下了（只读的，没有影响）。还需要的话重新调用。".into());
            }
            None if step.is_some() => {
                fixed.insert(id, "这一步没有执行：任务停下时它还在等用户确认。还需要的话重新调用（会再请用户确认）。".into());
            }
            None => {
                fixed.insert(id, "这一步没有执行：任务停下时它还没开始。还需要的话重新调用。".into());
            }
        }
    }
    if doubted {
        taskstore::save_doubts(task); // 新记下的和之前没了结的并在一起存
    }
    let results = if rerun.is_empty() { vec![] } else { run_calls(task, log, &rerun, env).await };
    let mut shots = vec![];
    for (c, (out, got)) in rerun.iter().zip(results) {
        fixed.insert(py::str_of(&c["id"]), out);
        shots.extend(got);
    }
    for c in &calls {
        let id = py::str_of(&c["id"]);
        if let Some(m) = have.remove(&id) {
            msgs.push(m);
        } else {
            let out = fixed.remove(&id).unwrap_or_default();
            msgs.push(json!({"role": "tool", "tool_call_id": c["id"], "content": py::head(&out, 8000)}));
        }
    }
    if let Some(s) = shots.last() {
        msgs.push(ctools::image_message(&s.png, s.caption.as_deref(), true));
    }
    doubted
}

/// 是不是写操作（重做一遍会有后果的）：工具名和参数（任务过程里存的那份）。拿不准的算写操作
pub fn is_write(name: &str, raw: &str) -> bool {
    let env = json!({"scope": WORKER, "pages": true, "use_pc": config::get_bool("computer.enabled", true)});
    let call = json!({"function": {"name": name, "arguments": raw}});
    !redo_safe(name, &call, &env)
}

/// 重做一遍也没关系的调用：只读的（不用确认、能并行的：查询、读网页、读文件），和只是问用户的。
/// 电脑和浏览器的操作当写操作（它们能自己看屏幕、页面核对）；拿不准的一律当写操作
fn redo_safe(name: &str, call: &Value, env: &Value) -> bool {
    if ["ask_user", "ask_human", "ask_approval"].contains(&name) {
        return true;
    }
    if ctools::is_name(name) {
        return false;
    }
    // 应用的工具：只有判断为只读的（approvals::read_only）才标了能并行
    lane(call, env).is_some()
}

/// 这一步卡住了：丢掉做了一半的，从上一步重试
fn stalled(task: &Arc<Task>, log: &Log, msgs: &mut Vec<Value>, mark: usize) {
    msgs.truncate(mark);
    tasklog::end_open(task, "error");
    log.note(&t!("这一步一直没有进展，从上一步重试"), "error");
    task.touch();
}

/// Heavy 模式：N 个 Worker 独立并行解题，Reviewer 互审后汇总
pub async fn run_heavy(task: Arc<Task>) -> anyhow::Result<String> {
    let n = config::get_i64("heavy.workers", 3).max(1) as usize;
    let angles = ["从最直接的角度", "从反面/质疑的角度核查", "从数据和一手来源角度", "从实践/落地角度"];
    Log::new(&task, "").note(&t!("{n} 个研究员分头做，做完汇总", n = n), "done");
    let goal = format!("{}{}", task.goal, task.notes);
    let futs = (0..n).map(|i| {
        let task = task.clone();
        let g = format!("{goal}\n\n（你是第 {} 个独立研究员，请{}回答）", i + 1, angles[i % angles.len()]);
        async move { run_worker(task, g, format!("h{i}")).await }
    });
    let drafts = join_all(futs).await;
    let mut ok = vec![];
    for (i, d) in drafts.into_iter().enumerate() {
        match d {
            Ok(s) => ok.push(s),
            Err(e) => {
                Log::new(&task, &format!("h{i}")).note(&t!("出错：{error}", error = e.to_string()), "error");
            }
        }
    }
    let (client, model) = providers::client_for("reviewer")?;
    let joined = ok.iter().enumerate().map(|(i, d)| format!("=== 方案 {} ===\n{d}", i + 1)).collect::<Vec<_>>().join("\n\n");
    Log::new(&task, "").note(&t!("{n} 份草稿交给审稿人汇总", n = ok.len()), "done");
    let msgs = vec![
        json!({"role": "system", "content": format!("你是审稿人。比较几份独立答案，找出矛盾和错误，合成一份最可靠的最终答案，保留来源链接。{}", i18n::model_hint())}),
        json!({"role": "user", "content": format!("问题：{goal}\n\n{joined}")}),
    ];
    let m = reply(&task, &Log::new(&task, "review"), "reviewer", &client, &model, &msgs, None).await?;
    Ok(m["content"].as_str().unwrap_or("").to_string())
}
