"""插件（osa/plugins.py）：列出时不执行代码、启用记指纹、改了文件要重新启用、出错整个撤销、卸载撤销全部注册、
服务器锁下关掉、插件市场的两个插件、Router / Worker 用插件的工具、事件。插件目录换成临时目录，碰不到 data/plugins。"""
import asyncio
import importlib.util
import sys
from datetime import date
import httpx
import pytest
from conftest import find, until
from fakellm import Call
from osa import bots, memory, plugins, registry, server
from osa.config import CFG


def http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "DIR", tmp_path / "plugins")
    plugins.DIR.mkdir()
    yield plugins.DIR
    for n in list(plugins._loaded):
        plugins.unload(n)
    plugins._errors.clear()
    memory.conn.execute("delete from plugins")
    memory.conn.commit()


@pytest.fixture
def bot(make_bot):
    made = []

    def make(name: str = "") -> dict:
        made.append(make_bot(name))
        return made[-1]
    yield make
    for b in made:
        bots.delete(b["id"])


ECHO = '''"""回声插件。

第二段不算说明。"""
from osa.registry import ROUTER, WORKER, Tool
META = {"title": "回声", "version": "0.1", "config": {"prefix": ">"}}
open(__file__ + ".ran", "w").write("x")   # 加载才会执行：列出时不该出现这个文件


def apply(ctx):
    async def run(call):
        return ctx.config["prefix"] + call.args.get("text", "")
    ctx.tool(Tool("echo_plugin", {"type": "function", "function": {"name": "echo_plugin", "description": "回声",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}, run, where=frozenset({ROUTER, WORKER})))
    seen = ctx.data_dir / "seen.txt"
    ctx.data_dir.mkdir(exist_ok=True)
    ctx.on("user_message", lambda chat, text, via: seen.open("a").write(text + "\\n"))

    async def forever():
        await __import__("asyncio").sleep(3600)
    ctx.background = ctx.task(forever())
'''


def test_listing_reads_without_executing(pdir):
    (pdir / "echo.py").write_text(ECHO)
    (pdir / "broken.py").write_text("def apply(:\n")
    (pdir / "noapply.py").write_text("X = 1\n")
    (pdir / "Bad-Name.py").write_text("")
    got = {p["name"]: p for p in plugins.listing()["plugins"]}
    assert set(got) == {"echo", "broken", "noapply"}
    assert got["echo"]["title"] == "回声" and got["echo"]["version"] == "0.1" and got["echo"]["description"] == "回声插件。"
    assert got["echo"]["config"] == {"prefix": ">"} and not got["echo"]["enabled"] and not got["echo"]["error"]
    assert got["broken"]["error"] and "apply" in got["noapply"]["error"]
    assert not (pdir / "echo.py.ran").exists()


async def test_enable_registers_disable_disposes(pdir):
    (pdir / "echo.py").write_text(ECHO)
    await plugins.enable("echo")
    t = registry.get("echo_plugin")
    assert t.owner == "plugin:echo" and registry.available("echo_plugin", registry.ROUTER)
    ctx = plugins._loaded["echo"]
    assert "osa_plugin_echo" in sys.modules and (pdir / "echo.py.ran").exists()
    assert await t.run(registry.Call(registry.ROUTER, {"id": 1}, 0, 0, {"text": "hi"})) == ">hi"
    assert plugins.info("echo")["tools"] == ["echo_plugin"] and plugins.info("echo")["loaded"]
    await plugins.set_config("echo", {"prefix": "#"})     # 改设置：重新加载
    assert await registry.get("echo_plugin").run(registry.Call(registry.ROUTER, {"id": 1}, 0, 0, {"text": "hi"})) == "#hi"
    bg = plugins._loaded["echo"].background
    await plugins.disable("echo")
    await asyncio.sleep(0)
    assert registry.get("echo_plugin") is None and "osa_plugin_echo" not in sys.modules
    assert bg.cancelled() and ctx not in plugins._loaded.values()
    assert not any(f.__qualname__.startswith("apply") for f in registry._LISTENERS.get("user_message", []))
    with pytest.raises(PermissionError):
        await plugins.load("echo")                        # 停用了就不加载


async def test_changed_file_needs_reenable(pdir):
    (pdir / "echo.py").write_text(ECHO)
    await plugins.enable("echo")
    (pdir / "echo.py").write_text(ECHO.replace('"回声"', '"改过的"'))
    assert plugins.info("echo")["changed"]
    with pytest.raises(PermissionError, match="改过"):
        await plugins.reload("echo")
    assert registry.get("echo_plugin").owner == "plugin:echo"   # 新文件不加载；正在跑的还是启用时看过的那份
    plugins.unload("echo")
    await plugins.start()                                   # 重启服务也不加载
    assert "echo" not in plugins._loaded and "改过" in plugins.info("echo")["error"]
    await plugins.enable("echo")                            # 用户看过、再启用一次
    assert plugins.info("echo")["loaded"] and not plugins.info("echo")["changed"]


async def test_folder_plugin_and_helper_change(pdir):
    d = pdir / "pkg"
    d.mkdir()
    (d / "helper.py").write_text("def word():\n    return 'from helper'\n")
    (d / "__init__.py").write_text('''from osa.registry import ROUTER, Tool
from .helper import word


def apply(ctx):
    async def run(call):
        return word()
    ctx.tool(Tool("pkg_tool", {"type": "function", "function": {"name": "pkg_tool", "description": "x",
        "parameters": {"type": "object", "properties": {}}}}, run, where=frozenset({ROUTER})))
''')
    await plugins.enable("pkg")
    assert await registry.get("pkg_tool").run(registry.Call(registry.ROUTER, {"id": 1}, 0, 0, {})) == "from helper"
    assert plugins.info("pkg")["kind"] == "folder" and "helper.py" in plugins.read_source("pkg")
    (d / "helper.py").write_text("def word():\n    return 'evil'\n")   # 只改了里面的另一个文件也算改过
    assert plugins.info("pkg")["changed"]
    await plugins.remove("pkg")
    assert not d.exists() and registry.get("pkg_tool") is None and "osa_plugin_pkg.helper" not in sys.modules


async def test_apply_error_rolls_back(pdir):
    (pdir / "bad.py").write_text('''from osa.registry import WORKER, Tool
def apply(ctx):
    ctx.tool(Tool("half_done", {"type": "function", "function": {"name": "half_done", "description": "x",
        "parameters": {"type": "object", "properties": {}}}}, None, where=frozenset({WORKER})))
    raise RuntimeError("boom")
''')
    with pytest.raises(RuntimeError, match="boom"):
        await plugins.enable("bad")
    assert registry.get("half_done") is None and "osa_plugin_bad" not in sys.modules
    assert "boom" in plugins.info("bad")["error"] and plugins.info("bad")["enabled"]
    # 不能盖掉别人的工具（盖了再卸载，内置的 save_page 就没了）
    (pdir / "thief.py").write_text('''from osa.registry import ROUTER, Tool
def apply(ctx):
    ctx.tool(Tool("save_page", {"type": "function", "function": {"name": "save_page"}}, None, where=frozenset({ROUTER})))
''')
    with pytest.raises(RuntimeError, match="已经由 builtin 注册"):
        await plugins.enable("thief")
    assert registry.get("save_page").owner == "builtin"


async def test_locked_server_has_no_plugins(pdir, monkeypatch):
    (pdir / "echo.py").write_text(ECHO)
    await plugins.enable("echo")
    plugins.unload("echo")
    monkeypatch.setitem(CFG, "security", {"local_exec": False})
    assert plugins.listing()["locked"]
    with pytest.raises(PermissionError):
        await plugins.enable("echo")
    with pytest.raises(PermissionError):
        await plugins.install("date_calc")
    await plugins.start()
    assert "echo" not in plugins._loaded and registry.get("echo_plugin") is None
    monkeypatch.setitem(CFG, "security", {"demo": True})
    assert plugins.locked()


def _catalog_module(name):
    spec = importlib.util.spec_from_file_location(f"cat_{name}", plugins.CATALOG / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_date_calc_logic():
    dc = _catalog_module("date_calc")
    today = date(2026, 11, 9)
    assert "相差 59 天" in dc.calc({"op": "diff", "from": "2026-01-01", "to": "2026-03-01"}, today)
    assert "工作日（周一到周五，不算节假日）5 个" in dc.calc({"op": "diff", "from": "2026-11-09", "to": "2026-11-16"}, today)
    assert "2024-02-29（星期四）" in dc.calc({"op": "add", "from": "2024-01-31", "months": 1}, today)
    assert "2026-11-02（星期一）" in dc.calc({"op": "add", "weeks": -1}, today)
    assert "2026-11-09（星期一）" in dc.calc({"op": "weekday"}, today) and "第 46 周" in dc.calc({"op": "weekday"}, today)
    with pytest.raises(ValueError):
        dc.calc({"op": "diff", "from": "2026-01-01"}, today)


async def test_router_uses_catalog_plugin(pdir, llm, events, bot):
    await plugins.install("date_calc")
    assert (pdir / "date_calc.py").exists() and not plugins.info("date_calc")["enabled"]   # 装了还没启用
    await plugins.enable("date_calc")
    b = bot("Calendar")
    llm.script("router:Calendar", Call("date_calc", {"op": "diff", "from": "2026-01-01", "to": "2026-03-01"}), "59 天")
    await server.safe_handle(bots.chat(b["chat_id"]), "1 月 1 日到 3 月 1 日几天")
    await until(lambda: find(b["chat_id"], role="assistant", text="59 天"), what="回复")
    body = llm.bodies("router", "Calendar")[-1]
    assert any(t["function"]["name"] == "date_calc" for t in body["tools"])
    assert any(m["role"] == "tool" and "相差 59 天" in m["content"] for m in body["messages"])
    assert "date_calc：日期计算" in body["messages"][0]["content"]          # 能力说明里也有
    # 市场里的新版本：装上会停掉旧的，要重新启用
    (pdir / "date_calc.py").write_text((pdir / "date_calc.py").read_text() + "\n# 本地改过\n")
    assert plugins.info("date_calc")["update"]
    await plugins.install("date_calc")
    assert not plugins.info("date_calc")["enabled"] and registry.get("date_calc") is None


async def test_task_webhook_on_task_done(pdir, llm, events, bot, monkeypatch):
    sent = []

    def handle(req: httpx.Request) -> httpx.Response:
        sent.append((str(req.url), req.read().decode()))
        return httpx.Response(200)
    await plugins.install("task_webhook")
    with pytest.raises(RuntimeError, match="先在设置里填 url"):
        await plugins.enable("task_webhook")
    from osa import vault
    vault.put("TEST_HOOK", "https://open.feishu.example/hook/abc")
    await plugins.set_config("task_webhook", {"url": "vault:TEST_HOOK", "kind": "feishu", "only_failed": False})
    assert plugins.info("task_webhook")["loaded"] and not plugins.info("task_webhook")["error"]
    # 只换掉插件模块里的 httpx（全局换会弄坏模型客户端）
    from types import SimpleNamespace
    monkeypatch.setattr(sys.modules["osa_plugin_task_webhook"], "httpx", SimpleNamespace(
        AsyncClient=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handle), **kw), HTTPError=httpx.HTTPError))

    b = bot("Hooky")
    llm.script("router:Hooky", Call("dispatch", {"goal": "整理一下周报"}), "好")
    llm.script("worker", "周报整理好了")
    await server.safe_handle(bots.chat(b["chat_id"]), "整理周报")
    await until(lambda: sent, what="webhook")
    url, payload = sent[0]
    assert url == "https://open.feishu.example/hook/abc"
    assert '"msg_type":"text"' in payload.replace(" ", "") and "Hooky 的任务" in payload and "周报整理好了" in payload
    vault.delete("TEST_HOOK")


async def test_user_message_event(pdir, llm, events, bot):
    (pdir / "echo.py").write_text(ECHO)
    await plugins.enable("echo")
    b = bot("Listener")
    llm.script("router:Listener", "嗯")
    await server.safe_handle(bots.chat(b["chat_id"]), "插件你听得到吗")
    seen = pdir / ".echo.data" / "seen.txt"
    await until(lambda: seen.exists() and "插件你听得到吗" in seen.read_text(), what="插件收到事件")
    await plugins.remove("echo")
    assert not seen.exists()                                # 删插件时它的数据目录一起删


async def test_rest(pdir, no_password):
    (pdir / "echo.py").write_text(ECHO)
    async with http() as c:
        d = (await c.get("/api/plugins")).json()
        assert not d["locked"] and [p["name"] for p in d["plugins"]] == ["echo"]
        assert {"date_calc", "task_webhook"} <= {p["name"] for p in d["catalog"]}
        assert "open(__file__" in (await c.get("/api/plugins/echo/source")).json()["source"]
        assert "def calc" in (await c.get("/api/plugins/date_calc/source", params={"catalog": 1})).json()["source"]
        assert (await c.get("/api/plugins/nope/source")).status_code == 404
        assert (await c.get("/api/plugins/a.b/source")).status_code == 404
        assert (await c.post("/api/plugins/Bad/install")).status_code == 404
        r = await c.post("/api/plugins/echo/enable")
        assert r.status_code == 200 and r.json()["loaded"]
        assert (await c.put("/api/plugins/echo/config", json={"config": "x"})).status_code == 400
        assert (await c.post("/api/plugins/echo/explode")).status_code == 404
        assert (await c.post("/api/plugins/date_calc/install")).status_code == 200
        assert (await c.post("/api/plugins/evil/install")).status_code == 404
        assert (await c.post("/api/plugins/echo/disable")).json()["loaded"] is False
        assert (await c.delete("/api/plugins/echo")).status_code == 200
        assert [p["name"] for p in (await c.get("/api/plugins")).json()["plugins"]] == ["date_calc"]
