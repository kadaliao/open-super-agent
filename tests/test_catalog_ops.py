"""插件市场里的几个运维插件：工具调用记录、花费提醒、关键词提醒。

不调真模型。插件目录换成临时目录，碰不到 data/plugins。
Epic 免费游戏、地震提醒、公网 IP 变了 已经是内置的事件来源，测试在 test_source_*.py。"""
import asyncio
import importlib.util
import json
from datetime import date
from types import SimpleNamespace
import pytest
from conftest import find, until
from osa import bots, memory, plugins, registry
from osa.config import CFG


def _catalog_module(name):
    spec = importlib.util.spec_from_file_location(f"cat_{name}", plugins.CATALOG / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "DIR", tmp_path / "plugins")
    plugins.DIR.mkdir()
    yield plugins.DIR
    for n in list(plugins._loaded):
        plugins.unload(n)
    plugins._errors.clear()
    plugins._waiting.clear()
    memory.conn.execute("delete from plugins")
    memory.conn.commit()


# ---------- 工具调用记录 ----------
def test_audit_redaction():
    al = _catalog_module("audit_log")
    for k in ("password", "api_key", "apiKey", "Authorization", "key", "access-token", "client_secret", "pwd"):
        assert al.secret_key(k), k
    for k in ("keyword", "monkey", "to", "subject", "path", "query"):
        assert not al.secret_key(k), k
    token = "Ab3dE5gH7jK9mN1pQ3sT5vX7zB9cD1fG"
    args = {"to": "a@b.com", "password": "hunter2", "headers": {"Authorization": "Bearer abc.def.ghi"},
            "note": "我的 key 是 sk-abcdefghijklmnopqrstuv，别外传", "url": "https://user:pw123@host.example/x?token=abc123&q=1",
            "keyword": "urgent", "blob": f"id {token} end", "sha": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "path": "/Users/me/Documents/Projects2024/SomeVeryLongFolderNameHere/report.md", "empty_token": "",
            "list": [{"secret": "x"}, "ghp_abcdefghijklmnopqrstuvwxyz0123"]}
    out = al.redact(args)
    s = json.dumps(out, ensure_ascii=False)
    for leak in ("hunter2", "abc.def.ghi", "sk-abcdefghijklmnopqrstuv", "pw123", "abc123", token, "ghp_abc"):
        assert leak not in s, leak
    assert out["to"] == "a@b.com" and out["keyword"] == "urgent" and out["sha"] == args["sha"] and out["path"] == args["path"]
    assert out["url"] == "https://user:***@host.example/x?token=***&q=1" and out["list"][0] == {"secret": "***"}
    assert out["empty_token"] == ""                        # 空的不用抹
    text = al.redact_text('登录成功，返回 {"access_token": "eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSM", "expires": 3600}')
    assert "eyJ" not in text and '"expires": 3600' in text
    assert al.redact_text("Basic information about tokens") == "Basic information about tokens"
    e = al.entry(0, "worker", {"id": 3, "name": "Nova"}, 7, {"id": 12}, "send_email",
                 {"api_key": "k", "to": "x@y.z", "body": "很长" * 400}, "已发送，token=s3cr3t" + "。" * 1000, "done")
    assert e["bot"] == "Nova" and e["task_id"] == 12 and e["tool"] == "send_email" and e["status"] == "done"
    assert len(e["args"]) <= al.MAX_ARGS + 1 and len(e["result"]) <= al.MAX_RESULT + 1
    assert "s3cr3t" not in e["result"] and '"api_key": "***"' in e["args"]


def test_audit_files_prune_and_search(tmp_path):
    al = _catalog_module("audit_log")
    rows = [
        {"ts": "2026-11-08 09:00:00", "bot": "Nova", "scope": "worker", "task_id": 5, "tool": "send_email", "status": "done",
         "args": '{"to": "boss@x.com"}', "result": "已发送"},
        {"ts": "2026-11-08 10:00:00", "bot": "Echo", "scope": "router", "task_id": None, "tool": "fetch_url", "status": "error",
         "args": '{"url": "https://a.example"}', "result": "超时"},
        {"ts": "2026-11-09 08:00:00", "bot": "Nova", "scope": "worker", "task_id": 6, "tool": "send_email", "status": "rejected",
         "args": '{"to": "all@x.com"}', "result": "用户拒绝了"},
    ]
    al.write(tmp_path, rows)
    assert sorted(p.name for p in tmp_path.glob("*.jsonl")) == ["2026-11-08.jsonl", "2026-11-09.jsonl"]
    today = date(2026, 11, 9)
    got = al.search(tmp_path, tool="send_email", today=today)
    assert [e["ts"] for e in got] == ["2026-11-09 08:00:00", "2026-11-08 09:00:00"]   # 新的在前
    assert [e["tool"] for e in al.search(tmp_path, day="2026-11-08", today=today)] == ["fetch_url", "send_email"]
    assert [e["bot"] for e in al.search(tmp_path, bot="@echo", today=today)] == ["Echo"]
    assert [e["status"] for e in al.search(tmp_path, keyword="BOSS", today=today)] == ["done"]
    assert al.search(tmp_path, status="rejected", today=today)[0]["task_id"] == 6
    assert len(al.search(tmp_path, today=today, limit=2)) == 2
    assert al.search(tmp_path, days=1, tool="fetch", today=today) == []
    assert "Nova（后台任务 #6）send_email 被拒绝" in al.line(got[0])
    with pytest.raises(ValueError):
        al.search(tmp_path, day="昨天", today=today)

    for d in ("2026-10-01", "2026-10-10", "2026-10-11"):
        (tmp_path / f"{d}.jsonl").write_text("{}\n")
    (tmp_path / "notes.jsonl").write_text("别删\n")
    assert al.prune(tmp_path, 30, today) == ["2026-10-01.jsonl", "2026-10-10.jsonl"]   # 留 30 天：10-11 到 11-09
    assert (tmp_path / "2026-10-11.jsonl").exists() and (tmp_path / "notes.jsonl").exists()


async def test_audit_log_plugin(pdir):
    await plugins.install("audit_log")
    await plugins.enable("audit_log")
    assert registry.available("audit_search", registry.ROUTER) and registry.available("audit_search", registry.WORKER)
    bot = {"id": 1, "name": "Nova"}
    await registry.emit("tool_done", scope="worker", bot=bot, chat_id=2, task={"id": 41, "goal": "发邮件"},
                        name="send_email", args={"to": "boss@x.com", "password": "hunter2"}, result="已发送", status="done")
    await registry.emit("tool_done", scope="router", bot=bot, chat_id=2, task=None, name="date_calc",
                        args={"op": "diff"}, result="相差 3 天", status="done")
    folder = pdir / ".audit_log.data"
    today = folder / f"{date.today().isoformat()}.jsonl"
    await until(lambda: today.exists() and len(today.read_text().splitlines()) == 2, what="写进记录")
    assert "hunter2" not in today.read_text() and "send_email" in today.read_text()
    tool = registry.get("audit_search")
    out = await tool.run(registry.Call(registry.ROUTER, bot, 2, 2, {"tool": "send_email"}))
    assert out.startswith("找到 1 条") and "Nova（后台任务 #41）send_email 成功" in out and "boss@x.com" in out
    assert "没有找到" in await tool.run(registry.Call(registry.ROUTER, bot, 2, 2, {"bot": "没有这个"}))
    assert "YYYY-MM-DD" in await tool.run(registry.Call(registry.ROUTER, bot, 2, 2, {"date": "昨天"}))
    await registry.emit("tool_done", scope="router", bot=bot, chat_id=2, task=None, name="audit_search",
                        args={}, result="找到…", status="done")         # 自己的查询不记
    await registry.emit("tool_done", scope="router", bot=bot, chat_id=2, task=None, name="fetch_url",
                        args={"url": "https://x"}, result="…", status="error")
    await plugins.disable("audit_log")                    # 停用时还没写的也写掉
    lines = [json.loads(x) for x in today.read_text().splitlines()]
    assert [x["tool"] for x in lines] == ["send_email", "date_calc", "fetch_url"]
    assert registry.get("audit_search") is None and not registry.listening("tool_done")


# ---------- 花费提醒 ----------
class FakeMessenger:
    def __init__(self):
        self.sent, self.fail = [], False

    async def post(self, text, bot=None, chat_id=None):
        if self.fail:
            raise LookupError("没有 Bot")
        self.sent.append((text, bot))
        return 1


def test_budget_crossed():
    ba = _catalog_module("budget_alert")
    assert ba.crossed(850, 1000, [80, 100], set(), "tokens") == [80]
    assert ba.crossed(1200, 1000, [100, 80], set(), "tokens") == [80, 100]
    assert ba.crossed(1200, 1000, [80, 100], {"tokens:80"}, "tokens") == [100]
    assert ba.crossed(500, 1000, [80, 100], set(), "tokens") == []
    assert ba.crossed(99, 0, [80], set(), "cost") == []                # 没定数就不看
    with pytest.raises(ValueError):
        ba._percents([])
    with pytest.raises(ValueError):
        ba._percents("80")


async def test_budget_alert_thresholds(tmp_path, monkeypatch):
    ba = _catalog_module("budget_alert")
    monkeypatch.setitem(CFG["limits"], "daily_token_budget", 1_000_000)
    monkeypatch.setitem(CFG["pricing"], "currency", "¥")
    m = FakeMessenger()
    ctx = SimpleNamespace(config={"bot": "Nova", "warn_at_percent": [80, 100], "daily_cost": 5}, data_dir=tmp_path / "d",
                          services={"messenger": m}, log=None)
    assert await ba.check(ctx, tokens=500_000, cost=1.0, today="2026-11-09") == []
    [msg] = await ba.check(ctx, tokens=850_000, cost=1.2, today="2026-11-09")
    assert "85%" in msg and "85.0 万 / 100.0 万" in msg and "¥1.20" in msg and m.sent[-1][1] == "Nova"
    assert await ba.check(ctx, tokens=900_000, cost=1.3, today="2026-11-09") == []       # 一天只提醒一次
    [msg] = await ba.check(ctx, tokens=1_200_000, cost=1.5, today="2026-11-09")
    assert msg.startswith("⛔") and "用完了" in msg
    [msg] = await ba.check(ctx, tokens=1_200_000, cost=4.2, today="2026-11-09")
    assert "¥4.20" in msg and "84%" in msg and "每天 ¥5" in msg
    [msg] = await ba.check(ctx, tokens=1_200_000, cost=6.0, today="2026-11-09")
    assert "超过了你定的每天 ¥5" in msg
    # 第二天重新算；一下子跨过两条线只发一条
    out = await ba.check(ctx, tokens=1_100_000, cost=0.1, today="2026-11-10")
    assert len(out) == 1 and out[0].startswith("⛔")
    assert await ba.check(ctx, tokens=1_100_000, cost=0.1, today="2026-11-10") == []
    # 发不出去的不记成提醒过，下次再试
    m.fail = True
    with pytest.raises(LookupError):
        await ba.check(ctx, tokens=1_100_000, cost=5.5, today="2026-11-10")
    m.fail = False
    assert len(await ba.check(ctx, tokens=1_100_000, cost=5.5, today="2026-11-10")) == 1
    # 只看 token、不看钱
    ctx.config["daily_cost"] = 0
    assert await ba.check(ctx, tokens=100, cost=999, today="2026-11-11") == []
    tokens, cost = ba.usage()
    assert tokens == memory.tokens_today() and cost >= 0     # 和每日预算是同一个数


async def test_budget_alert_plugin_config(pdir):
    await plugins.install("budget_alert")
    assert plugins.info("budget_alert")["inject"] == ["messenger"]
    await plugins.set_config("budget_alert", {"warn_at_percent": "80", "daily_cost": 0})
    with pytest.raises(RuntimeError, match="warn_at_percent"):
        await plugins.enable("budget_alert")
    await plugins.set_config("budget_alert", {"bot": "", "warn_at_percent": [50, 90], "daily_cost": 3})
    assert plugins.info("budget_alert")["loaded"] and not plugins.info("budget_alert")["error"]


# ---------- 关键词提醒 ----------
def test_keyword_logic():
    ka = _catalog_module("keyword_alert")
    assert ka.hits("线上 URGENT 了", ["urgent", "紧急"]) == ["urgent"] and ka.hits("", ["x"]) == []
    s = ka.snippet("前面" * 50 + "紧急：数据库挂了" + "后面" * 50, "紧急")
    assert "紧急：数据库挂了" in s and s.startswith("…") and s.endswith("…")
    assert ka.snippet("短消息 urgent", "urgent") == "短消息 urgent"
    cd = ka.Cooldown(60)
    assert cd.allow(1, "Urgent", now=1000) and not cd.allow(1, "urgent", now=1030)
    assert cd.allow(2, "urgent", now=1030) and cd.allow(1, "urgent", now=1061)
    group = {"id": 9, "kind": "group", "title": "运维群"}
    assert ka.where(group, "dingtalk") == "群「运维群」（钉钉）"
    assert ka.where({"id": 3, "kind": "direct", "title": "Nova", "parent": 2}, "") == "和「Nova」的私聊的讨论串"
    assert ka.notice(group, "", "服务器 urgent", ["urgent"]) == "🔔 群「运维群」里有人提到了「urgent」：服务器 urgent"


async def test_keyword_alert_plugin(pdir, llm, events, make_bot):
    alerts, other = make_bot("KwAlerts"), make_bot("KwOther")
    try:
        await plugins.install("keyword_alert")
        with pytest.raises(RuntimeError, match="keywords"):
            await plugins.enable("keyword_alert")
        await plugins.set_config("keyword_alert", {"keywords": ["urgent", "紧急"], "bot": "KwAlerts", "cooldown_minutes": 30})
        assert plugins.info("keyword_alert")["loaded"]
        group = {"id": 987654, "kind": "group", "title": "运维群", "bot_ids": [other["id"]]}
        await registry.emit("user_message", chat=group, text="大家好", via="")
        await registry.emit("user_message", chat=group, text="线上紧急！数据库连不上了", via="dingtalk")
        [m] = await until(lambda: find(alerts["chat_id"], kind="plugin"), what="提醒")
        assert m["content"].startswith("🔔 群「运维群」（钉钉）里有人提到了「紧急」：线上紧急") and m["meta"]["plugin"] == "keyword_alert"
        await registry.emit("user_message", chat=group, text="还是很紧急", via="dingtalk")          # 冷却中
        await registry.emit("user_message", chat=bots.chat(alerts["chat_id"]), text="urgent 测试", via="")   # 就在提醒的私聊里
        await registry.emit("user_message", chat=bots.chat(other["chat_id"]), text="this is URGENT", via="telegram")
        await until(lambda: len(find(alerts["chat_id"], kind="plugin")) == 2, what="第二条提醒")
        await asyncio.sleep(0.05)
        msgs = find(alerts["chat_id"], kind="plugin")
        assert len(msgs) == 2 and msgs[1]["content"].startswith("🔔 和「KwOther」的私聊（Telegram）里有人提到了「urgent」")
        await plugins.disable("keyword_alert")
        assert not any(getattr(f, "__qualname__", "").startswith("apply") for f in registry._LISTENERS.get("user_message", []))
    finally:
        for b in (alerts, other):
            bots.delete(b["id"])
