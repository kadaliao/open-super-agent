"""应用自带的界面（MCP Apps，osa/mcpapps.py）：接一个真的 stdio 应用（tests/fixtures/mcp_ui_server.py），
看工具按 visibility 分给谁、界面的 CSP、界面调工具（含确认卡片）、任务结果挂上界面、界面替用户发消息、断开后都撤掉。"""
import asyncio
import sys
from pathlib import Path
import httpx
import pytest
from conftest import find, until
from fakellm import Call
from osa import approvals, bots, mcp_hub, mcpapps, memory, registry, server

SERVER = Path(__file__).parent / "fixtures" / "mcp_ui_server.py"
URI = "ui://weather/view.html"


def http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


@pytest.fixture
async def uiapp():
    mcp_hub.save("uidemo", {"command": sys.executable, "args": [str(SERVER)]})
    status = await mcp_hub.connect("uidemo", timeout=60)
    assert mcp_hub.session_of("uidemo"), status
    yield "uidemo"
    await mcp_hub.disconnect("uidemo")
    mcp_hub.remove("uidemo")


@pytest.fixture
def bot(make_bot):
    made = []

    def make(name: str = "") -> dict:
        made.append(make_bot(name))
        return made[-1]
    yield make
    for b in made:
        bots.delete(b["id"])


def test_tool_ui_meta():
    assert mcpapps.tool_ui({"ui": {"resourceUri": URI}}) == (URI, ["model", "app"])
    assert mcpapps.tool_ui({"ui/resourceUri": URI}) == (URI, ["model", "app"])          # 废弃的写法也认
    assert mcpapps.tool_ui({"ui": {"resourceUri": "https://x", "visibility": ["app", "evil"]}}) == (None, ["app"])
    assert mcpapps.tool_ui(None) == (None, ["model", "app"])


def test_csp_only_takes_clean_domains():
    c = mcpapps.csp({"resourceDomains": ["https://cdn.ok.com", "https://a.com; script-src *", "*", "javascript:x",
                                         "'unsafe-eval'", "https://*.img.org"],
                     "connectDomains": ["wss://live.ok.com", "http://evil.com", "http://localhost:3000"]})
    assert c.startswith("sandbox allow-scripts")
    assert "script-src 'unsafe-inline' https://cdn.ok.com https://*.img.org;" in c
    assert "connect-src wss://live.ok.com http://localhost:3000;" in c
    assert "a.com" not in c and "unsafe-eval" not in c and "javascript" not in c and "evil.com" not in c
    assert "'self'" not in c and "allow-same-origin" not in c
    d = mcpapps.csp(None)
    assert "connect-src 'none'" in d and "frame-src 'none'" in d and "script-src 'unsafe-inline' ;" in d


async def test_tools_by_visibility(uiapp):
    w = {s["function"]["name"] for s in registry.schemas(registry.WORKER, {"scope": registry.WORKER, "pages": True})}
    assert "uidemo__show_weather" in w and "uidemo__secret_tool" in w
    assert "uidemo__refresh_weather" not in w                         # 只给界面用的不给模型
    assert registry.available("uidemo__refresh_weather", registry.APPLET) is None   # 也不给小应用
    assert registry.get("uidemo__show_weather").meta["mcp"] == {
        "tool": "show_weather", "ui": URI, "visibility": ["model", "app"], "description": "查一个城市的天气，结果用界面显示",
        "inputSchema": registry.get("uidemo__show_weather").schema["function"]["parameters"]}


async def test_view_endpoint(uiapp, no_password):
    async with http() as c:
        r = await c.get("/api/mcp-apps/uidemo/view", params={"uri": URI})
        assert r.status_code == 200 and "new App(" in r.text
        csp = r.headers["content-security-policy"]
        assert csp.startswith("sandbox allow-scripts") and "script-src 'unsafe-inline' https://cdn.jsdelivr.net;" in csp
        assert "connect-src 'none'" in csp
        assert (await c.get("/api/mcp-apps/uidemo/view", params={"uri": "ui://weather/other.html"})).status_code == 404
        assert (await c.get("/api/mcp-apps/nope/view", params={"uri": URI})).status_code == 404


async def test_call_from_view(uiapp, no_password, bot, events):
    b = bot()
    async with http() as c:
        r = await c.post("/api/mcp-apps/uidemo/call", json={"name": "refresh_weather", "arguments": {"city": "杭州"}, "bot_id": b["id"]})
        assert r.status_code == 200 and r.json()["structuredContent"]["city"] == "杭州"
        assert set(r.json()) <= {"content", "structuredContent", "isError", "_meta"}
        # 只给模型的工具界面不能调；没有的工具 404
        r = await c.post("/api/mcp-apps/uidemo/call", json={"name": "secret_tool", "arguments": {}, "bot_id": b["id"]})
        assert r.status_code == 403
        assert (await c.post("/api/mcp-apps/uidemo/call", json={"name": "nope", "arguments": {}})).status_code == 404
        assert (await c.post("/api/mcp-apps/uidemo/call", json={"name": "refresh_weather", "arguments": "x"})).status_code == 400
        # 命中“先问我”：卡片在 Bot 的私聊里，同意才调；拒绝 403
        approvals.add_rule("uidemo__refresh_weather", "ask")
        try:
            req = asyncio.create_task(c.post("/api/mcp-apps/uidemo/call", json={"name": "refresh_weather", "arguments": {"city": "苏州"}, "bot_id": b["id"]}))
            card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "uidemo__refresh_weather"), None), what="确认卡片")
            assert card["chat_id"] == b["chat_id"] and "界面" in card["goal"]
            approvals.resolve(card["id"], True)
            assert (await req).json()["structuredContent"]["city"] == "苏州"
            req = asyncio.create_task(c.post("/api/mcp-apps/uidemo/call", json={"name": "refresh_weather", "arguments": {"city": "x"}, "bot_id": b["id"]}))
            card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "uidemo__refresh_weather"), None), what="确认卡片")
            approvals.resolve(card["id"], False)
            assert (await req).status_code == 403
            # Bot 删了：要确认的就不能调
            r = await c.post("/api/mcp-apps/uidemo/call", json={"name": "refresh_weather", "arguments": {}, "bot_id": 999999})
            assert r.status_code == 403
        finally:
            memory.conn.execute("delete from rules where pattern='uidemo__refresh_weather'")
            memory.conn.commit()
        r = await c.post("/api/mcp-apps/uidemo/read", json={"uri": URI})
        assert r.status_code == 200 and r.json()["contents"][0]["mimeType"] == mcpapps.MIME


async def test_task_result_carries_view(uiapp, llm, events, bot):
    b = bot("Weatherly")
    llm.script("router:Weatherly", Call("dispatch", {"goal": "查杭州天气"}), "查着")
    llm.script("worker", Call("uidemo__show_weather", {"city": "杭州"}), "杭州今天晴，21 度")
    await server.safe_handle(bots.chat(b["chat_id"]), "杭州天气怎么样")
    res = (await until(lambda: find(b["chat_id"], kind="result"), what="任务结果"))[0]
    [v] = res["meta"]["apps"]
    assert v["app"] == "uidemo" and v["tool"] == "show_weather" and v["uri"] == URI and v["input"] == {"city": "杭州"}
    assert v["result"]["structuredContent"] == {"city": "杭州", "temp": 21, "sky": "晴"}
    tool = [m for m in llm.bodies("worker")[-1]["messages"] if m["role"] == "tool"][0]
    assert "不要贴原始数据" in tool["content"]                          # 告诉模型不用复述界面
    assert not any(t["function"]["name"] == "uidemo__refresh_weather" for t in llm.bodies("worker")[-1]["tools"])


async def test_say_from_view(uiapp, no_password, llm, events, bot):
    b = bot("Talky")
    llm.script("router:Talky", "收到")
    async with http() as c:
        r = await c.post("/api/mcp-apps/uidemo/say", json={"text": "界面说：杭州已刷新", "bot_id": b["id"]})
        assert r.status_code == 200
        assert (await c.post("/api/mcp-apps/uidemo/say", json={"text": "x", "bot_id": 999999})).status_code == 404
        assert (await c.post("/api/mcp-apps/nope/say", json={"text": "x", "bot_id": b["id"]})).status_code == 404
    await until(lambda: find(b["chat_id"], role="assistant", text="收到"), what="回复")
    user = find(b["chat_id"], role="user", text="杭州已刷新")[0]
    assert user["meta"]["applet"] == {"app": "uidemo", "title": "@uidemo"}
    body = llm.bodies("router", "Talky")[-1]
    assert any(m["role"] == "user" and "（来自应用 @uidemo 的界面）界面说" in m["content"] for m in body["messages"])


async def test_disconnect_drops_tools_and_views(no_password):
    mcp_hub.save("uidemo2", {"command": sys.executable, "args": [str(SERVER)]})
    try:
        await mcp_hub.connect("uidemo2", timeout=60)
        async with http() as c:
            assert (await c.get("/api/mcp-apps/uidemo2/view", params={"uri": URI})).status_code == 200
            assert ("uidemo2", URI) in mcpapps._views
            await mcp_hub.disconnect("uidemo2")
            assert registry.get("uidemo2__show_weather") is None and not mcp_hub.session_of("uidemo2")
            assert ("uidemo2", URI) not in mcpapps._views
            assert (await c.get("/api/mcp-apps/uidemo2/view", params={"uri": URI})).status_code == 404
    finally:
        mcp_hub.remove("uidemo2")


async def test_router_capabilities_skip_app_only_tools(uiapp, bot):
    from osa import router
    r = router.Router(bot()["id"])
    caps = r._capabilities()
    line = next(x for x in caps.splitlines() if "@uidemo" in x)
    assert "show_weather" in line and "secret_tool" in line and "refresh_weather" not in line
