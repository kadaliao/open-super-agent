"""讨论串：针对某条消息回复，Bot 在讨论串里回答，主对话不受影响。"""
import pytest
from conftest import find, settle, until
from fakellm import Call
from osa import approvals, bots, mcp_hub, memory, server


def _seen(body: dict) -> str:
    return "\n".join(str(m.get("content")) for m in body["messages"])


async def test_thread_in_direct_chat(llm, events, make_bot):
    a = make_bot("Teller")
    cid = a["chat_id"]
    llm.script("router:Teller", "三条新闻：A、B、C", "第二个话题的回答", "B 的详细情况是……")

    await server.safe_handle(bots.chat(cid), "今天有什么新闻")
    await server.safe_handle(bots.chat(cid), "换个话题：天气怎么样")
    root = find(cid, role="assistant", text="三条新闻")
    root_id = memory.conn.execute("select id from messages where chat_id=? and content like '三条新闻%'", (cid,)).fetchone()[0]

    th = bots.thread(cid, root_id)
    assert th["kind"] == "direct" and th["parent"] == cid and th["root"] == root_id
    assert bots.thread(cid, root_id)["id"] == th["id"]          # 同一条消息只有一个讨论串
    await server.safe_handle(bots.chat(th["id"]), "B 展开说说")
    await settle()

    # 回答在讨论串里，主对话里没有
    assert find(th["id"], role="assistant", text="B 的详细情况")
    assert not find(cid, text="B 展开说说") and not find(cid, text="B 的详细情况")
    assert root and len(find(cid, role="assistant")) == 2

    # Bot 看到的是主对话到那条消息为止，加上讨论串；那条消息之后的主对话看不到
    seen = _seen(llm.bodies("router", "Teller")[-1])
    assert "三条新闻：A、B、C" in seen and "B 展开说说" in seen
    assert f"针对上面 #{root_id} 这条消息" in seen
    assert "天气怎么样" not in seen and "第二个话题的回答" not in seen

    # 讨论串不进侧栏；主对话翻页时那条消息带着“N 条回复”
    assert th["id"] not in [c["id"] for c in bots.list_chats()]
    page = {m["id"]: m for m in memory.page(cid)}
    assert page[root_id]["thread"]["id"] == th["id"]
    assert page[root_id]["thread"]["count"] == 2 and page[root_id]["thread"]["bots"] == [a["id"]]
    # 讨论串里的消息事件带着上级对话和摘要
    ev = [e for e in events.of("message") if e["chat_id"] == th["id"]]
    assert ev and ev[-1]["thread"]["parent"] == cid and ev[-1]["thread"]["root"] == root_id
    assert ev[-1]["thread"]["count"] == 2


async def test_task_result_goes_back_to_thread(llm, events, make_bot):
    a = make_bot("Digger")
    cid = a["chat_id"]
    llm.script("router:Digger", "好的，有三家", Call("dispatch", {"goal": "查第二家的详细资料"}), "派出去了")
    llm.script("worker", "第二家成立于 2019 年")

    await server.safe_handle(bots.chat(cid), "列几家做向量数据库的公司")
    root_id = memory.conn.execute("select max(id) from messages where chat_id=? and role='assistant'", (cid,)).fetchone()[0]
    th = bots.thread(cid, root_id)
    await server.safe_handle(bots.chat(th["id"]), "查一下第二家")
    result = await until(lambda: find(th["id"], kind="result"), what="讨论串里的任务结果")

    assert "2019" in result[0]["content"]
    assert find(th["id"], kind="dispatch") and not find(cid, kind="result")
    assert memory.recall(root_id, th["id"]).startswith("[assistant] 好的")   # 讨论串里也能取回主对话的原文


async def test_group_thread_answered_by_root_author(llm, events, make_bot):
    a, b = make_bot("Lead"), make_bot("Expert")
    group = bots.create_group([a["id"], b["id"]], lead=a["id"])
    llm.script("router:Expert", "我的看法是 X", "X 的理由是……")

    await server.safe_handle(group, "@Expert 你怎么看")
    root_id = memory.conn.execute("select max(id) from messages where chat_id=? and role='assistant'", (group["id"],)).fetchone()[0]
    th = bots.thread(group["id"], root_id)
    assert th["kind"] == "group" and th["root_bot"] == b["id"] and th["lead"] == a["id"]

    await server.safe_handle(bots.chat(th["id"]), "为什么")   # 没 @ 人：被回复的 Expert 接，不是协调者
    await settle()
    assert find(th["id"], role="assistant", bot_id=b["id"], text="X 的理由")
    assert not find(th["id"], role="assistant", bot_id=a["id"])
    assert not llm.bodies("router", "Lead")

    # 群里的讨论串：不能当群聊改，删群时一起删
    with pytest.raises(ValueError):
        bots.update_group(th["id"], title="x")
    assert (await server.list_tasks(chat_id=group["id"])) == []
    bots.delete_group(group["id"])
    assert not bots.chat(th["id"]) and not find(th["id"])


async def test_thread_rules(llm, events, make_bot):
    a = make_bot("Rules")
    cid = a["chat_id"]
    await server.safe_handle(bots.chat(cid), "你好")
    root_id = memory.conn.execute("select max(id) from messages where chat_id=? and role='user'", (cid,)).fetchone()[0]
    th = bots.thread(cid, root_id)
    await server.safe_handle(bots.chat(th["id"]), "在讨论串里说一句")
    inner = memory.conn.execute("select max(id) from messages where chat_id=?", (th["id"],)).fetchone()[0]

    with pytest.raises(ValueError):
        bots.thread(th["id"], inner)            # 讨论串里不能再开讨论串
    with pytest.raises(ValueError):
        bots.thread(cid, inner)                 # 消息不在这个对话里
    with pytest.raises(Exception):
        await server.new_topic(th["id"])        # 讨论串里没有“新话题”
    got = await server.get_thread(cid, root_id)
    assert got["id"] == th["id"] and got["root"]["content"] == "你好"


async def test_dm_reply_comes_back_to_thread(llm, events, make_bot):
    nova, post = make_bot("Asker"), make_bot("Mailman")
    cid = nova["chat_id"]
    llm.script("router:Asker", "有两封邮件", Call("message_bot", {"to": "Mailman", "text": "第二封邮件是谁发的"}),
               "已经问 Mailman 了", "Mailman 说是小王发的")
    llm.script("router:Mailman", "小王发的")

    await server.safe_handle(bots.chat(cid), "看下邮件")
    root_id = memory.conn.execute("select max(id) from messages where chat_id=? and role='assistant'", (cid,)).fetchone()[0]
    th = bots.thread(cid, root_id)
    await server.safe_handle(bots.chat(th["id"]), "第二封是谁发的")
    await until(lambda: find(th["id"], role="assistant", text="小王发的"), what="讨论串里转告回信")

    assert find(th["id"], kind="dm_reply", text="小王发的")
    assert not find(cid, kind="dm_reply") and not find(cid, text="Mailman 说")


async def test_approval_from_group_thread(llm, events, make_bot, mcp_tool):
    async def create_note(text: str = "") -> str:
        return f"已创建笔记：{text}"
    schema = {"type": "function", "function": {"name": "demo__create_note", "description": "新建笔记",
              "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}
    mcp_tool("demo__create_note", create_note, schema, confirm=True)

    a, b = make_bot("Noter"), make_bot("Other")
    group = bots.create_group([a["id"], b["id"]], lead=a["id"])
    llm.script("router:Noter", "明天 10 点开会", Call("dispatch", {"goal": "记一条笔记：明天 10 点开会"}), "这就记")
    llm.script("worker", Call("demo__create_note", {"text": "明天 10 点开会"}), "记好了")

    await server.safe_handle(group, "明天几点开会")
    root_id = memory.conn.execute("select max(id) from messages where chat_id=? and role='assistant'", (group["id"],)).fetchone()[0]
    th = bots.thread(group["id"], root_id)
    await server.safe_handle(bots.chat(th["id"]), "帮我记下来")
    card = await until(lambda: [e for e in events.of("approval") if e.get("status") == "pending"], what="确认卡片")

    assert card[0]["chat_id"] == a["chat_id"]                # 卡片还是在 Noter 的私聊
    assert card[0]["origin_chat_id"] == group["id"]          # 在这个群里派的
    assert card[0]["origin_thread"] == th["id"]              # 具体是群里的这个讨论串
    approvals.resolve(card[0]["id"], True)
    result = await until(lambda: find(th["id"], kind="result"), what="讨论串里的任务结果")
    assert "记好了" in result[0]["content"]
    assert [t["id"] for t in await server.list_tasks(chat_id=group["id"])] == [find_id(th["id"], "dispatch")]


def find_id(chat_id: int, kind: str) -> int:
    return memory.conn.execute("select id from messages where chat_id=? and json_extract(meta, '$.kind')=?",
                               (chat_id, kind)).fetchone()[0]


async def test_thread_new_replies_until_seen(llm, events, make_bot):
    """讨论串里 Bot 的回复在你看过之前算“新回复”：刷新、换设备都还在，看过以后各处一起去掉。"""
    a = make_bot("Replier")
    cid = a["chat_id"]
    llm.script("router:Replier", "根消息", "讨论串里的回答", "第二个回答")

    await server.safe_handle(bots.chat(cid), "开始")
    root_id = memory.conn.execute("select max(id) from messages where chat_id=? and role='assistant'", (cid,)).fetchone()[0]
    th = bots.thread(cid, root_id)
    await server.safe_handle(bots.chat(th["id"]), "问一句")
    await settle()

    ev = [e for e in events.of("message") if e["chat_id"] == th["id"]]
    assert ev[0]["thread"]["unread"] == 0 and ev[-1]["thread"]["unread"] == 1   # 你自己说的不算
    news = [x for x in bots.thread_list(unread=True) if x["id"] == th["id"]]
    assert len(news) == 1 and news[0]["parent"] == cid and news[0]["root"] == root_id and news[0]["unread"] == 1
    assert news[0]["root_text"] == "根消息" and news[0]["root_role"] == "assistant"
    assert news[0]["preview"] == "讨论串里的回答" and news[0]["preview_bot"] == a["id"]
    assert next(c for c in bots.list_chats() if c["id"] == cid)["threads"] == 1
    assert [x["id"] for x in await server.list_threads(cid)] == [th["id"]]

    got = await server.thread_seen(th["id"], {})
    assert got["unread"] == 0 and got["count"] == 2
    assert events.of("thread_seen")[-1]["thread"]["id"] == th["id"] and events.of("thread_seen")[-1]["chat_id"] == cid
    assert not [x for x in bots.thread_list(unread=True) if x["id"] == th["id"]]
    assert [x["id"] for x in await server.list_threads(cid)] == [th["id"]]   # 看过的也还在列表里

    # 看过之后的回复又是新的；只看到前面某条为止时，后面的还算新
    first = memory.conn.execute("select max(id) from messages where chat_id=? and role='assistant'", (th["id"],)).fetchone()[0]
    await server.safe_handle(bots.chat(th["id"]), "再问一句")
    await settle()
    assert bots.thread_event(th["id"])["unread"] == 1
    assert bots.mark_seen(th["id"], first)["unread"] == 1
    assert bots.mark_seen(th["id"], 1 << 40)["unread"] == 0   # 超过最新一条的 id 按最新一条算

    with pytest.raises(server.HTTPException):
        await server.thread_seen(cid, {})                     # 不是讨论串
    # 点了讨论串的系统通知：按讨论串的 id 查到它在哪
    where = await server.get_thread_by_id(th["id"])
    assert where["parent"] == cid and where["root"] == root_id
    with pytest.raises(server.HTTPException):
        await server.get_thread_by_id(cid)
