"""写操作前的人工确认，以及确认规则。

判断顺序：命中“先问我”规则 → 要确认；否则命中“自动允许”规则 → 直接执行；否则按应用的默认策略。
规则写工具全名（可以用通配符，filesystem__*），或者“工具:参数”（computer_shell:git *、filesystem__write_file:/data/workspace/*）：
冒号后面和工具的主参数比——终端是命令（cmd），其他工具是 schema 里排第一个、值是文字的参数（subject）。以“ *”结尾的也认前缀本身
（computer_shell:git * 也管单独一个 git）。
电脑工具也按规则走（见 computer/tools.py）：本机电脑默认上传文件、写文件、提交（browser_submit）都先问，终端每条命令都问，
只有带命令前缀的“自动允许”规则（computer_shell:git *）放行，而且命令里有 ; & | ` $ ( ) < > 换行这类连接、替换、重定向的
不算命中（不然 git status; rm -rf ~ 也混得过去）；Docker、Instabox 的电脑默认不问，靠 Worker 自己用 ask_approval。
确认卡片上同意时选作用域（resolve 的 scope）：once 只这一次；task 这个任务里同一个工具不再问，任务结束（做完、失败、取消）清掉；
minutes:N 接下来 N 分钟不再问；always 生成一条“自动允许”规则（终端是“总是允许这个命令前缀”，默认是命令的第一个词）。
task、minutes 只记在内存里（grants），服务重启就没了。once=True 的卡片（新建、修改、删除 Bot，Worker 的 ask_approval）只能一次一次地允许。
后台任务（定时、webhook 触发的）的卡片超时按拒绝处理；对话里派的任务一直等用户。
卡片总是出现在 Bot 的私聊里；群聊里派的任务，群里只显示一条提示（origin_chat_id 是任务所在的对话）。
讨论串里派的任务：origin_chat_id 是讨论串所在的私聊或群聊，origin_thread 是讨论串（前端在讨论串里也显示卡片）。
"""
import asyncio
import fnmatch
import itertools
import json
import posixpath
import re
import time
from . import i18n, memory
from .config import CFG
from .memory import conn

conn.execute("""create table if not exists rules(
  id integer primary key, pattern text, action text, note text default '', created real)""")
conn.commit()

_ids = itertools.count(1)
pending: dict[int, dict] = {}
grants: list[dict] = []   # 临时允许（task、minutes 作用域）：{"pattern", "task_id"（task 作用域）, "until"（minutes 作用域的到期时间）}
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
HOSTS = ("host", "linked")   # 用户自己的电脑（同 computer.base.HOSTS；这里不 import computer）
SHELL = "computer_shell"
UNSAFE = re.compile(r"[;&|<>`$()\n\r]")         # 终端命令里有这些（连接、管道、重定向、替换）就不按前缀放行
_PARTS = re.compile(r"[;&|\n\r]+|\$\(|[`()]")   # “先问我”的规则把命令按这些切开，哪一段命中都问
MAX_MINUTES = 24 * 60


def _params(tool: str) -> list[str]:
    """工具 schema 里参数的顺序。"""
    from . import registry
    t = registry.get(tool)
    schema = t.schema if t else None
    if not schema and tool.startswith(("computer_", "browser_", "desktop_")):
        from .computer import tools as ctools
        schema = next((x for x in ctools.SCHEMAS + ctools.DESK_SCHEMAS if x["function"]["name"] == tool), None)
    return list((((schema or {}).get("function") or {}).get("parameters") or {}).get("properties") or {})


def subject(tool: str, args: dict | None) -> str | None:
    """规则冒号后面比的值：终端是命令，其他工具是 schema 里排第一个、值是文字的参数。按 schema 的顺序而不是模型给的顺序，
    免得把正文排到路径前面混过去。以 / 开头的当路径，先把 .. 化掉。"""
    if not isinstance(args, dict):
        return None
    if tool == SHELL:
        v = args.get("cmd", args.get("命令"))
        return v.strip() if isinstance(v, str) else None
    order = [k for k in _params(tool) if k in args]
    for k in order + [k for k in args if k not in order]:
        v = args[k]
        if isinstance(v, str):
            return posixpath.normpath(v) if v.startswith("/") else v
    return None


def _hit(pattern: str, tool: str, value: str | None, strict: bool) -> bool:
    """规则命中没有。没有冒号的只比工具名；有冒号的还要比参数。strict（“自动允许”）：终端命令带连接、替换、重定向的不算命中。"""
    if pattern == tool:   # 整个工具（“本次任务内允许”、卡片上点的“总是允许”）；工具名里自己带冒号的也认得
        return True
    name, colon, arg = pattern.partition(":")
    if not colon:
        return fnmatch.fnmatchcase(tool, pattern)
    if value is None or not fnmatch.fnmatchcase(tool, name):
        return False
    if strict and tool == SHELL and UNSAFE.search(value):
        return False
    return fnmatch.fnmatchcase(value, arg) or (arg.endswith(" *") and value == arg[:-2])


def _asks(pattern: str, tool: str, value: str | None) -> bool:
    """“先问我”：终端命令整条或者按 ; && | $( 切开的任何一段命中都算。"""
    if _hit(pattern, tool, value, False):
        return True
    if tool == SHELL and value and ":" in pattern:
        return any(_hit(pattern, tool, part.strip(), False) for part in _PARTS.split(value) if part.strip())
    return False


def decide(tool: str, backend: str | None = None, args: dict | None = None) -> tuple[bool, str]:
    """返回 (是否要确认, 原因)。规则用通配符匹配工具全名（filesystem__*）或“工具:参数”（见文件开头）。
    backend：电脑工具在哪种电脑上用。args：这次调用的参数，“工具:参数”的规则要用。"""
    value = subject(tool, args)
    host_shell = tool == SHELL and backend in HOSTS
    rs = rules()
    for r in rs:
        if r["action"] == "ask" and _asks(r["pattern"], tool, value):
            return True, f"规则「{r['pattern']}」要求先问"
    for r in rs:   # 本机终端只认带命令前缀的规则：以前的 computer_shell、computer_* 这类不放行它
        if r["action"] == "allow" and (":" in r["pattern"] or not host_shell) and _hit(r["pattern"], tool, value, True):
            return False, f"规则「{r['pattern']}」自动允许"
    if host_shell:
        return True, "本机电脑的终端每条命令都要确认"
    if tool in PC_TOOLS:
        return backend in HOSTS and tool in PC_ASK, "默认策略"
    if tool in ASK:
        return True, "默认策略"
    from . import mcp_hub, registry
    t = registry.get(tool)
    if t and t.gated and t.owner.startswith("plugin:"):
        return True, "默认策略"   # 插件标了 gated 的工具（发邮件这类）默认每次都问，点“总是允许”后不再问
    return (tool in mcp_hub.CONFIRM), "默认策略"


# ---------- 临时允许 ----------
def granted(task_id, tool: str, value: str | None) -> bool:
    """卡片上点过“本次任务内允许”（同一个任务）或者 minutes:N 还没到期的。"""
    now = time.time()
    grants[:] = [g for g in grants if g["until"] is None or g["until"] > now]
    return any((g["task_id"] is None or g["task_id"] == task_id) and _hit(g["pattern"], tool, value, True) for g in grants)


def end_task(task_id):
    """任务结束：它的“本次任务内允许”清掉。"""
    grants[:] = [g for g in grants if g["task_id"] is None or g["task_id"] != task_id]


def first_word(cmd: str | None) -> str:
    """终端命令“总是允许这个前缀”的默认值：第一个词。带 ; | $ 这类符号的不给。"""
    w = (cmd or "").split()
    return w[0] if w and _prefix_ok(w[0]) else ""


def _prefix_ok(p: str) -> bool:
    return bool(p) and p[0] not in "*?[" and not UNSAFE.search(p)


def _scope(item: dict, ok: bool, scope) -> tuple[str, int]:
    """(作用域, 分钟数)。不认识的、这张卡片不能用的都按 once。minutes 是限时的 always，能 always 的卡片才能用。"""
    s, minutes = str(scope or "once").strip().lower(), 0
    if m := re.fullmatch(r"minutes:(\d{1,6})", s):
        s, minutes = "minutes", min(int(m[1]), MAX_MINUTES)
    allowed = item.get("scopes") or ["once"]
    if s == "minutes":
        s = "minutes" if minutes and "always" in allowed else "once"
    elif s not in allowed:
        s = "once"
    return (s, minutes) if ok and s != "once" else ("once", 0)


def _allow_pattern(item: dict, prefix) -> tuple[str, str]:
    """always / minutes 记的规则，和给人看的前缀。终端是 computer_shell:<前缀> *；前缀不能用时返回 ("", "")。"""
    if item["tool"] != SHELL:
        return item["tool"], ""
    p = " ".join(prefix.split()) if isinstance(prefix, str) else ""
    if not _prefix_ok(p):
        p = item.get("prefix") or ""
    if not p:
        return "", ""
    return (f"{SHELL}:{p}" if p.endswith("*") else f"{SHELL}:{p} *"), p.removesuffix("*").strip()


# ---------- 确认请求 ----------
async def request(task, tool: str, args: dict, kind: str = "approval", once: bool = False,
                  limit: int = 3000, editable: tuple[str, ...] = ()) -> tuple[bool, str]:
    """创建确认请求并等待用户决定。返回 (是否允许, 用户附言)。once：只能一次一次地允许（卡片上没有“本次任务内允许”“总是允许”）。
    limit：卡片上的参数（JSON）最多多长，超出的截掉。
    editable：用户能在卡片上直接改的参数（args 里的键，值是文字）。同意时改过的直接写回 args，调用方从 args 里读最终的值。
    这个任务里点过“本次任务内允许”、或者 minutes:N 还没到期的，不弹卡片直接允许。"""
    global last_done
    value = subject(tool, args)
    if kind == "approval" and not once and granted(task.id, tool, value):
        return True, ""
    scopes = ["once"]
    if kind == "approval" and not once:   # 冒充的任务（对话里画图、应用界面调用）id 是文字，没有“任务结束”，不给 task
        scopes += (["task"] if isinstance(task.id, int) else []) + (["always"] if tool != SHELL or first_word(value) else [])
    aid = next(_ids)
    fut = asyncio.get_running_loop().create_future()
    background = bool(getattr(task, "background", False))
    timeout = CFG["limits"].get("approval_timeout_s", 1800) if background else None
    chat_id = getattr(task, "approval_chat_id", 0) or task.chat_id
    th = memory.thread_of(task.chat_id)
    item = {"id": aid, "kind": kind, "task_id": task.id, "bot_id": task.bot_id, "chat_id": chat_id,
            "origin_chat_id": th[0] if th else task.chat_id, **({"origin_thread": task.chat_id} if th else {}), "tool": tool,
            "goal": task.goal[:80], "args": json.dumps(args, ensure_ascii=False, indent=1)[:limit], "ts": time.time(),
            "expires": time.time() + timeout if timeout else None, "future": fut, **({"once": True} if once else {}),
            **({"scopes": scopes} if kind == "approval" else {}),
            # 终端：“总是允许这个前缀”的默认前缀；带 ; | $( 这类符号的命令按前缀放行管不到（compound），下次还会问
            **({"prefix": first_word(value), **({"compound": True} if UNSAFE.search(value or "") else {})} if tool == SHELL else {}),
            **({"editable": [k for k in editable if isinstance(args.get(k), str)], "_args": args} if editable else {})}
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


def resolve(aid: int, ok: bool, note: str = "", always: bool = False, edits: dict | None = None,
            scope: str | None = None, prefix: str | None = None) -> str:
    """scope：同意时管多久，once / task / minutes:N / always（不写时看 always）；这张卡片不能用的按 once。
    prefix：终端命令“总是允许这个前缀”的前缀（不给或不能用就用命令的第一个词）。
    edits：用户在卡片上改过的参数（只认 request 时 editable 里的），同意时才算数。"""
    item = pending.get(aid)
    if not item or item["future"].done():
        return i18n.t("没有待确认的请求 #{id}", id=aid)
    scope, minutes = _scope(item, ok, scope or ("always" if always else "once"))
    pattern, shown = _allow_pattern(item, prefix) if scope in ("always", "minutes") else ("", "")
    if scope in ("always", "minutes") and not pattern:
        scope = "once"
    if scope == "always":
        add_rule(pattern, "allow", i18n.N_("在确认卡片上点了总是允许这个命令前缀") if item["tool"] == SHELL
                 else i18n.N_("在确认卡片上点了总是允许"))
    elif scope == "minutes":
        grants.append({"pattern": pattern, "task_id": None, "until": time.time() + minutes * 60})
    elif scope == "task":   # 同一个工具都不再问，终端也是整个放开（只到这个任务结束）
        grants.append({"pattern": item["tool"], "task_id": item["task_id"], "until": None})
    edited = {}
    if ok and isinstance(edits, dict):
        edited = {k: v.strip() for k, v in edits.items()
                  if k in item.get("editable", ()) and isinstance(v, str) and v.strip()
                  and v.strip() != item["_args"][k].strip()}
        item["_args"].update(edited)
    item["future"].set_result((ok, note))
    if notify:
        asyncio.create_task(notify({"type": "approval", "id": aid, "chat_id": item["chat_id"],
                                    "status": "approved" if ok else "rejected", "always": scope == "always", "scope": scope,
                                    **({"rule": pattern} if scope == "always" else {}), "edited": list(edited)}))
    out = i18n.t("已允许请求 #{id}", id=aid) if ok else i18n.t("已拒绝请求 #{id}", id=aid)
    if scope == "always":
        out += i18n.t("，以后「{prefix}」开头的命令自动允许", prefix=shown) if shown else i18n.t("，以后同类操作自动允许")
    elif scope == "minutes":
        out += i18n.t("，{n} 分钟内同类操作不再问", n=minutes)
    elif scope == "task":
        out += i18n.t("，这个任务里同类操作不再问")
    return out


def cancel_for_task(task_id: int):
    """任务被取消时，清掉它的待确认请求和“本次任务内允许”。"""
    end_task(task_id)
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
    return {k: v for k, v in item.items() if k != "future" and not k.startswith("_")}


def summary(bot_id: int | None = None) -> str:
    return "\n".join(f"#{a['id']}（任务 #{a['task_id']}）{a['tool']} 参数：{a['args'][:200]}"
                     for a in pending.values() if bot_id is None or a["bot_id"] == bot_id) or "无"
