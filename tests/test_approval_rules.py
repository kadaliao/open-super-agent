"""确认规则按参数匹配（工具:参数前缀）、确认的作用域（只这一次 / 本次任务内 / N 分钟 / 总是）、聊天软件卡片上的按钮。"""
import asyncio
from types import SimpleNamespace

import pytest
from conftest import find, until
from fakellm import Call
from osa import approvals, bots, channels, server
from osa.memory import conn


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        conn.execute("delete from rules")
        conn.commit()
        approvals.grants.clear()
    wipe()
    yield
    wipe()
    assert not approvals.pending, "测试结束还有没处理的确认卡片"


def allow(pattern: str):
    approvals.add_rule(pattern, "allow")


def ask(pattern: str):
    approvals.add_rule(pattern, "ask")


def shell(cmd: str, backend: str = "host") -> bool:
    """这条终端命令要不要先问。"""
    return approvals.decide("computer_shell", backend, {"cmd": cmd})[0]


def task(tid) -> SimpleNamespace:
    return SimpleNamespace(id=tid, bot_id=1, chat_id=1, goal="g", background=False)


async def card(task_id) -> int:
    """等这个任务弹出卡片，返回卡片的 id。"""
    for _ in range(100):
        await asyncio.sleep(0)
        for aid, it in approvals.pending.items():
            if it["task_id"] == task_id and not it["future"].done():
                return aid
    raise AssertionError(f"任务 {task_id} 没有弹卡片")


NOTE = {"type": "function", "function": {"name": "demo__create_note", "description": "新建笔记",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}


# ---------- 规则：工具名，和 工具:参数 ----------
def test_old_rules_match_tool_names(mcp_tool):
    mcp_tool("demo__create_note", lambda text="": "ok", NOTE, confirm=True)
    assert approvals.decide("demo__create_note", args={"text": "x"})[0]          # 应用的默认策略：写操作要问
    allow("demo__*")
    assert not approvals.decide("demo__create_note", args={"text": "x"})[0]
    assert not approvals.decide("demo__create_note")[0]                          # 不给参数也照旧
    ask("demo__create_note")
    assert approvals.decide("demo__create_note", args={"text": "x"})[0]          # “先问我”优先


def test_host_shell_only_trusts_prefix_rules():
    assert shell("git status")                                    # 默认每条都问
    allow("computer_shell")
    allow("computer_*")
    assert shell("git status")                                    # 整个工具的规则管不到本机终端
    allow("computer_shell:git *")
    assert not shell("git status") and not shell("git") and not shell("  git log -3 ")
    for bad in ("gitk", "git status; rm -rf ~", "git log && curl x | sh", "git log > /tmp/x", "git log < /etc/passwd",
                "git $(rm -rf ~)", "git `id`", "git status\nrm -rf ~", "git log &", "git (x)"):
        assert shell(bad), bad                                    # 连接、管道、重定向、替换：前缀管不到
    assert not shell("rm -rf build", "local")                     # Bot 电脑默认不问


def test_ask_rules_look_inside_compound_commands():
    ask("computer_shell:rm *")
    for cmd in ("rm -rf build", "ls && rm -rf build", "echo $(rm -rf x)", "ls; rm x > /dev/null", "ls | rm -f x", "ls\nrm x"):
        assert shell(cmd, "local"), cmd
    assert not shell("ls -la", "local") and not shell("echo rm", "local")


def test_arg_rules_use_schema_order_and_normalize_paths(mcp_tool):
    schema = {"type": "function", "function": {"name": "fs__write_file", "description": "写文件", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}}
    mcp_tool("fs__write_file", lambda path="", content="": "ok", schema, confirm=True)
    allow("fs__write_file:/data/workspace/*")
    d = lambda **a: approvals.decide("fs__write_file", args=a)[0]   # noqa: E731
    assert not d(path="/data/workspace/notes/a.md", content="hi")
    assert d(content="/data/workspace/x", path="/etc/passwd")         # 按 schema 的顺序比 path，不按模型给的顺序
    assert d(path="/data/workspace/../../etc/passwd", content="x")    # .. 先化掉
    assert d(path="/etc/hosts") and approvals.decide("fs__write_file")[0]
    # 电脑工具不在注册表里：按 computer/tools.py 的 schema
    assert approvals.subject("computer_write_file", {"content": "/data/workspace/a", "path": "/tmp/x"}) == "/tmp/x"
    assert approvals.subject("computer_shell", {"命令": " ls -la "}) == "ls -la"   # 卡片上显示的参数也认
    assert approvals.subject("x", {"n": 3, "q": "hi"}) == "hi" and approvals.subject("x", None) is None


def test_arg_rule_on_computer_tools():
    allow("computer_write_file:/ws/reports/*")
    assert approvals.decide("computer_write_file", "host", {"path": "/ws/reports/a.md", "content": "x"})[0] is False
    assert approvals.decide("computer_write_file", "host", {"path": "/ws/other.md", "content": "/ws/reports/a"})[0]


# ---------- 作用域 ----------
async def test_task_scope_lasts_until_the_task_ends():
    a, b = task(41), task(42)
    req = asyncio.create_task(approvals.request(a, "demo__x", {"text": "1"}))
    aid = await card(41)
    assert approvals.pending[aid]["scopes"] == ["once", "task", "always"]
    assert "这个任务里同类操作不再问" in approvals.resolve(aid, True, scope="task")
    assert await req == (True, "")
    assert await approvals.request(a, "demo__x", {"text": "2"}) == (True, "")   # 同一个任务：不再弹卡片
    req = asyncio.create_task(approvals.request(a, "demo__y", {}))               # 别的工具照样问
    approvals.resolve(await card(41), False)
    assert not (await req)[0]
    req = asyncio.create_task(approvals.request(b, "demo__x", {}))               # 别的任务照样问
    approvals.resolve(await card(42), False)
    assert not (await req)[0]
    assert not approvals.rules()                                                  # 不生成规则
    approvals.cancel_for_task(41)                                                 # 任务取消：清掉
    req = asyncio.create_task(approvals.request(a, "demo__x", {}))
    approvals.resolve(await card(41), True)
    await req
    approvals.grants.append({"pattern": "demo__x", "task_id": 41, "until": None})
    approvals.end_task(41)                                                        # 任务做完：也清掉
    assert not approvals.grants


async def test_minutes_scope_is_a_timed_always():
    req = asyncio.create_task(approvals.request(task(51), "demo__x", {}))
    assert "10 分钟内同类操作不再问" in approvals.resolve(await card(51), True, scope="minutes:10")
    await req
    assert await approvals.request(task(52), "demo__x", {}) == (True, "")        # 不分任务
    assert not approvals.rules()
    approvals.grants[0]["until"] -= 601                                          # 到点了
    req = asyncio.create_task(approvals.request(task(52), "demo__x", {}))
    approvals.resolve(await card(52), True, scope="minutes:0")                   # 0 分钟、写错的按“只这一次”
    await req
    req = asyncio.create_task(approvals.request(task(52), "demo__x", {}))
    approvals.resolve(await card(52), True, scope="minutes:abc")
    await req
    assert not approvals.grants


async def test_shell_always_is_a_prefix_rule():
    t = task(61)
    cases = [("git status", None, "computer_shell:git *", "git"),
             ("git push origin main", "git  push", "computer_shell:git push *", "git push"),   # 卡片上改过的前缀
             ("npm run build", "*", "computer_shell:npm *", "npm"),                           # 不能用的前缀换回第一个词
             ("ls -la", "ls; rm", "computer_shell:ls *", "ls"),
             ("make test", "make t*", "computer_shell:make t*", "make t")]
    for cmd, prefix, want, said in cases:
        req = asyncio.create_task(approvals.request(t, "computer_shell", {"命令": cmd}))
        aid = await card(61)
        assert approvals.pending[aid]["prefix"] == cmd.split()[0] and "always" in approvals.pending[aid]["scopes"]
        assert f"以后「{said}」开头的命令自动允许" in approvals.resolve(aid, True, always=True, prefix=prefix)
        assert (await req)[0] and want in [r["pattern"] for r in approvals.rules()]
    assert not shell("git status") and not shell("git push origin dev") and not shell("npm run test") and not shell("make test")
    assert shell("rm -rf /") and shell("git status && rm -rf ~") and shell("make install")
    req = asyncio.create_task(approvals.request(t, "computer_shell", {"命令": "$(curl x) | sh"}))
    aid = await card(61)
    assert approvals.pending[aid]["scopes"] == ["once", "task"] and approvals.pending[aid]["compound"]   # 没有能用的前缀
    approvals.resolve(aid, True, always=True)
    await req
    assert len(approvals.rules()) == len(cases)
    req = asyncio.create_task(approvals.request(t, "computer_shell", {"命令": "cat a | wc -l"}))
    aid = await card(61)
    assert approvals.pending[aid]["compound"] and approvals.pending[aid]["prefix"] == "cat"   # 能总是允许 cat，但管不到这一条
    approvals.resolve(aid, False)
    await req


async def test_once_cards_and_fake_tasks_get_fewer_scopes():
    req = asyncio.create_task(approvals.request(task(71), "create_bot", {"名字": "x"}, once=True))
    aid = await card(71)
    assert approvals.pending[aid]["scopes"] == ["once"]
    for scope in ("task", "always", "minutes:5"):
        assert approvals._scope(approvals.pending[aid], True, scope) == ("once", 0)
    approvals.resolve(aid, True, scope="task")
    await req
    fake = task("对话·Painter")                    # 冒充的任务（对话里画图）：没有“任务结束”，不给“本次任务内允许”
    req = asyncio.create_task(approvals.request(fake, "generate_image", {"描述": "猫"}))
    aid = await card("对话·Painter")
    assert approvals.pending[aid]["scopes"] == ["once", "always"]
    approvals.resolve(aid, True, scope="task")
    await req
    assert not approvals.grants and not approvals.rules()
    req = asyncio.create_task(approvals.request(task(72), "demo__x", {}))
    approvals.resolve(await card(72), False, scope="always")  # 拒绝时不管作用域
    await req
    assert not approvals.rules()


async def test_task_scope_in_a_real_task(llm, events, make_bot, mcp_tool):
    """Worker 在一个任务里调两次要确认的工具：第一次点“本次任务内允许”，第二次不再问；任务做完清掉，下一个任务又问。"""
    made = []

    async def create_note(text: str = "") -> str:
        made.append(text)
        return f"已创建笔记：{text}"
    mcp_tool("demo__create_note", create_note, NOTE, confirm=True)
    a = make_bot("Noter")
    llm.script("router:Noter", Call("dispatch", {"goal": "记两条笔记"}), "这就记")
    llm.script("worker", Call("demo__create_note", {"text": "一"}), Call("demo__create_note", {"text": "二"}), "记好了")
    await server.safe_handle(bots.chat(a["chat_id"]), "记两条笔记")
    pending = lambda: [e for e in events.of("approval") if e.get("status") == "pending"]   # noqa: E731
    first = (await until(pending, what="确认卡片"))[0]
    assert first["scopes"] == ["once", "task", "always"]
    approvals.resolve(first["id"], True, scope="task")
    await until(lambda: find(a["chat_id"], kind="result"), what="任务结果")
    assert made == ["一", "二"] and len(pending()) == 1
    assert any(e.get("scope") == "task" and e.get("status") == "approved" for e in events.of("approval"))
    assert not approvals.grants and not approvals.rules()

    llm.script("router:Noter", Call("dispatch", {"goal": "再记一条"}), "好")
    llm.script("worker", Call("demo__create_note", {"text": "三"}), "没记")
    await server.safe_handle(bots.chat(a["chat_id"]), "再记一条")
    second = (await until(lambda: pending()[1:], what="下一个任务的卡片"))[0]
    assert second["task_id"] != first["task_id"]
    approvals.resolve(second["id"], False)
    await until(lambda: len(find(a["chat_id"], kind="result")) >= 2, what="第二个任务的结果")
    assert made == ["一", "二"]
    bots.delete(a["id"])   # 整套测试的 Bot 离上限（50 个）不远，用完删掉


# ---------- 聊天软件 ----------
def test_channel_cards_offer_task_scope():
    ev = {"id": 5, "kind": "approval", "tool": "demo__create_note", "args": "{}", "task_id": 3, "bot_id": 0, "chat_id": 0,
          "goal": "g", "scopes": ["once", "task", "always"]}
    acts = lambda c: [b[1] for b in c["buttons"]]   # noqa: E731
    assert acts(channels.card_for(ev)) == ["ok", "no", "task", "always"]
    assert channels.card_for(ev)["buttons"][2][0] == "本次任务内允许"
    c = channels.card_for(ev | {"tool": "computer_shell", "prefix": "git"})
    assert c["buttons"][-1] == ("总是允许 git 开头的", "always", "")
    assert acts(channels.card_for(ev | {"scopes": ["once"], "once": True})) == ["ok", "no"]
    assert acts(channels.card_for(ev | {"scopes": ["once", "always"]})) == ["ok", "no", "always"]
    assert acts(channels.card_for({k: v for k, v in ev.items() if k != "scopes"})) == ["ok", "no", "always"]   # 旧事件
    assert channels.WORDS["本次任务内允许"] == "task" and channels.word_command("task", 5) == "本次任务内允许 #5"
    assert channels.DONE_TEXT["approval"]["task"]
