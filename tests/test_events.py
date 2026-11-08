"""事件：旧 webhook 迁过来、地址就是密钥、GitHub 的 ping 和签名、逐条过滤、跑着时排队、轮询先记现状、
内部事件不触发自己、secret 进保险库；还有新加的两种定时方式（某个时间点一次、每月几号）和 Bot 在对话里建事件。"""
import hashlib
import hmac
import json
import time

import httpx
import pytest

from conftest import settle, until
from osa import events as evs, registry, routines, server, templates, vault
from osa.registry import POLL, Source


@pytest.fixture(scope="module")
def one_bot():
    """这个文件的测试共用一个 Bot，测完删掉：测试库里最多 50 个 Bot，别的文件也要建。"""
    from osa import bots
    b = bots.create("EventBot", None, "")
    yield b
    routines.delete_for_bot(b["id"])
    bots.delete(b["id"])


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


def _goal(llm) -> str:
    return llm.bodies("worker")[-1]["messages"][1]["content"]


async def _idle(r: dict):
    await until(lambda: r["id"] not in routines.RUNNING, what="事件跑完")
    await settle()


def _event(bot, source="webhook", name="ev", **trig) -> dict:
    return routines.save(bot["id"], {"name": name, "prompt": "处理一下", "trigger": {"kind": "event", "source": source, **trig}})


# ---------- 迁移和地址 ----------
async def test_legacy_webhook_becomes_event_and_old_url_still_works(llm, events, one_bot):
    a = one_bot
    rid = routines.conn.execute(
        "insert into routines(bot_id, name, prompt, skill, trigger, enabled, next_run, created, updated) values(?,?,?,?,?,?,?,?,?)",
        (a["id"], "legacy-gh", "看看", "", json.dumps({"kind": "webhook", "include": ["bug"], "exclude": [], "llm_filter": ""}),
         1, None, time.time(), time.time())).lastrowid
    routines.conn.commit()
    routines._migrate()
    r = routines.get(rid)
    t = r["trigger"]
    assert r["category"] == "event" and t["source"] == "webhook" and t["path"] == "legacy-gh" and len(t["key"]) >= 20
    assert t["include"] == ["bug"]

    llm.script("worker", "看过了")
    async with client() as c:
        res = await c.post("/hooks/legacy-gh", json={"title": "a bug"})   # 旧地址：本机、没设 token 时照旧能用
        assert res.status_code == 200 and res.json()["accepted"]
        await _idle(r)
        res = await c.post(f"/hooks/{t['key']}", json={"title": "no match"})   # 新地址：被关键词筛掉
        assert res.json()["stage"] == "rule"
    assert routines.runs(rid)[0]["status"] == "filtered"


async def test_key_is_the_credential(llm, events, one_bot):
    a = one_bot
    r = _event(a)
    key = r["trigger"]["key"]
    assert r["event"]["hook"] == f"/hooks/{key}"
    async with client() as c:
        assert (await c.post("/hooks/" + "x" * len(key), json={})).status_code == 404
        assert (await c.get(f"/hooks/{key}")).status_code == 200
        # 飞书、Slack 第一次配地址时的 challenge 原样回去
        assert (await c.post(f"/hooks/{key}", json={"type": "url_verification", "challenge": "abc"})).json() == {"challenge": "abc"}
        llm.script("worker", "收到")
        res = await c.post(f"/hooks/{key}", json={"title": "部署完成", "env": "prod"}, headers={"X-Forwarded-For": "1.2.3.4"})
    assert res.json()["accepted"]   # 从外面来的也行：地址本身就是凭据，不用 OSA_WEBHOOK_TOKEN
    await _idle(r)
    goal = _goal(llm)
    assert "这是事件「ev」" in goal and "部署完成" in goal and events_marker(goal)

    new = routines.rekey(r["id"])
    assert new["trigger"]["key"] != key
    async with client() as c:
        assert (await c.post(f"/hooks/{key}", json={})).status_code == 404


def events_marker(goal: str) -> bool:
    return routines.EVENT_START in goal and routines.EVENT_END in goal


async def test_github_ping_signature_and_summary(llm, events, one_bot):
    a = one_bot
    r = _event(a, "github", params={"events": ["issues"], "secret": "s3cret"})
    assert r["trigger"]["params"]["secret"].startswith("vault:")       # secret 存进保险库，参数里只有引用
    key = r["trigger"]["key"]
    body = json.dumps({"zen": "hi", "hook_id": 1, "repository": {"full_name": "o/r"}}).encode()

    def sig(b: bytes) -> str:
        return "sha256=" + hmac.new(b"s3cret", b, hashlib.sha256).hexdigest()

    async with client() as c:
        res = await c.post(f"/hooks/{key}", content=body, headers={"X-GitHub-Event": "ping", "Content-Type": "application/json"})
        assert res.status_code == 401                                       # 填了 secret 就要签名
        res = await c.post(f"/hooks/{key}", content=body, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sig(body)})
        assert "地址配好了" in res.json()["reason"]
        assert routines.runs(r["id"])[0]["status"] == "ignored"
        star = json.dumps({"action": "created", "sender": {"login": "x"}}).encode()
        res = await c.post(f"/hooks/{key}", content=star, headers={"X-GitHub-Event": "star", "X-Hub-Signature-256": sig(star)})
        assert "没选这类事件" in res.json()["reason"]
        issue = json.dumps({"action": "opened", "repository": {"full_name": "o/r"}, "sender": {"login": "alice", "type": "User"},
                            "issue": {"number": 7, "title": "点保存没反应", "body": "复现步骤……", "html_url": "https://github.com/o/r/issues/7"}}).encode()
        llm.script("worker", "是个 bug")
        res = await c.post(f"/hooks/{key}", content=issue, headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig(issue)})
        assert res.json()["accepted"]
    await _idle(r)
    goal = _goal(llm)
    assert "[o/r] 新 issue #7：点保存没反应（alice）" in goal and "https://github.com/o/r/issues/7" in goal
    assert "{" not in goal.split(routines.EVENT_START)[1]                   # 给 Bot 的是整理过的，不是一大坨 JSON


# ---------- 过滤和排队 ----------
async def test_filter_each_event_and_queue_while_running(llm, events, one_bot):
    a = one_bot
    r = _event(a, include=["bug"])
    llm.script("worker", "第一批")
    out = await evs.deliver(r, [{"title": "a bug here", "text": ""}, {"title": "just chat", "text": ""}])
    assert out["accepted"]
    out = await evs.deliver(r, [{"title": "another bug", "text": ""}])      # 还在跑：排队，不丢
    assert out.get("queued") == 1 and routines.get(r["id"])["event"]["pending"] == 1

    llm.script("worker", "第二批")
    await until(lambda: len(llm.bodies("worker")) >= 2, what="排队的事件接着处理")
    await _idle(r)
    first, second = (b["messages"][1]["content"] for b in llm.bodies("worker")[-2:])
    assert "a bug here" in first and "just chat" not in first                   # 关键词逐条筛
    assert "another bug" in second
    assert routines.get(r["id"])["event"]["pending"] == 0


# ---------- 轮询 ----------
@pytest.fixture
def fake_poll():
    items: list[dict] = []
    ctx = registry.Context("test")

    async def check(params, state):
        seen = set(state or [])
        return [i for i in items if i["id"] not in seen], [i["id"] for i in items]

    ctx.source(Source(id="fake_poll", title="测试轮询", description="测试", mode=POLL, check=check, every=10, min_every=1,
                      fields=[{"key": "token", "label": "令牌", "type": "secret"}, {"key": "n", "label": "数量", "type": "number", "min": 1}]))
    yield items
    ctx.dispose()


async def test_poll_baselines_then_reports_new(llm, events, one_bot, fake_poll):
    a = one_bot
    fake_poll.append({"id": "1", "title": "旧的一条", "text": "以前就有"})
    r = _event(a, "fake_poll", params={"token": "abc", "n": 3})
    assert r["next_run"] and r["next_run"] <= time.time() + 1               # 刚建好马上看一次
    out = await evs.poll(r)
    assert not out["accepted"] and "记下现状" in out["reason"]
    assert routines.runs(r["id"]) == []

    out = await evs.poll(r)
    assert out["reason"] == "没有新的"
    fake_poll.append({"id": "2", "title": "新的一条", "text": "刚出来"})
    llm.script("worker", "看了新的")
    assert (await evs.poll(r))["accepted"]
    await _idle(r)
    goal = _goal(llm)
    assert "新的一条" in goal and "旧的一条" not in goal
    assert routines.get(r["id"])["event"]["checked"]

    # 点“测试”：没有新的就拿最近看到的那条跑一遍，结果照常发
    llm.script("worker", "[无新内容]")
    out = await evs.test(r)
    assert out["accepted"]
    await _idle(r)
    assert routines.runs(r["id"])[0]["source"] == "test" and routines.runs(r["id"])[0]["status"] == "done"

    # 改了参数：重新记现状，不把旧的当新的
    r2 = routines.save(a["id"], {"trigger": {"kind": "event", "source": "fake_poll", "params": {"n": 5}}}, r["id"])
    assert r2["trigger"]["params"]["token"] == r["trigger"]["params"]["token"]   # secret 留空沿用
    assert routines.get(r["id"])["event"]["checked"] is None


async def test_poll_errors_show_up(one_bot):
    a = one_bot
    ctx = registry.Context("test-err")

    async def check(params, state):
        raise ValueError("订阅源打不开：404")
    ctx.source(Source(id="bad_poll", title="坏的", description="x", mode=POLL, check=check, min_every=1))
    try:
        r = _event(a, "bad_poll")
        out = await evs.poll(r)
        assert "404" in out["reason"]
        st = routines.get(r["id"])["event"]
        assert st["error"] == "订阅源打不开：404" and st["fails"] == 1
    finally:
        ctx.dispose()
    assert routines.get(r["id"])["event"]["missing"]                          # 提供来源的停了：事件还在，标出来


async def test_scheduler_polls_due_events(one_bot, fake_poll):
    a = one_bot
    r = _event(a, "fake_poll", every=3)
    await routines.tick(time.time() + 1)
    await settle()
    r = routines.get(r["id"])
    assert r["event"]["checked"] and r["next_run"] > time.time() + 100       # 下次在 3 分钟后


def test_params_are_checked(one_bot, fake_poll):
    a = one_bot
    with pytest.raises(ValueError, match="至少是 1"):
        _event(a, "fake_poll", params={"n": 0})
    with pytest.raises(ValueError, match="最快 1 分钟"):
        _event(a, "fake_poll", every=0.5)
    with pytest.raises(ValueError, match="没有这种事件来源"):
        _event(a, "nope")


async def test_secret_lives_in_vault_and_goes_with_the_event(one_bot, fake_poll):
    a = one_bot
    r = _event(a, "fake_poll", params={"token": "p@ss"})
    ref = r["trigger"]["params"]["token"]
    assert ref.startswith("vault:event_") and vault.get(ref[6:]) == "p@ss"
    src = evs.get_source("fake_poll")
    assert evs.resolve(src, r["trigger"]["params"])["token"] == "p@ss"
    t = templates.from_bot(a["id"])
    assert "token" not in t["routines"][0]["trigger"]["params"] and "p@ss" not in json.dumps(t)
    routines.delete(r["id"])
    assert vault.get(ref[6:]) is None


# ---------- 内部事件 ----------
async def test_task_failed_event_does_not_trigger_itself(llm, events, one_bot):
    a = one_bot
    r = _event(a, "task_failed", params={"scope": "all"})
    llm.script("worker", "原因是应用没连接")
    registry.fire("task_done", task={"id": 999001, "status": "failed", "goal": "整理邮件", "bot_id": a["id"]},
                  bot=a, result="任务失败：应用 gmail 没连接")
    await until(lambda: routines.runs(r["id"]), what="事件触发")
    await _idle(r)
    assert "整理邮件" in _goal(llm) and "gmail 没连接" in _goal(llm)
    tid = routines.runs(r["id"])[0]["task_id"]
    n = len(llm.bodies("worker"))
    registry.fire("task_done", task={"id": tid, "status": "failed", "goal": "x", "bot_id": a["id"]}, bot=a, result="又失败")
    registry.fire("task_done", task={"id": 999002, "status": "done", "goal": "x", "bot_id": a["id"]}, bot=a, result="好了")
    await settle()
    assert len(llm.bodies("worker")) == n and len(routines.runs(r["id"])) == 1


# ---------- 新的定时方式 ----------
def test_once_and_monthly(one_bot):
    a = one_bot
    soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
    r = routines.save(a["id"], {"name": "提醒", "prompt": "开会", "trigger": {"kind": "once", "at": soon.replace(" ", "T")}})
    assert r["trigger"] == {"kind": "once", "at": soon} and r["next_run"] == routines.parse_at(soon)
    assert routines.next_after(r["trigger"], r["next_run"] + 1) is None      # 跑过就没有下次
    with pytest.raises(ValueError, match="已经过了"):
        routines.save(a["id"], {"name": "x", "prompt": "x", "trigger": {"kind": "once", "at": "2020-01-01 09:00"}})

    feb = time.mktime((2027, 2, 1, 10, 0, 0, 0, 0, -1))
    m = routines._clean_trigger({"kind": "monthly", "month_days": [31], "times": ["9:30"]})
    assert m == {"kind": "monthly", "month_days": [31], "times": ["09:30"]}
    nxt = time.localtime(routines.next_after(m, feb))
    assert (nxt.tm_mon, nxt.tm_mday, nxt.tm_hour, nxt.tm_min) == (2, 28, 9, 30)   # 2 月没有 31 号：月底跑
    assert routines.describe(m, "zh") == "每月 31 号 09:30"


# ---------- Bot 在对话里 ----------
async def test_bot_creates_and_manages_events(events, one_bot):
    a = one_bot
    out = await routines.tool_create_event(a, {"name": "x"})
    assert "要填 source" in out and "- webhook：" in out and "- github：" in out   # 不知道有哪些来源：给它清单
    out = await routines.tool_create_event(a, {"name": "表单", "source": "webhook", "prompt": "整理", "include": ["报名"]})
    assert "已建事件" in out and "/hooks/" in out
    r = next(x for x in routines.list_for(a["id"]) if x["name"] == "表单")
    assert r["trigger"]["include"] == ["报名"]
    assert "地址本身就是密钥" in out

    out = await routines.tool_create(a, {"name": "x", "kind": "event"})
    assert "create_event" in out
    sch = routines.save(a["id"], {"name": "日报", "prompt": "写", "trigger": {"kind": "daily", "times": ["09:00"]}})
    listing = await routines.tool_list(a, {})
    assert listing.index("定时任务：") < listing.index("#%d「日报」" % sch["id"]) < listing.index("事件：") < listing.index("「表单」")
    assert r["trigger"]["key"] not in listing                                # 列表里不放地址，填 id 看详情才有
    assert r["trigger"]["key"] in await routines.tool_list(a, {"id": r["id"]})

    out = await routines.tool_update(a, {"id": r["id"], "kind": "daily", "times": ["10:00"]})
    assert "不能改成按时间跑" in out
    out = await routines.tool_update(a, {"id": r["id"], "exclude": ["测试"], "llm_filter": "只要真人报名"})
    assert "已改好" in out and routines.get(r["id"])["trigger"]["exclude"] == ["测试"]
    assert routines.get(r["id"])["trigger"]["key"] == r["trigger"]["key"]   # 改别的不换地址
    out = await routines.tool_delete(a, {"id": r["id"]})
    assert "create_event" in out and r["trigger"]["key"] not in out


async def test_event_sources_api(one_bot):
    async with client() as c:
        res = await c.get("/api/event-sources")
    ids = [s["id"] for s in res.json()]
    assert {"webhook", "github", "task_failed"} <= set(ids)
    gh = next(s for s in res.json() if s["id"] == "github")
    assert gh["mode"] == "push" and gh["scenarios"] and gh["fields"][0]["options"]
