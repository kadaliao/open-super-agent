"""主 Bot 新建 Bot：只有主 Bot 有 create_bot，每次都弹卡片（不能“总是允许”），同意才建、带上技能，
拒绝留一笔；参数不对不弹卡片；别的 Bot 的提示词让它去找主 Bot。"""
import pytest
from conftest import find, settle, until
from fakellm import Call, system_of
from osa import approvals, bots, memory, server, skills


@pytest.fixture
def boss(make_bot):
    """测试里没有主 Bot（服务没启动过）：临时把一个设成主 Bot。用完还原，并删掉这个测试建的 Bot
    （所有测试共用一个数据库，Bot 最多 50 个）。"""
    old = memory.conn.execute("select id from bots where is_primary=1").fetchone()
    start = memory.conn.execute("select coalesce(max(id), 0) from bots").fetchone()[0]
    b = make_bot("Boss")
    bots.set_primary(b["id"])
    yield bots.get(b["id"])
    memory.conn.execute("update bots set is_primary = (id = ?)", (old[0] if old else -1,))
    for (i,) in memory.conn.execute("select id from bots where id > ?", (start,)).fetchall():
        bots.delete(i)
        memory.conn.execute("delete from skills where bot_id=?", (i,))
    memory.conn.commit()


def tools_of(body: dict) -> set[str]:
    return {t["function"]["name"] for t in body.get("tools") or []}


def tool_outputs(body: dict) -> list[str]:
    return [m["content"] for m in body["messages"] if m["role"] == "tool"]


def card():
    return next((x for x in approvals.pending.values() if x["tool"] == "create_bot"), None)


async def test_only_primary_can_create(llm, events, boss, make_bot):
    other = make_bot("Helper7")
    llm.script("router:Helper7", Call("create_bot", {"name": "Sneaky", "instructions": "x"}), "好")
    await server.safe_handle(bots.chat(boss["chat_id"]), "你好")
    await server.safe_handle(bots.chat(other["chat_id"]), "建个 Bot")
    await settle()
    b, o = llm.bodies("router", "Boss")[-1], llm.bodies("router", "Helper7")[-1]
    assert "create_bot" in tools_of(b) and "create_bot" not in tools_of(o)
    assert "只有你能新建 Bot" in system_of(b)
    assert "新建 Bot 只有主 Bot「Boss」能做" in system_of(o) and "message_bot" in system_of(o)
    assert "只有主 Bot（Boss）能新建 Bot" in tool_outputs(o)[0]   # 没给它工具，它硬调也不行
    assert not card() and not bots.by_name("Sneaky")


async def test_create_after_approval(llm, events, boss):
    cid = boss["chat_id"]
    llm.script("router:Boss", Call("create_bot", {
        "name": "Quark7", "instructions": "专门找资源，存进夸克网盘",
        "skills": [{"name": "quark7", "description": "找资源存网盘", "body": "1. 找资源\n2. 存进网盘"}]}), "请在卡片上确认")
    await server.safe_handle(bots.chat(cid), "帮我建一个 Bot 叫 Quark7，专门找资源存网盘")
    c = await until(card, what="确认卡片")
    assert c["chat_id"] == cid and c.get("once")
    assert "Quark7" in c["args"] and "专门找资源" in c["args"] and "/quark7" in c["args"]
    assert find(cid, role="assistant", text="请在卡片上确认")   # 不干等，这一轮照常说完
    assert not bots.by_name("Quark7")                          # 没确认之前不建

    approvals.resolve(c["id"], True, always=True)   # 卡片只能一次一次地允许，不会生成“总是允许”的规则
    msg = await until(lambda: find(cid, kind="bot_created"), what="建好的消息")
    q = bots.by_name("Quark7")
    assert q and q["instructions"] == "专门找资源，存进夸克网盘" and not q["is_primary"] and q["chat_id"]
    assert msg[0]["meta"]["new_bot"] == q["id"] and "Quark7" in msg[0]["content"]
    own = [s for s in skills.list_for(q["id"]) if s["bot_id"] == q["id"]]
    assert [s["name"] for s in own] == ["quark7"] and own[0]["body"] == "1. 找资源\n2. 存进网盘"
    assert not [s for s in skills.list_for(boss["id"]) if s["bot_id"] == boss["id"]]   # 不存成主 Bot 自己的
    assert events.of("bots_changed")
    assert not [r for r in approvals.rules() if r["pattern"] == "create_bot"]


async def test_reject_leaves_a_note(llm, events, boss):
    cid = boss["chat_id"]
    llm.script("router:Boss", Call("create_bot", {"name": "Nope7", "instructions": "随便"}), "等你确认")
    await server.safe_handle(bots.chat(cid), "建个 Nope7")
    c = await until(card, what="确认卡片")
    approvals.resolve(c["id"], False, "名字换一个")
    note = await until(lambda: find(cid, kind="note", text="Nope7"), what="没建的记录")
    assert "名字换一个" in note[0]["content"] and not bots.by_name("Nope7")


async def test_bad_args_no_card(llm, events, boss, make_bot):
    make_bot("Taken7")
    llm.script("router:Boss", Call("create_bot", {"name": "taken7", "instructions": "x"}),
               Call("create_bot", {"name": "Empty7", "instructions": "  "}),
               Call("create_bot", {"name": "Bad7", "instructions": "x", "skills": [{"name": "有 空格", "body": "x"}]}), "好")
    await server.safe_handle(bots.chat(boss["chat_id"]), "建几个 Bot")
    await settle()
    out = tool_outputs(llm.bodies("router", "Boss")[-1])
    assert "已经有叫 taken7 的 Bot" in out[0] and "职责是空的" in out[1] and "技能 有 空格" in out[2]
    assert not card() and not bots.by_name("Empty7") and not bots.by_name("Bad7")
