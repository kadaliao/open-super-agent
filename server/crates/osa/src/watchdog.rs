//! Watchdog：独立于 Worker 的定时巡检。超时、无进展、重复调用（死循环）或超预算时取消任务。对应 osa/watchdog.py。

use crate::task::Task;
use crate::{config, memory, py, router};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

pub const STALL_S: f64 = 180.0;

/// 多久没动静算卡住：limits.task_stall_s，默认 3 分钟。卡住了先从上一步重试一次，再卡住才取消
pub fn stall_s() -> f64 {
    config::get_f64("limits.task_stall_s", STALL_S).max(1.0)
}
pub const SLACK_S: f64 = 60.0;

tokio::task_local! {
    /// 正在执行工具调用的任务（worker 设置）
    pub static TASK: Arc<Task>;
}

static KEYS: AtomicU64 = AtomicU64::new(1);

/// 自带时限、跑的时候不出声的工具调用：这期间按它自己的时限判断有没有卡住。不在任务里时什么都不做
pub struct LongCall {
    key: u64,
    task: Option<Arc<Task>>,
}

pub fn long_call(secs: f64) -> LongCall {
    let task = TASK.try_with(|t| t.clone()).ok();
    let key = KEYS.fetch_add(1, Ordering::Relaxed);
    if let Some(t) = &task {
        let mut s = t.st.lock();
        s.long_calls.insert(key, secs + SLACK_S);
        s.last_progress = py::now();
    }
    LongCall { key, task }
}

impl Drop for LongCall {
    fn drop(&mut self) {
        if let Some(t) = &self.task {
            let mut s = t.st.lock();
            s.long_calls.remove(&self.key);
            s.last_progress = py::now();
        }
    }
}

/// 没有进展（卡住了）：返回原因
pub fn stalled(t: &Task, now: f64) -> Option<String> {
    let stall = stall_s();
    let s = t.st.lock();
    let quiet = s.long_calls.values().copied().fold(stall, f64::max);
    if now - s.last_progress > quiet {
        return Some(if quiet == stall && stall == STALL_S {
            t!("3 分钟没有进展")
        } else if quiet == stall {
            t!("{n} 秒没有进展", n = stall as i64)
        } else {
            t!("工具调用超过 {n} 秒还没结束", n = quiet as i64)
        });
    }
    None
}

pub fn check(t: &Task, now: f64) -> Option<String> {
    let timeout = config::get_i64("limits.task_timeout_s", 600);
    let (started, paused) = {
        let s = t.st.lock();
        (s.started, s.paused_s)
    };
    if now - started - paused > timeout as f64 {
        return Some(t!("运行超过 {n} 秒", n = timeout));
    }
    if let Some(r) = stalled(t, now) {
        return Some(r);
    }
    let s = t.st.lock();
    let n = s.recent_calls.len();
    if n >= 4 && s.recent_calls[n - 4..].iter().all(|c| *c == s.recent_calls[n - 1]) {
        return Some(t!("连续重复同一个工具调用，疑似死循环"));
    }
    drop(s);
    if memory::tokens_today() > config::get_i64("limits.daily_token_budget", 2_000_000) {
        return Some(t!("今日 token 预算用完"));
    }
    None
}

pub async fn watchdog() {
    loop {
        let every = config::get_f64("limits.watchdog_interval_s", 15.0).max(1.0);
        tokio::time::sleep(std::time::Duration::from_secs_f64(every)).await;
        let now = py::now();
        for t in router::tasks() {
            if t.status() != "running" {
                continue;
            }
            if t.st.lock().waiting_approval.is_some() {
                t.st.lock().last_progress = now;
                continue;
            }
            if let Some(reason) = check(&t, now) {
                // 卡住了：单个 Worker 的任务先从上一步重试一次（Worker 那边记一笔），再卡住才取消
                let stall = stalled(&t, now).is_some_and(|r| r == reason);
                if stall && t.mode == "single" && t.retry_step() {
                    continue;
                }
                router::cancel(t.id, &t!("Watchdog：{reason}", reason = reason));
            }
        }
    }
}
