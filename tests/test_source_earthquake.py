"""地震提醒：距离、经纬度、按震级和范围筛、同名地点挑人口最多的、地名只查一次、震级修正后再报。
不联网：_util.TRANSPORT 换成 MockTransport，_util.blocked 换掉免得去解析域名。"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from osa import events as engine
from osa import registry
from osa.sources import _util, earthquake


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


def _quake(qid, mag, place, lat, lon, depth=10.0, kind="earthquake", minutes_ago=30, **props):
    t = int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return {"type": "Feature", "id": qid, "geometry": {"type": "Point", "coordinates": [lon, lat, depth]},
            "properties": {"mag": mag, "place": place, "time": t, "type": kind, "magType": "mww",
                           "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{qid}", "tsunami": 0, **props}}


TOKYO = {"results": [   # 同名的挑人口最多的：中文名常先查到一个村
    {"name": "东京", "latitude": 32.2, "longitude": 119.3, "country": "中国", "admin1": "江苏"},
    {"name": "東京", "latitude": 35.6895, "longitude": 139.69171, "country": "日本", "admin1": "东京都", "population": 9733276}]}


async def test_earthquake(web):
    assert 320 < earthquake.distance(35.6895, 139.6917, 38.3782, 141.937) < 360
    assert earthquake.latlon("30.66, 104.07") == (30.66, 104.07) and earthquake.latlon("成都") is None
    with pytest.raises(ValueError):
        earthquake.validate({"near": "95,10", "min_magnitude": 5, "radius_km": 100})
    assert earthquake.validate({"near": "", "min_magnitude": None, "radius_km": None}) == {"near": "", "min_magnitude": 5, "radius_km": 500}
    assert earthquake.summary({"min_magnitude": 6, "near": ""}) == "全球 6 级以上"
    assert earthquake.summary({"min_magnitude": 4, "near": "成都", "radius_km": 300}) == "成都 300 公里内、4 级以上"

    japan = _quake("us1", 5.8, "43 km E of Onagawa Chō, Japan", 38.3782, 141.937, tsunami=1, alert="yellow")
    chile = _quake("us2", 6.2, "46 km NW of Huara, Chile", -19.7, -69.9, minutes_ago=90)
    small = _quake("us3", 4.0, "near Tokyo", 35.7, 139.7)
    blast = _quake("us4", 5.1, "quarry", 35.7, 139.7, kind="quarry blast")
    feed = {"type": "FeatureCollection", "features": [japan, chile, small, blast]}
    web.routes["https://earthquake.usgs.gov/"] = feed
    web.routes[earthquake.GEOCODE] = TOKYO

    # 东京附近 500 公里、5 级以上：只有日本那次
    params = {"min_magnitude": 5, "near": "东京", "radius_km": 500}
    evs, state = await earthquake.check(params, None)
    assert [e["id"] for e in evs] == ["quake-us1"] and "4.5_day" in web.seen[-1]
    e = evs[0]
    assert e["title"].startswith("M5.8 地震：43 km E of Onagawa") and "离东京约 3" in e["title"]
    assert "深度：10 公里" in e["text"] and "海啸" in e["text"] and "黄色" in e["text"] and e["url"].endswith("/us1")
    assert "按“東京（日本 东京都）”算" in e["text"] and "count=5" in next(u for u in web.seen if u.startswith(earthquake.GEOCODE))
    assert state["where"]["name"] == "东京" and json.dumps(state)
    geocodes = sum(1 for u in web.seen if u.startswith(earthquake.GEOCODE))
    evs, state = await earthquake.check(params, state)
    assert evs == [] and sum(1 for u in web.seen if u.startswith(earthquake.GEOCODE)) == geocodes   # 报过的不再报，地名不再查

    # 震级修正到线以上的，下次还能报
    feed["features"] = [japan, chile, small | {"properties": small["properties"] | {"mag": 5.2}}, blast]
    evs, state = await earthquake.check(params, state)
    assert [e["id"] for e in evs] == ["quake-us3"]

    # 全球 6 级以上；4.5 级以下的用 2.5 级的源
    evs, _ = await earthquake.check({"min_magnitude": 6, "near": "", "radius_km": 500}, None)
    assert [e["id"] for e in evs] == ["quake-us2"] and "离你" not in evs[0]["text"]
    await earthquake.check({"min_magnitude": 3, "near": "38.4,141.9", "radius_km": 100}, None)
    assert "2.5_day" in web.seen[-1]

    web.routes[earthquake.GEOCODE] = {"results": []}
    with pytest.raises(ValueError, match="找不到"):
        await earthquake.check({"min_magnitude": 5, "near": "不存在的地方", "radius_km": 500}, None)


def test_builtin_source():
    """内置的来源（以前在插件市场里）：不用装，网页的“新建事件”里就有；常见用法的参数都过得了检查。"""
    src = registry.SOURCES["earthquake"]
    assert src is earthquake.EARTHQUAKE and src.owner == "builtin" and src.mode == registry.POLL and src.sample and src.icon
    assert src.category in engine.CATEGORIES
    got = next(s for s in engine.catalog() if s["id"] == "earthquake")
    assert got["plugin"] == "" and got["scenarios"]
    for sc in src.scenarios:
        engine.clean_params(src, sc.get("params") or {})
    t = engine.clean({"source": "earthquake", "params": {"near": "成都"}})
    assert t["params"] == {"min_magnitude": 5, "near": "成都", "radius_km": 500} and t["every"] == 10
    assert engine.describe(t) == "地震提醒：成都 500 公里内、5 级以上（每 10 分钟看一次）"
    with pytest.raises(ValueError):
        engine.clean({"source": "earthquake", "params": {"min_magnitude": 1}})
    with pytest.raises(ValueError):
        engine.clean({"source": "earthquake", "params": {"near": "100,500"}})
