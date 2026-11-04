"""Bot 和对话的增删改查。每个 Bot 有一个私聊对话；群聊（P6）的 bot_ids 有多个。"""
import json
import time
from .memory import conn

SHAPES = ["square", "sparkle", "clover", "flower", "heart", "house", "star", "circle"]
COLORS = ["#7c9cff", "#ff8a65", "#4fc3a1", "#f6c453", "#c792ea", "#ff6b9a", "#5cc8ff", "#a3a3a3"]
MAX_BOTS = 50


def _bot(row) -> dict:
    i, name, avatar, instr, primary, created = row
    chat = conn.execute("select id from chats where kind='direct' and bot_ids=?", (json.dumps([i]),)).fetchone()
    return {"id": i, "name": name, "avatar": json.loads(avatar or "{}"), "instructions": instr or "",
            "is_primary": bool(primary), "created": created, "chat_id": chat[0] if chat else None}


def list_bots() -> list[dict]:
    return [_bot(r) for r in conn.execute(
        "select id, name, avatar, instructions, is_primary, created from bots order by is_primary desc, id")]


def get(bot_id: int) -> dict | None:
    r = conn.execute("select id, name, avatar, instructions, is_primary, created from bots where id=?",
                     (bot_id,)).fetchone()
    return _bot(r) if r else None


def primary() -> dict:
    r = conn.execute("select id from bots where is_primary=1").fetchone()
    return get(r[0])


def create(name: str, avatar: dict | None = None, instructions: str = "", is_primary: bool = False) -> dict:
    n = conn.execute("select count(*) from bots").fetchone()[0]
    if n >= MAX_BOTS:
        raise ValueError(f"最多 {MAX_BOTS} 个 Bot")
    avatar = avatar or {"shape": SHAPES[n % len(SHAPES)], "color": COLORS[n % len(COLORS)]}
    now = time.time()
    cur = conn.execute("insert into bots(name, avatar, instructions, is_primary, created) values(?,?,?,?,?)",
                       (name.strip()[:40] or "未命名", json.dumps(avatar), instructions, int(is_primary), now))
    bid = cur.lastrowid
    conn.execute("insert into chats(kind, title, bot_ids, updated) values('direct', ?, ?, ?)",
                 (name, json.dumps([bid]), now))
    conn.commit()
    return get(bid)


def update(bot_id: int, **fields) -> dict:
    for k in ("name", "instructions"):
        if k in fields and fields[k] is not None:
            conn.execute(f"update bots set {k}=? where id=?", (str(fields[k])[:4000], bot_id))
    if fields.get("avatar"):
        conn.execute("update bots set avatar=? where id=?", (json.dumps(fields["avatar"]), bot_id))
    conn.commit()
    return get(bot_id)


def set_primary(bot_id: int):
    conn.execute("update bots set is_primary = (id = ?)", (bot_id,))
    conn.commit()


def delete(bot_id: int):
    b = get(bot_id)
    if not b:
        return
    if b["is_primary"]:
        raise ValueError("主 Bot 不能删除，先把别的 Bot 设为主 Bot")
    conn.execute("delete from messages where chat_id=?", (b["chat_id"],))
    conn.execute("delete from chats where id=?", (b["chat_id"],))
    conn.execute("delete from bots where id=?", (bot_id,))
    conn.commit()


def list_chats() -> list[dict]:
    out = []
    for i, kind, title, ids, pinned, updated in conn.execute(
            "select id, kind, title, bot_ids, pinned, updated from chats order by pinned desc, updated desc"):
        last = conn.execute("select content, role from messages where chat_id=? order by id desc limit 1", (i,)).fetchone()
        out.append({"id": i, "kind": kind, "title": title, "bot_ids": json.loads(ids), "pinned": bool(pinned),
                    "updated": updated, "preview": (last[0][:80] if last else "")})
    return out


def chat(chat_id: int) -> dict | None:
    return next((c for c in list_chats() if c["id"] == chat_id), None)


def ensure_default():
    """首次启动或从 v1 升级：没有 Bot 时创建主 Bot，并把没归属的旧消息迁到它的对话里。"""
    if not conn.execute("select 1 from bots limit 1").fetchone():
        create("Nova", {"shape": "sparkle", "color": "#7c9cff"}, is_primary=True)
    p = primary()
    conn.execute("update messages set chat_id=?, bot_id=? where chat_id is null", (p["chat_id"], p["id"]))
    conn.commit()
