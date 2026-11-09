"""工具注册表（osa/registry.py）：注册和撤销、按场合和环境挑工具、事件；Router 和 Worker 都从它找工具；应用断开时工具一起撤销。"""
import asyncio
import pytest
from conftest import find, until
from fakellm import Call
from osa import bots, mcp_hub, registry, router, server
from osa.registry import APPLET, ROUTER, WORKER, Context, Tool


async def _echo(text: str = "") -> str:
    return f"echo:{text}"


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}


def test_register_and_dispose():
    ctx = Context("p1")
    ctx.simple("t1", _schema("t1"), _echo, where=frozenset({WORKER}))
    ctx.tool(Tool("t2", _schema("t2"), lambda c: _echo(), where=frozenset({ROUTER}), when=lambda env: env.get("pages")))
    seen = []
    ctx.on("ping", lambda **kw: seen.append(kw))
    assert registry.get("t1").owner == "p1" and registry.available("t1", WORKER) and not registry.available("t1", ROUTER)
    assert not registry.available("t2", ROUTER) and registry.available("t2", ROUTER, {"pages": True})
    names = lambda scope, env=None: [s["function"]["name"] for s in registry.schemas(scope, env)]
    assert "t1" in names(WORKER) and "t1" not in names(ROUTER)
    assert "t2" in names(ROUTER, {"pages": True}) and "t2" not in names(ROUTER)
    # 别的 Context 不能覆盖同名工具（覆盖后再撤销，原来那个就没了）
    ctx2 = Context("p2")
    with pytest.raises(ValueError, match="已经由 p1 注册"):
        ctx2.simple("t1", _schema("t1"), _echo)
    assert registry.get("t1").owner == "p1"
    ctx.dispose()
    assert registry.get("t1") is None and registry.get("t2") is None
    ctx2.simple("t1", _schema("t1"), _echo)   # 撤销之后名字空出来了
    assert registry.get("t1").owner == "p2"
    ctx2.dispose()
    assert registry._LISTENERS.get("ping") == []


async def test_fire_and_task():
    ctx = Context("bg")
    got = []
    ctx.on("y", lambda **kw: got.append(kw))
    registry.fire("y", n=1)
    registry.fire("nobody_listens", n=2)
    await asyncio.sleep(0.01)
    assert got == [{"n": 1}]

    async def forever():
        await asyncio.sleep(3600)
    t = ctx.task(forever())
    ctx.dispose()
    await asyncio.sleep(0)
    assert t.cancelled() or t.cancelling()


async def test_emit_runs_sync_and_async_listeners():
    ctx = Context("ev")
    got = []

    async def a(**kw):
        got.append(("a", kw))

    def b(**kw):
        got.append(("b", kw))
        raise RuntimeError("坏掉的监听器不影响别人")
    ctx.on("x", b)
    ctx.on("x", a)
    await registry.emit("x", n=1)
    assert got == [("b", {"n": 1}), ("a", {"n": 1})]
    ctx.dispose()
    await registry.emit("x", n=2)
    assert len(got) == 2


def test_builtin_tools_by_scope():
    r = {s["function"]["name"] for s in registry.schemas(ROUTER, router.ROUTER_ENV)}
    w = {s["function"]["name"] for s in registry.schemas(WORKER, {"scope": WORKER, "pages": True, "use_pc": True})}
    draft = {s["function"]["name"] for s in registry.schemas(WORKER, {"scope": WORKER, "pages": False, "use_pc": True})}
    assert {"save_page", "read_page", "page_data", "send_file", "generate_image", "search_memory"} <= r
    assert "web_search" not in r                                   # 网页工具只给 Worker
    assert {"web_search", "fetch_url", "save_page", "send_file", "search_memory"} <= w and "generate_image" not in w
    assert "save_page" not in draft and "send_file" not in draft   # Heavy 的草稿不存页面、不发文件
    assert registry.available("web_search", APPLET) and not registry.available("save_page", APPLET)


async def test_worker_runs_registry_tool_and_mcp_dispose(llm, events, make_bot, mcp_tool):
    """Worker 从注册表找到应用的工具；应用断开（dispose）后同一个工具就没了。"""
    calls = []

    async def note(text: str = "") -> str:
        calls.append(text)
        return f"记下了：{text}"
    mcp_tool("demo__note", note)
    a = make_bot("Reg")
    llm.script("router:Reg", Call("dispatch", {"goal": "记一下：买牛奶"}), "好")
    llm.script("worker", Call("demo__note", {"text": "买牛奶"}), "记好了")
    await server.safe_handle(bots.chat(a["chat_id"]), "记一下：买牛奶")
    await until(lambda: find(a["chat_id"], kind="result"), what="任务结果")
    assert calls == ["买牛奶"]
    body = llm.bodies("worker")[-1]
    assert any(t["function"]["name"] == "demo__note" for t in body["tools"])
    assert mcp_hub.tools_of("demo") == ["note"]
    assert registry.get("demo__note").owner == "mcp:test"
    # 断开：撤销后 Worker 再也找不到它
    ctx = Context("mcp:demo2")
    ctx.simple("demo2__x", _schema("demo2__x"), note, where=frozenset({WORKER}), group="demo2")
    mcp_hub._ctx["demo2"] = ctx
    assert mcp_hub.tools_of("demo2") == ["x"]
    mcp_hub._drop_tools("demo2")
    assert registry.get("demo2__x") is None and mcp_hub.tools_of("demo2") == []
    bots.delete(a["id"])


async def test_router_runs_registry_tool(llm, events, make_bot):
    """Router 自己的工具链处理不了的名字，交给注册表（这里是 search_memory）。"""
    a = make_bot("Seeker")
    llm.script("router:Seeker", Call("search_memory", {"query": "上次的报告"}), "没找到")
    await server.safe_handle(bots.chat(a["chat_id"]), "上次那个报告呢")
    await until(lambda: find(a["chat_id"], role="assistant", text="没找到"), what="回复")
    tool = [m for m in llm.bodies("router", "Seeker")[-1]["messages"] if m["role"] == "tool"][0]
    assert "没找到相关的记忆或页面" in tool["content"]
    bots.delete(a["id"])


def test_unknown_tool_for_scope():
    assert registry.available("generate_image", WORKER) is None
    assert registry.available("nope", ROUTER) is None
    assert pytest  # noqa


def test_gated_tool_cannot_go_to_router():
    """gated 是“跑之前问用户”，Router 不能停下来等确认：两个一起写，注册时就报错，不让它悄悄不问。"""
    ctx = Context("plugin:gate")
    with pytest.raises(ValueError, match="不能给 ROUTER"):
        ctx.simple("gate_both", _schema("gate_both"), _echo, gated=True)            # where 默认含 ROUTER
    with pytest.raises(ValueError, match="不能给 ROUTER"):
        ctx.tool(Tool("gate_router", _schema("gate_router"), lambda c: _echo(), where=frozenset({ROUTER}), gated=True))
    assert registry.get("gate_both") is None and registry.get("gate_router") is None
    ctx.simple("gate_worker", _schema("gate_worker"), _echo, where=frozenset({WORKER, APPLET}), gated=True)
    assert registry.get("gate_worker").gated
    ctx.dispose()
    assert registry.get("gate_worker") is None


def test_plugin_source_clashing_with_builtin():
    """以前市场里的来源插件（epic_free 这几个）现在内置了：旧插件再登记同名来源时说清楚怎么办，内置的不受影响。"""
    old = registry.SOURCES["epic_free"]
    ctx = Context("plugin:epic_free")

    async def check(params, state):
        return [], state
    with pytest.raises(ValueError, match="已经内置了"):
        ctx.source(registry.Source(id="epic_free", title="Epic", description="", mode=registry.POLL, check=check))
    ctx.dispose()
    assert registry.SOURCES["epic_free"] is old and old.owner == "builtin"
