"""Bot 和对话的增删改查。每个 Bot 有一个私聊对话；群聊的 bot_ids 有 2–6 个，lead 是协调者。"""
import json
import time
from . import i18n
from .memory import conn

if "lead" not in {r[1] for r in conn.execute("pragma table_info(chats)")}:
    conn.execute("alter table chats add column lead integer")   # 群聊的协调者：没人被 @ 时由它接
    conn.commit()

SHAPES = ["square", "sparkle", "clover", "flower", "heart", "house", "star", "circle"]
COLORS = ["#7c9cff", "#ff8a65", "#4fc3a1", "#f6c453", "#c792ea", "#ff6b9a", "#5cc8ff", "#a3a3a3"]
MAX_BOTS = 50
GROUP_MIN, GROUP_MAX, MAX_GROUPS = 2, 6, 50


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


def by_name(name: str) -> dict | None:
    """按名字找 Bot（不区分大小写，去掉开头的 @）。"""
    key = (name or "").strip().lstrip("@").strip().lower()
    return next((b for b in list_bots() if b["name"].lower() == key), None) if key else None


def primary() -> dict:
    r = conn.execute("select id from bots where is_primary=1").fetchone()
    return get(r[0])


def create(name: str, avatar: dict | None = None, instructions: str = "", is_primary: bool = False) -> dict:
    n = conn.execute("select count(*) from bots").fetchone()[0]
    if n >= MAX_BOTS:
        raise ValueError(i18n.t("最多 {n} 个 Bot", n=MAX_BOTS))
    avatar = avatar or {"shape": SHAPES[n % len(SHAPES)], "color": COLORS[n % len(COLORS)]}
    now = time.time()
    cur = conn.execute("insert into bots(name, avatar, instructions, is_primary, created) values(?,?,?,?,?)",
                       (name.strip()[:40] or i18n.t("未命名"), json.dumps(avatar), instructions, int(is_primary), now))
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
        raise ValueError(i18n.t("主 Bot 不能删除，先把别的 Bot 设为主 Bot"))
    conn.execute("delete from task_steps where task_id in (select id from messages where chat_id=?)", (b["chat_id"],))
    conn.execute("delete from messages where chat_id=?", (b["chat_id"],))
    conn.execute("delete from chats where id=?", (b["chat_id"],))
    conn.execute("delete from bots where id=?", (bot_id,))
    # 从群聊里移出；群里的历史消息保留，协调者换成剩下的第一个
    for g in [c for c in list_chats() if c["kind"] == "group" and bot_id in c["bot_ids"]]:
        ids = [i for i in g["bot_ids"] if i != bot_id]
        lead = g["lead"] if g["lead"] in ids else (ids[0] if ids else None)
        conn.execute("update chats set bot_ids=?, lead=? where id=?", (json.dumps(ids), lead, g["id"]))
    conn.commit()


def list_chats() -> list[dict]:
    out = []
    for i, kind, title, ids, pinned, updated, lead in conn.execute(
            "select id, kind, title, bot_ids, pinned, updated, lead from chats order by pinned desc, updated desc"):
        last = conn.execute("select content, role from messages where chat_id=? order by id desc limit 1", (i,)).fetchone()
        out.append({"id": i, "kind": kind, "title": title, "bot_ids": json.loads(ids), "pinned": bool(pinned),
                    "updated": updated, "preview": (last[0][:80] if last else ""), "lead": lead})
    return out


def chat(chat_id: int) -> dict | None:
    return next((c for c in list_chats() if c["id"] == chat_id), None)


# ---------- 群聊 ----------
def _members(bot_ids) -> list[int]:
    ids = []
    for i in bot_ids or []:
        i = int(i)
        if i not in ids:
            if not get(i):
                raise ValueError(i18n.t("没有 Bot #{id}", id=i))
            ids.append(i)
    if not GROUP_MIN <= len(ids) <= GROUP_MAX:
        raise ValueError(i18n.t("群聊要有 {min}–{max} 个 Bot", min=GROUP_MIN, max=GROUP_MAX))
    return ids


def auto_title(ids: list[int]) -> str:
    names = [get(i)["name"] for i in ids]
    return i18n.t("、").join(names[:3]) + (i18n.t(" 等 {n} 个", n=len(names)) if len(names) > 3 else "")


def _lead(ids: list[int], lead) -> int:
    if lead is not None and int(lead) in ids:
        return int(lead)
    p = conn.execute("select id from bots where is_primary=1").fetchone()
    return p[0] if p and p[0] in ids else ids[0]


def create_group(bot_ids, title: str = "", lead=None) -> dict:
    ids = _members(bot_ids)
    if conn.execute("select count(*) from chats where kind='group'").fetchone()[0] >= MAX_GROUPS:
        raise ValueError(i18n.t("最多 {n} 个群聊", n=MAX_GROUPS))
    cur = conn.execute("insert into chats(kind, title, bot_ids, updated, lead) values('group', ?, ?, ?, ?)",
                       ((title or "").strip()[:60] or auto_title(ids), json.dumps(ids), time.time(), _lead(ids, lead)))
    conn.commit()
    return chat(cur.lastrowid)


def update_group(chat_id: int, title: str | None = None, bot_ids=None, lead=None) -> dict:
    g = chat(chat_id)
    if not g or g["kind"] != "group":
        raise ValueError(i18n.t("没有这个群聊"))
    ids = _members(bot_ids) if bot_ids is not None else g["bot_ids"]
    if title is not None:
        conn.execute("update chats set title=? where id=?", (title.strip()[:60] or auto_title(ids), chat_id))
    conn.execute("update chats set bot_ids=?, lead=? where id=?",
                 (json.dumps(ids), _lead(ids, lead if lead is not None else g["lead"]), chat_id))
    conn.commit()
    return chat(chat_id)


def delete_group(chat_id: int):
    conn.execute("delete from task_steps where task_id in (select id from messages where chat_id=?)", (chat_id,))
    conn.execute("delete from messages where chat_id=?", (chat_id,))
    conn.execute("delete from chats where id=? and kind='group'", (chat_id,))
    conn.commit()


def ensure_default():
    """首次启动或从 v1 升级：没有 Bot 时创建主 Bot，并把没归属的旧消息迁到它的对话里。"""
    if not conn.execute("select 1 from bots limit 1").fetchone():
        create("Nova", {"shape": "sparkle", "color": "#7c9cff"}, is_primary=True)
    p = primary()
    conn.execute("update messages set chat_id=?, bot_id=? where chat_id is null", (p["chat_id"], p["id"]))
    conn.commit()
