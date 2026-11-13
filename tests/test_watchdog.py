"""Watchdog：多久没有进展算卡住。自带时限、跑的时候不出声的工具调用（终端命令、run_code）在跑时，按它自己的时限算，
不然一条允许跑十分钟的命令到三分钟就被停掉（2026-11-13 #465：在 Mac 上跑 claude -p 被当成卡住）。"""
import pytest
from conftest import find, until
from fakellm import Call

from osa import approvals, bots, i18n, registry, router, sandbox, server, watchdog
from osa.computer import link
from osa.computer import tools as ctools
from osa.computer.base import ExecResult
from osa.config import CFG
from osa.worker import Task
from test_computer_mac import mac  # noqa: F401  fixture
from test_host_desktop import Recorder, _noop
from test_sandbox import box  # noqa: F401  fixture

STALL = i18n.t("3 分钟没有进展")


@pytest.fixture
def task(monkeypatch):
    monkeypatch.setitem(CFG["limits"], "task_timeout_s", 7200)   # 只看“没有进展”这一条
    t = Task(id=465, goal="剪视频")
    tok = watchdog.TASK.set(t)
    yield t
    watchdog.TASK.reset(tok)


def test_quiet_three_minutes_is_stalled(task):
    assert watchdog.check(task, task.last_progress + 170) is None
    assert watchdog.check(task, task.last_progress + 190) == STALL


def test_long_call_gets_its_own_limit(task):
    with watchdog.long_call(600):
        start = task.last_progress
        assert watchdog.check(task, start + 400) is None                      # 允许跑十分钟的命令，三分钟不算卡住
        limit = 600 + watchdog.SLACK_S
        assert watchdog.check(task, start + limit + 1) == i18n.t("工具调用超过 {n} 秒还没结束", n=limit)
    assert task.long_calls == {}                                              # 跑完了恢复三分钟
    assert watchdog.check(task, task.last_progress + 190) == STALL


def test_parallel_long_calls(task):
    """Heavy 模式几个 Worker 共用一个任务：短的先跑完，长的那个的时限还在。"""
    with watchdog.long_call(600):
        with watchdog.long_call(30):
            pass
        assert watchdog.check(task, task.last_progress + 400) is None


def test_outside_a_task_does_nothing():
    tok = watchdog.TASK.set(None)
    try:
        with watchdog.long_call(600):
            pass
    finally:
        watchdog.TASK.reset(tok)


def test_total_time_limit_still_applies(task, monkeypatch):
    monkeypatch.setitem(CFG["limits"], "task_timeout_s", 600)
    with watchdog.long_call(600):
        assert watchdog.check(task, task.started + 601) == i18n.t("运行超过 {n} 秒", n=600)


async def test_terminal_command_on_mac(task, mac, monkeypatch):  # noqa: F811
    """派到 Mac 上的终端命令：跑的时候按命令的时限（最多 600 秒）算。"""
    mac.lk.can_shell = True
    link.set_shell(mac.lid, True)
    monkeypatch.setattr(approvals, "request", Recorder(True))
    seen = []

    async def shell(cmd, timeout=120):
        seen.append((timeout, sorted(task.long_calls.values())))
        return ExecResult(0, "ok", "")
    monkeypatch.setattr(mac, "shell", shell)
    await ctools.call(task, "computer_shell", {"cmd": "claude -p ...", "timeout": 3600}, _noop, key="465:")
    await ctools.call(task, "computer_shell", {"cmd": "ls"}, _noop, key="465:")
    assert seen == [(600, [600 + watchdog.SLACK_S]), (120, [120 + watchdog.SLACK_S])]
    assert task.long_calls == {}


async def test_run_code(task, box, monkeypatch):  # noqa: F811
    seen = []

    async def exec_(cmd, timeout=120, stdin=None):
        seen.append(sorted(task.long_calls.values()))
        return ExecResult(0, "", "")
    monkeypatch.setattr(box, "exec", exec_)
    await sandbox.execute("python", "print(1)", timeout=500)
    assert seen and seen[-1] == [500 + watchdog.SLACK_S] and task.long_calls == {}


async def test_worker_marks_the_task(llm, make_bot, monkeypatch):
    """真的任务里：工具调用期间 watchdog.TASK 就是这个任务，long_call 记到它上面。"""
    monkeypatch.setattr(bots, "MAX_BOTS", 10 ** 6)   # 整套测试前面建的 Bot 已经快到上限了
    ctx = registry.Context("mcp:demo")
    seen = []

    async def run(call: registry.Call):
        with watchdog.long_call(300):
            t = watchdog.TASK.get()
            seen.append((t.id, sorted(t.long_calls.values())))
        return "好了"
    ctx.tool(registry.Tool("demo__render", {"type": "function", "function": {
        "name": "demo__render", "description": "渲染", "parameters": {"type": "object", "properties": {}}}}, run,
        where=frozenset({registry.WORKER})))
    try:
        b = make_bot()
        llm.script(f"router:{b['name']}", Call("dispatch", {"goal": "渲染"}), "好")
        llm.script("worker", Call("demo__render"), "渲染完了")
        await server.safe_handle(bots.chat(b["chat_id"]), "渲染一下")
        await until(lambda: find(b["chat_id"], kind="result"), what="任务结果")
        assert len(seen) == 1 and seen[0][0] in router.TASKS and seen[0][1] == [300 + watchdog.SLACK_S]
        assert router.TASKS[seen[0][0]].long_calls == {}
        assert watchdog.TASK.get() is None                                    # 任务外没有
        bots.delete(b["id"])
    finally:
        ctx.dispose()
