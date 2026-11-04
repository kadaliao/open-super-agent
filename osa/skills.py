"""技能：可复用的做事方法。用户在对话里用 /名字 调用，Bot 按技能正文去做。

bot_id 为空的技能所有 Bot 共用；同名时 Bot 自己的优先。
"""
import re
import time
from .memory import conn

conn.execute("""create table if not exists skills(
  id integer primary key, bot_id integer, name text, description text default '', body text,
  source text default 'manual', created real, updated real)""")
conn.commit()

NAME_RE = re.compile(r"^[\w一-鿿-]{1,40}$")
MAX_PER_BOT = 100


def _row(r) -> dict:
    i, bot_id, name, desc, body, source, created, updated = r
    return {"id": i, "bot_id": bot_id, "name": name, "description": desc or "", "body": body or "",
            "source": source, "created": created, "updated": updated}

_COLS = "id, bot_id, name, description, body, source, created, updated"


def list_for(bot_id: int) -> list[dict]:
    """这个 Bot 能用的技能（自己的 + 共用的），同名时自己的覆盖共用的。"""
    rows = [_row(r) for r in conn.execute(
        f"select {_COLS} from skills where bot_id=? or bot_id is null order by bot_id is null, name", (bot_id,))]
    seen, out = set(), []
    for s in rows:
        if s["name"] not in seen:
            seen.add(s["name"])
            out.append(s)
    return out


def get(sid: int) -> dict | None:
    r = conn.execute(f"select {_COLS} from skills where id=?", (sid,)).fetchone()
    return _row(r) if r else None


def find(bot_id: int, name: str) -> dict | None:
    return next((s for s in list_for(bot_id) if s["name"] == name), None)


def _check(name: str, body: str):
    name = name.strip().lstrip("/")
    if not NAME_RE.match(name):
        raise ValueError("技能名只能用中文、字母、数字、- 和 _，最长 40 个字")
    if not body.strip():
        raise ValueError("技能内容不能为空")
    return name


def save(bot_id: int | None, name: str, body: str, description: str = "", source: str = "manual",
         sid: int | None = None) -> dict:
    """新建或修改。同一个 Bot 下同名的技能会被覆盖。"""
    name = _check(name, body)
    now = time.time()
    if sid is None:
        r = conn.execute("select id from skills where name=? and bot_id is ?", (name, bot_id)).fetchone()
        sid = r[0] if r else None
    if sid is None:
        n = conn.execute("select count(*) from skills where bot_id is ?", (bot_id,)).fetchone()[0]
        if n >= MAX_PER_BOT:
            raise ValueError(f"每个 Bot 最多 {MAX_PER_BOT} 个技能")
        sid = conn.execute("insert into skills(bot_id, name, description, body, source, created, updated) "
                           "values(?,?,?,?,?,?,?)", (bot_id, name, description[:200], body[:8000], source, now, now)).lastrowid
    else:
        dup = conn.execute("select id from skills where name=? and bot_id is ? and id != ?", (name, bot_id, sid)).fetchone()
        if dup:
            raise ValueError(f"已经有叫 /{name} 的技能了")
        conn.execute("update skills set bot_id=?, name=?, description=?, body=?, updated=? where id=?",
                     (bot_id, name, description[:200], body[:8000], now, sid))
    conn.commit()
    return get(sid)


def delete(sid: int):
    conn.execute("delete from skills where id=?", (sid,))
    conn.commit()


def parse(bot_id: int, text: str) -> tuple[dict, str] | None:
    """消息以 /技能名 开头时返回 (技能, 其余文字)。"""
    m = re.match(r"^/([\w一-鿿-]+)(?:\s+([\s\S]*))?$", text.strip())
    if not m:
        return None
    s = find(bot_id, m[1])
    return (s, (m[2] or "").strip()) if s else None


def expand(skill: dict, extra: str) -> str:
    """把技能写成交给模型的说明。"""
    return (f"（用户调用了技能 /{skill['name']}，请按下面的做法执行；用户补充的要求优先。）\n"
            f"--- 技能做法 ---\n{skill['body']}\n--- 结束 ---"
            + (f"\n用户补充：{extra}" if extra else ""))


def summary(bot_id: int) -> str:
    return "\n".join(f"- /{s['name']}：{s['description'] or s['body'][:40]}" for s in list_for(bot_id)) or "暂无技能"
