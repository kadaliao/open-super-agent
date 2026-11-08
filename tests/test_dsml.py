"""DeepSeek 把工具调用写进正文（<｜DSML｜…> 标记，见 osa/dsml.py）：认出来当真的工具调用去做，标记不给用户看。"""
import json
import pytest
from conftest import find, settle, until
from osa import bots, dsml, memory, server


@pytest.fixture(autouse=True)
def cleanup():
    """所有测试共用一个数据库，Bot 最多 50 个：删掉这里建的。"""
    start = memory.conn.execute("select coalesce(max(id), 0) from bots").fetchone()[0]
    yield
    for (i,) in memory.conn.execute("select id from bots where id > ?", (start,)).fetchall():
        bots.delete(i)

# 生产上漏出来的原样（竖线个数、空格都不规整）
LEAK = ('<｜｜DSML｜｜ invoke name="message_bot">\n'
        '<｜｜DSML｜｜ parameter name="to" string="true">{to}</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="text" string="true">在吗？\n回我一句「在」</｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')


def test_split():
    text, calls = dsml.split("我去问问。\n\n" + LEAK.format(to="Quark"))
    assert text == "我去问问。"
    assert [(c["name"], json.loads(c["args"])) for c in calls] == [("message_bot", {"to": "Quark", "text": "在吗？\n回我一句「在」"})]

    text, calls = dsml.split('<|DSML|function_calls>\n<|DSML|invoke name="a">\n'
                             '<|DSML|parameter name="n" string="false">3</|DSML|parameter>\n</|DSML|invoke>\n'
                             '<|DSML|invoke name="b">\n</|DSML|invoke>\n</|DSML|function_calls>\n好了')
    assert text == "好了" and [(c["name"], json.loads(c["args"])) for c in calls] == [("a", {"n": 3}), ("b", {})]
    assert dsml.split("a < b，<b>粗体</b>") == ("a < b，<b>粗体</b>", [])
    # 半截断掉的：参数值不当正文
    assert dsml.split('好 <｜DSML｜invoke name="x">\n<｜DSML｜parameter name="t" string="true">半截')[0] == "好"


def test_visible():
    assert dsml.visible("你好 <｜｜DSML｜｜ invoke") == 3
    assert dsml.visible("你好 <｜") == 3 and dsml.visible("你好 <") == 3   # 可能是标记的开头，先压着
    assert dsml.visible("a < b") == 5 and dsml.visible("x </b") == 5


async def test_router_runs_leaked_call(llm, events, make_bot):
    a, b = make_bot("Leaky"), make_bot("Target")
    llm.script("router:Leaky", "我私信它一下。\n\n" + LEAK.format(to="Target"), "已经私信 Target 了")
    await server.safe_handle(bots.chat(a["chat_id"]), "去问问 Target")
    assert await until(lambda: find(b["chat_id"], kind="dm", text="回我一句「在」"), what="私信送到")
    await settle()
    said = find(a["chat_id"], role="assistant", bot_id=a["id"])
    assert "我私信它一下。" in said[0]["content"] and "DSML" not in said[0]["content"]
    assert not [e for e in events.of("stream") if "DSML" in (e.get("delta") or "")]   # 边收边推送时也没漏出去
    tool = [m for m in llm.bodies("router", "Leaky")[1]["messages"] if m["role"] == "tool"]
    assert tool and tool[0]["content"].startswith("已私信 Target")


async def test_worker_runs_leaked_call(llm, events, make_bot):
    a = make_bot("Paged")
    from fakellm import Call
    llm.script("router:Paged", Call("dispatch", {"goal": "整理一页"}), "派了")
    llm.script("worker", '整理好了。\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="save_page">\n'
               '<｜｜DSML｜｜ parameter name="title" string="true">漏出来的页面</｜｜DSML｜｜ parameter>\n'
               '<｜｜DSML｜｜ parameter name="content" string="true"><h1>标题</h1><p>正文</p></｜｜DSML｜｜ parameter>\n'
               '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>', "页面存好了")
    await server.safe_handle(bots.chat(a["chat_id"]), "整理一页")
    result = await until(lambda: find(a["chat_id"], kind="result"), what="任务结果")
    assert "页面存好了" in result[0]["content"] and "DSML" not in result[0]["content"]
    tool = [m for m in llm.bodies("worker")[-1]["messages"] if m["role"] == "tool"]
    assert tool and "漏出来的页面" in tool[0]["content"]


def test_history_drops_old_leaks(make_bot):
    a = make_bot("OldLeak")
    cid = a["chat_id"]
    memory.add("user", "问问 Quark", cid, a["id"])
    memory.add("assistant", LEAK.format(to="Quark"), cid, a["id"], reasoning="")
    memory.add("assistant", "好的。\n" + LEAK.format(to="Quark"), cid, a["id"], reasoning="")
    ctx = [m for m in memory.context(cid, me=a["id"]) if m["role"] == "assistant"]
    assert "DSML" not in str(ctx)
    assert "message_bot" in ctx[0]["content"] and "没有执行" in ctx[0]["content"] and ctx[1]["content"] == "好的。"
