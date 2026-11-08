"""网页变了、网站挂了：隔一段时间抓一次网页，和上次比。

web_change：网页的文字变了，或者某个词出现 / 消失了（到货、售罄、成绩查询……）。
  比的是网页转成的文字（去掉脚本、样式、标签），不是 HTML：改个样式、换个广告图不算变。
  计数器、时间、广告这类每次都变的行，用户填进“忽略”就不比了，不然每次都误报。
  state 里留一份文字（最多 MAX_KEEP 字节），变了的时候能列出加了哪些行、删了哪些行，Bot 不用自己去猜。
  出现 / 消失只在变的那一下报：出现了报一次，之后一直在就不再报；消失了再出现，又能报。
site_down：网站打不开了 / 恢复了。连续失败几次才报（偶尔一次超时不打扰），恢复时说挂了多久。
页面打不开（HTTP 报错、超时）时 web_change 不报变化，只在事件上显示出错，免得“网页变成了 502 页面”这种误报。
"""
import difflib
import hashlib
import html
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from ..i18n import N_
from ..registry import POLL, Source
from . import _util
from ._util import oneline, short, text_of

MAX_KEEP = 60_000      # state 里留多少字节的网页文字（用来列出变化）
MAX_DIFF = 3000        # 事件里列出的变化最多多少字
TIMEOUT = 20


# ---------- 共用 ----------
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I)
_TITLE = re.compile(r"<title[^>]*>([\s\S]*?)</title>", re.I)


def page_text(r: httpx.Response) -> str:
    """响应 → 文字。响应头没写编码的，看 <meta charset>（不少中文网站只在这里写 gbk）。"""
    enc = r.charset_encoding
    if not enc:
        m = _META_CHARSET.search(r.content[:4096])
        enc = m[1].decode() if m else "utf-8"
    enc = "gb18030" if enc.lower() in ("gb2312", "gbk", "x-gbk") else enc
    try:
        return r.content.decode(enc, errors="replace")
    except LookupError:
        return r.content.decode("utf-8", errors="replace")


def _neterr(e: Exception) -> str:
    s = oneline(str(e), 150)
    if isinstance(e, httpx.TimeoutException):
        return f"{TIMEOUT} 秒内没响应"
    if "CERTIFICATE" in s.upper() or "SSL" in s.upper():
        return f"HTTPS 证书有问题：{s}"
    return f"连不上：{s or type(e).__name__}"


async def _fetch(url: str) -> httpx.Response:
    try:
        return await _util.get(url, timeout=TIMEOUT)
    except httpx.HTTPStatusError as e:
        raise ValueError(f"网页打不开：HTTP {e.response.status_code}") from None
    except httpx.RequestError as e:
        raise ValueError(f"网页打不开：{_neterr(e)}") from None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


def _where(url: str) -> str:
    return oneline(re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/"), 60)


# ---------- 网页变了 ----------
def _words(kw) -> list[str]:
    return [w.strip() for w in re.split(r"[,，|｜]+", str(kw or "")) if w.strip()]


def _keep(text: str, ignore: list[str]) -> str:
    """去掉含“忽略”词的行。"""
    ig = [w.lower() for w in ignore if w]
    return "\n".join(ln for ln in text.split("\n") if not any(w in ln.lower() for w in ig))


def _cap(text: str) -> str:
    b = text.encode()
    if len(b) <= MAX_KEEP:
        return text
    b = b[:MAX_KEEP]
    return b[:b.rfind(b"\n")].decode(errors="ignore") if b"\n" in b else b.decode(errors="ignore")


def diff(old: str, new: str, limit: int = MAX_DIFF) -> str:
    """两份网页文字的变化：+ 新出现的行，- 没了的行，按页面上的顺序。"""
    lines = []
    for i, ln in enumerate(difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0)):
        if i < 2 or ln.startswith("@@"):
            continue                       # 前两行是文件头，@@ 是位置
        if ln[:1] in "+-" and ln[1:].strip():
            lines.append(ln[0] + " " + oneline(ln[1:], 300))
    if not lines:
        return ""
    added = sum(ln.startswith("+") for ln in lines)
    out, used = [f"多了 {added} 行，少了 {len(lines) - added} 行（+ 是新出现的，- 是没了的）："], 0
    for i, ln in enumerate(lines):
        if used + len(ln) > limit:
            out.append(f"……还有 {len(lines) - i} 行变化，打开链接看")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def _around(text: str, words: list[str], n: int = 5) -> str:
    """含关键词的那几行，让 Bot 看到上下文（价格、按钮旁边的字）。"""
    low = [w.lower() for w in words]
    hits = [oneline(ln, 200) for ln in text.split("\n") if any(w in ln.lower() for w in low)]
    return "\n".join(hits[:n])


def _has(text: str, words: list[str]) -> list[str]:
    low = text.lower()
    return [w for w in words if w.lower() in low]


def _quote(words: list[str]) -> str:
    return "".join(f"「{w}」" for w in words)


async def check_change(params: dict, state):
    url, mode = params["url"], params.get("mode") or "change"
    words, ignore = _words(params.get("keyword")), params.get("ignore") or []
    raw = page_text(await _fetch(url))
    text = _keep(text_of(raw), ignore)
    if not text.strip():
        raise ValueError("网页上没读到文字：可能是用 JavaScript 加载内容的页面，这种现在盯不了；也可能是“忽略”的词把整页都去掉了")
    m = _TITLE.search(raw)
    name = oneline(html.unescape(m[1]), 40) if m and m[1].strip() else _host(url)
    key = hashlib.sha1(json.dumps([url, mode, words, ignore], ensure_ascii=False).encode()).hexdigest()[:16]
    hits = _has(text, words)
    kept = _cap(text)
    new_state = {"key": key, "hash": hashlib.sha256(text.encode()).hexdigest()[:32], "text": kept, "found": bool(hits)}
    if state is None:      # 第一次：记下现状。返回的这条只当“测试”的样例
        if mode == "change":
            ev = {"title": f"网页变了：{name}", "text": "（测试用：这是第一次看到的内容，真的变了的时候这里只列出变了的行）\n" + short(text, 1500)}
        else:
            ev = {"title": f"现在网页上{'有' if hits else '没有'}{_quote(words)}：{name}",
                  "text": _around(text, words) or short(text, 1500)}
        return [ev | {"url": url}], new_state
    if state.get("key") != key:    # 改了网址、关键词或忽略的词：重新记下现状
        return [], new_state
    old = state.get("text") or ""
    if mode == "change":
        if new_state["hash"] == state.get("hash"):
            return [], new_state
        d = diff(old, kept) or "页面文字变了，但变化在很靠后的地方（太长没存下来）或者只是空白，打开链接看"
        return [{"title": f"网页变了：{name}", "url": url, "text": d}], new_state
    was = bool(state.get("found"))
    if mode == "appear" and hits and not was:
        d = diff(old, kept, 1500)
        return [{"title": f"网页上出现了{_quote(hits)}：{name}", "url": url,
                 "text": "含这个词的行：\n" + _around(text, hits) + (f"\n\n{d}" if d else "")}], new_state
    if mode == "disappear" and was and not hits:
        d = diff(old, kept, 1500)
        return [{"title": f"网页上的{_quote(words)}不见了：{name}", "url": url,
                 "text": (d or "页面上找不到这个词了") + "\n\n页面开头：\n" + short(text, 600)}], new_state
    return [], new_state


def validate_change(p: dict) -> dict:
    if p.get("mode") in ("appear", "disappear") and not _words(p.get("keyword")):
        raise ValueError("选了“出现”或“消失”，要填关键词")
    return p


def _summary_change(p: dict) -> str:
    words = _words(p.get("keyword"))
    if p.get("mode") == "appear" and words:
        return f"{_where(p.get('url', ''))} +{'/'.join(words)}"
    if p.get("mode") == "disappear" and words:
        return f"{_where(p.get('url', ''))} -{'/'.join(words)}"
    return _where(p.get("url", ""))


WEB_CHANGE = Source(
    id="web_change", mode=POLL, category=N_("通用"), icon="🔍",
    title=N_("网页变了"),
    description=N_("隔一段时间看一次网页，文字变了、或者某个词出现 / 消失了（到货、售罄、开放报名……），Bot 处理一次。"
                   "只看得到网页上直接写着的文字，要登录、用 JavaScript 加载的内容看不到。"),
    fields=[
        {"key": "url", "label": N_("网址"), "type": "url", "required": True, "placeholder": "https://"},
        {"key": "mode", "label": N_("什么时候算"), "type": "select", "default": "change",
         "options": [{"value": "change", "label": N_("文字有变化")}, {"value": "appear", "label": N_("出现了关键词")},
                     {"value": "disappear", "label": N_("关键词不见了")}]},
        {"key": "keyword", "label": N_("关键词"), "type": "text", "placeholder": N_("加入购物车"),
         "help": N_("选了“出现”或“消失”时要填。多个词用逗号分开：任意一个出现就算出现，全都不见了才算消失；不分大小写")},
        {"key": "ignore", "label": N_("忽略含这些词的行"), "type": "list",
         "help": N_("一行一个。计数、时间、广告这类每次都变的行填进来，免得误报，比如“阅读”“更新于”“广告”")},
    ],
    check=check_change, validate=validate_change, every=60, min_every=10,
    summary=_summary_change,
    sample={"title": "网页变了：通知公告 - 某某大学教务处", "url": "https://jwc.example.edu.cn/tzgg.htm",
            "text": "多了 2 行，少了 1 行（+ 是新出现的，- 是没了的）：\n"
                    "+ 关于 2026-2027 学年第一学期期末考试安排的通知 2026-11-09\n"
                    "+ 关于开展 2027 届毕业生图像信息采集工作的通知 2026-11-08\n"
                    "- 关于 2026 年秋季学期补考报名的通知 2026-09-01"},
    scenarios=[
        {"title": N_("网页内容变了就告诉我"), "description": N_("任何网页：文档、价格表、服务条款、项目主页……"),
         "name": N_("网页变化"), "params": {"mode": "change"},
         "prompt": N_("网页变了。根据列出的变化说清楚改了什么、对我有什么影响；只是日期、计数、广告这类无关紧要的变化，只写 [无新内容]。")},
        {"title": N_("商品到货提醒"), "description": N_("商品页出现“加入购物车”“立即购买”时提醒（缺货时没有这些按钮）"),
         "name": N_("到货提醒"), "params": {"mode": "appear", "keyword": "加入购物车,立即购买"}, "every": 30,
         "prompt": N_("商品好像到货了。告诉我是什么商品、现在的价格（页面上有的话），提醒我尽快去买。")},
        {"title": N_("缺货的商品补货了"), "description": N_("商品页上的“缺货”“售罄”不见了时提醒"),
         "name": N_("补货提醒"), "params": {"mode": "disappear", "keyword": "缺货,售罄,暂时无货"}, "every": 30,
         "prompt": N_("商品页上的缺货提示不见了，可能补货了。说清楚是什么商品、现在的价格，提醒我尽快去看；看着是页面坏了的写 [无新内容]。")},
        {"title": N_("降价提醒"), "description": N_("商品页的文字变了，价格比之前低了才告诉你"),
         "name": N_("降价提醒"), "params": {"mode": "change", "ignore": ["评价", "销量", "已售"]},
         "llm_filter": N_("只有商品价格比之前低了才处理；涨价、价格没变、别的文字变化都忽略"),
         "prompt": N_("商品降价了。说清楚商品、原价、现价、降了多少（百分之几），有优惠券、限时之类的条件也写上。")},
        {"title": N_("公告页有新通知"), "description": N_("学校教务处、政府部门、公司官网的通知公告页"),
         "name": N_("新通知"), "params": {"mode": "change"},
         "prompt": N_("公告页更新了。列出新增的通知：每条一句话说是什么事、跟谁有关、有没有截止日期，和我相关、要动手办的放前面。"
                      "只有删掉的旧通知或者排版变化，只写 [无新内容]。")},
        {"title": N_("招聘页有新职位"), "description": N_("公司的招聘页、职位列表有新职位"),
         "name": N_("新职位"), "params": {"mode": "change"},
         "prompt": N_("招聘页有变化。列出新增的职位（名称、地点、要求要点），和我背景相符的标出来；只是职位下架或者无关变化，只写 [无新内容]。")},
        {"title": N_("成绩、报名开放了"), "description": N_("考试院、学校的页面出现“成绩查询”“报名入口”时提醒"),
         "name": N_("成绩查询"), "params": {"mode": "appear", "keyword": "成绩查询,报名入口"}, "every": 30,
         "prompt": N_("页面上出现了查成绩或报名的入口。告诉我入口在哪、要准备什么、有没有截止时间。")},
    ])


# ---------- 网站挂了 ----------
def _secs(ms: int) -> str:
    return f"{ms / 1000:.2f} 秒" if ms < 10_000 else f"{ms // 1000} 秒"


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "不到 1 分钟"
    if m < 60:
        return f"{m} 分钟"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} 小时" + (f" {m} 分钟" if m else "")
    d, h = divmod(h, 24)
    return f"{d} 天" + (f" {h} 小时" if h else "")


def _clock(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


async def probe(url: str, expect: str = "") -> dict:
    """看一次网站：{"ok", "status", "ms", "error"}。状态码 < 400、页面上有 expect（填了的话）才算正常。"""
    t0 = time.monotonic()
    ms = lambda: int((time.monotonic() - t0) * 1000)
    try:
        r = await _util.get(url, timeout=TIMEOUT, max_bytes=10_000_000)
    except httpx.HTTPStatusError as e:
        return {"ok": False, "status": e.response.status_code, "ms": ms(),
                "error": f"HTTP {e.response.status_code} {e.response.reason_phrase}".strip()}
    except httpx.RequestError as e:
        return {"ok": False, "status": 0, "ms": ms(), "error": _neterr(e), "timeout": isinstance(e, httpx.TimeoutException)}
    except ValueError as e:       # 跳转太多次、跳到了内网、页面太大
        return {"ok": False, "status": 0, "ms": ms(), "error": str(e)}
    out = {"ok": True, "status": r.status_code, "ms": ms(), "error": ""}
    if expect and expect.lower() not in page_text(r).lower():
        out |= {"ok": False, "error": f"页面打得开（HTTP {r.status_code}），但上面没有「{expect}」"}
    return out


async def check_down(params: dict, state):
    url = params["url"]
    if why := await _util.blocked(url):     # 内网地址是设置问题，不算网站挂了
        raise ValueError(why)
    need = max(1, int(params.get("fails") or 2))
    p = await probe(url, str(params.get("expect") or "").strip())
    now = time.time()
    st = state if isinstance(state, dict) and state.get("url") == url else {}
    if p["ok"]:
        events = []
        if st.get("alerted") and params.get("notify_recover", True):
            since = st.get("since") or now
            last = (st.get("last") or {}).get("error") or ""
            events.append({"id": f"up-{int(now)}", "title": f"网站恢复了：{_host(url)}（打不开了大约 {_ago(now - since)}）",
                           "url": url,
                           "text": f"地址：{url}\n现在：HTTP {p['status']}，响应 {_secs(p['ms'])}\n"
                                   f"从 {_clock(since)} 到 {_clock(now)} 打不开，大约 {_ago(now - since)}"
                                   + (f"\n当时的情况：{last}" if last else "")})
        return events, {"url": url, "fails": 0, "since": None, "alerted": False, "last": p}
    fails = int(st.get("fails") or 0) + 1
    since = st.get("since") or now
    alerted = bool(st.get("alerted"))
    new_state = {"url": url, "fails": fails, "since": since, "alerted": alerted or fails >= need, "last": p}
    if alerted or fails < need:
        return [], new_state
    took = f"超过 {TIMEOUT} 秒" if p.get("timeout") else _secs(p["ms"])
    return [{"id": f"down-{int(since)}", "title": f"网站打不开了：{_host(url)}（{oneline(p['error'], 60)}）", "url": url,
             "text": f"地址：{url}\n情况：{p['error']}\n响应时间：{took}\n"
                     f"从 {_clock(since)} 开始，连续 {fails} 次检查都失败"}], new_state


SITE_DOWN = Source(
    id="site_down", mode=POLL, category=N_("系统"), icon="🚨",
    title=N_("网站挂了"),
    description=N_("隔几分钟访问一次网站，连续几次打不开（报错、超时、页面不对）时告诉你，恢复了也说一声，顺便说挂了多久。"),
    fields=[
        {"key": "url", "label": N_("网址"), "type": "url", "required": True, "placeholder": "https://"},
        {"key": "expect", "label": N_("页面上应该有的字（可选）"), "type": "text",
         "help": N_("填了的话，页面打得开但上面没有这几个字也算挂了（报错页、空白页、被劫持）。健康检查接口可以填 ok")},
        {"key": "fails", "label": N_("连续失败几次才报"), "type": "number", "default": 2, "min": 1, "max": 20,
         "help": N_("偶尔一次超时不打扰。检查间隔 × 次数，就是最快多久能发现")},
        {"key": "notify_recover", "label": N_("恢复了也告诉我"), "type": "bool", "default": True},
    ],
    check=check_down, every=5, min_every=2, fire_first=True,
    summary=lambda p: _where(p.get("url", "")),
    sample={"title": "网站打不开了：shop.example.com（HTTP 502 Bad Gateway）", "url": "https://shop.example.com/",
            "text": "地址：https://shop.example.com/\n情况：HTTP 502 Bad Gateway\n响应时间：0.35 秒\n从 11-09 14:20 开始，连续 2 次检查都失败"},
    scenarios=[
        {"title": N_("网站挂了提醒"), "description": N_("自己的网站、博客、常用的服务打不开时告诉你"),
         "name": N_("网站监控"), "params": {"fails": 2, "notify_recover": True},
         "prompt": N_("网站打不开了或者恢复了。一两句话说清楚是哪个网站、什么情况、挂了多久；打不开的，按情况给出排查的第一步"
                      "（5xx 看服务器和日志、超时看网络和负载、证书错误看证书有没有过期、DNS 解析不了看域名）。")},
        {"title": N_("API 健康检查"), "description": N_("定时访问健康检查接口，状态码不对或者返回里没有 ok 就报"),
         "name": N_("API 健康检查"), "params": {"expect": "ok", "fails": 3, "notify_recover": True}, "every": 2,
         "prompt": N_("健康检查失败了。说清楚是状态码不对、超时、还是返回的内容不对，推测可能的原因；恢复了的只用一句话告诉我。")},
        {"title": N_("关键页面打不开时排查"), "description": N_("登录页、支付页这类关键页面挂了，Bot 先自己查一查"),
         "name": N_("关键页面"), "params": {"fails": 2, "notify_recover": True},
         "prompt": N_("这个关键页面打不开了。用 fetch_url 再打开一次确认，看看现在是报错页、空白页还是跳到了别处，"
                      "再搜一下是不是服务商大面积故障；告诉我最可能的原因、影响多大、该先找谁。恢复了的一句话告诉我。")},
    ])

SOURCES = [WEB_CHANGE, SITE_DOWN]
