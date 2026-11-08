"""任务过程：后台任务每一步想了什么、调了什么工具、拿到了什么。边做边推给前端，做完存着，随时能回看。

一步是 task_steps 里的一行：
- kind：think 模型的思考；text 正在写的文字（写完变成 say 或 answer）；say 调工具之前说的话；
  tool 一次工具调用（detail 是参数，body 是工具返回）；answer 写出的结果（Heavy 模式里各个研究员的草稿也是）；
  note 过程提示（步数用完、开始汇总、失败、取消）
- worker：Heavy 模式里是哪个研究员（h0、h1……），汇总是 review；普通任务为空
- status：running / waiting（等你确认）/ done / error / rejected / cancelled / interrupted（服务重启时还没做完）

开始、结束、标题和状态变化时写数据库；流式输出的文字只在内存里（LIVE）并推给前端，写完才存。
接口返回时用 LIVE 盖掉数据库里的旧值，任务做到一半才打开也能看到前面的内容。
流式文字最多每 0.3 秒推一次，带上这段从第几个字开始（at），前端据此去重。
"""
import asyncio
import json
import re
import time
from .config import CFG
from .memory import conn

notify = None   # 由 server 设置：async (event) -> None
LIVE: dict[int, dict] = {}
KEYS = ("id", "task_id", "ts", "worker", "kind", "title", "body", "status", "dur", "detail")
LIMIT = {"think": 20000, "say": 4000, "tool": 6000, "note": 2000}   # 其余（结果、草稿）6 万字
FLUSH_S = 0.3
OPEN = ("running", "waiting")


def public(s: dict) -> dict:
    d = {k: s[k] for k in KEYS}
    if s["status"] in OPEN and "_sent" in s:
        d["body"] = s["body"][:s["_sent"]]   # 没推出去的部分留给下一次 append，免得重复
    return d


def save(s: dict):
    conn.execute("update task_steps set kind=?, title=?, body=?, status=?, dur=? where id=?",
                 (s["kind"], s["title"], s["body"], s["status"], s["dur"], s["id"]))
    conn.commit()


class Log:
    """一个 Worker 的过程记录。Heavy 模式里每个研究员各一个，汇总一个。"""

    def __init__(self, task, worker: str = ""):
        self.task, self.worker = task, worker
        self.current: dict | None = None   # 正在执行的工具：进度说明（排队等电脑、动作）写进它的标题

    async def _emit(self, ev: dict):
        if notify:
            await notify({"type": "task_step", "chat_id": self.task.chat_id, "bot_id": self.task.bot_id,
                          "task_id": self.task.id} | ev)

    async def start(self, kind: str, title: str = "", detail: str = "", status: str = "running") -> dict:
        now = time.time()
        sid = conn.execute("insert into task_steps(task_id, ts, worker, kind, title, body, status, dur, detail, name) "
                           "values(?,?,?,?,?,?,?,?,?,?)",
                           (self.task.id, now, self.worker, kind, title, "", status, 0, detail,
                            title if kind == "tool" else "")).lastrowid
        conn.commit()
        s = {"id": sid, "task_id": self.task.id, "ts": now, "worker": self.worker, "kind": kind, "title": title,
             "body": "", "status": status, "dur": 0, "detail": detail, "_sent": 0, "_t": now}
        if status in OPEN:
            LIVE[sid] = s
        await self._emit({"step": public(s)})
        return s

    async def note(self, text: str, status: str = "done") -> dict:
        return await self.start("note", text[:LIMIT["note"]], status=status)

    async def update(self, s: dict, **fields):
        """改标题、状态（进度说明、等你确认）。也写进库：服务中途重启时，回看能看到做到了哪个动作。"""
        s.update(fields)
        conn.execute("update task_steps set title=?, status=? where id=?", (s["title"], s["status"], s["id"]))
        conn.commit()
        await self._emit({"step": public(s)})

    async def append(self, s: dict, text: str):
        s["body"] += text
        now = time.time()
        if now - s["_t"] >= FLUSH_S:
            at = s["_sent"]
            s["_sent"], s["_t"] = len(s["body"]), now
            await self._emit({"id": s["id"], "at": at, "append": s["body"][at:]})

    async def finish(self, s: dict, status: str = "done", body: str | None = None, **fields):
        if s["status"] not in OPEN:   # 已经结束（比如任务取消时被 close 收掉了）
            return
        if body is not None:
            s["body"] = body
        s.update(fields, status=status, dur=round(time.time() - s["ts"], 1))
        s["body"] = s["body"][:LIMIT.get(s["kind"], 60000)]
        save(s)
        LIVE.pop(s["id"], None)
        if self.current is s:
            self.current = None
        await self._emit({"step": public(s)})


async def close(task, status: str, text: str = ""):
    """任务结束：还没做完的步骤跟着结束；失败、取消的话记一笔原因。"""
    for s in [s for s in LIVE.values() if s["task_id"] == task.id]:
        await Log(task, s["worker"]).finish(s, "cancelled" if status == "cancelled" else "error")
    if text:
        await Log(task).note(text, "error" if status == "failed" else status)


def steps(task_id: int) -> list[dict]:
    rows = conn.execute(f"select {', '.join(KEYS)} from task_steps where task_id=? order by id", (task_id,))
    return [public(LIVE[r[0]]) if r[0] in LIVE else dict(zip(KEYS, r)) for r in rows]


# ---------- 任务列表 ----------
# 任务的编号就是“任务已派发”那条消息的 id；状态、用时、步数在任务结束时写回这条消息的 meta
def _task(row) -> dict:
    i, ts, content, meta, chat_id, bot_id = row
    m = json.loads(meta or "{}")
    return {"id": i, "ts": ts, "goal": content.removeprefix("[任务已派发]").strip(), "mode": m.get("mode", "single"),
            "status": m.get("status", "done"), "elapsed": m.get("elapsed"), "steps": m.get("steps"),
            "chat_id": chat_id, "bot_id": bot_id, "handoff_to": m.get("handoff_to")}


DISPATCH = "role='task' and (json_extract(meta, '$.kind')='dispatch' or content like '[任务已派发]%')"


def list_tasks(bot_id: int | None = None, chat_id: int | None = None, before: int | None = None,
               limit: int = 30) -> list[dict]:
    where, args = [DISPATCH, "id < ?"], [before or 1 << 62]
    if bot_id:
        where.append("bot_id=?")
        args.append(bot_id)
    if chat_id:   # 连同这个对话下面的讨论串里派的
        where.append("chat_id in (select ? union select id from chats where kind='thread' and parent=?)")
        args += [chat_id, chat_id]
    rows = conn.execute(f"select id, ts, content, meta, chat_id, bot_id from messages where {' and '.join(where)} "
                        "order by id desc limit ?", (*args, limit)).fetchall()
    return [_task(r) for r in rows]


def detail(task_id: int) -> dict | None:
    r = conn.execute(f"select id, ts, content, meta, chat_id, bot_id from messages where id=? and {DISPATCH}",
                     (task_id,)).fetchone()
    if not r:
        return None
    t = _task(r)
    res = conn.execute("""select content, meta, ts from messages where chat_id=? and id>? and role='task'
        and json_extract(meta, '$.task_id')=? and json_extract(meta, '$.kind')='result' order by id limit 1""",
                       (t["chat_id"], task_id, task_id)).fetchone()
    if res:
        t["result"] = re.sub(r"^\[任务 #\d+ 结果\]\n?", "", res[0])
        t["finished"] = res[2]
        t["elapsed"] = t["elapsed"] or json.loads(res[1] or "{}").get("elapsed")
    cost, tokens = conn.execute("select coalesce(sum(cost), 0), coalesce(sum(cache_hit+cache_miss+output), 0) "
                                "from llm_calls where task_id=?", (task_id,)).fetchone()
    t |= {"cost": round(cost, 4), "tokens": tokens, "currency": CFG["pricing"]["currency"]}
    return t


# ---------- 启动和清理 ----------
def startup():
    """服务启动时：上次没做完的任务都已经停了（任务只在内存里跑），标成“已中断”。"""
    conn.execute("update task_steps set status='interrupted' where status in ('running', 'waiting')")
    conn.execute(f"""update messages set meta=json_set(meta, '$.status', 'interrupted')
        where {DISPATCH} and json_extract(meta, '$.status')='running'""")
    conn.commit()
    prune()


def prune():
    """只留最近 memory.task_log_days 天的过程（默认 90，0 表示一直留着）。任务结果在对话里，不受影响。"""
    days = (CFG.get("memory") or {}).get("task_log_days", 90)
    if days:
        conn.execute("delete from task_steps where ts < ?", (time.time() - days * 86400,))
        conn.commit()


async def prune_loop():
    while True:
        await asyncio.sleep(86400)
        prune()
