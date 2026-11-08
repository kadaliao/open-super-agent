"""观测：Bot 们花了多少、做了多少、快不快、哪里出了问题（侧栏“观测”页）。

只做统计，数据都是现成的记录：
- llm_calls：每次模型调用的 token、费用、用时（ms）、第一个字多久出来（ttft）、失败原因（err），记在哪个 Bot、哪个任务头上
  （memory.record_usage / record_error）。升级前的对话回复没记 Bot，归到“没记录是哪个 Bot”。
- messages 里的任务：“任务已派发”那条，meta 里有状态、用时、步数；定时任务派的带着“[定时任务 名字]”标记。
- task_steps 里的工具调用（name、status、dur）。过程只留 memory.task_log_days 天，更早的工具统计不到。
时间按看的人的时区分桶：前端传 tz（Date.getTimezoneOffset()，分钟，东八区是 -480）。
"""
import csv
import io
import json
import re
import time
from .config import CFG
from .memory import conn
from . import routines, tasklog

DAY, WEEK = 86400, 7 * 86400
RANGES = {"today": 1, "7d": 7, "30d": 30, "all": None}
TOP = 30          # 最近的任务、问题各列多少条
ROUTINE = re.compile(r"^\[任务已派发\] \[(?:定时任务|事件) (.+?)\] ")   # i18n-ignore：存在消息里的标记
UNKNOWN = -1      # 没记录是哪个 Bot 的调用


def _bot_sql(bot: int | None, col: str = "bot_id") -> tuple[str, tuple]:
    if bot is None:
        return "", ()
    if bot == UNKNOWN:
        return f" and {col} is null", ()
    return f" and {col}=?", (bot,)


def window(rng: str, tz: int, now: float | None = None) -> dict:
    """时间范围：开始、结束（都是时间戳）、分桶的大小，以及用来比较的上一段。"""
    now = now or time.time()
    off = tz * 60                       # 本地时间 = UTC - off
    today0 = (now - off) // DAY * DAY + off
    days = RANGES.get(rng, 7)
    if days is None:
        first = min([x for x in (conn.execute("select min(ts) from llm_calls").fetchone()[0],
                                 conn.execute(f"select min(ts) from messages where {tasklog.DISPATCH}").fetchone()[0])
                     if x] or [today0])
        start = (first - off) // DAY * DAY + off
        if (today0 - start) / DAY + 1 > 120:   # 太长就按周，周一开头
            start -= ((start - off) // DAY + 3) % 7 * DAY
            size = WEEK
        else:
            size = DAY
        return {"start": start, "end": today0 + DAY, "size": size, "prev": None}
    start = today0 - (days - 1) * DAY
    return {"start": start, "end": today0 + DAY, "size": 3600 if days == 1 else DAY,
            "prev": (start - days * DAY, start)}


# ---------- 模型调用 ----------
_U = """coalesce(sum(cost),0), coalesce(sum(cache_hit+cache_miss+output),0), coalesce(sum(cache_hit),0),
    coalesce(sum(cache_miss),0), coalesce(sum(output),0), sum(case when err is null then 1 else 0 end),
    sum(case when err is not null then 1 else 0 end)"""


def _usage_kpi(a: float, b: float, bot: int | None) -> dict:
    w, args = _bot_sql(bot)
    cost, tokens, hit, miss, out, calls, errors = conn.execute(
        f"select {_U} from llm_calls where ts>=? and ts<?{w}", (a, b, *args)).fetchone()
    # 首字：只看对话回复（Router），任务里的调用要先读一大堆资料，慢是正常的
    ttft = conn.execute(f"select avg(ttft) from llm_calls where ts>=? and ts<? and role='router' and err is null "
                        f"and ttft is not null{w}", (a, b, *args)).fetchone()[0]
    return {"cost": round(cost, 4), "tokens": tokens, "input": hit + miss, "output": out, "cache_hit": hit,
            "cache_rate": round(hit / (hit + miss), 3) if hit + miss else 0, "calls": calls or 0, "errors": errors or 0,
            "ttft": round(ttft) if ttft is not None else None, "speed": _speed(f"ts>=? and ts<?{w}", (a, b, *args))}


def _speed(where: str, args: tuple) -> float | None:
    """输出速度（token/秒）：第一个字出来之后，每秒出多少 token。"""
    r = conn.execute(f"select sum(output), sum(ms-ttft) from llm_calls where {where} and err is null and output>0 "
                     "and ttft is not null and ms>ttft", args).fetchone()
    return round(r[0] / (r[1] / 1000), 1) if r[1] else None


def _usage_groups(w: dict, bot: int | None) -> dict:
    a, b, size = w["start"], w["end"], w["size"]
    ws, args = _bot_sql(bot)
    base = f"from llm_calls where ts>=? and ts<?{ws}"
    series = conn.execute(f"select cast((ts-?)/? as integer), coalesce(bot_id,{UNKNOWN}), {_U} {base} group by 1, 2",
                          (a, size, a, b, *args)).fetchall()
    by_bot = conn.execute(f"select coalesce(bot_id,{UNKNOWN}), {_U}, avg(case when role='router' and err is null then ttft end) "
                          f"{base} group by 1", (a, b, *args)).fetchall()
    models = conn.execute(f"select coalesce(provider,''), model, {_U}, avg(case when err is null then ttft end), "
                          f"max(ts) {base} group by 1, 2 order by 1 desc", (a, b, *args)).fetchall()
    roles = conn.execute(f"select role, {_U} {base} group by 1 order by 2 desc", (a, b, *args)).fetchall()
    off = w["tz"] * 60
    hours = conn.execute(f"""select (cast((ts-?)/86400 as integer)+3)%7, (cast(ts-? as integer)%86400)/3600, count(*)
        {base} group by 1, 2""", (off, off, a, b, *args)).fetchall()
    return {"series": series, "by_bot": by_bot, "models": models, "roles": roles, "hours": hours}


# ---------- 任务 ----------
def _tasks(a: float, b: float, bot: int | None) -> list[dict]:
    ws, args = _bot_sql(bot)
    rows = conn.execute(f"select id, ts, content, meta, chat_id, bot_id from messages where {tasklog.DISPATCH} "
                        f"and ts>=? and ts<?{ws} order by ts desc", (a, b, *args)).fetchall()
    out = []
    for i, ts, content, meta, chat_id, bot_id in rows:
        m = json.loads(meta or "{}")
        hit = ROUTINE.match(content or "")
        out.append({"id": i, "ts": ts, "goal": ROUTINE.sub("", content or "").removeprefix("[任务已派发]").strip(),
                    "status": m.get("status", "done"), "elapsed": m.get("elapsed"), "steps": m.get("steps"),
                    "mode": m.get("mode", "single"), "quiet": bool(m.get("quiet")), "chat_id": chat_id, "bot_id": bot_id,
                    "routine": hit.group(1) if hit else None})
    return out


def _task_kpi(tasks: list[dict]) -> dict:
    n = {k: 0 for k in ("done", "failed", "cancelled", "running", "interrupted")}
    for t in tasks:
        n[t["status"] if t["status"] in n else "done"] += 1
    ended = n["done"] + n["failed"] + n["interrupted"]   # 你自己取消的不算失败
    took = [t["elapsed"] for t in tasks if t["status"] == "done" and t["elapsed"]]
    return {"tasks": len(tasks), **n, "success": round(n["done"] / ended, 3) if ended else None,
            "avg_s": round(sum(took) / len(took)) if took else None,
            "routine_runs": sum(1 for t in tasks if t["routine"]),
            "routine_failed": sum(1 for t in tasks if t["routine"] and t["status"] in ("failed", "interrupted")),
            "routine_quiet": sum(1 for t in tasks if t["routine"] and t["quiet"])}


def _costs(ids: list[int]) -> dict[int, tuple[float, int]]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {i: (c, t) for i, c, t in conn.execute(
        f"select task_id, sum(cost), sum(cache_hit+cache_miss+output) from llm_calls where task_id in ({q}) group by 1", ids)}


# ---------- 工具 ----------
def _tools(a: float, b: float, bot: int | None) -> list[dict]:
    ws, args = _bot_sql(bot, "m.bot_id")
    rows = conn.execute(f"""select coalesce(nullif(s.name,''), s.title), count(*),
        sum(case when s.status='error' then 1 else 0 end), sum(case when s.status='rejected' then 1 else 0 end),
        avg(case when s.status='done' then s.dur end), max(s.ts)
        from task_steps s join messages m on m.id=s.task_id where s.kind='tool' and s.ts>=? and s.ts<?{ws}
        group by 1 order by 2 desc""", (a, b, *args)).fetchall()
    return [{"name": n, "calls": c, "errors": e, "rejected": r, "avg_s": round(d, 1) if d is not None else None, "last": l}
            for n, c, e, r, d, l in rows]


# ---------- 出了问题的地方 ----------
def _problems(a: float, b: float, bot: int | None, tasks: list[dict]) -> list[dict]:
    """失败、中断的任务，模型调用出错，工具出错。一样的问题合成一条，记次数和最后一次。"""
    items: list[dict] = []
    bad = [t for t in tasks if t["status"] in ("failed", "interrupted")]
    if bad:
        q = ",".join("?" * len(bad))
        why = {tid: c for tid, c in conn.execute(
            f"""select json_extract(meta, '$.task_id'), content from messages where role='task'
            and json_extract(meta, '$.kind')='result' and json_extract(meta, '$.task_id') in ({q})""", [t["id"] for t in bad])}
        for t in bad:
            detail = re.sub(r"^\[任务 #\d+ 结果\]\n?", "", why.get(t["id"], ""))[:300]   # i18n-ignore：存在消息里的标记
            items.append({"kind": "task", "status": t["status"], "ts": t["ts"], "bot_id": t["bot_id"], "task_id": t["id"],
                          "chat_id": t["chat_id"], "title": t["goal"][:120], "detail": detail, "routine": t["routine"]})
    ws, args = _bot_sql(bot)
    for ts, bot_id, role, provider, model, err, task_id, chat_id in conn.execute(
            f"""select ts, bot_id, role, provider, model, err, task_id, chat_id from llm_calls
            where err is not null and ts>=? and ts<?{ws} order by ts desc limit 500""", (a, b, *args)):
        items.append({"kind": "model", "ts": ts, "bot_id": bot_id, "task_id": task_id, "chat_id": chat_id,
                      "title": f"{provider + '/' if provider else ''}{model}", "detail": err, "role": role})
    ws, args = _bot_sql(bot, "m.bot_id")
    for ts, name, body, task_id, bot_id, chat_id in conn.execute(
            f"""select s.ts, coalesce(nullif(s.name,''), s.title), s.body, s.task_id, m.bot_id, m.chat_id
            from task_steps s join messages m on m.id=s.task_id where s.kind='tool' and s.status='error'
            and s.ts>=? and s.ts<?{ws} order by s.ts desc limit 500""", (a, b, *args)):
        items.append({"kind": "tool", "ts": ts, "bot_id": bot_id, "task_id": task_id, "chat_id": chat_id,
                      "title": name, "detail": (body or "")[:300]})
    # 合并：同一个 Bot 的同一种问题、开头一样的原因算一条（任务各算各的）
    merged: dict[tuple, dict] = {}
    for it in sorted(items, key=lambda x: -x["ts"]):
        key = (it["kind"], it["bot_id"], it["title"], it["detail"][:60]) if it["kind"] != "task" else ("task", it["task_id"])
        if key in merged:
            merged[key]["count"] += 1
        else:
            merged[key] = it | {"count": 1}
    return list(merged.values())[:TOP]


# ---------- 定时任务 ----------
def _routines(bot: int | None, tasks: list[dict], costs: dict) -> list[dict]:
    if bot == UNKNOWN:
        return []
    out = []
    for r in routines.list_for(bot):
        mine = [t for t in tasks if t["routine"] == r["name"] and t["bot_id"] == r["bot_id"]]
        n = {k: sum(1 for t in mine if t["status"] == k) for k in ("done", "failed", "cancelled", "interrupted")}
        out.append({"id": r["id"], "bot_id": r["bot_id"], "name": r["name"], "enabled": r["enabled"], "trigger": r["trigger"],
                    "describe": r["describe"], "category": r["category"],
                    "next_run": r["next_run"], "running": r["running"], "last_run": r["last_run"],
                    "runs": len(mine), **n, "quiet": sum(1 for t in mine if t["quiet"]),
                    "cost": round(sum(costs.get(t["id"], (0, 0))[0] for t in mine), 4)})
    return out


def stats(rng: str = "7d", tz: int = 0, bot: int | None = None, now: float | None = None) -> dict:
    w = window(rng, tz, now) | {"tz": tz, "range": rng if rng in RANGES else "7d"}
    a, b, size = w["start"], w["end"], w["size"]
    n = int((b - a + size - 1) // size)
    g = _usage_groups(w, bot)
    tasks = _tasks(a, b, bot)
    costs = _costs([t["id"] for t in tasks])

    # 趋势：每个桶的费用、token、调用、任务，按 Bot 拆开
    series = [{"t": a + i * size, "cost": 0.0, "tokens": 0, "calls": 0, "errors": 0, "tasks": 0, "bots": {}} for i in range(n)]
    def cell(i: int, bot_id: int) -> dict:
        return series[i]["bots"].setdefault(str(bot_id), {"cost": 0.0, "tokens": 0, "calls": 0, "tasks": 0})
    for i, bot_id, cost, tokens, _, _, _, calls, errors in g["series"]:
        if 0 <= i < n:
            s, c = series[i], cell(i, bot_id)
            s["cost"] += cost; s["tokens"] += tokens; s["calls"] += calls; s["errors"] += errors
            c["cost"] += cost; c["tokens"] += tokens; c["calls"] += calls
    for t in tasks:
        i = int((t["ts"] - a) // size)
        if 0 <= i < n:
            series[i]["tasks"] += 1
            cell(i, t["bot_id"] if t["bot_id"] is not None else UNKNOWN)["tasks"] += 1
    for s in series:
        s["cost"] = round(s["cost"], 4)
        for c in s["bots"].values():
            c["cost"] = round(c["cost"], 4)

    # 按 Bot
    bots: dict[int, dict] = {}
    def bot_row(i: int) -> dict:
        return bots.setdefault(i, {"bot_id": i, "cost": 0.0, "tokens": 0, "calls": 0, "errors": 0, "ttft": None,
                                   "tasks": 0, "done": 0, "failed": 0, "avg_s": None, "routine_runs": 0})
    for bot_id, cost, tokens, _, _, _, calls, errors, ttft in g["by_bot"]:
        r = bot_row(bot_id)
        r.update(cost=round(cost, 4), tokens=tokens, calls=calls or 0, errors=errors or 0,
                 ttft=round(ttft) if ttft is not None else None)
    for bot_id in {t["bot_id"] for t in tasks}:
        k = _task_kpi([t for t in tasks if t["bot_id"] == bot_id])
        bot_row(bot_id if bot_id is not None else UNKNOWN).update(
            tasks=k["tasks"], done=k["done"], failed=k["failed"] + k["interrupted"], avg_s=k["avg_s"],
            success=k["success"], routine_runs=k["routine_runs"])

    models = []
    for provider, model, cost, tokens, hit, miss, out, calls, errors, ttft, last in g["models"]:
        models.append({"provider": provider, "model": model, "cost": round(cost, 4), "tokens": tokens, "output": out,
                       "cache_rate": round(hit / (hit + miss), 3) if hit + miss else 0, "calls": calls or 0,
                       "errors": errors or 0, "ttft": round(ttft) if ttft is not None else None, "last": last,
                       "speed": _speed("ts>=? and ts<? and coalesce(provider,'')=? and model=?" + _bot_sql(bot)[0],
                                       (a, b, provider, model, *_bot_sql(bot)[1]))})
    models.sort(key=lambda m: (-m["cost"], -m["calls"]))

    tools = _tools(a, b, bot)
    kpi = _usage_kpi(a, b, bot) | _task_kpi(tasks)
    kpi["tool_calls"] = sum(x["calls"] for x in tools)
    kpi["tool_errors"] = sum(x["errors"] for x in tools)
    prev = None
    if w["prev"]:
        pa, pb = w["prev"]
        prev = _usage_kpi(pa, pb, bot) | _task_kpi(_tasks(pa, pb, bot))

    recent = [t | {"cost": round(costs.get(t["id"], (0, 0))[0], 4), "tokens": costs.get(t["id"], (0, 0))[1]}
              for t in tasks[:TOP]]
    heat = [[0] * 24 for _ in range(7)]
    for d, h, c in g["hours"]:
        heat[int(d)][int(h)] = c
    return {
        "currency": CFG["pricing"]["currency"], "range": w["range"], "start": a, "end": b, "size": size,
        "kpi": kpi, "prev": prev, "series": series,
        "bots": sorted(bots.values(), key=lambda r: (-r["cost"], -r["tasks"])),
        "models": models,
        "roles": [{"role": r, "cost": round(c, 4), "tokens": t, "calls": n or 0, "errors": e or 0}
                  for r, c, t, _, _, _, n, e in g["roles"]],
        "tools": tools, "routines": _routines(bot, tasks, costs), "tasks": recent,
        "problems": _problems(a, b, bot, tasks), "hours": heat,
    }


# ---------- 调用明细 ----------
CALL_COLS = ("id", "ts", "bot_id", "chat_id", "task_id", "role", "provider", "model", "input", "cache_hit", "output",
             "cost", "ms", "ttft", "err")


def calls(rng: str = "7d", tz: int = 0, bot: int | None = None, before: int | None = None, limit: int = 50,
          failed: bool = False, now: float | None = None) -> dict:
    """一次次模型调用，新的在前。before：上一页最后一条的 id。"""
    w = window(rng, tz, now)
    ws, args = _bot_sql(bot)
    where = f"ts>=? and ts<?{ws}" + (" and err is not null" if failed else "")
    q = f"""select id, ts, bot_id, chat_id, task_id, role, provider, model, cache_hit+cache_miss, cache_hit, output,
        cost, ms, ttft, err from llm_calls where {where}"""
    rows = conn.execute(q + " and id<? order by id desc limit ?",
                        (w["start"], w["end"], *args, before or 1 << 62, limit + 1)).fetchall()
    total = conn.execute(f"select count(*) from llm_calls where {where}", (w["start"], w["end"], *args)).fetchone()[0]
    return {"items": [dict(zip(CALL_COLS, r)) for r in rows[:limit]], "more": len(rows) > limit, "total": total}


def calls_csv(rng: str = "7d", tz: int = 0, bot: int | None = None, names: dict | None = None) -> str:
    """调用明细导出成 CSV（Excel 能直接打开：带 BOM）。时间按看的人的时区写。"""
    w = window(rng, tz)
    ws, args = _bot_sql(bot)
    rows = conn.execute(f"""select id, ts, bot_id, chat_id, task_id, role, provider, model, cache_hit+cache_miss, cache_hit,
        output, cost, ms, ttft, err from llm_calls where ts>=? and ts<?{ws} order by id""",
                        (w["start"], w["end"], *args)).fetchall()
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["time", "bot", *CALL_COLS[2:]])
    for r in rows:
        local = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r[1] - tz * 60))
        out.writerow([local, (names or {}).get(r[2], ""), *("" if v is None else v for v in r[2:])])
    return "﻿" + buf.getvalue()
