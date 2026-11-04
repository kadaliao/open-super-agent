"""Router：常驻会话，负责和用户对话、派发任务。自己不执行工具（对应 DeepSeek 设计里 Router 无 shell）。"""
import asyncio
import json
import time
from .computer import tools as ctools
from .config import CFG, call_kwargs, client_for
from . import approvals, bots, memory
from . import worker as worker_mod
from .worker import Task, run_worker, run_heavy

ROUTER_PROMPT = """你是「{name}」，用户的 AI 队友。{instructions}
你在对话里直接和用户交流；你不亲自查资料，而是：
- 闲聊、常识、能直接答的问题：直接回答。
- 需要联网查证、多步研究的：调用 dispatch 派给后台 Worker，然后简短告诉用户已开始（用户可以继续聊别的）。
  难题、高风险判断用 mode=heavy（多个 Worker 并行互审，更贵），其余用 single。
- 收到“触发器”通知时：值得做就派任务，不值得就一句话说明为什么忽略。
- 用户问进度用 list_tasks；要取消用 cancel_task；需要早先对话原文用 recall。
- 标有“系统通知”的消息是后台任务的状态，不是用户说的话；不要模仿它的格式。
- 永远优先回应用户最新的那条消息；之前的任务结果用户已经看过了。
- 每条新的用户请求都要单独判断，需要查证就派发新任务，不要因为别的任务在跑而忽略。
- 派任务时只写用户要的东西，不要自己加码（用户说“打开某页面”，就只是打开，不要变成“把整站数据抓下来”）。
- 电脑上的简单操作（打开网页、点一下、搜索框输入、按键、滚动、看屏幕）你自己用 computer_* 工具直接做，
  做完看截图回答；只有多步、耗时、需要终端或要等人接手的活才派任务。
- 用户的新指令和正在跑的任务冲突（比如换了要做的事）时，先 cancel_task 把旧任务停掉再做。
今天是 {date}。

后台 Worker 当前能用的工具（判断能不能做时以此为准）：
{capabilities}

当前任务：
{tasks}

等待用户确认的写操作（用户在聊天里同意或拒绝时，调用 resolve_approval；意思不明确就先问清楚，不要替用户做决定）：
{approvals}"""

DIRECT_PC = {"computer_screenshot", "computer_open_url", "computer_click", "computer_type",
             "computer_key", "computer_scroll"}

ROUTER_TOOLS = [*[s for s in ctools.SCHEMAS if s["function"]["name"] in DIRECT_PC],
    {"type": "function", "function": {"name": "dispatch", "description": "派发后台任务",
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "完整、自包含的任务描述"},
            "mode": {"type": "string", "enum": ["single", "heavy"]}}, "required": ["goal"]}}},
    {"type": "function", "function": {"name": "list_tasks", "description": "查看任务状态",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "cancel_task", "description": "取消任务",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "resolve_approval", "description": "处理一个待确认的写操作",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer"}, "approve": {"type": "boolean"},
            "note": {"type": "string", "description": "用户附带的意见，例如修改要求"}}, "required": ["id", "approve"]}}},
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

    @property
    def bot(self) -> dict:
        return bots.get(self.bot_id)

    @property
    def chat_id(self) -> int:
        return self.bot["chat_id"]

    @property
    def tasks(self) -> dict[int, Task]:
        return {i: t for i, t in TASKS.items() if t.bot_id == self.bot_id}

    async def emit(self, event: dict):
        if notify:
            await notify({"chat_id": self.chat_id, "bot_id": self.bot_id} | event)

    async def add(self, role: str, content: str, **meta) -> int:
        mid = memory.add(role, content, self.chat_id, self.bot_id, **meta)
        await self.emit({"type": "message", "message": {"id": mid, "ts": time.time(), "role": role,
                         "content": content, "meta": meta, "bot_id": self.bot_id}})
        return mid

    def _over_budget(self) -> bool:
        return memory.tokens_today() > CFG["limits"]["daily_token_budget"]

    async def handle_user(self, text: str):
        await self.add("user", text)
        async with self.lock:
            await self._think()

    async def handle_event(self, source: str, text: str):
        """触发器事件：作为系统通知进入对话，由 Bot 决定回复、派任务还是忽略。"""
        await self.add("task", f"[触发器 {source}] {text}", kind="trigger", source=source)
        async with self.lock:
            await self._think()

    async def _stream(self, client, model, msgs, sid: str, started: float, use_tools: bool = True):
        """流式调用一次模型，边收边推送正文和思考过程。返回 (正文, 思考, 工具调用列表)。"""
        stream = await client.chat.completions.create(
            model=model, messages=msgs, stream=True, **({"tools": ROUTER_TOOLS} if use_tools else {}),
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
                await self.emit({"type": "stream", "sid": sid, "thinking": rc})
            if d.content:
                content += d.content
                await self.emit({"type": "stream", "sid": sid, "delta": d.content})
            for tc in d.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                c["id"] = tc.id or c["id"]
                if tc.function:
                    c["name"] += tc.function.name or ""
                    c["args"] += tc.function.arguments or ""
        memory.record_usage("router", model, usage)
        return content, reasoning, [calls[k] for k in sorted(calls)]

    async def _think(self):
        client, model = client_for("router")
        b = self.bot
        msgs = [{"role": "system", "content": ROUTER_PROMPT.format(
            name=b["name"], instructions=b["instructions"] or "你的具体职责以用户在对话里交代的为准。",
            date=time.strftime("%Y-%m-%d"), tasks=self._task_list(), capabilities=self._capabilities(),
            approvals=approvals.summary(self.bot_id))}] + memory.context(self.chat_id)
        sid, started = f"s{time.time_ns()}", time.time()
        self._turn_tasks = []             # 本轮派发的任务，挂在这条回复下面显示
        shown, thinking, shots = "", "", []
        await self.emit({"type": "stream_start", "sid": sid})
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
                    await self.emit({"type": "stream", "sid": sid, "delta": "\n\n"})
            if not shown.strip():   # 工具轮数用完还没开口：关掉工具，逼它给一句回复
                content, reasoning, _ = await self._stream(client, model, msgs, sid, started, use_tools=False)
                shown, thinking = content, thinking + reasoning
        finally:
            await self.emit({"type": "stream_end", "sid": sid})
        await self.add("assistant", shown.strip(), reasoning=reasoning, thinking=thinking,
                       thought_s=round(time.time() - started, 1), sid=sid, tasks=self._turn_tasks)
    async def _tool(self, name: str, args: dict) -> str:
        if name == "dispatch":
            if self._over_budget():
                return "今日 token 预算已用完，没有派发。请告诉用户。"
            mode = args.get("mode", "single")
            tid = await self.add("task", f"[任务已派发] {args['goal']}", kind="dispatch", mode=mode, status="running")
            task = Task(id=tid, goal=args["goal"], mode=mode, bot_id=self.bot_id, chat_id=self.chat_id)
            task.handle = asyncio.create_task(self._run(task))
            TASKS[tid] = task
            getattr(self, "_turn_tasks", []).append(tid)
            await self.emit({"type": "task", "task": task.public()})
            return f"已派发任务 #{tid}"
        if name == "list_tasks":
            return self._task_list()
        if name == "cancel_task":
            return self.cancel(args["task_id"], "用户要求取消")
        if name == "resolve_approval":
            return approvals.resolve(args["id"], args["approve"], args.get("note", ""))
        if name == "recall":
            return memory.recall(args["id"], self.chat_id)
        if name == "look_at_screen":
            return await self._look()
        if name in DIRECT_PC:
            return await self._direct(name, args)
        return "未知工具"

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
        from . import mcp_hub, tools
        lines = [f"- {n}：{d}" for n, (_, d, _) in tools.TOOLS.items()]
        if (CFG.get("computer") or {}).get("enabled", True):
            lines.append("- 电脑：每个后台任务都能用所有 Bot 共用的一台电脑（Linux 桌面 + Chromium 浏览器 + 终端），"
                         "可以打开网页、点击、输入、登录后操作、下载文件、运行脚本。用户能在右栏实时看到屏幕。"
                         "遇到登录、验证码、付款这类必须用户本人做的步骤，Worker 会在任务中途暂停，"
                         "在对话里请用户「接手电脑」，用户交还后同一个任务接着做完。所以这类事情派一个任务就够，"
                         "不要拆成两个任务，也不要让用户做完后再来找你。"
                         "你自己也能用 look_at_screen 直接看屏幕：用户问屏幕上是什么、任务做到哪了，先看再答，"
                         "不要说你看不见。")
        groups: dict[str, list[str]] = {}
        for full in mcp_hub.TOOLS:
            server, _, tool = full.partition("__")
            groups.setdefault(server, []).append(tool)
        lines += [f"- 插件 {srv}：{', '.join(ts)}" for srv, ts in groups.items()]
        return "\n".join(lines)

    def _task_list(self) -> str:
        return "\n".join(f"#{t.id} [{'等待确认' if t.waiting_approval else t.status}] {t.mode} 步数={t.steps} {t.goal[:50]}"
                         for t in self.tasks.values()) or "暂无任务"

    async def _run(self, task: Task):
        try:
            task.result = await (run_heavy(task) if task.mode == "heavy" else run_worker(task, task.goal))
            task.status = "done"
        except asyncio.CancelledError:
            task.status = "cancelled"
            return
        except Exception as e:
            task.status, task.result = "failed", f"任务失败：{e}"
        memory.update_meta(task.id, status=task.status)
        await self.add("task", f"[任务 #{task.id} 结果]\n{task.result}", kind="result", task_id=task.id,
                       status=task.status, elapsed=round(time.time() - task.started))
        await self.emit({"type": "task", "task": task.public()})

    def cancel(self, tid: int, reason: str) -> str:
        t = self.tasks.get(tid)
        if not t or t.status != "running":
            return f"任务 #{tid} 不在运行中"
        approvals.cancel_for_task(tid)
        t.handle.cancel()
        t.status = "cancelled"
        memory.update_meta(tid, status="cancelled")
        asyncio.create_task(self.add("task", f"[任务 #{tid} 已取消：{reason}]", kind="cancel", task_id=tid))
        asyncio.create_task(self.emit({"type": "task", "task": t.public()}))
        return f"已取消 #{tid}"
