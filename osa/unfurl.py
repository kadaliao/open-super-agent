"""链接卡片：对话里要展开的网页链接，服务器去取标题、摘要、配图、站点名和图标，前端画成书签卡片（像 Notion 的嵌入）。

- 和 fetch_url 一样先过 tools.blocked：不抓本机和内网地址，每一跳跳转都查。超时 8 秒，最多读 512 KB，只看 HTML。
- 解析 og:*、twitter:*、<title>、<meta name=description>、<link rel=icon>。没有标题的不算成功，前端退回普通链接。
- 结果缓存在 unfurls 表：成功的留 7 天，失败的留 1 小时。同一个地址同时有几个请求时只抓一次。
- YouTube、B 站、X 的播放器和推文由前端直接嵌入（见 web/src/components/Embeds.tsx），这里只给 B 站封面、YouTube 标题这类信息。
- 短链接（b23.tv、t.co……）跳到视频或推文页时不再往下抓，回 {"embed": 落地地址}，前端按那个地址显示播放器或推文。
"""
import asyncio
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import httpx
from . import tools
from .memory import conn

conn.execute("create table if not exists unfurls(url text primary key, data text, ok integer, fetched real)")
conn.commit()

KEEP_OK = 7 * 86400
KEEP_FAIL = 3600
TIMEOUT = 8
MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 5
UA = {"User-Agent": "Mozilla/5.0 (compatible; OpenSuperAgent/1.0; link preview)", "Accept": "text/html,application/xhtml+xml",
      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
_TRANSPORT = None   # 测试时换成假的
_inflight: dict[str, asyncio.Future] = {}


class _Head(HTMLParser):
    """收集 <head> 里的 meta、title、icon；到 <body> 就不再看了。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.icon = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag == "link" and "icon" in a.get("rel", "").lower().split() and a.get("href") and not self.icon:
            self.icon = a["href"]
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _charset(head: bytes, header: str | None) -> str:
    if header:
        return header
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", head[:4096], re.I)
    return m.group(1).decode() if m else "utf-8"


def _abs(base: str, u: str | None) -> str:
    u = (u or "").strip()
    if not u or u.startswith("data:"):
        return ""
    u = urljoin(base, "https:" + u if u.startswith("//") else u)
    return u if u.startswith(("http://", "https://")) else ""


def parse(html: bytes, url: str, encoding: str | None = None) -> dict | None:
    """从网页开头取卡片信息；连标题都没有返回 None。"""
    try:
        text = html.decode(_charset(html, encoding), errors="replace")
    except LookupError:
        text = html.decode("utf-8", errors="replace")
    p = _Head()
    try:
        p.feed(text)
    except Exception:
        pass
    m = p.meta
    clean = lambda s, n: re.sub(r"\s+", " ", s or "").strip()[:n]
    title = clean(m.get("og:title") or m.get("twitter:title") or p.title, 200)
    if not title:
        return None
    host = urlsplit(url).hostname or ""
    return {"url": url, "title": title,
            "description": clean(m.get("og:description") or m.get("twitter:description") or m.get("description"), 300),
            "image": _abs(url, m.get("og:image:secure_url") or m.get("og:image") or m.get("twitter:image") or m.get("twitter:image:src")),
            "site": clean(m.get("og:site_name"), 60) or host.removeprefix("www."),
            "icon": _abs(url, p.icon) or (f"{urlsplit(url).scheme}://{host}/favicon.ico" if host else "")}


# YouTube 的网页开头塞满了脚本，og:title 在 512 KB 以后；它的 oEmbed 接口只回一小段 JSON（标题、作者、封面）
_YOUTUBE = re.compile(r"^https?://((www|m|music)\.)?(youtube\.com|youtu\.be)/", re.I)


async def _oembed(c: httpx.AsyncClient, url: str) -> dict | None:
    r = await c.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"})
    if r.status_code >= 400 or len(r.content) > MAX_BYTES:
        return None
    d = r.json()
    title = re.sub(r"\s+", " ", str(d.get("title") or "")).strip()[:200]
    if not title:
        return None
    return {"url": url, "title": title, "description": str(d.get("author_name") or "")[:300],
            "image": _abs(url, d.get("thumbnail_url")), "site": "YouTube", "icon": "https://www.youtube.com/favicon.ico"}


# 前端能直接嵌入的页面（和 Embeds.tsx 的 parseEmbed 对应）
_EMBED = re.compile(r"^https?://((www|m|mobile|music)\.)?(bilibili\.com/video/BV|youtube\.com/(watch\?|shorts/|live/)|youtu\.be/"
                    r"|(x|twitter)\.com/[^/?#]+/status(es)?/\d)", re.I)
_KEEP_QS = {"v", "t", "p"}   # 视频的落地地址只留编号、开始时间、分 P，去掉分享来源、设备号这些


def _landing(url: str) -> dict:
    u = urlsplit(url)
    tweet = u.hostname and re.search(r"(^|\.)(x|twitter)\.com$", u.hostname)   # 推文的 s、t 都是分享来源
    qs = "" if tweet else urlencode([(k, v) for k, v in parse_qsl(u.query) if k in _KEEP_QS])
    url = urlunsplit((u.scheme, u.netloc, u.path, qs, ""))
    return {"url": url, "title": "", "description": "", "image": "", "site": "", "icon": "", "embed": url}


async def _fetch(url: str) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=UA, follow_redirects=False, transport=_TRANSPORT) as c:
        if _YOUTUBE.match(url):
            return await _oembed(c, url)
        for _ in range(MAX_REDIRECTS + 1):   # 跳转自己跟：每一跳都要检查，免得公网地址跳到内网
            if await tools.blocked(url):
                return None
            async with c.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(str(r.url), r.headers["location"])
                    if _EMBED.match(url):
                        return _landing(url)
                    continue
                if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html").lower():
                    return None
                buf = b""
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) >= MAX_BYTES or b"<body" in buf[-len(chunk) - 8:].lower():
                        break
                return parse(buf[:MAX_BYTES], str(r.url), r.charset_encoding)
    return None


def _cached(url: str) -> tuple[bool, dict | None]:
    r = conn.execute("select data, ok, fetched from unfurls where url=?", (url,)).fetchone()
    if not r or time.time() - r[2] > (KEEP_OK if r[1] else KEEP_FAIL):
        return False, None
    return True, json.loads(r[0]) if r[1] else None


async def get(url: str) -> dict | None:
    """一个网址的卡片信息；抓不到、不让抓的返回 None。"""
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s/]+", url) or len(url) > 2000:
        return None
    hit, data = _cached(url)
    if hit:
        return data
    if url in _inflight:
        return await asyncio.shield(_inflight[url])
    fut = asyncio.get_running_loop().create_future()
    _inflight[url] = fut
    try:
        try:
            data = await asyncio.wait_for(_fetch(url), TIMEOUT + 2)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
            data = None
        now = time.time()
        conn.execute("insert or replace into unfurls values(?,?,?,?)", (url, json.dumps(data or {}, ensure_ascii=False), int(bool(data)), now))
        conn.execute("delete from unfurls where fetched < ?", (now - KEEP_OK,))
        conn.commit()
        fut.set_result(data)
        return data
    finally:
        _inflight.pop(url, None)
        if not fut.done():
            fut.set_result(None)
