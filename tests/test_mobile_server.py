"""手机 App 要用的服务端功能：扫码配对的登录票、推送里的待确认数和 Topic、失效订阅的清理。"""
import json
import os
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from osa import approvals, auth, push, server
from conftest import until
from test_auth import _call, _scope


# ---------- 扫码配对 ----------
def _logged_in() -> str:
    return f"{auth.COOKIE}={auth.new_token()}"


async def _pair(cookie=None, origin=None) -> tuple[int, dict]:
    sent = await _call(server.app, _scope(method="POST", path="/api/pair", cookie=cookie, origin=origin))
    body = b"".join(m.get("body", b"") for m in sent[1:])
    return sent[0]["status"], json.loads(body or b"{}")


async def _use(ticket: str) -> dict:
    sent = await _call(server.app, _scope(path="/login/ticket", query=f"t={ticket}&next=/"))
    return {k.decode(): v.decode() for k, v in sent[0]["headers"]}


async def test_pair_ticket_logs_the_phone_in_once(password):
    assert (await _pair())[0] == 401                                     # 要先登录
    status, body = await _pair(_logged_in())
    assert status == 200 and body["expires_in"] == auth.PAIR_TTL == 300
    assert auth._tickets[body["ticket"]] == pytest.approx(time.time() + 300, abs=5)

    head = await _use(body["ticket"])
    assert head["location"] == "/" and head["set-cookie"].startswith(auth.COOKIE + "=")
    assert "set-cookie" not in await _use(body["ticket"])                # 只能用一次


async def test_pair_ticket_expires(password, monkeypatch):
    _, body = await _pair(_logged_in())
    monkeypatch.setitem(auth._tickets, body["ticket"], time.time() - 1)
    assert (await _use(body["ticket"]))["location"].startswith("/login")


async def test_other_sites_cannot_get_a_pair_ticket(password):
    assert (await _pair(_logged_in(), origin="https://evil.example"))[0] == 403


async def test_pair_without_login(no_password):
    assert await _pair() == (200, {"ticket": "", "expires_in": 0})


# ---------- 推送 ----------
def _decrypt(body: bytes, priv: ec.EllipticCurvePrivateKey, secret: bytes) -> dict:
    """RFC 8291 的接收方：手机 App 的通知扩展做的就是这件事。"""
    salt, idlen = body[:16], body[20]
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = HKDF(hashes.SHA256(), 32, secret, b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, ct, None).rstrip(b"\x00")
    assert plain.endswith(b"\x02")                                       # 最后一条记录的分隔符
    return json.loads(plain[:-1])


@pytest.fixture
def device(monkeypatch):
    """一台订阅了推送的设备，推送服务是假的：收到的请求记在 seen 里，回什么由 status 决定。"""
    priv, secret = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ep = "https://push.example/w/" + push.b64u(os.urandom(16))
    push.subscribe({"endpoint": ep, "keys": {"p256dh": push.b64u(pub), "auth": push.b64u(secret)}})
    d = {"endpoint": ep, "priv": priv, "secret": secret, "seen": [], "status": 201}

    def handle(req: httpx.Request) -> httpx.Response:
        d["seen"].append(req)
        return httpx.Response(d["status"])
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(handle))
    others = [r[0] for r in push.conn.execute("select endpoint from push_subs where endpoint != ?", (ep,))]
    for e in others:                                                     # 只留这一台，别的测试留下的不算
        push.unsubscribe(e)
    yield d
    push.unsubscribe(ep)


async def test_push_carries_badge_topic_and_short_title(device):
    assert await push.send("长" * 100, "正文", "/?chat=3", "chat-3", badge=2) == ["ok"]
    req = device["seen"][0]
    assert str(req.url) == device["endpoint"]
    assert req.headers["content-encoding"] == "aes128gcm" and req.headers["authorization"].startswith("vapid t=")
    assert req.headers["topic"] == push.topic("chat-3") and len(req.headers["topic"]) <= 32
    assert "chat-3" not in req.headers["topic"]
    data = _decrypt(req.content, device["priv"], device["secret"])
    assert data == {"title": "长" * 80, "body": "正文", "url": "/?chat=3", "tag": "chat-3", "badge": 2}


async def test_push_without_tag_or_badge(device):
    await push.send("t", "b")
    req = device["seen"][0]
    assert "topic" not in req.headers
    assert "badge" not in _decrypt(req.content, device["priv"], device["secret"])


async def test_gone_subscription_is_removed(device):
    device["status"] = 410
    await push.send("t", "b")
    assert push.count() == 0


async def test_approval_push_counts_pending(device, monkeypatch):
    calls = []

    async def fake_send(*a, **kw):
        calls.append((a, kw))
    monkeypatch.setattr(push, "send", fake_send)
    monkeypatch.setattr(approvals, "pending", {7: {}, 8: {}})
    monkeypatch.setattr(server, "clients", set())                        # 没有页面开着
    await server.broadcast({"type": "approval", "status": "pending", "id": 8, "chat_id": 1, "tool": "send_mail", "goal": "发邮件"})
    await until(lambda: calls, what="推送")
    (args, kw), = calls
    assert args[3] == "approval-8" and kw == {"urgency": "high", "badge": 2}


# ---------- 升级后不显示旧界面 ----------
async def test_index_is_revalidated(no_password):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1:8765") as c:
        for path in ("/", "/library/pages/3"):
            r = await c.get(path)
            assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache", path
