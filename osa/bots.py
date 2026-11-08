"""Bot 和对话的增删改查。每个 Bot 有一个私聊对话；群聊的 bot_ids 有 2–6 个，lead 是协调者。

讨论串（thread）：针对私聊或群聊里的某一条消息（root）回复，是挂在那个对话（parent）下面的一个子对话，
表里 kind='thread'。它不出现在侧栏；成员、协调者、标题跟着上级对话走，chat() 返回时 kind 也是上级的（direct / group），
所以 Router、群聊、任务、私信回信都按普通对话处理，结果自然回到讨论串里。"""
import json
import re
import time
from . import avatars, i18n, media
from .memory import conn

_cols = {r[1] for r in conn.execute("pragma table_info(chats)")}
if "lead" not in _cols:
    conn.execute("alter table chats add column lead integer")   # 群聊的协调者：没人被 @ 时由它接
if "parent" not in _cols:
    conn.execute("alter table chats add column parent integer")   # 讨论串：在哪个对话里
    conn.execute("alter table chats add column root integer")     # 讨论串：回复的是哪条消息
if "image" not in _cols:
    conn.execute("alter table chats add column image text")   # 群聊自己的头像图片（/avatars/…）；没有就用成员的头像拼
if "seen" not in _cols:
    # 讨论串：你看到过的最后一条消息 id，之后 Bot 的回复算“新回复”。升级上来时，已有的回复都算看过
    conn.execute("alter table chats add column seen integer")
    conn.execute("update chats set seen=(select coalesce(max(id), 0) from messages where chat_id=chats.id) where kind='thread'")
conn.execute("create index if not exists chats_thread on chats(parent, root)")
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


def primary_name() -> str:
    """主 Bot 的名字；没有主 Bot（只在测试里）是空的。"""
    r = conn.execute("select name from bots where is_primary=1").fetchone()
    return r[0] if r else ""


def clean_avatar(avatar, n: int = 0) -> dict:
    """头像：形状加颜色，可以再加一张图片（image，/avatars/…）。有图片时显示图片，形状和颜色留着：
    导出成模板、图片被清掉时用。不认识的形状、颜色换成默认的。"""
    a = avatar if isinstance(avatar, dict) else {}
    out = {"shape": a.get("shape") if a.get("shape") in SHAPES else SHAPES[n % len(SHAPES)],
           "color": a.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(a.get("color") or "")) else COLORS[n % len(COLORS)]}
    if a.get("image"):
        if not avatars.valid(a["image"]):
            raise ValueError(i18n.t("头像图片不存在，重新上传一次"))
        out["image"] = a["image"]
    return out


def _next_id() -> int:
    """新 Bot 的编号，删掉的 Bot 的编号不再用：bots 表没有 AUTOINCREMENT，删了最新的 Bot 再建，编号会和它一样，
    它留在群聊里的发言、资料库里的页面就算到新 Bot 头上了。用过的最大编号记在 settings 的 bot_id_hw。"""
    hw = conn.execute("select value from settings where key='bot_id_hw'").fetchone()
    used = conn.execute("select max(coalesce((select max(id) from bots), 0), "
                        "coalesce((select max(bot_id) from messages), 0))").fetchone()[0]
    bid = max(int(hw[0]) if hw else 0, used or 0) + 1
    conn.execute("insert or replace into settings(key, value) values('bot_id_hw', ?)", (str(bid),))
    return bid


def create(name: str, avatar: dict | None = None, instructions: str = "", is_primary: bool = False) -> dict:
    n = conn.execute("select count(*) from bots").fetchone()[0]
    if n >= MAX_BOTS:
        raise ValueError(i18n.t("最多 {n} 个 Bot", n=MAX_BOTS))
    avatar = clean_avatar(avatar, n)
    now = time.time()
    bid = _next_id()
    conn.execute("insert into bots(id, name, avatar, instructions, is_primary, created) values(?,?,?,?,?,?)",
                 (bid, name.strip()[:40] or i18n.t("未命名"), json.dumps(avatar), instructions, int(is_primary), now))
    conn.execute("insert into chats(kind, title, bot_ids, updated) values('direct', ?, ?, ?)",
                 (name, json.dumps([bid]), now))
    conn.commit()
    return get(bid)


def update(bot_id: int, **fields) -> dict:
    """改名字、职责、头像（没给或给 None 的不改）。改名字时私聊的标题跟着改。"""
    if fields.get("name") is not None:
        name = str(fields["name"]).strip()[:40] or i18n.t("未命名")
        conn.execute("update bots set name=? where id=?", (name, bot_id))
        conn.execute("update chats set title=? where kind='direct' and bot_ids=?", (name, json.dumps([bot_id])))
    if fields.get("instructions") is not None:
        conn.execute("update bots set instructions=? where id=?", (str(fields["instructions"])[:4000], bot_id))
    if fields.get("avatar"):
        conn.execute("update bots set avatar=? where id=?", (json.dumps(clean_avatar(fields["avatar"], bot_id)), bot_id))
    conn.commit()
    avatars.gc()
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
    _delete_chat(b["chat_id"])
    conn.execute("delete from bots where id=?", (bot_id,))
    # 从群聊里移出；群里的历史消息保留，协调者换成剩下的第一个
    for g in [c for c in list_chats() if c["kind"] == "group" and bot_id in c["bot_ids"]]:
        ids = [i for i in g["bot_ids"] if i != bot_id]
        lead = g["lead"] if g["lead"] in ids else (ids[0] if ids else None)
        conn.execute("update chats set bot_ids=?, lead=? where id=?", (json.dumps(ids), lead, g["id"]))
    conn.commit()
    avatars.gc()
    media.gc()


def thread_ids(chat_id: int) -> list[int]:
    """一个对话下面所有讨论串的 id。"""
    return [r[0] for r in conn.execute("select id from chats where kind='thread' and parent=?", (chat_id,))]


def _delete_chat(chat_id: int):
    """删掉一个对话和它的消息、任务过程，连同它下面的讨论串。"""
    for i in [chat_id, *thread_ids(chat_id)]:
        conn.execute("delete from task_steps where task_id in (select id from messages where chat_id=?)", (i,))
        conn.execute("delete from messages where chat_id=?", (i,))
        conn.execute("delete from chats where id=?", (i,))


_CHAT_COLS = "id, kind, title, bot_ids, pinned, updated, lead, parent, root, image"


def _chat(row) -> dict:
    i, kind, title, ids, pinned, updated, lead, parent, root, image = row
    last = conn.execute("select content, role from messages where chat_id=? order by id desc limit 1", (i,)).fetchone()
    threads = 0 if kind == "thread" else \
        conn.execute("select count(*) from chats where kind='thread' and parent=?", (i,)).fetchone()[0]
    return {"id": i, "kind": kind, "title": title, "bot_ids": json.loads(ids or "[]"), "pinned": pinned or 0,
            "updated": updated, "preview": (last[0][:80] if last else ""), "lead": lead, "image": image,
            "threads": threads}   # 下面有几个讨论串：有的话对话顶上显示“讨论串”按钮


def list_chats() -> list[dict]:
    """侧栏里的对话：私聊和群聊，不含讨论串。按最新消息排，置顶的也一样（置顶的另外在侧栏上面那排）。"""
    return [_chat(r) for r in conn.execute(
        f"select {_CHAT_COLS} from chats where kind != 'thread' order by updated desc")]


def set_pinned(chat_id: int, pinned: bool) -> dict:
    """置顶 / 取消置顶私聊或群聊。pinned 存置顶的时间，侧栏上面那排按它排（先置顶的在前），0 是没置顶。"""
    c = chat(chat_id)
    if not c or c.get("parent"):
        raise ValueError(i18n.t("没有这个对话"))
    if bool(c["pinned"]) != pinned:
        conn.execute("update chats set pinned=? where id=?", (time.time() if pinned else 0, chat_id))
        conn.commit()
    return chat(chat_id)


def chat(chat_id: int) -> dict | None:
    """一个对话。讨论串按上级对话的样子返回，另外带上 parent（上级对话）、root（回复的那条消息）
    和 root_bot（那条消息是群里哪个成员说的：没 @ 人时由它接着回答）。"""
    r = conn.execute(f"select {_CHAT_COLS} from chats where id=?", (chat_id,)).fetchone()
    if not r:
        return None
    c = _chat(r)
    if c["kind"] != "thread":
        return c
    up = chat(r[7])
    if not up:
        return None
    author = conn.execute("select bot_id from messages where id=?", (r[8],)).fetchone()
    return {**up, "id": c["id"], "updated": c["updated"], "preview": c["preview"], "pinned": 0,
            "parent": up["id"], "root": r[8], "root_bot": author[0] if author and author[0] in up["bot_ids"] else None}


# ---------- 讨论串 ----------
def find_thread(parent: int, root: int) -> int | None:
    r = conn.execute("select id from chats where kind='thread' and parent=? and root=?", (parent, root)).fetchone()
    return r[0] if r else None


def thread(parent: int, root: int) -> dict:
    """某条消息下的讨论串，没有就建一个。只能回复私聊、群聊里的消息，讨论串里不能再开讨论串。"""
    up = chat(parent)
    if not up or up.get("parent"):
        raise ValueError(i18n.t("只能回复私聊或群聊里的消息"))
    if not conn.execute("select 1 from messages where id=? and chat_id=?", (root, parent)).fetchone():
        raise ValueError(i18n.t("这个对话里没有这条消息"))
    tid = find_thread(parent, root)
    if not tid:
        tid = conn.execute("insert into chats(kind, title, bot_ids, updated, parent, root) values('thread', '', '[]', ?, ?, ?)",
                           (time.time(), parent, root)).lastrowid
        conn.commit()
    return chat(tid)


# 讨论串里算作“回复”的消息：用户和 Bot 说的话、任务结果；派任务、交接这类记录不算
_REPLY = ("(m.role = 'user' or (m.role = 'assistant' and m.content != '') "
          "or json_extract(m.meta, '$.kind') in ('result', 'dm_reply', 'image'))")


def thread_summaries(parent: int, roots: list[int]) -> dict[int, dict]:
    """这些消息下面的讨论串：root -> {id, count 回复数, last 最后回复的时间, bots 回复过的 Bot,
    unread 你还没看过的 Bot 回复数}。"""
    if not roots:
        return {}
    marks = ",".join("?" * len(roots))
    rows = conn.execute(f"""select c.id, c.root, count(m.id), max(m.ts),
        group_concat(distinct case when m.role != 'user' then m.bot_id end),
        sum(case when m.role != 'user' and m.id > coalesce(c.seen, 0) then 1 else 0 end)
        from chats c left join messages m on m.chat_id = c.id and {_REPLY}
        where c.kind = 'thread' and c.parent = ? and c.root in ({marks}) group by c.id""", (parent, *roots)).fetchall()
    return {root: {"id": i, "count": n, "last": last, "bots": [int(x) for x in (who or "").split(",") if x],
                   "unread": unread or 0}
            for i, root, n, last, who, unread in rows}


def thread_list(parent: int | None = None, unread: bool = False) -> list[dict]:
    """讨论串列表，最近有回复的在前。parent：只要这个对话下面的；unread：只要有新回复的。
    每项是 thread_summaries 的摘要，加上 parent、root，被回复的那条消息（root_text、root_role、root_bot）
    和最后一条回复（preview、preview_bot）的开头。还没有回复的讨论串不算。"""
    sql = "select parent, root from chats where kind='thread'" + (" and parent=?" if parent else "")
    roots: dict[int, list[int]] = {}
    for p, r in conn.execute(sql, (parent,) if parent else ()):
        roots.setdefault(p, []).append(r)
    out = []
    for p, rs in roots.items():
        for root, s in thread_summaries(p, rs).items():
            if not s["count"] or (unread and not s["unread"]):
                continue
            head = conn.execute("select role, content, bot_id from messages where id=?", (root,)).fetchone()
            tail = conn.execute(f"select m.content, m.bot_id from messages m where m.chat_id=? and {_REPLY} "
                                "order by m.id desc limit 1", (s["id"],)).fetchone()
            out.append({**s, "parent": p, "root": root,
                        "root_role": head[0] if head else "", "root_text": (head[1] if head else "")[:200],
                        "root_bot": head[2] if head else None,
                        "preview": (tail[0] if tail else "")[:200], "preview_bot": tail[1] if tail else None})
    return sorted(out, key=lambda x: x["last"] or 0, reverse=True)


def mark_seen(thread_id: int, upto: int | None = None) -> dict | None:
    """你看过了讨论串里到 upto（默认到最新一条）为止的消息。返回和 thread_event 一样的摘要；不是讨论串返回 None。"""
    top = conn.execute("select coalesce(max(id), 0) from messages where chat_id=?", (thread_id,)).fetchone()[0]
    upto = min(int(upto), top) if upto else top
    if not conn.execute("update chats set seen=max(coalesce(seen, 0), ?) where id=? and kind='thread'",
                        (upto, thread_id)).rowcount:
        return None
    conn.commit()
    return thread_event(thread_id)


def thread_event(chat_id: int) -> dict | None:
    """讨论串里来了新消息时，给前端更新上级对话里那条消息下面的“N 条回复”。不是讨论串返回 None。"""
    from .memory import thread_of
    r = thread_of(chat_id)
    if not r:
        return None
    s = thread_summaries(r[0], [r[1]]).get(r[1])
    return s and {"parent": r[0], "root": r[1], **s}


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


def update_group(chat_id: int, title: str | None = None, bot_ids=None, lead=None, image: str | None = None) -> dict:
    """image：群头像图片的地址，空字符串是不用图片（用成员的头像拼）；None 不改。"""
    g = chat(chat_id)
    if not g or g["kind"] != "group" or g.get("parent"):
        raise ValueError(i18n.t("没有这个群聊"))
    ids = _members(bot_ids) if bot_ids is not None else g["bot_ids"]
    if title is not None:
        conn.execute("update chats set title=? where id=?", (title.strip()[:60] or auto_title(ids), chat_id))
    if image is not None:
        if image and not avatars.valid(image):
            raise ValueError(i18n.t("头像图片不存在，重新上传一次"))
        conn.execute("update chats set image=? where id=?", (image or None, chat_id))
    conn.execute("update chats set bot_ids=?, lead=? where id=?",
                 (json.dumps(ids), _lead(ids, lead if lead is not None else g["lead"]), chat_id))
    conn.commit()
    if image is not None:
        avatars.gc()
    return chat(chat_id)


def delete_group(chat_id: int):
    if conn.execute("select 1 from chats where id=? and kind='group'", (chat_id,)).fetchone():
        _delete_chat(chat_id)
        conn.commit()
        avatars.gc()
        media.gc()


def ensure_default():
    """首次启动或从 v1 升级：没有 Bot 时创建主 Bot，并把没归属的旧消息迁到它的对话里。"""
    if not conn.execute("select 1 from bots limit 1").fetchone():
        create("Nova", {"shape": "sparkle", "color": "#7c9cff"}, is_primary=True)
    p = primary()
    conn.execute("update messages set chat_id=?, bot_id=? where chat_id is null", (p["chat_id"], p["id"]))
    conn.commit()
