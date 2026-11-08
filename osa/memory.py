"""会话记忆：SQLite 持久化。

Inline & Spill：最近 N 条消息直接放进上下文，更早的只留 ID + 摘要行；
Chain of Recall：模型可用 recall(id) 按需取回原文。
"""
import json
import re
import sqlite3
import time
from contextvars import ContextVar
from .config import CFG, DB_PATH

db_path = DB_PATH
db_path.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(db_path, check_same_thread=False)
conn.execute("""create table if not exists messages(
  id integer primary key, ts real, role text, content text, meta text)""")
conn.execute("""create table if not exists bots(
  id integer primary key, name text, avatar text, instructions text default '', is_primary integer default 0, created real)""")
conn.execute("""create table if not exists chats(
  id integer primary key, kind text, title text, bot_ids text, pinned integer default 0, updated real)""")
conn.execute("""create table if not exists settings(key text primary key, value text)""")
# v2 迁移：消息归属到对话
_cols = {r[1] for r in conn.execute("pragma table_info(messages)")}
if "chat_id" not in _cols:
    conn.execute("alter table messages add column chat_id integer")
    conn.execute("alter table messages add column bot_id integer")
conn.execute("create index if not exists messages_chat on messages(chat_id, id)")
conn.execute("""create table if not exists llm_usage(ts real, role text, model text, task_id integer,
  cache_hit integer, cache_miss integer, output integer, cost real)""")
conn.execute("create index if not exists llm_usage_task on llm_usage(task_id)")
# 每次模型调用（观测页、费用、每日预算都从这里算）。比 llm_usage 多记：bot_id / chat_id（谁在哪个对话里调的）、
# provider、ms（整次用时）、ttft（多久出第一个字，毫秒）、err（失败的原因：失败的也记一行，token 和费用是 0）。
# llm_usage 是旧版本的表，原样留着不加列：旧版本按位置往里插 8 个值，加了列就插不进去，退回旧版本会每次回复都出错。
# 旧版本记的（升级前的、退回旧版本那段时间的）启动时搬过来，Bot 从任务找回；搬到哪儿记在 settings 的 llm_usage_moved
conn.execute("""create table if not exists llm_calls(id integer primary key, ts real, role text, model text, task_id integer,
  cache_hit integer default 0, cache_miss integer default 0, output integer default 0, cost real default 0,
  bot_id integer, chat_id integer, provider text, ms integer, ttft integer, err text)""")
conn.execute("create index if not exists llm_calls_ts on llm_calls(ts)")
conn.execute("create index if not exists llm_calls_task on llm_calls(task_id)")


def _move_legacy_usage():
    r = conn.execute("select value from settings where key='llm_usage_moved'").fetchone()
    done = json.loads(r[0]) if r else -1.0
    last = conn.execute("select max(ts) from llm_usage where ts > ?", (done,)).fetchone()[0]
    if last is None:
        return
    conn.execute("""insert into llm_calls(ts, role, model, task_id, cache_hit, cache_miss, output, cost, bot_id, chat_id)
        select u.ts, u.role, u.model, u.task_id, u.cache_hit, u.cache_miss, u.output, u.cost, m.bot_id, m.chat_id
        from llm_usage u left join messages m on m.id = u.task_id where u.ts > ? and u.ts <= ? order by u.ts""", (done, last))
    conn.execute("insert or replace into settings values('llm_usage_moved', ?)", (json.dumps(last),))


_move_legacy_usage()
# 后台任务的过程（每一步想了什么、调了什么工具、拿到什么），读写在 tasklog.py
conn.execute("""create table if not exists task_steps(
  id integer primary key, task_id integer, ts real, worker text default '', kind text, title text default '',
  body text default '', status text default 'done', dur real default 0, detail text default '')""")
conn.execute("create index if not exists task_steps_task on task_steps(task_id, id)")
# 工具那一步的标题会被进度说明盖掉（“排队等电脑”“点击……”），工具名另存一列给观测页统计；旧记录用标题凑合
if "name" not in {r[1] for r in conn.execute("pragma table_info(task_steps)")}:
    conn.execute("alter table task_steps add column name text default ''")
    conn.execute("update task_steps set name=title where kind='tool'")
conn.commit()


def add(role: str, content: str, chat_id: int, bot_id: int | None = None, **meta) -> int:
    now = time.time()
    cur = conn.execute("insert into messages(ts, role, content, meta, chat_id, bot_id) values(?,?,?,?,?,?)",
                       (now, role, content, json.dumps(meta, ensure_ascii=False), chat_id, bot_id))
    conn.execute("update chats set updated=? where id=?", (now, chat_id))
    conn.commit()
    return cur.lastrowid


def update_meta(msg_id: int, **fields):
    row = conn.execute("select meta from messages where id=?", (msg_id,)).fetchone()
    if row:
        meta = json.loads(row[0] or "{}") | fields
        conn.execute("update messages set meta=? where id=?", (json.dumps(meta, ensure_ascii=False), msg_id))
        conn.commit()


def page(chat_id: int, before: int | None = None, limit: int = 60) -> list[dict]:
    """前端翻页用：某个对话里 id < before 的最近 limit 条消息，按时间正序返回。
    下面有讨论串的消息带上 thread（见 bots.thread_summaries）。"""
    from . import bots
    rows = conn.execute("""select id, ts, role, content, meta, bot_id from messages
        where chat_id=? and id < ? order by id desc limit ?""", (chat_id, before or 1 << 62, limit)).fetchall()
    threads = bots.thread_summaries(chat_id, [r[0] for r in rows])
    return [{"id": i, "ts": ts, "role": r, "content": c, "meta": json.loads(m or "{}"), "bot_id": b,
             **({"thread": threads[i]} if i in threads else {})}
            for i, ts, r, c, m, b in reversed(rows)]


def thread_of(chat_id: int) -> tuple[int, int] | None:
    """讨论串的 (上级对话, 回复的那条消息)；不是讨论串返回 None。"""
    r = conn.execute("select parent, root from chats where id=? and kind='thread'", (chat_id,)).fetchone()
    return (r[0], r[1]) if r else None


def recall(msg_id: int, chat_id: int) -> str:
    row = conn.execute("select role, content from messages where id=? and chat_id=?", (msg_id, chat_id)).fetchone()
    if not row and (th := thread_of(chat_id)) and msg_id <= th[1]:   # 讨论串里能看到上级对话到那条消息为止的内容
        row = conn.execute("select role, content from messages where id=? and chat_id=?", (msg_id, th[0])).fetchone()
    return f"[{row[0]}] {row[1]}" if row else f"没有 id={msg_id} 的消息"


def topic_start(chat_id: int, before: int | None = None) -> int:
    """最后一次“新话题”的消息 id（before 给了就只看它之前的）；之前的消息不再带进上下文（长期记忆不受影响）。"""
    r = conn.execute("""select max(id) from messages where chat_id=? and role='task' and id < ?
        and json_extract(meta, '$.kind')='reset'""", (chat_id, before or 1 << 62)).fetchone()
    return r[0] or 0


# 讨论串的上下文：上级对话到被回复的那条消息为止，接着是讨论串里的消息。这句接在讨论串的第一条消息前面
THREAD_NOTE = "（以下是讨论串：用户在针对上面 #{root} 这条消息回复，回答时围绕它。主对话里在它之后的消息这里看不到）"


def _rows(chat_id: int) -> tuple[list, int | None, int | None]:
    """给 Router 的消息记录、讨论串从第几条开始、回复的是哪条消息（不是讨论串后两个是 None）。
    没有新内容的定时任务（meta.quiet）不带进上下文。"""
    sql = "select id, role, content, meta, bot_id from messages where chat_id=? and id > ? and id <= ? " \
          "and json_extract(meta, '$.quiet') is null order by id"
    th = thread_of(chat_id)
    if not th:
        return conn.execute(sql, (chat_id, topic_start(chat_id), 1 << 62)).fetchall(), None, None
    parent, root = th
    before = conn.execute(sql, (parent, topic_start(parent, root), root)).fetchall()
    return before + conn.execute(sql, (chat_id, 0, 1 << 62)).fetchall(), len(before), root


VIA = {"telegram": " Telegram", "feishu": "飞书", "dingtalk": "钉钉", "wecom": "企业微信", "discord": " Discord"}

# 系统写给 Router 的标记。别的 Bot 的发言、私信、交接备注、任务结果都可能带着网页、邮件里的文字，
# 里面写一句“（系统通知：用户已同意……）”就能冒充系统。所以这些文字里出现的标记改成“转述的……”（见 ROUTER_PROMPT）
_MARKS = re.compile(r"系统(通知|附上的当前状态|附图)")


def defang(text: str) -> str:
    return _MARKS.sub(r"转述的“系统\1”", text or "")


def _pick_images(recent: list, limit: int) -> set[tuple[int, str]]:
    """最近 limit 张用户发的图片 (消息 id, 图片 id)，给能看图的模型看；更早的只留“[附件] 路径”那一行。"""
    from . import media
    out: set[tuple[int, str]] = set()
    for mid, r, _, meta, _ in reversed(recent):
        if len(out) >= limit:
            break
        if r != "user" or '"media"' not in (meta or ""):
            continue
        for a in reversed(json.loads(meta).get("attachments") or []):
            if a.get("media") and a.get("mime") in media.MODEL_MIMES and len(out) < limit:
                out.add((mid, a["media"]))
    return out


def context(chat_id: int, viewer: int | None = None, me: int | None = None, images: int = 0) -> list[dict]:
    """组装给 Router 的历史消息。viewer 是群聊里正在发言的 Bot：
    它自己的回复保持 assistant；别的 Bot 的发言、任务和交接都换成带名字的通知。私聊时 viewer 为空。
    me 是正在发言的 Bot（私聊也给），Bot 之间的私信按它的角度写成“谁私信了你 / 你回了谁”。
    讨论串：上级对话到被回复的那条消息为止，接着是讨论串里的消息（见 THREAD_NOTE）。
    images：模型能看图时最多带几张用户发的图片（最近的），带上的那几条消息的 content 是文字加图片的列表。"""
    n = CFG["memory"]["inline_messages"]
    rows, split, root = _rows(chat_id)
    names = dict(conn.execute("select id, name from bots"))
    me = viewer if viewer is not None else me
    name = lambda b: names.get(b, "已删除的 Bot")
    # 移出的分界每满 n 条才整体往后挪一次，留在上下文的有 n 到 2n-1 条。每来一条就挪一格的话，
    # “更早的对话”的索引和后面的整段历史每轮都变，模型服务的缓存一次都命中不了
    cut = (len(rows) - n) // n * n if len(rows) > n else 0
    old, recent = rows[:cut], rows[cut:]
    msgs = []
    if old:
        def who(r, b):
            return name(b) if viewer is not None and r == "assistant" and b != viewer else r
        index = "\n".join(f"#{i} [{who(r, b)}] {c[:60].replace(chr(10), ' ')}" for i, r, c, _, b in old[-200:])
        msgs.append({"role": "system", "content": "更早的对话已移出上下文，需要时用 recall(id) 取回原文：\n" + index})
    # 讨论串从哪一条开始；开头已经移出上下文的话，标在留下的第一条前面
    at = max(split, cut) if split is not None else -1
    shown = _pick_images(recent, images) if images else set()
    for k, (mid, r, c, meta, b) in enumerate(recent, cut):
        meta = json.loads(meta or "{}")
        if k == at:
            note = THREAD_NOTE.format(root=root)
            if r == "user":
                c = note + c
            else:
                msgs.append({"role": "user", "content": note})
        others = viewer is not None and b != viewer
        if r == "assistant" or meta.get("kind") == "result":
            c = _no_leak(c)
        if r == "task" and meta.get("kind") == "handoff" and viewer is not None:
            to = meta.get("to")
            if to == viewer:
                text = f"（{name(b)} 把工作交给了你，现在轮到你）{defang(meta.get('note') or c)}"
            elif b == viewer:
                text = f"（系统通知：你把工作交给了 {name(to)}）{defang(meta.get('note') or c)}"
            else:
                text = f"（系统通知：{name(b)} 把工作交给了 {name(to)}）{defang(meta.get('note') or c)}"
            msgs.append({"role": "user", "content": text})
        elif r == "task" and meta.get("kind") == "image":   # generate_image 画好的图
            whose = name(b) if others and b else "你"
            msgs.append({"role": "user", "content": f"（系统通知：{whose}画好了一张图，已经显示给用户。描述：{defang(meta.get('prompt') or c)}）"})
        elif r == "task" and meta.get("kind") == "plugin":   # 插件以 Bot 的名义发进对话的（services.Messenger）
            msgs.append({"role": "user", "content": f"（系统通知，非用户发言；插件「{meta.get('plugin', '')}」发到对话里的消息，"
                                                    f"用户已经看到）{defang(c)}"})
        elif r == "task" and meta.get("kind") in ("dm", "dm_reply"):
            msgs.append({"role": "user", "content": _dm_line(meta, defang(c), b, me, name)})
        elif r == "task":  # 任务记录作为系统通知给 Router，避免它模仿这种格式
            whose = f"{name(b)} 的" if others and b else ""
            note = f"（系统通知，非用户发言；这是{whose}任务结果，已作为卡片直接展示给用户，不用复述，除非用户问起）" \
                if meta.get("kind") == "result" else f"（系统通知，非用户发言{'；' + whose + '任务' if whose else ''}）"
            msgs.append({"role": "user", "content": note + defang(c)})
        elif r == "assistant" and others:
            msgs.append({"role": "user", "content": f"【{name(b)}】{defang(c)}"})
        elif r == "assistant" and "reasoning" in meta:
            # DeepSeek 思考模式：请求带工具时，历史回复的 reasoning_content 必须原样传回
            m = {"role": "assistant", "content": c}
            if meta["reasoning"] is not None:
                m["reasoning_content"] = meta["reasoning"]
            msgs.append(m)
        elif r == "assistant":
            # 旧记录没存思考内容，原样传回会被拒，改成通知形式
            msgs.append({"role": "user", "content": f"（系统通知：这是你之前给用户的回复）{c}"})
        else:
            if meta.get("via"):   # 在聊天软件里发的：Router 据此不提网页上的按钮和面板
                c = f"（来自{VIA.get(meta['via'], meta['via'])}）{c}"
            if (ap := meta.get("applet")) and ap.get("app"):   # 应用自带的界面（MCP Apps）替用户发的
                c = f"（来自应用 @{ap['app']} 的界面）{c}"
            elif ap:   # 小应用的界面替用户发的
                c = f"（来自小应用「{ap.get('title', '')}」）{c}"
            if meta.get("skill_prompt"):   # /技能：把技能做法附在用户原话后面
                c = f"{c}\n\n{meta['skill_prompt']}"
            parts = []
            if shown:
                from . import media
                parts = [p for a in meta.get("attachments") or [] if (mid, a.get("media")) in shown
                         and (p := media.image_part(a["media"]))]
            msgs.append({"role": "user", "content": [{"type": "text", "text": c}, *parts] if parts else c})
    return msgs


def _no_leak(c: str) -> str:
    """修好之前，DeepSeek 写进正文的工具调用（见 dsml.py）会原样存下来。传回给模型前去掉，免得它照着学。"""
    from . import dsml
    text, calls = dsml.split(c)
    if not calls:
        return text
    return text or f"（这里想调用 {'、'.join(x['name'] for x in calls)}，但写成了文字，没有执行）"


def _dm_line(meta: dict, c: str, sender: int | None, me: int | None, name) -> str:
    """Bot 之间的私信和回信（见 dm.py），按 me 的角度写。sender 是发信的 Bot，meta["to"] 是收信的。"""
    to = meta.get("to")
    if meta["kind"] == "dm":
        if to == me:
            return f"（{name(sender)} 私信了你，不是用户说的话）{c}"
        if sender == me:
            return f"（系统通知：你私信了 {name(to)}，等它回信）{c}"
        return f"（系统通知：{name(sender)} 私信了 {name(to)}）{c}"
    if meta.get("sent"):   # 写回信的那一边留的记录，回信内容就是上面那条回复或任务结果
        return f"（系统通知：{'你' if sender == me else name(sender)}已把回信发给 {name(to)}）"
    if meta.get("task_id") and meta.get("status") == "done":
        c = f"任务 #{meta['task_id']} 的结果：\n{c}"
    if to == me:
        return f"（{name(sender)} 回了你的私信，不是用户说的话）{c}"
    return f"（系统通知：{name(sender)} 回了 {name(to)} 的私信）{c}"


def _is_peak(ts: float) -> bool:
    t = time.localtime(ts)  # 假定服务器在北京时间
    if t.tm_wday >= 5:
        return False
    hm = time.strftime("%H:%M", t)
    return any(a <= hm < b for a, b in (r.split("-") for r in CFG["pricing"]["peak_hours"]))


# 这次模型调用是谁、在哪个对话里发起的：Router 说话、Worker 做任务时设上，调用记录（llm_calls）据此记到 Bot 头上。
# 画图这类经过好几层才调模型的，也能从这里认出是哪个 Bot（asyncio 新开的任务会带着它）
WHO: ContextVar[dict | None] = ContextVar("osa_usage_who", default=None)


def add_usage(role: str, model: str, *, task_id: int | None = None, provider: str | None = None,
              bot_id: int | None = None, chat_id: int | None = None, hit: int = 0, miss: int = 0, out: int = 0,
              cost: float = 0.0, ms: int | None = None, ttft: int | None = None, err: str = ""):
    """往 llm_calls 里记一行。bot_id / chat_id 不给就看 WHO，再不行从任务找回 Bot。"""
    who = WHO.get() or {}
    task_id = task_id if task_id is not None else who.get("task_id")
    bot_id = bot_id if bot_id is not None else who.get("bot_id")
    chat_id = chat_id if chat_id is not None else who.get("chat_id")
    if bot_id is None and task_id is not None:
        r = conn.execute("select bot_id, chat_id from messages where id=?", (task_id,)).fetchone()
        if r:
            bot_id, chat_id = r[0], chat_id if chat_id is not None else r[1]
    conn.execute("""insert into llm_calls(ts, role, model, task_id, cache_hit, cache_miss, output, cost,
        bot_id, chat_id, provider, ms, ttft, err) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (time.time(), role, model, task_id, hit, miss, out, cost, bot_id, chat_id, provider,
                  None if ms is None else int(ms), None if ttft is None else int(ttft), err or None))
    conn.commit()


def record_usage(role: str, model: str, usage, task_id: int | None = None, provider: str | None = None,
                 bot_id: int | None = None, chat_id: int | None = None, ms: float | None = None, ttft: float | None = None):
    """记录一次模型调用的 token 和费用。usage 是 OpenAI 格式的 usage 对象；provider 不给就按角色的配置。
    ms / ttft：整次用时、第一个字出来的时间（毫秒），知道的话给。
    服务没返回 usage 时：知道用时的（流式调用）照样记一次调用，token 和费用按 0；否则不记。"""
    if usage is None and ms is None:
        return
    prompt = (usage.prompt_tokens or 0) if usage else 0
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is None:  # 非 DeepSeek 接口：尝试 OpenAI 的 cached_tokens 字段
        details = getattr(usage, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", 0) or 0
    miss, out = prompt - hit, (usage.completion_tokens or 0) if usage else 0
    from . import prices
    provider = provider or (CFG["roles"].get(role) or {}).get("provider")
    cost = prices.cost(provider, model, hit, miss, out, _is_peak(time.time()))
    add_usage(role, model, task_id=task_id, provider=provider, bot_id=bot_id, chat_id=chat_id,
              hit=hit, miss=miss, out=out, cost=cost, ms=ms, ttft=ttft)


def record_error(role: str, model: str, error: str, task_id: int | None = None, provider: str | None = None,
                 bot_id: int | None = None, chat_id: int | None = None, ms: float | None = None):
    """一次没成功的模型调用（连不上、报错、超时）：记下原因，观测页据此算出错率。"""
    provider = provider or (CFG["roles"].get(role) or {}).get("provider")
    add_usage(role, model, task_id=task_id, provider=provider, bot_id=bot_id, chat_id=chat_id, ms=ms,
              err=(error or "error")[:300])


def tokens_today() -> int:
    return conn.execute("select coalesce(sum(cache_hit+cache_miss+output),0) from llm_calls where ts > ?",
                        (time.time() - 86400,)).fetchone()[0]


def usage_stats() -> dict:
    """费用面板用：今天、近 7 天、近 30 天的汇总，以及按角色、按任务、按天的拆分。"""
    now = time.time()
    day0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))

    def total(since):
        r = conn.execute("""select coalesce(sum(cost),0), coalesce(sum(cache_hit),0), coalesce(sum(cache_miss),0),
            coalesce(sum(output),0), count(*) from llm_calls where ts >= ? and err is null""", (since,)).fetchone()
        return {"cost": round(r[0], 4), "cache_hit": r[1], "cache_miss": r[2], "output": r[3], "calls": r[4],
                "cache_rate": round(r[1] / (r[1] + r[2]), 3) if r[1] + r[2] else 0}

    by_role = conn.execute("""select role, model, sum(cost), sum(cache_hit+cache_miss+output), count(*) from llm_calls
        where ts >= ? and err is null group by role, model order by 3 desc""", (now - 30 * 86400,)).fetchall()
    by_task = conn.execute("""select u.task_id, sum(u.cost), sum(u.cache_hit+u.cache_miss+u.output), m.content
        from llm_calls u left join messages m on m.id = u.task_id where u.task_id is not null
        group by u.task_id order by u.task_id desc limit 20""").fetchall()
    by_day = conn.execute("""select date(ts, 'unixepoch', 'localtime') d, sum(cost) from llm_calls
        where ts >= ? group by d order by d""", (now - 30 * 86400,)).fetchall()
    return {
        "currency": CFG["pricing"]["currency"],
        "today": total(day0), "week": total(now - 7 * 86400), "month": total(now - 30 * 86400),
        "by_role": [{"role": r, "model": m, "cost": round(c, 4), "tokens": t, "calls": n} for r, m, c, t, n in by_role],
        "by_task": [{"id": i, "cost": round(c, 4), "tokens": t, "goal": (g or "").removeprefix("[任务已派发] ")[:60]}
                    for i, c, t, g in by_task],
        "by_day": [{"day": d, "cost": round(c, 4)} for d, c in by_day],
    }
