"""别的网站不能借你的浏览器连本机的服务：开没开登录都查 Origin；没开登录时还查 Host（防 DNS rebinding）。"""
import pytest

from osa import auth, config, server

LOCAL = "127.0.0.1:8765"


def _scope(kind="http", method="GET", path="/api/me", host=LOCAL, origin=None, cookie=None, query="", extra=()) -> dict:
    headers = [(b"host", host.encode())] if host else []
    if origin:
        headers.append((b"origin", origin.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    headers += [(k.encode(), v.encode()) for k, v in extra]
    scope = {"type": kind, "path": path, "raw_path": path.encode(), "headers": headers, "query_string": query.encode(),
             "scheme": "ws" if kind == "websocket" else "http", "root_path": "", "http_version": "1.1",
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765)}
    if kind == "http":
        scope["method"] = method
    return scope


async def _call(app, scope) -> list[dict]:
    sent = []

    async def receive():
        if scope["type"] == "websocket":
            return {"type": "websocket.connect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)
    await app(scope, receive, send)
    return sent


async def _reach(scope, receive, send):
    """代替真正的接口：走到这里就是放行了。"""
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
    else:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

APP = auth.AuthMiddleware(_reach)


async def outcome(**kw):
    first = (await _call(APP, _scope(**kw)))[0]
    return {"websocket.accept": "accept", "websocket.close": "close"}.get(first["type"]) or first["status"]


async def test_other_sites_cannot_use_the_websocket(no_password):
    assert await outcome(kind="websocket", path="/ws", origin="https://evil.example") == "close"
    assert await outcome(kind="websocket", path="/ws", origin="null") == "close"
    assert await outcome(kind="websocket", path="/ws", origin=f"http://{LOCAL}") == "accept"
    assert await outcome(kind="websocket", path="/ws") == "accept"                       # 不是浏览器：没有 Origin


async def test_other_sites_cannot_post(no_password):
    assert await outcome(method="POST", path="/api/bots", origin="https://evil.example") == 403
    assert await outcome(method="POST", path="/api/bots", origin=f"http://{LOCAL}") == 200
    assert await outcome(method="POST", path="/api/bots") == 200                         # curl、脚本
    assert await outcome(method="GET", path="/api/me", origin="https://evil.example") == 200   # 读不到内容，由浏览器的同源策略管
    # webhook：浏览器跨站发来的也挡住；真正的 webhook 不带 Origin
    assert await outcome(method="POST", path="/hooks/github", origin="https://evil.example") == 403
    assert await outcome(method="POST", path="/hooks/github") == 200


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "192.168.1.5:8765", "100.64.1.2",
                                  "mm.tail1234.ts.net", "mm.tail1234.ts.net:8443", "mac-mini.local:8765", "osa.localhost:5173", None])
async def test_local_addresses_are_accepted(no_password, host):
    assert await outcome(host=host) == 200


async def test_other_domains_are_refused_without_password(no_password):
    # DNS rebinding：evil.example 解析到 127.0.0.1 后，浏览器认为同源，带的 Host 和 Origin 都是 evil.example
    assert await outcome(kind="websocket", path="/ws", host="evil.example:8765", origin="http://evil.example:8765") == "close"
    assert await outcome(path="/api/bootstrap", host="evil.example:8765") == 403
    page = await _call(APP, _scope(path="/", host="evil.example:8765"))
    assert page[0]["status"] == 403 and (b"content-type", b"text/plain; charset=utf-8") in page[0]["headers"]
    assert b"server.allowed_hosts" in page[1]["body"] and b"evil.example" in page[1]["body"]


async def test_allowed_hosts(no_password, monkeypatch):
    monkeypatch.setitem(config.CFG, "server", {"allowed_hosts": ["*.example.com"]})
    monkeypatch.setenv("OSA_ALLOWED_HOSTS", "box.lan, nas.lan")
    assert await outcome(host="osa.example.com:8765") == 200
    assert await outcome(host="nas.lan") == 200
    assert await outcome(host="other.lan") == 403
    assert await outcome(host="example.com.evil.example") == 403


async def test_webhook_with_token_works_from_any_address(no_password, monkeypatch):
    assert await outcome(method="POST", path="/hooks/github", host="hooks.example.org") == 403
    # 事件自己的地址（随机密钥）不用 token 也能从外面调：不知道密钥凑不出这个地址
    assert await outcome(method="POST", path="/hooks/QmcLDBjvjgeasnvh3GYWVF_w", host="hooks.example.org") == 200
    assert await outcome(method="POST", path="/hooks/QmcLDBjvjgeasnvh3GYWVF_w", origin="https://evil.example") == 403
    monkeypatch.setenv("OSA_WEBHOOK_TOKEN", "t")                                         # token 由接口自己检查
    assert await outcome(method="POST", path="/hooks/github", host="hooks.example.org") == 200
    assert await outcome(path="/api/bootstrap", host="hooks.example.org") == 403


async def test_with_password_behaviour_is_unchanged(password):
    cookie = f"{auth.COOKIE}={auth.new_token()}"
    assert await outcome(kind="websocket", path="/ws", origin="https://evil.example", cookie=cookie) == "close"
    assert await outcome(kind="websocket", path="/ws", origin=f"http://{LOCAL}") == "close"   # 没登录
    assert await outcome(kind="websocket", path="/ws", origin=f"http://{LOCAL}", cookie=cookie) == "accept"
    assert await outcome(path="/api/me") == 401
    assert await outcome(path="/api/me", host="osa.example.com", cookie=cookie) == 200   # 开了登录就不查 Host
    assert await outcome(method="POST", path="/hooks/github", host="hooks.example.org") == 200


async def test_server_app_uses_the_checks(no_password):
    closed = await _call(server.app, _scope(kind="websocket", path="/ws", origin="https://evil.example"))
    assert closed[0] == {"type": "websocket.close", "code": 1008}
    assert (await _call(server.app, _scope(path="/api/me", host="evil.example")))[0]["status"] == 403
    assert (await _call(server.app, _scope(path="/api/me")))[0]["status"] == 200
