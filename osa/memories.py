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
from . import memory

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
        raise ValueError("记忆内容不能为空")
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
        raise ValueError(f"{'共享的用户事实' if bot_id is None else '这个 Bot 的记忆'}最多 {cap} 条，先删掉一些旧的")
    mid = conn.execute("insert into memories(bot_id, kind, text, source, created, updated) values(?,?,?,?,?,?)",
                       (bot_id, "note", text, source, now, now)).lastrowid
    conn.commit()
    return get(mid)


_KEEP = object()


def update(mid: int, text: str | None = None, bot_id=_KEEP) -> dict:
    """改内容，或者在“共享 / 只给这个 Bot”之间移动（bot_id=None 表示共享）。"""
    m = get(mid)
    if not m:
        raise ValueError(f"没有 #{mid} 这条记忆")
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
                 (bot_id, "work", f"{time.strftime('%m-%d', time.localtime(now))} 任务 #{task_id}：{line}",
                  "task", now, now))
    conn.execute("""delete from memories where bot_id=? and kind='work' and id not in
        (select id from memories where bot_id=? and kind='work' order by id desc limit ?)""", (bot_id, bot_id, KEEP_WORK))
    conn.commit()


def _lines(items: list[dict]) -> str:
    return "\n".join(f"- #{m['id']} {m['text']}" for m in items)


def prompt_block(bot_id: int) -> str:
    """写进 Router 系统提示词的记忆。"""
    shared, own = _list(None, "note", PROMPT_NOTES), _list(bot_id, "note", PROMPT_NOTES)
    work = _list(bot_id, "work", PROMPT_WORK)
    parts = [f"关于用户（所有 Bot 共享）：\n{_lines(shared) or '（暂无）'}",
             f"你自己记得的：\n{_lines(own) or '（暂无）'}"]
    if work:
        parts.append(f"最近做过的事（自动记录）：\n{_lines(work)}")
    return "\n".join(parts)


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


async def parse_import(text: str, existing: list[str] | None = None) -> list[dict]:
    """把导出的文字拆成记忆条目。模型出错时退回按行拆。"""
    text = (text or "").strip()[:20000]
    if not text:
        raise ValueError("没有可导入的内容")
    have = "\n".join(f"- {t}" for t in existing or []) or "（无）"
    try:
        client, model = client_for("filter")
        resp = await client.chat.completions.create(model=model, messages=[
            {"role": "system", "content": IMPORT_PROMPT},
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
    for item in await parse_import(text, known):
        try:
            saved.append(add(None if item["shared"] else bot_id, item["text"], "import"))
        except ValueError:
            continue   # 满了或者是空的，跳过这条
    return saved
