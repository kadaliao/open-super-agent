"""群聊：2–6 个 Bot 在同一个对话里。这里决定每条消息轮到谁说。

- 消息里 @ 了谁就轮到谁，按 @ 出现的顺序；@everyone（或 @所有人）是所有成员按顺序。
- 没 @ 任何人：由协调者（chats.lead）接，它可以自己答，也可以用 handoff 分给别人。
- 轮到的 Bot 用 handoff 把工作交给别人时，对方在它说完后马上接着说。
- dispatch 时填了 handoff_to 的，后台任务做完自动交接；在这之前，同一轮里点名给对方的发言先跳过，免得它空等着表态。
- 一轮（用户的一条消息，或一次任务交接）最多 MAX_TURNS 次发言，防止 Bot 之间来回停不下来。
- 确认卡片和“请你帮忙”都发到 Bot 的私聊（见 approvals），群里不出现。
- 群里的 Bot 私信了群外的 Bot（见 dm.py），回信回到群里时，它按 "dm" 排进来接着说。
"""
import asyncio
import logging
import re
import time
from . import bots, i18n, memory, providers

MAX_TURNS = 8
EVERYONE = ("everyone", "所有人", "全体成员")
notify = None   # 由 server 设置：async (event) -> None
log = logging.getLogger(__name__)


class Group:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.lock = asyncio.Lock()          # 同一个群一次只跑一轮
        self.handoffs: list[int] = []       # 当前发言的 Bot 交出去的工作，它说完就轮到这些 Bot
        self.expected: dict[int, int] = {}  # bot_id -> 有几个任务做完后会交给它

    async def run(self, first: list[tuple[int, str]], turn: dict | None = None):
        """依次让 Bot 发言。first 是 [(bot_id, 原因)]，原因：mention / lead / handoff / dm。
        turn 是私信引起的这一轮的来由（见 dm.py），只给原因是 dm 的那一轮；私信深度这一串都沿用。"""
        turn = turn or {}
        queue, turns = list(first), 0
        while queue:
            bot_id, why = queue.pop(0)
            chat = bots.chat(self.chat_id)
            if not chat or bot_id not in chat["bot_ids"]:
                continue
            if why == "mention" and self.expected.get(bot_id):
                continue   # 它要等别人的任务结果，交接时再说
            if turns >= MAX_TURNS:
                await note(self.chat_id, i18n.t("Bot 们这一轮已经说了 {n} 次，先停一下，等你发话。", n=MAX_TURNS))
                break
            turns += 1
            self.handoffs = []
            from . import router
            try:
                await router.runtime(bot_id).respond(self.chat_id, turn if why == "dm" else {"depth": turn.get("depth", 0)})
            except Exception as e:
                log.exception("群聊 %s 里 Bot %s 出错", self.chat_id, bot_id)
                if notify:
                    await notify({"type": "error", "chat_id": self.chat_id,
                                  "message": i18n.t("{name} 出错了：{e}", name=_name(bot_id), e=providers.brief(e))})
            # 交出去的工作排在最前面，按交接的先后
            queue = [(b, "handoff") for b in self.handoffs] + [q for q in queue if q[0] not in self.handoffs]
        self.handoffs = []


_groups: dict[int, Group] = {}


def get(chat_id: int) -> Group:
    if chat_id not in _groups:
        _groups[chat_id] = Group(chat_id)
    return _groups[chat_id]


def _name(bot_id: int) -> str:
    b = bots.get(bot_id)
    return b["name"] if b else i18n.t("已删除的 Bot")


def mentions(text: str, member_ids: list[int]) -> list[int]:
    """消息里 @ 到的成员，按出现顺序。名字可以带空格和中文，长名字优先匹配。"""
    low = text.lower()
    if any(f"@{w}" in low for w in EVERYONE):
        return list(member_ids)
    names = sorted(((b["name"].lower(), b["id"]) for i in member_ids if (b := bots.get(i))), key=lambda x: -len(x[0]))
    taken, found = [], []
    for name, bid in names:
        for m in re.finditer("@" + re.escape(name), low):
            a, z = m.span()
            if any(x < z and a < y for x, y in taken):
                continue   # 已经被更长的名字占了
            if z < len(low) and low[z].isascii() and low[z].isalnum() and name[-1:].isascii():
                continue   # @Nova2 不算 @Nova
            taken.append((a, z))
            found.append((a, bid))
            break
    return [bid for _, bid in sorted(found)]


async def _message(chat_id: int, role: str, content: str, bot_id: int | None = None, **meta) -> int:
    mid = memory.add(role, content, chat_id, bot_id, **meta)
    if notify:
        await notify({"type": "message", "chat_id": chat_id, "bot_id": bot_id, "message": {
            "id": mid, "ts": time.time(), "role": role, "content": content, "meta": meta, "bot_id": bot_id}})
    return mid


async def note(chat_id: int, text: str):
    await _message(chat_id, "task", f"[{text}]", None, kind="note")


async def on_user(chat: dict, text: str, via: str = "", attachments: list | None = None):
    """用户在群里发了一条消息。via：从哪个聊天软件发来的（网页上发的为空）。attachments：附件（见 media.py）。"""
    await _message(chat["id"], "user", text, **({"via": via} if via else {}), **({"attachments": attachments} if attachments else {}))
    g = get(chat["id"])
    async with g.lock:
        chat = bots.chat(chat["id"]) or chat
        if not chat["bot_ids"]:
            await note(chat["id"], i18n.t("群里没有 Bot 了，先在右栏添加成员"))
            return
        hit = mentions(text, chat["bot_ids"])
        # 讨论串里没 @ 人：由被回复的那条消息的作者接；回复的是用户自己的消息，还是协调者接
        lead = chat.get("root_bot") or (chat["lead"] if chat["lead"] in chat["bot_ids"] else chat["bot_ids"][0])
        await g.run([(b, "mention") for b in hit] or [(lead, "lead")])


async def handoff(chat_id: int, from_id: int, to_id: int, request: str):
    """from 在发言中把工作交给 to：群里出一条交接记录，from 说完后轮到 to。"""
    await _message(chat_id, "task", i18n.t("→ {name}：{text}", name=_name(to_id), text=request), from_id,
                   kind="handoff", to=to_id, note=request)
    g = get(chat_id)
    if to_id not in g.handoffs:
        g.handoffs.append(to_id)


def expect(chat_id: int, to_id: int):
    g = get(chat_id)
    g.expected[to_id] = g.expected.get(to_id, 0) + 1


def unexpect(chat_id: int, to_id: int):
    g = get(chat_id)
    if g.expected.get(to_id):
        g.expected[to_id] -= 1


async def after_task(task):
    """填了 handoff_to 的任务结束了：做完就交接，失败就说一声。"""
    h = task.handoff
    unexpect(task.chat_id, h["to"])
    if task.status != "done":
        await note(task.chat_id, i18n.t("任务 #{id} 没做完，没有交给 {name}", id=task.id, name=_name(h["to"])))
        return
    g = get(task.chat_id)
    async with g.lock:
        request = h["note"] or i18n.t("资料在上面，请接着做")
        await _message(task.chat_id, "task", i18n.t("→ {name}：{text}（任务 #{id} 的结果）", name=_name(h["to"]), text=request, id=task.id),
                       task.bot_id, kind="handoff", to=h["to"], note=i18n.t("任务 #{id} 的结果在上面。{text}", id=task.id, text=request),
                       task_id=task.id)
        await g.run([(h["to"], "handoff")], {"depth": task.depth})


async def wake(chat_id: int, bot_id: int, turn: dict):
    """群里的 Bot 收到了私信的回信：等群里这一轮说完，让它接着说。"""
    g = get(chat_id)
    async with g.lock:
        await g.run([(bot_id, "dm")], turn)


def forget(chat_id: int):
    _groups.pop(chat_id, None)
