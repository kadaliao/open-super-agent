"""Worker 一轮里要了好几个工具时：相邻的只读调用一起跑，写的、要确认的照旧一个个按顺序跑；结果、截图按调用的顺序交回模型。"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from fakellm import Call
from conftest import find, until
from osa import approvals, bots, mcp_hub, registry, server


def _tool(name: str, ann: dict | None = None):
    return SimpleNamespace(name=name, annotations=SimpleNamespace(**ann) if ann is not None else None)


@pytest.mark.parametrize("name,app,want", [
    ("notion-fetch", "notion", True),
    ("notion-search", "notion", True),
    ("list_emails_metadata", "gmail", True),
    ("get_emails_content", "gmail", True),
    ("ask_wiki_question", "deepwiki", True),
    ("getUser", "github", True),
    ("notion-update-page", "notion", False),
    ("notion-get-comments", "notion", False),        # 带 comment：宁可当成写
    ("notion-query-data-sources", "notion", False),  # query 不算：数据库应用的 query 什么 SQL 都能跑
    ("mark_emails_as_read", "gmail", False),
    ("search_and_replace", "docs", False),
    ("get_or_create_label", "gmail", False),
    ("createIssue", "github", False),
    ("send_email", "gmail", False),
    ("weather", "amap", False),                      # 不是动词开头：拿不准就当成写
    ("notion", "notion", False),
])
def test_read_only_by_name(name, app, want):
    assert approvals.read_only(_tool(name), app) is want


def test_read_only_trusts_server_hints():
    assert approvals.read_only(_tool("do_stuff", {"readOnlyHint": True}))
    assert not approvals.read_only(_tool("get_thing", {"readOnlyHint": False}))
    assert approvals.read_only(_tool("get_thing", {"readOnlyHint": None}))   # 标了别的、没标只读：按名字
    assert approvals.read_only(_tool("query", {"read_only_hint": True}))


def test_mcp_parallel_per_app():
    read, write = _tool("get_page"), _tool("update_page")
    assert mcp_hub._parallel("remote", {"type": "http"}, read, None) == 3
    assert mcp_hub._parallel("remote", {"type": "sse"}, read, None) == 3
    assert mcp_hub._parallel("local", {"command": "uvx"}, read, None) == 1   # 本机起的一次一个
    assert mcp_hub._parallel("remote", {"type": "http"}, write, None) == 0
    assert mcp_hub._parallel("remote", {"type": "http"}, read, "ui://x") == 0   # 带界面的按顺序挂


def test_builtin_read_tools_are_parallel():
    for name in ("web_search", "fetch_url", "read_page", "search_memory", "use_skill"):
        assert registry.get(name).parallel > 0, name
    for name in ("save_page", "send_file", "page_data", "run_code"):
        assert registry.get(name).parallel == 0, name


# ---------- 在真的任务里 ----------
PNG = lambda tag: b"\x89PNG" + b"\0" * 12 + (8).to_bytes(4, "big") + (8).to_bytes(4, "big") + tag   # noqa: E731


@pytest.fixture
def demo():
    """一个假应用：几个工具记下自己什么时候开始、什么时候结束。"""
    ctx = registry.Context("mcp:demo")
    spans: dict[str, tuple[float, float]] = {}
    order: list[str] = []

    def add(name: str, delay: float, parallel: int, shot: bytes | None = None, group: str = "demo"):
        async def run(call: registry.Call):
            start = time.monotonic()
            order.append(name)
            await asyncio.sleep(delay)
            spans[name] = (start, time.monotonic())
            text = f"{name} 的结果"
            return (text, shot) if shot else text
        ctx.tool(registry.Tool(name, {"type": "function", "function": {
            "name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}, run,
            where=frozenset({registry.WORKER}), gated=True, group=group, parallel=parallel))
    yield SimpleNamespace(add=add, spans=spans, order=order)
    ctx.dispose()


def overlap(spans, a, b) -> bool:
    return spans[a][0] < spans[b][1] and spans[b][0] < spans[a][1]


async def _run(llm, make_bot, *calls):
    """派一个任务，Worker 第一轮一起要 calls，第二轮回答。返回 Worker 第二次请求的消息。"""
    b = make_bot()
    llm.script(f"router:{b['name']}", Call("dispatch", {"goal": "查一下"}), "好")
    llm.script("worker", list(calls), "查完了")
    await server.safe_handle(bots.chat(b["chat_id"]), "查一下")
    await until(lambda: find(b["chat_id"], kind="result"), what="任务结果")
    bots.delete(b["id"])
    return llm.bodies("worker")[-1]["messages"]


async def test_reads_run_together_results_in_order(llm, events, make_bot, demo):
    demo.add("demo__get_a", 0.4, 3)
    demo.add("demo__get_b", 0.1, 3)
    t0 = time.monotonic()
    msgs = await _run(llm, make_bot, Call("demo__get_a"), Call("demo__get_b"))
    assert overlap(demo.spans, "demo__get_a", "demo__get_b")
    assert time.monotonic() - t0 < 2
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_0", "call_1"]   # b 先做完，结果还是按调用的顺序
    assert [m["content"] for m in tool_msgs] == ["demo__get_a 的结果", "demo__get_b 的结果"]
    steps = [e["step"] for e in events.of("task_step") if e["step"]["kind"] == "tool" and e["step"]["status"] == "running"]
    assert [s["title"] for s in steps][:2] == ["demo__get_a", "demo__get_b"]   # 任务过程里的顺序也一样


async def test_write_in_between_keeps_order(llm, events, make_bot, demo):
    demo.add("demo__get_a", 0.2, 3)
    demo.add("demo__save_b", 0.2, 0)   # 写的：前面的做完才开始，它做完后面的才开始
    demo.add("demo__get_c", 0.2, 3)
    demo.add("demo__get_d", 0.2, 3)
    await _run(llm, make_bot, Call("demo__get_a"), Call("demo__save_b"), Call("demo__get_c"), Call("demo__get_d"))
    s = demo.spans
    assert s["demo__get_a"][1] <= s["demo__save_b"][0]
    assert s["demo__save_b"][1] <= s["demo__get_c"][0] and s["demo__save_b"][1] <= s["demo__get_d"][0]
    assert overlap(s, "demo__get_c", "demo__get_d")


async def test_limit_per_group(llm, events, make_bot, demo):
    demo.add("one__get_a", 0.2, 1, group="one")   # 本机起的应用：同一个应用一次一个
    demo.add("one__get_b", 0.2, 1, group="one")
    demo.add("two__get_c", 0.3, 3, group="two")
    await _run(llm, make_bot, Call("one__get_a"), Call("one__get_b"), Call("two__get_c"))
    s = demo.spans
    assert not overlap(s, "one__get_a", "one__get_b")
    assert overlap(s, "one__get_a", "two__get_c")


async def test_asking_rule_runs_it_alone(llm, events, make_bot, demo):
    """规则要求先问的只读工具：和写的一样单独跑，卡片点了才继续。"""
    demo.add("demo__get_a", 0.1, 3)
    demo.add("demo__get_b", 0.1, 3)
    rule = approvals.add_rule("demo__get_b", "ask")
    try:
        b = make_bot()
        llm.script(f"router:{b['name']}", Call("dispatch", {"goal": "查一下"}), "好")
        llm.script("worker", [Call("demo__get_a"), Call("demo__get_b")], "查完了")
        await server.safe_handle(bots.chat(b["chat_id"]), "查一下")
        card = (await until(lambda: [e for e in events.of("approval") if e.get("status") == "pending"], what="确认卡片"))[0]
        assert card["tool"] == "demo__get_b" and "demo__get_a" in demo.spans and "demo__get_b" not in demo.spans
        approvals.resolve(card["id"], True)
        await until(lambda: find(b["chat_id"], kind="result"), what="任务结果")
        assert demo.spans["demo__get_a"][1] <= demo.spans["demo__get_b"][0]
        bots.delete(b["id"])
    finally:
        approvals.delete_rule(rule["id"])


async def test_screenshot_follows_call_order(llm, events, make_bot, demo):
    demo.add("demo__get_a", 0.3, 3, shot=PNG(b"A"))
    demo.add("demo__get_b", 0.05, 3, shot=PNG(b"B"))   # 先做完，但它是后调的：附上的是它的图
    msgs = await _run(llm, make_bot, Call("demo__get_a"), Call("demo__get_b"))
    import base64
    img = msgs[-1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(img.split(",", 1)[1]).endswith(b"B")
