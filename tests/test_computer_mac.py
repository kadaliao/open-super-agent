"""派到你的 Mac 上的任务（link.py、mac.py、agent.py）：令牌、可以同时连好几台、服务器和 Mac 上的小程序之间一来一回、
文件只在 Mac 的工作文件夹里、终端每台单独开（默认关）、Mac 上没权限就不能动屏幕、令牌不对连不上；
Bot 派任务时写 mac，先问你，同意了那个任务的电脑就是那台 Mac。服务器上从不用服务器自己当电脑。
服务器和 Mac 的小程序在同一个进程里直接对接（不走网络），屏幕的系统接口换成假的。"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from conftest import find, until
from fakellm import Call
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from osa import approvals, bots, computer, router, security, server, settings
from osa.config import CFG
from osa.computer import agent, host, link, mac as macs, tools as ctools
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
    """一台连着的 Mac：Mac 那边是真的本机电脑（工作文件夹在临时目录），服务器这边是 MacComputer。"""
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
    lid, _ = link.create("测试的 Mac mini")   # 连上时服务器按 hello 改名（serve）；这里不走网络，直接起好名字
    pipe = Pipe(pc)
    lk = link.Link(pipe, {"id": lid, "name": "Mac"}, {"name": "测试的 Mac mini", "desktop": True, "workspace": pc.workspace,
                                                       "perms": {"accessibility": True, "screen": True}})
    pipe.link = lk
    monkeypatch.setitem(link.online, lid, lk)
    c = macs.MacComputer(lid)
    monkeypatch.setattr(ctools, "current", lambda: c)   # 这个任务派到了这台 Mac
    c.mac, c.pipe, c.lk = pc, pipe, lk
    yield c
    link.conn.execute("delete from computer_links where id=?", (lid,))
    link.conn.commit()


def test_tokens():
    lid, token = link.create("我的 Mac")
    lid2, _ = link.create("我的 Mac")
    assert link.check(token) == {"id": lid, "name": "我的 Mac"} and link.check("x" + token) is None and link.check("") is None
    names = {r["id"]: r["name"] for r in link.rows()}
    assert names[lid] == f"我的 Mac #{lid}" and names[lid2] == f"我的 Mac #{lid2}"   # 名字重了加编号，派任务时分得清
    assert link.find(f"我的 mac #{lid}")["id"] == lid and link.find(f"#{lid2}")["id"] == lid2 and link.find("别的") is None
    assert not link.row(lid)["shell"]
    link.set_shell(lid, True)
    assert link.row(lid)["shell"] and not link.row(lid2)["shell"]                   # 终端每台单独开
    asyncio.run(link.revoke(lid))
    asyncio.run(link.revoke(lid2))
    assert link.check(token) is None and link.row(lid) is None


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


async def test_no_terminal_by_default_and_uploads_stay_in_workspace(mac):
    with pytest.raises(ComputerError, match="太旧"):                       # 旧版 App 的小程序（hello 里没有 shell）
        await mac.exec("ls")
    assert not mac.shell_enabled()
    assert "computer_shell" not in {s["function"]["name"] for s in ctools.schemas(mac)}
    assert "exec" not in mac.pipe.sent                                    # 服务器这边就拦下了，没发到 Mac
    with pytest.raises(ComputerError, match="工作文件夹"):
        await mac._helper_run({"op": "upload", "files": ["/etc/hosts"]})
    await mac.write_bytes("a.pdf", b"%PDF")
    await mac._helper_run({"op": "upload", "files": ["a.pdf"]})
    assert mac.mac.helper_calls[-1]["files"] == [str(mac.mac.root / "a.pdf")]   # Mac 上换成真实路径


async def test_terminal_when_turned_on(mac, monkeypatch):
    """新版 App 的小程序能执行命令（hello 里报 shell）；电脑面板里给这台打开了（哪台设备上都能开）才给 Bot。
    命令在 Mac 的工作文件夹里跑，密钥不传，每条照样先问你。"""
    mac.lk.can_shell = True
    with pytest.raises(ComputerError, match="终端没有打开"):
        await mac.exec("ls")                                              # 默认关
    assert not mac.shell_enabled() and "exec" not in mac.pipe.sent

    link.set_shell(mac.lid, True)
    monkeypatch.setenv("FOO_API_KEY", "k")
    monkeypatch.setenv("PLAIN_VAR", "ok")
    r = await mac.shell("pwd; echo hi > a.txt; env")
    assert mac.pipe.sent[-1] == "exec"                                    # 在 Mac 上跑的
    assert r.exit_code == 0 and r.stdout.splitlines()[0] == mac.mac.workspace
    assert (mac.mac.root / "a.txt").read_text() == "hi\n"
    assert "PLAIN_VAR=ok" in r.stdout and "FOO_API_KEY" not in r.stdout
    assert (await mac.exec("cat", stdin="from stdin")).stdout == "from stdin"
    r = await mac.shell("sleep 5", timeout=1)
    assert r.timed_out and r.exit_code == 124

    assert "computer_shell" in {s["function"]["name"] for s in ctools.schemas(mac)}
    assert "每条命令都要用户确认" in ctools.system_hint(mac) and "测试的 Mac mini" in ctools.system_hint(mac)
    user = Recorder(True)
    monkeypatch.setattr(approvals, "request", user)
    task = SimpleNamespace(id=6, bot_id=1, chat_id=1, goal="g", background=False)
    out, _ = await ctools.call(task, "computer_shell", {"cmd": "echo hi"}, _noop, key="6:")
    assert "hi" in out and user.asked[-1][0] == "computer_shell"         # 每条命令先问
    user.answer = False
    out, _ = await ctools.call(task, "computer_shell", {"cmd": "touch b.txt"}, _noop, key="6:")
    assert not (mac.mac.root / "b.txt").exists()                          # 你拒绝了就不执行

    mac.lk.can_shell = False                                              # 换回旧版 App：开着也用不了，说清楚为什么
    assert not mac.shell_enabled()
    with pytest.raises(ComputerError, match="太旧"):
        await mac.exec("ls")


async def test_screen_through_the_link(mac, monkeypatch):
    """那台 Mac 有两个权限就能用 desktop_*，动手在 Mac 上做，照样不碰自己的窗口。"""
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
    st = await mac.desk_state()
    assert "屏幕录制" in st["problem"] and st["accessibility"] and not st["screen"]   # 面板按这两个写明缺哪个
    assert not mac.desktop_enabled()                                      # 问过一次就知道了：不再给 desktop_*


async def test_offline(mac, monkeypatch):
    monkeypatch.delitem(link.online, mac.lid)
    st = await mac.status()
    assert st["state"] == "error" and "没连上" in st["detail"]
    with pytest.raises(ComputerError, match="没连上"):
        await mac.read_bytes("a.txt")
    assert not mac.desktop_enabled() and not mac.shell_enabled()


def test_several_macs_at_once(password):
    """令牌不对：连上就被关掉（4401）；两台 Mac 可以同时连着；同一台重连换成新连接。"""
    lid, token = link.create("Mac")
    lid2, token2 = link.create("另一台")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    def wait(pred):
        import time
        for _ in range(100):
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError("没等到")
    try:
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect("/api/computer/link", headers={"Authorization": "Bearer nope"}) as ws:
                ws.receive_text()
        assert e.value.code == 4401
        with client.websocket_connect("/api/computer/link", headers={"Authorization": f"Bearer {token}"}) as ws:
            ws.send_json({"type": "hello", "name": "我的 Mac mini", "desktop": True, "workspace": "/Users/x/ws"})
            wait(lambda: link.connected(lid))
            assert link.connected(lid).name == "我的 Mac mini" and not link.connected(lid).can_shell   # 旧版 App：不能执行命令
            with client.websocket_connect("/api/computer/link", headers={"Authorization": f"Bearer {token2}"}) as ws2:
                ws2.send_json({"type": "hello", "name": "MacBook", "shell": True})
                wait(lambda: link.connected(lid2))
                assert link.connected(lid) and link.connected(lid2).can_shell                         # 两台都连着
                assert link.row(lid)["connected"] and link.row(lid2)["connected"]
                assert link.row(lid2)["name"] == "MacBook"                                          # 名字用那台 Mac 报的
            wait(lambda: not link.connected(lid2))
            assert link.connected(lid)
    finally:
        asyncio.run(link.revoke(lid))
        asyncio.run(link.revoke(lid2))


def test_same_mac_relinked_replaces_old_row(password):
    """同一台 Mac 换了令牌再连上来（重新允许、装了另一份 App）：旧的那行删掉、旧令牌作废，还连着的断开；
    别的 Mac、不报编号的旧版 App 不受影响。"""
    old, old_token = link.create("Mac")
    other, _ = link.create("Mac")
    legacy, _ = link.create("Mac")
    new, new_token = link.create("Mac")
    link.conn.execute("update computer_links set device='aaa' where id=?", (old,))
    link.conn.execute("update computer_links set device='bbb' where id=?", (other,))
    link.conn.commit()
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    def wait(pred):
        import time
        for _ in range(100):
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError("没等到")
    try:
        with client.websocket_connect("/api/computer/link", headers={"Authorization": f"Bearer {old_token}"}) as ws_old:
            ws_old.send_json({"type": "hello", "name": "Xingyi的Mac mini", "device": "aaa"})
            wait(lambda: link.connected(old))
            with client.websocket_connect("/api/computer/link", headers={"Authorization": f"Bearer {new_token}"}) as ws:
                ws.send_json({"type": "hello", "name": "Xingyi的Mac mini", "device": "aaa"})
                wait(lambda: link.row(old) is None)
                assert link.check(old_token) is None and not link.connected(old)   # 旧令牌作废，旧连接断开
                assert link.connected(new) and link.row(other) and link.row(legacy)
                names = [r["name"] for r in link.rows() if r["id"] in (new, other, legacy)]
                assert "Xingyi的Mac mini" in names   # 只剩一行叫这个名字，不用加编号
            with pytest.raises(WebSocketDisconnect):
                ws_old.receive_text()
    finally:
        for i in (old, other, legacy, new):
            asyncio.run(link.revoke(i))


def test_mac_api(no_password, monkeypatch):
    """电脑面板：列出 Mac（连着的带权限）、给某一台开终端（服务器锁不拦，动手的是你的 Mac）、移除；演示服务器上没有。"""
    lid, _ = link.create("Mac")
    monkeypatch.setattr(security, "local_exec", lambda: False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    try:
        got = client.get("/api/computer/macs").json()
        me = next(m for m in got if m["id"] == lid)
        assert me["connected"] is False and me["shell"] is False and me["can_shell"] is False and me["users"] == []
        r = client.put(f"/api/computer/macs/{lid}", json={"shell": True})
        assert r.status_code == 200 and r.json()["shell"] is True and link.row(lid)["shell"]
        assert client.put("/api/computer/macs/99999", json={"shell": True}).status_code == 404
        monkeypatch.setattr(security, "demo", lambda: True)
        assert client.put(f"/api/computer/macs/{lid}", json={"shell": False}).status_code == 403
        assert client.post("/api/computer/links", json={"name": "Mac"}).status_code == 403
        monkeypatch.setattr(security, "demo", lambda: False)
        assert client.delete(f"/api/computer/links/{lid}").json() == {"ok": True}
        assert link.row(lid) is None
    finally:
        asyncio.run(link.revoke(lid))


def test_server_never_uses_itself(monkeypatch):
    """服务器（Linux）上不用服务器自己当电脑：写了 host 也换成 Docker / 云电脑，界面上不给，设置里存不进去。
    你自己的 Mac 上（桌面 App，或者在 Mac 上直接跑服务）照常能用。以前存的 linked（借用你的 Mac）也换掉。"""
    monkeypatch.delenv("INSTABOX_API_KEY", raising=False)
    monkeypatch.setattr(computer, "_computers", {})
    monkeypatch.setattr(security, "local_exec", lambda: True)
    monkeypatch.setattr(computer.sys, "platform", "linux")
    monkeypatch.delenv("OSA_DESKTOP", raising=False)
    monkeypatch.setitem(CFG.setdefault("computer", {}), "backend", "host")
    assert not computer.host_allowed() and computer.backend_name() == "local"
    assert "host" not in computer.choices()
    assert next(x for x in computer.available() if x["id"] == "host")["ok"] is False
    with pytest.raises(ValueError, match="服务器"):
        settings.save({"computer_backend": "host"})
    monkeypatch.setitem(CFG["computer"], "backend", "linked")
    assert computer.backend_name() == "local"
    monkeypatch.setenv("OSA_DESKTOP", "1")
    monkeypatch.setitem(CFG["computer"], "backend", "host")
    assert computer.host_allowed() and computer.backend_name() == "host" and "host" in computer.choices()


async def test_dispatch_to_a_mac(llm, events, make_bot, mac, monkeypatch):
    """Bot 派任务时写 mac：先弹卡片问你；不同意任务就结束；同意了，这个任务里的电脑就是那台 Mac。"""
    monkeypatch.setattr(ctools, "current", computer.current)   # 用真的：看任务里拿到的是哪台
    seen = []

    async def fake_worker(task, goal, label=""):
        seen.append(computer.current())
        return "整理好了"
    monkeypatch.setattr(router, "run_worker", fake_worker)
    b = make_bot("Macer")
    name = link.row(mac.lid)["name"]
    llm.script("router:Macer", Call("dispatch", {"goal": "整理桌面上的文件", "mac": name}), "派了")
    await server.safe_handle(bots.chat(b["chat_id"]), "去我 Mac 上整理一下")
    pending = lambda: [e for e in events.of("approval") if e.get("status") == "pending" and e["tool"] == "use_mac"]   # noqa: E731
    card = (await until(pending, what="确认卡片"))[0]
    assert card["mac"] == mac.lid and json.loads(card["args"])["电脑"] == name and "always" in card["scopes"]
    assert find(b["chat_id"], kind="dispatch")[0]["meta"]["mac"] == name
    approvals.resolve(card["id"], False)
    res = (await until(lambda: find(b["chat_id"], kind="result"), what="任务结果"))[0]
    assert res["meta"]["status"] == "failed" and "没同意" in res["content"] and not seen

    llm.script("router:Macer", Call("dispatch", {"goal": "再整理一次", "mac": name}), "派了")
    await server.safe_handle(bots.chat(b["chat_id"]), "再来一次")
    card = (await until(lambda: pending()[1:], what="第二张卡片"))[0]
    approvals.resolve(card["id"], True)
    await until(lambda: len(find(b["chat_id"], kind="result")) >= 2, what="第二个任务的结果")
    assert seen == [macs.get(mac.lid)]                                     # 任务里的电脑是那台 Mac
    assert computer.current() is computer.get()                           # 任务外照常是共用电脑

    approvals.add_rule(f"use_mac:{name}", "allow")                         # 卡片上点过“总是允许”（或者规则只放行这一台）
    try:
        llm.script("router:Macer", Call("dispatch", {"goal": "第三次", "mac": name}), "派了")
        await server.safe_handle(bots.chat(b["chat_id"]), "第三次")
        await until(lambda: len(find(b["chat_id"], kind="result")) >= 3, what="第三个任务的结果")
        assert len(pending()) == 2 and len(seen) == 2                      # 不再弹卡片，直接在那台 Mac 上做
    finally:
        approvals.conn.execute("delete from rules")
        approvals.conn.commit()
    bots.delete(b["id"])


def test_pick_mac(mac, monkeypatch):
    name = link.row(mac.lid)["name"]
    assert router._pick_mac(name)[0]["id"] == mac.lid
    r, why = router._pick_mac("不存在的 Mac")
    assert r is None and "没有叫" in why and name in why
    monkeypatch.delitem(link.online, mac.lid)
    r, why = router._pick_mac(name)
    assert r is None and "没连上" in why
    monkeypatch.setattr(security, "demo", lambda: True)
    assert router._pick_mac(name)[0] is None
