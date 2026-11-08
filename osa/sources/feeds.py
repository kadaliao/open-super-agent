"""订阅源有新文章：RSS 2.0 / RSS 1.0 / Atom / JSON Feed。

隔一段时间抓一次，和上次比，新的条目一条一个事件。state 里只记看到过的条目 id（哈希过，最多 MAX_SEEN 个），很小。
- 网址填成网站首页也行：页面里写了订阅源地址（<link rel="alternate" type="application/rss+xml">）就照着去抓。
- 有的订阅源 XML 写得不规范：开头有 BOM、空行或者 PHP 的警告，用了 &nbsp; 这类 HTML 实体，有裸的 &。
  先去掉开头的杂物再解析；解析不了的把实体和控制字符修一修再试一次。
- 不用会解析外部实体的 XML 库：ElementTree 不去取外部实体，DOCTYPE（里面能定义实体）也先整段去掉。
"""
import hashlib
import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.entities import name2codepoint
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx

from ..i18n import N_
from ..registry import POLL, Source
from . import _util
from ._util import oneline, short, text_of

MAX_SEEN = 500        # 记住多少条看到过的
MAX_NEW = 20          # 一次最多报几条（订阅源换了 id 的规则时，不至于一下子涌进来几百条）
MAX_TEXT = 600        # 每条给 Bot 看的摘要多长
MAX_BYTES = 8_000_000   # 播客的订阅源常常有好几 MB
ACCEPT = ("application/rss+xml, application/atom+xml, application/feed+json, application/xml;q=0.9, "
          "text/xml;q=0.9, */*;q=0.8")
RSSHUB = "https://docs.rsshub.app"


# ---------- 抓取 ----------
async def _get(url: str) -> httpx.Response:
    try:
        return await _util.get(url, max_bytes=MAX_BYTES, headers={"Accept": ACCEPT})
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        why = {401: "要登录", 403: "对方拒绝了，可能不让程序访问", 404: "地址不存在", 410: "订阅源已经停了",
               429: "请求太频繁，被对方限流了"}.get(code, "")
        raise ValueError(f"订阅源打不开：HTTP {code}" + (f"（{why}）" if why else "")) from None
    except httpx.TimeoutException:
        raise ValueError("订阅源打不开：超时了") from None
    except httpx.RequestError as e:
        raise ValueError(f"订阅源打不开：{oneline(str(e), 150) or type(e).__name__}") from None


async def fetch(url: str) -> dict:
    """抓订阅源 → {"title", "items"}。抓到的是网页的话，找页面里写的订阅源地址再抓一次。"""
    r = await _get(url)
    feed = parse(r.content, r.headers.get("content-type", ""), str(r.url))
    if feed is None:
        found = discover(_decode(r.content, r.headers.get("content-type", "")), str(r.url))
        if found and found != str(r.url):
            r = await _get(found)
            feed = parse(r.content, r.headers.get("content-type", ""), str(r.url))
    if feed is None:
        raise ValueError(f"这个网址是普通网页，不是订阅源（RSS / Atom）。没有订阅源的网站可以试试用 RSSHub 转一个：{RSSHUB}")
    return feed


_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(["'])(.*?)\2""", re.S)


def discover(page: str, base: str) -> str:
    """网页里写着的订阅源地址（RSS 优先），没有返回空。"""
    best = ""
    for tag in _LINK_TAG.findall(page[:300_000]):
        a = {k.lower(): unescape(v) for k, _, v in _ATTR.findall(tag)}
        typ = a.get("type", "").lower()
        if "alternate" not in a.get("rel", "").lower().split() or not a.get("href"):
            continue
        if typ in ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json"):
            url = urljoin(base, a["href"].strip())
            if typ == "application/rss+xml":
                return url
            best = best or url
    return best


# ---------- 解析 ----------
_ENC = re.compile(rb"""^\s*<\?xml[^>]*encoding\s*=\s*["']([\w.:-]+)""")
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)
_START = re.compile(r"<\?xml|<rss\b|<feed\b|<rdf:RDF\b|<RDF\b", re.I)
_HTML = re.compile(r"\s*(?:<!--[\s\S]*?-->\s*)*<(?:!doctype\s+html|html)\b", re.I)
_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_DOCTYPE = re.compile(r"<!DOCTYPE[^>\[]*(?:\[[\s\S]*?\])?\s*>", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)(\w*;?)")
_TAGS = re.compile(r"</?(?:p|div|br|a|img|span|strong|em|ul|li)\b", re.I)


def _decode(raw: bytes, ctype: str = "") -> str:
    """字节 → 文字：先看 XML 声明里的编码，再看响应头，都没有按 UTF-8。gb2312 / gbk 按 gb18030 解，它是两者的超集。"""
    raw = raw.removeprefix(b"\xef\xbb\xbf")
    m = _ENC.match(raw) or _CHARSET.search(ctype or "")
    enc = (m[1].decode() if isinstance(m[1], bytes) else m[1]).lower() if m else "utf-8"
    if enc in ("gb2312", "gbk", "x-gbk"):
        enc = "gb18030"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _entity(m: re.Match) -> str:
    name = m[1]
    if name.endswith(";") and name[:-1] in name2codepoint:
        return f"&#{name2codepoint[name[:-1]]};"
    return "&amp;" + name


def _xml(text: str) -> ET.Element:
    text = _DOCTYPE.sub("", _DECL.sub("", text, count=1), count=1)
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        pass
    try:
        return ET.fromstring(_AMP.sub(_entity, _CTRL.sub("", text)))
    except ET.ParseError as e:
        raise ValueError(f"订阅源的 XML 坏了，解析不了（{e}）") from None


def _ln(tag) -> str:
    """去掉命名空间的标签名：{http://www.w3.org/2005/Atom}entry → entry。"""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _child(el, name: str):
    return next((c for c in el if _ln(c.tag) == name), None)


def _first(el, *names: str) -> str:
    """按顺序找第一个有内容的子元素；直接的子元素里没有，再往下层找（YouTube 的简介在 media:group/media:description 里）。"""
    for deep in (False, True):
        for n in names:
            for c in (el.iter() if deep else el):
                if c is not el and _ln(c.tag) == n and (t := _text(c)):
                    return t
    return ""


def _when(s) -> tuple[float | None, str]:
    """日期 → (时间戳, 显示用的“2026-11-09 14:30”，换成本机时区)。认不出的原样显示。"""
    s = str(s or "").strip()
    if not s:
        return None, ""
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None, oneline(s, 40)
    if dt.tzinfo:
        dt = dt.astimezone()
    return dt.timestamp(), dt.strftime("%Y-%m-%d %H:%M")


def _plain(raw: str) -> str:
    """HTML 片段 → 文字。有的订阅源把 HTML 转义了两遍，转一次还剩标签的再转一次。"""
    t = text_of(raw or "")
    return text_of(t) if _TAGS.search(t) else t


def _duration(v) -> str:
    s = str(v or "").strip()
    if not s.isdigit():
        return s[:20]
    n = int(s)
    return f"{n // 3600}:{n % 3600 // 60:02}:{n % 60:02}" if n >= 3600 else f"{n // 60}:{n % 60:02}"


def _item(id_: str, title: str, url: str, when, author: str, summary: str, full: str,
          media: str = "", media_type: str = "", duration="") -> dict:
    ts, shown = _when(when)
    s, f = _plain(summary), _plain(full)
    body = s if len(s) >= 150 or len(s) >= len(f) else f      # 摘要太短（只有一句话）就用正文开头
    id_ = (id_ or url or f"{title}|{when}").strip()
    return {"key": hashlib.sha1(id_.encode()).hexdigest()[:16], "title": oneline(_plain(title), 200), "url": url,
            "ts": ts, "when": shown, "author": oneline(author, 60), "body": body, "media": media,
            "media_type": media_type, "duration": _duration(duration)}


def _link(el, base: str) -> str:
    url = ""
    for c in el:
        if _ln(c.tag) != "link":
            continue
        href = (c.get("href") or "").strip()
        if href:                                   # Atom：<link rel="alternate" href="…"/>
            if c.get("rel", "alternate") == "alternate":
                url = href
                break
        elif t := _text(c):                        # RSS：<link>…</link>
            url = t
            break
    if not url:
        g = _child(el, "guid")
        if g is not None and g.get("isPermaLink", "true") != "false" and _text(g).startswith("http"):
            url = _text(g)
    if not url and (i := _text(_child(el, "id"))).startswith("http"):
        url = i
    return urljoin(base, url) if url else ""


def _author(el) -> str:
    for c in el:
        if _ln(c.tag) in ("author", "creator"):
            name = next((_text(x) for x in c if _ln(x.tag) == "name"), "") or _text(c)
            m = re.fullmatch(r"\S+@\S+\s*\((.+)\)", name)      # RSS 的 author 常写成 “邮箱 (名字)”
            return m[1] if m else name
    return ""


def _media(el) -> tuple[str, str]:
    for c in el:
        n = _ln(c.tag)
        if n == "enclosure" and c.get("url"):
            return c.get("url"), c.get("type") or ""
        if n == "link" and c.get("rel") == "enclosure" and c.get("href"):
            return c.get("href"), c.get("type") or ""
    return "", ""


def _xml_item(el, base: str) -> dict:
    media, mtype = _media(el)
    return _item(_first(el, "guid", "id"), _first(el, "title"), _link(el, base),
                 _first(el, "pubDate", "published", "date", "issued", "updated", "modified"), _author(el),
                 _first(el, "description", "summary"), _first(el, "encoded", "content"),
                 media, mtype, _first(el, "duration"))


def _json_feed(text: str, base: str) -> dict:
    try:
        d = json.loads(text)
    except ValueError:
        raise ValueError("订阅源的内容不是合法的 JSON") from None
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise ValueError("这个 JSON 不是 JSON Feed（没有 items）")
    items = []
    for it in d["items"]:
        if not isinstance(it, dict):
            continue
        authors = it.get("authors") if isinstance(it.get("authors"), list) else [it.get("author")]
        author = "、".join(str(a["name"]) for a in authors if isinstance(a, dict) and a.get("name"))
        att = next((a for a in it.get("attachments") or [] if isinstance(a, dict) and a.get("url")), {})
        url = str(it.get("url") or it.get("external_url") or "")
        items.append(_item(str(it.get("id") or ""), str(it.get("title") or ""), urljoin(base, url) if url else "",
                           it.get("date_published") or it.get("date_modified"), author, str(it.get("summary") or ""),
                           str(it.get("content_html") or it.get("content_text") or ""), str(att.get("url") or ""),
                           str(att.get("mime_type") or ""), att.get("duration_in_seconds") or ""))
    return {"title": oneline(str(d.get("title") or ""), 80), "items": items}


def parse(raw: bytes, ctype: str = "", base: str = "") -> dict | None:
    """订阅源原文 → {"title", "items"}。看着是网页（HTML）返回 None，格式坏得修不好抛 ValueError。"""
    text = _decode(raw, ctype).lstrip("﻿ \t\r\n")
    if text[:1] in ("{", "["):
        return _json_feed(text, base)
    if _HTML.match(text):
        return None
    m = _START.search(text)
    if not m and "<" not in text:
        raise ValueError("这个网址的内容不是 XML 也不是 JSON，不像订阅源")
    text = text[m.start():] if m else text[text.find("<"):]      # 去掉开头的杂物
    try:
        root = _xml(text)
    except ValueError:
        if re.search(r"<html\b", text[:5000], re.I):              # 写成 XHTML 的网页，解析不了也当网页
            return None
        raise
    kind = _ln(root.tag).lower()
    if kind == "html":
        return None
    chan = _child(root, "channel")
    parents = [root] if chan is None else [chan, root]           # RSS 2.0 的条目在 channel 里，RSS 1.0 的和 channel 并列
    items = [_xml_item(el, base) for p in parents for el in p if _ln(el.tag) in ("item", "entry")]
    if not items and kind not in ("rss", "rdf", "feed"):
        raise ValueError("这个网址的内容不像订阅源（没有 RSS / Atom 的条目）")
    return {"title": oneline(_plain(_text(_child(chan if chan is not None else root, "title"))), 80), "items": items}


# ---------- 检查 ----------
def _event(it: dict, feed_title: str) -> dict:
    meta = "　".join(x for x in (f"来自：{feed_title}" if feed_title else "", f"作者：{it['author']}" if it["author"] else "",
                                f"发布：{it['when']}" if it["when"] else "") if x)
    lines = [meta, short(it["body"], MAX_TEXT)]
    if it["media"]:
        kind = "音频" if it["media_type"].startswith("audio") else "视频" if it["media_type"].startswith("video") else "附件"
        lines.append(f"{kind}：{it['media']}" + (f"（时长 {it['duration']}）" if it["duration"] else ""))
    return {"id": it["key"], "title": it["title"] or oneline(it["body"], 80) or "（没有标题）", "url": it["url"],
            "text": "\n".join(x for x in lines if x)}


async def check(params: dict, state):
    url = params["url"]
    feed = await fetch(url)
    items = feed["items"]
    if items and all(it["ts"] is not None for it in items):
        items = sorted(items, key=lambda it: it["ts"], reverse=True)    # 有的订阅源旧的在前
    same = isinstance(state, dict) and state.get("url") == url
    seen = (state.get("seen") or []) if same else []
    new_state = {"url": url, "seen": list(dict.fromkeys([it["key"] for it in items] + seen))[:MAX_SEEN]}
    if state is None:     # 第一次：只记下现状；最新的一条留作“测试”的样例
        return ([_event(items[0], feed["title"])] if items else []), new_state
    if not same:          # 换了网址：重新记下现状，不把新网址里的旧文章当成新的
        return [], new_state
    old, fresh = set(seen), []
    for it in items:
        if it["key"] not in old:
            old.add(it["key"])
            fresh.append(it)
    events = [_event(it, feed["title"]) for it in fresh[:MAX_NEW]]
    if len(fresh) > MAX_NEW:
        events[0]["text"] += f"\n（这次一共有 {len(fresh)} 条新的，只列了最新的 {MAX_NEW} 条）"
    return events, new_state


def _summary(p: dict) -> str:
    return oneline(re.sub(r"^https?://(www\.)?", "", str(p.get("url") or "")).rstrip("/"), 60)


FEED = Source(
    id="rss", mode=POLL, category=N_("资讯"), icon="📰",
    title=N_("订阅源有新文章"),
    description=N_("RSS / Atom / JSON Feed 有新文章、新视频、新一期播客时，Bot 处理一次：总结、挑出值得看的、翻译……"),
    fields=[{"key": "url", "label": N_("订阅源地址"), "type": "url", "required": True, "placeholder": "https://sspai.com/feed",
             "help": N_("RSS、Atom、JSON Feed 都行；填网站首页也可以，页面里写了订阅源地址的会自动找到。"
                        "没有 RSS 的网站（微博、B 站、公众号……）很多能用 RSSHub 转成订阅源：https://docs.rsshub.app")}],
    check=check, every=30, min_every=10,
    summary=_summary,
    sample={"title": "用快捷指令把每天的待办自动发到微信", "url": "https://sspai.com/post/90000",
            "text": "来自：少数派　作者：王小明　发布：2026-11-09 09:30\n"
                    "每天早上打开好几个 App 看待办太麻烦了。这篇文章介绍怎么用 iPhone 的快捷指令，每天 8 点把提醒事项里今天要做的事整理好，"
                    "自动发到微信的文件传输助手，顺便附上天气……"},
    scenarios=[
        {"title": N_("订阅源有新文章时总结"), "description": N_("任何 RSS：每篇新文章用几句话说清楚讲什么"),
         "name": N_("订阅源"),
         "prompt": N_("订阅源有新文章。每篇用三条要点总结，最后一句说值不值得细读；明显是广告、和我关心的事无关的跳过。"
                      "都不值得看就只写 [无新内容]。")},
        {"title": N_("Hacker News 上有关键词的新帖"), "description": N_("hnrss.org 按关键词筛的 Hacker News 新帖，把网址里的 LLM 换成你关心的词"),
         "name": "Hacker News", "params": {"url": "https://hnrss.org/newest?q=LLM"}, "every": 60,
         "prompt": N_("这些是 Hacker News 上的新帖子。每条用一句话说讲什么、为什么值得看；和关键词关系不大、标题党的跳过。"
                      "都不值得看就只写 [无新内容]。")},
        {"title": N_("V2EX 节点有新帖"), "description": N_("V2EX 某个节点的新主题，把网址里的 create（分享创造）换成别的节点名"),
         "name": "V2EX", "params": {"url": "https://www.v2ex.com/feed/create.xml"},
         "prompt": N_("V2EX 有新帖子。挑出有意思的项目或讨论，每条一句话说是什么；求助、广告、水帖跳过。都不值得看就只写 [无新内容]。")},
        {"title": N_("YouTube 频道有新视频"), "description": N_("把网址里的 channel_id 换成频道的 ID（频道页面的“分享 → 复制频道 ID”）"),
         "name": "YouTube", "params": {"url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCXUPKJO5MZQN11PqgIvyuvQ"},
         "prompt": N_("频道发了新视频。根据标题和简介说清楚视频讲什么、适合谁看，值得看的给出两三个看点。")},
        {"title": N_("Reddit 版块有新帖"), "description": N_("Reddit 某个版块的新帖，把网址里的 LocalLLaMA 换成你关注的版块"),
         "name": "Reddit", "params": {"url": "https://www.reddit.com/r/LocalLLaMA/new/.rss"}, "every": 60,
         "prompt": N_("Reddit 版块有新帖子。挑出有信息量的（新发布、实测结果、教程、重要讨论），每条一两句话说清楚；"
                      "提问、梗图、重复的跳过。都不值得看就只写 [无新内容]。")},
        {"title": N_("arXiv 上有新论文"), "description": N_("按关键词搜 arXiv 最新论文，把网址里的 agent 换成你关心的方向；"
                                                       "想看整个分类填 https://rss.arxiv.org/rss/cs.AI（每天上百篇，一次最多列 20 篇）"),
         "name": "arXiv", "every": 720,
         "params": {"url": "https://export.arxiv.org/api/query?search_query=cat:cs.AI+AND+abs:agent"
                           "&sortBy=submittedDate&sortOrder=descending&max_results=30"},
         "prompt": N_("这些是 arXiv 上的新论文。挑出最值得看的几篇，每篇用一两句话说做了什么、新在哪、结论可不可信；"
                      "增量很小的跳过。都不值得看就只写 [无新内容]。")},
        {"title": N_("播客出了新一期"), "description": N_("播客的 RSS 地址一般在节目主页或 Apple 播客里能找到"),
         "name": N_("播客"), "params": {"url": "https://lexfridman.com/feed/podcast/"}, "every": 120,
         "prompt": N_("播客出了新一期。根据节目介绍说清楚这期聊什么、嘉宾是谁、多长，列出两三个最值得听的话题。")},
        {"title": N_("少数派有新文章"), "description": N_("少数派的 RSS，挑出和效率工具、数码相关的"),
         "name": N_("少数派"), "params": {"url": "https://sspai.com/feed"},
         "llm_filter": N_("只处理介绍软件、效率方法、数码产品的文章；纯广告、活动通知忽略"),
         "prompt": N_("少数派有新文章。每篇用两三句话说清楚介绍了什么、对我有什么用；值得马上试的工具单独列出来。")},
        {"title": N_("关注的博客更新了"), "description": N_("个人博客、公司技术博客有新文章（例子是阮一峰的网络日志）"),
         "name": N_("博客更新"), "params": {"url": "https://www.ruanyifeng.com/blog/atom.xml"}, "every": 120,
         "prompt": N_("关注的博客有新文章。用三条要点总结，摘出一句最有意思的话，最后说值不值得花时间读全文。")},
        {"title": N_("B 站 UP 主有新视频（RSSHub）"), "description": N_("B 站没有 RSS，用 RSSHub 转；把网址里的数字换成 UP 主的 uid。"
                                                                 "公共的 rsshub.app 常常限流，最好自己部署一个"),
         "name": N_("B 站更新"), "params": {"url": "https://rsshub.app/bilibili/user/video/946974"}, "every": 60,
         "prompt": N_("UP 主发了新视频。根据标题和简介说清楚讲什么，值不值得看。")},
    ])

SOURCES = [FEED]
