"""小应用（osa/applets.py）：清单、数据、从界面调工具（含确认卡片）、替用户发消息、SDK 注入。"""
import asyncio
import httpx
import pytest
from conftest import find, until
from osa import applets, approvals, bots, library, memory, registry, server


def http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


@pytest.fixture
def bot(make_bot):
    """用完就删：测试库里 Bot 有 50 个的上限，大家共用。"""
    made = []

    def make(name: str = "") -> dict:
        made.append(make_bot(name))
        return made[-1]
    yield make
    for b in made:
        bots.delete(b["id"])


@pytest.fixture
def echo():
    """一个假的工具 echo：原样返回参数，Worker 和小应用都能用。"""
    async def fn(text: str = "") -> str:
        return f"echo:{text}"
    ctx = registry.Context("test")
    ctx.simple("echo", {"type": "function", "function": {"name": "echo", "description": "回声",
                                                          "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}},
               fn, where=frozenset({registry.WORKER, registry.APPLET}), gated=True)
    memory.conn.execute("delete from rules where pattern='echo'")
    memory.conn.commit()
    yield fn
    ctx.dispose()
    memory.conn.execute("delete from rules where pattern='echo'")
    memory.conn.commit()


def test_manifest():
    assert applets.manifest(None) is None and applets.manifest({}) is None
    assert applets.manifest({"tools": ["a", " a ", "b", ""], "chat": 1}) == {"tools": ["a", "b"], "chat": True}
    assert applets.manifest({"tools": ["x"] * 30})["tools"] == ["x"]
    with pytest.raises(ValueError):
        applets.manifest("tools")
    with pytest.raises(ValueError):
        applets.manifest({"tools": "web_search"})


def test_inject():
    assert applets.inject("<html><head><title>x</title></head></html>").startswith("<html><head><script>")
    assert applets.inject("<!doctype html><html lang=zh><body>hi</body></html>").index("<script>") > 0
    assert applets.inject("<p>hi</p>").startswith("<script>")
    assert "window.osa" in applets.SDK_JS


def test_save_page_with_app(bot):
    b = bot()
    p = library.save_page(b["id"], "看板", "<html></html>", "html", app={"tools": ["web_search"], "chat": True})
    assert p["app"] == {"tools": ["web_search"], "chat": True}
    # 更新时不给 app 就保留；markdown 没有 app
    p2 = library.save_page(b["id"], "看板 2", "<html>2</html>", "html", pid=p["id"])
    assert p2["app"] == p["app"] and p2["title"] == "看板 2"
    p3 = library.save_page(b["id"], "看板", "<html></html>", "html", pid=p["id"], app={})
    assert p3["app"] == p["app"]   # 空清单也算没给
    md = library.save_page(b["id"], "报告", "# hi", "markdown", app={"tools": ["web_search"]})
    assert md["app"] is None
    library.delete_page(p["id"])
    library.delete_page(md["id"])


def test_tool_save_page_checks_tools(bot, echo):
    b = bot()
    out = library.tool_save_page(b["id"], {"title": "x", "content": "<p>x</p>", "format": "html", "app": {"tools": ["no_such_tool"]}})
    assert "没保存" in out and "no_such_tool" in out
    out = library.tool_save_page(b["id"], {"title": "x", "content": "<p>x</p>", "format": "html", "app": {"tools": ["echo"]}})
    assert "已保存小应用" in out and "page_data" in out
    pid = int(out.split("#")[1].split("「")[0])
    assert library.get_page(pid, False)["app"] == {"tools": ["echo"], "chat": False}
    library.delete_page(pid)


async def test_data_and_limits(bot):
    b = bot()
    p = library.save_page(b["id"], "表单", "<p></p>", "html", app={"tools": []})
    pid = p["id"]
    assert library.get_data(pid) == {}
    assert library.set_data(pid, {"name": "张三", "n": 3, "list": [1, 2]}) == {"name": "张三", "n": 3, "list": [1, 2]}
    assert library.set_data(pid, {"n": None}) == {"name": "张三", "list": [1, 2]}
    with pytest.raises(ValueError):
        library.set_data(pid, {"big": "x" * (applets.MAX_VALUE + 1)})
    with pytest.raises(ValueError):
        library.set_data(pid, {f"k{i}": i for i in range(applets.MAX_KEYS + 1)})
    with pytest.raises(ValueError):
        library.set_data(pid, {"": 1})
    # Bot 用工具读写：别人的只能读；不是小应用的没有数据
    other = bot()
    assert "只能读" in await library.tool_page_data(other["id"], {"page_id": pid, "set": {"a": 1}})
    assert "张三" in await library.tool_page_data(other["id"], {"page_id": pid})
    out = await library.tool_page_data(b["id"], {"page_id": pid, "set": {"status": "done"}})
    assert "已写入 1 个键" in out and '"status": "done"' in out
    plain = library.save_page(b["id"], "报告", "# hi")
    assert "不是小应用" in await library.tool_page_data(b["id"], {"page_id": plain["id"]})
    assert "没有页面" in await library.tool_page_data(b["id"], {"page_id": 999999})
    library.delete_page(pid)
    assert library.get_data(pid) == {}   # 删页面时数据一起删
    library.delete_page(plain["id"])


async def test_rest_data_call_and_raw(no_password, bot, echo, events):
    b = bot()
    async with http() as c:
        r = await c.post("/api/pages", json={"bot_id": b["id"], "title": "工具面板", "format": "html",
                                             "content": "<html><head></head><body>hi</body></html>", "app": {"tools": ["echo"]}})
        assert r.status_code == 200 and r.json()["app"] == {"tools": ["echo"], "chat": False}
        pid = r.json()["id"]
        plain = (await c.post("/api/pages", json={"bot_id": b["id"], "title": "普通", "format": "html", "content": "<p>x</p>"})).json()["id"]
        # 原文：小应用注入 SDK，普通 html 页面不注入
        assert "window.osa" in (await c.get(f"/api/pages/{pid}/raw")).text
        assert "window.osa" not in (await c.get(f"/api/pages/{plain}/raw")).text
        # 数据
        assert (await c.get(f"/api/pages/{plain}/data")).status_code == 400
        assert (await c.put(f"/api/pages/{pid}/data", json={"a": 1, "b": "二"})).json() == {"a": 1, "b": "二"}
        assert (await c.get(f"/api/pages/{pid}/data")).json() == {"a": 1, "b": "二"}
        assert (await c.put(f"/api/pages/{pid}/data", json={"a": None})).json() == {"b": "二"}
        assert any(e.get("type") == "page_data" and e["page_id"] == pid and e["data"] == {"b": "二"} for e in events.events)
        assert (await c.put(f"/api/pages/{pid}/data", json={"": 1})).status_code == 400
        # 调工具：只认清单里的
        r = await c.post(f"/api/pages/{pid}/call", json={"tool": "echo", "args": {"text": "你好"}})
        assert r.status_code == 200 and r.json() == {"result": "echo:你好"}
        assert (await c.post(f"/api/pages/{pid}/call", json={"tool": "web_search", "args": {"query": "x"}})).status_code == 403
        assert (await c.post(f"/api/pages/{pid}/call", json={"tool": "echo", "args": "x"})).status_code == 400
        # 页面不能发消息
        assert (await c.post(f"/api/pages/{pid}/say", json={"text": "hi"})).status_code == 403
    library.delete_page(pid)
    library.delete_page(plain)


async def test_call_waits_for_approval(no_password, bot, echo, events):
    """命中“先问我”规则的工具：卡片出现在 Bot 的私聊里，用户同意才执行，拒绝就报错。"""
    b = bot()
    p = library.save_page(b["id"], "面板", "<p></p>", "html", app={"tools": ["echo"]})
    approvals.add_rule("echo", "ask")
    try:
        async with http() as c:
            req = asyncio.create_task(c.post(f"/api/pages/{p['id']}/call", json={"tool": "echo", "args": {"text": "x"}}))
            card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "echo"), None), what="确认卡片")
            assert card["chat_id"] == b["chat_id"] and card["bot_id"] == b["id"] and "面板" in card["goal"]
            approvals.resolve(card["id"], True)
            r = await req
            assert r.status_code == 200 and r.json()["result"] == "echo:x"
            req = asyncio.create_task(c.post(f"/api/pages/{p['id']}/call", json={"tool": "echo", "args": {"text": "y"}}))
            card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "echo"), None), what="确认卡片")
            approvals.resolve(card["id"], False, "不要")
            r = await req
            assert r.status_code == 403 and "拒绝" in r.json()["detail"] and "不要" in r.json()["detail"]
    finally:
        memory.conn.execute("delete from rules where pattern='echo'")
        memory.conn.commit()
        library.delete_page(p["id"])


async def test_say_goes_to_bot_chat(no_password, llm, events, bot):
    b = bot("Formy")
    p = library.save_page(b["id"], "报名表", "<p></p>", "html", app={"tools": [], "chat": True})
    llm.script("router:Formy", "收到报名")
    async with http() as c:
        r = await c.post(f"/api/pages/{p['id']}/say", json={"text": "张三报名了"})
        assert r.status_code == 200
        assert (await c.post(f"/api/pages/{p['id']}/say", json={"text": "  "})).status_code == 400
    await until(lambda: find(b["chat_id"], role="assistant", text="收到报名"), what="Bot 的回复")
    user = find(b["chat_id"], role="user", text="张三报名了")[0]
    assert user["meta"]["applet"] == {"id": p["id"], "title": "报名表"}
    body = llm.bodies("router", "Formy")[-1]
    assert any(m["role"] == "user" and "（来自小应用「报名表」）张三报名了" in m["content"] for m in body["messages"])
    library.delete_page(p["id"])


def test_prompts_mention_applets():
    from osa import router, worker
    assert "window.osa" in router.ROUTER_PROMPT and "page_data" in router.ROUTER_PROMPT
    assert "window.osa" in worker.PAGE_HINT
    assert any(s["function"]["name"] == "page_data" for s in registry.schemas(registry.ROUTER, router.ROUTER_ENV))
