"""所有 Bot 共用一台电脑（和 Grok Bot 一样），几个任务可以同时用，各自一个浏览器窗口（见 base.py）。
后端由 config.yaml 的 computer.backend 决定（设置里也能换）：host（本机的 Chrome，见 host.py）/ linked（服务器借用你的 Mac，见 linked.py）/
local（本地 Docker）/ instabox，
auto 是桌面 App 里用 host，否则有 Instabox 的 API key（界面里填的或环境变量 INSTABOX_API_KEY）用 Instabox、没有用本地 Docker。
"""
import asyncio
import os
import shutil
import time
from .. import security
from ..config import CFG
from ..memory import conn
from . import link
from .base import HOSTS, Computer, ComputerError, ExecResult

conn.execute("create table if not exists computers(bot_id integer primary key, backend text, ref text)")
conn.commit()

_computers: dict[int, Computer] = {}
SHARED = 0   # 共用电脑的编号；接口里仍保留 bot_id 参数，方便以后改回每个 Bot 一台
notify = None   # 由 server 设置：async (event) -> None
_queued = False


def _changed():
    """谁在用电脑变了：告诉前端（几件事前后脚发生时只发一次）。"""
    global _queued
    if notify and not _queued:
        _queued = True

        async def go():
            global _queued
            await asyncio.sleep(0.2)
            _queued = False
            try:
                await notify({"type": "computer", "computer": await public_state()})
            except Exception:
                pass
        background(go())


_tasks: set = set()
link.on_change = _changed


def background(coro):
    """在后台跑，留着引用，免得还没跑完就被回收。"""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


BACKENDS = ("host", "linked", "local", "instabox")


def backend_name() -> str:
    b = (CFG.get("computer") or {}).get("backend", "auto")
    if b == "auto" and os.environ.get("OSA_DESKTOP"):
        b = "host"
    if b == "auto" or (b == "host" and not security.local_exec()) or (b == "linked" and security.demo()):   # 服务器锁：不用这台机器当电脑
        from .instabox import key_source
        return "instabox" if key_source() else "local"
    return b


def choices() -> list[str]:
    """电脑面板里给哪几种（config.yaml 的 computer.choices，没写就是全部）。比如服务器上不想用 Docker 就只写 [instabox]。
    “借用你的 Mac”（linked）不归这里管。"""
    want = (CFG.get("computer") or {}).get("choices")
    return [b for b in ("host", "local", "instabox") if not want or b in want]


def available() -> list[dict]:
    """设置里能选的电脑，以及这台机器上能不能用。instabox 的 key：API key 从哪来（saved 界面里填的 / env 环境变量 / 空 还没有）。"""
    from .host import chrome_path
    from .instabox import key_source
    host = {"id": "host", "ok": bool(chrome_path())} if security.local_exec() else {"id": "host", "ok": False, "locked": True}
    lk = link.connected()
    linked = {"id": "linked", "ok": bool(lk), **({"name": lk.name} if lk else {})}   # 服务器借用你的 Mac：连上了才能选
    key = key_source()
    return [host, *([] if security.demo() else [linked]), {"id": "local", "ok": bool(shutil.which("docker"))},
            {"id": "instabox", "ok": bool(key), "key": key}]


async def _quietly(coro):
    try:
        await coro
    except Exception:
        pass   # 比如 Docker 已经关了、Instabox 的 key 删掉了：暂停不了也不影响换过去的那台


def get(bot_id: int = SHARED) -> Computer:
    bot_id = SHARED
    c = _computers.get(bot_id)
    if c is None or c.backend != backend_name():
        if c is not None and c.backend != "linked":   # 换成别的电脑了：旧的那台暂停（本机关掉 Chrome，Docker、Instabox 暂停省资源）
            try:
                background(_quietly(c.pause()))
            except RuntimeError:
                pass   # 不在事件循环里（测试）
        cfg = CFG.get("computer") or {}
        if backend_name() == "instabox":
            from .instabox import InstaboxComputer
            c = InstaboxComputer(bot_id, cfg.get("instabox_template", "agent-browser"), cfg.get("idle_pause_s", 900),
                                 cfg.get("instabox_machine") or None)
        elif backend_name() == "host":
            from .host import HostComputer
            c = HostComputer(bot_id)
        elif backend_name() == "linked":
            from .linked import LinkedComputer
            c = LinkedComputer(bot_id)
        else:
            from .local import LocalComputer
            c = LocalComputer(bot_id)
        c.width, c.height = cfg.get("width", 1280), cfg.get("height", 800)
        c.on_change = _changed
        c.background = background
        _computers[bot_id] = c
    return c


async def public_state(bot_id: int = SHARED) -> dict:
    """给前端的电脑状态。users 是正在用电脑的任务（各有一个浏览器窗口），waiting 是排队的。"""
    c = get(bot_id)
    users = [{"key": k, "label": u["label"], "task_id": u.get("task_id"), "bot_id": u.get("bot_id"),
              "window": bool(u.get("window"))} for k, u in c.users.items()]
    lk = link.connected()
    extra = {"link": {"connected": bool(lk), "name": lk.name if lk else "",   # 借给服务器的 Mac 连没连上（换电脑时用）
                      "shell": bool(lk and lk.shell)}}                        # 那台 Mac 上的 App 能不能执行终端命令
    if c.backend in HOSTS:
        extra |= {"shell": c.shell_enabled(), "desktop": await c.desk_state()}
    return {"bot_id": bot_id, "backend": c.backend, "controller": c.controller, "width": c.width, "height": c.height,
            "parallel": c.slots(), "users": users, "waiting": list(c.waiting.values()), "watch": c.watch,
            "exclusive": c.exclusive_label, "workspace": c.workspace, **extra, **(await c.status())}


async def idle_pauser():
    """闲置一段时间的电脑自动暂停，省资源（Instabox 那边也设置了自动暂停，这里是双保险）。"""
    while True:
        await asyncio.sleep(60)
        limit = (CFG.get("computer") or {}).get("idle_pause_s", 900)
        for c in list(_computers.values()):
            if c.auto_pause and c.controller == "bot" and time.time() - c.last_used > limit:
                try:
                    await c.pause()
                except Exception:
                    pass


async def shutdown():
    """服务停止时关掉本机电脑的 Chrome（Docker、Instabox 的电脑不管，下次接着用）。"""
    for c in list(_computers.values()):
        if c.backend == "host":
            try:
                await c.pause()
            except Exception:
                pass
