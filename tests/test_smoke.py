"""冒烟测试：私聊派任务、群聊交接、Bot 之间私信、确认卡片进私聊。"""
from conftest import find, settle, until
from fakellm import Call
from osa import approvals, bots, mcp_hub, server


async def test_dispatch_in_direct_chat(llm, events, make_bot):
    a = make_bot("Scout")
    llm.script("router:Scout", Call("dispatch", {"goal": "查一下北京明天的天气"}), "已经派出去了")
    llm.script("worker", "北京明天晴，最高 12 度")

    await server.safe_handle(bots.chat(a["chat_id"]), "帮我查下北京明天天气")
    result = await until(lambda: find(a["chat_id"], kind="result"), what="任务结果")

    assert "北京明天晴" in result[0]["content"]
    assert find(a["chat_id"], role="assistant", text="已经派出去了")
    assert find(a["chat_id"], kind="dispatch", text="查一下北京明天的天气")
    worker = llm.bodies("worker")
    assert worker and worker[0]["messages"][1] == {"role": "user", "content": "查一下北京明天的天气"}


async def test_group_handoff_after_task(llm, events, make_bot):
    a, b = make_bot("Finder"), make_bot("Writer")
    group = bots.create_group([a["id"], b["id"]], lead=a["id"])
    llm.script("router:Finder", Call("dispatch", {"goal": "查资料", "handoff_to": "Writer", "handoff_note": "写个总结"}),
               "我先查，查完交给 Writer")
    llm.script("worker", "资料：三条要点")
    llm.script("router:Writer", "总结：三条要点都很重要")

    await server.safe_handle(group, "@Finder 查资料，查完交给 @Writer 写总结")
    await until(lambda: find(group["id"], role="assistant", bot_id=b["id"]), what="Writer 的总结")
    await settle()

    # Writer 被点名，但要等任务结果，所以只说了一次（交接之后）
    assert len(llm.bodies("router", "Writer")) == 1
    assert find(group["id"], kind="handoff", text="任务 #")
    seen = "\n".join(str(m.get("content")) for m in llm.bodies("router", "Writer")[0]["messages"])
    assert "资料：三条要点" in seen and "写个总结" in seen


async def test_dm_reply_comes_back(llm, events, make_bot):
    nova, post = make_bot("Nova"), make_bot("Postman")
    llm.script("router:Nova", Call("message_bot", {"to": "Postman", "text": "看看有没有 Anthropic 的邮件"}),
               "已经让 Postman 去看了", "Postman 说没有新邮件")
    llm.script("router:Postman", "没有 Anthropic 的新邮件")

    await server.safe_handle(bots.chat(nova["chat_id"]), "让 Postman 看看有没有 Anthropic 的邮件")
    await until(lambda: find(nova["chat_id"], role="assistant", text="Postman 说没有新邮件"), what="Nova 转告回信")

    assert find(post["chat_id"], kind="dm", text="看看有没有 Anthropic 的邮件")
    assert find(nova["chat_id"], kind="dm_reply", text="没有 Anthropic 的新邮件")
    assert find(post["chat_id"], kind="dm_reply")[0]["meta"].get("sent")


async def test_approval_goes_to_private_chat(llm, events, make_bot, mcp_tool):
    async def create_note(text: str = "") -> str:
        return f"已创建笔记：{text}"
    schema = {"type": "function", "function": {"name": "demo__create_note", "description": "新建笔记",
              "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}
    mcp_tool("demo__create_note", create_note, schema, confirm=True)

    a, b = make_bot("Keeper"), make_bot("Helper")
    group = bots.create_group([a["id"], b["id"]], lead=a["id"])
    llm.script("router:Keeper", Call("dispatch", {"goal": "记一条笔记：明天开会"}), "这就记")
    llm.script("worker", Call("demo__create_note", {"text": "明天开会"}), "记好了")

    await server.safe_handle(group, "帮我记一条笔记：明天开会")
    card = await until(lambda: [e for e in events.of("approval") if e.get("status") == "pending"], what="确认卡片")

    assert card[0]["chat_id"] == a["chat_id"]          # 卡片在 Keeper 的私聊
    assert card[0]["origin_chat_id"] == group["id"]    # 任务是在群里派的
    approvals.resolve(card[0]["id"], True)
    result = await until(lambda: find(group["id"], kind="result"), what="任务结果")
    assert "记好了" in result[0]["content"]
    tool_out = [m for m in llm.bodies("worker")[-1]["messages"] if m["role"] == "tool"]
    assert tool_out and "已创建笔记：明天开会" in tool_out[0]["content"]
