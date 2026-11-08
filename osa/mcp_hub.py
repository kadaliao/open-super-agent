"""应用（MCP server）：在应用市场里添加、启用和停用，工具交给 Worker 使用。

三种连接方式：
- stdio：在本机启动一个进程（npx / uvx / 可执行文件）。
- http：连远程地址（Streamable HTTP）。可以在请求头里带 token，也可以走 OAuth：
  用户在弹窗里登录授权，客户端用动态注册（DCR）自己拿 client_id，令牌加密存进保险库，过期自动刷新。
  不支持动态注册的（比如 Slack），oauth 里写用户自己建的 client_id / client_secret。
- sse：旧版的远程协议（HTTP + SSE），只提供这种地址的服务（百度网盘）用。请求头和 OAuth 和 http 一样。

应用配置存在数据库里；第一次启动时从 config.yaml 的 mcp_servers 导入。
工具名格式为 <应用名>__<工具>。deny 列表里的词出现在工具名中时，这个工具不开放。
配置里的值可以写 vault:名字 / env:名字（整个值），或在字符串里嵌 {vault:名字}，连接时才取明文。

每个应用跑在自己的后台协程里（MCP 的连接不能跨协程关闭），停用时通知它退出。
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse
import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth import oauth2 as _oauth2, utils as _auth_utils
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED
from .config import CFG, DATA
from .memory import conn
from . import i18n, mcpapps, registry, security, vault

conn.execute("create table if not exists apps(name text primary key, config text, enabled integer, created real)")
conn.commit()

_ctx: dict[str, "registry.Context"] = {}   # 每个应用注册的工具都挂在它自己的 Context 上，断开时一起撤销
_sessions: dict = {}                          # 连着的应用的会话：界面要读资源、调工具（见 mcpapps.py）


def session_of(name: str):
    return _sessions.get(name)
status: dict[str, "str | i18n.Later"] = {}   # 应用 -> 状态说明，给前端看；到显示时才翻（i18n.later），读的时候 str()
CONFIRM: set[str] = set()        # 默认需要用户确认的工具（规则可以覆盖）
_running: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
_connected: set[str] = set()
_needs_login: set[str] = set()   # OAuth 应用：还没授权，或者授权过期了
_reconnected: dict[str, float] = {}
notify = None                    # 由 server 设置：async (event) -> None

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
WORKSPACE = DATA / "workspace"
KEYS = ("catalog", "type", "command", "args", "env", "url", "headers", "oauth", "oauth_redirect",
        "deny", "confirm", "owned")
LOGIN_WAIT_S = 600               # 等用户在弹窗里登录授权的时限


class NeedsLogin(Exception):
    """OAuth 应用需要用户在浏览器里登录授权。"""


class _QuietLogin(logging.Filter):
    """要用户登录是正常情况，SDK 不用为它打一整段报错栈。"""
    def filter(self, record):
        return not (record.exc_info and isinstance(record.exc_info[1], NeedsLogin))


logging.getLogger("mcp.client.auth.oauth2").addFilter(_QuietLogin())
log = logging.getLogger(__name__)


def _issuer_ok(meta, expected: str):
    """滴答清单的发现文档写 https://dida365.com/，元数据里写 https://dida365.com。
    SDK 自己的 issuers_match 也认为两者是同一个服务器，只是这一处比较用了严格相等。"""
    if not _auth_utils.issuers_match(str(meta.issuer), expected):
        _auth_utils.validate_metadata_issuer(meta, expected)


_oauth2.validate_metadata_issuer = _issuer_ok


# ---------- 存储 ----------
def from_yaml(s: dict) -> dict:
    """config.yaml 里 mcp_servers 的一项 → 应用配置。"""
    if s.get("url"):
        return {"type": "http", "url": s["url"], "headers": s.get("headers", {}), "oauth": bool(s.get("oauth")),
                "deny": s.get("deny", []), "confirm": s.get("confirm", "auto")}
    return {"command": s.get("command", ""), "args": s.get("args", []), "env": {k: f"env:{k}" for k in s.get("env_from", [])},
            "deny": s.get("deny", []), "confirm": s.get("confirm", "auto")}


def _seed():
    """把 config.yaml 里还没导入过的 mcp_servers 导入数据库。"""
    have = {r[0] for r in conn.execute("select name from apps")}
    for name, s in (CFG.get("mcp_servers") or {}).items():
        if name in have:
            continue
        conn.execute("insert into apps values(?,?,?,?)", (name, json.dumps(from_yaml(s)), int(bool(s.get("enabled"))), time.time()))
    conn.commit()


def all_apps() -> dict[str, dict]:
    return {n: json.loads(c) | {"enabled": bool(e)}
            for n, c, e in conn.execute("select name, config, enabled from apps order by created")}


def get(name: str) -> dict | None:
    return all_apps().get(name)


def _secret_free(v):
    """给前端看的配置：嵌在网址里的密钥只显示引用名。"""
    return v if not isinstance(v, str) or not v.startswith("vault:") else i18n.t("（保险库：{name}）", name=v[6:])


def state(name: str, a: dict) -> str:
    """connected 已连接 | connecting 连接中 | login 等用户授权 | needs_login 要重新授权 | off 停用 | error 出错"""
    if name in _connected:
        return "connected"
    if not a["enabled"]:
        return "off"
    w = _login.get(name)
    if w and w["url"].done() and not w["code"].done():
        return "login"
    if name in _needs_login:
        return "needs_login"
    if name in _running:
        return "connecting"
    return "error"


def public() -> list[dict]:
    out = []
    for n, a in all_apps().items():
        tools = tools_of(n)
        out.append({"name": n, "catalog": a.get("catalog") or "", "type": a.get("type") or "stdio",
                    "command": a.get("command", ""), "args": a.get("args", []), "env": a.get("env", {}),
                    "url": _secret_free(a.get("url", "")), "headers": list((a.get("headers") or {}).keys()),
                    "oauth": bool(a.get("oauth")), "deny": a.get("deny", []), "confirm": a.get("confirm", "auto"),
                    "enabled": a["enabled"], "status": str(status.get(n, "")), "state": state(n, a), "tools": tools,
                    "connected": n in _connected,
                    "confirm_tools": sorted(t.split("__", 1)[1] for t in CONFIRM if t.startswith(n + "__"))})
    return out


def save(name: str, data: dict) -> dict:
    """新建或修改应用配置。只改 data 里给了的字段，其余沿用旧值。"""
    if not NAME.match(name):
        raise ValueError(i18n.t("应用名只能用小写字母、数字、- 和 _，以字母开头，最多 24 个字符"))
    old = get(name) or {}
    cfg = {k: old[k] for k in KEYS if k in old} | {k: data[k] for k in KEYS if k in data}
    if data.get("oauth") is True and isinstance(old.get("oauth"), dict):
        cfg["oauth"] = old["oauth"]     # 自定义编辑器只知道开或关，保留清单里写的客户端和 scope
    cfg.setdefault("deny", [])
    cfg.setdefault("confirm", "auto")
    if cfg.get("type", "stdio") in ("http", "sse"):
        cfg["url"] = (cfg.get("url") or "").strip()
        if not cfg["url"]:
            raise ValueError(i18n.t("缺少应用地址"))
        if not isinstance(cfg.get("headers", {}), dict):
            raise ValueError(i18n.t("headers 要是对象"))
    else:
        cfg["command"] = (cfg.get("command") or "").strip()
        cfg.setdefault("args", [])
        cfg.setdefault("env", {})
        if not cfg["command"]:
            raise ValueError(i18n.t("缺少启动命令"))
        if not isinstance(cfg["args"], list) or not isinstance(cfg["env"], dict):
            raise ValueError(i18n.t("args 要是列表，env 要是对象"))
    enabled = bool(data.get("enabled", old.get("enabled", True)))
    if enabled:      # 停用、删除不检查：已经不能用的应用也要能关掉
        security.check_app(name, cfg)
    conn.execute("insert or replace into apps values(?,?,?,coalesce((select created from apps where name=?),?))",
                 (name, json.dumps(cfg), int(enabled), name, time.time()))
    conn.commit()
    return cfg | {"enabled": enabled}


def oauth_key(name: str) -> str:
    return "OAUTH_" + name.upper().replace("-", "_")


def remove(name: str):
    """删除应用，连同市场替它存进保险库的密钥和授权令牌。"""
    a = get(name) or {}
    for ref in [*(a.get("owned") or []), oauth_key(name)]:
        vault.delete(ref)
    conn.execute("delete from apps where name=?", (name,))
    conn.commit()
    status.pop(name, None)
    _needs_login.discard(name)


# ---------- 密钥 ----------
_REF = re.compile(r"\{(vault|env):([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve(v, missing: list) -> str:
    """vault:X / env:X（整个值）或字符串里的 {vault:X} / {env:X} 换成明文；取不到的记进 missing。"""
    v = str(v)
    if v.startswith(("vault:", "env:")):
        v = "{" + v + "}"

    def sub(m):
        val = vault.get(m[2]) if m[1] == "vault" else os.environ.get(m[2])
        if not val:
            missing.append(m[0][1:-1])
            return ""
        return val
    return _REF.sub(sub, v)


def _env(a: dict) -> dict:
    missing: list = []
    env = {k: _resolve(v, missing) for k, v in (a.get("env") or {}).items()}
    if missing:
        raise ValueError(i18n.t("缺少密钥：{names}", names=", ".join(missing)))
    return env


# ---------- OAuth ----------
class _Storage:
    """令牌和动态注册得到的 client 信息，整个存成保险库里的一条（加密）。
    client：用户自己建的 OAuth 客户端，不用注册，也不用存。"""

    def __init__(self, name: str, redirect: str, client: OAuthClientInformationFull | None = None):
        self.key, self.redirect, self.client = oauth_key(name), redirect, client

    def load(self) -> dict:
        raw = vault.get(self.key)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _put(self, **kv):
        vault.put(self.key, json.dumps(self.load() | kv))

    async def get_tokens(self):
        t = self.load().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens):
        exp = time.time() + tokens.expires_in if tokens.expires_in else None
        self._put(tokens=tokens.model_dump(mode="json", exclude_none=True), expires_at=exp)

    async def get_client_info(self):
        if self.client:
            return self.client
        c = self.load().get("client")
        if not c:
            return None
        info = OAuthClientInformationFull.model_validate(c)
        # 换了访问地址（回调地址跟着变）就重新注册
        if self.redirect and [str(u) for u in info.redirect_uris or []] != [self.redirect]:
            return None
        return info

    async def set_client_info(self, info):
        if not self.client:
            self._put(client=info.model_dump(mode="json", exclude_none=True))


async def _tidy_token(r: httpx2.Response) -> httpx2.Response:
    """Slack 的令牌接口出错也回 200（{"ok": false, "error": …}），令牌类型写的是 user，
    其实就是放进 Bearer 请求头的令牌。SDK 只认 Bearer，这里先整理一下。"""
    if r.status_code != 200:
        return r
    body = await r.aread()
    try:
        d = json.loads(body)
    except ValueError:
        return r
    if not isinstance(d, dict):
        return r
    if d.get("ok") is False:
        return httpx2.Response(400, content=body, request=r.request)
    if isinstance(d.get("token_type"), str) and d["token_type"].lower() != "bearer":
        return httpx2.Response(200, json=d | {"token_type": "Bearer"}, request=r.request)
    return r


class _Provider(OAuthClientProvider):
    def __init__(self, *args, scope: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.scope = scope
        prepare = self.context.prepare_token_auth

        def prepare_token_auth(data, headers=None):
            # SDK 用 Basic 头认证时，请求体里还留着 client_id。Notion 会当成两种认证方式，
            # 报 “Client must not use multiple authentication methods”。
            data, headers = prepare(data, headers)
            if headers.get("Authorization", "").startswith("Basic "):
                data = {k: v for k, v in data.items() if k != "client_id"}
            return data, headers

        self.context.prepare_token_auth = prepare_token_auth

    async def _perform_authorization(self):
        # SDK 会把 scope 换成对方公布的全部权限（Slack 有 30 个，连发消息都在里面），清单里写了就只要这些
        if self.scope:
            self.context.client_metadata.scope = self.scope
        return await super()._perform_authorization()

    async def _handle_token_response(self, response):
        return await super()._handle_token_response(await _tidy_token(response))

    async def _handle_refresh_response(self, response):
        return await super()._handle_refresh_response(await _tidy_token(response))

    async def _initialize(self):
        await super()._initialize()
        # SDK 不记令牌什么时候过期；从保险库恢复，这样重启后会先刷新令牌，而不是要用户重新登录
        exp = self.context.storage.load().get("expires_at")
        if self.context.current_tokens and exp:
            self.context.token_expiry_time = exp - 30

    async def _refresh_token(self):
        # SDK 只在完整授权时查令牌地址。重启以后不知道，就去猜 <域名>/token，
        # flomo 那里是 405，令牌一过期就得重新登录。刷新前先按授权时的办法查一遍。
        if not self.context.oauth_metadata:
            try:
                await self._discover()
            except Exception as e:
                log.warning("查找 %s 的令牌地址失败：%s", self.context.server_url, e)
        return await super()._refresh_token()

    async def _discover(self):
        """和 SDK 授权流程里一样：先查受保护资源的元数据，找到授权服务器，再查它的元数据。"""
        ctx = self.context
        async with httpx2.AsyncClient(timeout=15) as c:
            for url in _auth_utils.build_protected_resource_metadata_discovery_urls(None, ctx.server_url):
                r = await c.send(_auth_utils.create_oauth_metadata_request(url))
                prm = await _auth_utils.handle_protected_resource_response(r)
                if prm:
                    await self._validate_resource_match(prm)
                    ctx.protected_resource_metadata = prm
                    ctx.auth_server_url = self._select_authorization_server([str(u) for u in prm.authorization_servers])
                    break
            expected = self._expected_issuer()
            # 授权服务器换了：令牌是原来那家发的，不能拿去新地址刷新，交给 SDK 走完整授权
            if ctx.client_info and not _auth_utils.credentials_match_issuer(
                    ctx.client_info, expected, ctx.client_metadata_url):
                return
            for url in _auth_utils.build_oauth_authorization_server_metadata_discovery_urls(
                    ctx.auth_server_url, ctx.server_url):
                r = await c.send(_auth_utils.create_oauth_metadata_request(url))
                ok, asm = await _auth_utils.handle_auth_metadata_response(r)
                if not ok:
                    return
                if asm:
                    if ctx.auth_server_url is None and _auth_utils.issuers_match(str(asm.issuer), expected):
                        expected = str(asm.issuer)
                    _oauth2.validate_metadata_issuer(asm, expected)
                    ctx.oauth_metadata = asm
                    return


# 正在进行的授权：应用名 -> {"url": 授权页地址, "code": 回调带回来的授权码, "task": 连接任务}
_login: dict[str, dict] = {}
_states: dict[str, str] = {}     # OAuth state -> 应用名


def _fail(fut: asyncio.Future, e: Exception):
    """让等着授权码的一方收到异常。没人等时也不在日志里报“异常没人取”。"""
    if not fut.done():
        fut.set_exception(e)
        fut.exception()


def has_tokens(name: str) -> bool:
    return bool(_Storage(name, "").load().get("tokens"))


def _oauth(name: str, a: dict, url: str) -> _Provider:
    redirect = a.get("oauth_redirect") or ""
    redirect_uris = [redirect or "http://127.0.0.1/oauth/callback"]
    o = a["oauth"] if isinstance(a.get("oauth"), dict) else {}
    client = None
    if o.get("client_id"):
        missing: list = []
        client_id = _resolve(o["client_id"], missing)
        secret = _resolve(o["client_secret"], missing) if o.get("client_secret") else None
        if missing:
            raise ValueError(i18n.t("缺少密钥：{names}", names=", ".join(missing)))
        client = OAuthClientInformationFull(
            client_id=client_id, client_secret=secret, redirect_uris=redirect_uris,
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="client_secret_post" if secret else "none")

    async def on_redirect(auth_url: str):
        w = _login.get(name)
        if not w or w["url"].done():
            _needs_login.add(name)
            raise NeedsLogin(i18n.t("需要重新登录授权"))
        head, sep, query = auth_url.partition("?")
        query = query.replace("?", "&")                    # 授权地址本身带参数时（腾讯文档），SDK 会拼出两个 ?
        if o.get("params"):                                # 比如 Google 要 access_type=offline 才给刷新令牌
            query = urlencode(dict(parse_qsl(query, keep_blank_values=True)) | o["params"])
        auth_url = head + sep + query
        state_ = parse_qs(urlparse(auth_url).query).get("state", [""])[0]
        _states[state_] = name
        w["state"] = state_
        status[name] = i18n.later("等你在弹出的窗口里登录授权…")
        w["url"].set_result(auth_url)
        await _changed()

    async def on_callback() -> AuthorizationCodeResult:
        w = _login[name]
        try:
            return await asyncio.wait_for(asyncio.shield(w["code"]), LOGIN_WAIT_S)
        except asyncio.TimeoutError:
            _needs_login.add(name)
            raise NeedsLogin(i18n.t("没有完成授权"))
        finally:
            _states.pop(w.get("state", ""), None)

    meta = OAuthClientMetadata(client_name="Open Super Agent", redirect_uris=redirect_uris,
                               grant_types=["authorization_code", "refresh_token"], response_types=["code"])
    return _Provider(url, meta, _Storage(name, redirect, client), on_redirect, on_callback, scope=o.get("scope"))


async def login(name: str, redirect: str) -> tuple[str, str]:
    """开始授权。返回 ("url", 授权页地址)；不需要登录就连上了返回 ("done", 状态)。"""
    if not get(name):
        raise ValueError(i18n.t("没有应用 {name}", name=name))
    save(name, {"oauth_redirect": redirect, "enabled": True})
    # 从头授权：旧令牌已经不能用了，动态注册的 client 也可能被对方清掉了，一起重新来
    await disconnect(name)
    vault.delete(oauth_key(name))
    loop = asyncio.get_running_loop()
    w = {"url": loop.create_future(), "code": loop.create_future()}
    _login[name] = w
    _needs_login.discard(name)
    w["task"] = asyncio.create_task(connect(name, timeout=LOGIN_WAIT_S + 60))
    await asyncio.wait([w["url"], w["task"]], timeout=60, return_when=asyncio.FIRST_COMPLETED)
    if w["url"].done():
        return "url", w["url"].result()
    if w["task"].done():
        return "done", str(status.get(name, ""))
    await disconnect(name)
    status[name] = i18n.later("打不开授权页：应用没有响应")
    return "done", str(status[name])


async def finish_login(state_: str, code: str, iss: str | None, error: str) -> str | None:
    """OAuth 回调：把授权码交给等着的连接，等它连上。返回应用名。"""
    name = _states.get(state_)
    w = _login.get(name or "")
    if not name or not w or w["code"].done():
        return None
    if error:
        _fail(w["code"], NeedsLogin(i18n.t("授权没有完成：{error}", error=error)))
    else:
        w["code"].set_result(AuthorizationCodeResult(code=code, state=state_, iss=iss or None))
    status[name] = i18n.later("已授权，正在连接…")
    await _changed()
    try:
        await asyncio.wait_for(asyncio.shield(w["task"]), 60)
    except asyncio.TimeoutError:
        pass
    return name


async def cancel_login(name: str):
    w = _login.get(name)
    if w:
        _fail(w["code"], NeedsLogin(i18n.t("取消了授权")))


# ---------- 连接 ----------
def _text(result) -> str:
    parts = [getattr(c, "text", None) or str(c) for c in result.content]
    out = "\n".join(parts)
    is_error = getattr(result, "is_error", None) or getattr(result, "isError", False)  # 兼容新旧 SDK 字段名
    return ("工具报错：" + out) if is_error else out


def _needs_confirm(name: str, a: dict, tool) -> bool:
    from .approvals import default_confirm
    return default_confirm(a.get("confirm", "auto"), tool)


async def _changed():
    if notify:
        await notify({"type": "apps_changed"})


def _lost(e: Exception) -> bool:
    """是不是连接本身断了（而不是工具报错）。"""
    if isinstance(e, MCPError):
        return e.error.code == CONNECTION_CLOSED or "Session terminated" in str(e)
    return True


def _first(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def _transport(name: str, a: dict):
    """返回 (transport 上下文, 要一起关的 http client 或 None)。"""
    security.check_app(name, a)   # 服务器锁、演示模式：不管配置是怎么进的数据库（界面、恢复备份），连之前都查一遍
    if a.get("type") in ("http", "sse"):
        missing: list = []
        url = _resolve(a["url"], missing)
        headers = {k: _resolve(v, missing) for k, v in (a.get("headers") or {}).items()}
        if missing:
            raise ValueError(i18n.t("缺少密钥：{names}", names=", ".join(missing)))
        auth = _oauth(name, a, url) if a.get("oauth") else None
        if a["type"] == "sse":
            # 事件流闲着时不断开（默认 5 分钟没消息就断）；真断了，下次调用工具时会重连
            return sse_client(url, headers=headers or None, auth=auth, sse_read_timeout=24 * 3600), None
        client = create_mcp_http_client(headers=headers or None, auth=auth)
        return streamable_http_client(url, http_client=client), client
    # 登录密码不交给第三方应用
    env = {**{k: v for k, v in os.environ.items() if k != "OSA_PASSWORD"}, **_env(a)}
    missing = []
    args = [_resolve(x, missing) if "vault:" in str(x) or "env:" in str(x) else str(x) for x in a.get("args", [])]
    if missing:
        raise ValueError(i18n.t("缺少密钥：{names}", names=", ".join(missing)))
    if name == "filesystem" or "server-filesystem" in " ".join(args):
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    return stdio_client(StdioServerParameters(command=a["command"], args=args, env=env)), None


async def _serve(name: str, a: dict, ready: asyncio.Future, stop: asyncio.Event, wait_s: float):
    """一个应用的整个生命周期：连上、登记工具、等停用信号、清理。"""
    connected = ""   # 连上时的状态说明
    try:
        transport, client = _transport(name, a)
        async with contextlib.AsyncExitStack() as stack:
            if client:
                await stack.enter_async_context(client)
            read, write = await stack.enter_async_context(transport)
            # 告诉应用我们能显示它自带的界面（MCP Apps）；有的应用看到这个才登记带界面的工具
            session = await stack.enter_async_context(ClientSession(read, write, extensions=mcpapps.CAPABILITY))
            await asyncio.wait_for(session.initialize(), wait_s)
            tools = (await session.list_tools()).tools
            deny = [w.lower() for w in a.get("deny", [])]
            _drop_tools(name)   # 重连时先撤掉上一次注册的
            ctx = _ctx[name] = registry.Context(f"mcp:{name}")
            _sessions[name] = session
            ctx.effect(lambda: _sessions.pop(name, None) if _sessions.get(name) is session else None)
            ctx.effect(lambda: mcpapps.forget(name))
            n = c = 0
            for t in tools:
                if any(w in t.name.lower() for w in deny):
                    continue
                full = f"{name}__{t.name}"[:64]

                ui, vis = mcpapps.tool_ui(getattr(t, "meta", None))

                async def run(c: registry.Call, _tool=t.name, _full=full, _ui=ui):
                    try:
                        result = await session.call_tool(_tool, c.args)
                    except Exception as e:
                        # 连接断了（远程会话过期、进程退出）：重连一次再试
                        if not _lost(e) or stop.is_set() or time.time() - _reconnected.get(name, 0) < 60:
                            raise
                        _reconnected[name] = time.time()
                        await connect(name)
                        again = registry.get(_full)
                        if not again or again.owner != f"mcp:{name}":
                            hint = "，请用户到应用市场重新登录" if name in _needs_login else ""   # 写给模型看的
                            raise RuntimeError(f"应用 @{name} 连接断开：{status.get(name, '')}{hint}") from e
                        return await again.run(c)
                    text = _text(result)
                    # 带界面的工具：后台任务（不是 Heavy 的草稿）把结果记下来，任务结果下面用应用的界面显示
                    if _ui and c.scope == registry.WORKER and c.env.get("pages", True) and len(c.apps) < mcpapps.MAX_VIEWS:
                        if rec := mcpapps.view_record(name, _tool, _ui, c.args, result):
                            c.apps.append(rec)
                            text += mcpapps.MODEL_HINT
                    return text

                params = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
                # visibility 里没有 model 的只给自己的界面调，不给模型、也不给小应用
                ctx.tool(registry.Tool(full, {"type": "function", "function": {
                    "name": full, "description": (t.description or "")[:1000], "parameters": params}}, run,
                    where=frozenset({registry.WORKER, registry.APPLET}) if "model" in vis else frozenset(),
                    gated=True, group=name,
                    meta={"mcp": {"tool": t.name, "ui": ui, "visibility": vis, "description": t.description or "",
                                  "inputSchema": params}}))
                if _needs_confirm(name, a, t):
                    CONFIRM.add(full)
                    c += 1
                n += 1
            status[name] = connected = i18n.later("已连接，{n} 个工具（{c} 个写操作默认要确认）", n=n, c=c)
            _connected.add(name)
            _needs_login.discard(name)
            if not ready.done():
                ready.set_result(None)
            await _changed()
            await stop.wait()
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError) and stop.is_set():
            raise
        e = _first(e)
        if isinstance(e, NeedsLogin):
            _needs_login.add(name)
            msg = str(e)
        elif isinstance(e, asyncio.TimeoutError):
            msg = i18n.later("连接超时")
        elif "Token exchange failed" in str(e):      # 自己建的客户端最常见：密钥或回调地址填错
            msg = i18n.later("登录后没换到令牌，检查一下客户端 ID、密钥和回调地址：{error}", error=str(e).split(": ", 1)[-1][:200])
        else:
            msg = i18n.later("连接失败：{error}", error=(str(e) or type(e).__name__)[:300])
        status[name] = msg
        if not ready.done():
            ready.set_exception(e if isinstance(e, Exception) else RuntimeError(msg))
        if not isinstance(e, Exception):
            raise
    finally:
        _drop_tools(name)
        _connected.discard(name)
        if _running.get(name, (None,))[0] is asyncio.current_task():
            _running.pop(name, None)
        if connected and status.get(name) == connected:   # 连上过、状态还没被别的说明盖掉：停用了
            status[name] = i18n.later("已停用")
        await _changed()


def _drop_tools(name: str):
    """应用断开：它注册的工具全部撤销（Context.dispose 反向撤销），确认集合里的也清掉。"""
    if c := _ctx.pop(name, None):
        c.dispose()
    for k in [k for k in CONFIRM if k.startswith(name + "__")]:
        CONFIRM.discard(k)


def tools_of(name: str) -> list[str]:
    """一个应用现在注册着的工具名（不带应用前缀）。"""
    return sorted(t.name.split("__", 1)[1] for t in registry.by_group().get(name, []))


def all_tool_names() -> list[str]:
    return sorted(t.name for ts in registry.by_group().values() for t in ts)


async def connect(name: str, timeout: float = 150) -> str:
    a = get(name)
    if not a:
        raise ValueError(i18n.t("没有应用 {name}", name=name))
    await disconnect(name, keep_login=True)
    interactive = name in _login and not _login[name]["url"].done()
    if a.get("oauth") and not interactive and not has_tokens(name):
        _needs_login.add(name)
        status[name] = i18n.later("还没有登录授权")
        await _changed()
        return str(status[name])
    status[name] = i18n.later("正在连接…")
    await _changed()
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
    task = asyncio.create_task(_serve(name, a, ready, stop, timeout - 10 if interactive else 120))
    _running[name] = (task, stop)
    try:
        await asyncio.wait_for(asyncio.shield(ready), timeout)
    except asyncio.TimeoutError:
        status[name] = i18n.later("连接超时")
        await disconnect(name)
    except Exception:
        pass
    return str(status[name])


async def disconnect(name: str, keep_login: bool = False):
    if not keep_login:
        await cancel_login(name)
    item = _running.pop(name, None)
    if not item:
        return
    task, stop = item
    stop.set()
    try:
        await asyncio.wait_for(task, 10)
    except (asyncio.TimeoutError, Exception):
        task.cancel()
    _drop_tools(name)
    _connected.discard(name)


async def start():
    _seed()
    for name, a in all_apps().items():
        if a["enabled"]:
            asyncio.create_task(connect(name))   # 不阻塞服务启动；npx 第一次下载可能要一会儿
        else:
            status[name] = i18n.later("未启用")


async def stop():
    for name in list(_running):
        await disconnect(name)
