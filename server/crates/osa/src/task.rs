//! 一个后台任务（Python 版 worker.py 的 Task）。不变的字段直接放，会变的放在 st 里。

use parking_lot::Mutex;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use tokio_util::sync::CancellationToken;

/// 私信的回信去向（见 dm.rs）
#[derive(Clone, Debug, PartialEq)]
pub struct Reply {
    pub bot: i64,
    pub chat: i64,
    pub depth: i64,
    pub then: Option<Box<Reply>>,
}

#[derive(Clone, Debug)]
pub struct Handoff {
    pub to: i64,
    pub note: String,
}

/// 交给 Worker 的一句话：kind 是 user（用户说的，过程里记成 user）或 note（系统的提醒）
#[derive(Clone, Debug)]
pub struct Note {
    pub kind: &'static str,
    /// 给模型看的
    pub model: String,
    /// 记进任务过程的
    pub shown: String,
}

impl Note {
    pub fn user(text: &str) -> Note {
        Note { kind: "user", model: format!("用户补充：{text}"), shown: text.to_string() }
    }
    pub fn system(model: &str, shown: &str) -> Note {
        Note { kind: "note", model: format!("（系统：{model}）"), shown: shown.to_string() }
    }
}

pub type OnDone = Box<dyn FnOnce(Arc<Task>) -> futures::future::BoxFuture<'static, ()> + Send>;

pub struct TaskState {
    pub status: String,
    pub started: f64,
    pub last_progress: f64,
    pub long_calls: HashMap<u64, f64>,
    pub steps: i64,
    pub recent_calls: Vec<String>,
    pub waiting_approval: Option<i64>,
    pub paused_s: f64,
    pub last_action: String,
    pub result: String,
    pub handoff: Option<Handoff>,
    pub reply: Option<Reply>,
    pub quiet: bool,
    pub files: Vec<Value>,
    pub apps: Vec<Value>,
    /// 卡住后已经从上一步重试过一次（这一步做完就清掉）
    pub stall_retried: bool,
    /// 结果发到哪个对话（在讨论串里回复任务后，之后的结果发进讨论串）
    pub result_chat: Option<i64>,
}

pub struct Task {
    pub id: i64,
    /// 冒充的任务（对话里直接操作电脑、画图、应用界面调用）：id 是文字
    pub fake_id: Option<String>,
    pub goal: String,
    pub mode: String,
    pub bot_id: i64,
    pub chat_id: i64,
    pub approval_chat_id: i64,
    pub background: bool,
    pub depth: i64,
    pub quiet_ok: bool,
    pub notes: String,
    pub mac: i64,
    pub mac_name: String,
    pub st: Mutex<TaskState>,
    pub cancel: CancellationToken,
    pub on_done: Mutex<Option<OnDone>>,
    pub files: Arc<Mutex<Vec<Value>>>,
    pub apps: Arc<Mutex<Vec<Value>>>,
    /// 第几轮（做完以后接着做就加 1）
    pub round: i64,
    /// 存着过程（taskstore）：做完以后还能接着说、能重试
    pub store: bool,
    /// 用户发给任务、还没交给 Worker 的话
    pub inbox: Mutex<Vec<Note>>,
    /// 接着做时从存下的过程开始（没有就从头开始）
    pub seed: Mutex<Option<Vec<Value>>>,
    /// 当前这一步：卡住时 Watchdog 取消它，Worker 从上一步重试
    pub step: Mutex<CancellationToken>,
    /// 这一轮收尾了（after_round 之后不再收话，收件箱里的话交给下一轮）
    pub closed: AtomicBool,
    /// 停服务时停在了步与步之间
    pub parked: AtomicBool,
    /// 重启后恢复时，重启那一刻还开着的工具步（taskstore::StepInfo）
    pub resume_steps: Mutex<Option<Vec<crate::taskstore::StepInfo>>>,
    /// 重启时说不准有没有做成的写操作
    pub doubts: Mutex<Vec<crate::taskstore::Doubt>>,
    /// 模型这一步调工具前说的话（卡片上的“查过什么”）
    pub last_say: Mutex<String>,
}

#[derive(Default)]
pub struct NewTask {
    pub id: i64,
    pub fake_id: Option<String>,
    pub goal: String,
    pub mode: String,
    pub bot_id: i64,
    pub chat_id: i64,
    pub approval_chat_id: i64,
    pub background: bool,
    pub handoff: Option<Handoff>,
    pub reply: Option<Reply>,
    pub depth: i64,
    pub quiet_ok: bool,
    pub notes: String,
    pub mac: i64,
    pub mac_name: String,
    pub round: i64,
    pub store: bool,
    pub seed: Option<Vec<Value>>,
    pub result_chat: Option<i64>,
}

impl Task {
    pub fn new(n: NewTask) -> Arc<Task> {
        let now = crate::py::now();
        Arc::new(Task {
            id: n.id,
            fake_id: n.fake_id,
            goal: n.goal,
            mode: if n.mode.is_empty() { "single".into() } else { n.mode },
            bot_id: n.bot_id,
            chat_id: n.chat_id,
            approval_chat_id: n.approval_chat_id,
            background: n.background,
            depth: n.depth,
            quiet_ok: n.quiet_ok,
            notes: n.notes,
            mac: n.mac,
            mac_name: n.mac_name,
            st: Mutex::new(TaskState {
                status: "running".into(),
                started: now,
                last_progress: now,
                long_calls: HashMap::new(),
                steps: 0,
                recent_calls: vec![],
                waiting_approval: None,
                paused_s: 0.0,
                last_action: String::new(),
                result: String::new(),
                handoff: n.handoff,
                reply: n.reply,
                quiet: false,
                files: vec![],
                apps: vec![],
                stall_retried: false,
                result_chat: n.result_chat,
            }),
            cancel: CancellationToken::new(),
            on_done: Mutex::new(None),
            files: Arc::new(Mutex::new(vec![])),
            apps: Arc::new(Mutex::new(vec![])),
            round: n.round.max(1),
            store: n.store,
            inbox: Mutex::new(vec![]),
            seed: Mutex::new(n.seed),
            step: Mutex::new(CancellationToken::new()),
            closed: AtomicBool::new(false),
            parked: AtomicBool::new(false),
            resume_steps: Mutex::new(None),
            doubts: Mutex::new(vec![]),
            last_say: Mutex::new(String::new()),
        })
    }

    /// 冒充的任务（Python 的 SimpleNamespace）
    pub fn fake(id: &str, bot_id: i64, chat_id: i64, goal: &str, background: bool) -> Arc<Task> {
        Task::new(NewTask {
            fake_id: Some(id.to_string()),
            goal: goal.into(),
            bot_id,
            chat_id,
            approval_chat_id: chat_id,
            background,
            ..Default::default()
        })
    }

    pub fn id_value(&self) -> Value {
        match &self.fake_id {
            Some(s) => json!(s),
            None => json!(self.id),
        }
    }

    pub fn status(&self) -> String {
        self.st.lock().status.clone()
    }

    pub fn touch(&self) {
        self.st.lock().last_progress = crate::py::now();
    }

    /// 在等什么：approval（等确认）、help（请用户接手电脑）、question（等用户回答），没在等是 None
    pub fn waiting_kind(&self) -> Option<String> {
        let aid = self.st.lock().waiting_approval?;
        let kind = crate::approvals::PENDING.lock().get(&aid).map(|p| p.item["kind"].as_str().unwrap_or("approval").to_string());
        Some(kind.unwrap_or_else(|| "approval".into()))
    }

    /// 卡住了：取消当前这一步，让 Worker 从上一步重试。已经重试过一次（还没做完一步）就返回 false
    pub fn retry_step(&self) -> bool {
        {
            let mut s = self.st.lock();
            if s.stall_retried {
                return false;
            }
            s.stall_retried = true;
            s.last_progress = crate::py::now();
        }
        self.step.lock().cancel();
        true
    }

    pub fn public(&self) -> Value {
        let waiting = self.waiting_kind();
        // 归在哪张卡上（看板）：从 need_tasks 读，合并了卡也对
        let need = if self.fake_id.is_none() { crate::board::need_of_task(self.id) } else { Value::Null };
        let s = self.st.lock();
        let mut o = json!({"id": self.id_value(), "goal": self.goal, "mode": self.mode, "status": s.status, "steps": s.steps,
            "bot_id": self.bot_id, "chat_id": self.chat_id, "started": s.started,
            "waiting_approval": s.waiting_approval, "last_action": s.last_action,
            "elapsed": crate::py::round0(crate::py::now() - s.started), "quiet": s.quiet,
            "waiting_kind": if s.status == "running" { json!(waiting) } else { Value::Null },
            "can_continue": self.store, "round": self.round, "need": need});
        if self.mac != 0 {
            o["mac"] = json!(self.mac_name);
        }
        o
    }
}
