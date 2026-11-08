"""工具调用记录：Bot 每调一次工具记一行（什么时候、哪个 Bot、调了什么、参数和结果的开头），对话里能问“昨天谁调了 send_email”。

记在插件自己的数据目录里，一天一个文件（YYYY-MM-DD.jsonl，一行一个 JSON），留 keep_days 天（默认 30），更早的自动删。
看起来像密钥的会先抹掉再写：名字里带 password、token、secret、key、authorization 的参数，长串的随机字符，
sk-、ghp_ 这类开头的 key，网址里的密码。抹不干净的情况总会有，这些文件别发给别人。
Bot 能用 audit_search 工具按工具名、Bot、关键词、日期查，一次最多 50 条。
只是旁观（订阅 tool_done 事件）：拦不住、也改不了工具调用。
"""
import asyncio
import json
import re
import threading
import time
from datetime import date, timedelta
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "工具调用记录", "version": "1.0", "author": "open-super-agent", "config": {"keep_days": 30}}
MAX_ARGS = 500          # 参数存多少字
MAX_RESULT = 300        # 结果存多少字
MAX_QUEUE = 5000        # 写得太慢时最多攒多少条，再多就丢
MAX_HITS = 50
STATUS = {"done": "成功", "error": "出错", "rejected": "被拒绝"}
SCOPE = {"router": "对话里", "worker": "后台任务", "applet": "小应用"}
HIDDEN = "***"

# ---------- 抹掉密钥 ----------
_SECRET_WORDS = ("password", "passwd", "passphrase", "token", "secret", "authorization", "cookie", "credential",
                 "apikey", "accesskey", "privatekey")
_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),                   # OpenAI、Stripe 这类
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),                      # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                 # AWS
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"),   # JWT
]
_BEARER = re.compile(r"\b([Bb]earer|Basic)(\s+)([A-Za-z0-9._~+/\-]{8,}=*)")
_PAIR = re.compile(r"(?i)\b([\w-]*(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|"
                   r"authorization|cookie)[\w-]*)(\"?\s*[:=]\s*\"?)([^\s\"'&,;}]+)")
_URL_PASS = re.compile(r"(://[^/\s:@]+:)[^@\s/]+@")
# 不含 /：长路径、网址按段算，一段一段看像不像随机串
_LONG = re.compile(r"(?<![A-Za-z0-9+=_\-])[A-Za-z0-9+=_\-]{32,}(?![A-Za-z0-9+=_\-])")


def secret_key(k) -> bool:
    """参数名像不像存密钥的：password、api_key、apiKey、Authorization、key……（keyword、monkey 不算）。"""
    k = str(k)
    flat = re.sub(r"[^a-z0-9]", "", k.lower())
    return flat in ("key", "pwd", "pass") or any(w in flat for w in _SECRET_WORDS) or \
        bool(re.search(r"(^|[_\-.\s])key$|[a-z0-9]Key$", k))


def _random_looking(s: str) -> bool:
    """32 个字符以上、大小写字母和数字混着、没几个连字符的一串：正常的文字、带日期的长标题、哈希（全小写）不会这样。"""
    return bool(re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s)) and \
        s.count("-") + s.count("_") <= 4


def redact_text(s: str) -> str:
    s = _URL_PASS.sub(rf"\1{HIDDEN}@", s)
    s = _PAIR.sub(lambda m: m[1] + m[2] + HIDDEN, s)
    s = _BEARER.sub(lambda m: m[1] + m[2] + HIDDEN if re.search(r"[\d+/=._\-]", m[3]) else m[0], s)
    for p in _PATTERNS:
        s = p.sub(HIDDEN, s)
    return _LONG.sub(lambda m: HIDDEN if _random_looking(m[0]) else m[0], s)


def redact(v, depth: int = 0):
    """参数（dict / list / 文字）里像密钥的都换成 ***。"""
    if depth > 6:
        return "…"
    if isinstance(v, dict):
        return {k: HIDDEN if secret_key(k) and x not in (None, "", [], {}) else redact(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [redact(x, depth + 1) for x in v[:50]]
    if isinstance(v, str):
        return redact_text(v[:2000])
    return v


def _cut(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def entry(ts: float, scope: str, bot: dict, chat_id, task, name: str, args, result, status: str) -> dict:
    """一次工具调用 → 存进文件的一行。"""
    try:
        a = json.dumps(redact(args if isinstance(args, dict) else {"args": args}), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        a = "{}"
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), "bot": (bot or {}).get("name") or "",
            "bot_id": (bot or {}).get("id"), "scope": scope, "chat_id": chat_id,
            "task_id": task.get("id") if isinstance(task, dict) else None, "tool": name, "status": status,
            "args": _cut(a, MAX_ARGS), "result": _cut(redact_text(str(result or "")[:2000]), MAX_RESULT)}


# ---------- 文件 ----------
_lock = threading.Lock()   # 停用时后台线程可能还在写，和收尾的那次别交错


def write(folder, lines: list[dict]):
    """按日期追加到对应的文件里（跨午夜的一批会分到两个文件）。"""
    by_day: dict[str, list[str]] = {}
    for e in lines:
        by_day.setdefault(e["ts"][:10], []).append(json.dumps(e, ensure_ascii=False))
    with _lock:
        folder.mkdir(parents=True, exist_ok=True)
        for day, rows in by_day.items():
            with open(folder / f"{day}.jsonl", "a", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")


def prune(folder, keep_days: int, today: date | None = None) -> list[str]:
    """删掉 keep_days 天以前的文件，返回删了哪些。"""
    if not folder.is_dir():
        return []
    first = (today or date.today()) - timedelta(days=max(1, keep_days) - 1)
    gone = []
    for p in folder.glob("*.jsonl"):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d < first:
            p.unlink(missing_ok=True)
            gone.append(p.name)
    return sorted(gone)


def search(folder, tool: str = "", bot: str = "", keyword: str = "", day: str = "", days: int = 7,
           status: str = "", limit: int = 20, today: date | None = None) -> list[dict]:
    """从新到旧找，找够 limit 条就停。tool、keyword 是包含就算，bot 是名字（不分大小写）。"""
    today = today or date.today()
    if day:
        dates = [date.fromisoformat(day)]
    else:
        dates = [today - timedelta(days=i) for i in range(max(1, min(int(days or 7), 366)))]
    tool, bot, keyword = tool.lower().strip(), bot.lower().strip().lstrip("@"), keyword.lower().strip()
    out = []
    for d in dates:
        p = folder / f"{d.isoformat()}.jsonl"
        if not p.is_file():
            continue
        for line in reversed(p.read_text(encoding="utf-8", errors="replace").splitlines()):
            if keyword and keyword not in line.lower():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if tool and tool not in str(e.get("tool", "")).lower():
                continue
            if bot and bot != str(e.get("bot", "")).lower():
                continue
            if status and e.get("status") != status:
                continue
            out.append(e)
            if len(out) >= limit:
                return out
    return out


def line(e: dict) -> str:
    where = SCOPE.get(e.get("scope"), e.get("scope") or "")
    if e.get("task_id"):
        where += f" #{e['task_id']}"
    return (f"{e.get('ts', '')} {e.get('bot') or '?'}（{where}）{e.get('tool', '')} "
            f"{STATUS.get(e.get('status'), e.get('status') or '')}　参数 {_cut(e.get('args', ''), 160)}"
            f"　结果 {_cut(e.get('result', ''), 120)}")


SCHEMA = {"type": "function", "function": {
    "name": "audit_search",
    "description": "查 Bot 们调用工具的记录（工具调用记录插件记的）：哪天几点、哪个 Bot、调了什么工具、参数和结果的开头、成没成功。"
                   "用户问“昨天谁调了 send_email”“这周有没有删过文件”时用。新的在前，最多 50 条。"
                   "date 写 YYYY-MM-DD（“昨天”就填昨天的日期），不写就看最近 days 天。",
    "parameters": {"type": "object", "properties": {
        "tool": {"type": "string", "description": "工具名，包含这段就算，比如 send_email"},
        "bot": {"type": "string", "description": "Bot 的名字"},
        "keyword": {"type": "string", "description": "参数或结果里包含的文字"},
        "date": {"type": "string", "description": "只看这一天，YYYY-MM-DD"},
        "days": {"type": "integer", "description": "不写 date 时往前看几天，默认 7"},
        "status": {"type": "string", "enum": ["done", "error", "rejected"]},
        "limit": {"type": "integer", "description": "最多几条，默认 20，最多 50"}}}}}


def apply(ctx):
    keep = int(ctx.config.get("keep_days") or 30)
    if keep < 1:
        raise ValueError("keep_days 至少是 1")
    folder = ctx.data_dir
    queue: asyncio.Queue = asyncio.Queue(MAX_QUEUE)
    dropped = [0]

    def on_tool(scope, bot, chat_id, name, args, result, status, task=None, **_):
        # 在事件循环里只做最少的事：放进队列。抹密钥、写文件在后台线程里做
        if name == "audit_search":
            return
        try:
            queue.put_nowait((time.time(), scope, bot, chat_id, task, name, args,
                              result if isinstance(result, str) else str(result), status))
        except asyncio.QueueFull:
            dropped[0] += 1

    def flush(items: list):
        write(folder, [entry(*it) for it in items])

    async def writer():
        await asyncio.to_thread(prune, folder, keep)
        last_prune = date.today()
        while True:
            items = [await queue.get()]
            while not queue.empty() and len(items) < 500:
                items.append(queue.get_nowait())
            try:
                await asyncio.to_thread(flush, items)
            except Exception as e:
                ctx.log.warning("工具调用记录没写进去（%d 条）：%s", len(items), e)
            if dropped[0]:
                ctx.log.warning("写得太慢，丢了 %d 条工具调用记录", dropped[0])
                dropped[0] = 0
            if date.today() != last_prune:
                last_prune = date.today()
                await asyncio.to_thread(prune, folder, keep)

    def drain():
        """停用时把还没写的写掉（后台任务已经先取消了）。"""
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        if items:
            try:
                flush(items)
            except Exception as e:
                ctx.log.warning("工具调用记录没写进去（%d 条）：%s", len(items), e)

    async def run(call):
        a = call.args
        try:
            hits = await asyncio.to_thread(
                search, folder, str(a.get("tool") or ""), str(a.get("bot") or ""), str(a.get("keyword") or ""),
                str(a.get("date") or "").strip(), int(a.get("days") or 7), str(a.get("status") or ""),
                max(1, min(int(a.get("limit") or 20), MAX_HITS)))
        except ValueError:
            return "没查：date 要写成 YYYY-MM-DD，days、limit 要是数字"
        if not hits:
            return "没有找到符合条件的工具调用记录（只记了装上这个插件以后的，最多留 %d 天）。" % keep
        return f"找到 {len(hits)} 条（新的在前）：\n" + "\n".join(line(e) for e in hits)

    ctx.effect(drain)                 # 先登记：撤销时反着来，后台写入先停，再把剩下的写掉
    ctx.on("tool_done", on_tool)
    ctx.task(writer())
    ctx.tool(Tool("audit_search", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="查 Bot 调用工具的记录：哪天、哪个 Bot、调了什么、成没成功",
                  progress=lambda a: "查工具调用记录"))
