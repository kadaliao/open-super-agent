"""Router：常驻会话，负责和用户对话、派发任务。自己不执行工具（对应 DeepSeek 设计里 Router 无 shell）。

一个 Bot 一个 Router，既在私聊里说话，也在它所在的群聊里说话；每一轮都明确是在哪个对话里。"""
import asyncio
import json
import time
from .computer import tools as ctools
from .config import CFG, call_kwargs, client_for
from . import approvals, bots, library, memories, memory, routines, skills
from . import worker as worker_mod
from .worker import Task, run_worker, run_heavy

ROUTER_PROMPT = """你是「{name}」，用户的 AI 队友。{instructions}
你在对话里直接和用户交流；你不亲自查资料，而是：
- 闲聊、常识、能直接答的问题：直接回答。
- 需要联网查证、多步研究的：调用 dispatch 派给后台 Worker，然后简短告诉用户已开始（用户可以继续聊别的）。
  难题、高风险判断用 mode=heavy（多个 Worker 并行互审，更贵），其余用 single。
- 用户用“/技能名”调用技能时，按消息里附带的技能做法执行（需要查证、多步的就派任务，并把做法完整写进 goal）。
- 用户说“把刚才的做法存成技能”之类时，用 save_skill 把可复用的步骤写清楚（不写这次的具体数据）。
- 长期记忆：用户说“记住……”“以后都……”，或者说出了以后还用得上的稳定信息（偏好、背景、身份）时，用 remember 记下来，
  回复里顺带一句“记住了”。一次性的事不记；每条写成独立完整的一句话。
  只有名字、时区、所在城市、常用语言、职业这类关于用户本人的基本事实设 shared=true（所有 Bot 共享）；
  对你工作的要求和偏好只记在你自己这里。新信息和旧记忆冲突时用 replace_id 改那一条；用户让你忘掉就 forget。
- 用户要把结果“做成页面 / 报告 / 能分享的页面”时，用 save_page 存进资料库，回复里附上返回的链接；
  需要先查资料再写的，派任务并在 goal 里写明“最后用 save_page 保存成页面”。
- 消息里的“[附件] 路径”是用户上传到电脑里的文件；要读或处理它就派任务，goal 里写上完整路径。
- 用户要求定期 / 每天 / 每隔一段时间做某事，或收到外部事件时做某事，用 create_routine 建定时任务；
  建好后告诉用户什么时候运行、可以在右栏“定时”里暂停或修改。
- 用户问进度用 list_tasks；要取消用 cancel_task；需要早先对话原文用 recall。
- 标有“系统通知”的消息是后台任务的状态，不是用户说的话；不要模仿它的格式。
- 永远优先回应用户最新的那条消息；之前的任务结果用户已经看过了。
- 每条新的用户请求都要单独判断，需要查证就派发新任务，不要因为别的任务在跑而忽略。
- 派任务时只写用户要的东西，不要自己加码（用户说“打开某页面”，就只是打开，不要变成“把整站数据抓下来”）。
- 电脑上的简单操作（打开网页、点一下、搜索框输入、按键、滚动、看屏幕）你自己直接做：网页优先用 browser_* 工具
  （按元素编号操作，返回页面文字），要看画面才用 computer_screenshot / look_at_screen；只有多步、耗时、需要终端或要等人接手的活才派任务。
- 用户用“@应用名”指定应用时，派任务的 goal 里原样保留“@应用名”，并写明要用这个应用完成。
  提到的应用没连接时，告诉用户去设置 → 应用里连接，不要假装能做。
- 用户的新指令和正在跑的任务冲突（比如换了要做的事）时，先 cancel_task 把旧任务停掉再做。
今天是 {date}。

后台 Worker 当前能用的工具（判断能不能做时以此为准）：
{capabilities}

你的技能（用户用 /名字 调用）：
{skills}

你的定时任务：
{routines}

你的长期记忆（每次对话都带着，按这些偏好做事；#后面是编号）：
{memories}

当前任务：
{tasks}

等待用户确认的写操作（用户在聊天里同意或拒绝时，调用 resolve_approval；意思不明确就先问清楚，不要替用户做决定）：
{approvals}{group}"""

GROUP_PROMPT = """

现在你在群聊「{title}」里，不是私聊。成员：
{members}
{lead}
群聊规则：
- 用户的消息没有标记；“【名字】”开头的是别的 Bot 的发言。你只代表你自己说话，不要替别的 Bot 回答，也不要模仿“【名字】”的格式。
- 轮到你时只做和你有关的部分；别人负责的、或者用户点名给别人的，不要抢着做。
- 要别的 Bot 接着做（比如你查完资料让擅长写作的 Bot 总结），用 handoff 交给它，说清楚要它做什么；
  如果要等后台任务做完才能交接，在 dispatch 里填 handoff_to 和 handoff_note，任务结果会自动转给它，不用你再回来。
- 你那部分要等别的 Bot 先做完时，简短说一句会接着做就行，不要重复别人的工作。
- 交接给你的工作，直接做完并给出结果；需要查证就派任务。
- 确认卡片和“接手电脑”的请求会出现在你和用户的私聊里，不在群里。"""

HANDOFF = {"type": "function", "function": {"name": "handoff", "description": "在群聊里把工作交给另一个 Bot，它会马上接着做",
    "parameters": {"type": "object", "properties": {
        "to": {"type": "string", "description": "成员的名字"},
        "request": {"type": "string", "description": "要它做什么，写完整（它能看到群里之前的消息）"}},
        "required": ["to", "request"]}}}

DIRECT_PC = {"computer_screenshot", "computer_click", "computer_type",
             "computer_key", "computer_scroll", "browser_open", "browser_snapshot", "browser_click",
             "browser_type", "browser_key", "browser_scroll", "browser_back", "browser_read"}

ROUTER_TOOLS = [*[s for s in ctools.SCHEMAS if s["function"]["name"] in DIRECT_PC],
    {"type": "function", "function": {"name": "dispatch", "description": "派发后台任务",
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "完整、自包含的任务描述"},
            "mode": {"type": "string", "enum": ["single", "heavy"]},
            "handoff_to": {"type": "string", "description": "只在群聊里用：任务完成后把结果交给哪个成员接着做"},
            "handoff_note": {"type": "string", "description": "交接时要对方做什么"}}, "required": ["goal"]}}},
    {"type": "function", "function": {"name": "save_skill", "description": "保存或覆盖一个技能（可复用的做事方法）",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "简短的名字，用于 /名字 调用，不含空格"},
            "description": {"type": "string", "description": "一句话说明什么时候用"},
            "body": {"type": "string", "description": "完整的做法：步骤、要用的网站或应用、输出格式"}},
            "required": ["name", "body"]}}},
    {"type": "function", "function": {"name": "create_routine", "description": "新建定时任务",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "名字；webhook 类只能用字母数字 - _"},
            "prompt": {"type": "string", "description": "每次运行要做的事，写完整"},
            "skill": {"type": "string", "description": "可选：每次按哪个技能做（不带 /）"},
            "kind": {"type": "string", "enum": ["daily", "every", "webhook"]},
            "times": {"type": "array", "items": {"type": "string"}, "description": "daily 用：HH:MM 列表"},
            "days": {"type": "array", "items": {"type": "integer"}, "description": "daily 用：周几运行，周一=1，留空=每天"},
            "minutes": {"type": "integer", "description": "every 用：间隔分钟，至少 5"}},
            "required": ["name", "kind"]}}},
    {"type": "function", "function": {"name": "remember", "description": "记下一条长期记忆（以后的对话都会带着）",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "独立完整的一句话，例如“用户希望回答先给结论”"},
            "shared": {"type": "boolean", "description": "关于用户本人的基本事实（名字、时区、所在城市等）为 true，所有 Bot 共享"},
            "replace_id": {"type": "integer", "description": "可选：用这条替换已有的那条记忆"}},
            "required": ["text"]}}},
    {"type": "function", "function": {"name": "forget", "description": "删除一条长期记忆",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
    library.SAVE_PAGE,
    {"type": "function", "function": {"name": "list_tasks", "description": "查看任务状态",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "cancel_task", "description": "取消任务",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "resolve_approval", "description": "处理一个待确认的写操作",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer"}, "approve": {"type": "boolean"},
            "note": {"type": "string", "description": "用户附带的意见，例如修改要求"},
            "always": {"type": "boolean", "description": "用户明确说以后同类操作都不用问时为 true"}}, "required": ["id", "approve"]}}},
    {"type": "function", "function": {"name": "recall", "description": "按 id 取回早先消息原文",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "look_at_screen",
        "description": "看一眼共用电脑现在的屏幕（截图 + 当前窗口标题）。用户问屏幕上有什么、某个任务做到哪了、"
                       "或者你要判断下一步该派什么任务时用。只看不操作；要操作就派任务。",
        "parameters": {"type": "object", "properties": {}}}},
]


TASKS: dict[int, Task] = {}       # 所有 Bot 的任务，Watchdog 统一巡检
RUNTIMES: dict[int, "Router"] = {}
notify = None                     # 由 server 设置：async (event) -> None


async def _progress(task, text: str):
    if notify:
        await notify({"type": "task", "chat_id": task.chat_id, "bot_id": task.bot_id, "task": task.public()})

worker_mod.PROGRESS = _progress


def runtime(bot_id: int) -> "Router":
    if bot_id not in RUNTIMES:
        RUNTIMES[bot_id] = Router(bot_id)
    return RUNTIMES[bot_id]


def cancel(tid: int, reason: str) -> str:
    t = TASKS.get(tid)
    return runtime(t.bot_id).cancel(tid, reason) if t else f"没有任务 #{tid}"


class Router:
    """一个 Bot 的常驻会话。一次处理一条消息；派出去的 Worker 在后台并行。"""

    def __init__(self, bot_id: int):
        self.bot_id = bot_id
        self.lock = asyncio.Lock()
        self.chat: dict = {}              # 本轮在哪个对话里说话（_think 开始时设置）

    @property
    def bot(self) -> dict:
        return bots.get(self.bot_id)

    @property
    def chat_id(self) -> int:
        """这个 Bot 的私聊。群聊里说话时用 self.chat（本轮所在的对话）。"""
        return self.bot["chat_id"]

    @property
    def tasks(self) -> dict[int, Task]:
        return {i: t for i, t in TASKS.items() if t.bot_id == self.bot_id}

    async def emit(self, event: dict, chat_id: int | None = None):
        if notify:
            await notify({"chat_id": chat_id or self.chat_id, "bot_id": self.bot_id} | event)

    async def add(self, role: str, content: str, chat_id: int | None = None, **meta) -> int:
        cid = chat_id or self.chat_id
        mid = memory.add(role, content, cid, self.bot_id, **meta)
        await self.emit({"type": "message", "message": {"id": mid, "ts": time.time(), "role": role,
                         "content": content, "meta": meta, "bot_id": self.bot_id}}, cid)
        return mid

    def _over_budget(self) -> bool:
        return memory.tokens_today() > CFG["limits"]["daily_token_budget"]

    async def handle_user(self, text: str):
        meta = {}
        if hit := skills.parse(self.bot_id, text):
            meta = {"skill": hit[0]["name"], "skill_prompt": skills.expand(*hit)}
        await self.add("user", text, **meta)
        await self.respond(self.chat_id)

    async def respond(self, chat_id: int):
        """在某个对话里说一轮话。群聊由 groups 决定轮到谁。"""
        async with self.lock:
            await self._think(bots.chat(chat_id))

    async def _stream(self, client, model, msgs, sid: str, started: float, use_tools: bool = True):
        """流式调用一次模型，边收边推送正文和思考过程。返回 (正文, 思考, 工具调用列表)。"""
        tools = ROUTER_TOOLS + ([HANDOFF] if self.chat["kind"] == "group" else [])
        cid = self.chat["id"]
        stream = await client.chat.completions.create(
            model=model, messages=msgs, stream=True, **({"tools": tools} if use_tools else {}),
            stream_options={"include_usage": True}, **call_kwargs("router"))
        content, reasoning, calls, usage = "", "", {}, None
        async for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            if rc := getattr(d, "reasoning_content", None):
                reasoning += rc
                await self.emit({"type": "stream", "sid": sid, "thinking": rc}, cid)
            if d.content:
                content += d.content
                await self.emit({"type": "stream", "sid": sid, "delta": d.content}, cid)
            for tc in d.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                c["id"] = tc.id or c["id"]
                if tc.function:
                    c["name"] += tc.function.name or ""
                    c["args"] += tc.function.arguments or ""
        memory.record_usage("router", model, usage)
        return content, reasoning, [calls[k] for k in sorted(calls)]

    def _group_block(self, chat: dict) -> str:
        if chat["kind"] != "group":
            return ""
        lines = []
        for i in chat["bot_ids"]:
            m = bots.get(i)
            if m:
                role = (m["instructions"] or "职责没写明，看群里的对话").replace("\n", " ")[:120]
                lines.append(f"- {m['name']}{'（你）' if i == self.bot_id else ''}{'（协调者）' if i == chat['lead'] else ''}：{role}")
        lead = ("你是这个群的协调者：用户没有 @ 任何人的消息由你接。能自己答就答；适合别人做的，用 handoff 分给对应的 Bot"
                "（可以分给几个），再简短告诉用户谁在做什么。") if chat["lead"] == self.bot_id else \
            "用户 @ 你、或者别的 Bot 把工作交给你时才轮到你。"
        return GROUP_PROMPT.format(title=chat["title"], members="\n".join(lines), lead=lead)

    async def _think(self, chat: dict):
        client, model = client_for("router")
        b = self.bot
        self.chat = chat
        cid = chat["id"]
        msgs = [{"role": "system", "content": ROUTER_PROMPT.format(
            name=b["name"], instructions=b["instructions"] or "你的具体职责以用户在对话里交代的为准。",
            date=time.strftime("%Y-%m-%d"), tasks=self._task_list(), skills=skills.summary(self.bot_id),
            routines=self._routine_list(), capabilities=self._capabilities(), memories=memories.prompt_block(self.bot_id),
            approvals=approvals.summary(self.bot_id), group=self._group_block(chat))}] \
            + memory.context(cid, self.bot_id if chat["kind"] == "group" else None)
        sid, started = f"s{time.time_ns()}", time.time()
        self._turn_tasks = []             # 本轮派发的任务，挂在这条回复下面显示
        shown, thinking, shots = "", "", []
        await self.emit({"type": "stream_start", "sid": sid}, cid)
        try:
            for _ in range(10):
                content, reasoning, calls = await self._stream(client, model, msgs, sid, started)
                shown += content
                thinking += reasoning
                if not calls:
                    break
                msgs.append({"role": "assistant", "content": content, "reasoning_content": reasoning,
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                            for c in calls]})
                for c in calls:
                    try:
                        args = json.loads(c["args"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    out = await self._tool(c["name"], args)
                    if isinstance(out, tuple):          # look_at_screen：文字 + 截图
                        out, png = out
                        shots.append(png)
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
                if shots:   # 图片要放在所有 tool 消息之后；只在本轮用，不存进记录
                    msgs.append(ctools.image_message(shots[-1], "共用电脑的当前屏幕"))
                    shots = []
                if shown and not shown.endswith("\n"):
                    shown += "\n\n"
                    await self.emit({"type": "stream", "sid": sid, "delta": "\n\n"}, cid)
            if not shown.strip():   # 工具轮数用完还没开口：关掉工具，逼它给一句回复
                content, reasoning, _ = await self._stream(client, model, msgs, sid, started, use_tools=False)
                shown, thinking = content, thinking + reasoning
        finally:
            await self.emit({"type": "stream_end", "sid": sid}, cid)
        await self.add("assistant", shown.strip(), cid, reasoning=reasoning, thinking=thinking,
                       thought_s=round(time.time() - started, 1), sid=sid, tasks=self._turn_tasks)

    def _member(self, name: str) -> dict | None:
        """群里按名字找成员（不区分大小写，去掉开头的 @）。"""
        key = (name or "").strip().lstrip("@").lower()
        for i in self.chat["bot_ids"]:
            m = bots.get(i)
            if m and m["name"].lower() == key:
                return m
        return None

    async def _tool(self, name: str, args: dict) -> str:
        if name == "dispatch":
            if self._over_budget():
                return "今日 token 预算已用完，没有派发。请告诉用户。"
            handoff = None
            if args.get("handoff_to") and self.chat["kind"] == "group":
                to = self._member(args["handoff_to"])
                if not to or to["id"] == self.bot_id:
                    return f"没派发：群里没有叫 {args['handoff_to']} 的其他成员"
                handoff = {"to": to["id"], "note": args.get("handoff_note") or ""}
                from . import groups
                groups.expect(self.chat["id"], to["id"])
            task = await self.dispatch(args["goal"], args.get("mode", "single"), chat_id=self.chat["id"], handoff=handoff)
            self._turn_tasks.append(task.id)
            if handoff:
                return f"已派发任务 #{task.id}，做完后结果会自动交给 {to['name']}"
            return f"已派发任务 #{task.id}"
        if name == "handoff":
            if self.chat["kind"] != "group":
                return "handoff 只能在群聊里用"
            to = self._member(args.get("to", ""))
            if not to or to["id"] == self.bot_id:
                return f"群里没有叫 {args.get('to')} 的其他成员"
            from . import groups
            await groups.handoff(self.chat["id"], self.bot_id, to["id"], args.get("request", ""))
            return f"已交给 {to['name']}，你这轮说完它就接着做"
        if name == "save_skill":
            try:
                s = skills.save(self.bot_id, args.get("name", ""), args.get("body", ""), args.get("description", ""), "chat")
            except ValueError as e:
                return f"没保存：{e}"
            await self.emit({"type": "skills_changed"})
            return f"已保存技能 /{s['name']}"
        if name == "create_routine":
            kind = args.get("kind")
            trig = {"kind": kind, "times": args.get("times") or [], "days": args.get("days") or [],
                    "minutes": args.get("minutes")}
            try:
                r = routines.save(self.bot_id, {"name": args.get("name", ""), "prompt": args.get("prompt", ""),
                                                "skill": args.get("skill", ""), "trigger": trig})
            except (ValueError, TypeError) as e:
                return f"没建成：{e}"
            await self.emit({"type": "routines_changed"})
            when = routines.describe(r["trigger"])
            if r["next_run"]:
                when += "，下次 " + time.strftime("%m-%d %H:%M", time.localtime(r["next_run"]))
            if kind == "webhook":
                when += f"，地址 POST /hooks/{r['name']}"
            return f"已建定时任务「{r['name']}」：{when}"
        if name == "remember":
            return await self._remember(args)
        if name == "forget":
            mid = args.get("id")
            if not isinstance(mid, int) or not memories.visible(mid, self.bot_id):
                return f"没有 #{mid} 这条记忆"
            memories.delete(mid)
            await self.emit({"type": "memories_changed"})
            return f"已删除记忆 #{mid}"
        if name == "save_page":
            out = library.tool_save_page(self.bot_id, args)
            await self.emit({"type": "pages_changed"})
            return out
        if name == "list_tasks":
            return self._task_list()
        if name == "cancel_task":
            return self.cancel(args["task_id"], "用户要求取消")
        if name == "resolve_approval":
            return approvals.resolve(args["id"], args["approve"], args.get("note", ""), bool(args.get("always")))
        if name == "recall":
            return memory.recall(args["id"], self.chat["id"])
        if name == "look_at_screen":
            return await self._look()
        if name in DIRECT_PC:
            return await self._direct(name, args)
        return "未知工具"

    async def _remember(self, args: dict) -> str:
        owner = None if args.get("shared") else self.bot_id
        rid = args.get("replace_id")
        try:
            if isinstance(rid, int) and memories.visible(rid, self.bot_id):
                # 没说 shared 就保持原来的归属，免得改一条共享事实时把它变成私有的
                m = memories.update(rid, args.get("text", ""), owner if "shared" in args else memories.get(rid)["bot_id"])
            else:
                m = memories.add(owner, args.get("text", ""), "chat")
        except ValueError as e:
            return f"没记住：{e}"
        await self.emit({"type": "memories_changed"})
        return f"已记住 #{m['id']}（{'所有 Bot 共享' if m['shared'] else '只有你知道'}）：{m['text']}"

    async def _direct(self, name: str, args: dict):
        """Router 自己动手做简单的电脑操作；电脑被任务占着就不等，让模型决定要不要先取消任务。"""
        from types import SimpleNamespace
        from . import computer
        c = computer.get()
        key = f"chat-{self.bot_id}"
        if c.owner and c.owner != key:
            return f"电脑正被{c.owner_label}占用。如果用户的新指令取代了那个任务，先 cancel_task 再操作；否则告诉用户在排队。"
        if c.controller == "human":
            return "用户正在接手电脑，先别操作。"
        if (await c.status())["state"] != "running":
            await c.start()
        token = ctools.CLAIM.set(key)
        try:
            fake = SimpleNamespace(id=f"对话·{self.bot['name']}", bot_id=self.bot_id, chat_id=self.chat_id)
            async def noop(_): pass
            text, png = await ctools.call(fake, name, args, noop)
            await c.release(key)   # 一步一放，不挡后台任务
        finally:
            ctools.CLAIM.reset(token)
        return (text, png) if png else text

    async def _look(self):
        from . import computer
        c = computer.get()
        try:
            if (await c.status())["state"] != "running":
                return "电脑现在没开（已暂停或还没创建）。需要的话派任务，任务会自动开机。"
            png = await c.screenshot()
            title = (await c.exec("DISPLAY=:1 xdotool getactivewindow getwindowname 2>/dev/null", timeout=10)).stdout.strip()
        except Exception as e:
            return f"看不了屏幕：{e}"
        who = f"正在被{c.owner_label}使用" if c.owner else "空闲"
        if c.controller == "human":
            who = "用户正在接手操作"
        return (f"截图见下一条消息。当前窗口：{title or '无'}；电脑{who}。", png)

    def _capabilities(self) -> str:
        from . import catalog, mcp_hub, tools
        lines = [f"- {n}：{d}" for n, (_, d, _) in tools.TOOLS.items()]
        if (CFG.get("computer") or {}).get("enabled", True):
            lines.append("- 电脑：每个后台任务都能用所有 Bot 共用的一台电脑（Linux 桌面 + Chromium 浏览器 + 终端），"
                         "可以打开网页、点击、输入、登录后操作、下载文件、运行脚本。用户能在右栏实时看到屏幕。"
                         "遇到登录、验证码、付款这类必须用户本人做的步骤，Worker 会在任务中途暂停，"
                         "在对话里请用户「接手电脑」，用户交还后同一个任务接着做完。所以这类事情派一个任务就够，"
                         "不要拆成两个任务，也不要让用户做完后再来找你。"
                         "提交、付款、发送、删除这类有后果的操作，Worker 会先用 ask_approval 弹确认卡片，用户同意才做；"
                         "用户要求“先问我再做”时，把这个要求写进 goal 就行。"
                         "你自己也能用 look_at_screen 直接看屏幕：用户问屏幕上是什么、任务做到哪了，先看再答，"
                         "不要说你看不见。")
        groups: dict[str, list[str]] = {}
        for full in mcp_hub.TOOLS:
            server, _, tool = full.partition("__")
            groups.setdefault(server, []).append(tool)
        def label(n: str) -> str:
            t = catalog.title(n)
            return f"@{n}" if t == "@" + n else f"@{n}（{t}）"
        lines += [f"- 应用 {label(srv)}：{', '.join(ts)}" for srv, ts in groups.items()]
        off = [n for n in mcp_hub.all_apps() if n not in groups]
        if off:
            lines.append(f"- 未连接的应用（现在用不了，请用户到「应用」页面重新连接）：{', '.join(label(n) for n in off)}")
        return "\n".join(lines)

    async def dispatch(self, goal: str, mode: str = "single", background: bool = False,
                       on_done=None, label: str = "", chat_id: int | None = None, handoff: dict | None = None) -> Task:
        """派一个后台任务。定时任务也走这里（background=True，确认卡片会过期）。
        chat_id 是任务卡片和结果出现的对话（默认私聊）；确认卡片总是发到私聊。"""
        cid = chat_id or self.chat_id
        tid = await self.add("task", f"[任务已派发] {label + ' ' if label else ''}{goal}", cid, kind="dispatch",
                             mode=mode, status="running", **({"handoff_to": handoff["to"]} if handoff else {}))
        task = Task(id=tid, goal=goal, mode=mode, bot_id=self.bot_id, chat_id=cid, approval_chat_id=self.chat_id,
                    background=background, on_done=on_done, handoff=handoff)
        task.handle = asyncio.create_task(self._run(task))
        TASKS[tid] = task
        await self.emit({"type": "task", "task": task.public()}, cid)
        return task

    def _routine_list(self) -> str:
        return "\n".join(f"- {r['name']}（{routines.describe(r['trigger'])}{'' if r['enabled'] else '，已暂停'}）"
                         for r in routines.list_for(self.bot_id)) or "暂无"

    def _task_list(self) -> str:
        return "\n".join(f"#{t.id} [{'等待确认' if t.waiting_approval else t.status}] {t.mode} 步数={t.steps} {t.goal[:50]}"
                         for t in self.tasks.values()) or "暂无任务"

    async def _run(self, task: Task):
        try:
            task.result = await (run_heavy(task) if task.mode == "heavy" else run_worker(task, task.goal))
            task.status = "done"
            if not task.background:   # 定时任务有自己的运行记录，不记进“做过的事”
                memories.log_work(self.bot_id, task.id, task.goal)
        except asyncio.CancelledError:
            task.status = "cancelled"
            if task.handoff:
                from . import groups
                groups.unexpect(task.chat_id, task.handoff["to"])
            await self._done(task)
            return
        except Exception as e:
            task.status, task.result = "failed", f"任务失败：{e}"
        memory.update_meta(task.id, status=task.status)
        await self.add("task", f"[任务 #{task.id} 结果]\n{task.result}", task.chat_id, kind="result", task_id=task.id,
                       status=task.status, elapsed=round(time.time() - task.started))
        await self.emit({"type": "task", "task": task.public()}, task.chat_id)
        if task.handoff:
            from . import groups
            asyncio.create_task(groups.after_task(task))
        await self._done(task)

    async def _done(self, task: Task):
        if task.on_done:
            try:
                await task.on_done(task)
            except Exception:
                pass

    def cancel(self, tid: int, reason: str) -> str:
        t = self.tasks.get(tid)
        if not t or t.status != "running":
            return f"任务 #{tid} 不在运行中"
        approvals.cancel_for_task(tid)
        t.handle.cancel()
        t.status = "cancelled"
        memory.update_meta(tid, status="cancelled")
        asyncio.create_task(self.add("task", f"[任务 #{tid} 已取消：{reason}]", t.chat_id, kind="cancel", task_id=tid))
        asyncio.create_task(self.emit({"type": "task", "task": t.public()}, t.chat_id))
        return f"已取消 #{tid}"
