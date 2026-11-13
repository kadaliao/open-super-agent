"""Watchdog：独立于 Worker 的定时巡检。发现超时、无进展、重复调用（死循环）或超预算时取消任务并通知。"""
import asyncio
import contextlib
import time
from contextvars import ContextVar
from .config import CFG
from . import i18n, memory

STALL_S = 180   # 多久没有进展算卡住
SLACK_S = 60    # 自带时限的工具调用：到了它自己的时限再多等这么久（派到 Mac、沙箱外层各自还有十几二十秒余量）
TASK: ContextVar = ContextVar("osa_watch_task", default=None)   # 正在执行工具调用的任务（worker 设置）


@contextlib.contextmanager
def long_call(secs: float):
    """自带时限、跑的时候不出声的工具调用（终端命令、run_code）：这期间按它自己的时限判断有没有卡住。
    不然一条允许跑十分钟的命令，到三分钟就会被当成“没有进展”停掉。不在任务里（对话里、测试）时什么都不做。"""
    t = TASK.get()
    if t is None:
        yield
        return
    key = object()   # Heavy 模式几个 Worker 共用一个任务，可能同时有几个
    t.long_calls[key] = secs + SLACK_S
    t.last_progress = time.time()
    try:
        yield
    finally:
        t.long_calls.pop(key, None)
        t.last_progress = time.time()


def check(t, now: float) -> str | None:
    """这个任务该不该停：该停返回原因。"""
    lim = CFG["limits"]
    if now - t.started - t.paused_s > lim["task_timeout_s"]:
        return i18n.t("运行超过 {n} 秒", n=lim["task_timeout_s"])
    quiet = max([STALL_S, *t.long_calls.values()])
    if now - t.last_progress > quiet:
        return i18n.t("3 分钟没有进展") if quiet == STALL_S else i18n.t("工具调用超过 {n} 秒还没结束", n=int(quiet))
    if len(t.recent_calls) >= 4 and len(set(t.recent_calls[-4:])) == 1:
        return i18n.t("连续重复同一个工具调用，疑似死循环")
    if memory.tokens_today() > lim["daily_token_budget"]:
        return i18n.t("今日 token 预算用完")
    return None


async def watchdog():
    from .router import TASKS, cancel
    while True:
        await asyncio.sleep(CFG["limits"]["watchdog_interval_s"])
        now = time.time()
        for t in list(TASKS.values()):
            if t.status != "running":
                continue
            if t.waiting_approval:  # 在等用户确认，不算超时或卡死
                t.last_progress = now
                continue
            if reason := check(t, now):
                cancel(t.id, i18n.t("Watchdog：{reason}", reason=reason))
