"""定时任务：没有新内容时不打扰；按时间触发的带上上次的结果；Bot 在对话里能看、改、暂停、删自己的。"""
import pytest

from conftest import find, settle, until
from fakellm import Call
from osa import bots, memory, registry, routines, server


def _routine(bot: dict, kind: str = "every") -> dict:
    trig = {"kind": "every", "minutes": 30} if kind == "every" else {"kind": "webhook"}
    return routines.save(bot["id"], {"name": f"查邮件{bot['id']}", "prompt": "看看有没有新邮件", "trigger": trig})


async def _run(r: dict, source: str = "schedule", event: str = "") -> dict:
    out = await routines.fire(r, source, event)
    assert out["accepted"]
    await until(lambda: r["id"] not in routines.RUNNING, what="定时任务跑完")
    await settle()
    return routines.runs(r["id"])[0]


async def test_nothing_new_stays_quiet(llm, events, make_bot):
    a = make_bot()
    r = _routine(a)
    llm.script("worker", "[无新内容]")
    run = await _run(r)

    assert run["status"] == "quiet" and run["detail"] == "没有新内容"
    assert not find(a["chat_id"], kind="result")                      # 没有结果消息：也就不推送、不发聊天软件
    assert not [e for e in events.of("message") if (e["message"].get("meta") or {}).get("kind") == "result"]
    dispatch = find(a["chat_id"], kind="dispatch")
    assert dispatch and dispatch[0]["meta"].get("quiet")              # 网页上按这个隐藏
    assert [e["task"] for e in events.of("task")][-1]["quiet"] is True
    assert not any("[定时任务" in str(m.get("content")) for m in memory.context(a["chat_id"], me=a["id"]))


async def test_news_is_posted_and_next_run_compares(llm, events, make_bot):
    a = make_bot()
    r = _routine(a)
    llm.script("worker", "有 2 封新邮件：A、B")
    run = await _run(r)
    assert run["status"] == "done"
    assert "有 2 封新邮件：A、B" in find(a["chat_id"], kind="result")[0]["content"]

    llm.script("worker", "没有新内容。")                                # 写法稍有不同也认
    assert (await _run(r))["status"] == "quiet"
    goal = llm.bodies("worker")[-1]["messages"][1]["content"]
    assert "上次运行的结果" in goal and "有 2 封新邮件：A、B" in goal   # 和上次有内容的那次比
    assert routines.QUIET in goal
    shown = find(a["chat_id"], kind="dispatch")[-1]["content"]           # 这些只给 Worker 看，不显示、不存进对话
    assert "上次运行的结果" not in shown and routines.QUIET not in shown


async def test_webhook_runs_do_not_carry_last_result(llm, events, make_bot):
    a = make_bot()
    r = _routine(a, "webhook")
    llm.script("worker", "处理了 issue #1")
    await _run(r, "webhook", '{"issue": 1}')
    llm.script("worker", "处理了 issue #2")
    await _run(r, "webhook", '{"issue": 2}')
    goal = llm.bodies("worker")[-1]["messages"][1]["content"]
    assert "上次运行的结果" not in goal and '{"issue": 2}' in goal


async def test_manual_test_run_always_posts(llm, events, make_bot):
    a = make_bot()
    r = _routine(a)
    llm.script("worker", "第一次的结果")
    await _run(r)
    llm.script("worker", "[无新内容]")
    run = await _run(r, "test")
    assert run["status"] == "done"                                     # 你点“测试”：照常发出来
    goal = llm.bodies("worker")[-1]["messages"][1]["content"]
    assert "上次运行的结果" not in goal and routines.QUIET not in goal


@pytest.mark.parametrize("text", ["[无新内容]", "【无新内容】", "无新内容", " [nothing new] ", "Nothing new.", "没有新内容。"])
def test_quiet_markers(text):
    assert routines.is_quiet(text)


@pytest.mark.parametrize("text", ["", "有新邮件。[无新内容]", "无新内容，但是有一封退信", "nothing new here, except one thing"])
def test_not_quiet(text):
    assert not routines.is_quiet(text)


async def test_bot_tools_list_update_delete_own_only(events, make_bot):
    a, b = make_bot(), make_bot()
    r = routines.save(a["id"], {"name": "早报", "prompt": "整理今天的科技新闻，" + "细节" * 200,
                                "trigger": {"kind": "daily", "times": ["09:00"]}})
    other = _routine(b)

    brief = await routines.tool_list(a["id"], {})
    assert f"#{r['id']}「早报」每天 09:00，下次" in brief and "看全文用 list_routines 填 id" in brief
    assert "查邮件" not in brief                                          # 别的 Bot 的看不到
    full = await routines.tool_list(a["id"], {"id": r["id"]})
    assert "细节" * 200 in full and "还没跑过" in full
    assert "你没有" in await routines.tool_list(a["id"], {"id": other["id"]})

    # 只改时间：要求、名字照旧；没改的字段模型常填成 null
    out = await routines.tool_update(a["id"], {"id": r["id"], "times": ["08:30"], "days": [1, 3, 5], "prompt": None})
    got = routines.get(r["id"])
    assert out.startswith("已改好") and got["trigger"] == {"kind": "daily", "times": ["08:30"], "days": [1, 3, 5]}
    assert got["prompt"] == r["prompt"] and got["name"] == "早报"
    # 换触发方式
    await routines.tool_update(a["id"], {"id": r["id"], "kind": "every", "minutes": 120})
    assert routines.get(r["id"])["trigger"] == {"kind": "every", "minutes": 120}
    assert "间隔至少 5 分钟" in await routines.tool_update(a["id"], {"id": r["id"], "minutes": 1})
    # 暂停、恢复
    assert "已暂停" in await routines.tool_update(a["id"], {"id": r["id"], "enabled": False})
    assert not routines.get(r["id"])["enabled"]
    await routines.tool_update(a["id"], {"id": r["id"], "enabled": True})
    assert routines.get(r["id"])["enabled"]
    assert "没说要改什么" in await routines.tool_update(a["id"], {"id": r["id"]})
    assert "只能改你自己的" in await routines.tool_update(a["id"], {"id": other["id"], "enabled": False})
    assert routines.get(other["id"])["enabled"]

    assert "只能删你自己的" in await routines.tool_delete(a["id"], {"id": other["id"]})
    out = await routines.tool_delete(a["id"], {"id": r["id"]})
    assert routines.get(r["id"]) is None and routines.get(other["id"])
    assert "已删除定时任务" in out and "细节" * 200 in out             # 原设置留在对话里，反悔了能重建
    await settle()
    assert {e["bot_id"] for e in events.of("routines_changed")} >= {a["id"]}
    bots.delete(a["id"]), bots.delete(b["id"])


async def test_router_has_routine_tools_worker_does_not(llm, events, make_bot):
    a = make_bot("Timer")
    r = routines.save(a["id"], {"name": "周报", "prompt": "提醒写周报", "trigger": {"kind": "daily", "times": ["16:00"], "days": [5]}})
    llm.script("router:Timer", Call("update_routine", {"id": r["id"], "enabled": False}), "暂停了")
    await server.safe_handle(bots.chat(a["chat_id"]), "周报提醒先停一下")
    await until(lambda: find(a["chat_id"], role="assistant", text="暂停了"), what="回复")
    assert not routines.get(r["id"])["enabled"]
    body = llm.bodies("router", "Timer")[-1]
    mine = {"create_routine", "list_routines", "update_routine", "delete_routine"}
    assert mine <= {t["function"]["name"] for t in body["tools"]}
    assert f"#{r['id']} 周报" in body["messages"][0]["content"]          # 提示词里的列表带编号，能直接用
    worker = {s["function"]["name"] for s in registry.schemas(registry.WORKER, {"scope": registry.WORKER, "pages": True})}
    assert not mine & worker                                              # 定时任务跑在 Worker 里，不让它自己改
    bots.delete(a["id"])
