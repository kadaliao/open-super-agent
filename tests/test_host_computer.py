"""本机电脑（host.py）：文件只在工作文件夹里、终端默认关、上传 / 写文件 / 提交 / 终端命令先问；auto 在桌面 App 里选它。
不开真的 Chrome（那个在文件末尾，要 OSA_TEST_CHROME=1 才跑）。"""
import asyncio
import os
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from osa import approvals, computer, library
from osa.config import CFG
from osa.computer import host, tools as ctools
from osa.computer.base import ComputerError


@pytest.fixture
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "DATA", tmp_path / "data")
    monkeypatch.setattr(host, "chrome_path", lambda: "/fake/chrome")
    c = host.HostComputer(0)

    async def running():
        return {"state": "running", "detail": ""}
    c.status = running
    return c


@pytest.fixture
def shell(monkeypatch):
    monkeypatch.setitem(CFG.setdefault("computer", {}), "host_shell", True)


async def test_files_stay_in_workspace(pc, tmp_path):
    await pc.write_bytes("notes/a.txt", b"hi")              # 相对路径从工作文件夹算起
    assert (pc.root / "notes" / "a.txt").read_bytes() == b"hi"
    assert await pc.read_bytes(f"{pc.workspace}/notes/a.txt") == b"hi"
    assert await pc.read_bytes("/data/workspace/notes/a.txt") == b"hi"   # 以前 Docker 电脑里的路径也认
    (tmp_path / "secret.txt").write_text("x")
    for bad in ("../secret.txt", str(tmp_path / "secret.txt"), "~/.ssh/id_rsa", "notes/../../secret.txt"):
        with pytest.raises(ComputerError, match="工作文件夹"):
            await pc.read_bytes(bad)
    with pytest.raises(ComputerError, match="工作文件夹"):
        await pc.write_bytes("../escape.txt", b"x")
    os.symlink(tmp_path, pc.root / "out")                  # 经链接跑出去也不行
    with pytest.raises(ComputerError, match="工作文件夹"):
        await pc.read_bytes("out/secret.txt")
    with pytest.raises(ComputerError, match="工作文件夹"):
        pc.upload_paths(["out/secret.txt"])
    assert pc.upload_paths(["notes/a.txt"]) == [str(pc.root / "notes" / "a.txt")]
    with pytest.raises(ComputerError, match="不存在"):
        pc.upload_paths(["notes/missing.txt"])


async def test_library_on_host(pc, tmp_path, monkeypatch):
    monkeypatch.setattr(library, "root", lambda: pc.workspace)
    r = await library.put_file(pc, "uploads", "a.txt", b"1")
    r2 = await library.put_file(pc, "uploads", "a.txt", b"2")
    assert (r["name"], r2["name"]) == ("a.txt", "a (2).txt")
    entries = await library.list_dir(pc, "uploads")
    assert [e["name"] for e in entries] == ["a (2).txt", "a.txt"] and entries[1]["size"] == 1
    assert [e["name"] for e in await library.list_dir(pc, "")] == ["uploads"]
    assert (await library.list_dir(pc, ""))[0]["dir"]
    with pytest.raises(ValueError, match="目录不存在"):
        await library.list_dir(pc, "nope")
    with pytest.raises(ValueError):
        library.norm("/etc/passwd")
    stats = await library.stat_many(pc, [r["path"], pc.workspace + "/gone.txt"])
    assert list(stats) == [r["path"]] and stats[r["path"]]["size"] == 1

    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "f").write_text("x")
    os.symlink(outside, pc.root / "link")
    await library.remove(pc, pc.workspace + "/link")      # 删链接只删链接本身
    assert (outside / "f").exists() and not (pc.root / "link").exists()
    await library.remove(pc, r["path"])
    assert not (pc.root / "uploads" / "a.txt").exists()
    with pytest.raises(ValueError, match="整个工作目录"):
        await library.remove(pc, pc.workspace)


async def test_shell_is_off_by_default(pc):
    with pytest.raises(ComputerError, match="终端没有打开"):
        await pc.shell("echo hi")


async def test_shell_runs_in_workspace_without_secrets(pc, shell, monkeypatch):
    monkeypatch.setenv("OSA_PASSWORD", "pw")
    monkeypatch.setenv("FOO_API_KEY", "k")
    monkeypatch.setenv("PLAIN_VAR", "ok")
    r = await pc.shell("pwd; env")
    assert r.exit_code == 0
    assert r.stdout.splitlines()[0] == pc.workspace
    assert "PLAIN_VAR=ok" in r.stdout and "OSA_PASSWORD" not in r.stdout and "FOO_API_KEY" not in r.stdout
    r = await pc.shell("sleep 5", timeout=1)
    assert r.timed_out


def test_auto_backend(monkeypatch):
    monkeypatch.setitem(CFG.setdefault("computer", {}), "backend", "auto")
    monkeypatch.delenv("INSTABOX_API_KEY", raising=False)
    monkeypatch.delenv("OSA_DESKTOP", raising=False)
    assert computer.backend_name() == "local"
    monkeypatch.setenv("INSTABOX_API_KEY", "x")
    assert computer.backend_name() == "instabox"
    monkeypatch.setenv("OSA_DESKTOP", "1")          # 桌面 App 里默认用本机的 Chrome
    assert computer.backend_name() == "host"
    monkeypatch.setitem(CFG["computer"], "backend", "local")
    assert computer.backend_name() == "local"


async def test_chrome_without_permission(tmp_path, monkeypatch):
    """Chrome 在外接硬盘上、App 没权限看：算找到了，状态里说清楚去哪给权限，而不是“没找到 Chrome”。"""
    chrome = "/Volumes/Ext/Google Chrome.app/Contents/MacOS/Google Chrome"
    monkeypatch.setitem(CFG["computer"], "enabled", True)     # 测试配置里电脑是关的，关着的电脑 start() 直接拒绝
    monkeypatch.setattr(host, "DATA", tmp_path / "data")
    monkeypatch.setitem(CFG.setdefault("computer", {}), "chrome_path", chrome)
    real = os.stat

    def stat(p, *a, **k):
        if str(p) == chrome:
            raise PermissionError(1, "Operation not permitted")
        return real(p, *a, **k)
    monkeypatch.setattr(host.os, "stat", stat)
    assert host.chrome_path() == chrome
    assert computer.available()[0] == {"id": "host", "ok": True}
    c = host.HostComputer(0)
    st = await c.status()
    assert st["state"] == "error" and "可移除的宗卷" in st["detail"]
    with pytest.raises(ComputerError, match="可移除的宗卷"):
        await c.start()
    monkeypatch.setitem(CFG["computer"], "chrome_path", str(tmp_path / "nope"))   # 真没有
    assert host.chrome_path() is None


def test_default_rules():
    assert approvals.decide("browser_upload", "host")[0]
    assert approvals.decide("browser_submit", "host")[0]
    assert approvals.decide("computer_write_file", "host")[0]
    assert approvals.decide("computer_shell", "host")[0]
    for t in ("browser_upload", "browser_submit", "computer_write_file", "computer_shell"):
        assert not approvals.decide(t, "local")[0]      # Docker / Instabox 的电脑默认不问
    rule = approvals.add_rule("browser_submit", "allow")
    sh = approvals.add_rule("computer_shell", "allow")
    try:
        assert not approvals.decide("browser_submit", "host")[0]
        assert approvals.decide("computer_shell", "host")[0]   # 本机终端不能自动允许
    finally:
        approvals.delete_rule(rule["id"])
        approvals.delete_rule(sh["id"])


class Recorder:
    """冒充用户：记下弹出的确认卡片，按 answer 回答。"""

    def __init__(self, answer: bool):
        self.answer, self.asked = answer, []

    async def __call__(self, task, tool, args, kind="approval", once=False):
        self.asked.append((tool, args, once))
        return self.answer, ""


@pytest.fixture
def fake(pc, monkeypatch):
    """本机电脑，浏览器控制脚本收到的动作记在 ops 里；inspect 返回 info。"""
    pc.ops, pc.info = [], {"submit": False, "what": ""}

    pc.payloads = []

    async def helper(payload):
        pc.ops.append(payload["op"])
        pc.payloads.append(payload)
        out = dict(pc.info) if payload["op"] == "inspect" else {"title": "", "url": "", "elements": [], "text": "", "scroll": 0}
        return out | {"window": 1, "browser": "b"}

    async def start():
        pass
    pc._helper_run, pc.start = helper, start
    monkeypatch.setattr(ctools, "get", lambda: pc)
    return pc


TASK = SimpleNamespace(id=7, bot_id=1, chat_id=1, goal="g", background=False)


async def _noop(_):
    pass


async def test_click_asks_only_for_submit(fake, monkeypatch):
    user = Recorder(False)
    monkeypatch.setattr(approvals, "request", user)
    await ctools.call(TASK, "browser_click", {"ref": 3}, _noop, key="7:")
    assert fake.ops[-2:] == ["inspect", "click"] and not user.asked     # 普通按钮：看一眼就点

    fake.info = {"submit": True, "what": "点击「发布」", "url": "https://x.example/", "title": "X"}
    out, _ = await ctools.call(TASK, "browser_click", {"ref": 4}, _noop, key="7:")
    assert "用户拒绝" in out and fake.ops[-1] == "inspect"           # 被拒绝：没点
    assert user.asked[-1][0] == "browser_submit" and user.asked[-1][1]["操作"] == "点击「发布」"

    user.answer = True
    await ctools.call(TASK, "browser_type", {"ref": 5, "text": "hi", "submit": True}, _noop, key="7:")
    assert fake.ops[-2:] == ["inspect", "type"]
    await ctools.call(TASK, "browser_key", {"key": "Tab"}, _noop, key="7:")
    assert fake.ops[-1] == "key" and fake.ops[-2] != "inspect"           # 不是回车，不用看
    assert fake.payloads[-1]["name"] == "Tab"                            # 按键名不和“谁在操作”的 key 撞名


async def test_upload_and_write_ask(fake, monkeypatch):
    user = Recorder(False)
    monkeypatch.setattr(approvals, "request", user)
    await fake.write_bytes("a.pdf", b"%PDF")
    out, _ = await ctools.call(TASK, "browser_upload", {"ref": 2, "paths": "a.pdf"}, _noop, key="7:")
    assert "用户拒绝" in out and "upload" not in fake.ops and user.asked[-1][0] == "browser_upload"
    out, _ = await ctools.call(TASK, "browser_upload", {"ref": 2, "paths": ["/etc/hosts"]}, _noop, key="7:")
    assert "工作文件夹" in out and len(user.asked) == 1                     # 工作文件夹外的文件：不问，直接不行
    out, _ = await ctools.call(TASK, "computer_write_file", {"path": "b.txt", "content": "x"}, _noop, key="7:")
    assert "用户拒绝" in out and not (fake.root / "b.txt").exists()
    user.answer = True
    await ctools.call(TASK, "browser_upload", {"ref": 2, "paths": ["a.pdf"]}, _noop, key="7:")
    assert fake.ops[-2:] == ["upload", "snapshot"]


async def test_shell_asks_every_time(fake, shell, monkeypatch):
    user = Recorder(True)
    monkeypatch.setattr(approvals, "request", user)
    out, _ = await ctools.call(TASK, "computer_shell", {"cmd": "echo hi"}, _noop, key="7:")
    assert "hi" in out and user.asked[-1] == ("computer_shell", {"命令": "echo hi"}, False)   # 能按命令前缀总是允许


async def test_docker_computer_does_not_inspect(fake, monkeypatch):
    fake.backend = "local"
    user = Recorder(False)
    monkeypatch.setattr(approvals, "request", user)
    fake.info = {"submit": True, "what": "点击「发布」"}
    await ctools.call(TASK, "browser_click", {"ref": 4}, _noop, key="7:")
    assert fake.ops[-1] == "click" and "inspect" not in fake.ops and not user.asked


def test_host_tools_and_prompt(fake, monkeypatch):
    names = {s["function"]["name"] for s in ctools.schemas(fake)}
    assert "computer_shell" not in names and "browser_upload" in names
    hint = ctools.system_hint(fake)
    assert fake.workspace in hint and "Linux" not in hint and "没有终端" in hint
    monkeypatch.setitem(CFG.setdefault("computer", {}), "host_shell", True)
    assert "computer_shell" in {s["function"]["name"] for s in ctools.schemas(fake)}
    assert "每条命令都要用户确认" in ctools.system_hint(fake)


def test_once_card_cannot_be_always_allowed():
    loop = asyncio.new_event_loop()
    try:
        async def go():
            task = SimpleNamespace(id=8, bot_id=1, chat_id=1, goal="g", background=False)
            req = asyncio.create_task(approvals.request(task, "computer_shell", {"命令": "ls"}, once=True))
            await asyncio.sleep(0)
            aid = next(a for a, i in approvals.pending.items() if i["task_id"] == 8)
            approvals.resolve(aid, True, "", always=True)
            assert (await req)[0]
        loop.run_until_complete(go())
    finally:
        loop.close()
    assert not [r for r in approvals.rules() if r["pattern"] == "computer_shell"]


# ---------- 真的 Chrome：OSA_TEST_CHROME=1 uv run pytest tests/test_host_computer.py -k chrome ----------
PAGE = """<!doctype html><meta charset=utf-8><title>测试页</title>
<form method=post action=/post><input name=msg placeholder=消息><button>发送</button></form>
<form method=get action=/search><input name=q placeholder=关键词><button>搜索</button></form>
<textarea placeholder=评论></textarea>
<button id=plain onclick="document.title='点过了'">展开更多</button>
<a href=/file.txt download>下载</a>"""


@pytest.mark.skipif(not os.environ.get("OSA_TEST_CHROME"), reason="要真的 Chrome：OSA_TEST_CHROME=1")
async def test_real_chrome(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(PAGE)
    (site / "file.txt").write_text("downloaded")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    monkeypatch.setattr(host, "DATA", tmp_path / "data")
    c = host.HostComputer(0)
    try:
        assert (await c.status())["state"] == "stopped"
        await c.start()
        assert (await c.status())["state"] == "running"
        port = c._port()
        assert port and port != 9222
        await c.claim("t1", "测试")
        page = await c.browser("open", key="t1", url=url)
        assert page["title"] == "测试页"
        ref = lambda word: int(next(e for e in page["elements"] if word in e).split("]")[0][1:])   # noqa: E731

        async def submit(word, mode="click"):
            return (await c.browser("inspect", key="t1", ref=ref(word), mode=mode))["submit"]
        assert await submit("发送") and await submit("消息", "enter") and await submit("评论", "enter")
        assert not await submit("搜索") and not await submit("关键词", "enter") and not await submit("展开更多")
        assert not await submit("下载")
        shot = await c.page_shot("t1")
        w = int.from_bytes(shot[16:20], "big")
        assert shot[:4] == b"\x89PNG" and 600 < w <= 1400   # 页面像素，高分屏上也不是两倍大
        assert (await c.peek())[:2] == b"\xff\xd8"          # 给界面的是 jpeg
        await c.browser("click", key="t1", ref=ref("下载"))
        for _ in range(40):
            if (c.root / "downloads" / "file.txt").exists():
                break
            await asyncio.sleep(0.25)
        assert (c.root / "downloads" / "file.txt").read_text() == "downloaded"   # 下载存进工作文件夹
    finally:
        await c.pause()
        srv.shutdown()
        for _ in range(20):
            if (await c.status())["state"] != "running":
                break
            await asyncio.sleep(0.25)
        shutil.rmtree(tmp_path / "data", ignore_errors=True)
