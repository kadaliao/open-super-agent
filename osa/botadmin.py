"""Bot 管理 Bot：查看、新建、修改、删除（Router 的 get_bot / create_bot / update_bot / delete_bot 工具）。

谁能做什么：
- 主 Bot 管所有 Bot：查看、新建、修改（包括它自己）、删除（不能删主 Bot，也就是不能删它自己）。
- 别的 Bot 只有 update_bot，只能改它自己；要新建、删除或改别的 Bot，私信主 Bot（提示词里写着）。
确认：
- 新建、修改、删除每次都先弹卡片（不能“总是允许”），卡片在发起的 Bot 的私聊里，同意才做；查看不用确认。
  修改的卡片上有职责的改动（删掉的划掉、加上的标出来），删除的卡片上写着会一起删掉什么。
- 和画图一样不让 Router 干等：工具马上返回，卡片和改动在后台进行；用户也能在对话里说“建吧”（resolve_approval）。
- 等确认的时候情况可能变了（同名的建了、要改的 Bot 被删了），同意后按原来的参数再检查一遍才做。
结果：
- 做成了在调用的那个对话里记一条（kind=bot_created / bot_updated / bot_deleted，界面上能点过去），侧栏马上变。
- 有了结果（做成、用户没同意、没做成）就叫醒发起的 Bot 在那个对话里接着说一轮（dm.wake，kind=botadmin），它才知道结果，
  好去做答应过的事（比如私信新 Bot 交代事情）。用户没同意又没附意见、也没有 Bot 等着的，不叫醒。
  是别的 Bot 私信托它做的，那一轮的回复不当场送回，等有了结果，叫醒的这一轮的回复作为回信送回去（同 dispatch）。
"""
import asyncio
import difflib
import re
import time
from types import SimpleNamespace
from . import approvals, bots, dm, i18n, memories, routines, skills
from .memory import conn

MAX_SKILLS = 10          # 一次带上或改的技能
MAX_INSTRUCTIONS = 4000
MAX_SKILL_SHOWN = 3000   # 查看时每个技能的做法最多给模型看这么多，要全文用 skill 参数
CARD_LIMIT = 12000       # 卡片参数（JSON）的长度：职责最长 4000 字，改动里新旧两份都有

_SKILL_ITEMS = {"type": "object", "properties": {
    "name": {"type": "string", "description": "简短的名字，用于 /名字 调用，不含空格"},
    "description": {"type": "string", "description": "一句话说明什么时候用"},
    "body": {"type": "string", "description": "完整的做法：步骤、要用的网站或应用、输出格式"}},
    "required": ["name", "body"]}
_AVATAR = {"type": "object", "description": "可选：头像，一个形状加一个颜色",
           "properties": {"shape": {"type": "string", "enum": bots.SHAPES},
                          "color": {"type": "string", "enum": bots.COLORS}}}

GET_BOT = {"type": "function", "function": {
    "name": "get_bot",
    "description": "查看 Bot 的资料，不用确认。不填 name：所有 Bot 的概况（职责开头、技能和定时任务个数、在哪些群、最近什么时候聊过、"
                   "有没有在做的任务）；填 name：这个 Bot 的完整资料（全部职责、技能和做法、定时任务、所在群聊、正在做的任务）。"
                   "改职责或技能之前先用它看原文",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "可选：Bot 的名字"},
        "skill": {"type": "string", "description": "可选：只看它的这个技能的完整做法（不带 /）"}}}}}

CREATE_BOT = {"type": "function", "function": {
    "name": "create_bot",
    "description": "新建一个 Bot。会先弹卡片请用户确认（上面有名字、职责和技能），用户同意后自动建好；"
                   "有了结果（建好、用户没同意、没建成）你会接着说一轮，建好后要做的事（比如私信它交代事情）到时再做。",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "名字，最多 40 个字，不能和现有的 Bot 重名"},
        "instructions": {"type": "string", "description": "它的职责和做事方式，写完整：负责什么、怎么做、有哪些硬性约束。"
                                                          "别的 Bot 也靠这段判断什么事该找它"},
        "avatar": _AVATAR,
        "skills": {"type": "array", "description": f"可选：一起给它的技能（可复用的做法），最多 {MAX_SKILLS} 个",
                   "items": _SKILL_ITEMS}},
        "required": ["name", "instructions"]}}}


def _update_schema(primary: bool) -> dict:
    who = "要改哪个 Bot（现在的名字），改你自己就填你的名字" if primary else "填你自己的名字：你不是主 Bot，只能改你自己"
    return {"type": "function", "function": {
        "name": "update_bot",
        "description": ("修改一个 Bot" if primary else "修改你自己") + "：名字、职责、头像、技能，只填要改的。"
                       "会先弹卡片请用户确认（上面有改前改后），用户同意后自动改好；有了结果你会接着说一轮",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": who},
            "new_name": {"type": "string", "description": "可选：新名字，最多 40 个字"},
            "instructions": {"type": "string", "description": "可选：改完后的完整职责，是全文，不是只写改动。"
                                                              "先用 get_bot 看原文，只改要改的地方，其余一字不差照抄"},
            "avatar": _AVATAR,
            "skills": {"type": "array", "description": f"可选：给它新增的技能，或者同名的整个替换掉（做法写全文），最多 {MAX_SKILLS} 个",
                       "items": _SKILL_ITEMS},
            "remove_skills": {"type": "array", "items": {"type": "string"}, "description": "可选：要删掉的它的技能，写名字（不带 /）"}},
            "required": ["name"]}}}


DELETE_BOT = {"type": "function", "function": {
    "name": "delete_bot",
    "description": "删除一个 Bot，连同它的私聊记录、技能、定时任务和它自己的记忆，删了不能恢复；它会退出所在的群聊，在做的任务会停掉。"
                   "只在用户明确要删时用。会先弹卡片请用户确认，同意后才删；有了结果你会接着说一轮",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "要删的 Bot 的名字"},
        "reason": {"type": "string", "description": "为什么删，写在卡片上给用户看"}},
        "required": ["name"]}}}

TOOLS = {"get_bot", "create_bot", "update_bot", "delete_bot"}


def tools_for(bot: dict) -> list[dict]:
    """主 Bot 四个都有；别的 Bot 只有 update_bot（只能改自己）。"""
    if bot.get("is_primary"):
        return [GET_BOT, CREATE_BOT, _update_schema(True), DELETE_BOT]
    return [_update_schema(False)]


# 结果写进对话的样子：(做成了, 没同意, 没同意附了意见, 没做成)。kind 是做成那一条的 meta.kind
TEXT = {
    "create_bot": ("bot_created", i18n.N_("[已新建 Bot「{name}」]"), i18n.N_("[没新建 Bot「{name}」]"),
                   i18n.N_("[没新建 Bot「{name}」：{note}]"), i18n.N_("[没建成 Bot「{name}」：{error}]")),
    "update_bot": ("bot_updated", i18n.N_("[已修改 Bot「{name}」：{what}]"), i18n.N_("[没修改 Bot「{name}」]"),
                   i18n.N_("[没修改 Bot「{name}」：{note}]"), i18n.N_("[没改成 Bot「{name}」：{error}]")),
    "delete_bot": ("bot_deleted", i18n.N_("[已删除 Bot「{name}」]"), i18n.N_("[没删除 Bot「{name}」]"),
                   i18n.N_("[没删除 Bot「{name}」：{note}]"), i18n.N_("[没删成 Bot「{name}」：{error}]")),
}
GOAL = {"create_bot": i18n.N_("新建 Bot"), "update_bot": i18n.N_("修改 Bot"), "delete_bot": i18n.N_("删除 Bot")}

_running: set[asyncio.Task] = set()   # 后台等确认、做改动的，留个引用免得被回收


# ---------- 检查参数（不行报 ValueError，写给模型看） ----------
def _name(name, exclude: int | None = None) -> str:
    name = str(name or "").strip().lstrip("@").strip()
    if not name:
        raise ValueError("名字是空的")
    if len(name) > 40:
        raise ValueError("名字最多 40 个字")
    if (b := bots.by_name(name)) and b["id"] != exclude:
        raise ValueError(f"已经有叫 {b['name']} 的 Bot 了，换个名字" + ("，或者直接用 message_bot 找它" if exclude is None else ""))
    return name


def _instructions(text) -> str:
    text = str(text or "").strip()
    if not text:
        raise ValueError("职责是空的：写清楚它负责什么、怎么做")
    if len(text) > MAX_INSTRUCTIONS:
        raise ValueError(f"职责太长了（{len(text)} 个字），最多 {MAX_INSTRUCTIONS} 个字，精简一下")
    return text


def _avatar(a) -> dict | None:
    if not isinstance(a, dict) or not (a.get("shape") or a.get("color")):
        return None
    shape, color = a.get("shape"), str(a.get("color") or "")
    if shape and shape not in bots.SHAPES:
        raise ValueError(f"头像形状只能是 {'、'.join(bots.SHAPES)} 之一")
    if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError(f"头像颜色要写成 #RRGGBB，比如 {bots.COLORS[0]}")
    return {k: v for k, v in (("shape", shape), ("color", color.lower())) if v}


def _skills(items) -> list[dict]:
    if not isinstance(items, list):
        items = []
    if len(items) > MAX_SKILLS:
        raise ValueError(f"技能一次最多 {MAX_SKILLS} 个")
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
    return out


def _target(name) -> dict:
    b = bots.by_name(str(name or ""))
    if not b:
        others = "、".join(x["name"] for x in bots.list_bots())
        raise ValueError(f"没有叫 {name} 的 Bot（没建成或已删除，记忆里说有也不算）。现有：{others}")
    return b


def _own_skills(bot_id: int) -> dict[str, dict]:
    return {s["name"]: s for s in skills.list_for(bot_id) if s["bot_id"] == bot_id}


def _skill_line(s: dict) -> str:
    return f"/{s['name']}：{s['description'] or s['body'][:60]}"


def plan_create(me: dict, args: dict) -> dict:
    if not me.get("is_primary"):
        raise ValueError(f"只有主 Bot（{bots.primary_name()}）能新建 Bot，用 message_bot 私信它，把名字和职责写清楚")
    name = _name(args.get("name"))
    if len(bots.list_bots()) >= bots.MAX_BOTS:
        raise ValueError(f"已经有 {bots.MAX_BOTS} 个 Bot，到上限了，请用户先删掉不用的")
    plan = {"name": name, "instructions": _instructions(args.get("instructions")),
            "avatar": _avatar(args.get("avatar")), "skills": _skills(args.get("skills"))}
    shown = {"名字": name, "职责": plan["instructions"], "技能": [_skill_line(s) for s in plan["skills"]]}
    if plan["avatar"]:
        shown["头像"] = plan["avatar"]
    return {**plan, "label": name, "shown": shown}


def _diff(old: str, new: str) -> list[list]:
    """职责的改动，按分句比：[[0, 没变的], [-1, 删掉的], [1, 加上的], …]。中文一句一句地比，比逐字比好读。"""
    split = lambda s: [x for x in re.split(r"(?<=[。！？；;!?\n])", s) if x]
    a, b = split(old), split(new)
    out: list[list] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        for sign, part in [(0, a[i1:i2])] if op == "equal" else [(-1, a[i1:i2]), (1, b[j1:j2])]:
            if not part:
                continue
            if out and out[-1][0] == sign:
                out[-1][1] += "".join(part)
            else:
                out.append([sign, "".join(part)])
    return out


def plan_update(me: dict, args: dict) -> dict:
    t = _target(args.get("name"))
    if not me.get("is_primary") and t["id"] != me["id"]:
        raise ValueError(f"你只能改你自己。要改 {t['name']}，私信主 Bot「{bots.primary_name()}」，把要改成什么写清楚")
    plan: dict = {"id": t["id"]}
    shown: dict = {"Bot": t["name"], "编号": t["id"]}
    what: list[str] = []
    new_name = str(args.get("new_name") or "").strip().lstrip("@").strip()
    if new_name and new_name != t["name"]:
        plan["name"] = shown["新名字"] = _name(new_name, exclude=t["id"])
        what.append(i18n.t("名字改成 {name}", name=new_name))
    text = str(args.get("instructions") or "").strip()
    if text and text != t["instructions"].strip():
        plan["instructions"] = shown["职责"] = _instructions(text)
        shown["职责改动"] = _diff(t["instructions"], text)
        what.append(i18n.t("职责"))
    av = _avatar(args.get("avatar"))
    if av:
        old = t["avatar"]
        new = {"shape": av.get("shape") or old["shape"], "color": av.get("color") or old["color"]}
        if new != {"shape": old["shape"], "color": old["color"]} or old.get("image"):
            plan["avatar"] = shown["头像"] = new   # 有图片头像的，换成形状和颜色就不再用那张图
            if old.get("image"):
                shown["不再用图片头像"] = True
            what.append(i18n.t("头像"))
    own = _own_skills(t["id"])
    add = _skills(args.get("skills"))
    drop = [str(n or "").strip().lstrip("/") for n in (args.get("remove_skills") or []) if str(n or "").strip()]
    for n in drop:
        if n not in own:
            mine = "、".join("/" + x for x in own) or "没有"
            raise ValueError(f"{t['name']} 没有自己的技能 /{n}（共用的技能不能在这里删）。它自己的技能：{mine}")
        if any(s["name"] == n for s in add):
            raise ValueError(f"技能 /{n} 又要删又要改，只留一个")
    if add or drop:
        if len(own) - len(drop) + len([s for s in add if s["name"] not in own]) > skills.MAX_PER_BOT:
            raise ValueError(f"每个 Bot 最多 {skills.MAX_PER_BOT} 个技能")
        plan["skills"], plan["remove_skills"] = add, sorted(set(drop))
        if new := [_skill_line(s) for s in add if s["name"] not in own]:
            shown["新技能"] = new
        if changed := [_skill_line(s) for s in add if s["name"] in own]:
            shown["改技能"] = changed
        if plan["remove_skills"]:
            shown["删技能"] = ["/" + n for n in plan["remove_skills"]]
        what.append(i18n.t("技能") + " " + " ".join(f"/{n}" for n in [*(s["name"] for s in add), *plan["remove_skills"]]))
    if len(plan) == 1:
        raise ValueError("没有要改的：要改的地方和现在一样，或者都没填。名字、职责、头像、技能至少填一样")
    return {**plan, "label": t["name"], "shown": shown, "what": i18n.t("、").join(what)}


def _footprint(b: dict) -> dict:
    """删掉一个 Bot 会一起删掉什么。"""
    from . import router
    n_msgs = conn.execute("select count(*) from messages where chat_id=?", (b["chat_id"],)).fetchone()[0] if b["chat_id"] else 0
    in_groups = [c for c in bots.list_chats() if c["kind"] == "group" and b["id"] in c["bot_ids"]]
    return {"会删掉": {"消息": n_msgs, "技能": len(_own_skills(b["id"])), "定时任务和事件": len(routines.list_for(b["id"])),
                    "记忆": len(memories.list_for(b["id"])["own"])},
            "群聊": [c["title"] for c in in_groups],
            "在做的任务": sum(1 for t in router.TASKS.values() if t.bot_id == b["id"] and t.status == "running")}


def plan_delete(me: dict, args: dict) -> dict:
    if not me.get("is_primary"):
        raise ValueError(f"只有主 Bot（{bots.primary_name()}）能删除 Bot")
    t = _target(args.get("name"))
    if t["id"] == me["id"] or t["is_primary"]:
        raise ValueError("主 Bot 不能删除。要删的话请用户先在设置里把别的 Bot 设为主 Bot")
    shown = {"Bot": t["name"], "编号": t["id"], **_footprint(t)}
    if reason := str(args.get("reason") or "").strip():
        shown["原因"] = reason[:500]
    return {"id": t["id"], "label": t["name"], "shown": shown}


PLAN = {"create_bot": plan_create, "update_bot": plan_update, "delete_bot": plan_delete}
# 用户能在卡片上直接改的：卡片参数的键 → 工具参数。新建和修改时的职责（改后的全文）
EDITABLE = {"create_bot": {"职责": "instructions"}, "update_bot": {"职责": "instructions"}}


# ---------- 做 ----------
async def remove(bot_id: int):
    """删掉一个 Bot 和它的一切（网页上删和主 Bot 删都走这里）：停掉它在做的任务，删私聊、技能、定时任务、它自己的记忆；
    群里移出；聊天软件绑着它的私聊的，改成和主 Bot 对话。共享的用户事实、资料库里的页面留着。主 Bot 不能删（ValueError）。"""
    from . import channels, router
    b = bots.get(bot_id)
    if not b:
        return
    if b["is_primary"]:
        raise ValueError(i18n.t("主 Bot 不能删除，先把别的 Bot 设为主 Bot"))
    for t in list(router.TASKS.values()):
        if t.bot_id == bot_id and t.status == "running":
            router.cancel(t.id, i18n.t("Bot 已删除"))
    approvals.cancel_for_bot(bot_id)
    bots.delete(bot_id)
    routines.delete_for_bot(bot_id)
    memories.delete_for_bot(bot_id)
    conn.execute("delete from skills where bot_id=?", (bot_id,))
    conn.commit()
    router.RUNTIMES.pop(bot_id, None)
    channels.chat_removed(b["chat_id"])


async def _apply(tool: str, plan: dict, chat_id: int, post, emit) -> str:
    """同意后做。返回记进对话的那条（做成了）。"""
    kind, done = TEXT[tool][0], TEXT[tool][1]
    if tool == "create_bot":
        b = bots.create(plan["name"], plan["avatar"], plan["instructions"])
        for s in plan["skills"]:
            skills.save(b["id"], s["name"], s["body"], s["description"], "chat")
        text = i18n.t(done, name=b["name"])
        meta = {"new_bot": b["id"]}
    elif tool == "update_bot":
        for n in plan.get("remove_skills") or []:
            if s := _own_skills(plan["id"]).get(n):
                skills.delete(s["id"])
        for s in plan.get("skills") or []:
            skills.save(plan["id"], s["name"], s["body"], s["description"], "chat")
        b = bots.update(plan["id"], name=plan.get("name"), instructions=plan.get("instructions"), avatar=plan.get("avatar"))
        text = i18n.t(done, name=b["name"], what=plan["what"])
        meta = {"bot": b["id"]}
    else:
        await remove(plan["id"])
        text = i18n.t(done, name=plan["label"])
        meta = {}
    await emit({"type": "bots_changed"})
    if plan.get("skills") or plan.get("remove_skills"):
        await emit({"type": "skills_changed"})
    await post("task", text, chat_id, kind=kind, **meta)
    return text


async def call(tool: str, bot: dict, chat_id: int, private_chat_id: int, args: dict, post, emit,
               owed: dict | None = None, depth: int = 0) -> str:
    """Router 调的入口。bot：谁在调；chat_id：结果记在哪个对话；private_chat_id：卡片发到哪（它的私聊）。
    post 是 Router.add：(role, content, chat_id, **meta)；emit 是 Router.emit。
    owed、depth：这一轮欠谁回信、私信深度（见 dm.py），有了结果叫醒它时带上。"""
    if tool == "get_bot":
        return view(bot, args)
    verb = {"create_bot": "建", "update_bot": "改", "delete_bot": "删"}[tool]
    try:
        plan = PLAN[tool](bot, args)
    except ValueError as e:
        return f"没{verb}：{e}"
    shown = plan["shown"] | ({"请求方": dm._name(owed["bot"])} if owed else {})
    fake = SimpleNamespace(id=f"对话·{bot['name']}", bot_id=bot["id"], chat_id=chat_id, approval_chat_id=private_chat_id,
                           goal=i18n.t(GOAL[tool]), background=False, waiting_approval=None)
    _, _, no, no_note, failed = TEXT[tool]
    label = plan["label"]

    async def tell(text: str, edited: list[str] | None = None):
        """叫醒发起的 Bot 在这个对话里接着说一轮（见 dm.wake），它从最近的系统通知里看到结果。
        edited：用户在卡片上改过哪些（卡片参数的键），它要以改过的为准。"""
        await dm.wake(bot["id"], chat_id, {"reply": owed, "depth": depth, "woke": {
            "kind": "botadmin", "from": bot["id"], "text": text, **({"edited": edited} if edited else {})}})

    editable = EDITABLE.get(tool, {})

    async def wait_and_do():
        ok, note = await approvals.request(fake, tool, shown, once=True, limit=CARD_LIMIT,
                                           editable=tuple(k for k in editable if k in shown))
        if not bots.get(bot["id"]):   # 等的时候发起的 Bot 自己被删了
            return
        if not ok:   # 记一笔：下一轮模型知道用户没同意（和附带的意见），不会以为做成了
            text = i18n.t(no_note, name=label, note=note) if note else i18n.t(no, name=label)
            await post("task", text, chat_id, kind="note")
            if note or owed:   # 用户附了意见要它改，或者有 Bot 等着结果
                await tell(text)
            return
        # 用户在卡片上改过的（已经写回 shown）换进参数里
        edited = [k for k in editable if k in shown and shown[k] != plan["shown"][k]]
        final = {**args, **{editable[k]: shown[k] for k in edited}}
        try:
            fresh = PLAN[tool](bots.get(bot["id"]), final)   # 等确认的时候情况可能变了：再检查一遍
            text = await _apply(tool, fresh, chat_id, post, emit)
        except ValueError as e:
            text = i18n.t(failed, name=label, error=e)
            await post("task", text, chat_id, kind="note")
        await tell(text, edited)

    t = asyncio.create_task(wait_and_do())
    _running.add(t)
    t.add_done_callback(_running.discard)
    what = {"create_bot": f"新建 Bot「{label}」", "update_bot": f"修改 Bot「{label}」", "delete_bot": f"删除 Bot「{label}」"}[tool]
    after = f"，到时你的回复会作为回信发给 {dm._name(owed['bot'])}" if owed else ""
    return (f"已弹出确认卡片（{what}），卡片在你的私聊里；用户同意后会自动{verb}好。"
            f"有了结果你会接着说一轮{after}，之后要做的事（比如私信新 Bot 交代事情）到时再做。"
            f"现在简短告诉用户确认一下就行，不要说已经{verb}好。")


# ---------- 查看 ----------
def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "没聊过"


def _chat_stats(b: dict) -> tuple[int, float | None]:
    if not b["chat_id"]:
        return 0, None
    n, last = conn.execute("select count(*), max(ts) from messages where chat_id=?", (b["chat_id"],)).fetchone()
    return n, last


def _groups_of(bot_id: int) -> list[dict]:
    return [c for c in bots.list_chats() if c["kind"] == "group" and bot_id in c["bot_ids"]]


def _running_tasks(bot_id: int) -> list:
    from . import router
    return [t for t in router.TASKS.values() if t.bot_id == bot_id and t.status == "running"]


def _overview(me: dict) -> str:
    rows = bots.list_bots()
    lines = [f"共 {len(rows)} 个 Bot（最多 {bots.MAX_BOTS} 个）："]
    for b in rows:
        tags = [x for x in ("主 Bot" if b["is_primary"] else "", "就是你" if b["id"] == me["id"] else "") if x]
        _, last = _chat_stats(b)
        role = (b["instructions"] or "职责没写明").replace("\n", " ")
        rs = routines.list_for(b["id"])
        ne = sum(1 for r in rs if routines.is_event(r))
        stats = [f"技能 {len(_own_skills(b['id']))} 个", f"定时任务 {len(rs) - ne} 个"] + ([f"事件 {ne} 个"] if ne else [])
        if g := _groups_of(b["id"]):
            stats.append(f"在 {len(g)} 个群里")
        if r := _running_tasks(b["id"]):
            stats.append(f"在做 {len(r)} 个任务")
        stats.append(f"最近私聊：{_when(last)}")
        lines.append(f"- {b['name']}{'（' + '，'.join(tags) + '）' if tags else ''}：{role[:60]}{'…' if len(role) > 60 else ''}"
                     f"｜{'，'.join(stats)}")
    lines.append("要看某个 Bot 的完整职责和技能，get_bot 填 name。")
    return "\n".join(lines)


def _detail(b: dict, me: dict) -> str:
    n, last = _chat_stats(b)
    av = b["avatar"]
    head = [f"{b['name']}（{'主 Bot' if b['is_primary'] else '普通 Bot'}{'，就是你' if b['id'] == me['id'] else ''}）",
            f"建于 {_when(b['created'])}；头像：{av.get('shape')} {av.get('color')}{'（用的是一张图片）' if av.get('image') else ''}",
            f"私聊：{n} 条消息，最近一条 {_when(last)}",
            "职责：", b["instructions"] or "（没写）"]
    own = list(_own_skills(b["id"]).values())
    shared = [s for s in skills.list_for(b["id"]) if s["bot_id"] is None and s["name"] not in {x["name"] for x in own}]
    parts = ["\n".join(head), f"它自己的技能（{len(own)} 个）：" if own else "它自己的技能：无"]
    budget = 12000
    for s in own:
        body = s["body"] if len(s["body"]) <= MAX_SKILL_SHOWN else \
            s["body"][:MAX_SKILL_SHOWN] + f"\n……（后面还有 {len(s['body']) - MAX_SKILL_SHOWN} 个字，要全文用 get_bot 的 skill 参数）"
        if budget - len(body) < 0:
            body = f"（做法太长没列出来，要看用 get_bot 的 skill 参数：{s['name']}）"
        budget -= len(body)
        parts.append(f"/{s['name']}：{s['description'] or '（没写说明）'}\n做法：\n{body}")
    if shared:
        parts.append("所有 Bot 共用的技能：" + " ".join("/" + s["name"] for s in shared))
    rs = routines.list_for(b["id"])
    for label, part in (("定时任务", [r for r in rs if not routines.is_event(r)]), ("事件", [r for r in rs if routines.is_event(r)])):
        parts.append(f"{label}：" + ("\n" + "\n".join(
            f"- #{r['id']} {r['name']}（{routines.describe(r['trigger'], in_lang='zh')}{'' if r['enabled'] else '，已暂停'}）：{r['prompt'][:100]}"
            for r in part) if part else "无"))
    gs = _groups_of(b["id"])
    parts.append("所在群聊：" + ("、".join(f"「{c['title']}」" + ("（协调者）" if c["lead"] == b["id"] else "") for c in gs) or "无"))
    ts = _running_tasks(b["id"])
    parts.append("正在做的任务：" + ("\n" + "\n".join(f"- #{t.id} {t.goal[:80]}" for t in ts) if ts else "无"))
    parts.append(f"长期记忆：它自己记了 {len(memories.list_for(b['id'])['own'])} 条（内容只有它自己能看）")
    return "\n\n".join(parts)


def view(me: dict, args: dict) -> str:
    """get_bot 工具。"""
    name = str(args.get("name") or "").strip()
    if not name:
        return _overview(me)
    b = bots.by_name(name)
    if not b:
        others = "、".join(x["name"] for x in bots.list_bots())
        return f"没有叫 {name} 的 Bot（没建成或已删除，记忆里说有也不算）。现有：{others}"
    if sk := str(args.get("skill") or "").strip().lstrip("/"):
        s = _own_skills(b["id"]).get(sk) or skills.find(b["id"], sk)
        if not s:
            return f"{b['name']} 没有 /{sk} 这个技能。它能用的：" + (" ".join("/" + x["name"] for x in skills.list_for(b["id"])) or "无")
        whose = "所有 Bot 共用的" if s["bot_id"] is None else "它自己的"
        return f"{b['name']} 的技能 /{s['name']}（{whose}）：{s['description'] or '（没写说明）'}\n做法：\n{s['body']}"
    return _detail(b, me)
