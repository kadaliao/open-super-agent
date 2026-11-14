"""内置的网页工具：web_search、fetch_url。注册在 builtin.py，Router / Worker / 小应用都从 registry 找。
用哪家搜索、哪家读网页按配方来（websearch.py）；这里管安全检查（内网地址、不用的网站）和直接抓网页。"""
import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit
import httpx
from . import websearch
from .config import CFG

UA = {"User-Agent": "open-super-agent/0.1"}
MAX_REDIRECTS = 5
_TRANSPORT = None   # 测试时换成假的


def _days(v) -> int | None:
    try:
        return max(1, min(int(v), 3650)) if v not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None


async def web_search(query: str, days=None) -> str:
    return await websearch.search(query, _days(days))


async def search_posts(platform: str, query: str, days=None, count=None) -> str:
    try:
        count = int(count) if count not in (None, "") else None
    except (TypeError, ValueError):
        count = None
    return await websearch.search_posts(platform, query, _days(days), count)


# Clash、Surge 这类代理的 TUN 模式把所有域名解析到这一段假地址再转发出去，不是真的内网
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def private_ip(ip: str) -> bool:
    """本机、内网、链路本地（含云服务器的元数据地址 169.254.169.254）、100.64/10（Tailscale 等）、保留和组播地址。"""
    a = ipaddress.ip_address(ip.split("%")[0])
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a in FAKE_IP:
        return False
    return not a.is_global or a.is_multicast


async def blocked(url: str) -> str | None:
    """fetch_url 能不能抓这个地址：不能的话返回原因。
    网页里的提示注入可能让 Bot 去读本机的接口（没开登录时不用密码）、内网服务或云服务器的元数据，所以默认只抓公网地址。
    先解析域名再判断，解析到内网的域名也挡住。检查和真正连接之间域名还可能换地址（DNS rebinding），这里不管。"""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "只能抓取 http / https 网址"
    if (CFG.get("tools") or {}).get("fetch_private"):
        return None
    if await private_host(url):
        return f"没抓取：{u.hostname} 是本机或内网地址。要抓内网页面，在 config.yaml 里设 tools.fetch_private: true"
    return None


async def private_host(url: str) -> bool:
    """网址的主机是不是本机或内网地址：先解析域名，解析到内网的也算。解析不了的算不是，交给真正的请求报错。"""
    u = urlsplit(url)
    host = u.hostname or ""
    try:
        ips = [host] if _is_ip(host) else [i[4][0] for i in await asyncio.get_running_loop().getaddrinfo(
            host, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)]
    except (OSError, ValueError):
        return False
    return any(private_ip(ip) for ip in ips)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%")[0])
        return True
    except ValueError:
        return False


async def fetch_url(url: str) -> str:
    url = str(url or "").strip()
    if why := await blocked(url):
        return why
    if websearch.blocked_domain(url):
        return websearch.refuse_text(url, fetching=True)
    return await websearch.fetch(url, private=await private_host(url))


async def direct_fetch(url: str) -> str:
    """自己下载网页、去掉标签（配方里的 direct）。"""
    async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=False, transport=_TRANSPORT) as c:
        for _ in range(MAX_REDIRECTS + 1):   # 跳转自己跟：每一跳都要检查，免得公网地址跳到内网、不用的网站
            if why := await blocked(url):
                return why
            if websearch.blocked_domain(url):
                return websearch.refuse_text(url, fetching=True)
            r = await c.get(url)
            if not (r.is_redirect and r.headers.get("location")):
                break
            url = urljoin(str(r.url), r.headers["location"])
        else:
            return f"没抓取：跳转超过 {MAX_REDIRECTS} 次"
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", "", r.text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text)[:8000]


def schema(name: str, desc: str, params: dict[str, str]) -> dict:
    """参数都是文字的简单工具的 schema。"""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in params.items()},
                       "required": list(params)}}}
