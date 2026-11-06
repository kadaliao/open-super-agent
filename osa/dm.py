"""Bot 之间的私信：一个 Bot 直接请另一个 Bot 帮忙，不用拉群。

- A 调 message_bot：A 所在的对话里记一条“A 私信 B”，B 的私聊里出现同样的来信，B 醒来在自己的私聊里处理。
- 回信自动送回，不靠模型记得回：因来信而起的一轮带着 reply（谁在等、在哪个对话里等）。
  · 这一轮没派任务、也没再私信别人：B 这一轮的回复就是回信。
  · 派了任务：任务带上 reply，做完把结果作为回信送回（失败、取消也说一声）。
  · 又私信了 C：C 的回信回到 B 时把 reply 还给 B，B 那一轮的回复再送回 A。
- 收到回信的 Bot 在原来的对话里接着说一轮，告诉用户结果；那个对话是群聊的话按群的规矩排队。
- 深度：用户的消息引起的私信是 1，因私信或回信而起的一轮再发私信就加 1，超过 MAX_DEPTH 拒绝，免得 Bot 之间来回停不下来。
- 同一个 Bot 在同一个对话里已经排着一轮还没开始，又来了同样来由的私信或回信（比如派的两个任务前后脚做完），
  不再多排一轮：那一轮开始时会一起看到。
- 等回信只记在内存里：服务重启后，还没回的私信不会再回（任务会被标成“已中断”）。

reply 的样子：{"bot": 等回信的 Bot, "chat": 它在哪个对话里等, "depth": 深度, "then": 它那一轮自己欠谁回信（没有为 None）}
"""
import asyncio
import time
from . import bots, i18n, memory

MAX_DEPTH = 5
notify = None   # 由 server 设置：async (event) -> None
_queued: dict[tuple, dict] = {}   # 排着队还没开始的一轮：(Bot, 对话, 来由, 欠谁回信) -> turn


def _name(bot_id: int | None) -> str:
    b = bots.get(bot_id) if bot_id else None
    return b["name"] if b else i18n.t("已删除的 Bot")


async def _message(chat_id: int, content: str, bot_id: int, **meta) -> int:
    mid = memory.add("task", content, chat_id, bot_id, **meta)
    if notify:
        await notify({"type": "message", "chat_id": chat_id, "bot_id": bot_id, "message": {
            "id": mid, "ts": time.time(), "role": "task", "content": content, "meta": meta, "bot_id": bot_id}})
    return mid


async def send(sender: int, to: dict, text: str, origin: int, owed: dict | None, depth: int) -> str:
    """sender 在 origin 对话里私信 to。owed 是 sender 这一轮欠谁回信，depth 是这一轮的深度。返回给模型看的结果。"""
    text = (text or "").strip()
    if not text:
        return "没发：私信内容是空的"
    if depth + 1 > MAX_DEPTH:
        return f"没发：Bot 之间已经转了 {MAX_DEPTH} 手，先停下来，把现在的情况告诉用户，等用户决定"
    reply = {"bot": sender, "chat": origin, "depth": depth + 1, "then": owed}
    await _message(origin, text, sender, kind="dm", to=to["id"])
    await _message(to["chat_id"], text, sender, kind="dm", to=to["id"], chat=origin)   # chat：从哪个对话发来的
    asyncio.create_task(_wake(to["id"], to["chat_id"], {"reply": reply, "depth": depth + 1,
                                                       "woke": {"kind": "dm", "from": sender, "text": text}}))
    return f"已私信 {to['name']}。它的回信会自动回到这个对话，到时你会接着说一轮，现在不用等"


async def answer(sender: int, reply: dict, text: str, from_chat: int, **meta):
    """sender 把回信送回 reply 指的 Bot。from_chat 是 sender 写回信的对话（它的私聊），在那里留一条“已回复”。"""
    to = bots.get(reply["bot"])
    if not to:
        await _message(from_chat, i18n.t("[等回信的 Bot 已经删除，回信没有送出]"), sender, kind="note")
        return
    chat = bots.chat(reply["chat"])
    cid = reply["chat"] if chat and (chat["kind"] != "group" or to["id"] in chat["bot_ids"]) else to["chat_id"]
    await _message(from_chat, i18n.t("[已回复 {name}]", name=to["name"]), sender, kind="dm_reply", to=to["id"], sent=True, chat=cid)
    await _message(cid, text, sender, kind="dm_reply", to=to["id"], **meta)
    asyncio.create_task(_wake(to["id"], cid, {"reply": reply["then"], "depth": reply["depth"],
                                             "woke": {"kind": "reply", "from": sender, "text": text}}))


async def after_task(task):
    """因私信派的任务结束了：结果作为回信送回去；没做完也说一声。"""
    if task.status == "done":
        text = task.result   # 给模型看时 memory 会标上是哪个任务的结果
    else:   # result 里是“任务失败：……”或“已取消：谁取消的”
        text = i18n.t("（任务 #{id} 没做完。{result}）", id=task.id, result=task.result or task.status)
    await answer(task.bot_id, task.reply, text, task.chat_id, task_id=task.id, status=task.status)


def _owed(reply: dict | None):
    return reply and (reply["bot"], reply["chat"], _owed(reply["then"]))


def same(a: dict | None, b: dict | None) -> bool:
    """两个回信去向是不是同一个（同一个 Bot、同一个对话、往后也一样）。"""
    return _owed(a) == _owed(b)


async def _wake(bot_id: int, chat_id: int, turn: dict):
    """让 bot_id 在 chat_id 里说一轮。turn 是这一轮的来由：{"reply", "depth", "woke"}。"""
    from . import groups, router
    chat = bots.chat(chat_id)
    if not chat or not bots.get(bot_id):   # 等着的时候 Bot 或对话被删了
        return
    key = (bot_id, chat_id, turn["woke"]["kind"], turn["woke"]["from"], _owed(turn.get("reply")))
    if q := _queued.get(key):
        q["depth"] = max(q["depth"], turn["depth"])
        return
    _queued[key] = turn
    turn["started"] = lambda: _queued.pop(key, None)
    try:
        if chat["kind"] == "group":
            await groups.wake(chat_id, bot_id, turn)
        else:
            await router.runtime(bot_id).respond(chat_id, turn)
    except Exception as e:
        if notify:
            await notify({"type": "error", "chat_id": chat_id, "message": i18n.t("{name} 出错了：{e}", name=_name(bot_id), e=e)})
    finally:
        if _queued.get(key) is turn:   # 群里没轮到它（比如已经不在群里）
            del _queued[key]
