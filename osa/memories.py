"""长期记忆：每个 Bot 自己记得的事，加上所有 Bot 共享的用户事实。

（memory.py 管对话记录和用量；这里管跨对话长期保留的记忆。）

- bot_id 为空：关于用户本人的少数基本事实（名字、时区、所在城市、语言……），所有 Bot 都知道。
- bot_id 不为空：这个 Bot 自己的记忆，比如用户对它的要求和偏好、它的角色背景。
- kind=work：任务完成时自动记一笔“做过的事”，每个 Bot 只留最近 KEEP_WORK 条。
记忆写进 Router 的系统提示词；Worker 也能看到用户事实和偏好（不含做过的事）。
"""
import json
import time
from .config import call_kwargs, client_for
from .memory import conn
from . import i18n, memory

conn.execute("""create table if not exists memories(
  id integer primary key, bot_id integer, kind text default 'note', text text, source text default 'chat',
  created real, updated real)""")
conn.execute("create index if not exists memories_bot on memories(bot_id, kind)")
conn.commit()

MAX_LEN = 500
MAX_NOTES = 200        # 每个 Bot 自己的记忆
MAX_SHARED = 50        # 共享的用户事实
KEEP_WORK = 20
PROMPT_NOTES = 80      # 写进提示词的条数上限（最近更新的优先）
PROMPT_RECENT = 60     # 超过上限时：最近的 60 条，加上和这次对话相关的更早记忆
PROMPT_RELATED = 20
PROMPT_WORK = 10

_COLS = "id, bot_id, kind, text, source, created, updated"


def _row(r) -> dict:
    i, bot_id, kind, text, source, created, updated = r
    return {"id": i, "bot_id": bot_id, "shared": bot_id is None, "kind": kind, "text": text,
            "source": source, "created": created, "updated": updated}


def get(mid: int) -> dict | None:
    r = conn.execute(f"select {_COLS} from memories where id=?", (mid,)).fetchone()
    return _row(r) if r else None


def _list(bot_id: int | None, kind: str, limit: int = 1000) -> list[dict]:
    return [_row(r) for r in conn.execute(
        f"select {_COLS} from memories where bot_id is ? and kind=? order by updated desc limit ?",
        (bot_id, kind, limit))]


def list_for(bot_id: int) -> dict:
    """记忆面板用：共享的用户事实、这个 Bot 自己的记忆、它最近做过的事。"""
    return {"shared": _list(None, "note"), "own": _list(bot_id, "note"), "work": _list(bot_id, "work")}


def _clean(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise ValueError(i18n.t("记忆内容不能为空"))
    return text[:MAX_LEN]


def add(bot_id: int | None, text: str, source: str = "chat") -> dict:
    """记一条。同一处已经有一模一样的就只刷新时间。"""
    text = _clean(text)
    now = time.time()
    old = conn.execute("select id from memories where bot_id is ? and kind='note' and lower(text)=lower(?)",
                       (bot_id, text)).fetchone()
    if old:
        conn.execute("update memories set updated=? where id=?", (now, old[0]))
        conn.commit()
        return get(old[0])
    n = conn.execute("select count(*) from memories where bot_id is ? and kind='note'", (bot_id,)).fetchone()[0]
    cap = MAX_SHARED if bot_id is None else MAX_NOTES
    if n >= cap:
        raise ValueError(i18n.t("共享的用户事实最多 {n} 条，先删掉一些旧的", n=cap) if bot_id is None
                         else i18n.t("这个 Bot 的记忆最多 {n} 条，先删掉一些旧的", n=cap))
    mid = conn.execute("insert into memories(bot_id, kind, text, source, created, updated) values(?,?,?,?,?,?)",
                       (bot_id, "note", text, source, now, now)).lastrowid
    conn.commit()
    return get(mid)


_KEEP = object()


def update(mid: int, text: str | None = None, bot_id=_KEEP) -> dict:
    """改内容，或者在“共享 / 只给这个 Bot”之间移动（bot_id=None 表示共享）。"""
    m = get(mid)
    if not m:
        raise ValueError(i18n.t("没有 #{id} 这条记忆", id=mid))
    fields: list[str] = ["updated=?"]
    args: list[object] = [time.time()]
    if text is not None:
        fields.append("text=?")
        args.append(_clean(text))
    if bot_id is not _KEEP and m["kind"] == "note":
        fields.append("bot_id=?")
        args.append(bot_id)
    conn.execute(f"update memories set {', '.join(fields)} where id=?", (*args, mid))
    conn.commit()
    return get(mid)


def delete(mid: int):
    conn.execute("delete from memories where id=?", (mid,))
    conn.commit()


def delete_for_bot(bot_id: int):
    conn.execute("delete from memories where bot_id=?", (bot_id,))
    conn.commit()


def visible(mid: int, bot_id: int) -> bool:
    """这个 Bot 能不能改这条：自己的，或者共享的。"""
    m = get(mid)
    return bool(m) and m["bot_id"] in (None, bot_id)


def log_work(bot_id: int, task_id: int, goal: str):
    """任务完成时记一笔做过的事（不调模型，只取任务描述的第一行）。"""
    line = next((l.strip() for l in goal.splitlines() if l.strip()), "")[:100]
    if not line:
        return
    now = time.time()
    conn.execute("insert into memories(bot_id, kind, text, source, created, updated) values(?,?,?,?,?,?)",
                 (bot_id, "work", i18n.t("{date} 任务 #{id}：{goal}", date=time.strftime("%m-%d", time.localtime(now)),
                                         id=task_id, goal=line),
                  "task", now, now))
    conn.execute("""delete from memories where bot_id=? and kind='work' and id not in
        (select id from memories where bot_id=? and kind='work' order by id desc limit ?)""", (bot_id, bot_id, KEEP_WORK))
    conn.commit()


def _lines(items: list[dict]) -> str:
    return "\n".join(f"- #{m['id']} {m['text']}" for m in items)


def _count(bot_id: int | None) -> int:
    return conn.execute("select count(*) from memories where bot_id is ? and kind='note'", (bot_id,)).fetchone()[0]


async def related(bot_id: int, text: str) -> list[int]:
    """记忆多到放不下时，按这次对话的内容找出相关的更早记忆（编号）。放得下就不用找。"""
    if not text or max(_count(None), _count(bot_id)) <= PROMPT_NOTES:
        return []
    from . import search
    hits = await search.search(text, bot_id, ("memory",), PROMPT_RELATED * 2, subs=("note",))
    return [h["ref"] for h in hits]


def _shown(bot_id: int) -> tuple[bool, list[dict], list[dict]]:
    """系统提示词里放哪些记忆：(是不是全放下了, 共享的, 自己的)。"""
    full = max(_count(None), _count(bot_id)) <= PROMPT_NOTES
    n = PROMPT_NOTES if full else PROMPT_RECENT
    return full, _list(None, "note", n), _list(bot_id, "note", n)


def prompt_block(bot_id: int) -> str:
    """写进 Router 系统提示词的记忆。只放记下、改掉、删掉时才变的，系统提示词才能一直命中缓存；
    每轮都可能变的（相关的更早记忆、做过的事）见 turn_block。"""
    full, shared, own = _shown(bot_id)
    parts = [f"关于用户（所有 Bot 共享）：\n{_lines(shared) or '（暂无）'}",
             f"你自己记得的：\n{_lines(own) or '（暂无）'}"]
    if not full:
        parts.append("（记忆比较多，这里只放了最近的；和这次对话可能相关的更早记忆附在最新一条消息后面，其余的用 search_memory 查。）")
    return "\n".join(parts)


def turn_block(bot_id: int, related_ids: list[int] = ()) -> str:
    """每轮都可能变的记忆，附在最新一条消息后面（见 router.STATE_PROMPT）。related_ids：和这次对话相关的更早记忆。"""
    _, shared, own = _shown(bot_id)
    shown = {m["id"] for m in shared + own}
    extra = [m for i in related_ids if i not in shown and (m := get(i)) and m["kind"] == "note"][:PROMPT_RELATED]
    work = _list(bot_id, "work", PROMPT_WORK)
    parts = []
    if extra:
        parts.append(f"和这次对话可能相关的更早记忆（#后面是编号）：\n{_lines(extra)}")
    if work:
        parts.append(f"最近做过的事（自动记录）：\n{_lines(work)}")
    return "\n\n".join(parts)


def worker_block(bot_id: int) -> str:
    """给 Worker 的：用户事实和偏好，没有就返回空字符串。"""
    items = _list(None, "note", PROMPT_NOTES) + _list(bot_id, "note", PROMPT_NOTES)
    return "\n".join(f"- {m['text']}" for m in items)


# ---------- 从其他助手导入 ----------
IMPORT_PROMPT = """用户从别的 AI 助手（ChatGPT、Claude、Gemini 等）导出了它记得的关于用户的内容，请整理成一条条独立的长期记忆。
- 每条是一句完整、独立能看懂的话，用中文，以“用户”开头或直接陈述。
- 合并重复的，去掉一次性的、过时的、和用户无关的内容；“已有的记忆”里已经说过的也去掉。
- 关于用户本人的基本事实（名字、时区、所在地、常用语言、职业）shared=true，其余（偏好、习惯、正在做的事）shared=false。
只输出 JSON：{"items": [{"text": "...", "shared": false}]}"""


def _import_prompt() -> str:
    """英文界面时记忆用英文写；中文界面和以前一样。"""
    return IMPORT_PROMPT if i18n.lang() == "zh" else IMPORT_PROMPT.replace("用中文，以“用户”开头", "用英文，以“The user”开头")


async def parse_import(text: str, existing: list[str] | None = None) -> list[dict]:
    """把导出的文字拆成记忆条目。模型出错时退回按行拆。"""
    text = (text or "").strip()[:20000]
    if not text:
        raise ValueError(i18n.t("没有可导入的内容"))
    have = "\n".join(f"- {t}" for t in existing or []) or "（无）"
    try:
        client, model = client_for("filter")
        resp = await client.chat.completions.create(model=model, messages=[
            {"role": "system", "content": _import_prompt()},
            {"role": "user", "content": f"已有的记忆：\n{have}\n\n导出的内容：\n{text}"}],
            response_format={"type": "json_object"}, **call_kwargs("filter"))
        memory.record_usage("filter", model, resp.usage)
        items = json.loads(resp.choices[0].message.content).get("items") or []
        out = [{"text": str(i["text"]).strip(), "shared": bool(i.get("shared"))}
               for i in items if isinstance(i, dict) and str(i.get("text", "")).strip()]
        if out:
            return out
    except Exception:
        pass
    return [{"text": l.strip(" -•*\t"), "shared": False} for l in text.splitlines() if l.strip(" -•*\t")]


async def import_text(bot_id: int, text: str) -> list[dict]:
    saved = []
    known = [m["text"] for m in _list(None, "note") + _list(bot_id, "note")]
    who = memory.WHO.set({"bot_id": bot_id})   # 拆记忆的那次模型调用记到这个 Bot 头上
    try:
        items = await parse_import(text, known)
    finally:
        memory.WHO.reset(who)
    for item in items:
        try:
            saved.append(add(None if item["shared"] else bot_id, item["text"], "import"))
        except ValueError:
            continue   # 满了或者是空的，跳过这条
    return saved
