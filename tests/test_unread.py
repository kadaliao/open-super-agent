"""主对话的未读：存在服务端（chats.seen），刷新、断线重连、手机 App 重新打开都还在。"""
import pytest
from fastapi import HTTPException
from conftest import settle
from osa import bots, memory, server


@pytest.fixture
def make_bot(make_bot, monkeypatch):
    """整套测试共用一个数据库，跑到这里 Bot 已经到上限了：这里放开上限，用完就删，不给后面的测试添数。"""
    monkeypatch.setattr(bots, "MAX_BOTS", 10 ** 6)
    made = []

    def make(name: str = "") -> dict:
        made.append(make_bot(name))
        return made[-1]
    yield make
    for b in made:
        bots.delete(b["id"])


def _unread(chat_id: int) -> int:
    return next(c["unread"] for c in bots.list_chats() if c["id"] == chat_id)


async def test_bot_reply_is_unread_until_seen(llm, events, make_bot):
    a = make_bot("Pinger")
    cid = a["chat_id"]
    llm.script("router:Pinger", "第一条回复", "第二条回复")
    assert _unread(cid) == 0

    await server.safe_handle(bots.chat(cid), "在吗")
    await settle()
    # 你自己发的不算，Bot 的回复算。重新打开页面时还在：bootstrap 的 chats 就是 list_chats
    assert _unread(cid) == 1 and bots.chat(cid)["unread"] == 1
    # 消息事件带着服务端算好的未读数，侧栏的数字跟着它走
    assert [e for e in events.of("message") if e["chat_id"] == cid][-1]["unread"] == 1

    first = memory.conn.execute("select max(id) from messages where chat_id=?", (cid,)).fetchone()[0]
    await server.safe_handle(bots.chat(cid), "再说一句")
    await settle()
    assert _unread(cid) == 2

    # 只看到第一条回复为止：后面那条还是未读；seen 不会往回退
    assert (await server.chat_seen(cid, {"id": first})) == {"unread": 1}
    assert events.of("chat_seen")[-1] == {"type": "chat_seen", "chat_id": cid, "unread": 1}
    assert bots.mark_chat_seen(cid, 1) == 1
    assert (await server.chat_seen(cid, {})) == {"unread": 0}   # 不给 id：全部看过
    assert _unread(cid) == 0 and events.of("chat_seen")[-1]["unread"] == 0


async def test_records_do_not_count(make_bot):
    a = make_bot()
    cid = a["chat_id"]
    memory.add("task", "[任务已派发]", cid, a["id"], kind="dispatch")   # 派任务的记录不是 Bot 对你说的话
    memory.add("assistant", "", cid, a["id"])
    assert _unread(cid) == 0
    memory.add("task", "做完了", cid, a["id"], kind="result", status="done")
    assert _unread(cid) == 1


async def test_thread_replies_stay_on_the_thread(llm, events, make_bot):
    a = make_bot("Sider")
    cid = a["chat_id"]
    llm.script("router:Sider", "主对话的回复", "讨论串里的回复")
    await server.safe_handle(bots.chat(cid), "你好")
    await settle()
    root = memory.conn.execute("select max(id) from messages where chat_id=?", (cid,)).fetchone()[0]
    bots.mark_chat_seen(cid)

    th = bots.thread(cid, root)
    await server.safe_handle(bots.chat(th["id"]), "展开说说")
    await settle()
    assert _unread(cid) == 0                                           # 讨论串的新回复不算进主对话的未读
    assert bots.thread_summaries(cid, [root])[root]["unread"] == 1     # 它有自己的
    assert "unread" not in [e for e in events.of("message") if e["chat_id"] == th["id"]][-1]
    # 两个接口各管各的：讨论串不能当对话标，对话也不能当讨论串标
    assert bots.mark_chat_seen(th["id"]) is None and bots.mark_seen(cid) is None
    with pytest.raises(HTTPException):
        await server.chat_seen(th["id"], {})


async def test_upgrade_counts_old_messages_as_seen(make_bot):
    a, b = make_bot(), make_bot()
    for bot in (a, b):
        memory.add("assistant", "升级前的回复", bot["chat_id"], bot["id"])
    # 升级前的样子：主对话的 seen 是空的，也没跑过初始化
    memory.conn.execute("update chats set seen=null where id in (?, ?)", (a["chat_id"], b["chat_id"]))
    memory.conn.execute("delete from settings where key='chat_seen_init'")
    bots._init_seen()
    assert _unread(a["chat_id"]) == 0 and _unread(b["chat_id"]) == 0

    # 只做一次：之后来的消息、之后新建的对话，重启也不会被当成看过
    memory.add("assistant", "升级后的回复", a["chat_id"], a["id"])
    c = make_bot()
    memory.add("assistant", "新 Bot 的第一句", c["chat_id"], c["id"])
    bots._init_seen()
    assert _unread(a["chat_id"]) == 1 and _unread(c["chat_id"]) == 1
