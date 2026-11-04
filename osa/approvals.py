"""写操作前的人工确认。

Worker 调用需要确认的工具时，在这里创建一个待确认请求并等待；
用户在网页上点“允许/拒绝”，或在聊天里告诉 Router，请求就会被处理。
超时没人处理按拒绝算。
"""
import asyncio
import itertools
import json
import time
from .config import CFG

_ids = itertools.count(1)
pending: dict[int, dict] = {}
notify = None          # 由 server 设置：async (event) -> None

DEFAULT_WORDS = ["create", "update", "delete", "remove", "archive", "append", "patch", "post", "write",
                 "edit", "move", "send", "insert", "upload", "publish", "comment", "set_", "add_"]


def needs_confirm(server: str, tool) -> bool:
    """按插件配置判断某个 MCP 工具是否需要确认。

    confirm: auto（默认）—— 服务端标了只读的放行，其余按写入词判断
             all —— 全部要确认；none —— 全部不用确认；也可以写一个词列表
    """
    rule = (CFG.get("mcp_servers", {}).get(server) or {}).get("confirm", "auto")
    if rule == "none":
        return False
    if rule == "all":
        return True
    ann = getattr(tool, "annotations", None)
    if rule == "auto" and ann is not None:
        ro = getattr(ann, "read_only_hint", None)
        if ro is None:
            ro = getattr(ann, "readOnlyHint", None)
        if ro is True:
            return False
    words = DEFAULT_WORDS if rule == "auto" else rule
    name = tool.name.lower()
    return any(w.lower() in name for w in words)


async def request(task, tool: str, args: dict, kind: str = "approval") -> tuple[bool, str]:
    """创建确认请求并等待用户决定。返回 (是否允许, 用户附言)。"""
    aid = next(_ids)
    fut = asyncio.get_running_loop().create_future()
    item = {"id": aid, "kind": kind, "task_id": task.id, "bot_id": task.bot_id, "chat_id": task.chat_id, "tool": tool, "goal": task.goal[:80],
            "args": json.dumps(args, ensure_ascii=False, indent=1)[:3000], "ts": time.time(), "future": fut}
    pending[aid] = item
    task.waiting_approval = aid
    if notify:
        await notify(public(item) | {"type": "approval", "status": "pending"})
    timeout = CFG["limits"].get("approval_timeout_s", 1800)
    try:
        ok, note = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        ok, note = False, f"{timeout // 60} 分钟内没有确认，按拒绝处理"
        if notify:
            await notify({"type": "approval", "id": aid, "status": "expired", "chat_id": task.chat_id})
    finally:
        pending.pop(aid, None)
        task.waiting_approval = None
        task.last_progress = time.time()
    return ok, note


def resolve(aid: int, ok: bool, note: str = "") -> str:
    item = pending.get(aid)
    if not item or item["future"].done():
        return f"没有待确认的请求 #{aid}"
    item["future"].set_result((ok, note))
    if notify:
        asyncio.create_task(notify({"type": "approval", "id": aid, "chat_id": item["chat_id"],
                                    "status": "approved" if ok else "rejected"}))
    return f"已{'允许' if ok else '拒绝'}请求 #{aid}"


def cancel_for_task(task_id: int):
    """任务被取消时，清掉它的待确认请求。"""
    for aid, item in list(pending.items()):
        if item["task_id"] == task_id and not item["future"].done():
            item["future"].set_result((False, "任务已取消"))
            if notify:
                asyncio.create_task(notify({"type": "approval", "id": aid, "status": "cancelled",
                                            "chat_id": item["chat_id"]}))


def public(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "future"}


def summary(bot_id: int | None = None) -> str:
    return "\n".join(f"#{a['id']}（任务 #{a['task_id']}）{a['tool']} 参数：{a['args'][:200]}"
                     for a in pending.values() if bot_id is None or a["bot_id"] == bot_id) or "无"
