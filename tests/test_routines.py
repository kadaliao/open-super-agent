"""定时任务：没有新内容时不打扰；按时间触发的带上上次的结果。"""
import pytest

from conftest import find, settle, until
from osa import memory, routines


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
