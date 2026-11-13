"""所有 Bot 共用一台电脑（和 Grok Bot 一样），几个任务可以同时用，各自一个浏览器窗口（见 base.py）。
后端由 config.yaml 的 computer.backend 决定（设置里也能换）：host（这台 Mac 的 Chrome，见 host.py）/ local（Docker 沙盒）/ instabox（云电脑），
auto 是桌面 App 里用 host，否则有 Instabox 的 API key（界面里填的或环境变量 INSTABOX_API_KEY）用 Instabox、没有用 Docker。
在设置 → 电脑里选，选好以后一直用那一台（电脑面板里只显示是哪台）。
host 只在你自己的 Mac 上有（桌面 App 单独运行，或者在 Mac 上直接跑服务）：服务器上从不直接用它自己这台机器。

另外，服务器上的 Bot 可以把某个任务派到你的 Mac 上做（mac.py、link.py）：那个任务里的电脑工具用的是那台 Mac，
别的照常用这台共用电脑。current() 给出“这个任务用的电脑”。
"""
import asyncio
import contextvars
import os
import shutil
import sys
import time
from .. import i18n, security
from ..config import CFG
from ..memory import conn
from . import link, mac
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
_macs_queued = False


def _macs_changed():
    """哪台 Mac 连上、断开、谁在用、改了设置：告诉前端（几件事前后脚发生时只发一次）。"""
    global _macs_queued
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return   # 不在事件循环里（命令行、测试）：没有网页要通知
    if notify and not _macs_queued:
        _macs_queued = True

        async def go():
            global _macs_queued
            await asyncio.sleep(0.2)
            _macs_queued = False
            try:
                await notify({"type": "macs", "macs": await macs_state()})
            except Exception:
                pass
        background(go())


link.on_change = _macs_changed
mac.on_change = _macs_changed


def background(coro):
    """在后台跑，留着引用，免得还没跑完就被回收。"""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


mac.background = background


BACKENDS = ("host", "local", "instabox")


def host_allowed() -> bool:
    """能不能用服务所在的这台机器当电脑：只有你自己的 Mac（桌面 App 单独运行，或者在 Mac 上直接跑服务），
    而且没上服务器锁。服务器（Linux）上从不直接用它自己：Bot 在上面开浏览器、执行命令太危险，只用 Docker 沙盒或云电脑。"""
    return bool(os.environ.get("OSA_DESKTOP") or sys.platform == "darwin") and security.local_exec()


def backend_name() -> str:
    b = (CFG.get("computer") or {}).get("backend", "auto")
    if b == "auto" and os.environ.get("OSA_DESKTOP"):
        b = "host"
    if b not in BACKENDS or (b == "host" and not host_allowed()):   # auto；以前的 linked（借用你的 Mac）；服务器上的 host
        from .instabox import key_source
        return "instabox" if key_source() else "local"
    return b


def choices() -> list[str]:
    """电脑面板里给哪几种（config.yaml 的 computer.choices，没写就是全部）。比如服务器上不想用 Docker 就只写 [instabox]。
    这台机器（host）只在你自己的 Mac 上给。"""
    want = (CFG.get("computer") or {}).get("choices")
    return [b for b in BACKENDS if (not want or b in want) and (b != "host" or host_allowed())]


def available() -> list[dict]:
    """设置里能选的电脑，以及这台机器上能不能用（why：用不了的原因）。
    instabox 的 key：API key 从哪来（saved 界面里填的 / env 环境变量 / 空 还没有）。"""
    from .host import chrome_path
    from .instabox import key_source
    host = {"id": "host", "ok": bool(chrome_path())} if host_allowed() else {"id": "host", "ok": False, "locked": True}
    key = key_source()
    why = docker_problem()
    return [host, {"id": "local", "ok": not why, "why": why}, {"id": "instabox", "ok": bool(key), "key": key}]


def docker_problem() -> str:
    """Docker 沙盒为什么用不了；用得了返回空。compose 部署时电脑是单独的容器（服务经 unix socket 找它），
    那个容器没开（比如服务器为了省内存只用云电脑）就用不了；否则看这台机器装没装 docker 命令。"""
    from .local import SOCKET
    if SOCKET:
        return "" if _listening(SOCKET) else i18n.t("电脑容器没开（docker compose up -d computer）")
    return "" if shutil.which("docker") else i18n.t("这台电脑没装 Docker")


def docker_ok() -> bool:
    return not docker_problem()


def _listening(path: str) -> bool:
    """unix socket 那头有没有在听（容器停了，socket 文件还留在卷里，所以要真连一下）。"""
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


async def _quietly(coro):
    try:
        await coro
    except Exception:
        pass   # 比如 Docker 已经关了、Instabox 的 key 删掉了：暂停不了也不影响换过去的那台


def get(bot_id: int = SHARED) -> Computer:
    bot_id = SHARED
    c = _computers.get(bot_id)
    if c is None or c.backend != backend_name():
        if c is not None:   # 换成别的电脑了：旧的那台暂停（本机关掉 Chrome，Docker、Instabox 暂停省资源）
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
        else:
            from .local import LocalComputer
            c = LocalComputer(bot_id)
        c.width, c.height = cfg.get("width", 1280), cfg.get("height", 800)
        c.on_change = _changed
        c.background = background
        _computers[bot_id] = c
    return c


# 这个任务用的电脑：派到你的 Mac 上的任务是那台 Mac（router 在跑任务前设置），否则是共用电脑
TASK_PC: contextvars.ContextVar[Computer | None] = contextvars.ContextVar("osa_task_pc", default=None)


def current() -> Computer:
    return TASK_PC.get() or get()


def _users(c: Computer) -> list[dict]:
    return [{"key": k, "label": u["label"], "task_id": u.get("task_id"), "bot_id": u.get("bot_id"),
             "window": bool(u.get("window"))} for k, u in c.users.items()]


async def public_state(bot_id: int = SHARED) -> dict:
    """给前端的电脑状态。users 是正在用电脑的任务（各有一个浏览器窗口），waiting 是排队的。"""
    c = get(bot_id)
    extra = {"shell": c.shell_enabled(), "desktop": await c.desk_state()} if c.backend in HOSTS else {}
    return {"bot_id": bot_id, "backend": c.backend, "controller": c.controller, "width": c.width, "height": c.height,
            "parallel": c.slots(), "users": _users(c), "waiting": list(c.waiting.values()), "watch": c.watch,
            "exclusive": c.exclusive_label, "workspace": c.workspace, **extra, **(await c.status())}


async def mac_state(r: dict) -> dict:
    """一台 Mac 给前端的样子：连没连着、终端开没开（can_shell：那台的 App 能不能执行命令）、权限（desktop）、谁在用。"""
    lk = link.connected(r["id"])
    out = {**r, "can_shell": bool(lk and lk.can_shell), "users": [], "controller": "bot"}
    if lk:
        c = mac.get(r["id"])
        try:
            desk = await asyncio.wait_for(c.desk_state(), 10)
        except asyncio.TimeoutError:
            desk = {"supported": True, "problem": i18n.t("「{name}」没有回应（超时）", name=r["name"])}
        out |= {"users": _users(c), "waiting": list(c.waiting.values()), "controller": c.controller, "desktop": desk}
    return out


async def macs_state() -> list[dict]:
    """登记过的 Mac（派任务的去处）。演示服务器上没有。"""
    if security.demo():
        return []
    return list(await asyncio.gather(*(mac_state(r) for r in link.rows())))


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
