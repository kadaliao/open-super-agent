"""观测：模型调用记到 Bot、对话、任务头上（带用时、失败原因）；统计按时间、Bot、模型、工具、定时任务拆开。"""
import time

import openai._base_client
import pytest
from conftest import find, settle, until
from fakellm import Call
from osa import bots, memory, observe, routines, server

TZ = -480   # 东八区


@pytest.fixture
def make_bot(make_bot):
    """用完就删：测试库里 Bot 有 50 个的上限，大家共用。"""
    made = []

    def make(name: str = "") -> dict:
        made.append(make_bot(name))
        return made[-1]
    yield make
    for b in made:
        if bots.get(b["id"]) and not bots.get(b["id"]).get("is_primary"):
            bots.delete(b["id"])


def _usage(bot_id: int) -> list[dict]:
    rows = memory.conn.execute("""select role, chat_id, task_id, ms, ttft, err, output from llm_calls
        where bot_id=? order by id""", (bot_id,)).fetchall()
    return [dict(zip(("role", "chat_id", "task_id", "ms", "ttft", "err", "output"), r)) for r in rows]


async def test_calls_are_recorded_against_bot_chat_and_task(llm, events, make_bot):
    a = make_bot("Watcher")
    llm.script("router:Watcher", Call("dispatch", {"goal": "查一下天气"}), "派出去了")
    llm.script("worker", "晴")
    await server.safe_handle(bots.chat(a["chat_id"]), "查天气")
    await until(lambda: find(a["chat_id"], kind="result"), what="任务结果")
    await settle()

    rows = _usage(a["id"])
    router = [r for r in rows if r["role"] == "router"]
    worker = [r for r in rows if r["role"] == "worker"]
    assert router and worker
    assert all(r["chat_id"] == a["chat_id"] and r["task_id"] is None for r in router)   # 对话里的回复
    task_id = find(a["chat_id"], kind="dispatch")[0]
    assert all(r["task_id"] is not None for r in worker)                                 # 任务里的调用
    assert all(r["ms"] is not None and r["ttft"] is not None and r["err"] is None for r in rows)
    assert memory.WHO.get() is None                                                      # 说完一轮就清掉
    assert task_id


async def test_failed_model_call_is_recorded(llm, events, make_bot, monkeypatch):
    monkeypatch.setattr(openai._base_client.BaseClient, "_calculate_retry_timeout", lambda self, *a, **kw: 0)
    b = make_bot("Broken")
    llm.fail(500, 500, 500)   # 假模型客户端默认重试两次：三次都失败
    await server.safe_handle(bots.chat(b["chat_id"]), "你好")
    await settle()

    rows = _usage(b["id"])
    assert len(rows) == 1 and rows[0]["err"] and rows[0]["output"] == 0
    s = observe.stats("today", TZ, b["id"])
    assert s["kpi"]["errors"] == 1 and s["kpi"]["calls"] == 0
    assert [p["kind"] for p in s["problems"]] == ["model"]


def _task(bot: dict, goal: str, status: str, elapsed: int = 10, result: str = "") -> int:
    tid = memory.add("task", f"[任务已派发] {goal}", bot["chat_id"], bot["id"], kind="dispatch", mode="single",
                     status=status, elapsed=elapsed, steps=2)
    if result:
        memory.add("task", f"[任务 #{tid} 结果]\n{result}", bot["chat_id"], bot["id"], kind="result", task_id=tid,
                   status=status)
    return tid


def _tool(task_id: int, name: str, status: str, dur: float = 1.0):
    memory.conn.execute("""insert into task_steps(task_id, ts, worker, kind, title, body, status, dur, detail, name)
        values(?,?,'','tool','点击“提交”',?,?,?,'',?)""", (task_id, time.time(), "找不到这个按钮", status, dur, name))
    memory.conn.commit()


def test_stats_split_by_time_bot_model_tool_and_routine(make_bot):
    a = make_bot()
    routines.save(a["id"], {"name": "早报", "prompt": "整理新闻", "trigger": {"kind": "every", "minutes": 60}})
    memory.add_usage("router", "m1", provider="p", bot_id=a["id"], chat_id=a["chat_id"], hit=60, miss=40, out=12,
                     cost=0.5, ms=2000, ttft=500)
    memory.add_usage("worker", "m1", provider="p", bot_id=a["id"], ms=100, err="连不上")
    memory.add_usage("worker", "m1", provider="p", bot_id=a["id"], ms=120, err="连不上")
    failed = _task(a, "[定时任务 早报] 整理新闻", "failed", 30, "任务失败：超时了")
    done = _task(a, "查天气", "done", 10, "晴")
    _task(a, "算了不做了", "cancelled")
    _tool(done, "browser_click", "error", 1.5)
    _tool(done, "browser_click", "done", 0.5)
    _tool(done, "web_search", "rejected")

    s = observe.stats("7d", TZ, a["id"])
    k = s["kpi"]
    assert len(s["series"]) == 7 and s["size"] == 86400
    assert (k["cost"], k["calls"], k["errors"], k["tokens"]) == (0.5, 1, 2, 112)
    assert k["cache_rate"] == 0.6 and k["ttft"] == 500
    assert k["speed"] == 8.0                                      # 12 个 token / 1.5 秒
    assert (k["tasks"], k["done"], k["failed"], k["cancelled"]) == (3, 1, 1, 1)
    assert k["success"] == 0.5                                    # 你自己取消的不算失败
    assert (k["routine_runs"], k["routine_failed"]) == (1, 1)
    assert (k["tool_calls"], k["tool_errors"]) == (3, 1)
    assert s["series"][-1]["cost"] == 0.5 and s["series"][-1]["tasks"] == 3
    assert s["series"][-1]["bots"][str(a["id"])]["tasks"] == 3
    assert sum(map(sum, s["hours"])) == 3                         # 活跃时段：每次模型调用算一次

    assert [b["bot_id"] for b in s["bots"]] == [a["id"]]
    assert s["bots"][0]["failed"] == 1 and s["bots"][0]["routine_runs"] == 1
    m = s["models"][0]
    assert (m["provider"], m["model"], m["calls"], m["errors"]) == ("p", "m1", 1, 2)
    tools = {t["name"]: t for t in s["tools"]}
    assert tools["browser_click"]["calls"] == 2 and tools["browser_click"]["errors"] == 1
    assert tools["browser_click"]["avg_s"] == 0.5 and tools["web_search"]["rejected"] == 1
    r = next(x for x in s["routines"] if x["name"] == "早报")
    assert (r["runs"], r["failed"], r["done"]) == (1, 1, 0)

    probs = {p["kind"]: p for p in s["problems"]}
    assert set(probs) == {"task", "model", "tool"}
    assert probs["task"]["task_id"] == failed and probs["task"]["routine"] == "早报"
    assert probs["task"]["detail"] == "任务失败：超时了" and probs["task"]["title"] == "整理新闻"
    assert probs["model"]["count"] == 2                           # 一样的错合成一条
    assert probs["tool"]["title"] == "browser_click"              # 工具名，不是被进度盖掉的标题

    today = observe.stats("today", TZ, a["id"])
    assert len(today["series"]) == 24 and today["size"] == 3600
    assert today["prev"]["cost"] == 0                              # 和昨天比
    assert observe.stats("all", TZ, a["id"])["prev"] is None


def test_unknown_bot_and_window_alignment(make_bot):
    # 东八区 2026-11-08 10:00 → 7 天的范围从 11-02 零点开始
    now = time.mktime((2026, 11, 8, 2, 0, 0, 0, 0, 0)) - time.timezone   # UTC 02:00 = 北京 10:00
    w = observe.window("7d", TZ, now)
    assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(w["start"] - TZ * 60)) == "2026-11-02 00:00"
    assert w["end"] - w["start"] == 7 * 86400
    t = observe.window("today", TZ, now)
    assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(t["start"] - TZ * 60)) == "2026-11-08 00:00"
    assert t["prev"] == (t["start"] - 86400, t["start"])
    # 升级前对话里的回复没记 Bot：能单独看（bot=-1）
    memory.add_usage("router", "legacy-model", provider="p", cost=0.25)
    s = observe.stats("today", TZ, observe.UNKNOWN)
    assert any(m["model"] == "legacy-model" for m in s["models"]) and s["routines"] == []


def test_calls_page_and_csv(make_bot):
    a = make_bot("Ledger")
    for i in range(3):
        memory.add_usage("router", f"m{i}", provider="p", bot_id=a["id"], hit=1, miss=2, out=3, cost=0.01, ms=900)
    first = observe.calls("today", TZ, a["id"], limit=2)
    assert first["total"] == 3 and first["more"] and [c["model"] for c in first["items"]] == ["m2", "m1"]
    assert first["items"][0]["input"] == 3 and first["items"][0]["ms"] == 900
    rest = observe.calls("today", TZ, a["id"], before=first["items"][-1]["id"], limit=2)
    assert [c["model"] for c in rest["items"]] == ["m0"] and not rest["more"]
    assert observe.calls("today", TZ, a["id"], failed=True)["total"] == 0

    text = observe.calls_csv("today", TZ, a["id"], {a["id"]: "Ledger"})
    lines = text.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("time,bot,bot_id") and len(lines) == 4
    assert all(",Ledger," in line for line in lines[1:])


def test_rows_from_old_versions_are_moved_once(make_bot):
    """旧版本照旧往 llm_usage 按位置插 8 个值（退回旧版本也不出错）；再启动新版本时搬进 llm_calls，Bot 从任务找回。"""
    a = make_bot()
    tid = _task(a, "旧版本派的任务", "done")
    ts = time.time() + 1000   # 比已经搬过的都新
    memory.conn.execute("insert into llm_usage values(?,?,?,?,?,?,?,?)", (ts, "worker", "old-model", tid, 5, 5, 5, 0.02))
    memory.conn.execute("insert into llm_usage values(?,?,?,?,?,?,?,?)", (ts + 1, "router", "old-model", None, 1, 1, 1, 0.01))
    memory.conn.commit()
    memory._move_legacy_usage()
    memory._move_legacy_usage()   # 再来一次不会重复
    rows = memory.conn.execute("select role, bot_id, chat_id, cost from llm_calls where model='old-model' order by ts").fetchall()
    assert rows == [("worker", a["id"], a["chat_id"], 0.02), ("router", None, None, 0.01)]
