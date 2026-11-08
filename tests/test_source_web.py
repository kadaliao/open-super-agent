"""网页变了、网站挂了：文字比较和变化列表、忽略的行、关键词出现 / 消失只报一次、连续失败才报、恢复时说挂了多久。不联网。"""
import json
import time

import httpx
import pytest

from osa import events as engine
from osa.sources import _util, web

URL = "https://shop.example.com/item/1"


@pytest.fixture
def pages(monkeypatch):
    """假的互联网：网址 → HTML（或 httpx.Response，或函数）。没有的网址 404。"""
    table: dict = {}

    def handle(req: httpx.Request) -> httpx.Response:
        v = table.get(str(req.url))
        if v is None:
            return httpx.Response(404)
        if callable(v):
            v = v(req)
        if isinstance(v, httpx.Response):
            return v
        return httpx.Response(200, content=v.encode() if isinstance(v, str) else v,
                              headers={"content-type": "text/html; charset=utf-8"})

    async def not_blocked(url):
        return None
    monkeypatch.setattr(_util, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(_util, "blocked", not_blocked)
    return table


def page(*lines: str, title: str = "小米手环 10") -> str:
    return f"<html><head><title>{title}</title><script>var t = {time.time()};</script></head><body>" + \
        "".join(f"<p>{ln}</p>" for ln in lines) + "</body></html>"


def change(**kw) -> dict:
    return engine.clean_params(web.WEB_CHANGE, {"url": URL, **kw})


# ---------- 网页变了 ----------
async def test_change_lists_added_and_removed_lines(pages):
    p = change()
    pages[URL] = page("价格：299 元", "颜色：黑色", "库存紧张")
    evs, st = await web.check_change(p, None)
    assert len(evs) == 1 and "第一次看到的内容" in evs[0]["text"]       # 只是“测试”的样例
    json.dumps(st)

    evs, st = await web.check_change(p, st)                             # 脚本里的时间变了，文字没变
    assert evs == []

    pages[URL] = page("价格：259 元", "颜色：黑色", "库存紧张", "新增：白色")
    evs, st = await web.check_change(p, st)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["title"] == "网页变了：小米手环 10" and ev["url"] == URL
    assert "+ 价格：259 元" in ev["text"] and "- 价格：299 元" in ev["text"] and "+ 新增：白色" in ev["text"]
    assert "颜色：黑色" not in ev["text"]                                 # 没变的行不列
    assert "多了 2 行，少了 1 行" in ev["text"]


async def test_ignored_lines_dont_count(pages):
    p = change(ignore="阅读\n更新于")
    pages[URL] = page("通知一", "阅读 100", "更新于 10:00")
    _, st = await web.check_change(p, None)
    pages[URL] = page("通知一", "阅读 250", "更新于 10:05")
    evs, st = await web.check_change(p, st)
    assert evs == []
    pages[URL] = page("通知二", "通知一", "阅读 300")
    evs, _ = await web.check_change(p, st)
    assert len(evs) == 1 and "+ 通知二" in evs[0]["text"] and "阅读" not in evs[0]["text"]


async def test_appear_fires_once_and_rearms(pages):
    p = change(mode="appear", keyword="加入购物车,立即购买")
    pages[URL] = page("暂时缺货", "到货通知")
    evs, st = await web.check_change(p, None)
    assert "现在网页上没有" in evs[0]["title"]                            # 样例
    pages[URL] = page("¥299", "加入购物车")
    evs, st = await web.check_change(p, st)
    assert len(evs) == 1 and evs[0]["title"] == "网页上出现了「加入购物车」：小米手环 10"
    assert "含这个词的行" in evs[0]["text"] and "+ 加入购物车" in evs[0]["text"]
    pages[URL] = page("¥289", "加入购物车")
    assert (await web.check_change(p, st))[0] == []                      # 一直在：不再报
    pages[URL] = page("暂时缺货")
    evs, st = await web.check_change(p, st)
    assert evs == [] and st["found"] is False                            # 出现模式下消失不报，但重新等它出现
    pages[URL] = page("立即购买")
    evs, st = await web.check_change(p, st)
    assert len(evs) == 1 and "立即购买" in evs[0]["title"]


async def test_disappear_needs_all_words_gone(pages):
    p = change(mode="disappear", keyword="缺货，售罄")
    pages[URL] = page("缺货", "售罄")
    _, st = await web.check_change(p, None)
    pages[URL] = page("售罄")
    evs, st = await web.check_change(p, st)
    assert evs == []
    pages[URL] = page("¥299", "加入购物车")
    evs, st = await web.check_change(p, st)
    assert len(evs) == 1 and evs[0]["title"] == "网页上的「缺货」「售罄」不见了：小米手环 10"
    assert "- 售罄" in evs[0]["text"]
    assert (await web.check_change(p, st))[0] == []


async def test_keyword_is_case_insensitive(pages):
    p = change(mode="appear", keyword="In Stock")
    pages[URL] = page("Sold out")
    _, st = await web.check_change(p, None)
    pages[URL] = page("IN STOCK now")
    evs, _ = await web.check_change(p, st)
    assert len(evs) == 1


async def test_changed_settings_rebaseline(pages):
    pages[URL] = page("缺货")
    _, st = await web.check_change(change(mode="appear", keyword="有货"), None)
    pages[URL] = page("有货", "库存 5")
    evs, st = await web.check_change(change(mode="appear", keyword="库存"), st)
    assert evs == [] and st["found"] is True


async def test_errors_dont_look_like_changes(pages):
    p = change()
    pages[URL] = httpx.Response(502)
    with pytest.raises(ValueError, match="HTTP 502"):
        await web.check_change(p, {"key": "x"})
    pages[URL] = "<html><body><div id=app></div><script>render()</script></body></html>"
    with pytest.raises(ValueError, match="JavaScript"):
        await web.check_change(p, None)


async def test_gbk_page_without_charset_header(pages):
    html = '<html><head><meta charset="gbk"><title>教务处</title></head><body><p>期末考试安排</p></body></html>'
    pages[URL] = httpx.Response(200, content=html.encode("gbk"), headers={"content-type": "text/html"})
    evs, _ = await web.check_change(change(), None)
    assert "期末考试安排" in evs[0]["text"] and "教务处" in evs[0]["title"]


async def test_big_page_state_and_diff_are_capped(pages):
    p = change()
    pages[URL] = page(*[f"第 {i} 行：这是一段比较长的文字，用来把页面撑大一些" for i in range(3000)])
    _, st = await web.check_change(p, None)
    assert len(st["text"].encode()) <= web.MAX_KEEP
    pages[URL] = page(*[f"第 {i} 行：改过的文字" for i in range(3000)])
    evs, _ = await web.check_change(p, st)
    assert len(evs[0]["text"]) < web.MAX_DIFF + 200 and "还有" in evs[0]["text"]


def test_change_params():
    with pytest.raises(ValueError, match="关键词"):
        change(mode="appear")
    with pytest.raises(ValueError):
        change(mode="sometimes")
    p = change(ignore="阅读, 广告")
    assert p["mode"] == "change" and p["ignore"] == ["阅读", "广告"]
    assert web.WEB_CHANGE.summary(p) == "shop.example.com/item/1"
    assert web.WEB_CHANGE.summary(change(mode="appear", keyword="有货")) == "shop.example.com/item/1 +有货"
    for sc in web.WEB_CHANGE.scenarios:
        engine.clean_params(web.WEB_CHANGE, {"url": URL, **sc.get("params", {})})


# ---------- 网站挂了 ----------
SITE = "https://status.example.com/health"


def down(**kw) -> dict:
    return engine.clean_params(web.SITE_DOWN, {"url": SITE, **kw})


async def test_site_down_after_n_failures_then_recovers(pages):
    p = down()
    assert p["fails"] == 2 and p["notify_recover"] is True
    pages[SITE] = "ok"
    evs, st = await web.check_down(p, None)
    assert evs == []                                   # 第一次是好的：什么都不报

    pages[SITE] = httpx.Response(503)
    evs, st = await web.check_down(p, st)
    assert evs == [] and st["fails"] == 1              # 一次失败不打扰
    evs, st = await web.check_down(p, st)
    assert len(evs) == 1
    assert evs[0]["title"] == "网站打不开了：status.example.com（HTTP 503 Service Unavailable）"
    assert "连续 2 次" in evs[0]["text"] and "响应时间" in evs[0]["text"]
    evs, st = await web.check_down(p, st)
    assert evs == [] and st["fails"] == 3              # 已经报过了

    st["since"] = time.time() - 3 * 3600 - 5 * 60       # 假装挂了 3 小时 5 分钟
    pages[SITE] = "ok"
    evs, st = await web.check_down(p, st)
    assert len(evs) == 1 and evs[0]["title"] == "网站恢复了：status.example.com（打不开了大约 3 小时 5 分钟）"
    assert "HTTP 200" in evs[0]["text"] and "HTTP 503" in evs[0]["text"]
    assert st["fails"] == 0 and not st["alerted"]
    json.dumps(st)


async def test_site_down_first_check_and_no_recover(pages):
    p = down(fails=1, notify_recover=False)
    pages[SITE] = httpx.Response(500)
    evs, st = await web.check_down(p, None)
    assert len(evs) == 1                               # fire_first：一开始就挂着，第一次就报
    assert web.SITE_DOWN.fire_first
    pages[SITE] = "ok"
    evs, st = await web.check_down(p, st)
    assert evs == [] and not st["alerted"]             # 不要恢复通知


async def test_site_down_expect_timeout_and_dns(pages):
    pages[SITE] = "<html><body>Error establishing a database connection</body></html>"
    evs, _ = await web.check_down(down(fails=1, expect="ok"), None)
    assert "上面没有「ok」" in evs[0]["text"] and "HTTP 200" in evs[0]["text"]

    def slow(req):
        raise httpx.ReadTimeout("timed out", request=req)
    pages[SITE] = slow
    evs, _ = await web.check_down(down(fails=1), None)
    assert "没响应" in evs[0]["text"] and f"超过 {web.TIMEOUT} 秒" in evs[0]["text"]

    def nodns(req):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided", request=req)
    pages[SITE] = nodns
    evs, _ = await web.check_down(down(fails=1), None)
    assert "连不上" in evs[0]["text"]

    def badcert(req):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired", request=req)
    pages[SITE] = badcert
    evs, _ = await web.check_down(down(fails=1), None)
    assert "证书" in evs[0]["text"]


async def test_site_down_redirects_are_followed(pages):
    pages[SITE] = httpx.Response(301, headers={"location": "/v2/health"})
    pages["https://status.example.com/v2/health"] = "ok"
    evs, st = await web.check_down(down(fails=1), None)
    assert evs == [] and st["last"]["ok"]


async def test_site_down_blocks_intranet(monkeypatch):
    async def blocked(url):
        return "没抓取：192.168.1.1 是本机或内网地址"
    monkeypatch.setattr(_util, "blocked", blocked)
    with pytest.raises(ValueError, match="内网"):
        await web.check_down(engine.clean_params(web.SITE_DOWN, {"url": "http://192.168.1.1/"}), None)


def test_site_down_params():
    with pytest.raises(ValueError):
        down(fails=0)
    with pytest.raises(ValueError):
        engine.clean_params(web.SITE_DOWN, {})
    assert web.SITE_DOWN.summary(down()) == "status.example.com/health"
    for sc in web.SITE_DOWN.scenarios:
        engine.clean_params(web.SITE_DOWN, {"url": SITE, **sc.get("params", {})})
