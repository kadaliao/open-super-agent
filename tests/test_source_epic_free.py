"""Epic 免费游戏：接口返回的 JSON 怎么认（打折的不算、预告、合集的链接）、看过的不再报、地区参数。
不联网：_util.TRANSPORT 换成 MockTransport，_util.blocked 换掉免得去解析域名。"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from osa import events as engine
from osa import registry
from osa.sources import _util, epic_free


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
    now = datetime.now(timezone.utc)
    week = timedelta(days=7)
    flotsam = _game("g1", "浮游世界", "flotsam-266287", 0, current=_offer(now - timedelta(days=1), now + week, 0))
    sale = _game("g2", "LISA", "lisa", 6480, current=_offer(now - timedelta(days=1), now + week, 60))   # 打折不算
    soon = _game("g3", "斗阵骑士", "knightica-3be45c", 4700, upcoming=_offer(now + week, now + 2 * week, 0))
    bundle = _game("g4", "合集", "some-bundle", 0, current=_offer(now - timedelta(hours=1), now + week, 0), offer_type="BUNDLE")
    current, upcoming = epic_free.games(epic_body(flotsam, sale, soon))
    assert [g["title"] for g in current] == ["浮游世界"] and [g["title"] for g in upcoming] == ["斗阵骑士"]
    assert current[0]["url"] == "https://store.epicgames.com/zh-CN/p/flotsam-266287"
    assert epic_free.link(bundle) == "https://store.epicgames.com/zh-CN/bundles/some-bundle"
    assert epic_free.link({"productSlug": "x/home"}).endswith("/p/x") and epic_free.link({}).endswith("/free-games")
    assert epic_free.games({}) == ([], []) and epic_free.games({"data": {"Catalog": {"searchStore": None}}}) == ([], [])

    web.routes[epic_free.API] = epic_body(flotsam, sale, soon)
    evs, state = await epic_free.check({"country": "CN"}, None)
    assert len(evs) == 1 and evs[0]["title"] == "Epic 免费送：浮游世界" and evs[0]["url"].endswith("flotsam-266287")
    assert "原价：¥75.00" in evs[0]["text"] and "下一批预告：斗阵骑士" in evs[0]["text"]
    assert "country=CN" in web.seen[-1] and "locale=zh-CN" in web.seen[-1]
    evs, state = await epic_free.check({"country": "CN"}, state)
    assert evs == []                                       # 看过的不再报
    web.routes[epic_free.API] = epic_body(flotsam, bundle)
    evs, state = await epic_free.check({"country": "US"}, state)
    assert [e["title"] for e in evs] == ["Epic 免费送：合集"] and "country=US" in web.seen[-1]
    assert len(state["seen"]) == 2 and json.dumps(state)     # state 能存成 JSON


def test_builtin_source():
    """内置的来源（以前在插件市场里）：不用装，网页的“新建事件”里就有；常见用法的参数都过得了检查。"""
    src = registry.SOURCES["epic_free"]
    assert src is epic_free.EPIC_FREE and src.owner == "builtin" and src.mode == registry.POLL and src.sample and src.icon
    assert src.category in engine.CATEGORIES
    got = next(s for s in engine.catalog() if s["id"] == "epic_free")
    assert got["plugin"] == "" and got["scenarios"]
    for sc in src.scenarios:
        engine.clean_params(src, sc.get("params") or {})
