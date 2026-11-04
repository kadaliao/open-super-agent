"""Worker：隔离的 ReAct 循环，带工具，单独执行一个任务。"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from .config import CFG, call_kwargs, client_for
from . import approvals, library, memories, memory, mcp_hub, tools
from .computer import tools as ctools

PROGRESS = None   # 由 router 设置：async (task, text) -> None，把动作推送到任务卡片
PAGES_CHANGED = None   # 由 server 设置：async () -> None，资料库有新页面时通知前端

WORKER_PROMPT = """你是执行任务的 Worker。用工具查证事实，给出结论时附上来源链接。
需要读写用户的外部服务（应用）时，使用名字带“应用名__”前缀的工具；任务里写了“@应用名”就必须用那个应用。
写操作会先请用户确认；被拒绝时不要换个工具绕过去，按用户的意见调整或说明无法完成。
完成后直接输出最终答案（中文，简洁、结构清晰）。今天是 {date}。"""
PAGE_HINT = "任务要求做成页面或报告时，用 save_page 保存，最终答案里附上返回的链接。"



@dataclass
class Task:
    id: int
    goal: str
    mode: str = "single"                   # single | heavy
    bot_id: int = 0
    chat_id: int = 0
    status: str = "running"                # running | done | failed | cancelled
    started: float = field(default_factory=time.time)
    last_progress: float = field(default_factory=time.time)
    steps: int = 0
    recent_calls: list = field(default_factory=list)
    waiting_approval: int | None = None    # 正在等用户确认的请求 id
    background: bool = False               # 定时 / webhook 触发的任务：确认卡片会过期
    paused_s: float = 0                    # 等用户确认花掉的时间，不算进超时
    last_action: str = ""                  # 最近一个动作，显示在任务卡片上
    result: str = ""
    handle: asyncio.Task | None = None
    on_done: object = None                 # 定时任务用：async (task) -> None，任务结束时调用
    approval_chat_id: int = 0              # 确认卡片发到哪个对话：总是 Bot 的私聊（群聊里派的任务也一样）
    handoff: dict | None = None            # 群聊：做完后把结果交给谁 {"to": bot_id, "note": str}


def _public(self) -> dict:
    return {"id": self.id, "goal": self.goal, "mode": self.mode, "status": self.status, "steps": self.steps,
            "bot_id": self.bot_id, "chat_id": self.chat_id, "started": self.started,
            "waiting_approval": self.waiting_approval, "last_action": self.last_action, "elapsed": round(time.time() - self.started)}


Task.public = _public


async def run_worker(task: Task, goal: str, label: str = "") -> str:
    ctools.CLAIM.set(f"{task.id}:{label}")
    try:
        return await _run_worker(task, goal, label)
    finally:
        await ctools.release()   # 任务结束（含取消、失败）就把电脑让给排队的任务


async def _run_worker(task: Task, goal: str, label: str = "") -> str:
    client, model = client_for("worker")
    use_pc = (CFG.get("computer") or {}).get("enabled", True)
    pages = not label   # Heavy 模式的几个 Worker 只出草稿，不各自存页面
    system = WORKER_PROMPT.format(date=time.strftime("%Y-%m-%d")) + ("\n" + PAGE_HINT if pages else "") \
        + ("\n\n" + ctools.SYSTEM_HINT if use_pc else "")
    if known := memories.worker_block(task.bot_id):
        system += f"\n\n关于用户（长期记忆，做事时遵守其中的偏好）：\n{known}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": goal}]
    schemas = tools.schemas() + ([library.SAVE_PAGE] if pages else []) + (ctools.SCHEMAS if use_pc else [])

    async def progress(text: str):
        task.last_action = text
        if PROGRESS:
            await PROGRESS(task, text)

    for _ in range(CFG["limits"]["worker_max_steps"]):
        ctools.prune_images(msgs)
        resp = await client.chat.completions.create(model=model, messages=msgs, tools=schemas,
                                                      **call_kwargs("worker"))
        memory.record_usage("worker", model, resp.usage, task.id)
        m = resp.choices[0].message
        task.steps += 1
        task.last_progress = time.time()
        if not m.tool_calls:
            return m.content or ""
        msgs.append(m.model_dump(exclude_none=True))
        shots = []   # 截图要等这一轮的工具结果都追加完再附上
        for call in m.tool_calls:
            sig = f"{call.function.name}:{call.function.arguments}"
            task.recent_calls = (task.recent_calls + [sig])[-6:]
            fn = tools.lookup(call.function.name)
            try:
                args = json.loads(call.function.arguments or "{}")
                if call.function.name in ctools.NAMES:
                    out, shot = await ctools.call(task, call.function.name, args, progress)
                    if shot:
                        shots.append(shot)
                    msgs.append({"role": "tool", "tool_call_id": call.id, "content": out[:8000]})
                    task.last_progress = time.time()
                    continue
                if call.function.name == "save_page" and pages:
                    await progress("保存页面：" + str(args.get("title", ""))[:40])
                    msgs.append({"role": "tool", "tool_call_id": call.id,
                                 "content": library.tool_save_page(task.bot_id, args, task.id)})
                    if PAGES_CHANGED:
                        await PAGES_CHANGED()
                    continue
                await progress({"web_search": "搜索：", "fetch_url": "读取网页："}.get(call.function.name, call.function.name + " ")
                               + str(args.get("query") or args.get("url") or "")[:60])
                if fn and call.function.name in mcp_hub.TOOLS and approvals.decide(call.function.name)[0]:
                    ok, note = await approvals.request(task, call.function.name, args)
                    if not ok:
                        msgs.append({"role": "tool", "tool_call_id": call.id,
                                     "content": f"用户拒绝了这个操作，没有执行。{note}".strip()})
                        continue
                out = await fn(**args) if fn else "未知工具"
            except Exception as e:  # 工具出错交给模型自己处理
                out = f"工具出错：{e}"
            msgs.append({"role": "tool", "tool_call_id": call.id, "content": out[:8000]})
        if shots:
            msgs.append(ctools.image_message(shots[-1]))
    msgs.append({"role": "user", "content": "步数用完了，请根据已有信息给出最终答案。"})
    resp = await client.chat.completions.create(model=model, messages=msgs, **call_kwargs("worker"))
    memory.record_usage("worker", model, resp.usage, task.id)
    return resp.choices[0].message.content or ""


async def run_heavy(task: Task) -> str:
    """Heavy 模式：N 个 Worker 独立并行解题，Reviewer 互审后汇总。"""
    n = CFG["heavy"]["workers"]
    angles = ["从最直接的角度", "从反面/质疑的角度核查", "从数据和一手来源角度", "从实践/落地角度"]
    drafts = await asyncio.gather(*[
        run_worker(task, f"{task.goal}\n\n（你是第 {i+1} 个独立研究员，请{angles[i % len(angles)]}回答）", f"h{i}")
        for i in range(n)], return_exceptions=True)
    drafts = [d for d in drafts if isinstance(d, str)]
    client, model = client_for("reviewer")
    joined = "\n\n".join(f"=== 方案 {i+1} ===\n{d}" for i, d in enumerate(drafts))
    resp = await client.chat.completions.create(**call_kwargs("reviewer"), model=model, messages=[
        {"role": "system", "content": "你是审稿人。比较几份独立答案，找出矛盾和错误，合成一份最可靠的最终答案，保留来源链接。"},
        {"role": "user", "content": f"问题：{task.goal}\n\n{joined}"}])
    memory.record_usage("reviewer", model, resp.usage, task.id)
    return resp.choices[0].message.content or ""
