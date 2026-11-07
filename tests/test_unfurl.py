"""链接卡片：解析 og 信息、不抓内网、跳转每一跳都查、只读开头、缓存（失败的也缓存一会儿）、短链接落到视频推文上。"""
import time
import httpx
import pytest
from osa import memory, server, unfurl

PAGE = """<!doctype html><html><head>
<meta charset="gbk">
<title>  备用标题 </title>
<meta property="og:title" content="开源 Super Agent &amp; 朋友">
<meta property="og:description" content="一个自部署的   AI 队友">
<meta property="og:image" content="/social.png">
<meta property="og:site_name" content="GitHub">
<link rel="shortcut icon" href="//github.githubassets.com/favicon.svg">
</head><body><meta property="og:title" content="正文里的不算"></body></html>"""


@pytest.fixture
def web(monkeypatch):
    seen = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        path = req.url.path
        if path == "/repo":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=PAGE.encode("gbk"))
        if path == "/plain":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                  content="<html><head><title>只有标题</title></head></html>".encode())
        if path == "/hop":
            return httpx.Response(302, headers={"location": "/repo"})
        if path == "/evil":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        if req.url.host == "www.youtube.com" and path == "/oembed":
            assert req.url.params["url"] == "https://youtu.be/dQw4w9WgXcQ"
            return httpx.Response(200, json={"title": "Never Gonna  Give You Up", "author_name": "Rick Astley",
                                             "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"})
        if req.url.host == "b23.tv":
            return httpx.Response(302, headers={"location": "https://m.bilibili.com/video/BV1GJ411x7h7?p=2&share_source=copy&bbid=XY123&ts=1"})
        if req.url.host == "t.co":
            return httpx.Response(301, headers={"location": "https://x.com/jack/status/20?s=46&t=abc"})
        if path == "/pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        if path == "/huge":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  content=b"<html><head>" + b"<!-- x -->" * 100_000 + b"<title>too late</title></head></html>")
        return httpx.Response(404)
    monkeypatch.setattr(unfurl, "_TRANSPORT", httpx.MockTransport(handle))
    memory.conn.execute("delete from unfurls")
    memory.conn.commit()
    return seen


async def test_parse_og(web):
    d = await unfurl.get("https://site.example.invalid/repo")
    assert d == {"url": "https://site.example.invalid/repo", "title": "开源 Super Agent & 朋友", "description": "一个自部署的 AI 队友",
                 "image": "https://site.example.invalid/social.png", "site": "GitHub", "icon": "https://github.githubassets.com/favicon.svg"}
    d = await unfurl.get("https://www.other.example.invalid/plain")
    assert d["title"] == "只有标题" and d["site"] == "other.example.invalid" and d["icon"].endswith("/favicon.ico") and d["image"] == ""


async def test_youtube_uses_oembed(web):
    d = await unfurl.get("https://youtu.be/dQw4w9WgXcQ")
    assert d["title"] == "Never Gonna Give You Up" and d["description"] == "Rick Astley" and d["site"] == "YouTube"
    assert web == ["https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ&format=json"]


async def test_redirects_checked_each_hop(web):
    assert (await unfurl.get("https://site.example.invalid/hop"))["url"] == "https://site.example.invalid/repo"
    assert await unfurl.get("https://site.example.invalid/evil") is None
    assert not any("169.254" in u for u in web)               # 内网那一跳没发出去
    assert await unfurl.get("http://127.0.0.1:8765/api/bootstrap") is None
    assert not any("127.0.0.1" in u for u in web)


async def test_short_links_land_on_embeds(web):
    d = await unfurl.get("https://b23.tv/AbCdEfG")
    assert d["embed"] == "https://m.bilibili.com/video/BV1GJ411x7h7?p=2"   # 分享来源、设备号去掉，分 P 留着
    assert not any("bilibili.com" in u for u in web)                     # 落地页不用再抓
    assert (await unfurl.get("https://t.co/xyz"))["embed"] == "https://x.com/jack/status/20"
    assert (await server.unfurl_link("https://b23.tv/AbCdEfG"))["ok"] is True
    assert "embed" not in await unfurl.get("https://site.example.invalid/hop")   # 普通跳转照旧抓落地页


async def test_only_html_and_only_the_head(web):
    assert await unfurl.get("https://site.example.invalid/pdf") is None
    assert await unfurl.get("https://site.example.invalid/huge") is None   # 512 KB 以后的标题看不到
    assert await unfurl.get("ftp://site.example.invalid/x") is None


async def test_cache(web):
    await unfurl.get("https://site.example.invalid/repo")
    await unfurl.get("https://site.example.invalid/nothing")
    n = len(web)
    assert (await unfurl.get("https://site.example.invalid/repo"))["title"]
    assert await unfurl.get("https://site.example.invalid/nothing") is None
    assert len(web) == n                                         # 成功和失败都缓存了
    memory.conn.execute("update unfurls set fetched=? where url like '%nothing'", (time.time() - unfurl.KEEP_FAIL - 1,))
    await unfurl.get("https://site.example.invalid/nothing")
    assert len(web) == n + 1                                     # 失败的过一小时再试


async def test_endpoint(web):
    assert (await server.unfurl_link("https://site.example.invalid/repo"))["ok"] is True
    assert await server.unfurl_link("http://10.0.0.1/") == {"ok": False}
