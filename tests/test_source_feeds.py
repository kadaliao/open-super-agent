"""订阅源：RSS / Atom / JSON Feed 的解析、不规范 XML 的容错、新条目判断、首页自动找订阅源。不联网。"""
import json

import httpx
import pytest

from conftest import settle, until
from osa import events as engine, routines
from osa.sources import _util, feeds

URL = "https://blog.example.com/feed.xml"


@pytest.fixture
def pages(monkeypatch):
    """假的互联网：网址 → 内容（或 (内容, content-type)，或 httpx.Response，或函数）。没有的网址 404。"""
    table: dict = {}

    def handle(req: httpx.Request) -> httpx.Response:
        v = table.get(str(req.url))
        if v is None:
            return httpx.Response(404)
        if callable(v):
            v = v(req)
        if isinstance(v, httpx.Response):
            return v
        body, ctype = v if isinstance(v, tuple) else (v, "application/rss+xml")
        return httpx.Response(200, content=body.encode() if isinstance(body, str) else body, headers={"content-type": ctype})

    async def not_blocked(url):
        return None
    monkeypatch.setattr(_util, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(_util, "blocked", not_blocked)
    return table


def rss(*items: tuple[str, str], title: str = "示例博客") -> str:
    """items：(guid, 标题)，排在前面的是新的。"""
    body = "".join(f"""<item><title>{t}</title><link>https://blog.example.com/{g}</link><guid>{g}</guid>
        <pubDate>Mon, 0{9 - i} Nov 2026 08:00:00 +0800</pubDate><description>&lt;p&gt;{t} 的摘要&lt;/p&gt;</description></item>"""
                   for i, (g, t) in enumerate(items))
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>{body}</channel></rss>'


# ---------- 解析 ----------
def test_rss2_with_content_encoded():
    xml = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>少数派</title>
<item><title>一篇文章</title><link>https://sspai.com/post/1</link><guid isPermaLink="false">post-1</guid>
<dc:creator>王小明</dc:creator><pubDate>Mon, 09 Nov 2026 01:30:00 GMT</pubDate>
<description>短</description>
<content:encoded><![CDATA[<p>这是<b>正文</b>的第一段，比摘要长得多。</p><p>第二段</p><script>alert(1)</script>]]></content:encoded>
</item></channel></rss>"""
    feed = feeds.parse(xml.encode())
    assert feed["title"] == "少数派"
    it = feed["items"][0]
    assert it["title"] == "一篇文章" and it["url"] == "https://sspai.com/post/1" and it["author"] == "王小明"
    assert "正文" in it["body"] and "第二段" in it["body"] and "alert" not in it["body"] and "<p>" not in it["body"]
    assert it["when"].startswith("2026-11-09")
    ev = feeds._event(it, feed["title"])
    assert ev["title"] == "一篇文章" and "来自：少数派" in ev["text"] and "作者：王小明" in ev["text"]


def test_atom_feed():
    xml = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title type="text">Reddit</title>
<entry><title>Qwen4 is out</title><id>t3_abc</id>
<link rel="self" href="https://example.com/self"/><link href="/r/LocalLLaMA/comments/abc/"/>
<author><name>/u/alice</name><uri>https://www.reddit.com/user/alice</uri></author>
<published>2026-11-09T04:37:00+00:00</published>
<content type="html">&lt;div&gt;Benchmarks &amp;amp; weights&lt;/div&gt;</content></entry></feed>"""
    feed = feeds.parse(xml.encode(), "application/atom+xml", "https://www.reddit.com/r/LocalLLaMA/new/.rss")
    it = feed["items"][0]
    assert it["url"] == "https://www.reddit.com/r/LocalLLaMA/comments/abc/"     # 相对地址按订阅源地址补全；rel=self 的不要
    assert it["author"] == "/u/alice"
    assert it["body"] == "Benchmarks & weights"


def test_json_feed_and_podcast_enclosure():
    jf = {"version": "https://jsonfeed.org/version/1.1", "title": "播客",
          "items": [{"id": "ep-2", "url": "https://pod.example.com/2", "title": "第二期", "content_text": "聊了很多",
                     "date_published": "2026-11-08T10:00:00Z", "authors": [{"name": "主播"}],
                     "attachments": [{"url": "https://cdn.example.com/2.mp3", "mime_type": "audio/mpeg",
                                      "duration_in_seconds": 3725}]}]}
    feed = feeds.parse(json.dumps(jf).encode(), "application/feed+json")
    ev = feeds._event(feed["items"][0], feed["title"])
    assert ev["title"] == "第二期" and "作者：主播" in ev["text"]
    assert "音频：https://cdn.example.com/2.mp3（时长 1:02:05）" in ev["text"]

    xml = """<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>P</title>
<item><title>EP1</title><guid>e1</guid><enclosure url="https://cdn.example.com/1.mp3" type="audio/mpeg" length="1"/>
<itunes:duration>45:10</itunes:duration></item></channel></rss>"""
    ev = feeds._event(feeds.parse(xml.encode())["items"][0], "P")
    assert "音频：https://cdn.example.com/1.mp3（时长 45:10）" in ev["text"]


def test_youtube_description_is_nested():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
<title>Andrej Karpathy</title><entry><id>yt:video:abc</id><yt:videoId>abc</yt:videoId><title>Let's build GPT</title>
<link rel="alternate" href="https://www.youtube.com/watch?v=abc"/><published>2026-11-01T00:00:00+00:00</published>
<media:group><media:title>Let's build GPT</media:title><media:content url="https://www.youtube.com/v/abc" type="application/x-shockwave-flash"/>
<media:description>We build a GPT from scratch.</media:description></media:group></entry></feed>"""
    it = feeds.parse(xml.encode())["items"][0]
    assert it["url"] == "https://www.youtube.com/watch?v=abc" and it["body"] == "We build a GPT from scratch."


def test_sloppy_xml_is_repaired():
    # BOM + PHP 警告 + HTML 实体 + 裸的 & + 控制字符
    xml = ("﻿<br />\n<b>Warning</b>: something in feed.php<br />\n"
           '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>A &amp; B</title>'
           "<item><title>Tom &amp; Jerry&nbsp;回来了 & 更多\x0b</title><guid>1</guid><description>x &hellip;</description></item>"
           "</channel></rss>")
    feed = feeds.parse(xml.encode())
    assert feed["title"] == "A & B"
    assert feed["items"][0]["title"] == "Tom & Jerry 回来了 & 更多"


def test_doctype_entities_are_not_expanded():
    xml = """<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>
<rss><channel><title>t</title><item><title>&b;</title><guid>1</guid></item></channel></rss>"""
    it = feeds.parse(xml.encode())["items"][0]
    assert "AAAA" not in it["title"]


def test_gb2312_declared_encoding():
    xml = '<?xml version="1.0" encoding="gb2312"?><rss><channel><title>新闻</title><item><title>今天的新闻</title><guid>1</guid></item></channel></rss>'
    feed = feeds.parse(xml.encode("gb18030"))
    assert feed["title"] == "新闻" and feed["items"][0]["title"] == "今天的新闻"


def test_html_and_garbage():
    assert feeds.parse(b"<!DOCTYPE html><html><head><title>x</title></head><body>hi</body></html>") is None
    with pytest.raises(ValueError, match="不像订阅源"):
        feeds.parse(b"just some text")
    with pytest.raises(ValueError, match="不像订阅源"):
        feeds.parse(b"<root><thing>1</thing></root>")
    with pytest.raises(ValueError, match="JSON Feed"):
        feeds.parse(b'{"hello": 1}')


# ---------- 检查 ----------
async def test_first_check_is_baseline_then_only_new(pages):
    pages[URL] = rss(("b", "第二篇"), ("a", "第一篇"))
    evs, st = await feeds.check({"url": URL}, None)
    assert [e["title"] for e in evs] == ["第二篇"]          # 只当样例，引擎会丢掉
    assert len(st["seen"]) == 2 and st["url"] == URL
    json.dumps(st)

    evs, st = await feeds.check({"url": URL}, st)
    assert evs == []

    pages[URL] = rss(("c", "第三篇"), ("b", "第二篇"), ("a", "第一篇"))
    evs, st = await feeds.check({"url": URL}, st)
    assert [e["title"] for e in evs] == ["第三篇"]
    assert evs[0]["url"] == "https://blog.example.com/c" and "第三篇 的摘要" in evs[0]["text"]
    assert len(st["seen"]) == 3

    # 旧文章掉出订阅源以后也还记得，不会因为又出现而重报
    pages[URL] = rss(("c", "第三篇"))
    evs, st = await feeds.check({"url": URL}, st)
    pages[URL] = rss(("c", "第三篇"), ("a", "第一篇"))
    evs, st = await feeds.check({"url": URL}, st)
    assert evs == []


async def test_changed_url_rebaselines(pages):
    pages[URL] = rss(("a", "一"))
    _, st = await feeds.check({"url": URL}, None)
    other = "https://other.example.com/rss"
    pages[other] = rss(("x", "X"), ("y", "Y"))
    evs, st = await feeds.check({"url": other}, st)
    assert evs == [] and st["url"] == other


async def test_flood_is_capped(pages):
    pages[URL] = rss(("old", "旧的"))
    _, st = await feeds.check({"url": URL}, None)
    many = [(f"n{i}", f"新文章{i}") for i in range(25)]
    pages[URL] = rss(*many, ("old", "旧的"))
    evs, st = await feeds.check({"url": URL}, st)
    assert len(evs) == feeds.MAX_NEW and evs[0]["title"] == "新文章0"
    assert "一共有 25 条新的" in evs[0]["text"]


async def test_seen_is_capped(pages):
    st = {"url": URL, "seen": [f"k{i}" for i in range(feeds.MAX_SEEN)]}
    pages[URL] = rss(("z", "Z"))
    evs, st = await feeds.check({"url": URL}, st)
    assert len(evs) == 1 and len(st["seen"]) == feeds.MAX_SEEN and st["seen"][-1] == f"k{feeds.MAX_SEEN - 2}"


async def test_sorted_newest_first_when_dates_known(pages):
    xml = """<rss><channel><title>t</title>
<item><title>旧</title><guid>1</guid><pubDate>Sun, 01 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>新</title><guid>2</guid><pubDate>Mon, 09 Nov 2026 08:00:00 GMT</pubDate></item></channel></rss>"""
    pages[URL] = xml
    evs, _ = await feeds.check({"url": URL}, None)
    assert evs[0]["title"] == "新"


async def test_homepage_autodiscovers_feed(pages):
    home = "https://blog.example.com/"
    html = ('<html><head><link rel="alternate" type="application/atom+xml" href="/atom.xml">'
            '<link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.xml"></head><body></body></html>')
    pages[home] = (html, "text/html; charset=utf-8")
    pages[URL] = rss(("a", "一"))
    evs, _ = await feeds.check({"url": home}, None)
    assert evs[0]["title"] == "一"


async def test_plain_page_and_http_errors(pages):
    pages["https://plain.example.com/"] = ("<html><body>没有订阅源</body></html>", "text/html")
    with pytest.raises(ValueError, match="RSSHub"):
        await feeds.check({"url": "https://plain.example.com/"}, None)
    with pytest.raises(ValueError, match="HTTP 404"):
        await feeds.check({"url": "https://nothing.example.com/feed"}, None)
    pages[URL] = httpx.Response(429)
    with pytest.raises(ValueError, match="限流"):
        await feeds.check({"url": URL}, None)


def test_params_and_summary():
    with pytest.raises(ValueError):
        engine.clean_params(feeds.FEED, {})
    with pytest.raises(ValueError):
        engine.clean_params(feeds.FEED, {"url": "sspai.com/feed"})
    p = engine.clean_params(feeds.FEED, {"url": " https://www.sspai.com/feed "})
    assert p == {"url": "https://www.sspai.com/feed"}
    assert feeds.FEED.summary(p) == "sspai.com/feed"
    for sc in feeds.FEED.scenarios:
        if sc.get("params"):
            engine.clean_params(feeds.FEED, sc["params"])


async def test_poll_through_engine(pages, llm, make_bot):
    a = make_bot()
    pages[URL] = rss(("a", "第一篇"))
    r = routines.save(a["id"], {"name": "博客", "prompt": "总结新文章",
                                "trigger": {"kind": "event", "source": "rss", "params": {"url": URL}}})
    try:
        out = await engine.poll(r)
        assert not out["accepted"] and engine.state_of(r["id"])["has_sample"]

        pages[URL] = rss(("b", "第二篇"), ("a", "第一篇"))
        llm.script("worker", "[无新内容]")
        out = await engine.poll(routines.get(r["id"]))
        assert out["accepted"]
        await until(lambda: r["id"] not in routines.RUNNING, what="事件跑完")
        await settle()
        sent = json.dumps(llm.bodies("worker")[-1], ensure_ascii=False)
        assert "第二篇" in sent and "第一篇" not in sent
    finally:
        routines.delete(r["id"])
