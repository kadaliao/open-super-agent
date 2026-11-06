"""按意思找记忆和资料库页面。

两路检索，按排名融合（RRF）：
- 关键词：SQLite FTS5，不需要任何额外服务。中文没有空格，入库前把每段中文切成相邻两个字一组
  （“今天天气” → 今天 天天 天气），英文、数字按词，统一小写；BM25 排序。
- 向量（可选）：设置里选了 embedding 服务（任何兼容 OpenAI /embeddings 接口的，比如 OpenAI、硅基流动、Ollama）就开启。
  向量在后台算（embed_loop），归一化后存在 SQLite 里；查询时算一次问句的向量，和所有条目算点积。几千条以内直接算就很快。

索引不靠到处埋钩子：每次检索前 sync() 比一遍记忆和页面的 (id, updated)，变了的重新切块、分词，删了的去掉。
所以从备份恢复、删除 Bot 这类绕开 memories / library 的改动也不会漏。页面按段切成约 600 字一块，最多索引前 200 块。
"""
import asyncio
import math
import operator
import re
import struct
import time
from .config import CFG
from .memory import conn

conn.execute("""create table if not exists search_docs(
  id integer primary key, kind text, ref integer, chunk integer, bot_id integer, sub text default '',
  src_updated real, title text default '', text text, model text default '', vec blob)""")
conn.execute("create index if not exists search_docs_ref on search_docs(kind, ref)")
conn.execute("create virtual table if not exists search_fts using fts5(tokens)")
conn.commit()

CHUNK = 600
MAX_CHUNKS = 200
BATCH = 32
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+|[a-z0-9_]+")
_wake = asyncio.Event()
_dot = getattr(math, "sumprod", None) or (lambda a, b: sum(map(operator.mul, a, b)))   # 3.12 起有 C 实现的 sumprod
status = {"error": "", "last": 0.0, "failed": 0.0}


# ---------- 分词 ----------
def tokens(text: str) -> list[str]:
    out = []
    for m in _CJK.finditer((text or "").lower()):
        s = m[0]
        if s[0] > "　":
            out += [s[i:i + 2] for i in range(len(s) - 1)] or [s]
        else:
            out.append(s)
    return out


def _match(q: str) -> str:
    """问句 → FTS5 查询：每个词加引号，用 OR 连起来。"""
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens(q)))


# ---------- 切块 ----------
def _plain(content: str, fmt: str) -> str:
    if fmt == "html":
        content = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", content)
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"&nbsp;|&#160;", " ", content)
    return content


def chunks(text: str) -> list[str]:
    """按空行分段，凑到 CHUNK 字左右一块；太长的段硬切。"""
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text or ""):
        para = " ".join(para.split())
        if not para:
            continue
        while len(para) > CHUNK * 2:
            out.append(para[:CHUNK])
            para = para[CHUNK:]
        if cur and len(cur) + len(para) > CHUNK:
            out.append(cur)
            cur = ""
        cur = f"{cur} {para}".strip()
        if len(out) >= MAX_CHUNKS:
            break
    if cur and len(out) < MAX_CHUNKS:
        out.append(cur)
    return out[:MAX_CHUNKS]


# ---------- 同步索引 ----------
def _drop(ids: list[int]):
    for i in ids:
        conn.execute("delete from search_fts where rowid=?", (i,))
    conn.executemany("delete from search_docs where id=?", [(i,) for i in ids])


def _add(kind: str, ref: int, bot_id, sub: str, updated: float, title: str, parts: list[str]):
    for n, text in enumerate(parts):
        did = conn.execute("insert into search_docs(kind, ref, chunk, bot_id, sub, src_updated, title, text) values(?,?,?,?,?,?,?,?)",
                           (kind, ref, n, bot_id, sub, updated, title, text)).lastrowid
        conn.execute("insert into search_fts(rowid, tokens) values(?, ?)", (did, " ".join(tokens(f"{title} {text}"))))


def sync() -> int:
    """让索引跟上记忆和页面：返回改动了多少条。"""
    have: dict[tuple, tuple] = {}
    for kind, ref, upd, did in conn.execute("select kind, ref, src_updated, id from search_docs"):
        u, ids = have.get((kind, ref), (upd, []))
        have[(kind, ref)] = (u, ids + [did])
    src = {("memory", i): u for i, u in conn.execute("select id, updated from memories")}
    src |= {("page", i): u for i, u in conn.execute("select id, updated from pages")}
    changed = 0
    for key, (u, ids) in have.items():
        if key not in src or src[key] != u:
            _drop(ids)
            changed += 1
    for (kind, ref), u in src.items():
        if (kind, ref) in have and have[(kind, ref)][0] == u:
            continue
        if kind == "memory":
            r = conn.execute("select bot_id, kind, text from memories where id=?", (ref,)).fetchone()
            if r:
                _add(kind, ref, r[0], r[1], u, "", [r[2]])
        else:
            r = conn.execute("select bot_id, title, format, content from pages where id=?", (ref,)).fetchone()
            if r:
                _add(kind, ref, r[0], "", u, r[1] or "", chunks(_plain(r[3] or "", r[2])) or [r[1] or ""])
        changed += 1
    if changed:
        conn.commit()
        _wake.set()
    return changed


# ---------- 向量 ----------
def embed_config() -> dict | None:
    """设置里选了 embedding 服务就返回 {provider, model}。"""
    e = CFG.get("embedding") or {}
    if e.get("provider") in CFG["providers"] and (e.get("model") or "").strip():
        return {"provider": e["provider"], "model": e["model"].strip()}
    return None


async def embed(texts: list[str]) -> list[list[float]]:
    from . import providers
    e = embed_config()
    client = providers.client(e["provider"], timeout=60)
    r = await client.embeddings.create(model=e["model"], input=[t[:8000] or " " for t in texts])
    out = []
    for d in sorted(r.data, key=lambda d: d.index):
        v = d.embedding
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unpack(b: bytes) -> tuple:
    return struct.unpack(f"{len(b) // 4}f", b)


async def embed_loop():
    """后台给还没有向量（或者换了模型）的条目算向量。没配 embedding 时什么都不做。"""
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), 300)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
        while (e := embed_config()):
            rows = conn.execute("select id, title, text from search_docs where model != ? or vec is null limit ?",
                                (e["model"], BATCH)).fetchall()
            if not rows:
                break
            try:
                vecs = await embed([f"{t} {x}".strip() for _, t, x in rows])
            except Exception as err:
                status.update(error=f"{type(err).__name__}: {str(err)[:200]}", failed=time.time())
                await asyncio.sleep(60)
                break
            for (did, _, _), v in zip(rows, vecs):
                conn.execute("update search_docs set vec=?, model=? where id=?", (_pack(v), e["model"], did))
            conn.commit()
            status.update(error="", last=time.time(), failed=0.0)
            await asyncio.sleep(0.2)


def info() -> dict:
    e = embed_config()
    total = conn.execute("select count(*) from search_docs").fetchone()[0]
    done = conn.execute("select count(*) from search_docs where vec is not null and model=?", (e["model"],)).fetchone()[0] if e else 0
    return {"embedding": e, "chunks": total, "embedded": done, "error": status["error"] if e else ""}


# ---------- 检索 ----------
def _visible(kinds: tuple, bot_id) -> tuple[str, list]:
    where, args = [f"d.kind in ({','.join('?' * len(kinds))})"], list(kinds)
    if bot_id is not None and "memory" in kinds:   # 记忆：只看这个 Bot 自己的和共享的；页面都能看
        where.append("(d.kind != 'memory' or d.bot_id is null or d.bot_id = ?)")
        args.append(bot_id)
    return " and ".join(where), args


async def search(q: str, bot_id: int | None = None, kinds: tuple = ("memory", "page"), limit: int = 8,
                 subs: tuple | None = None) -> list[dict]:
    """返回 [{kind, ref, title, snippet, bot_id, sub, score}]，同一个页面只出一次（取最相关的那块）。"""
    q = (q or "").strip()
    if not q:
        return []
    sync()
    where, args = _visible(kinds, bot_id)
    if subs:
        where += f" and (d.kind != 'memory' or d.sub in ({','.join('?' * len(subs))}))"
        args += list(subs)
    ranks: dict[int, float] = {}
    if m := _match(q):
        rows = conn.execute(f"select d.id from search_fts f join search_docs d on d.id = f.rowid "
                            f"where search_fts match ? and {where} order by bm25(search_fts) limit 60", [m] + args).fetchall()
        for i, (did,) in enumerate(rows):
            ranks[did] = ranks.get(did, 0) + 1 / (60 + i)
    e = embed_config()
    if e and time.time() - status["failed"] > 60 and conn.execute(
            "select 1 from search_docs where vec is not null and model=? limit 1", (e["model"],)).fetchone():
        try:
            qv = (await embed([q]))[0]
            rows = conn.execute(f"select d.id, d.vec from search_docs d where d.vec is not null and d.model = ? and {where}",
                                [e["model"]] + args).fetchall()
            scored = sorted(((_dot(qv, _unpack(v)), did) for did, v in rows), reverse=True)[:60]
            for i, (s, did) in enumerate(scored):
                if s > 0.2:
                    ranks[did] = ranks.get(did, 0) + 1 / (60 + i)
        except Exception as err:   # 向量服务出问题时退回只用关键词，一分钟内不再试
            status.update(error=f"{type(err).__name__}: {str(err)[:200]}", failed=time.time())
    if not ranks:
        return []
    ids = sorted(ranks, key=ranks.get, reverse=True)
    docs = {r[0]: r for r in conn.execute(
        f"select id, kind, ref, title, text, bot_id, sub from search_docs where id in ({','.join('?' * len(ids))})", ids)}
    out, seen = [], set()
    for did in ids:
        if did not in docs:
            continue
        _, kind, ref, title, text, b, sub = docs[did]
        if (kind, ref) in seen:
            continue
        seen.add((kind, ref))
        out.append({"kind": kind, "ref": ref, "title": title, "snippet": text if kind == "memory" else snippet(text, q), "bot_id": b, "sub": sub,
                    "score": round(ranks[did], 5)})
        if len(out) >= limit:
            break
    return out


def snippet(text: str, q: str, width: int = 120) -> str:
    """命中词附近的一段。"""
    low = text.lower()
    pos = min((p for t in tokens(q) if (p := low.find(t)) >= 0), default=-1)
    if pos < 0 or len(text) <= width:
        return text[:width] + ("…" if len(text) > width else "")
    start = max(0, pos - width // 3)
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


# ---------- 给 Bot 用的工具 ----------
SEARCH_TOOL = {"type": "function", "function": {
    "name": "search_memory",
    "description": "按意思查长期记忆和资料库里的页面。提示词里只放了最近的记忆；用户提到以前说过的事、以前做过的报告或页面，"
                   "或者要参考之前的结果时用它。查到页面要看全文用 read_page。",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "要找什么，用自然的说法；可以多写几个同义的词"},
        "scope": {"type": "string", "enum": ["all", "memories", "pages"], "description": "默认 all"}},
        "required": ["query"]}}}


async def tool(bot_id: int, args: dict) -> str:
    from . import library
    kinds = {"memories": ("memory",), "pages": ("page",)}.get(args.get("scope") or "all", ("memory", "page"))
    hits = await search(str(args.get("query") or ""), bot_id, kinds, 8)
    if not hits:
        return "没找到相关的记忆或页面"
    lines = []
    for h in hits:
        if h["kind"] == "memory":
            who = "所有 Bot 共享" if h["bot_id"] is None else "做过的事" if h["sub"] == "work" else "你的记忆"
            lines.append(f"- 记忆 #{h['ref']}（{who}）：{h['snippet']}")
        else:
            lines.append(f"- 页面 #{h['ref']}《{h['title']}》{library.link(h['ref'])}：{h['snippet']}")
    return "\n".join(lines)
