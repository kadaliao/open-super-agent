"""Bot 管理 Bot（botadmin.py）：主 Bot 能查看、新建、修改、删除所有 Bot，别的 Bot 只能改自己。
新建、修改、删除每次都弹卡片（不能“总是允许”），同意才做，拒绝留一笔；参数不对不弹卡片；别的 Bot 的提示词让它去找主 Bot。
有了结果发起的 Bot 会被叫醒接着说一轮（能去私信新 Bot）；别的 Bot 托它做的，结果作为回信送回去。"""
import pytest
from conftest import find, settle, until
from fakellm import Call, system_of
from osa import approvals, botadmin, bots, memories, memory, routines, server, skills


@pytest.fixture
async def boss(make_bot):
    """测试里没有主 Bot（服务没启动过）：临时把一个设成主 Bot。用完还原，并删掉这个测试建的 Bot 和它们的技能、定时任务、记忆
    （所有测试共用一个数据库，Bot 最多 50 个）。"""
    old = memory.conn.execute("select id from bots where is_primary=1").fetchone()
    start = memory.conn.execute("select coalesce(max(id), 0) from bots").fetchone()[0]
    b = make_bot("Boss")
    bots.set_primary(b["id"])
    yield bots.get(b["id"])
    await settle()
    memory.conn.execute("update bots set is_primary = (id = ?)", (old[0] if old else -1,))
    memory.conn.commit()
    for (i,) in memory.conn.execute("select id from bots where id > ?", (start,)).fetchall():
        await botadmin.remove(i)


def tools_of(body: dict) -> set[str]:
    return {t["function"]["name"] for t in body.get("tools") or []}


def tool_outputs(body: dict) -> list[str]:
    return [m["content"] for m in body["messages"] if m["role"] == "tool"]


def card(tool: str = "create_bot"):
    return next((x for x in approvals.pending.values() if x["tool"] == tool), None)


def last_user(body: dict) -> str:
    return next(str(m.get("content")) for m in reversed(body["messages"]) if m["role"] == "user")


async def test_only_primary_can_create(llm, events, boss, make_bot):
    other = make_bot("Helper7")
    llm.script("router:Helper7", Call("create_bot", {"name": "Sneaky", "instructions": "x"}), "好")
    await server.safe_handle(bots.chat(boss["chat_id"]), "你好")
    await server.safe_handle(bots.chat(other["chat_id"]), "建个 Bot")
    await settle()
    b, o = llm.bodies("router", "Boss")[-1], llm.bodies("router", "Helper7")[-1]
    assert {"get_bot", "create_bot", "update_bot", "delete_bot"} <= tools_of(b)
    assert "update_bot" in tools_of(o) and not {"get_bot", "create_bot", "delete_bot"} & tools_of(o)   # 只能改自己
    assert "你是主 Bot，负责管理所有 Bot" in system_of(b)
    assert "管理 Bot 的事归主 Bot「Boss」" in system_of(o) and "message_bot" in system_of(o)
    assert "只有主 Bot（Boss）能新建 Bot" in tool_outputs(o)[0]   # 没给它工具，它硬调也不行
    assert not card() and not bots.by_name("Sneaky")


async def test_create_after_approval(llm, events, boss):
    cid = boss["chat_id"]
    llm.script("router:Boss", Call("create_bot", {
        "name": "Quark7", "instructions": "专门找资源，存进夸克网盘", "avatar": {"shape": "star", "color": "#4fc3a1"},
        "skills": [{"name": "quark7", "description": "找资源存网盘", "body": "1. 找资源\n2. 存进网盘"}]}), "请在卡片上确认",
        Call("message_bot", {"to": "Quark7", "text": "交代一下：夸克要扫码登录"}), "已经交代给 Quark7 了")
    await server.safe_handle(bots.chat(cid), "帮我建一个 Bot 叫 Quark7，专门找资源存网盘")
    assert "有了结果你会接着说一轮" in tool_outputs(llm.bodies("router", "Boss")[-1])[0]
    c = await until(card, what="确认卡片")
    assert c["chat_id"] == cid and c.get("once")
    assert "Quark7" in c["args"] and "专门找资源" in c["args"] and "/quark7" in c["args"]
    assert find(cid, role="assistant", text="请在卡片上确认")   # 不干等，这一轮照常说完
    assert not bots.by_name("Quark7")                          # 没确认之前不建

    approvals.resolve(c["id"], True, always=True)   # 卡片只能一次一次地允许，不会生成“总是允许”的规则
    msg = await until(lambda: find(cid, kind="bot_created"), what="建好的消息")
    q = bots.by_name("Quark7")
    assert q and q["instructions"] == "专门找资源，存进夸克网盘" and not q["is_primary"] and q["chat_id"]
    assert q["avatar"] == {"shape": "star", "color": "#4fc3a1"}
    assert msg[0]["meta"]["new_bot"] == q["id"] and "Quark7" in msg[0]["content"]
    own = [s for s in skills.list_for(q["id"]) if s["bot_id"] == q["id"]]
    assert [s["name"] for s in own] == ["quark7"] and own[0]["body"] == "1. 找资源\n2. 存进网盘"
    assert not [s for s in skills.list_for(boss["id"]) if s["bot_id"] == boss["id"]]   # 不存成主 Bot 自己的
    assert events.of("bots_changed")
    assert not [r for r in approvals.rules() if r["pattern"] == "create_bot"]

    # 建好后主 Bot 被叫醒：知道建好了，去做答应过的事（私信新 Bot）
    dm = await until(lambda: find(q["chat_id"], kind="dm", text="夸克要扫码登录"), what="主 Bot 私信新 Bot")
    assert dm[0]["bot_id"] == boss["id"]
    woke = llm.bodies("router", "Boss")[2]   # 前两次是用户那一轮（调工具、再说话）
    assert "新建 / 修改 / 删除 Bot 有结果了" in last_user(woke) and "已新建 Bot「Quark7」" in str(woke["messages"])
    assert find(cid, role="assistant", text="已经交代给 Quark7 了")
    await settle()


async def test_reject_leaves_a_note(llm, events, boss):
    cid = boss["chat_id"]
    llm.script("router:Boss", Call("create_bot", {"name": "Nope7", "instructions": "随便"}), "等你确认")
    await server.safe_handle(bots.chat(cid), "建个 Nope7")
    c = await until(card, what="确认卡片")
    approvals.resolve(c["id"], False, "名字换一个")
    note = await until(lambda: find(cid, kind="note", text="Nope7"), what="没建的记录")
    assert "名字换一个" in note[0]["content"] and not bots.by_name("Nope7")
    # 附了意见：叫醒主 Bot，好按意见改了再提
    await until(lambda: len(llm.bodies("router", "Boss")) == 3, what="主 Bot 被叫醒")
    assert "新建 / 修改 / 删除 Bot 有结果了" in last_user(llm.bodies("router", "Boss")[2])


async def test_reject_without_note_stays_quiet(llm, events, boss):
    cid = boss["chat_id"]
    llm.script("router:Boss", Call("create_bot", {"name": "Nope8", "instructions": "随便"}), "等你确认")
    await server.safe_handle(bots.chat(cid), "建个 Nope8")
    approvals.resolve((await until(card, what="确认卡片"))["id"], False)
    await until(lambda: find(cid, kind="note", text="Nope8"), what="没建的记录")
    await settle()
    assert len(llm.bodies("router", "Boss")) == 2   # 只有用户那一轮（调工具、再说话）。用户只是不要，没什么要主 Bot 接着说的


async def test_requested_by_other_bot_gets_result(llm, events, boss, make_bot):
    """别的 Bot 私信主 Bot 要一个新 Bot：卡片弹出时不回信，有了结果再把主 Bot 那一轮的回复作为回信送回去。"""
    other = make_bot("Asker8")
    llm.script("router:Asker8", Call("message_bot", {"to": "Boss", "text": "需要一个管网盘的 Bot，叫 Quark8"}),
               "已经请 Boss 建了", "Quark8 建好了")
    llm.script("router:Boss", Call("create_bot", {"name": "Quark8", "instructions": "管网盘"}), "卡片弹出来了，等用户确认",
               "Quark8 建好了，可以私信它")
    await server.safe_handle(bots.chat(other["chat_id"]), "让 Boss 建个管网盘的 Bot")
    c = await until(card, what="确认卡片")
    await settle()
    assert "回信发给 Asker8" in tool_outputs(llm.bodies("router", "Boss")[-1])[0]
    assert not find(other["chat_id"], kind="dm_reply")   # 还没结果：不回信

    approvals.resolve(c["id"], True)
    reply = await until(lambda: find(other["chat_id"], kind="dm_reply"), what="回信")
    assert "Quark8 建好了，可以私信它" in reply[0]["content"] and bots.by_name("Quark8")
    assert "Asker8 私信请你做的" in last_user(llm.bodies("router", "Boss")[-1])
    assert '"请求方": "Asker8"' in c["args"]   # 卡片上写着是谁托它建的
    await until(lambda: find(other["chat_id"], role="assistant", text="Quark8 建好了"), what="Asker8 收到回信后接着说")


async def test_bad_args_no_card(llm, events, boss, make_bot):
    make_bot("Taken7")
    llm.script("router:Boss", Call("create_bot", {"name": "taken7", "instructions": "x"}),
               Call("create_bot", {"name": "Empty7", "instructions": "  "}),
               Call("create_bot", {"name": "Bad7", "instructions": "x", "skills": [{"name": "有 空格", "body": "x"}]}), "好")
    await server.safe_handle(bots.chat(boss["chat_id"]), "建几个 Bot")
    await settle()
    out = tool_outputs(llm.bodies("router", "Boss")[-1])
    assert "已经有叫 Taken7 的 Bot" in out[0] and "职责是空的" in out[1] and "技能 有 空格" in out[2]
    assert not card() and not bots.by_name("Empty7") and not bots.by_name("Bad7")


async def test_missing_bot_means_create_it(llm, events, boss, make_bot):
    """记忆里说有、列表里没有的 Bot 就是没建成：私信它时告诉主 Bot 用 create_bot 建，别的 Bot 去找主 Bot。"""
    other = make_bot("Helper9")
    llm.script("router:Boss", Call("message_bot", {"to": "Quark", "text": "在吗"}), "它还没建，我来建",
               Call("message_bot", {"to": "Boss", "text": "自己"}), "好")
    llm.script("router:Helper9", Call("message_bot", {"to": "Quark", "text": "在吗"}), "好")

    await server.safe_handle(bots.chat(boss["chat_id"]), "让 Quark 找本书")
    out = tool_outputs(llm.bodies("router", "Boss")[-1])[-1]
    assert "没有叫 Quark" in out and "create_bot" in out and "设置" in out
    assert "以这里为准" in system_of(llm.bodies("router", "Boss")[-1])   # 提示词：Bot 列表比记忆可信

    await server.safe_handle(bots.chat(boss["chat_id"]), "再试")
    assert tool_outputs(llm.bodies("router", "Boss")[-1])[-1] == "没发：这是你自己"

    await server.safe_handle(bots.chat(other["chat_id"]), "让 Quark 找本书")
    out = tool_outputs(llm.bodies("router", "Helper9")[-1])[-1]
    assert "没有叫 Quark" in out and "Boss" in out and "create_bot" not in out


async def test_get_bot(llm, events, boss, make_bot):
    """查看不用确认：不填名字是概况，填名字是完整资料，填 skill 是这个技能的全文。"""
    w = make_bot("Viewer9", "负责写周报。\n每周五交。")
    skills.save(w["id"], "weekly", "1. 收集本周进展\n2. 写成周报" + "很长" * 2000, "写周报", "manual")
    routines.save(w["id"], {"name": "周五提醒", "prompt": "提醒写周报", "trigger": {"kind": "daily", "times": ["16:00"], "days": [5]}})
    memories.add(w["id"], "用户喜欢简短的周报")
    g = bots.create_group([boss["id"], w["id"]], "周报组")
    llm.script("router:Boss", Call("get_bot", {}), Call("get_bot", {"name": "viewer9"}),
               Call("get_bot", {"name": "Viewer9", "skill": "/weekly"}), Call("get_bot", {"name": "Ghost9"}), "看完了")
    await server.safe_handle(bots.chat(boss["chat_id"]), "看看 Viewer9")
    await settle()
    overview, detail, skill, missing = tool_outputs(llm.bodies("router", "Boss")[-1])
    assert "个 Bot" in overview and "Boss（主 Bot，就是你）" in overview
    assert "Viewer9：负责写周报。 每周五交。" in overview and "技能 1 个" in overview and "在 1 个群里" in overview
    assert "负责写周报。\n每周五交。" in detail and "/weekly：写周报" in detail and "1. 收集本周进展" in detail
    assert "后面还有" in detail and "skill 参数" in detail                    # 太长的做法截断，告诉它怎么看全文
    assert "周五提醒" in detail and "「周报组」" in detail and "它自己记了 1 条" in detail
    assert "用户喜欢简短的周报" not in detail                                    # 别的 Bot 的记忆内容不给看
    assert skill.endswith("很长" * 2000) and "它自己的" in skill
    assert "没有叫 Ghost9 的 Bot" in missing and "Viewer9" in missing
    assert not approvals.pending
    bots.delete_group(g["id"])


async def test_update_after_approval(llm, events, boss, make_bot):
    """修改：卡片上是改前改后（职责按句子比出改动），同意才改；名字、私聊标题、职责、头像、技能一起改好，主 Bot 被叫醒。"""
    w = make_bot("Writer9", "负责写周报。每周五下午交。语气正式。")
    skills.save(w["id"], "draft", "旧的写法", "起草", "manual")
    skills.save(w["id"], "old", "不用了", "", "manual")
    llm.script("router:Boss", Call("update_bot", {
        "name": "writer9", "new_name": "Scribe9", "instructions": "负责写周报。每周五上午交。语气正式。",
        "avatar": {"shape": "star", "color": "#4FC3A1"},
        "skills": [{"name": "draft", "body": "新的写法"}, {"name": "fresh", "description": "新技能", "body": "做法"}],
        "remove_skills": ["/old"]}), "请确认", "改好了")
    await server.safe_handle(bots.chat(boss["chat_id"]), "Writer9 改成周五上午交，改名 Scribe9")
    assert "修改 Bot「Writer9」" in tool_outputs(llm.bodies("router", "Boss")[-1])[0]
    c = await until(lambda: card("update_bot"), what="确认卡片")
    d = __import__("json").loads(c["args"])
    assert c["once"] and c["chat_id"] == boss["chat_id"]
    assert d["Bot"] == "Writer9" and d["新名字"] == "Scribe9" and d["头像"] == {"shape": "star", "color": "#4fc3a1"}
    assert d["职责改动"] == [[0, "负责写周报。"], [-1, "每周五下午交。"], [1, "每周五上午交。"], [0, "语气正式。"]]
    assert d["新技能"] == ["/fresh：新技能"] and d["改技能"] == ["/draft：新的写法"] and d["删技能"] == ["/old"]
    assert bots.get(w["id"])["name"] == "Writer9"   # 没确认之前不改

    approvals.resolve(c["id"], True, always=True)
    msg = await until(lambda: find(boss["chat_id"], kind="bot_updated"), what="改好的消息")
    b = bots.get(w["id"])
    assert b["name"] == "Scribe9" and bots.chat(b["chat_id"])["title"] == "Scribe9"
    assert b["instructions"] == "负责写周报。每周五上午交。语气正式。" and b["avatar"]["shape"] == "star"
    own = {s["name"]: s["body"] for s in skills.list_for(w["id"]) if s["bot_id"] == w["id"]}
    assert own == {"draft": "新的写法", "fresh": "做法"}
    assert msg[0]["meta"]["bot"] == w["id"] and "Scribe9" in msg[0]["content"] and "职责" in msg[0]["content"]
    assert events.of("bots_changed") and events.of("skills_changed")
    assert not [r for r in approvals.rules() if r["pattern"] == "update_bot"]
    await until(lambda: find(boss["chat_id"], role="assistant", text="改好了"), what="主 Bot 被叫醒")


async def test_update_checks(llm, events, boss, make_bot):
    """参数不对不弹卡片：没这个 Bot、没要改的、重名、删别人的或共用的技能、职责太长。"""
    w = make_bot("Check9", "原来的职责")
    make_bot("Taken9")
    skills.save(None, "shared9", "共用的", "", "manual")
    llm.script("router:Boss", Call("update_bot", {"name": "Ghost9", "instructions": "x"}),
               Call("update_bot", {"name": "Check9", "instructions": "原来的职责", "new_name": "Check9"}),
               Call("update_bot", {"name": "Check9", "new_name": "taken9"}),
               Call("update_bot", {"name": "Check9", "remove_skills": ["shared9"]}),
               Call("update_bot", {"name": "Check9", "instructions": "长" * 5000}), "好")
    await server.safe_handle(bots.chat(boss["chat_id"]), "改一下")
    await settle()
    out = tool_outputs(llm.bodies("router", "Boss")[-1])
    assert out[0].startswith("没改：没有叫 Ghost9 的 Bot") and "没有要改的" in out[1] and "已经有叫 Taken9 的 Bot" in out[2]
    assert "没有自己的技能 /shared9" in out[3] and "职责太长了" in out[4]
    assert not card("update_bot") and bots.get(w["id"])["instructions"] == "原来的职责"
    memory.conn.execute("delete from skills where name='shared9'")
    memory.conn.commit()


async def test_other_bot_updates_only_itself(llm, events, boss, make_bot):
    """别的 Bot 只能改自己：卡片在它自己的私聊里；改别人被拒，让它找主 Bot。"""
    me, other = make_bot("Self9", "旧职责"), make_bot("Other9", "别动我")
    llm.script("router:Self9", Call("update_bot", {"name": "Other9", "instructions": "改掉"}),
               Call("update_bot", {"name": "Self9", "instructions": "新职责"}), "请确认", "改好了")
    await server.safe_handle(bots.chat(me["chat_id"]), "把你的职责改成新职责")
    out = tool_outputs(llm.bodies("router", "Self9")[-1])
    assert "你只能改你自己" in out[0] and "Boss" in out[0] and out[1].startswith("已弹出确认卡片")
    c = await until(lambda: card("update_bot"), what="确认卡片")
    assert c["chat_id"] == me["chat_id"] and c["bot_id"] == me["id"]
    approvals.resolve(c["id"], True)
    await until(lambda: find(me["chat_id"], kind="bot_updated"), what="改好的消息")
    assert bots.get(me["id"])["instructions"] == "新职责" and bots.get(other["id"])["instructions"] == "别动我"
    await settle()


async def test_delete_after_approval(llm, events, boss, make_bot):
    """删除：卡片上写着原因和会一起删掉什么；同意后私聊、技能、定时任务、它的记忆都删掉，退出群聊，主 Bot 被叫醒。"""
    w, k = make_bot("Gone9", "要删的"), make_bot("Keep9")
    skills.save(w["id"], "gone", "做法", "", "manual")
    routines.save(w["id"], {"name": "每日", "prompt": "x", "trigger": {"kind": "daily", "times": ["08:00"]}})
    memories.add(w["id"], "只有它记得")
    shared = memories.add(None, "用户住在上海")
    g = bots.create_group([boss["id"], w["id"], k["id"]], "三人组", lead=w["id"])
    llm.script("router:Boss", Call("delete_bot", {"name": "Gone9", "reason": "用户说不用了"}), "请确认", "删掉了")
    await server.safe_handle(bots.chat(boss["chat_id"]), "把 Gone9 删了")
    c = await until(lambda: card("delete_bot"), what="确认卡片")
    d = __import__("json").loads(c["args"])
    assert c["once"] and d["Bot"] == "Gone9" and d["原因"] == "用户说不用了"
    assert d["会删掉"] == {"消息": 0, "技能": 1, "定时任务和事件": 1, "记忆": 1} and d["群聊"] == ["三人组"] and d["在做的任务"] == 0
    assert bots.get(w["id"])

    approvals.resolve(c["id"], True)
    msg = await until(lambda: find(boss["chat_id"], kind="bot_deleted"), what="删掉的消息")
    assert "Gone9" in msg[0]["content"] and not bots.get(w["id"]) and not bots.chat(w["chat_id"])
    assert not [s for s in skills.list_for(k["id"]) if s["bot_id"] == w["id"]]
    assert not routines.list_for(w["id"]) and not memories.list_for(w["id"])["own"] and memories.get(shared["id"])
    grp = bots.chat(g["id"])
    assert grp["bot_ids"] == [boss["id"], k["id"]] and grp["lead"] == boss["id"]
    await until(lambda: find(boss["chat_id"], role="assistant", text="删掉了"), what="主 Bot 被叫醒")
    memories.delete(shared["id"])
    bots.delete_group(g["id"])


async def test_delete_checks(llm, events, boss, make_bot):
    other = make_bot("Asker9")
    llm.script("router:Boss", Call("delete_bot", {"name": "Boss"}), Call("delete_bot", {"name": "Ghost9"}), "好")
    llm.script("router:Asker9", Call("delete_bot", {"name": "Boss"}), "好")
    await server.safe_handle(bots.chat(boss["chat_id"]), "删")
    await server.safe_handle(bots.chat(other["chat_id"]), "删")
    await settle()
    out = tool_outputs(llm.bodies("router", "Boss")[-1])
    assert "主 Bot 不能删除" in out[0] and "没有叫 Ghost9" in out[1]
    assert "只有主 Bot（Boss）能删除 Bot" in tool_outputs(llm.bodies("router", "Asker9")[-1])[0]
    assert not card("delete_bot") and bots.get(boss["id"])


async def test_target_deleted_while_waiting(llm, events, boss, make_bot):
    """等确认的时候要改的 Bot 被删了：同意后再检查一遍，记一笔没改成，叫醒主 Bot 说明。"""
    w = make_bot("Flaky9", "x")
    llm.script("router:Boss", Call("update_bot", {"name": "Flaky9", "instructions": "y"}), "请确认", "它已经没了")
    await server.safe_handle(bots.chat(boss["chat_id"]), "改 Flaky9")
    c = await until(lambda: card("update_bot"), what="确认卡片")
    await botadmin.remove(w["id"])   # 用户在设置里删了它
    approvals.resolve(c["id"], True)
    note = await until(lambda: find(boss["chat_id"], kind="note", text="没改成 Bot「Flaky9」"), what="没改成的记录")
    assert "没有叫 Flaky9" in note[0]["content"]
    await until(lambda: find(boss["chat_id"], role="assistant", text="它已经没了"), what="主 Bot 被叫醒")


async def test_deleting_a_bot_drops_its_cards(llm, events, boss, make_bot):
    """Bot 被删了，它还在等的卡片按拒绝处理，不再往已经删掉的私聊里记东西。"""
    me = make_bot("Doomed9", "x")
    llm.script("router:Doomed9", Call("update_bot", {"name": "Doomed9", "instructions": "y"}), "请确认")
    await server.safe_handle(bots.chat(me["chat_id"]), "改你自己")
    c = await until(lambda: card("update_bot"), what="确认卡片")
    await server.delete_bot(me["id"])   # 网页上删也走 botadmin.remove
    await settle()
    assert c["id"] not in approvals.pending and not find(me["chat_id"])
    assert [e for e in events.of("approval") if e.get("id") == c["id"] and e["status"] == "cancelled"]


def test_deleted_bot_ids_are_not_reused(make_bot):
    """删掉最新的 Bot 再建一个，新 Bot 的编号和它不一样：它留在群里的发言、资料库里的页面不会算到新 Bot 头上。"""
    a = make_bot()
    bots.delete(a["id"])
    b = make_bot()
    assert b["id"] > a["id"]
    bots.delete(b["id"])


def test_cards_in_chat_apps_and_push(boss):
    """聊天软件里的卡片和手机推送：修改写改了什么，删除写会一起删掉什么、不能恢复；都只能一次一次地允许。"""
    import json
    from osa import channels, push
    up = {"id": 1, "tool": "update_bot", "bot_id": boss["id"], "chat_id": boss["chat_id"], "args": json.dumps(
        {"Bot": "Writer", "编号": 2, "新名字": "Scribe", "职责": "新职责", "删技能": ["/old"], "请求方": "Asker"}, ensure_ascii=False)}
    c = channels.card_for(up)
    assert "想修改 Bot「Writer」" in c["title"] and "Scribe" in c["body"] and "新职责" in c["body"] and "/old" in c["body"]
    assert "Asker 私信请它做的" in c["body"] and "always" not in [b[1] for b in c["buttons"]]
    rm = {**up, "id": 2, "tool": "delete_bot", "args": json.dumps(
        {"Bot": "Gone", "编号": 3, "原因": "不用了", "会删掉": {"消息": 3, "技能": 1, "定时任务": 0, "记忆": 0},
         "群聊": ["三人组"], "在做的任务": 1}, ensure_ascii=False)}
    c = channels.card_for(rm)
    assert "想删除 Bot「Gone」" in c["title"] and "不用了" in c["body"] and "私聊里的 3 条消息、1 个技能" in c["body"]
    assert "定时任务" not in c["body"] and "「三人组」" in c["body"] and "1 个任务会停掉" in c["body"] and "不能恢复" in c["body"]
    title, body, *_ = push.message_for({"type": "approval", "status": "pending", **rm})
    assert "想删除一个 Bot" in title and body == "Gone"
    assert channels.render({"role": "task", "content": "[已删除 Bot「Gone」]", "meta": {"kind": "bot_deleted"}}, False) == "🤖 已删除 Bot「Gone」"


async def test_edit_role_on_card(llm, events, boss):
    """新建卡片上的职责能直接改：按改过的建，主 Bot 醒来知道用户改过。只认卡片允许改的、改成空的不算。"""
    cid = boss["chat_id"]
    llm.script("router:Boss", Call("create_bot", {"name": "Edit9", "instructions": "原来的职责"}), "请确认", "建好了")
    await server.safe_handle(bots.chat(cid), "建个 Edit9")
    c = await until(card, what="确认卡片")
    assert c["editable"] == ["职责"] and "_args" not in approvals.public(c)
    approvals.resolve(c["id"], True, edits={"职责": "  用户改过的职责  ", "名字": "Hacked9"})
    await until(lambda: find(cid, kind="bot_created"), what="建好的消息")
    b = bots.by_name("Edit9")
    assert b and b["instructions"] == "用户改过的职责" and not bots.by_name("Hacked9")
    assert [e for e in events.of("approval") if e.get("id") == c["id"] and e.get("edited") == ["职责"]]
    await until(lambda: len(llm.bodies("router", "Boss")) == 3, what="主 Bot 被叫醒")
    assert "用户在卡片上改过职责" in last_user(llm.bodies("router", "Boss")[2])
    await settle()


async def test_edit_role_on_update_card(llm, events, boss, make_bot):
    w = make_bot("Upd9", "旧职责。")
    llm.script("router:Boss", Call("update_bot", {"name": "Upd9", "instructions": "模型写的新职责。"}),
               Call("update_bot", {"name": "Upd9", "instructions": "第二次。"}), "请确认")
    await server.safe_handle(bots.chat(boss["chat_id"]), "改两次")
    await until(lambda: len([x for x in approvals.pending.values() if x["tool"] == "update_bot"]) == 2, what="两张卡片")
    first, second = sorted((x for x in approvals.pending.values() if x["tool"] == "update_bot"), key=lambda x: x["id"])
    approvals.resolve(second["id"], False, edits={"职责": "拒绝时改的不算"})
    await until(lambda: find(boss["chat_id"], kind="note", text="没修改 Bot「Upd9」"), what="没改的记录")
    approvals.resolve(first["id"], True, edits={"职责": "   "})   # 改成空的：当没改
    await until(lambda: find(boss["chat_id"], kind="bot_updated"), what="改好的消息")
    assert bots.get(w["id"])["instructions"] == "模型写的新职责。"
    await settle()

    llm.script("router:Boss", Call("update_bot", {"name": "Upd9", "instructions": "再改一次。"}), "请确认")
    await server.safe_handle(bots.chat(boss["chat_id"]), "再改")
    c = await until(lambda: card("update_bot"), what="确认卡片")
    approvals.resolve(c["id"], True, edits={"职责": "用户自己写的。"})
    await until(lambda: bots.get(w["id"])["instructions"] == "用户自己写的。", what="按用户改的改好")
    await settle()


async def test_turn_lists_current_tools(llm, events, boss, make_bot):
    """每一轮的状态里写着这一轮能用的工具：以前在对话里说过“删不了”，升级后照着当前的工具做（生产上 Nova 照旧话拒绝过）。"""
    other = make_bot("Plain9")
    await server.safe_handle(bots.chat(boss["chat_id"]), "把小白删了")
    await server.safe_handle(bots.chat(other["chat_id"]), "你好")
    await settle()
    mine, theirs = last_user(llm.bodies("router", "Boss")[-1]), last_user(llm.bodies("router", "Plain9")[-1])
    assert "这一轮你能用的工具" in mine and "delete_bot" in mine and "以前说过“做不到”" in mine
    assert "update_bot" in theirs and "delete_bot" not in theirs
    assert "以这一轮给你的工具为准" in system_of(llm.bodies("router", "Boss")[-1])
