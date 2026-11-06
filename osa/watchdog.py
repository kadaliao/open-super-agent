"""Watchdog：独立于 Worker 的定时巡检。发现超时、无进展、重复调用（死循环）或超预算时取消任务并通知。"""
import asyncio
import time
from .config import CFG
from . import i18n, memory


async def watchdog():
    from .router import TASKS, cancel
    lim = CFG["limits"]
    while True:
        await asyncio.sleep(lim["watchdog_interval_s"])
        now = time.time()
        for t in list(TASKS.values()):
            if t.status != "running":
                continue
            if t.waiting_approval:  # 在等用户确认，不算超时或卡死
                t.last_progress = now
                continue
            reason = None
            if now - t.started - t.paused_s > lim["task_timeout_s"]:
                reason = i18n.t("运行超过 {n} 秒", n=lim["task_timeout_s"])
            elif now - t.last_progress > 180:
                reason = i18n.t("3 分钟没有进展")
            elif len(t.recent_calls) >= 4 and len(set(t.recent_calls[-4:])) == 1:
                reason = i18n.t("连续重复同一个工具调用，疑似死循环")
            elif memory.tokens_today() > lim["daily_token_budget"]:
                reason = i18n.t("今日 token 预算用完")
            if reason:
                cancel(t.id, i18n.t("Watchdog：{reason}", reason=reason))
