"""插件市场里的几个事件来源和运维插件：Epic 免费游戏、地震提醒、公网 IP 变了、工具调用记录、花费提醒、关键词提醒。

联网的都换成假的（_util.TRANSPORT 换成 MockTransport，_util.blocked 换掉免得去解析域名），不调真模型。
插件目录换成临时目录，碰不到 data/plugins。"""
import asyncio
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import httpx
import pytest
from conftest import find, until
from osa import bots, memory, plugins, registry
from osa import events as engine
from osa.config import CFG
from osa.sources import _util


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


@pytest.fixture
def web(monkeypatch):
    """假的网络：routes[网址前缀] = 响应（dict 发 JSON、str 发文字、int 是状态码、函数按请求算）。记下每次请求的网址。"""
    routes, seen = {}, []

    def handle(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        seen.append(url)
        for prefix, out in sorted(routes.items(), key=lambda kv: -len(kv[0])):
            if url.startswith(prefix):
                out = out(req) if callable(out) else out
                if isinstance(out, int):
                    return httpx.Response(out)
                if isinstance(out, (dict, list)):
                    return httpx.Response(200, json=out)
                return httpx.Response(200, text=out)
        return httpx.Response(404)

    async def fake_blocked(url):
        return None
    monkeypatch.setattr(_util, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(_util, "blocked", fake_blocked)
    return SimpleNamespace(routes=routes, seen=seen)


# ---------- Epic 免费游戏 ----------
def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _offer(a, b, pct):
    return [{"promotionalOffers": [{"startDate": _iso(a), "endDate": _iso(b),
                                    "discountSetting": {"discountType": "PERCENTAGE", "discountPercentage": pct}}]}]


def _game(gid, title, slug, discount_price, current=None, upcoming=None, offer_type="BASE_GAME", original="¥75.00"):
    return {"title": title, "id": gid, "namespace": "ns", "offerType": offer_type, "productSlug": None,
            "description": f"{title} 的简介", "catalogNs": {"mappings": [{"pageSlug": slug, "pageType": "productHome"}]},
            "price": {"totalPrice": {"discountPrice": discount_price, "originalPrice": 7500,
                                     "fmtPrice": {"originalPrice": original}}},
            "promotions": {"promotionalOffers": current or [], "upcomingPromotionalOffers": upcoming or []}}


def epic_body(*els):
    return {"data": {"Catalog": {"searchStore": {"elements": list(els)}}}}


async def test_epic_free(web):
    ep = _catalog_module("epic_free")
    now = datetime.now(timezone.utc)
    week = timedelta(days=7)
    flotsam = _game("g1", "浮游世界", "flotsam-266287", 0, current=_offer(now - timedelta(days=1), now + week, 0))
    sale = _game("g2", "LISA", "lisa", 6480, current=_offer(now - timedelta(days=1), now + week, 60))   # 打折不算
    soon = _game("g3", "斗阵骑士", "knightica-3be45c", 4700, upcoming=_offer(now + week, now + 2 * week, 0))
    bundle = _game("g4", "合集", "some-bundle", 0, current=_offer(now - timedelta(hours=1), now + week, 0), offer_type="BUNDLE")
    current, upcoming = ep.games(epic_body(flotsam, sale, soon))
    assert [g["title"] for g in current] == ["浮游世界"] and [g["title"] for g in upcoming] == ["斗阵骑士"]
    assert current[0]["url"] == "https://store.epicgames.com/zh-CN/p/flotsam-266287"
    assert ep.link(bundle) == "https://store.epicgames.com/zh-CN/bundles/some-bundle"
    assert ep.link({"productSlug": "x/home"}).endswith("/p/x") and ep.link({}).endswith("/free-games")
    assert ep.games({}) == ([], []) and ep.games({"data": {"Catalog": {"searchStore": None}}}) == ([], [])

    web.routes[ep.API] = epic_body(flotsam, sale, soon)
    evs, state = await ep.check({"country": "CN"}, None)
    assert len(evs) == 1 and evs[0]["title"] == "Epic 免费送：浮游世界" and evs[0]["url"].endswith("flotsam-266287")
    assert "原价：¥75.00" in evs[0]["text"] and "下一批预告：斗阵骑士" in evs[0]["text"]
    assert "country=CN" in web.seen[-1] and "locale=zh-CN" in web.seen[-1]
    evs, state = await ep.check({"country": "CN"}, state)
    assert evs == []                                       # 看过的不再报
    web.routes[ep.API] = epic_body(flotsam, bundle)
    evs, state = await ep.check({"country": "US"}, state)
    assert [e["title"] for e in evs] == ["Epic 免费送：合集"] and "country=US" in web.seen[-1]
    assert len(state["seen"]) == 2 and json.dumps(state)     # state 能存成 JSON


# ---------- 地震提醒 ----------
def _quake(qid, mag, place, lat, lon, depth=10.0, kind="earthquake", minutes_ago=30, **props):
    t = int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return {"type": "Feature", "id": qid, "geometry": {"type": "Point", "coordinates": [lon, lat, depth]},
            "properties": {"mag": mag, "place": place, "time": t, "type": kind, "magType": "mww",
                           "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{qid}", "tsunami": 0, **props}}


TOKYO = {"results": [   # 同名的挑人口最多的：中文名常先查到一个村
    {"name": "东京", "latitude": 32.2, "longitude": 119.3, "country": "中国", "admin1": "江苏"},
    {"name": "東京", "latitude": 35.6895, "longitude": 139.69171, "country": "日本", "admin1": "东京都", "population": 9733276}]}


async def test_earthquake(web):
    eq = _catalog_module("earthquake")
    assert 320 < eq.distance(35.6895, 139.6917, 38.3782, 141.937) < 360
    assert eq.latlon("30.66, 104.07") == (30.66, 104.07) and eq.latlon("成都") is None
    with pytest.raises(ValueError):
        eq.validate({"near": "95,10", "min_magnitude": 5, "radius_km": 100})
    assert eq.validate({"near": "", "min_magnitude": None, "radius_km": None}) == {"near": "", "min_magnitude": 5, "radius_km": 500}
    assert eq.summary({"min_magnitude": 6, "near": ""}) == "全球 6 级以上"
    assert eq.summary({"min_magnitude": 4, "near": "成都", "radius_km": 300}) == "成都 300 公里内、4 级以上"

    japan = _quake("us1", 5.8, "43 km E of Onagawa Chō, Japan", 38.3782, 141.937, tsunami=1, alert="yellow")
    chile = _quake("us2", 6.2, "46 km NW of Huara, Chile", -19.7, -69.9, minutes_ago=90)
    small = _quake("us3", 4.0, "near Tokyo", 35.7, 139.7)
    blast = _quake("us4", 5.1, "quarry", 35.7, 139.7, kind="quarry blast")
    feed = {"type": "FeatureCollection", "features": [japan, chile, small, blast]}
    web.routes["https://earthquake.usgs.gov/"] = feed
    web.routes[eq.GEOCODE] = TOKYO

    # 东京附近 500 公里、5 级以上：只有日本那次
    params = {"min_magnitude": 5, "near": "东京", "radius_km": 500}
    evs, state = await eq.check(params, None)
    assert [e["id"] for e in evs] == ["quake-us1"] and "4.5_day" in web.seen[-1]
    e = evs[0]
    assert e["title"].startswith("M5.8 地震：43 km E of Onagawa") and "离东京约 3" in e["title"]
    assert "深度：10 公里" in e["text"] and "海啸" in e["text"] and "黄色" in e["text"] and e["url"].endswith("/us1")
    assert "按“東京（日本 东京都）”算" in e["text"] and "count=5" in next(u for u in web.seen if u.startswith(eq.GEOCODE))
    assert state["where"]["name"] == "东京" and json.dumps(state)
    geocodes = sum(1 for u in web.seen if u.startswith(eq.GEOCODE))
    evs, state = await eq.check(params, state)
    assert evs == [] and sum(1 for u in web.seen if u.startswith(eq.GEOCODE)) == geocodes   # 报过的不再报，地名不再查

    # 震级修正到线以上的，下次还能报
    feed["features"] = [japan, chile, small | {"properties": small["properties"] | {"mag": 5.2}}, blast]
    evs, state = await eq.check(params, state)
    assert [e["id"] for e in evs] == ["quake-us3"]

    # 全球 6 级以上；4.5 级以下的用 2.5 级的源
    evs, _ = await eq.check({"min_magnitude": 6, "near": "", "radius_km": 500}, None)
    assert [e["id"] for e in evs] == ["quake-us2"] and "离你" not in evs[0]["text"]
    await eq.check({"min_magnitude": 3, "near": "38.4,141.9", "radius_km": 100}, None)
    assert "2.5_day" in web.seen[-1]

    web.routes[eq.GEOCODE] = {"results": []}
    with pytest.raises(ValueError, match="找不到"):
        await eq.check({"min_magnitude": 5, "near": "不存在的地方", "radius_km": 500}, None)


# ---------- 公网 IP 变了 ----------
async def test_ip_change(web):
    ipc = _catalog_module("ip_change")
    assert ipc.parse("1.2.3.4\n", 4) == "1.2.3.4" and ipc.parse("<html>", 4) is None
    assert ipc.parse("192.168.1.2", 4) is None and ipc.parse("2001:4860:4860::8888", 4) is None
    assert ipc.same("2001:4860:4860::8888", "2001:4860:4860::1", 6) and not ipc.same("2001:4860:4860::1", "2001:4860:4861::1", 6)

    ip = {"ipify": "1.2.3.4", "icanhazip": "1.2.3.4"}
    web.routes["https://api.ipify.org"] = lambda req: ip["ipify"]
    web.routes["https://ipv4.icanhazip.com"] = lambda req: ip["icanhazip"]
    web.routes["https://ifconfig.me/ip"] = 500
    evs, state = await ipc.check({"ipv6": False}, None)
    assert evs == [] and state == {"v4": "1.2.3.4"}        # 第一次只记下

    ip.update(ipify="5.6.7.8", icanhazip="5.6.7.8\n")
    evs, state = await ipc.check({"ipv6": False}, state)
    assert [e["title"] for e in evs] == ["公网 IPv4 变了：1.2.3.4 → 5.6.7.8"] and state["v4"] == "5.6.7.8"
    assert "换了一家确认过" in evs[0]["text"]

    ip.update(ipify="9.9.9.9")                             # 一家说变了，另一家说没变：不报，下次再看
    evs, state = await ipc.check({"ipv6": False}, state)
    assert evs == [] and state["v4"] == "5.6.7.8"

    ip.update(ipify="<html>oops</html>", icanhazip="5.6.7.8")   # 第一家答得不对就问下一家
    evs, state = await ipc.check({"ipv6": False}, state)
    assert evs == [] and state["v4"] == "5.6.7.8"

    web.routes["https://api.ipify.org"] = 503
    web.routes["https://ipv4.icanhazip.com"] = 503
    with pytest.raises(RuntimeError, match="都没答上"):
        await ipc.check({"ipv6": False}, state)

    # IPv6：前缀变了才报；没有 IPv6 不算错
    web.routes["https://api.ipify.org"] = "5.6.7.8"
    v6 = {"ip": "2001:4860:4860::8888"}
    web.routes["https://api6.ipify.org"] = lambda req: v6["ip"]
    web.routes["https://ipv6.icanhazip.com"] = lambda req: v6["ip"]
    evs, state = await ipc.check({"ipv6": True}, {"v4": "5.6.7.8"})
    assert evs == [] and state["v6"] == "2001:4860:4860::8888"
    v6["ip"] = "2001:4860:4860::abcd"                      # 临时地址换了，前缀没变
    evs, state = await ipc.check({"ipv6": True}, state)
    assert evs == []
    v6["ip"] = "2001:4860:4861::1"
    evs, state = await ipc.check({"ipv6": True}, state)
    assert [e["title"] for e in evs] == ["公网 IPv6 前缀 变了：2001:4860:4860::abcd → 2001:4860:4861::1"]
    web.routes["https://api6.ipify.org"] = 500
    web.routes["https://ipv6.icanhazip.com"] = 500
    evs, state2 = await ipc.check({"ipv6": True}, state)
    assert evs == [] and state2["v6"] == state["v6"]


# ---------- 事件来源插件：装上、启用、停用 ----------
async def test_source_plugins_register_and_unregister(pdir):
    for name, sid in (("epic_free", "epic_free"), ("earthquake", "earthquake"), ("ip_change", "ip_change")):
        await plugins.install(name)
        assert sid not in registry.SOURCES                 # 装了还没启用
        await plugins.enable(name)
        src = registry.SOURCES[sid]
        assert src.owner == f"plugin:{name}" and src.mode == registry.POLL and src.scenarios and src.sample
        assert src.category in ("开发", "资讯", "生活", "金融", "系统", "通用") and src.icon
        got = next(s for s in engine.catalog() if s["id"] == sid)
        assert got["plugin"] == name and got["scenarios"]
        await plugins.disable(name)
        assert sid not in registry.SOURCES and all(s["id"] != sid for s in engine.catalog())

    await plugins.install("earthquake")
    await plugins.enable("earthquake")
    t = engine.clean({"source": "earthquake", "params": {"near": "成都"}})
    assert t["params"] == {"min_magnitude": 5, "near": "成都", "radius_km": 500} and t["every"] == 10
    assert engine.describe(t) == "地震提醒：成都 500 公里内、5 级以上（每 10 分钟看一次）"
    with pytest.raises(ValueError):
        engine.clean({"source": "earthquake", "params": {"min_magnitude": 1}})
    with pytest.raises(ValueError):
        engine.clean({"source": "earthquake", "params": {"near": "100,500"}})
    await plugins.remove("earthquake")
    assert "earthquake" not in registry.SOURCES


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
