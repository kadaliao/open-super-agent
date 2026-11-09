"""公网 IP 变了：认 IP、第一次只记下、换一家确认、一家抽风不报、都答不上报错、IPv6 只看前缀。
不联网：_util.TRANSPORT 换成 MockTransport，_util.blocked 换掉免得去解析域名。"""
from types import SimpleNamespace

import httpx
import pytest

from osa import events as engine
from osa import registry
from osa.sources import _util, ip_change


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


async def test_ip_change(web):
    assert ip_change.parse("1.2.3.4\n", 4) == "1.2.3.4" and ip_change.parse("<html>", 4) is None
    assert ip_change.parse("192.168.1.2", 4) is None and ip_change.parse("2001:4860:4860::8888", 4) is None
    assert ip_change.same("2001:4860:4860::8888", "2001:4860:4860::1", 6) and not ip_change.same("2001:4860:4860::1", "2001:4860:4861::1", 6)

    ip = {"ipify": "1.2.3.4", "icanhazip": "1.2.3.4"}
    web.routes["https://api.ipify.org"] = lambda req: ip["ipify"]
    web.routes["https://ipv4.icanhazip.com"] = lambda req: ip["icanhazip"]
    web.routes["https://ifconfig.me/ip"] = 500
    evs, state = await ip_change.check({"ipv6": False}, None)
    assert evs == [] and state == {"v4": "1.2.3.4"}        # 第一次只记下

    ip.update(ipify="5.6.7.8", icanhazip="5.6.7.8\n")
    evs, state = await ip_change.check({"ipv6": False}, state)
    assert [e["title"] for e in evs] == ["公网 IPv4 变了：1.2.3.4 → 5.6.7.8"] and state["v4"] == "5.6.7.8"
    assert "换了一家确认过" in evs[0]["text"]

    ip.update(ipify="9.9.9.9")                             # 一家说变了，另一家说没变：不报，下次再看
    evs, state = await ip_change.check({"ipv6": False}, state)
    assert evs == [] and state["v4"] == "5.6.7.8"

    ip.update(ipify="<html>oops</html>", icanhazip="5.6.7.8")   # 第一家答得不对就问下一家
    evs, state = await ip_change.check({"ipv6": False}, state)
    assert evs == [] and state["v4"] == "5.6.7.8"

    web.routes["https://api.ipify.org"] = 503
    web.routes["https://ipv4.icanhazip.com"] = 503
    with pytest.raises(RuntimeError, match="都没答上"):
        await ip_change.check({"ipv6": False}, state)

    # IPv6：前缀变了才报；没有 IPv6 不算错
    web.routes["https://api.ipify.org"] = "5.6.7.8"
    v6 = {"ip": "2001:4860:4860::8888"}
    web.routes["https://api6.ipify.org"] = lambda req: v6["ip"]
    web.routes["https://ipv6.icanhazip.com"] = lambda req: v6["ip"]
    evs, state = await ip_change.check({"ipv6": True}, {"v4": "5.6.7.8"})
    assert evs == [] and state["v6"] == "2001:4860:4860::8888"
    v6["ip"] = "2001:4860:4860::abcd"                      # 临时地址换了，前缀没变
    evs, state = await ip_change.check({"ipv6": True}, state)
    assert evs == []
    v6["ip"] = "2001:4860:4861::1"
    evs, state = await ip_change.check({"ipv6": True}, state)
    assert [e["title"] for e in evs] == ["公网 IPv6 前缀 变了：2001:4860:4860::abcd → 2001:4860:4861::1"]
    web.routes["https://api6.ipify.org"] = 500
    web.routes["https://ipv6.icanhazip.com"] = 500
    evs, state2 = await ip_change.check({"ipv6": True}, state)
    assert evs == [] and state2["v6"] == state["v6"]


def test_builtin_source():
    """内置的来源（以前在插件市场里）：不用装，网页的“新建事件”里就有；常见用法的参数都过得了检查。"""
    src = registry.SOURCES["ip_change"]
    assert src is ip_change.IP_CHANGE and src.owner == "builtin" and src.mode == registry.POLL and src.sample and src.icon
    assert src.category in engine.CATEGORIES
    got = next(s for s in engine.catalog() if s["id"] == "ip_change")
    assert got["plugin"] == "" and got["scenarios"]
    for sc in src.scenarios:
        engine.clean_params(src, sc.get("params") or {})
