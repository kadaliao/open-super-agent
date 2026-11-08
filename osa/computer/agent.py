"""借给服务器的 Mac 上跑的小程序：桌面 App 连着服务器、你同意借用这台 Mac 后，App 在后台启动它（python -m osa.computer.agent）。

它主动连到服务器的 /api/computer/link（服务器那边见 link.py、linked.py），替服务器上的 Bot 操作这台 Mac。动手的代码就是本机电脑的（host.py）：
- Bot 专用的 Chrome：单独的配置目录，和“在这台电脑上运行”时共用，在那边登录过的网站这里也能用；
- 文件：读写、列目录、删除、往网页上传都只在工作文件夹里（服务器给的路径在 Mac 上再查一遍）；
- 整个屏幕：借出时你在 Mac 上同意的就包括屏幕；能不能做看 App 有没有“辅助功能”和“屏幕录制”权限；
  屏幕上的动作在 Mac 上照样不碰 Open Super Agent 自己的窗口；
- 不执行终端命令。
断了隔几秒重连；令牌作废了（4401）隔几分钟再试；App 退出时跟着退出（OSA_PARENT_PID），退出前关掉 Bot 的 Chrome。

环境变量：OSA_LINK_URL（wss://服务器/api/computer/link）、OSA_LINK_TOKEN、OSA_DATA_DIR。
"""
import asyncio
import base64
import json
import logging
import os
import signal
import socket
import subprocess
import sys

import websockets

from ..config import CFG
from . import macdesk
from .base import ComputerError
from .host import HostComputer

log = logging.getLogger("osa.agent")
MAX_MSG = 64 << 20
RETRY = {4401: 300, 4409: 60}   # 令牌作废了、已经借了另一台：隔久一点再试


def computer_name() -> str:
    try:
        name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or socket.gethostname()


async def handle(pc: HostComputer, op: str, a: dict):
    """做服务器要的一件事。服务器那边对应的调用见 linked.py。"""
    if op == "helper":
        payload = dict(a["payload"])
        if payload.get("op") == "upload":   # 只能传工作文件夹里的文件
            payload["files"] = pc.upload_paths([str(x) for x in payload.get("files") or []])
        return await pc._helper_run(payload)
    if op == "status":
        return await pc.status()
    if op == "start":
        return await pc.start()
    if op == "pause":
        return await pc.pause()
    if op == "read":
        return base64.b64encode(await pc.read_bytes(str(a["path"]), int(a.get("limit") or 32 << 20))).decode()
    if op == "write":
        return await pc.write_bytes(str(a["path"]), base64.b64decode(a["data"]))
    if op == "ls":
        return await pc.ls(str(a["path"]))
    if op == "stat":
        return await pc.stat_paths([str(p) for p in a["paths"]])
    if op == "rm":
        return await pc.rm(str(a["path"]))
    if op == "desk_info":
        return await pc._desk_info() if macdesk.supported() else {"supported": False}
    if op in ("desk", "capture", "front", "grant"):
        if not macdesk.supported():
            raise ComputerError("这台电脑不是 Mac，不能操作整个屏幕")
        if op == "desk":
            r = await pc._desk_run(str(a["action"]), dict(a.get("a") or {}))
            return {**r, "png": base64.b64encode(r["png"]).decode()} if "png" in r else r
        if op == "capture":
            return base64.b64encode(await pc._capture(str(a.get("fmt") or "png"), int(a.get("quality") or 70))).decode()
        if op == "front":
            return await pc._front()
        return await pc.grant()
    raise ComputerError(f"不认识的操作：{op}")


async def reply(ws, pc: HostComputer, msg: dict):
    out: dict = {"id": msg.get("id")}
    try:
        out["result"] = await handle(pc, str(msg.get("op")), msg.get("args") or {})
    except ValueError as e:   # 资料库要分辨“目录不存在”这类
        out |= {"error": str(e), "kind": "value"}
    except (ComputerError, KeyError, TypeError, OSError, RuntimeError) as e:
        out["error"] = str(e) or type(e).__name__
    except Exception as e:
        log.exception("处理 %s 出错", msg.get("op"))
        out["error"] = f"{type(e).__name__}: {e}"
    try:
        await ws.send(json.dumps(out, ensure_ascii=False))
    except websockets.ConnectionClosed:
        pass


async def connect_loop(pc: HostComputer, url: str, token: str):
    hello = {"type": "hello", "name": computer_name(), "platform": sys.platform, "desktop": macdesk.supported(),
             "perms": macdesk.permissions() if macdesk.supported() else {}, "workspace": pc.workspace}
    wait, tasks = 1, set()
    while True:
        code = None
        try:
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=MAX_MSG,
                                          open_timeout=20, ping_interval=20, ping_timeout=30) as ws:
                await ws.send(json.dumps(hello, ensure_ascii=False))
                log.info("连上了 %s", url)
                wait = 1
                async for raw in ws:
                    t = asyncio.create_task(reply(ws, pc, json.loads(raw)))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        except websockets.ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            log.info("连接断了（%s）", code)
        except (OSError, asyncio.TimeoutError, websockets.InvalidHandshake, websockets.InvalidURI) as e:
            log.info("连不上：%s", e)
        delay = RETRY.get(code or 0, wait)
        wait = min(30, wait * 2)
        await asyncio.sleep(delay)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # 每次看 Chrome 开没开都记一行，太多
    url, token = os.environ.get("OSA_LINK_URL", ""), os.environ.pop("OSA_LINK_TOKEN", "")
    if not url or not token:
        sys.exit("要 OSA_LINK_URL 和 OSA_LINK_TOKEN")
    cfg = CFG.get("computer") or {}
    pc = HostComputer(0)
    pc.width, pc.height = cfg.get("width", 1280), cfg.get("height", 800)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def watch_parent():   # App 异常退出时跟着退出（和 server.parent_watch 一样，看那个进程还在不在）
        parent = int(os.environ.get("OSA_PARENT_PID") or 0)
        while parent:
            await asyncio.sleep(2)
            try:
                os.kill(parent, 0)
            except ProcessLookupError:
                stop.set()
                return
            except PermissionError:   # 还在，只是不归这个用户
                pass
    work = [asyncio.create_task(connect_loop(pc, url, token)), asyncio.create_task(watch_parent())]
    await stop.wait()
    log.info("退出，关掉 Bot 的 Chrome")
    for t in work:
        t.cancel()
    try:
        await asyncio.wait_for(pc.pause(), 10)   # 关掉 Bot 的 Chrome（登录状态在配置目录里，下次还在）
    except Exception as e:
        log.info("关 Chrome 没成功：%r", e)


if __name__ == "__main__":
    asyncio.run(main())
