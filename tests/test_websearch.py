"""搜索网页、读网页按配方调各家：自动的顺序、出错换下一家、只要最近几天的、不用的网站、设置里的顺序和密钥。"""
import datetime as dt
import json

import httpx
import pytest

from osa import tools, vault, websearch
from osa.config import CFG
from osa.memory import conn

DDG = """
<a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=x.com&amp;u3=1">广告</a>
<a class="result__snippet" href="#">广告词</a>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage&amp;rut=1">示例 <b>页面</b></a>
<a class="result__snippet" href="#">这是 &amp; 摘要</a>
<a rel="nofollow" class="result__a" href="https://baike.baidu.com/item/x">不要的</a>
<a class="result__snippet" href="#">百科</a>
"""


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    conn.execute("delete from settings where key='web'")
    conn.commit()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    for n in ("WEB_TAVILY_API_KEY", "WEB_JINA_API_KEY"):
        vault.delete(n)
    yield
    conn.execute("delete from settings where key='web'")
    conn.commit()


def fake(monkeypatch, handler):
    seen = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)
    t = httpx.MockTransport(h)
    monkeypatch.setattr(websearch, "_TRANSPORT", t)
    monkeypatch.setattr(tools, "_TRANSPORT", t)
    return seen


def deepseek_on(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")


def names(kind):
    return [r["name"] for r in websearch.order(kind)]


def test_auto_order_without_deepseek():
    assert names("search") == ["duckduckgo"]       # 没填密钥、没填地址的不用
    assert names("fetch") == ["direct"]           # Jina 默认不开


def test_auto_order_with_deepseek(monkeypatch):
    deepseek_on(monkeypatch)
    assert names("search") == ["deepseek", "duckduckgo"]
    assert names("fetch") == ["deepseek", "direct"]


async def test_duckduckgo_parse_and_block(monkeypatch):
    seen = fake(monkeypatch, lambda req: httpx.Response(200, text=DDG))
    out = await tools.web_search("示例")
    assert out == "- 示例 页面 | https://example.org/page\n  这是 & 摘要"   # 广告和 baidu.com 的去掉了，链接换成真实地址
    assert seen[0].url.host == "html.duckduckgo.com"


async def test_deepseek_search_with_days(monkeypatch):
    deepseek_on(monkeypatch)
    body = {"results": [{"title": "新闻", "url": "https://news.example.org/1", "summary": "长摘要", "snippet": "短",
                         "date": "2026-11-10T08:00:00Z"},
                        {"title": "百度的", "url": "https://www.baidu.com/x", "summary": "x"}]}
    seen = fake(monkeypatch, lambda req: httpx.Response(200, json=body))
    before = conn.execute("select count(*) from llm_calls where role='web'").fetchone()[0]
    out = await tools.web_search("电动车 补贴", days="7")
    assert out == "- 新闻 | https://news.example.org/1 | 2026-11-10\n  长摘要"
    req = seen[0]
    assert str(req.url) == "https://api.deepseek.com/v1/tools/search" and req.headers["authorization"] == "Bearer sk-test"
    since = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    assert json.loads(req.content) == {"query": f"电动车 补贴 after:{since}", "count": websearch.COUNT}
    row = conn.execute("select model, provider, cost from llm_calls where role='web' order by id desc limit 1").fetchone()
    assert conn.execute("select count(*) from llm_calls where role='web'").fetchone()[0] == before + 1
    assert row[0] == "search" and row[1] == "deepseek" and row[2] > 0


async def test_falls_back_to_next(monkeypatch):
    deepseek_on(monkeypatch)

    def handler(req):
        if req.url.host == "api.deepseek.com":
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return httpx.Response(200, text=DDG)
    fake(monkeypatch, handler)
    out = await tools.web_search("示例")
    assert out.startswith("- 示例 页面 | https://example.org/page")
    assert "DeepSeek：HTTP 402：Insufficient Balance" in out and "DuckDuckGo" in out.splitlines()[-1]


async def test_all_fail(monkeypatch):
    fake(monkeypatch, lambda req: httpx.Response(429))
    out = await tools.web_search("x")
    assert out.startswith("搜索失败") and "429" in out


async def test_no_results(monkeypatch):
    fake(monkeypatch, lambda req: httpx.Response(200, text="<html>nothing</html>"))
    assert (await tools.web_search("x")).startswith("无结果")


async def test_fetch_deepseek_markdown(monkeypatch):
    deepseek_on(monkeypatch)
    seen = fake(monkeypatch, lambda req: httpx.Response(200, json={"title": "标题", "content": "# 正文\n\n\n\n第二段"}))
    out = await tools.fetch_url("https://site.example.invalid/a")
    assert out == "# 标题\n\n# 正文\n\n第二段"
    assert json.loads(seen[0].content) == {"url": "https://site.example.invalid/a"}


async def test_fetch_falls_back_to_direct(monkeypatch):
    deepseek_on(monkeypatch)

    def handler(req):
        if req.url.host == "api.deepseek.com":
            return httpx.Response(404, json={"error": {"message": "no usable content"}})
        return httpx.Response(200, text="<p>自己抓的</p>")
    fake(monkeypatch, handler)
    out = await tools.fetch_url("https://site.example.invalid/a")
    assert out.startswith("自己抓的") and "HTTP 404" in out


async def test_private_pages_never_go_to_services(monkeypatch):
    deepseek_on(monkeypatch)
    monkeypatch.setitem(CFG, "tools", {"fetch_private": True})
    seen = fake(monkeypatch, lambda req: httpx.Response(200, text="<p>内网页面</p>"))
    assert await tools.fetch_url("http://127.0.0.1:9/x") == "内网页面"
    assert [r.url.host for r in seen] == ["127.0.0.1"]


async def test_blocked_sites(monkeypatch):
    seen = fake(monkeypatch, lambda req: httpx.Response(200, text="x"))
    for url in ("https://www.baidu.com/s?wd=x", "https://baike.baidu.com/item/y", "https://baidu.com/"):
        assert (await tools.fetch_url(url)).startswith("没抓取")
    assert not seen
    assert not websearch.blocked_domain("https://notbaidu.com/") and not websearch.blocked_domain("https://example.org/")


async def test_redirect_into_blocked_site(monkeypatch):
    seen = fake(monkeypatch, lambda req: httpx.Response(302, headers={"location": "https://www.baidu.com/link"}))
    assert (await tools.fetch_url("https://short.example.invalid/x")).startswith("没抓取")
    assert len(seen) == 1


def test_save_order_fields_and_block():
    out = websearch.save({"search": ["tavily", "duckduckgo"]})
    assert out["search"]["auto"] is False and [s["name"] for s in out["search"]["services"]][:2] == ["tavily", "duckduckgo"]
    assert names("search") == ["duckduckgo"]                      # Tavily 还没填 Key
    tav = next(s for s in out["search"]["services"] if s["name"] == "tavily")
    assert tav["missing"] and tav["fields"][0]["set"] is False
    out = websearch.save({"fields": {"tavily": {"api_key": "tvly-x"}}})
    assert names("search") == ["tavily", "duckduckgo"]
    assert vault.get("WEB_TAVILY_API_KEY") == "tvly-x"
    assert next(s for s in out["search"]["services"] if s["name"] == "tavily")["fields"][0] == {
        "key": "api_key", "label": "API Key", "secret": True, "optional": False, "placeholder": "", "set": True, "value": ""}
    websearch.save({"fields": {"tavily": {"api_key": ""}}})
    assert vault.get("WEB_TAVILY_API_KEY") is None
    assert websearch.save({"search": []})["search"]["auto"] is True
    assert websearch.save({"block": ["https://www.Example.COM/path", "foo.org"]})["block"] == ["example.com", "foo.org"]
    assert websearch.save({"block": []})["block"] == []
    assert websearch.save({"block": None})["block"] == ["baidu.com"]   # 回到 config.yaml 的
    with pytest.raises(ValueError):
        websearch.save({"search": ["nope"]})
    with pytest.raises(ValueError):
        websearch.save({"block": ["not a domain"]})
    with pytest.raises(ValueError):
        websearch.save({"fields": {"searxng": {"url": "ftp://x"}}})


async def test_searxng_field_and_range(monkeypatch):
    websearch.save({"fields": {"searxng": {"url": "http://localhost:8080/"}}})
    seen = fake(monkeypatch, lambda req: httpx.Response(200, json={"results": [
        {"title": "S", "url": "https://s.example.org/", "content": "c", "publishedDate": None}]}))
    assert names("search") == ["searxng", "duckduckgo"]
    out = await tools.web_search("q", days=30)
    assert out == "- S | https://s.example.org/\n  c"
    assert str(seen[0].url) == "http://localhost:8080/search?q=q&format=json&time_range=month"


async def test_config_recipe_overrides_default(monkeypatch):
    mine = {"name": "duckduckgo", "kind": "search", "title": "我的", "request": {"method": "GET", "url": "https://my.example.org/s",
            "params": {"q": "$query"}}, "results": "hits", "item": {"title": "t", "url": "u", "text": "s"}}
    monkeypatch.setitem(CFG, "web", {**(CFG.get("web") or {}), "recipes": [mine]})
    fake(monkeypatch, lambda req: httpx.Response(200, json={"hits": [{"t": "A", "u": "https://a.example.org/", "s": "b"}]}))
    assert [r["title"] for r in websearch.recipes("search") if r["name"] == "duckduckgo"] == ["我的"]
    assert await tools.web_search("q") == "- A | https://a.example.org/\n  b"


async def test_settings_test_endpoint(monkeypatch):
    deepseek_on(monkeypatch)
    fake(monkeypatch, lambda req: httpx.Response(200, json={"results": [{"title": "t", "url": "https://x.example.org/"}]}))
    r = await websearch.test("search", "deepseek")
    assert r["ok"] and r["sample"].startswith("- t")
    r = await websearch.test("search", "tavily")
    assert not r["ok"] and r["error"] == "还没填：API Key"
