"""登录鉴权。设置了环境变量 OSA_PASSWORD 才生效；不设置时和以前一样不用登录（只适合本机或内网）。

登录后发一个 HttpOnly cookie：<过期时间>.<签名>。签名密钥由 data/session.key 和密码一起派生，
改密码或删掉 session.key 后，所有设备都要重新登录。
不用登录就能访问的：登录页、分享页（/s/、/skill/、/template/ 和 /api/shared/）、webhook（有自己的 token）、前端静态文件。
"""
import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from urllib.parse import parse_qs, quote, urlsplit
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from .memory import db_path

COOKIE = "osa_session"
MAX_AGE = 30 * 86400          # 登录保持 30 天
RENEW_BELOW = 15 * 86400      # 剩不到 15 天时，下一次请求自动续期
FAIL_WINDOW, MAX_FAILS = 600, 5   # 同一个 IP 10 分钟内输错 5 次，暂停登录到窗口结束

# 前端构建产物里不含数据的文件：登录页和分享页要用
PUBLIC_FILES = {"/manifest.json", "/sw.js", "/favicon.png", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"}
PUBLIC_PREFIXES = ("/assets/", "/hooks/", "/api/shared/", "/s/", "/skill/", "/template/")

router = APIRouter()
_fails: dict[str, list[float]] = {}
_key: bytes | None = None


def enabled() -> bool:
    return bool(os.environ.get("OSA_PASSWORD"))


def _signing_key() -> bytes:
    global _key
    if _key is None:
        f = db_path.parent / "session.key"
        if not f.exists():
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
        _key = f.read_text().strip().encode()
    return hmac.new(_key, os.environ.get("OSA_PASSWORD", "").encode(), hashlib.sha256).digest()


def _sign(exp: int) -> str:
    return hmac.new(_signing_key(), f"v1.{exp}".encode(), hashlib.sha256).hexdigest()


def new_token() -> str:
    exp = int(time.time()) + MAX_AGE
    return f"{exp}.{_sign(exp)}"


def check_token(token: str | None) -> int | None:
    """有效时返回过期时间。"""
    try:
        exp_s, sig = (token or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < time.time() or not hmac.compare_digest(sig, _sign(exp)):
        return None
    return exp


def check_password(given: str) -> bool:
    want = os.environ.get("OSA_PASSWORD", "")
    return bool(want) and hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(want.encode()).digest())


def _cookie_header(token: str, max_age: int, secure: bool) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def _cookies(headers: dict) -> dict:
    out = {}
    for part in headers.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            out[k] = v
    return out


def _same_origin(headers: dict) -> bool:
    """浏览器发起的跨站请求会带上别的 Origin；没有 Origin 的（curl、同源 GET）放行。"""
    origin = headers.get("origin")
    if not origin:
        return True
    return origin != "null" and urlsplit(origin).netloc == headers.get("host")


def _public(path: str) -> bool:
    if ".." in path:   # 不让 /s/../ 这种路径借公开前缀绕过
        return False
    return path in PUBLIC_FILES or path in ("/login", "/logout") or path.startswith(PUBLIC_PREFIXES)


def _safe_next(n: str | None) -> str:
    n = n or "/"
    if not n.startswith("/") or n.startswith("//") or "\\" in n or n.startswith("/login"):
        return "/"
    return n


class AuthMiddleware:
    """纯 ASGI 中间件，HTTP 和 WebSocket 都经过这里。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not enabled():
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        path, method = scope["path"], scope.get("method", "GET")
        if (scope["type"] == "websocket" or method not in ("GET", "HEAD", "OPTIONS")) \
                and not path.startswith("/hooks/") and not _same_origin(headers):
            return await _deny(scope, send, 403, "跨站请求被拒绝")
        if _public(path):
            return await self.app(scope, receive, send)
        exp = check_token(_cookies(headers).get(COOKIE))
        if exp is None:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            if method in ("GET", "HEAD") and not path.startswith("/api/"):
                qs = scope.get("query_string", b"").decode("latin-1")
                nxt = path + ("?" + qs if qs else "")
                return await _redirect(send, "/login" + ("" if nxt == "/" else "?next=" + quote(nxt, safe="")))
            return await _deny(scope, send, 401, "请先登录")
        if scope["type"] == "http" and exp - time.time() < RENEW_BELOW:
            cookie = _cookie_header(new_token(), MAX_AGE, scope.get("scheme") == "https").encode("latin-1")

            async def send_renewed(msg):
                if msg["type"] == "http.response.start":
                    msg = {**msg, "headers": [*msg.get("headers", []), (b"set-cookie", cookie)]}
                await send(msg)
            return await self.app(scope, receive, send_renewed)
        return await self.app(scope, receive, send)


async def _deny(scope, send, status: int, detail: str):
    if scope["type"] == "websocket":
        return await send({"type": "websocket.close", "code": 1008})
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _redirect(send, location: str):
    await send({"type": "http.response.start", "status": 303,
                "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


def _blocked(ip: str) -> bool:
    now = time.time()
    if len(_fails) > 10000:   # 别让大量不同 IP 把内存撑大
        for k in [k for k, v in _fails.items() if not v or now - v[-1] > FAIL_WINDOW]:
            del _fails[k]
    recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    _fails[ip] = recent
    return len(recent) >= MAX_FAILS


# ---------- 登录页 ----------
PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d0d0e"><link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/png" href="/favicon.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Super Agent">
<title>登录 · Open Super Agent</title><style>
:root{--bg:#0d0d0e;--bg2:#151517;--line:#26262a;--fg:#ececec;--fg2:#a8a8ad;--fg3:#6e6e74;--acc:#ececec;--acc-fg:#0d0d0e;--bad:#ef6b6b;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#fbfbfa;--bg2:#f2f2f0;--line:#e2e2df;--fg:#18181a;--fg2:#57575c;--fg3:#8d8d92;--acc:#18181a;--acc-fg:#fbfbfa;color-scheme:light}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
form{width:min(340px,90vw);display:flex;flex-direction:column;gap:14px}
.logo{font-size:30px;text-align:center}h1{margin:0;font-size:18px;text-align:center;font-weight:600}
input{font:inherit;color:var(--fg);background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;outline:none}
input:focus{border-color:var(--fg3)}
button{font:inherit;padding:10px;border-radius:10px;border:1px solid var(--acc);background:var(--acc);color:var(--acc-fg);cursor:pointer}
button:hover{opacity:.88}.err{color:var(--bad);font-size:13px;margin:0;text-align:center}
</style></head><body><form method="post" action="/login">
<div class="logo">◆</div><h1>Open Super Agent</h1>
<input type="hidden" name="next" value="{next}">
<input type="password" name="password" placeholder="密码" autocomplete="current-password" autofocus required>
{error}<button type="submit">登录</button></form></body></html>"""


def _page(nxt: str, error: str = "", status: int = 200) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = PAGE.replace("{next}", html.escape(nxt, quote=True)).replace("{error}", err)
    return HTMLResponse(body, status, headers={"cache-control": "no-store"})


@router.get("/login")
async def login_page(request: Request):
    nxt = _safe_next(request.query_params.get("next"))
    if not enabled() or check_token(request.cookies.get(COOKIE)):
        return RedirectResponse(nxt, 303)
    return _page(nxt)


@router.post("/login")
async def login(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
    nxt = _safe_next(form.get("next"))
    if not enabled():
        return RedirectResponse(nxt, 303)
    ip = request.client.host if request.client else "?"
    if _blocked(ip):
        return _page(nxt, "输错次数太多，请 10 分钟后再试", 429)
    if not check_password(form.get("password", "")):
        _fails.setdefault(ip, []).append(time.time())
        await asyncio.sleep(1)
        return _page(nxt, "密码不对", 401)
    _fails.pop(ip, None)
    r = RedirectResponse(nxt, 303)
    r.headers.append("set-cookie", _cookie_header(new_token(), MAX_AGE, request.url.scheme == "https"))
    return r


@router.post("/logout")
async def logout(request: Request):
    r = RedirectResponse("/login" if enabled() else "/", 303)
    r.headers.append("set-cookie", _cookie_header("", 0, request.url.scheme == "https"))
    return r


@router.get("/api/me")
async def me():
    """要登录才能访问：前端用它判断登录有没有过期。"""
    return {"auth": enabled()}
