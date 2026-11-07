"""手机 App 的推送中转：服务器照常发网页推送，中转验签名后转给（假的）苹果推送服务，App 能解开内容。"""
import base64
import json
import os
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from osa import push, relay
from test_mobile_server import _decrypt

ORIGIN = "https://relay.test"
TOKEN = "ab" * 32


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


class FakeApns:
    """记下中转发来的请求；replies 里按顺序给出 (状态码, 原因)，用完了就回 200。"""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.replies: list[tuple[int, str]] = []
        self.key: ec.EllipticCurvePrivateKey | None = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        status, reason = self.replies.pop(0) if self.replies else (200, "")
        return httpx.Response(status, json={"reason": reason} if reason else None)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    apns_key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "AuthKey.p8").write_bytes(apns_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    f = FakeApns()
    f.key = apns_key
    monkeypatch.setattr(relay, "ORIGIN", ORIGIN)
    monkeypatch.setattr(relay, "DB_PATH", str(tmp_path / "relay.db"))
    monkeypatch.setattr(relay, "_db", None)
    monkeypatch.setattr(relay, "KEY_FILE", str(tmp_path / "AuthKey.p8"))
    monkeypatch.setattr(relay, "KEY_ID", "KEY123")
    monkeypatch.setattr(relay, "_transport", httpx.MockTransport(f))
    monkeypatch.setattr(relay, "apns", relay.Apns())
    monkeypatch.setattr(relay, "_limits", relay.Limiter())
    # 服务器（osa/push.py）发推送时直接打到中转上
    monkeypatch.setattr(push, "_transport", httpx.ASGITransport(app=relay.app))
    yield f
    if relay._db:
        relay._db.close()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=relay.app), base_url=ORIGIN)


async def _register(vapid: str | None = None, token: str = TOKEN, env: str = "production") -> dict:
    async with client() as c:
        r = await c.post("/v1/endpoints", json={"token": token, "env": env, "vapid": vapid or push.public_key()})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def phone(fake):
    """一台登记过、又在服务器上订阅了推送的手机：手机自己的密钥只有它和服务器知道，中转不知道。"""
    reg = await _register()
    priv, secret = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    for (e,) in push.conn.execute("select endpoint from push_subs").fetchall():
        push.unsubscribe(e)
    push.subscribe({"endpoint": reg["endpoint"], "keys": {"p256dh": push.b64u(_point(priv)), "auth": push.b64u(secret)}})
    yield {**reg, "priv": priv, "secret": secret}
    push.unsubscribe(reg["endpoint"])


async def test_push_reaches_the_phone_and_only_the_phone_can_read_it(fake, phone):
    assert await push.send("小助手 需要你确认", "send_mail：发邮件", "/?chat=4&thread=9", "approval-3", urgency="high", badge=1) == ["ok"]
    req, = fake.seen
    assert str(req.url) == f"https://api.push.apple.com/3/device/{TOKEN}"
    h = req.headers
    assert h["apns-push-type"] == "alert" and h["apns-topic"] == relay.TOPIC and h["apns-priority"] == "10"
    assert h["apns-collapse-id"] == push.topic("approval-3")
    assert int(h["apns-expiration"]) == pytest.approx(time.time() + 86400, abs=10)

    # 给苹果的令牌：用我们的推送密钥签的
    jwt = h["authorization"].removeprefix("bearer ")
    head_b64, claims_b64, sig_b64 = jwt.split(".")
    assert json.loads(relay.unb64u(head_b64)) == {"alg": "ES256", "kid": "KEY123"}
    assert json.loads(relay.unb64u(claims_b64))["iss"] == relay.TEAM_ID
    sig = relay.unb64u(sig_b64)
    fake.key.public_key().verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                                 f"{head_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))

    body = json.loads(req.content)
    assert body["aps"] == {"alert": {"loc-key": "PUSH_PLACEHOLDER"}, "mutable-content": 1, "sound": "default"}
    assert body["s"] == phone["id"]
    assert "邮件" not in req.content.decode()                          # 中转和苹果只看到密文
    data = _decrypt(base64.b64decode(body["e"]), phone["priv"], phone["secret"])
    assert data == {"title": "小助手 需要你确认", "body": "send_mail：发邮件", "url": "/?chat=4&thread=9", "tag": "approval-3", "badge": 1}


async def test_low_urgency_and_no_topic(fake, phone):
    await push.send("t", "b", urgency="low")
    h = fake.seen[0].headers
    assert h["apns-priority"] == "5" and "apns-collapse-id" not in h


async def test_gone_phone_unsubscribes_on_the_server(fake, phone):
    fake.replies = [(410, "Unregistered")]
    await push.send("t", "b")
    assert push.count() == 0                                            # 中转回 410，服务器删掉订阅
    assert relay.db().execute("select count(*) from endpoints").fetchone()[0] == 0


async def test_dev_build_token_falls_back_to_sandbox(fake, phone):
    fake.replies = [(400, "BadDeviceToken")]
    assert await push.send("t", "b") == ["ok"]
    assert [r.url.host for r in fake.seen] == ["api.push.apple.com", "api.sandbox.push.apple.com"]
    assert relay.db().execute("select env from endpoints").fetchone()[0] == "sandbox"


async def test_expired_provider_token_is_refreshed(fake, phone):
    fake.replies = [(403, "ExpiredProviderToken")]
    assert await push.send("t", "b") == ["ok"]
    assert len(fake.seen) == 2


async def test_apns_outage_is_reported(fake, phone):
    fake.replies = [(503, "ServiceUnavailable")]
    out, = await push.send("t", "b")
    assert "502" in out and push.count() == 1                           # 临时故障：不删订阅


async def test_too_many_pushes(fake, phone, monkeypatch):
    monkeypatch.setattr(relay, "PER_MINUTE", 2)
    results = [r for _ in range(3) for r in await push.send("t", "b")]
    assert results[:2] == ["ok", "ok"] and "429" in results[2]


# ---------- 别人冒充服务器 ----------
def _vapid(key: ec.EllipticCurvePrivateKey, **claims) -> str:
    claims = {"aud": ORIGIN, "exp": int(time.time()) + 3600, "sub": "mailto:x@example.com", **claims}
    head = push.b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    body = push.b64u(json.dumps(claims).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"vapid t={head}.{body}.{push.b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={push.b64u(_point(key))}"


async def _post(eid: str, auth: str, body: bytes | None = None, **headers) -> httpx.Response:
    body = body if body is not None else push.encrypt(b'{"title":"x"}', push.b64u(_point(ec.generate_private_key(ec.SECP256R1()))),
                                                     push.b64u(os.urandom(16)))
    h = {"authorization": auth, "content-encoding": "aes128gcm", "ttl": "60", **headers}
    async with client() as c:
        return await c.post(f"/w/{eid}", content=body, headers={k: v for k, v in h.items() if v is not None})


async def test_only_the_registered_server_can_push(fake):
    server = ec.generate_private_key(ec.SECP256R1())
    eid = (await _register(vapid=push.b64u(_point(server))))["id"]
    assert (await _post(eid, _vapid(server))).status_code == 201
    other = ec.generate_private_key(ec.SECP256R1())
    assert (await _post(eid, _vapid(other))).status_code == 403                       # 别的服务器的密钥
    assert (await _post(eid, _vapid(server, aud="https://evil.example"))).status_code == 401
    assert (await _post(eid, _vapid(server, exp=int(time.time()) - 10))).status_code == 401
    assert (await _post(eid, _vapid(server, exp=int(time.time()) + 3 * 86400))).status_code == 401
    assert (await _post(eid, _vapid(server, sub=""))).status_code == 401
    forged = _vapid(server).replace("vapid t=", "vapid t=x")
    assert (await _post(eid, forged)).status_code == 401
    k = push.b64u(_point(server))
    sig_swapped = _vapid(other).split(", k=")[0] + f", k={k}"                          # 别人签的，冒用这台服务器的公钥
    assert (await _post(eid, sig_swapped)).status_code == 401
    assert (await _post(eid, "")).status_code == 401
    assert (await _post("nope", _vapid(server))).status_code == 404
    assert len(fake.seen) == 1                                                          # 只有第一条到了苹果


async def test_bad_requests(fake):
    server = ec.generate_private_key(ec.SECP256R1())
    eid = (await _register(vapid=push.b64u(_point(server))))["id"]
    assert (await _post(eid, _vapid(server), ttl=None)).status_code == 400
    assert (await _post(eid, _vapid(server), **{"content-encoding": "aesgcm"})).status_code == 400
    assert (await _post(eid, _vapid(server), body=b"x" * 50)).status_code == 400
    assert (await _post(eid, _vapid(server), body=os.urandom(16) + b"\0\0\x10\0" + bytes([65]) + os.urandom(3000))).status_code == 413
    assert fake.seen == []


# ---------- 登记 ----------
async def test_registration_checks_input(fake):
    async with client() as c:
        for bad in [{"token": "xyz", "env": "production", "vapid": push.public_key()},
                    {"token": TOKEN, "env": "staging", "vapid": push.public_key()},
                    {"token": TOKEN, "env": "production", "vapid": "AAAA"},
                    ["not", "an", "object"]]:
            assert (await c.post("/v1/endpoints", json=bad)).status_code == 400


async def test_update_and_delete_need_the_secret(fake):
    reg = await _register()
    url = f"/v1/endpoints/{reg['id']}"
    async with client() as c:
        assert (await c.put(url, json={"token": "cd" * 32})).status_code == 401
        assert (await c.put(url, json={"token": "cd" * 32}, headers={"authorization": "Bearer wrong"})).status_code == 401
        assert (await c.delete(url, headers={"authorization": "Bearer wrong"})).status_code == 401
        ok = {"authorization": f"Bearer {reg['secret']}"}
        r = await c.put(url, json={"token": "cd" * 32, "env": "sandbox"}, headers=ok)
        assert r.status_code == 200 and r.json()["endpoint"] == reg["endpoint"]
        assert relay.db().execute("select token, env from endpoints").fetchone() == ("cd" * 32, "sandbox")
        assert (await c.put("/v1/endpoints/unknown", json={}, headers=ok)).status_code == 404   # App 据此重新登记
        assert (await c.delete(url, headers=ok)).status_code == 204
        assert relay.db().execute("select count(*) from endpoints").fetchone()[0] == 0


async def test_registration_limits(fake, monkeypatch):
    monkeypatch.setattr(relay, "MAX_PER_TOKEN", 2)
    await _register(); await _register()
    async with client() as c:
        r = await c.post("/v1/endpoints", json={"token": TOKEN, "env": "production", "vapid": push.public_key()})
    assert r.status_code == 429


async def test_stale_endpoints_are_cleaned(fake):
    old = await _register()
    relay.db().execute("update endpoints set created=?, last_ok=? where id=?", (time.time() - 100 * 86400, 0, old["id"]))
    await _register(token="ef" * 32)
    ids = [r[0] for r in relay.db().execute("select id from endpoints")]
    assert old["id"] not in ids and len(ids) == 1


async def test_health(fake):
    async with client() as c:
        r = await c.get("/healthz")
    assert r.json()["ok"] and r.json()["apns"]


async def test_missing_apns_key_is_a_clean_error(fake, phone, monkeypatch):
    monkeypatch.setattr(relay, "KEY_FILE", "/nonexistent/AuthKey.p8")
    out, = await push.send("t", "b")
    assert "502" in out and "not configured" in out and push.count() == 1
