"""本机电脑的“操作整台电脑”（host.py + tools.py 的 desktop_*）：默认关、第一次用先问、一次一个任务、你一动鼠标键盘就停、
屏幕锁着不做、点“发送”先问、不碰自己的窗口、坐标换算。系统接口（macdesk）全换成假的，不动真的鼠标键盘。
真的鼠标键盘在文件末尾，要 OSA_TEST_DESKTOP=1 才跑（会在屏幕上打开文本编辑打字）。"""
import asyncio
import os
import struct
import sys
import time
from types import SimpleNamespace

import pytest

from osa import approvals
from osa.config import CFG
from osa.computer import host, macdesk, tools as ctools


def png(w: int = 1280, h: int = 720) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


class Desk:
    """假的 macdesk：记下发了哪些鼠标键盘动作；idle 可以改（模拟你动了鼠标）。"""

    def __init__(self):
        self.ops, self.idle_s, self.lock = [], 999.0, False
        self.el = {"role": "AXButton", "label": "打开", "pid": 50}
        self.front_app = {"app": "备忘录", "pid": 50, "title": "笔记"}
        self.perms = {"accessibility": True, "screen": True}

    def install(self, mp):
        mp.setattr(macdesk, "supported", lambda: True)
        mp.setattr(macdesk, "permissions", lambda: dict(self.perms))
        mp.setattr(macdesk, "locked", lambda: self.lock)
        mp.setattr(macdesk, "idle", lambda: self.idle_s)
        mp.setattr(macdesk, "screen", lambda: (1920.0, 1080.0, 1280, 720))
        mp.setattr(macdesk, "capture", lambda fmt="png", q=70: (png(), 1280, 720))
        mp.setattr(macdesk, "cursor", lambda: (960.0, 540.0))
        mp.setattr(macdesk, "front", lambda: dict(self.front_app))
        mp.setattr(macdesk, "windows", lambda limit=12: [{"app": "备忘录", "pid": 50, "title": "笔记", "x": 0, "y": 0, "w": 1920, "h": 1080}])
        mp.setattr(macdesk, "element_at", lambda x, y: dict(self.el))
        mp.setattr(macdesk, "focused", lambda: {"role": "AXTextArea", "label": "", "pid": 50})
        for name in ("click", "drag", "scroll", "type_text", "press", "press_seq", "open_app"):
            mp.setattr(macdesk, name, lambda *a, _n=name, **k: self.ops.append((_n, a, k)) and "")


class Recorder:
    """冒充用户：记下弹出的确认卡片，按 answer 回答。"""

    def __init__(self, answer: bool):
        self.answer, self.asked = answer, []

    async def __call__(self, task, tool, args, kind="approval", once=False):
        self.asked.append((tool, args))
        return self.answer, ""


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "DATA", tmp_path / "data")
    monkeypatch.setattr(host, "chrome_path", lambda: "/fake/chrome")
    monkeypatch.setattr(ctools.asyncio, "sleep", _no_sleep)
    d = Desk()
    d.install(monkeypatch)
    monkeypatch.setitem(CFG.setdefault("computer", {}), "host_desktop", True)
    c = host.HostComputer(0)

    async def running():
        return {"state": "running", "detail": ""}
    c.status = running
    monkeypatch.setattr(ctools, "get", lambda: c)
    c.fake = d
    return c


_sleep = asyncio.sleep


async def _no_sleep(s):
    await _sleep(0)


async def _noop(_):
    pass


def task(i=7):
    return SimpleNamespace(id=i, bot_id=1, chat_id=1, goal="整理备忘录", background=False)


def names(c) -> set:
    return {s["function"]["name"] for s in ctools.schemas(c)}


async def test_permissions_are_the_switch(desk, monkeypatch):
    """没有开关：是 Mac、给了两个权限就有 desktop_*；缺权限、不是 Mac、服务器锁、config 里关掉都没有。"""
    monkeypatch.delitem(CFG["computer"], "host_desktop")                # 默认就开
    assert ctools.DESKTOP <= names(desk) and "整台电脑" in ctools.system_hint(desk)
    desk.fake.perms["screen"] = False                                    # 缺屏幕录制：只用浏览器
    assert not ctools.DESKTOP & names(desk) and not ctools.desk_schemas(desk) and "整台电脑" not in ctools.system_hint(desk)
    st = await desk.desk_state()
    assert st["supported"] and not st["screen"] and "屏幕录制" in st["problem"]   # 面板上提示去打开
    desk.fake.perms["screen"] = True
    monkeypatch.setitem(CFG["computer"], "host_desktop", False)          # config.yaml 里关掉
    assert not ctools.DESKTOP & names(desk)
    monkeypatch.setitem(CFG["computer"], "host_desktop", True)
    monkeypatch.setattr(macdesk, "supported", lambda: False)             # 不是 Mac
    assert not host.desktop_on() and await desk.desk_state() == {"supported": False}
    monkeypatch.setattr(macdesk, "supported", lambda: True)
    monkeypatch.setitem(CFG, "security", {"local_exec": False})          # 服务器锁
    assert not host.desktop_on()


async def test_first_use_asks_once_and_refusal_sticks(desk, monkeypatch):
    user = Recorder(True)
    monkeypatch.setattr(approvals, "request", user)
    text, shot = await ctools.call(task(), "desktop_screenshot", {}, _noop, key="7:")
    assert user.asked == [("desktop", {"任务": "整理备忘录"})]
    assert "1280×720" in text and "备忘录 — 笔记" in text and shot.caption == "整个屏幕的截图"
    assert "整个屏幕" in ctools.image_message(shot)["content"][0]["text"]
    text, _ = await ctools.call(task(), "desktop_screenshot", {"save": True}, _noop, key="7:")
    saved = next(desk.root.glob("screenshots/*.png"))
    assert saved.read_bytes() == png() and str(saved.name) in text and "send_file" in text   # 存进工作文件夹，能发给用户
    await ctools.call(task(), "desktop_open_app", {"name": "备忘录"}, _noop, key="7:")
    assert len(user.asked) == 1 and desk.fake.ops[-1][0] == "open_app"   # 同一个任务只问一次
    await desk.release("7:")

    no = Recorder(False)
    monkeypatch.setattr(approvals, "request", no)
    out, _ = await ctools.call(task(8), "desktop_click", {"x": 1, "y": 1}, _noop, key="8:")
    assert "没同意" in out and not desk.desk_owner                      # 不同意：屏幕让出来
    out, _ = await ctools.call(task(8), "desktop_click", {"x": 1, "y": 1}, _noop, key="8:")
    assert "没同意" in out and len(no.asked) == 1                        # 一会儿之内不再问
    assert all(op[0] != "click" for op in desk.fake.ops)

    rule = approvals.add_rule("desktop", "allow")                       # “总是允许”过
    try:
        monkeypatch.setattr(approvals, "request", Recorder(False))
        out, _ = await ctools.call(task(9), "desktop_screenshot", {}, _noop, key="9:")
        assert "屏幕截图" in out
    finally:
        approvals.delete_rule(rule["id"])


async def test_coordinates_follow_the_screenshot(desk, monkeypatch):
    monkeypatch.setattr(approvals, "request", Recorder(True))
    await ctools.call(task(), "desktop_click", {"x": 640, "y": 360, "double": True}, _noop, key="7:")
    assert desk.fake.ops[-1] == ("click", (960.0, 540.0, "left", 2), {})   # 截图 1280 宽、屏幕 1920 点宽
    await ctools.call(task(), "desktop_drag", {"x": 0, "y": 0, "to_x": 5000, "to_y": 100}, _noop, key="7:")
    assert desk.fake.ops[-1] == ("drag", (0.0, 0.0, 1919, 150.0), {})       # 出了屏幕的夹回来
    await ctools.call(task(), "desktop_scroll", {"x": 10, "y": 10, "direction": "down", "amount": 4}, _noop, key="7:")
    assert desk.fake.ops[-1] == ("scroll", (15.0, 15.0), {"dy": -4, "dx": 0})
    out, _ = await ctools.call(task(), "desktop_key", {"keys": "cmd+foo"}, _noop, key="7:")
    assert "参数不对" in out or "认不出" in out


async def test_one_task_at_a_time(desk, monkeypatch):
    monkeypatch.setattr(approvals, "request", Recorder(True))
    await ctools.call(task(1), "desktop_screenshot", {}, _noop, key="1:")
    waits = []

    async def wait(text):
        waits.append(text)
    second = asyncio.create_task(ctools.call(task(2), "desktop_screenshot", {}, wait, key="2:"))
    await _sleep(0.05)
    assert not second.done() and any("排队等屏幕" in w for w in waits)
    out, _ = await ctools.call(task(3), "desktop_screenshot", {}, _noop, key="chat-1")
    assert "正被" in out                                                   # 对话里直接操作的不排队，直接告诉模型
    await desk.release("1:")
    out, _ = await asyncio.wait_for(second, 2)
    assert "屏幕截图" in out and desk.desk_owner == "2:"


async def test_your_input_pauses_the_bot(desk, monkeypatch):
    monkeypatch.setattr(approvals, "request", Recorder(True))
    monkeypatch.setattr(approvals, "last_done", 0.0)                       # 别的测试刚点过的卡片不算
    await ctools.call(task(), "desktop_screenshot", {}, _noop, key="7:")
    desk.desk_at = time.time() - 10
    desk.fake.idle_s = 2.0                                                  # Bot 10 秒前动的，你 2 秒前动了鼠标
    step = asyncio.create_task(ctools.call(task(), "desktop_click", {"x": 1, "y": 1}, _noop, key="7:"))
    await _sleep(0.05)
    assert desk.controller == "human" and desk.paused_by == "input" and not step.done()
    assert (await desk.desk_state())["paused_by"] == "input"
    desk.handback()
    out, _ = await asyncio.wait_for(step, 2)
    assert "desktop_screenshot" in out and not any(op[0] == "click" for op in desk.fake.ops)   # 交还后这一步不做
    desk.fake.idle_s = 0.0                                                  # 交还的那一下点击不算
    await ctools.call(task(), "desktop_click", {"x": 1, "y": 1}, _noop, key="7:")
    assert desk.fake.ops[-1][0] == "click"
    approvals.last_done = time.time() + 1                                  # 刚在 App 里点过确认卡片：也不算
    desk.desk_at = time.time() - 10
    await ctools.call(task(), "desktop_click", {"x": 1, "y": 1}, _noop, key="7:")
    assert desk.controller == "bot"


async def test_locked_screen_and_missing_permissions(desk, monkeypatch):
    monkeypatch.setattr(approvals, "request", Recorder(True))
    desk.fake.lock = True
    out, _ = await ctools.call(task(), "desktop_type", {"text": "hi"}, _noop, key="7:")
    assert "锁着" in out and not desk.fake.ops
    desk.fake.lock = False
    desk.fake.perms["accessibility"] = False
    out, _ = await ctools.call(task(), "desktop_type", {"text": "hi"}, _noop, key="7:")
    assert "辅助功能" in out and not desk.fake.ops
    assert "辅助功能" in (await desk.desk_state())["problem"]


async def test_send_buttons_and_enter_ask(desk, monkeypatch):
    user = Recorder(True)
    monkeypatch.setattr(approvals, "request", user)
    await ctools.call(task(), "desktop_click", {"x": 1, "y": 1}, _noop, key="7:")
    assert [t for t, _ in user.asked] == ["desktop"]                         # 普通按钮：不问
    user.answer = False
    desk.fake.el = {"role": "AXButton", "label": "发送", "pid": 50}
    out, _ = await ctools.call(task(), "desktop_click", {"x": 1, "y": 1}, _noop, key="7:")
    assert "用户拒绝" in out and user.asked[-1] == ("desktop_submit", {"操作": "在「备忘录」里点击「发送」"})
    assert sum(op[0] == "click" for op in desk.fake.ops) == 1
    out, _ = await ctools.call(task(), "desktop_key", {"keys": "enter"}, _noop, key="7:")
    assert "用户拒绝" in out and "输入框里按 enter" in user.asked[-1][1]["操作"]
    await ctools.call(task(), "desktop_key", {"keys": "cmd+a"}, _noop, key="7:")
    assert desk.fake.ops[-1] == ("press_seq", ("cmd+a",), {})
    out, _ = await ctools.call(task(), "desktop_key", {"keys": "1 2 * 3 enter"}, _noop, key="7:")
    assert "用户拒绝" in out                                               # 一串键里有回车：也先问


async def test_never_touches_its_own_window(desk, monkeypatch):
    monkeypatch.setattr(approvals, "request", Recorder(True))
    desk.fake.el = {"role": "AXButton", "label": "总是允许", "pid": os.getpid()}
    out, _ = await ctools.call(task(), "desktop_click", {"x": 1, "y": 1}, _noop, key="7:")
    assert "自己的窗口" in out and not desk.fake.ops
    monkeypatch.setattr(macdesk, "windows", lambda limit=12: [   # 右半边是 App 的窗口：拖过去也不行
        {"app": "备忘录", "pid": 50, "title": "笔记", "x": 0, "y": 0, "w": 960, "h": 1080},
        {"app": "Open Super Agent", "pid": os.getpid(), "title": "Open Super Agent", "x": 960, "y": 0, "w": 960, "h": 1080}])
    desk.fake.el = {"role": "AXCell", "label": "a.txt", "pid": 50}
    out, _ = await ctools.call(task(), "desktop_drag", {"x": 100, "y": 100, "to_x": 1000, "to_y": 100}, _noop, key="7:")
    assert "自己的窗口" in out and not desk.fake.ops
    desk.fake.front_app = {"app": "Google Chrome", "pid": 99, "title": "Open Super Agent"}   # 浏览器里开着的界面
    out, _ = await ctools.call(task(), "desktop_type", {"text": "批准"}, _noop, key="7:")
    assert "自己的窗口" in out and not desk.fake.ops


async def test_view_follows_last_action(desk, monkeypatch):
    shots = []

    async def chrome_peek(fmt):
        shots.append("chrome")
        return {"data": "", "title": "", "url": ""}
    desk._peek = chrome_peek
    await desk.peek()
    assert shots == ["chrome"]                                             # 还没用过屏幕：看 Bot 的 Chrome
    desk.recent_desk = True
    assert await desk.peek() == png() and shots == ["chrome"]
    await desk.peek(view="chrome")
    assert shots == ["chrome", "chrome"]


def test_parse_keys():
    assert macdesk.parse_keys("cmd+shift+4") == (["cmd", "shift"], "4")
    assert macdesk.parse_keys("Return") == ([], "return")
    assert macdesk.parse_keys("ctrl + A") == (["ctrl"], "a")
    assert macdesk.parse_keys("shift") == (["shift"], "")
    assert macdesk.parse_keys("*") == (["shift"], "8") and macdesk.parse_keys("+") == (["shift"], "=")
    assert macdesk.parse_keys("cmd+?") == (["cmd", "shift"], "/")
    for bad in ("cmd+nope", "a+b", ""):
        with pytest.raises(ValueError):
            macdesk.parse_keys(bad)


@pytest.mark.skipif(not (os.environ.get("OSA_TEST_DESKTOP") and sys.platform == "darwin"), reason="要 OSA_TEST_DESKTOP=1，会动真的鼠标键盘")
def test_real_desktop(tmp_path):
    """真的鼠标键盘：在文本编辑里点一下、粘贴中文、全选删除，剪贴板原样还原。要辅助功能、屏幕录制权限，屏幕不能锁着。"""
    assert all(macdesk.permissions().values()) and not macdesk.locked()
    f = tmp_path / "osa-desktop-test.txt"
    f.write_text("")
    pool = macdesk.L().objc.objc_autoreleasePoolPush()
    yours = macdesk._save(macdesk._board())   # 你剪贴板里的东西：测完放回去
    macdesk.L().objc.objc_autoreleasePoolPop(pool)
    try:
        _real(f)
    finally:
        if yours is not None:
            pool = macdesk.L().objc.objc_autoreleasePoolPush()
            macdesk._write(macdesk._board(), yours)
            macdesk.L().objc.objc_autoreleasePoolPop(pool)


def _real(f):
    import subprocess
    macdesk.set_clipboard_text("剪贴板原来的内容")
    front = {}
    subprocess.run(["open", "-a", "TextEdit", str(f)], check=True)
    for _ in range(40):
        front = macdesk.front()
        if front["app"] in ("TextEdit", "文本编辑") and f.name in front["title"]:
            break
        time.sleep(0.25)
    else:
        pytest.fail(f"文本编辑没到前面：{front}")
    win = next(w for w in macdesk.windows() if w["pid"] == front["pid"])
    macdesk.click(win["x"] + win["w"] / 2, win["y"] + win["h"] / 2)
    time.sleep(0.3)
    macdesk.type_text("你好，osa 123")
    time.sleep(0.3)
    el = macdesk._ax(macdesk.L().sys, "AXFocusedUIElement")
    assert macdesk._ax_text(el, "AXValue") == "你好，osa 123"
    assert macdesk.clipboard_text() == "剪贴板原来的内容"
    macdesk.press("cmd+a")
    macdesk.press("backspace")
    time.sleep(0.3)
    assert macdesk._ax_text(el, "AXValue") == ""
    assert macdesk.front()["pid"] == front["pid"]
    macdesk.press("cmd+q")


async def test_chat_consent_lasts_a_while(desk, monkeypatch):
    user = Recorder(True)
    monkeypatch.setattr(approvals, "request", user)
    chat = SimpleNamespace(id="对话·Nova", bot_id=1, chat_id=1, goal="在对话里直接操作电脑", background=False)
    await ctools.call(chat, "desktop_screenshot", {}, _noop, key="chat-1")
    await desk.desk_release("chat-1")                                      # 一轮说完让出屏幕（router._release_desk）
    assert not desk.desk_owner
    await ctools.call(chat, "desktop_screenshot", {}, _noop, key="chat-1")
    assert len(user.asked) == 1                                            # 下一轮不用再问
    desk.desk_ok["chat-1"] = time.time() - host.CHAT_OK - 1
    await ctools.call(chat, "desktop_screenshot", {}, _noop, key="chat-1")
    assert len(user.asked) == 2                                            # 过了一阵子再问
    await desk.desk_release("chat-1")
    for k in ("7:h0", "7:h1"):                                             # Heavy 的两个研究员：同一个任务只问一次
        await ctools.call(task(), "desktop_screenshot", {}, _noop, key=k)
        await desk.release(k)
    assert len(user.asked) == 3 and not desk.desk_owner
