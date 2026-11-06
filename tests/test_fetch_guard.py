"""fetch_url 默认不抓本机和内网地址，跳转到内网也挡住。"""
import httpx
import pytest

from osa import tools
from osa.config import CFG


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.17.0.1", "169.254.169.254",
                                "100.100.100.100", "0.0.0.0", "::1", "::ffff:127.0.0.1", "fe80::1", "fd00::1", "224.0.0.1"])
def test_private_addresses(ip):
    assert tools.private_ip(ip)


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "198.18.0.7"])   # 最后一个：代理的假地址
def test_public_addresses(ip):
    assert not tools.private_ip(ip)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765/api/bootstrap", "http://localhost:8765/", "http://[::1]/",
                                 "http://169.254.169.254/latest/meta-data/", "http://2130706433/", "http://127.1/"])
async def test_blocked_urls(url):
    assert (await tools.fetch_url(url)).startswith("没抓取")


async def test_only_http():
    assert await tools.blocked("file:///etc/passwd") == "只能抓取 http / https 网址"


async def test_redirect_to_private_is_blocked(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/bootstrap"})
    monkeypatch.setattr(tools, "_TRANSPORT", httpx.MockTransport(handler))
    out = await tools.fetch_url("https://public.example.invalid/start")
    assert out.startswith("没抓取") and seen == ["https://public.example.invalid/start"]   # 内网那一跳没发出去


async def test_redirects_are_followed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, text="<html><script>x()</script><p>到了 b</p></html>")
    monkeypatch.setattr(tools, "_TRANSPORT", httpx.MockTransport(handler))
    assert await tools.fetch_url("https://site.example.invalid/a") == " 到了 b "


async def test_private_allowed_when_configured(monkeypatch):
    monkeypatch.setitem(CFG, "tools", {"fetch_private": True})
    assert await tools.blocked("http://127.0.0.1:8765/") is None
