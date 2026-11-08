"""Bot 的资料库：它交给用户的东西，按时间倒序。右栏“资料库”标签用。

四种：
- page：资料库里这个 Bot 存的页面（pages.bot_id）
- image：它的回复、任务结果、画的图里带的图片（附件里有 media）
- file：它用 send_file 发的电脑里的文件（附件里只有 path）
- link：它的回复和任务结果正文里的 http(s) 链接

只看 Bot 自己说的话：用户的消息、派任务那条（目标多半是用户给的）、Bot 之间的私信和交接都不算。
同一个文件、图片、链接出现多次只留最新的一次（链接带上出现了几次）。
"""
import json
import re
from urllib.parse import urlsplit
from .memory import conn

SCAN = 3000   # 最多看最近多少条带附件或链接的消息
# 站内地址：页面已经单独列出，文件、图片走附件
INTERNAL = re.compile(r"^/(library|api|files|media|s)(/|$)")
_FENCE = re.compile(r"```.*?(```|$)", re.S)
_MD = re.compile(r"!?\[([^\]\n]{1,300})\]\((https?://(?:[^\s()]|\([^\s()]*\))+)(?:\s+\"[^\"\n]*\")?\)")
# 主机名只认 ASCII（“见https://a.com获取”不把“获取”算进去）；路径里可以有中文，碰到中文标点就断开
_BARE = re.compile(r"https?://[A-Za-z0-9.\-]+(?::\d+)?(?:[/?#][^\s<>\"'`|\\^{}\u3000-\u303f\uff00-\uffef]*)?")
_CJK_PUNCT = re.compile(r"[　-〿＀-￯‘-‟…]")
_TRAIL = ".,;:!?'\"*_~"
_PAIRS = {")": "(", "]": "[", "}": "{"}
_RESULT = re.compile(r"^\[任务 #(\d+) 结果\]")   # 旧的任务结果没有 meta.kind


def clean_url(u: str) -> str:
    """去掉粘在链接后面的标点：中文标点处截断，结尾的句号逗号去掉，多出来的右括号去掉（成对的保留）。"""
    if m := _CJK_PUNCT.search(u):
        u = u[:m.start()]
    while u:
        c = u[-1]
        if c in _TRAIL:
            u = u[:-1]
        elif c in _PAIRS and u.count(c) > u.count(_PAIRS[c]):
            u = u[:-1]
        else:
            break
    return u


def _title(text: str) -> str:
    """markdown 链接文字：去掉强调符号、反引号，压成一行。"""
    text = re.sub(r"[*_`~]+", "", text)
    return " ".join(text.split())[:160]


def links(text: str) -> list[tuple[str, str]]:
    """正文里的 http(s) 链接，按出现顺序去重：[(网址, 标题)]，标题是 markdown 链接的文字，裸链接为空。代码块里的不算。"""
    text = _FENCE.sub(" ", text or "")
    out: dict[str, str] = {}
    for m in _MD.finditer(text):
        u = clean_url(m.group(2))
        title = _title(m.group(1))
        if title == u or title.startswith(("http://", "https://")):
            title = ""
        if valid(u) and not out.get(u):
            out[u] = title
    text = _MD.sub(" ", text)
    for m in _BARE.finditer(text):
        u = clean_url(m.group(0))
        if valid(u):
            out.setdefault(u, "")
    return list(out.items())


def valid(u: str) -> bool:
    try:
        p = urlsplit(u)
    except ValueError:
        return False
    host = p.hostname or ""
    return p.scheme in ("http", "https") and ("." in host.strip(".") or host == "localhost")


def internal(u: str, own: set[str]) -> bool:
    """指向这个站点自己的页面、文件、接口的链接（资料库里已经有了，或者别人点不开）。"""
    p = urlsplit(u)
    local = p.netloc.lower() in own or (p.hostname or "") in ("localhost", "127.0.0.1")
    return local and bool(INTERNAL.match(p.path or "/"))


def _source(role: str, content: str, meta: dict) -> tuple[bool, int | None]:
    """这条消息算不算 Bot 交出来的东西，以及是哪个任务的结果。"""
    if role == "assistant":
        return True, None
    kind = meta.get("kind")
    if kind == "result":
        return True, meta.get("task_id")
    if kind == "image":
        return True, None
    if kind is None and (m := _RESULT.match(content or "")):
        return True, int(m.group(1))
    return False, None


def for_bot(bot_id: int, limit: int = 500, own_hosts: set[str] | None = None) -> list[dict]:
    own = {h.lower() for h in own_hosts or set() if h}
    items: dict[str, dict] = {}

    def put(key: str, item: dict):
        old = items.get(key)
        if old is None:
            items[key] = item | {"key": key, "count": 1}
        else:   # 消息是从新到旧看的：先见到的就是最新的，后面的只记次数
            old["count"] += 1

    rows = conn.execute("""select id, ts, role, content, meta, chat_id from messages
        where bot_id=? and role in ('assistant', 'task')
          and (meta like '%"attachments"%' or content like '%http%')
        order by id desc limit ?""", (bot_id, SCAN)).fetchall()
    for mid, ts, role, content, meta, chat_id in rows:
        m = json.loads(meta or "{}")
        ok, task_id = _source(role, content, m)
        if not ok:
            continue
        src = {"ts": ts, "chat_id": chat_id, "msg_id": mid, "task_id": task_id}
        for a in m.get("attachments") or []:
            if not isinstance(a, dict):
                continue
            if a.get("media"):
                put("image:" + a["media"], src | {"kind": "image", "title": a.get("name") or "", "media": a["media"],
                                                 "mime": a.get("mime"), "size": a.get("size"), "w": a.get("w"),
                                                 "h": a.get("h"), "path": a.get("path")})
            elif a.get("path"):
                put("file:" + a["path"], src | {"kind": "file", "title": a.get("name") or a["path"].rsplit("/", 1)[-1],
                                                "path": a["path"], "size": a.get("size")})
        for u, title in links(content):
            if internal(u, own):
                continue
            key = "link:" + u
            if key in items:
                items[key]["count"] += 1
                if not items[key]["title"] and title:   # 新的一次是裸链接、旧的有标题：用旧的标题
                    items[key]["title"] = title
                continue
            put(key, src | {"kind": "link", "title": title, "url": u, "host": urlsplit(u).hostname or ""})

    for pid, title, fmt, token, task_id, created, updated in conn.execute(
            "select id, title, format, share_token, task_id, created, updated from pages where bot_id=?", (bot_id,)):
        put(f"page:{pid}", {"kind": "page", "ts": created, "updated": updated, "title": title, "page_id": pid,
                            "format": fmt, "shared": bool(token), "task_id": task_id, "chat_id": None, "msg_id": None})

    out = sorted(items.values(), key=lambda x: x["ts"] or 0, reverse=True)
    return out[:limit]
