"""服务器借用你的 Mac（link.py、linked.py、agent.py）：借用令牌、服务器和 Mac 上的小程序之间一来一回、
文件只在 Mac 的工作文件夹里、没有终端、Mac 上没允许就不能动屏幕、令牌不对连不上。
服务器和 Mac 的小程序在同一个进程里直接对接（不走网络），屏幕的系统接口换成假的。"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from osa import approvals, server
from osa.config import CFG
from osa.computer import agent, host, link, linked, tools as ctools
from osa.computer.base import ComputerError
from test_host_desktop import Desk, Recorder, _noop, png


class Pipe:
    """冒充网络：服务器发出去的请求直接交给 Mac 上的小程序处理，回复再交回服务器。"""

    def __init__(self, pc):
        self.pc, self.link, self.sent = pc, None, []

    async def send_json(self, msg):
        self.sent.append(msg["op"])
        reply = SimpleNamespace(msg=None)

        class W:
            async def send(self, raw):
                reply.msg = json.loads(raw)
        await agent.reply(W(), self.pc, json.loads(json.dumps(msg)))
        self.link.resolve(reply.msg)

    async def close(self, code=1000):
        pass


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """借来的 Mac：Mac 那边是真的本机电脑（工作文件夹在临时目录），服务器这边是 LinkedComputer。"""
    monkeypatch.setattr(host, "DATA", tmp_path / "mac")
    monkeypatch.setattr(host, "chrome_path", lambda: "/fake/chrome")
    pc = host.HostComputer(0)
    pc.helper_calls = []

    async def helper(payload):
        pc.helper_calls.append(payload)
        return {"window": 1, "browser": "b", "title": "", "url": "", "elements": [], "text": "", "scroll": 0}
    pc._helper_run = helper

    async def running():
        return {"state": "running", "detail": ""}
    pc.status = running
    pipe = Pipe(pc)
    lk = link.Link(pipe, {"id": 1, "name": "Mac"}, {"name": "测试的 Mac mini", "desktop": True, "workspace": pc.workspace,
                                                     "perms": {"accessibility": True, "screen": True}})
    pipe.link = lk
    monkeypatch.setattr(link, "current", lk)
    c = linked.LinkedComputer(0)
    monkeypatch.setattr(ctools, "get", lambda: c)
    c.mac, c.pipe = pc, pipe
    return c


def test_tokens():
    lid, token = link.create("我的 Mac")
    assert link.check(token) == {"id": lid, "name": "我的 Mac"} and link.check("x" + token) is None and link.check("") is None
    assert any(x["id"] == lid for x in link.links())
    asyncio.run(link.revoke(lid))
    assert link.check(token) is None


async def test_files_live_on_the_mac(mac):
    await mac.write_bytes("notes/a.txt", b"hi")
    assert (mac.mac.root / "notes" / "a.txt").read_bytes() == b"hi"       # 写到了 Mac 的工作文件夹
    assert await mac.read_bytes("notes/a.txt") == b"hi"
    assert [x["name"] for x in await mac.ls(mac.mac.workspace + "/notes")] == ["a.txt"]
    assert mac.workspace == mac.mac.workspace                               # 路径用 Mac 上的
    with pytest.raises(ComputerError, match="工作文件夹"):
        await mac.read_bytes("../../etc/passwd")
    with pytest.raises(ValueError, match="目录不存在"):                       # 资料库要的是 ValueError
        await mac.ls(mac.mac.workspace + "/nope")
    await mac.rm(mac.mac.workspace + "/notes/a.txt")
    assert not (mac.mac.root / "notes" / "a.txt").exists()


async def test_no_terminal_and_uploads_stay_in_workspace(mac):
    with pytest.raises(ComputerError, match="终端"):
        await mac.exec("ls")
    assert not mac.shell_enabled()
    assert "computer_shell" not in {s["function"]["name"] for s in ctools.schemas(mac)}
    with pytest.raises(ComputerError, match="工作文件夹"):
        await mac._helper_run({"op": "upload", "files": ["/etc/hosts"]})
    await mac.write_bytes("a.pdf", b"%PDF")
    await mac._helper_run({"op": "upload", "files": ["a.pdf"]})
    assert mac.mac.helper_calls[-1]["files"] == [str(mac.mac.root / "a.pdf")]   # Mac 上换成真实路径


async def test_screen_through_the_link(mac, monkeypatch):
    """借出时同意的就包括屏幕：那台 Mac 有两个权限就能用 desktop_*，动手在 Mac 上做，照样不碰自己的窗口。"""
    d = Desk()
    d.install(monkeypatch)
    monkeypatch.setattr(approvals, "request", Recorder(True))
    task = SimpleNamespace(id=5, bot_id=1, chat_id=1, goal="g", background=False)
    assert mac.desktop_enabled() and ctools.DESKTOP <= {s["function"]["name"] for s in ctools.schemas(mac)}
    out, shot = await ctools.call(task, "desktop_click", {"x": 640, "y": 360}, _noop, key="5:")
    assert "已点击" in out and shot == png() and d.ops[-1][0] == "click" and "desk" in mac.pipe.sent
    assert (await mac.desk_state())["problem"] == ""
    assert await mac.peek() == png()
    d.el = {"role": "AXButton", "label": "允许", "pid": os.getpid()}          # Mac 上照样不碰自己的窗口
    out, _ = await ctools.call(task, "desktop_click", {"x": 1, "y": 1}, _noop, key="5:")
    assert "自己的窗口" in out

    d.perms["screen"] = False                                             # 那台 Mac 没给屏幕录制
    mac._info = (0.0, {})                                                 # 过了一秒，面板再问
    assert "屏幕录制" in (await mac.desk_state())["problem"]
    assert not mac.desktop_enabled()                                      # 问过一次就知道了：不再给 desktop_*


async def test_offline(mac, monkeypatch):
    monkeypatch.setattr(link, "current", None)
    st = await mac.status()
    assert st["state"] == "error" and "没连上" in st["detail"]
    with pytest.raises(ComputerError, match="没连上"):
        await mac.read_bytes("a.txt")
    assert not mac.desktop_enabled()


def test_handshake(password, monkeypatch):
    """令牌不对：连上就被关掉（4401）；令牌对：发完 hello 就算借上了；借着一台时另一台连不上（4409）。"""
    monkeypatch.setattr(link, "current", None)
    lid, token = link.create("Mac")
    lid2, token2 = link.create("另一台")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    try:
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect("/api/computer/link", headers={"Authorization": "Bearer nope"}) as ws:
                ws.receive_text()
        assert e.value.code == 4401
        with client.websocket_connect("/api/computer/link", headers={"Authorization": f"Bearer {token}"}) as ws:
            ws.send_json({"type": "hello", "name": "我的 Mac mini", "desktop": True, "workspace": "/Users/x/ws"})
            for _ in range(50):
                if link.connected():
                    break
                import time
                time.sleep(0.02)
            assert link.connected() and link.connected().name == "我的 Mac mini"
            assert next(x for x in link.links() if x["id"] == lid)["connected"]
            with pytest.raises(WebSocketDisconnect) as e:
                with client.websocket_connect("/api/computer/link", headers={"Authorization": f"Bearer {token2}"}) as ws2:
                    ws2.send_json({"type": "hello", "name": "MacBook"})
                    ws2.receive_text()
            assert e.value.code == 4409
    finally:
        asyncio.run(link.revoke(lid))
        asyncio.run(link.revoke(lid2))
        monkeypatch.setattr(link, "current", None)
