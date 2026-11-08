"""天气提醒：查城市（记在 state 里不重复查）、各种情况、同一天只报一次、夜里不报、周五报周末。不联网，时间固定。"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from osa import events
from osa.sources import _util, weather

CST = timezone(timedelta(hours=8))


def at(*a) -> float:
    """北京时间 → 时间戳。"""
    return datetime(*a, tzinfo=CST).timestamp()


def forecast(start="2026-11-09", **cols) -> dict:
    """四天的预报：默认都是晴、温和、没雨没风，cols 里给的列覆盖默认。"""
    d0 = datetime.fromisoformat(start)
    daily = {"time": [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)],
             "weather_code": [0, 0, 0, 0], "temperature_2m_max": [20, 20, 20, 20], "temperature_2m_min": [12, 12, 12, 12],
             "precipitation_probability_max": [0, 0, 0, 0], "precipitation_sum": [0, 0, 0, 0],
             "wind_speed_10m_max": [10, 10, 10, 10], "snowfall_sum": [0, 0, 0, 0]}
    daily.update(cols)
    return {"utc_offset_seconds": 28800, "timezone": "Asia/Shanghai", "daily": daily}


GEO = {"results": [{"name": "杭州", "latitude": 30.29, "longitude": 120.16, "country": "中国", "admin1": "浙江"}]}


@pytest.fixture
def net(monkeypatch):
    box = {"geo": GEO, "fc": forecast(), "seen": []}

    def handler(req: httpx.Request) -> httpx.Response:
        box["seen"].append(req.url)
        if req.url.host == "geocoding-api.open-meteo.com":
            g = box["geo"]
            return httpx.Response(200, json=g(req) if callable(g) else g)
        if req.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=box["fc"])
        return httpx.Response(404)

    async def open_(url):
        return None
    monkeypatch.setattr(_util, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(_util, "blocked", open_)
    return box


def P(**kw) -> dict:
    return events.clean_params(weather.WEATHER, {"city": "杭州"} | kw)


def clock(monkeypatch, ts: float):
    monkeypatch.setattr(weather, "_now", lambda: ts)


def test_params():
    p = P()
    assert p["conditions"] == ["rain_tomorrow"] and p["rain_prob"] == 50 and p["hot"] == 35 and p["cold"] == 0
    with pytest.raises(ValueError, match="至少选一种"):
        P(conditions=[])
    with pytest.raises(ValueError, match="要填城市"):
        events.clean_params(weather.WEATHER, {})
    assert weather.WEATHER.summary(p) == "杭州"
    for sc in weather.WEATHER.scenarios:
        events.clean_params(weather.WEATHER, sc["params"])


async def test_rain_tomorrow_once_per_day(net, monkeypatch):
    net["fc"] = forecast(weather_code=[3, 63, 2, 0], precipitation_probability_max=[10, 85, 30, 0],
                         precipitation_sum=[0, 12.4, 0, 0])
    clock(monkeypatch, at(2026, 11, 9, 10, 0))
    evs, st = await weather.check(P(), None)
    assert len(evs) == 1
    e = evs[0]
    assert e["title"] == "杭州（浙江，中国）：明天中雨（降水概率 85%）"
    assert "今天 11月9日 周一：阴，12~20°C" in e["text"] and "明天 11月10日 周二：中雨" in e["text"] and "降水 12.4 mm" in e["text"]
    assert st["geo"]["lat"] == 30.29 and st["fired"] == ["rain_tomorrow:2026-11-10"]
    geo_calls = sum(1 for u in net["seen"] if u.host == "geocoding-api.open-meteo.com")
    # 同一天再看：不重复报，也不再查城市
    clock(monkeypatch, at(2026, 11, 9, 13, 0))
    evs, st = await weather.check(P(), st)
    assert evs == []
    assert sum(1 for u in net["seen"] if u.host == "geocoding-api.open-meteo.com") == geo_calls == 1
    # 第二天：明天（11 日）没雨，不报
    net["fc"] = forecast("2026-11-10", precipitation_probability_max=[85, 30, 0, 0])
    clock(monkeypatch, at(2026, 11, 10, 10, 0))
    evs, st = await weather.check(P(), st)
    assert evs == [] and st["fired"] == ["rain_tomorrow:2026-11-10"]
    # 过去的日子从 state 里清掉
    net["fc"] = forecast("2026-11-11")
    clock(monkeypatch, at(2026, 11, 11, 10, 0))
    evs, st = await weather.check(P(), st)
    assert evs == [] and st["fired"] == []


async def test_rain_threshold(net, monkeypatch):
    net["fc"] = forecast(precipitation_probability_max=[0, 40, 0, 0])
    clock(monkeypatch, at(2026, 11, 9, 10, 0))
    assert (await weather.check(P(), None))[0] == []
    assert len((await weather.check(P(rain_prob=40), None))[0]) == 1


async def test_quiet_hours(net, monkeypatch):
    net["fc"] = forecast(precipitation_probability_max=[0, 90, 0, 0])
    clock(monkeypatch, at(2026, 11, 9, 23, 0))
    evs, st = await weather.check(P(), None)
    assert evs == [] and st["fired"] == []
    clock(monkeypatch, at(2026, 11, 9, 6, 30))
    assert (await weather.check(P(), st))[0] == []
    clock(monkeypatch, at(2026, 11, 9, 7, 0))
    assert len((await weather.check(P(), st))[0]) == 1


async def test_several_conditions_in_one_event(net, monkeypatch):
    net["fc"] = forecast(temperature_2m_max=[22, 13, 12, 12], temperature_2m_min=[14, -1, 0, 0],
                         wind_speed_10m_max=[10, 55, 10, 10], weather_code=[1, 73, 3, 3], snowfall_sum=[0, 2.5, 0, 0])
    clock(monkeypatch, at(2026, 11, 9, 10, 0))
    p = P(conditions=["temp_drop", "cold", "wind", "snow", "hot"])
    evs, st = await weather.check(p, None)
    assert len(evs) == 1
    title = evs[0]["title"]
    for part in ("明天降温 15°C", "明天最低 -1°C", "明天大风，最大风速 55 km/h", "明天中雪（2.5 cm）"):
        assert part in title
    assert "高温" not in title
    assert sorted(st["fired"]) == sorted(f"{c}:2026-11-10" for c in ("temp_drop", "cold", "wind", "snow"))


async def test_hot(net, monkeypatch):
    net["fc"] = forecast(temperature_2m_max=[33, 37, 30, 30])
    clock(monkeypatch, at(2026, 11, 9, 10, 0))
    evs, _ = await weather.check(P(conditions=["hot"]), None)
    assert evs[0]["title"].endswith("明天高温 37°C")
    assert (await weather.check(P(conditions=["hot"], hot=38), None))[0] == []


async def test_weekend_on_friday(net, monkeypatch):
    net["fc"] = forecast("2026-11-12")
    clock(monkeypatch, at(2026, 11, 12, 10, 0))      # 周四不报
    assert (await weather.check(P(conditions=["weekend"]), None))[0] == []
    net["fc"] = forecast("2026-11-13", weather_code=[0, 61, 2, 0])
    clock(monkeypatch, at(2026, 11, 13, 10, 0))      # 周五
    evs, st = await weather.check(P(conditions=["weekend"]), None)
    assert evs[0]["title"].endswith("周末天气")
    assert "明天 11月14日 周六：小雨" in evs[0]["text"] and "后天 11月15日 周日" in evs[0]["text"]
    clock(monkeypatch, at(2026, 11, 13, 16, 0))
    assert (await weather.check(P(conditions=["weekend"]), st))[0] == []


async def test_city_fallback_and_errors(net, monkeypatch):
    asked = []

    def geo(req):
        asked.append(req.url.params["name"])
        return GEO if req.url.params["name"] == "杭州" else {}
    net["geo"] = geo
    clock(monkeypatch, at(2026, 11, 9, 10, 0))
    _, st = await weather.check(P(city="杭州市"), None)
    assert asked == ["杭州市", "杭州"] and st["geo"]["q"] == "杭州市"
    # 改了城市：重新查
    with pytest.raises(ValueError, match="找不到城市：火星"):
        await weather.check(P(city="火星"), st)


async def test_coordinates_skip_geocoding(net, monkeypatch):
    clock(monkeypatch, at(2026, 11, 9, 10, 0))
    _, st = await weather.check(P(city="30.27, 120.15"), None)
    assert st["geo"]["lat"] == 30.27 and not [u for u in net["seen"] if u.host == "geocoding-api.open-meteo.com"]
    fc = [u for u in net["seen"] if u.host == "api.open-meteo.com"][0]
    assert fc.params["timezone"] == "auto" and "precipitation_probability_max" in fc.params["daily"]


def test_missing_probability_uses_rain_amount():
    days = weather.days_of(forecast(precipitation_probability_max=[None] * 4, precipitation_sum=[0, 3.2, 0, 0], weather_code=[0, 61, 0, 0]))
    got = weather.hits({"conditions": ["rain_tomorrow"], "rain_prob": 50}, days, "2026-11-09", 0)
    assert got == [("rain_tomorrow:2026-11-10", "明天小雨")]
