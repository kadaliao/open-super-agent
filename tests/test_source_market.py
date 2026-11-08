"""价格到线：代码整理、三种行情接口的解析、到线只报一次（回到线另一边才再报）、涨跌幅一天一次。不联网。"""
import json

import httpx
import pytest

from osa import events
from osa.sources import _util, market


@pytest.fixture
def net(monkeypatch):
    """假网络：routes 里 {网址片段: 响应或 (request) -> 响应}，seen 记下请求过的网址。"""
    routes, seen = {}, []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        for k, v in routes.items():
            if k in str(req.url):
                return v(req) if callable(v) else v
        return httpx.Response(404)

    async def open_(url):
        return None
    monkeypatch.setattr(_util, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(_util, "blocked", open_)
    return routes, seen


def tencent(code: str, name: str, price, prev, pct, when="20261106143200", n=88) -> httpx.Response:
    """腾讯行情的样子：v_sh600519="1~名字~代码~现价~昨收~…"，第 30 个是时间，第 32 个是涨跌幅，GBK 编码。"""
    f = [""] * n
    f[0], f[1], f[2], f[3], f[4], f[30], f[32] = "1", name, code[2:], str(price), str(prev), when, str(pct)
    return httpx.Response(200, content=f'v_{code}="{"~".join(f)}";\n'.encode("gbk"))


def P(**kw) -> dict:
    return events.clean_params(market.PRICE, kw)


@pytest.mark.parametrize("m,raw,want", [
    ("cn", "600519", "sh600519"), ("cn", "000001", "sz000001"), ("cn", "sh000001", "sh000001"), ("cn", "300750", "sz300750"),
    ("cn", "510300", "sh510300"), ("cn", "920000", "bj920000"), ("cn", "830799", "bj830799"), ("cn", "600519.SS", "sh600519"),
    ("cn", "SZ000001", "sz000001"), ("hk", "700", "00700"), ("hk", "HK00700", "00700"), ("hk", "hsi", "HSI"),
    ("us", "aapl", "AAPL"), ("us", "BRK.B", "BRK.B"), ("crypto", "btc", "BTC"), ("crypto", "eth/cny", "ETH/CNY"),
    ("crypto", "Bitcoin", "bitcoin"), ("fx", "usdcny", "USD/CNY"), ("fx", "eur/usd", "EUR/USD"),
])
def test_norm_symbol(m, raw, want):
    assert market.norm_symbol(m, raw) == want


@pytest.mark.parametrize("m,raw", [("cn", "60051"), ("cn", "abc"), ("hk", "1234567"), ("us", "苹果"), ("fx", "USDCN")])
def test_bad_symbol(m, raw):
    with pytest.raises(ValueError):
        market.norm_symbol(m, raw)


def test_validate():
    p = P(market="cn", symbol=" 600519 ", condition="above", threshold="1500")
    assert p == {"market": "cn", "symbol": "sh600519", "condition": "above", "threshold": 1500}
    assert market.summary(p) == "sh600519 ≥ 1500"
    assert market.summary(P(market="crypto", symbol="btc", condition="change", threshold=5)) == "BTC ±5%"
    with pytest.raises(ValueError, match="要填"):
        P(market="cn", symbol="600519")
    with pytest.raises(ValueError, match="大于 0"):
        P(market="cn", symbol="600519", threshold=0)
    with pytest.raises(ValueError, match="6 位"):
        P(market="cn", symbol="茅台", threshold=1)
    with pytest.raises(ValueError):
        P(market="nasdaq", symbol="AAPL", threshold=1)


def q(price, pct=1.0, day="2026-11-06"):
    return {"name": "贵州茅台", "code": "sh600519", "price": price, "prev": 1450.0, "pct": pct, "time": f"{day} 14:32",
            "day": day, "ccy": "CNY"}


def test_above_fires_once_then_rearms():
    p = P(market="cn", symbol="600519", condition="above", threshold=1500)
    evs, st = market.judge(p, q(1490), None)
    assert evs == [] and not st.get("hit")
    evs, st = market.judge(p, q(1501), st)
    assert len(evs) == 1 and "涨到 1500 以上" in evs[0]["title"] and "1,501.00" in evs[0]["title"]
    for price in (1510, 1495):        # 1495 还在余量（0.5%）里：不算回去
        evs, st = market.judge(p, q(price), st)
        assert evs == [] and st["hit"]
    evs, st = market.judge(p, q(1490), st)
    assert evs == [] and st["hit"] is False
    evs, st = market.judge(p, q(1500), st)
    assert len(evs) == 1


def test_already_past_the_line_tells_once():
    """建好时已经到线了：第一次就报（fire_first），之后不重复。"""
    assert market.PRICE.fire_first
    p = P(market="cn", symbol="600519", condition="below", threshold=1500)
    evs, st = market.judge(p, q(1453), None)
    assert len(evs) == 1 and "跌到 1500 以下" in evs[0]["title"]
    assert market.judge(p, q(1440), st)[0] == []


def test_change_once_per_day():
    p = P(market="cn", symbol="600519", condition="change", threshold=5)
    evs, st = market.judge(p, q(1530, 5.5), None)
    assert len(evs) == 1 and "+5.50%" in evs[0]["title"]
    assert market.judge(p, q(1540, 6.2), st)[0] == []
    assert market.judge(p, q(1450, 3.0, "2026-11-07"), st)[0] == []
    evs, st = market.judge(p, q(1370, -5.2, "2026-11-07"), st)
    assert len(evs) == 1 and "-5.20%" in evs[0]["title"]


def test_no_price_does_nothing():
    p = P(market="cn", symbol="600519", condition="below", threshold=1500)
    assert market.judge(p, q(0.0), None)[0] == []


async def test_a_share_quote(net):
    routes, seen = net
    routes["q=sh600519"] = tencent("sh600519", "贵州茅台", "1502.30", "1452.00", "3.46")
    evs, st = await market.check(P(market="cn", symbol="600519", condition="above", threshold=1500), None)
    assert seen == ["https://qt.gtimg.cn/q=sh600519"]
    e = evs[0]
    assert e["title"] == "贵州茅台（sh600519）涨到 1500 以上：现价 1,502.30（+3.46%）"
    assert "昨收 1,452.00" in e["text"] and "行情时间：2026-11-06 14:32" in e["text"] and "CNY" in e["text"]
    assert st["hit"] is True


async def test_hk_and_us_quote(net):
    routes, seen = net
    routes["q=hk00700"] = tencent("hk00700", "腾讯控股", "455.000", "430.400", "5.72", "2026/11/06 16:08:54", n=78)
    routes["q=usAAPL"] = tencent("usAAPL", "苹果", "342.37", "344.17", "-0.52", "2026-11-06 16:00:01", n=73)
    evs, _ = await market.check(P(market="hk", symbol="700", condition="above", threshold=450), None)
    assert "腾讯控股" in evs[0]["title"] and "HKD" in evs[0]["text"] and "2026-11-06 16:08" in evs[0]["text"]
    evs, _ = await market.check(P(market="us", symbol="aapl", condition="below", threshold=350), None)
    assert "苹果（AAPL）" in evs[0]["title"] and "-0.52%" in evs[0]["title"]
    assert seen == ["https://qt.gtimg.cn/q=hk00700", "https://qt.gtimg.cn/q=usAAPL"]


async def test_unknown_stock(net):
    routes, _ = net
    routes["qt.gtimg.cn"] = httpx.Response(200, content=b'v_pv_none_match="1";\n')
    with pytest.raises(ValueError, match="没找到"):
        await market.check(P(market="cn", symbol="600000", condition="above", threshold=1), None)


async def test_crypto(net):
    routes, seen = net
    routes["coingecko"] = httpx.Response(200, json={"bitcoin": {"cny": 540000, "cny_24h_change": -6.3, "last_updated_at": 1794171542}})
    evs, st = await market.check(P(market="crypto", symbol="btc/cny", condition="change", threshold=5), None)
    assert "ids=bitcoin" in seen[0] and "vs_currencies=cny" in seen[0]
    assert evs[0]["title"].startswith("BTC/CNY 涨跌幅超过 5%：现价 540,000.00")
    assert "540,000.00 CNY" in evs[0]["text"] and "-6.30%（24 小时）" in evs[0]["text"]


async def test_crypto_unknown(net):
    routes, _ = net
    routes["coingecko"] = httpx.Response(200, json={})
    with pytest.raises(ValueError, match="CoinGecko 上找不到"):
        await market.check(P(market="crypto", symbol="nosuchcoin", condition="above", threshold=1), None)


async def test_fx_change_compares_with_previous_update(net):
    routes, seen = net
    data = {"result": "success", "time_last_update_unix": 1000, "rates": {"USD": 1, "CNY": 7.10}}
    routes["open.er-api.com"] = lambda req: httpx.Response(200, content=json.dumps(data))
    p = P(market="fx", symbol="usd/cny", condition="change", threshold=2)
    evs, st = await market.check(p, None)
    assert evs == [] and "latest/USD" in seen[0]
    evs, st = await market.check(p, st)            # 数据还没更新：没有涨跌幅
    assert evs == []
    data.update(time_last_update_unix=2000, rates={"CNY": 7.25})
    evs, st = await market.check(p, st)
    assert len(evs) == 1 and "+2.11%" in evs[0]["title"] and "7.25 CNY" in evs[0]["text"]
    below = P(market="fx", symbol="USD/CNY", condition="below", threshold=7.3)
    evs, _ = await market.check(below, None)
    assert "USD/CNY 跌到 7.3 以下" in evs[0]["title"]


async def test_fx_unknown_currency(net):
    routes, _ = net
    routes["open.er-api.com"] = httpx.Response(200, json={"result": "success", "rates": {"CNY": 7.1}})
    with pytest.raises(ValueError, match="XYZ"):
        await market.check(P(market="fx", symbol="USD/XYZ", condition="above", threshold=1), None)


def test_scenarios_are_valid():
    for sc in market.PRICE.scenarios:
        events.clean_params(market.PRICE, sc["params"])
