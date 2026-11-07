"""置顶：置顶的私聊、群聊在侧栏上面那排，按置顶先后排；下面的列表一律按最新消息排，置顶的也一样。"""
import pytest
from fastapi import HTTPException
from osa import bots, memory, server


async def test_pin_keeps_list_by_time(events, make_bot):
    a, b = make_bot(), make_bot()
    group = bots.create_group([a["id"], b["id"]])
    memory.add("user", "早一点的消息", a["chat_id"])
    memory.add("user", "晚一点的消息", b["chat_id"])

    first = await server.pin_chat(group["id"], {"pinned": True})
    second = await server.pin_chat(a["chat_id"], {"pinned": True})
    assert 0 < first["pinned"] < second["pinned"]
    assert events.of("bots_changed")

    # 置顶不改变列表顺序：b 最新，排在置顶的 a 前面
    order = [c["id"] for c in bots.list_chats()]
    assert order.index(b["chat_id"]) < order.index(a["chat_id"])

    # 再置顶一次不改置顶时间（位置不跳）；取消后归零
    assert (await server.pin_chat(group["id"], {"pinned": True}))["pinned"] == first["pinned"]
    assert (await server.pin_chat(group["id"], {"pinned": False}))["pinned"] == 0
    assert bots.chat(a["chat_id"])["pinned"] == second["pinned"]


async def test_cannot_pin_thread(events, make_bot):
    a = make_bot()
    mid = memory.add("user", "你好", a["chat_id"])
    th = bots.thread(a["chat_id"], mid)
    with pytest.raises(HTTPException):
        await server.pin_chat(th["id"], {"pinned": True})
    with pytest.raises(HTTPException):
        await server.pin_chat(999999, {"pinned": True})
