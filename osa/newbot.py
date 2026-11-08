"""主 Bot 新建 Bot：Router 的 create_bot 工具，只有主 Bot 有。

- 每次都先弹确认卡片，卡片上是名字、职责和一起带上的技能；不能“总是允许”。用户同意才建。
- 和画图一样不让 Router 干等：工具马上返回，卡片和新建在后台进行；用户也能在对话里说“建吧”（resolve_approval）。
- 别的 Bot 需要新 Bot 时私信主 Bot（提示词里写着），由主 Bot 调这个工具，所以卡片总在主 Bot 的私聊里。
- 建好后在调用的那个对话里记一条（kind=bot_created，界面上能点过去），侧栏马上出现新 Bot。
- 有了结果（建好、用户没同意、没建成）就叫醒主 Bot 在那个对话里接着说一轮，它才知道结果，
  好去做答应过的事（比如私信新 Bot 交代事情）。用户没同意又没附意见、也没有 Bot 等着的，不叫醒。
  是别的 Bot 私信托它建的，那一轮的回复不当场送回，等有了结果，叫醒的这一轮的回复作为回信送回去（同 dispatch）。
"""
import asyncio
from types import SimpleNamespace
from . import approvals, bots, dm, i18n, skills

TOOL = "create_bot"
MAX_SKILLS = 10
CREATE_BOT = {"type": "function", "function": {
    "name": TOOL,
    "description": "新建一个 Bot。会先弹卡片请用户确认（上面有名字、职责和技能），用户同意后自动建好；"
                   "有了结果（建好、用户没同意、没建成）你会接着说一轮，建好后要做的事（比如私信它交代事情）到时再做。",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "名字，最多 40 个字，不能和现有的 Bot 重名"},
        "instructions": {"type": "string", "description": "它的职责和做事方式，写完整：负责什么、怎么做、有哪些硬性约束。"
                                                          "别的 Bot 也靠这段判断什么事该找它"},
        "skills": {"type": "array", "description": f"可选：一起给它的技能（可复用的做法），最多 {MAX_SKILLS} 个",
                   "items": {"type": "object", "properties": {
                       "name": {"type": "string", "description": "简短的名字，用于 /名字 调用，不含空格"},
                       "description": {"type": "string", "description": "一句话说明什么时候用"},
                       "body": {"type": "string", "description": "完整的做法：步骤、要用的网站或应用、输出格式"}},
                       "required": ["name", "body"]}}},
        "required": ["name", "instructions"]}}}

_running: set[asyncio.Task] = set()   # 后台等确认、新建的，留个引用免得被回收


def _check(name: str, instructions: str, items) -> tuple[str, str, list[dict]]:
    """检查参数，返回 (名字, 职责, 技能)；不行报 ValueError（写给模型看）。"""
    name = (name or "").strip().lstrip("@").strip()
    if not name:
        raise ValueError("名字是空的")
    if len(name) > 40:
        raise ValueError("名字最多 40 个字")
    if bots.by_name(name):
        raise ValueError(f"已经有叫 {name} 的 Bot 了，换个名字，或者直接用 message_bot 找它")
    if len(bots.list_bots()) >= bots.MAX_BOTS:
        raise ValueError(f"已经有 {bots.MAX_BOTS} 个 Bot，到上限了，请用户先删掉不用的")
    instructions = (instructions or "").strip()
    if not instructions:
        raise ValueError("职责是空的：写清楚它负责什么、怎么做")
    if not isinstance(items, list):
        items = []
    if len(items) > MAX_SKILLS:
        raise ValueError(f"技能最多 {MAX_SKILLS} 个")
    out, seen = [], set()
    for s in items:
        s = s if isinstance(s, dict) else {}
        try:
            n = skills._check(str(s.get("name") or ""), str(s.get("body") or ""))
        except ValueError as e:
            raise ValueError(f"技能 {s.get('name') or '(没名字)'}：{e}") from None
        if n in seen:
            raise ValueError(f"技能 /{n} 写了两遍")
        seen.add(n)
        out.append({"name": n, "description": str(s.get("description") or "")[:200], "body": str(s["body"])})
    return name, instructions[:4000], out


async def _make(name: str, instructions: str, items: list[dict], chat_id: int, post, emit) -> dict:
    b = bots.create(name, None, instructions)
    for s in items:
        skills.save(b["id"], s["name"], s["body"], s["description"], "chat")
    await emit({"type": "bots_changed"})
    if items:
        await emit({"type": "skills_changed"})
    await post("task", i18n.t("[已新建 Bot「{name}」]", name=b["name"]), chat_id, kind="bot_created", new_bot=b["id"])
    return b


async def create(bot: dict, chat_id: int, private_chat_id: int, args: dict, post, emit,
                 owed: dict | None = None, depth: int = 0) -> str:
    """create_bot 工具。bot：谁在建（主 Bot）；chat_id：建好的消息发到哪个对话；private_chat_id：卡片发到哪（它的私聊）。
    post 是 Router.add：(role, content, chat_id, **meta)；emit 是 Router.emit。
    owed、depth：主 Bot 这一轮欠谁回信、私信深度（见 dm.py），有了结果叫醒它时带上。"""
    if not bot.get("is_primary"):
        return f"没建：只有主 Bot（{bots.primary_name()}）能新建 Bot，用 message_bot 私信它，把名字和职责写清楚"
    try:
        name, instructions, items = _check(str(args.get("name") or ""), str(args.get("instructions") or ""), args.get("skills"))
    except ValueError as e:
        return f"没建：{e}"

    fake = SimpleNamespace(id=f"对话·{bot['name']}", bot_id=bot["id"], chat_id=chat_id, approval_chat_id=private_chat_id,
                           goal=i18n.t("新建 Bot"), background=False, waiting_approval=None)
    shown = {"名字": name, "职责": instructions, "技能": [f"/{s['name']}：{s['description'] or s['body'][:60]}" for s in items]}

    async def tell(text: str):
        """叫醒主 Bot 在这个对话里接着说一轮（见 dm.wake），它从最近的系统通知里看到结果。"""
        await dm.wake(bot["id"], chat_id, {"reply": owed, "depth": depth,
                                           "woke": {"kind": "newbot", "from": bot["id"], "text": text}})

    async def wait_and_make():
        ok, note = await approvals.request(fake, TOOL, shown, once=True)
        if not ok:   # 记一笔：下一轮模型知道用户没同意（和附带的意见），不会以为建好了
            text = i18n.t("[没新建 Bot「{name}」：{note}]", name=name, note=note) if note else \
                i18n.t("[没新建 Bot「{name}」]", name=name)
            await post("task", text, chat_id, kind="note")
            if note or owed:   # 用户附了意见要它改，或者有 Bot 等着结果
                await tell(text)
            return
        try:
            _check(name, instructions, items)   # 等确认的时候可能有人建了同名的
            b = await _make(name, instructions, items, chat_id, post, emit)
        except ValueError as e:
            text = i18n.t("[没建成 Bot「{name}」：{error}]", name=name, error=e)
            await post("task", text, chat_id, kind="note")
            await tell(text)
            return
        await tell(i18n.t("[已新建 Bot「{name}」]", name=b["name"]))

    t = asyncio.create_task(wait_and_make())
    _running.add(t)
    t.add_done_callback(_running.discard)
    after = f"，到时你的回复会作为回信发给 {dm._name(owed['bot'])}" if owed else ""
    return (f"已弹出确认卡片（新建 Bot「{name}」），卡片在你的私聊里；用户同意后会自动建好。"
            f"有了结果你会接着说一轮{after}，建好后要做的事（比如私信它交代事情）到时再做。"
            "现在简短告诉用户确认一下就行，不要说已经建好。")
