"""事件来源共用的小工具：联网抓取（挡内网、限大小）、网页转文字、截断。"""
import html
import re
from urllib.parse import urljoin
import httpx
from .. import tools

UA = {"User-Agent": "Mozilla/5.0 (compatible; open-super-agent events)"}
TRANSPORT = None           # 测试时换成 httpx.MockTransport
MAX_REDIRECTS = 5
blocked = tools.blocked    # 测试时换掉，免得去解析域名


async def get(url: str, *, timeout: float = 20, max_bytes: int = 3_000_000, headers: dict | None = None,
              params: dict | None = None) -> httpx.Response:
    """GET 一个公网地址。跳转自己跟，每一跳都检查；内网地址、太大的响应抛 ValueError，HTTP 错误抛 httpx.HTTPStatusError。"""
    async with httpx.AsyncClient(timeout=timeout, headers=UA | (headers or {}), follow_redirects=False,
                                 transport=TRANSPORT) as c:
        for _ in range(MAX_REDIRECTS + 1):
            if why := await blocked(url):
                raise ValueError(why)
            r = await c.get(url, params=params)
            params = None
            if not (r.is_redirect and r.headers.get("location")):
                break
            url = urljoin(str(r.url), r.headers["location"])
        else:
            raise ValueError(f"跳转超过 {MAX_REDIRECTS} 次")
    if len(r.content) > max_bytes:
        raise ValueError(f"内容太大（超过 {max_bytes // 1_000_000} MB）")
    r.raise_for_status()
    return r


_BLOCK = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|ul|ol|table|blockquote|pre)\b[^>]*>", re.I)


def text_of(page: str) -> str:
    """网页 → 文字：去掉脚本、样式和标签，块级元素换行，保留段落，方便比较前后变化。"""
    page = re.sub(r"<(script|style|noscript|svg|template)[\s\S]*?</\1>", " ", page, flags=re.I)
    page = re.sub(r"<!--[\s\S]*?-->", " ", page)
    page = _BLOCK.sub("\n", page)
    page = html.unescape(re.sub(r"<[^>]+>", " ", page))
    lines = (re.sub(r"[ \t\r\f\v 　]+", " ", ln).strip() for ln in page.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def short(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def oneline(s: str, n: int = 200) -> str:
    return short(re.sub(r"\s+", " ", s or ""), n)
