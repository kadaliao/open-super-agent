"""写操作前的人工确认，以及确认规则。

判断顺序：命中“先问我”规则 → 要确认；否则命中“自动允许”规则 → 直接执行；否则按应用的默认策略。
电脑工具也按规则走（见 computer/tools.py）：本机电脑默认上传文件、写文件、提交（browser_submit）都先问，
终端每条命令都问、不能“总是允许”；Docker、Instabox 的电脑默认不问，靠 Worker 自己用 ask_approval。
确认卡片上点“总是允许”会生成一条针对这个工具的“自动允许”规则。
后台任务（定时、webhook 触发的）的卡片超时按拒绝处理；对话里派的任务一直等用户。
卡片总是出现在 Bot 的私聊里；群聊里派的任务，群里只显示一条提示（origin_chat_id 是任务所在的对话）。
讨论串里派的任务：origin_chat_id 是讨论串所在的私聊或群聊，origin_thread 是讨论串（前端在讨论串里也显示卡片）。
"""
import asyncio
import fnmatch
import itertools
import json
import time
from . import i18n, memory
from .config import CFG
from .memory import conn

conn.execute("""create table if not exists rules(
  id integer primary key, pattern text, action text, note text default '', created real)""")
conn.commit()

_ids = itertools.count(1)
pending: dict[int, dict] = {}
last_done = 0.0        # 最近一次确认结束的时间：之前那一下点击是你在点卡片，不算你在用电脑（computer/host.py）
notify = None          # 由 server 设置：async (event) -> None

DEFAULT_WORDS = ["create", "update", "delete", "remove", "archive", "append", "patch", "post", "write",
                 "edit", "move", "send", "insert", "upload", "publish", "comment", "set_", "add_",
                 "forward", "reply", "trash", "share", "cancel", "rename", "transfer"]


def default_confirm(rule, tool) -> bool:
    """应用的默认策略。confirm: auto —— 服务端标了只读的放行，其余按写入词判断；
    all —— 全部要确认；none —— 全部不用；也可以写一个词列表。"""
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


# ---------- 规则 ----------
def rules() -> list[dict]:
    # 自动生成的说明存的是中文原文，显示时按界面语言翻；用户自己写的原样
    return [{"id": i, "pattern": p, "action": a, "note": i18n.t(n) if n else n, "created": c}
            for i, p, a, n, c in conn.execute("select id, pattern, action, note, created from rules order by id")]


def add_rule(pattern: str, action: str, note: str = "") -> dict:
    pattern = pattern.strip()
    if not pattern:
        raise ValueError(i18n.t("规则不能为空"))
    if action not in ("allow", "ask"):
        raise ValueError(i18n.t("action 只能是 allow 或 ask"))
    old = conn.execute("select id from rules where pattern=? and action=?", (pattern, action)).fetchone()
    if old:
        return next(r for r in rules() if r["id"] == old[0])
    cur = conn.execute("insert into rules(pattern, action, note, created) values(?,?,?,?)",
                       (pattern, action, note, time.time()))
    conn.commit()
    return next(r for r in rules() if r["id"] == cur.lastrowid)


def delete_rule(rid: int):
    conn.execute("delete from rules where id=?", (rid,))
    conn.commit()


# 本机电脑默认先问的电脑工具。desktop：任务第一次用整个屏幕（真实的鼠标键盘）；desktop_submit：在屏幕上点“发送 / 付款”这类按钮、在输入框里按回车
PC_ASK = {"browser_upload", "computer_write_file", "browser_submit", "desktop", "desktop_submit"}
PC_TOOLS = PC_ASK | {"computer_shell"}
ASK = {"generate_image"}   # 默认每次都问的（画图要花钱）；点“总是允许”后不再问


def decide(tool: str, backend: str | None = None) -> tuple[bool, str]:
    """返回 (是否要确认, 原因)。规则用通配符匹配工具全名，例如 filesystem__* 。backend：电脑工具在哪种电脑上用。"""
    if tool == "computer_shell" and backend in ("host", "linked"):
        return True, "本机电脑的终端每条命令都要确认"
    rs = rules()
    for r in rs:
        if r["action"] == "ask" and fnmatch.fnmatchcase(tool, r["pattern"]):
            return True, f"规则「{r['pattern']}」要求先问"
    for r in rs:
        if r["action"] == "allow" and fnmatch.fnmatchcase(tool, r["pattern"]):
            return False, f"规则「{r['pattern']}」自动允许"
    if tool in PC_TOOLS:
        return backend in ("host", "linked") and tool in PC_ASK, "默认策略"
    if tool in ASK:
        return True, "默认策略"
    from . import mcp_hub
    return (tool in mcp_hub.CONFIRM), "默认策略"


# ---------- 确认请求 ----------
async def request(task, tool: str, args: dict, kind: str = "approval", once: bool = False,
                  limit: int = 3000) -> tuple[bool, str]:
    """创建确认请求并等待用户决定。返回 (是否允许, 用户附言)。once：只能一次一次地允许（卡片上没有“总是允许”）。
    limit：卡片上的参数（JSON）最多多长，超出的截掉。"""
    global last_done
    aid = next(_ids)
    fut = asyncio.get_running_loop().create_future()
    background = bool(getattr(task, "background", False))
    timeout = CFG["limits"].get("approval_timeout_s", 1800) if background else None
    chat_id = getattr(task, "approval_chat_id", 0) or task.chat_id
    th = memory.thread_of(task.chat_id)
    item = {"id": aid, "kind": kind, "task_id": task.id, "bot_id": task.bot_id, "chat_id": chat_id,
            "origin_chat_id": th[0] if th else task.chat_id, **({"origin_thread": task.chat_id} if th else {}), "tool": tool,
            "goal": task.goal[:80], "args": json.dumps(args, ensure_ascii=False, indent=1)[:limit], "ts": time.time(),
            "expires": time.time() + timeout if timeout else None, "future": fut, **({"once": True} if once else {})}
    pending[aid] = item
    task.waiting_approval = aid
    t0 = time.time()
    if notify:
        await notify(public(item) | {"type": "approval", "status": "pending"})
    try:
        ok, note = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        ok, note = False, f"{timeout // 60} 分钟内没有确认，按拒绝处理"
        if notify:
            await notify({"type": "approval", "id": aid, "status": "expired", "chat_id": chat_id})
    finally:
        last_done = time.time()
        pending.pop(aid, None)
        task.waiting_approval = None
        task.last_progress = time.time()
        if hasattr(task, "paused_s"):
            task.paused_s += time.time() - t0   # 等确认的时间不算进任务超时
    return ok, note


def resolve(aid: int, ok: bool, note: str = "", always: bool = False) -> str:
    item = pending.get(aid)
    if not item or item["future"].done():
        return i18n.t("没有待确认的请求 #{id}", id=aid)
    always = ok and always and item["kind"] == "approval" and not item.get("once")
    if always:
        add_rule(item["tool"], "allow", i18n.N_("在确认卡片上点了总是允许"))
    item["future"].set_result((ok, note))
    if notify:
        asyncio.create_task(notify({"type": "approval", "id": aid, "chat_id": item["chat_id"],
                                    "status": "approved" if ok else "rejected", "always": always}))
    return (i18n.t("已允许请求 #{id}", id=aid) if ok else i18n.t("已拒绝请求 #{id}", id=aid)) \
        + (i18n.t("，以后同类操作自动允许") if always else "")


def cancel_for_task(task_id: int):
    """任务被取消时，清掉它的待确认请求。"""
    for aid, item in list(pending.items()):
        if item["task_id"] == task_id and not item["future"].done():
            item["future"].set_result((False, "任务已取消"))
            if notify:
                asyncio.create_task(notify({"type": "approval", "id": aid, "status": "cancelled",
                                            "chat_id": item["chat_id"]}))


def cancel_for_bot(bot_id: int):
    """Bot 被删了：它还在等的确认请求都按拒绝处理。"""
    for aid, item in list(pending.items()):
        if item["bot_id"] == bot_id and not item["future"].done():
            item["future"].set_result((False, "Bot 已删除"))
            if notify:
                asyncio.create_task(notify({"type": "approval", "id": aid, "status": "cancelled",
                                            "chat_id": item["chat_id"]}))


def public(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "future"}


def summary(bot_id: int | None = None) -> str:
    return "\n".join(f"#{a['id']}（任务 #{a['task_id']}）{a['tool']} 参数：{a['args'][:200]}"
                     for a in pending.values() if bot_id is None or a["bot_id"] == bot_id) or "无"
