"""所有 Bot 共用一台电脑（和 Grok Bot 一样）。后端由 config.yaml 的 computer.backend 决定：
auto（有 INSTABOX_API_KEY 用 Instabox，否则本地 Docker）/ instabox / local。
"""
import asyncio
import os
import time
from ..config import CFG
from ..memory import conn
from .base import Computer, ComputerError, ExecResult

conn.execute("create table if not exists computers(bot_id integer primary key, backend text, ref text)")
conn.commit()

_computers: dict[int, Computer] = {}
SHARED = 0   # 共用电脑的编号；接口里仍保留 bot_id 参数，方便以后改回每个 Bot 一台


def backend_name() -> str:
    b = (CFG.get("computer") or {}).get("backend", "auto")
    if b == "auto":
        return "instabox" if os.environ.get("INSTABOX_API_KEY") else "local"
    return b


def get(bot_id: int = SHARED) -> Computer:
    bot_id = SHARED
    c = _computers.get(bot_id)
    if c is None or c.backend != backend_name():
        cfg = CFG.get("computer") or {}
        if backend_name() == "instabox":
            from .instabox import InstaboxComputer
            c = InstaboxComputer(bot_id, cfg.get("instabox_template", "agent-browser"), cfg.get("idle_pause_s", 900),
                                 cfg.get("instabox_machine") or None)
        else:
            from .local import LocalComputer
            c = LocalComputer(bot_id)
        c.width, c.height = cfg.get("width", 1280), cfg.get("height", 800)
        _computers[bot_id] = c
    return c


async def public_state(bot_id: int = SHARED) -> dict:
    """给前端的电脑状态。"""
    c = get(bot_id)
    return {"bot_id": bot_id, "backend": c.backend, "controller": c.controller,
            "width": c.width, "height": c.height, **(await c.status())}


async def idle_pauser():
    """闲置一段时间的电脑自动暂停，省资源（Instabox 那边也设置了自动暂停，这里是双保险）。"""
    while True:
        await asyncio.sleep(60)
        limit = (CFG.get("computer") or {}).get("idle_pause_s", 900)
        for c in list(_computers.values()):
            if c.controller == "bot" and time.time() - c.last_used > limit:
                try:
                    await c.pause()
                except Exception:
                    pass
