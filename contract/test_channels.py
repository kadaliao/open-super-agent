"""聊天软件（假的 Telegram Bot API）和手机推送（假的推送服务，HTTPS 自签证书）。

- 添加连接、配对码、配对成功后在“Telegram”里和 Bot 说话，回复发回 Telegram；
- 确认请求变成带按钮的消息，点按钮（callback_query）就允许，卡片改成结果；/new；停用、删除；
- 没有页面正被看着时推送到订阅的设备，内容用测试自己的私钥解得开；404 / 410 的订阅删掉。
- 只有 Rust 版有的：任务提问卡片、重启时说不准那一步的卡片，在 Telegram、飞书（假的长连接）、钉钉（假的 Stream）里
  点按钮、引用卡片回复、打字回复、直接说（交给 Bot 判断是不是在回答），以及这两种卡片的推送。
"""
import base64
import datetime
import ipaddress
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import conftest
from conftest import HERE, Server, Socket, idle, until
from fakellm_http import Call

TOKEN = "123456789:" + "A" * 35
OWNER, STRANGER = 777, 888


class FakeTelegram:
    """Bot API：getUpdates 从 updates 里取；发出去的都记在 sent 里。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.updates: list[dict] = []
        self.sent: list[tuple[str, dict]] = []
        self.next_update = 1
        self.next_msg = 100
        self.mids: dict[int, int] = {}   # 发出去的消息（sent 里那个 dict 的 id()）→ message_id
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def push(self, **u):
        with self.lock:
            u["update_id"] = self.next_update
            self.next_update += 1
            self.updates.append(u)

    def say(self, chat: int, text: str, reply_to: int | None = None):
        """reply_to：引用回复哪条消息（message_id）"""
        m = {"message_id": self.next_update, "chat": {"id": chat, "type": "private"},
             "from": {"id": chat, "first_name": "Test", "last_name": "User"}, "text": text}
        if reply_to is not None:
            m["reply_to_message"] = {"message_id": reply_to, "chat": {"id": chat, "type": "private"}, "text": "…"}
        self.push(message=m)

    def mid(self, body: dict) -> int:
        """发出去的这条消息的 message_id"""
        with self.lock:
            return self.mids[id(body)]

    def press(self, chat: int, data: str):
        self.push(callback_query={"id": f"cq{self.next_update}", "data": data, "message": {"chat": {"id": chat}}})

    def of(self, method: str, chat: int | None = None) -> list[dict]:
        with self.lock:
            return [b for m, b in self.sent if m == method and (chat is None or str(b.get("chat_id")) == str(chat))]

    def texts(self, chat: int) -> list[str]:
        return [b.get("text", "") for b in self.of("sendMessage", chat)]

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, d):
                body = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                token, method = self.path.split("/")[1][3:], self.path.split("/")[2]
                if token != TOKEN:
                    return self._json({"ok": False, "error_code": 401, "description": "Unauthorized"})
                if method == "getUpdates":
                    if body.get("offset"):   # 和真的一样：offset 之前的算确认过了，不再给（重启以后不会重放旧消息）
                        with fake.lock:
                            fake.updates = [u for u in fake.updates if u["update_id"] >= body["offset"]]
                    end = time.time() + 1
                    while time.time() < end:
                        with fake.lock:
                            ups = [u for u in fake.updates if u["update_id"] >= (body.get("offset") or 0)]
                        if ups:
                            return self._json({"ok": True, "result": ups})
                        time.sleep(0.03)
                    return self._json({"ok": True, "result": []})
                with fake.lock:
                    fake.sent.append((method, body))
                if method == "getMe":
                    return self._json({"ok": True, "result": {"id": 4242, "is_bot": True, "username": "osa_test_bot"}})
                if method == "sendMessage":
                    with fake.lock:
                        fake.next_msg += 1
                        n = fake.next_msg
                        fake.mids[id(body)] = n
                    return self._json({"ok": True, "result": {"message_id": n}})
                return self._json({"ok": True, "result": True})

        return H


# ---------- 假的推送服务（HTTPS） ----------
def _cert(tmp: Path) -> tuple[Path, ssl.SSLContext]:
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "osa test ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (tmp / "ca.pem").write_bytes(ca.public_bytes(pem))
    (tmp / "leaf.pem").write_bytes(leaf.public_bytes(pem))
    (tmp / "leaf.key").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp / "leaf.pem", tmp / "leaf.key")
    return tmp / "ca.pem", ctx


class FakePush:
    def __init__(self, ctx: ssl.SSLContext):
        self.got: list[tuple[str, dict, bytes]] = []
        self.status: dict[str, int] = {}
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                fake.got.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                code = fake.status.get(self.path, 201)
                self.send_response(code)
                self.send_header("content-length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.url = f"https://localhost:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def point(k) -> bytes:
    return k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def decrypt(body: bytes, priv, auth: bytes) -> dict:
    """RFC 8291 的另一头：手机用自己的私钥和 auth 解开。"""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_pub = body[21:21 + idlen]
    assert rs == 4096 and idlen == 65
    secret = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + point(priv) + as_pub).derive(secret)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert plain.endswith(b"\x02")
    return json.loads(plain[:-1])


@pytest.fixture(scope="module")
def tg():
    return FakeTelegram()


@pytest.fixture(scope="module")
def pushsvc():
    tmp = Path(tempfile.mkdtemp(prefix="osa-push-"))
    ca, ctx = _cert(tmp)
    p = FakePush(ctx)
    p.ca = ca
    return p


@pytest.fixture(scope="module")
def srv(llm, tg, pushsvc):
    s = Server(llm, env={"OSA_TELEGRAM_API": tg.url, "SSL_CERT_FILE": str(pushsvc.ca)})
    yield s
    s.stop()


@pytest.fixture(scope="module")
def channel(srv, tg):
    """一个配对好的 Telegram 连接，绑定 Bot「Tele」的私聊。"""
    bot = srv.make_bot("Tele")
    r = srv.http.post("/api/channels", json={"kind": "telegram", "fields": {"token": "bad"}, "chat_id": bot["chat_id"]})
    assert r.status_code == 400 and "token" in r.json()["detail"]
    ch = srv.post("/api/channels", {"kind": "telegram", "fields": {"token": TOKEN}, "chat_id": bot["chat_id"]})
    assert ch["name"] == "@osa_test_bot" and ch["kind"] == "telegram" and not ch["paired"]
    assert len(ch["pair_code"]) == 6 and ch["pair_link"] == f"https://t.me/osa_test_bot?start={ch['pair_code']}"
    dup = srv.http.post("/api/channels", json={"kind": "telegram", "fields": {"token": TOKEN}, "chat_id": bot["chat_id"]})
    assert dup.status_code == 400
    until(lambda: tg.of("setMyCommands"), what="连上 Telegram")
    assert until(lambda: [c for c in srv.get("/api/channels")["channels"] if c["state"] == "connected"], what="状态是已连接")

    tg.say(OWNER, "000000")
    until(lambda: "配对码不对。" in tg.texts(OWNER), what="配对码不对")
    tg.say(OWNER, f"/start {ch['pair_code']}")
    until(lambda: any("配对成功" in t for t in tg.texts(OWNER)), what="配对成功")
    c = until(lambda: [c for c in srv.get("/api/channels")["channels"] if c["paired"]], what="接口里显示配对了")[0]
    assert c["peer_name"] == "Test User" and "pair_code" not in c
    return {"bot": bot, "id": ch["id"]}


def test_chat_through_telegram(srv, fake, tg, channel):
    bot = channel["bot"]
    tg.say(STRANGER, "我是别人")
    fake.script("router:Tele", "你好，我是 **Tele**")
    tg.say(OWNER, "你好")
    until(lambda: any("我是 <b>Tele</b>" in t for t in tg.texts(OWNER)), what="回复发回 Telegram（HTML 格式）")
    user = srv.find(bot["chat_id"], role="user", text="你好")
    assert user and user[0]["meta"].get("via") == "telegram"
    assert not srv.find(bot["chat_id"], text="我是别人")      # 没配对的人说的话不理
    assert not tg.texts(STRANGER) or all("配对" not in t for t in tg.texts(STRANGER)[1:])
    assert tg.of("sendChatAction", OWNER)                     # 说话前显示“正在输入”

    tg.say(OWNER, "/new")
    until(lambda: any("已开始新话题" in t for t in tg.texts(OWNER)), what="/new")
    assert srv.find(bot["chat_id"], kind="reset")
    tg.say(OWNER, "/help")
    until(lambda: any("/stop" in t for t in tg.texts(OWNER)), what="/help")


def test_approval_button(srv, fake, tg, channel):
    until(lambda: any(a["name"] == "demo" and a.get("state") == "connected" for a in srv.get("/api/apps")["apps"]),
          timeout=30, what="假应用连上")
    fake.script("router:Tele", Call("dispatch", {"goal": "记一条笔记：周五交周报"}), "这就记")
    fake.script("worker", Call("demo__create_note", {"text": "周五交周报"}), "记好了")
    tg.say(OWNER, "帮我记一条笔记")
    card = until(lambda: [b for b in tg.of("sendMessage", OWNER) if b.get("reply_markup", {}).get("inline_keyboard")],
                 what="确认卡片（带按钮）")[0]
    buttons = card["reply_markup"]["inline_keyboard"][0]
    assert "需要你确认" in card["text"] and "create_note" in card["text"]
    ok = next(b for b in buttons if b["callback_data"].endswith(":ok"))
    tg.press(STRANGER, ok["callback_data"])
    until(lambda: any(b.get("text") == "没有权限" for b in tg.of("answerCallbackQuery")), what="别人点按钮没用")
    tg.press(OWNER, ok["callback_data"])
    until(lambda: any("已允许" in (b.get("text") or "") for b in tg.of("answerCallbackQuery")), what="点按钮的提示")
    until(lambda: any("已允许" in b["text"] for b in tg.of("editMessageText", OWNER)), what="卡片改成结果")
    until(lambda: any("完成" in t and "记好了" in t for t in tg.texts(OWNER)), what="任务结果发回 Telegram")
    idle(srv)


def test_word_command_and_settings(srv, fake, tg, channel):
    fake.script("router:Tele", Call("dispatch", {"goal": "再记一条"}), "好")
    fake.script("worker", Call("demo__create_note", {"text": "再记一条"}), "又记好了")
    tg.say(OWNER, "再记一条")
    card = until(lambda: [b for b in tg.of("sendMessage", OWNER) if b.get("reply_markup", {}).get("inline_keyboard")
                          and "再记一条" in b["text"]], what="第二张卡片")[0]
    aid = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    tg.say(OWNER, f"拒绝 #{aid}")
    until(lambda: any("已拒绝" in b["text"] for b in tg.of("editMessageText", OWNER)), what="打字拒绝")
    idle(srv)

    bad = srv.http.put("/api/channels/settings", json={"public_url": "ftp://x"})
    assert bad.status_code == 400
    assert srv.put("/api/channels/settings", {"public_url": "https://osa.example.com/"})["public_url"] == "https://osa.example.com"


def test_disable_and_delete(srv, tg, channel):
    cid = channel["id"]
    c = srv.patch(f"/api/channels/{cid}", {"enabled": False})
    assert c["state"] == "off" and not c["enabled"]
    c = srv.patch(f"/api/channels/{cid}", {"enabled": True})
    assert c["enabled"]
    re = srv.post(f"/api/channels/{cid}/pair")
    assert len(re["pair_code"]) == 6 and not re["paired"]
    assert srv.http.post("/api/channels/99999/pair").status_code == 404
    assert srv.delete(f"/api/channels/{cid}") == {"ok": True}
    assert not srv.get("/api/channels")["channels"]


def test_push(srv, fake, pushsvc):
    info = srv.get("/api/push")
    assert info["count"] == 0 and len(unb64u(info["key"])) == 65
    assert srv.http.post("/api/push/test").status_code == 400
    assert srv.http.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://x"}}).status_code == 400
    priv, auth = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    keys = {"p256dh": b64u(point(priv)), "auth": b64u(auth)}
    assert srv.post("/api/push/subscribe", {"subscription": {"endpoint": f"{pushsvc.url}/w/phone", "keys": keys}})["count"] == 1
    pushsvc.status["/w/gone"] = 410
    assert srv.post("/api/push/subscribe", {"subscription": {"endpoint": f"{pushsvc.url}/w/gone", "keys": keys}})["count"] == 2

    bot = srv.make_bot("Pusher")
    fake.script("router:Pusher", "推送的回复")
    ws = Socket(srv)
    ws.send({"type": "visibility", "visible": False})   # 页面切到后台：没有页面正被看着
    time.sleep(0.2)
    ws.say(bot["chat_id"], "推一下")
    hit = until(lambda: [g for g in pushsvc.got if g[0] == "/w/phone" and decrypt(g[2], priv, auth)["body"] == "推送的回复"],
                what="推送到手机")[0]
    msg = decrypt(hit[2], priv, auth)
    assert msg["title"] == "Pusher" and msg["url"] == f"/?chat={bot['chat_id']}" and msg["tag"] == f"chat-{bot['chat_id']}"
    h = hit[1]
    assert h["content-encoding"] == "aes128gcm" and h["ttl"] == "86400" and h["authorization"].startswith("vapid t=")
    assert h["topic"] and len(h["topic"]) <= 32
    ws.close()
    until(lambda: srv.get("/api/push")["count"] == 1, what="410 的订阅删掉了")
    r = srv.post("/api/push/test")
    assert r["results"] == ["ok"]
    assert srv.post("/api/push/unsubscribe", {"endpoint": f"{pushsvc.url}/w/phone"})["count"] == 0
    idle(srv)


# ======================================================================
# 只有 Rust 版有：任务提问卡片、重启时说不准那一步的卡片（docs/plans/tasks-board.md 第二波 channels）
# ======================================================================
rust_only = pytest.mark.skipif(conftest.IMPL != "rust", reason="提问卡片、说不准那一步的卡片只有 Rust 版有")
FS_OWNER, FS_STRANGER = "oc_owner", "oc_stranger"
DD_OWNER, DD_STRANGER = "staff_owner", "staff_stranger"
SLOW_APPS = {f"s{c}": {"enabled": True, "command": sys.executable, "args": [str(HERE / "fake_mcp_slow.py")], "confirm": "auto"}
             for c in "abcdefghi"}


# ---------- 飞书长连接的 pbbp2.Frame ----------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            return n, i


def _pb_fields(b: bytes) -> list[tuple[int, object]]:
    out, i = [], 0
    while i < len(b):
        key, i = _read_varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(b, i)
        elif wt == 2:
            n, i = _read_varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            raise ValueError(f"wire type {wt}")
        out.append((num, v))
    return out


def pb_frame(headers: dict[str, str], payload: bytes, service: int = 7) -> bytes:
    num = lambda f, v: _varint(f << 3) + _varint(v)                       # noqa: E731
    byt = lambda f, v: _varint(f << 3 | 2) + _varint(len(v)) + v          # noqa: E731
    out = num(1, 1) + num(2, 0) + num(3, service) + num(4, 1)
    for k, v in headers.items():
        out += byt(5, byt(1, k.encode()) + byt(2, v.encode()))
    return out + byt(8, payload)


def pb_parse(b: bytes) -> tuple[dict[str, str], bytes]:
    hs, payload = {}, b""
    for num, v in _pb_fields(b):
        if num == 5:
            h = dict(_pb_fields(v))
            hs[h.get(1, b"").decode()] = h.get(2, b"").decode()
        elif num == 8:
            payload = v
    return hs, payload


class _Http(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, d, code=200):
        body = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")


class _WsFake:
    """假的长连接服务：记住最新的一条连接（服务重启后会连上来一条新的）"""

    def _start_ws(self):
        from websockets.sync.server import serve
        self.conn = None
        self.conns = 0
        self.ws = serve(self._ws, "127.0.0.1", 0)
        self.ws_port = self.ws.socket.getsockname()[1]
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()

    def _ws(self, conn):
        with self.lock:
            self.conn = conn
            self.conns += 1
        try:
            for msg in conn:
                self.got(msg)
        except Exception:
            pass

    def got(self, msg):
        pass

    def connected(self, after: int = 0) -> bool:
        return self.conns > after and self.conn is not None


class FakeFeishu(_WsFake):
    """开放平台接口 + 长连接。发出去的消息记在 sent：(create | patch, message_id, 发给谁, 类型, 内容)；点按钮回给飞书的提示记在 toasts。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent: list[tuple[str, str, str, str, dict]] = []
        self.toasts: list[str] = []
        self.n = 0
        self._start_ws()
        fake = self

        class H(_Http):
            def do_GET(self):
                if self.path.startswith("/open-apis/bot/v3/info"):
                    return self._json({"code": 0, "bot": {"app_name": "OSA 飞书", "open_id": "ou_bot"}})
                self._json({"code": 404, "msg": "not found"}, 404)

            def do_POST(self):
                path, body = self.path.split("?")[0], self._body()
                if path == "/open-apis/auth/v3/tenant_access_token/internal":
                    return self._json({"code": 0, "tenant_access_token": "t-test", "expire": 7200})
                if path == "/callback/ws/endpoint":
                    return self._json({"code": 0, "data": {"URL": f"ws://127.0.0.1:{fake.ws_port}/ws?service_id=7",
                                                           "ClientConfig": {"PingInterval": 120}}})
                if path == "/open-apis/im/v1/messages":
                    with fake.lock:
                        fake.n += 1
                        mid = f"om_{fake.n}"
                        fake.sent.append(("create", mid, body["receive_id"], body["msg_type"], json.loads(body["content"])))
                    return self._json({"code": 0, "data": {"message_id": mid}})
                if path.endswith("/reactions"):
                    return self._json({"code": 0, "data": {"reaction_id": "r1"}})
                self._json({"code": 404, "msg": "not found"}, 404)

            def do_PATCH(self):
                mid, body = self.path.split("?")[0].rsplit("/", 1)[1], self._body()
                with fake.lock:
                    fake.sent.append(("patch", mid, "", "interactive", json.loads(body["content"])))
                self._json({"code": 0, "data": {}})

            def do_DELETE(self):
                self._json({"code": 0, "data": {}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def got(self, msg):
        if not isinstance(msg, bytes):
            return
        hs, payload = pb_parse(msg)
        if "biz_rt" not in hs:   # 不是对事件的回应（ping）
            return
        d = json.loads(payload or b"{}")
        if d.get("data"):
            with self.lock:
                self.toasts.append(json.loads(base64.b64decode(d["data"]))["toast"]["content"])

    def _event(self, etype: str, ev: dict):
        with self.lock:
            self.n += 1
            n, conn = self.n, self.conn
        body = json.dumps({"schema": "2.0", "header": {"event_id": f"ev{n}", "event_type": etype}, "event": ev}, ensure_ascii=False)
        conn.send(pb_frame({"type": "event", "message_id": f"m{n}", "sum": "1", "seq": "0"}, body.encode()))

    def say(self, chat: str, text: str, parent_id: str | None = None):
        """parent_id：引用回复哪条消息（飞书的引用回复带 parent_id 和 root_id）"""
        msg = {"chat_type": "p2p", "chat_id": chat, "message_id": f"om_in_{self.n}", "message_type": "text",
               "content": json.dumps({"text": text}, ensure_ascii=False)}
        if parent_id:
            msg["parent_id"] = msg["root_id"] = parent_id
        self._event("im.message.receive_v1", {"message": msg, "sender": {"sender_type": "user"}})

    def press(self, chat: str, data: str):
        self._event("card.action.trigger", {"action": {"value": {"osa": data}}, "context": {"open_chat_id": chat}})

    def texts(self, chat: str) -> list[str]:
        with self.lock:
            posts = [c for k, _, to, t, c in self.sent if k == "create" and to == chat and t == "post"]
        return ["\n".join(el["text"] for row in p["zh_cn"]["content"] for el in row) for p in posts]

    def cards(self, needle: str) -> list[tuple[str, dict]]:
        with self.lock:
            return [(mid, c) for k, mid, _, t, c in self.sent
                    if k == "create" and t == "interactive" and needle in json.dumps(c, ensure_ascii=False)]

    def card(self, needle: str) -> tuple[str, dict]:
        return until(lambda: self.cards(needle), what=f"飞书卡片：{needle}")[0]

    def done(self, mid: str) -> str:
        with self.lock:
            ps = [c for k, m, _, _, c in self.sent if k == "patch" and m == mid]
        return "\n".join(el.get("content", "") for c in ps for el in c["elements"] if el["tag"] == "markdown")


def fs_buttons(card: dict) -> list[tuple[str, str]]:
    return [(a["text"]["content"], a["value"]["osa"]) for el in card["elements"] if el["tag"] == "action"
            for a in el["actions"] if "value" in a]


class FakeDingTalk(_WsFake):
    """开放平台接口 + Stream 长连接。机器人发出去的 markdown 记在 sent：(发给谁, 标题, 正文)。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent: list[tuple[str, str, str]] = []
        self.n = 0
        self._start_ws()
        fake = self

        class H(_Http):
            def do_POST(self):
                path, body = self.path.split("?")[0], self._body()
                if path == "/v1.0/oauth2/accessToken":
                    return self._json({"accessToken": "dt-test", "expireIn": 7200})
                if path == "/v1.0/gateway/connections/open":
                    return self._json({"endpoint": f"ws://127.0.0.1:{fake.ws_port}/", "ticket": "tk"})
                if path == "/v1.0/robot/oToMessages/batchSend":
                    p = json.loads(body["msgParam"])
                    with fake.lock:
                        for u in body["userIds"]:
                            fake.sent.append((u, p["title"], p["text"]))
                    return self._json({"processQueryKey": "q"})
                self._json({"code": "NotFound", "message": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def say(self, staff: str, text: str):
        with self.lock:
            self.n += 1
            n, conn = self.n, self.conn
        msg = {"msgId": f"msg{n}", "conversationType": "1", "senderStaffId": staff, "senderNick": "Test User",
               "msgtype": "text", "text": {"content": text}, "robotCode": "ding_robot"}
        conn.send(json.dumps({"specVersion": "1.0", "type": "CALLBACK",
                              "headers": {"topic": "/v1.0/im/bot/messages/get", "messageId": f"dm{n}"},
                              "data": json.dumps(msg, ensure_ascii=False)}, ensure_ascii=False))

    def texts(self, staff: str) -> list[str]:
        with self.lock:
            return [t for u, _, t in self.sent if u == staff]

    def card(self, needle: str) -> str:
        return until(lambda: [t for t in self.texts(DD_OWNER) if needle in t], what=f"钉钉卡片：{needle}")[0]


@pytest.fixture(scope="module")
def tg2():
    return FakeTelegram()


@pytest.fixture(scope="module")
def fs():
    return FakeFeishu()


@pytest.fixture(scope="module")
def dd():
    return FakeDingTalk()


@pytest.fixture(scope="module")
def xsrv(llm, tg2, fs, dd, pushsvc):
    s = Server(llm, env={"OSA_TELEGRAM_API": tg2.url, "OSA_FEISHU_API": fs.url, "OSA_DINGTALK_API": dd.url,
                         "SSL_CERT_FILE": str(pushsvc.ca)},
               config={"mcp_servers": SLOW_APPS})
    yield s
    s.stop()


@pytest.fixture(scope="module")
def chans(xsrv, tg2, fs, dd, pushsvc):
    """三个配对好的连接：Telegram ↔ Bot「TgAsk」、飞书 ↔「FsAsk」、钉钉 ↔「DdAsk」；再订阅一台手机收推送（没有网页开着）"""
    bots = {k: xsrv.make_bot(n) for k, n in (("tg", "TgAsk"), ("fs", "FsAsk"), ("dd", "DdAsk"))}
    tc = xsrv.post("/api/channels", {"kind": "telegram", "fields": {"token": TOKEN}, "chat_id": bots["tg"]["chat_id"]})
    fc = xsrv.post("/api/channels", {"kind": "feishu", "fields": {"app_id": "cli_test", "app_secret": "sec"},
                                     "chat_id": bots["fs"]["chat_id"]})
    dc = xsrv.post("/api/channels", {"kind": "dingtalk", "fields": {"app_key": "dingkey", "app_secret": "sec", "name": "钉钉"},
                                     "chat_id": bots["dd"]["chat_id"]})
    until(lambda: len([c for c in xsrv.get("/api/channels")["channels"] if c["state"] == "connected"]) == 3,
          what="三个连接都连上了")
    until(lambda: fs.connected() and dd.connected(), what="飞书、钉钉的长连接连上")
    tg2.say(OWNER, tc["pair_code"])
    fs.say(FS_OWNER, fc["pair_code"])
    dd.say(DD_OWNER, dc["pair_code"])
    until(lambda: any("配对成功" in t for t in tg2.texts(OWNER)), what="Telegram 配对成功")
    until(lambda: any("配对成功" in t for t in fs.texts(FS_OWNER)), what="飞书配对成功")
    until(lambda: any("配对成功" in t for t in dd.texts(DD_OWNER)), what="钉钉配对成功")
    priv, auth = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    keys = {"p256dh": b64u(point(priv)), "auth": b64u(auth)}
    xsrv.post("/api/push/subscribe", {"subscription": {"endpoint": f"{pushsvc.url}/w/xphone", "keys": keys}})
    return {"bots": bots, "push": (priv, auth)}


def user_text(body: dict) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], str))


def tool_text(body: dict) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "tool")


def by_goal(plan: dict[str, list]):
    """Worker 的剧本按任务分：请求里带着哪个任务的标记，就给那个任务的下一条（几个任务同时跑时不会拿错）"""
    def pick(body):
        text = user_text(body)
        for key, replies in plan.items():
            if key in text:
                return replies.pop(0) if replies else "完成"
        return "完成"
    return pick


def worker_saw(fake, key: str, text: str) -> bool:
    return any(key in user_text(b) and text in tool_text(b) for b in fake.bodies("worker"))


def results(srv, chat_id: int, tid: int) -> list[dict]:
    return [m for m in srv.find(chat_id, kind="result") if m["meta"].get("task_id") == tid]


def pending(srv, aid: int) -> bool:
    return any(a["id"] == aid for a in srv.get("/api/bootstrap")["approvals"])


def dispatch(srv, fake, chat_id: int, name: str, goals: list[str], say) -> list[int]:
    """让 Bot 一轮派出这几个任务（say：在聊天软件里发一句话），返回任务编号"""
    before = {m["id"] for m in srv.find(chat_id, kind="dispatch")}
    fake.script(f"router:{name}", [Call("dispatch", {"goal": g}) for g in goals], "派了")
    say()
    new = until(lambda: (n := [m for m in srv.find(chat_id, kind="dispatch") if m["id"] not in before]) and len(n) == len(goals) and n,
                what="派发")
    return [next(m["id"] for m in new if g in m["content"]) for g in goals]


def pushes(pushsvc, priv, auth) -> list[dict]:
    return [decrypt(g[2], priv, auth) for g in list(pushsvc.got) if g[0] == "/w/xphone"]


def tg_card(tg, needle: str) -> dict:
    return until(lambda: [b for b in tg.of("sendMessage", OWNER) if b.get("reply_markup", {}).get("inline_keyboard")
                          and needle in b["text"]], what=f"Telegram 卡片：{needle}")[0]


def tg_rows(card: dict) -> list[tuple[str, str]]:
    return [(r[0]["text"], r[0]["callback_data"]) for r in card["reply_markup"]["inline_keyboard"]]


def tg_toast(tg, text: str):
    until(lambda: any(text in (b.get("text") or "") for b in tg.of("answerCallbackQuery")), what=f"按钮的提示：{text}")


def tg_done(tg, text: str):
    until(lambda: any(text in b["text"] for b in tg.of("editMessageText", OWNER)), what=f"卡片改成：{text}")


@rust_only
def test_question_telegram(xsrv, fake, tg2, chans, pushsvc):
    bot = chans["bots"]["tg"]
    chat = bot["chat_id"]
    plan = {"[tq1]": [Call("ask_user", {"question": "要哪个城市？", "options": ["北京", "上海"]}), "按上海查好了"],
            "[tq2]": [Call("ask_user", {"question": "要几天的？"}), "查了三天"],
            "[tq3]": [Call("ask_user", {"question": "要不要发？", "options": ["发"]}), "没发"]}
    fake.script("worker", *[by_goal(plan)] * 20)

    # 点选项：选项一个一行，另有“不回答”；别人点、别人回话都不算
    tid, = dispatch(xsrv, fake, chat, "TgAsk", ["[tq1] 查天气"], lambda: tg2.say(OWNER, "帮我查天气"))
    card = tg_card(tg2, "要哪个城市？")
    assert "有个问题要你回答" in card["text"] and f"任务 #{tid}" in card["text"]
    rows = tg_rows(card)
    assert [r[0] for r in rows] == ["北京", "上海", "不回答"]
    assert rows[1][1].endswith(":a1") and rows[2][1].endswith(":skip")
    aid = int(rows[1][1].split(":")[1])
    tg2.press(STRANGER, rows[1][1])
    tg_toast(tg2, "没有权限")
    tg2.say(STRANGER, "北京")
    time.sleep(0.5)
    assert pending(xsrv, aid)
    tg2.press(OWNER, rows[1][1])
    tg_toast(tg2, "已回答：上海")
    tg_done(tg2, "已回答：上海")
    res = until(lambda: results(xsrv, chat, tid), what="结果")
    assert "按上海查好了" in res[0]["content"]
    assert worker_saw(fake, "[tq1]", "用户回答：上海")
    # 推送：等你回答，正文是问题
    priv, auth = chans["push"]
    hit = until(lambda: [p for p in pushes(pushsvc, priv, auth) if p["tag"] == f"approval-{aid}"], what="提问的推送")[0]
    assert hit["title"] == "TgAsk 等你回答" and hit["body"] == "要哪个城市？"

    # 直接说的话交给 Bot：它在状态里看到任务在问什么，判断是在回答，用 resolve_approval 把原话交给任务
    tid, = dispatch(xsrv, fake, chat, "TgAsk", ["[tq2] 查天气预报"], lambda: tg2.say(OWNER, "帮我查天气预报"))
    card = tg_card(tg2, "要几天的？")
    assert [r[0] for r in tg_rows(card)] == ["不回答"]
    assert "引用这条消息回复" in card["text"] and "Bot 会看是不是在回答" in card["text"]
    aid = int(tg_rows(card)[0][1].split(":")[1])
    n_router = len(fake.bodies("router", "TgAsk"))
    fake.script("router:TgAsk", Call("resolve_approval", {"id": aid, "approve": True, "note": "三天"}), "好的，告诉它了")
    tg2.say(OWNER, "三天")
    tg_done(tg2, "已回答：三天")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[tq2]", "用户回答：三天")
    assert xsrv.find(chat, role="user", text="三天")
    seen = [b for b in fake.bodies("router", "TgAsk")[n_router:] if "三天" in user_text(b)]
    assert seen and f"#{aid}（任务 #{tid}）在问用户：要几天的？" in user_text(seen[0])

    # 不回答
    tid, = dispatch(xsrv, fake, chat, "TgAsk", ["[tq3] 看情况发消息"], lambda: tg2.say(OWNER, "看情况发消息"))
    rows = tg_rows(tg_card(tg2, "要不要发？"))
    assert [r[0] for r in rows] == ["发", "不回答"]
    tg2.press(OWNER, rows[-1][1])
    tg_done(tg2, "没有回答")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[tq3]", "用户没有回答")
    idle(xsrv)


@rust_only
def test_two_questions_telegram(xsrv, fake, tg2, chans):
    """两个问题同时在等：直接回的话照常交给 Bot；用“回答 #编号 内容”（中英文都行）回答指定的那个"""
    bot = chans["bots"]["tg"]
    chat = bot["chat_id"]
    opts = [f"选项{i}" for i in range(6)]
    plan = {"[tw1]": [Call("ask_user", {"question": "先查哪个？", "options": opts}), "好了一"],
            "[tw2]": [Call("ask_user", {"question": "再查哪个？"}), "好了二"]}
    fake.script("worker", *[by_goal(plan)] * 20)
    t1, t2 = dispatch(xsrv, fake, chat, "TgAsk", ["[tw1] 查一", "[tw2] 查二"], lambda: tg2.say(OWNER, "查两样"))
    c1, c2 = tg_card(tg2, "先查哪个？"), tg_card(tg2, "再查哪个？")
    # 选项太多：按钮只放前 4 个，其余写在正文里
    assert [r[0] for r in tg_rows(c1)] == opts[:4] + ["不回答"]
    assert "选项4、选项5" in c1["text"]
    a1, a2 = (int(tg_rows(c)[0][1].split(":")[1]) for c in (c1, c2))
    fake.script("router:TgAsk", "收到你的话")
    tg2.say(OWNER, "随便聊聊")
    until(lambda: any("收到你的话" in t for t in tg2.texts(OWNER)), what="交给了 Bot")
    assert xsrv.find(chat, role="user", text="随便聊聊")
    assert pending(xsrv, a1) and pending(xsrv, a2)
    # 一个做完再回答下一个：两个任务同时结束会撞上 tasklog.rs 的死锁（见报告）
    tg2.say(OWNER, f"回答 #{a1} 选项5")
    until(lambda: results(xsrv, chat, t1), what="第一个结果")
    tg2.say(OWNER, f"answer #{a2}: 乙")
    until(lambda: results(xsrv, chat, t2), what="第二个结果")
    assert worker_saw(fake, "[tw1]", "用户回答：选项5") and worker_saw(fake, "[tw2]", "用户回答：乙")
    tg_done(tg2, "已回答：选项5")
    tg_done(tg2, "已回答：乙")
    idle(xsrv)


@rust_only
def test_question_other_words_telegram(xsrv, fake, tg2, chans):
    """一个问题在等时：说别的事照常交给 Bot、问题接着等；以卡片编号开头的话交给那张卡的任务，问题也接着等；
    引用提问卡片回复就是回答，不经过 Bot"""
    bot = chans["bots"]["tg"]
    chat = bot["chat_id"]
    xsrv.post("/api/projects", {"key": "TGQ", "name": "提问项目"})
    evt = threading.Event()
    plan = {"[tn1]": [Call("ask_user", {"question": "要查哪一天？"}), "查好了那天"],
            "[tn2]": [lambda body: (evt.wait(15), Call("search_memory", {"query": "欧洲"}))[1], "报告含英国"]}
    pick = by_goal(plan)
    fake.script("worker", *[lambda body: (r(body) if callable(r := pick(body)) else r)] * 20)
    # 两张卡：[tn2] 在做（卡在第一步），[tn1] 在问问题
    before = {m["id"] for m in xsrv.find(chat, kind="dispatch")}
    fake.script("router:TgAsk", Call("dispatch", {"goal": "[tn2] 查欧洲市场", "need": "欧洲市场报告", "project": "TGQ"}), "派了")
    tg2.say(OWNER, "查一下欧洲市场")
    d2 = until(lambda: [m for m in xsrv.find(chat, kind="dispatch") if m["id"] not in before], what="派发")[0]
    t2 = d2["id"]
    key = xsrv.get(f"/api/needs/{d2['meta']['need_id']}")["key"]
    until(lambda: any("[tn2]" in user_text(b) for b in fake.bodies("worker")), what="[tn2] 开始")
    t1, = dispatch(xsrv, fake, chat, "TgAsk", ["[tn1] 查天气"], lambda: tg2.say(OWNER, "再查个天气"))
    card = tg_card(tg2, "要查哪一天？")
    aid = int(tg_rows(card)[0][1].split(":")[1])

    # 说别的事：Bot 直接答话、不调 resolve_approval，问题接着等，任务没拿到这句话
    n = len(fake.bodies("router", "TgAsk"))
    fake.script("router:TgAsk", "明天晴，适合出门")
    tg2.say(OWNER, "顺便查下明天天气")
    until(lambda: any("明天晴" in t for t in tg2.texts(OWNER)), what="Bot 照常回话")
    asked = [b for b in fake.bodies("router", "TgAsk")[n:] if "顺便查下明天天气" in user_text(b)]
    assert asked and f"#{aid}（任务 #{t1}）在问用户：要查哪一天？" in user_text(asked[0])
    assert pending(xsrv, aid) and xsrv.find(chat, role="user", text="顺便查下明天天气")
    assert not any("顺便查下" in user_text(b) + tool_text(b) for b in fake.bodies("worker") if "[tn1]" in user_text(b))

    # 以卡片编号开头：交给那张卡的任务，Bot 不被叫醒，问题接着等
    n = len(fake.bodies("router", "TgAsk"))
    tg2.say(OWNER, f"{key} 加上英国")
    until(lambda: [f for f in xsrv.find(chat, kind="followup") if f["meta"]["task_id"] == t2], what="给 [tn2] 的话")
    assert pending(xsrv, aid) and len(fake.bodies("router", "TgAsk")) == n
    evt.set()
    until(lambda: results(xsrv, chat, t2), what="[tn2] 的结果")
    assert any("[tn2]" in user_text(b) and f"{key} 加上英国" in user_text(b) for b in fake.bodies("worker"))
    assert pending(xsrv, aid)

    # 引用提问卡片回复：就是回答，直接交给任务，Bot 没有新请求
    tg2.say(OWNER, "后天", reply_to=tg2.mid(card))
    tg_done(tg2, "已回答：后天")
    until(lambda: results(xsrv, chat, t1), what="[tn1] 的结果")
    assert worker_saw(fake, "[tn1]", "用户回答：后天")
    assert len(fake.bodies("router", "TgAsk")) == n and not xsrv.find(chat, role="user", text="后天")
    assert [f for f in xsrv.find(chat, kind="followup") if f["meta"]["task_id"] == t1]   # 对话里留一条“给任务的话”
    idle(xsrv)


@rust_only
def test_question_feishu(xsrv, fake, fs, chans):
    bot = chans["bots"]["fs"]
    chat = bot["chat_id"]
    plan = {"[fq1]": [Call("ask_user", {"question": "飞书要哪个城市？", "options": ["北京", "上海"]}), "按北京查好了"],
            "[fq2]": [Call("ask_user", {"question": "飞书要几天的？"}), "查了五天"],
            "[fq3]": [Call("ask_user", {"question": "飞书要不要发？"}), "没发"],
            "[fq4]": [Call("ask_user", {"question": "飞书住几晚？"}), "订了两晚"]}
    fake.script("worker", *[by_goal(plan)] * 20)

    tid, = dispatch(xsrv, fake, chat, "FsAsk", ["[fq1] 查天气"], lambda: fs.say(FS_OWNER, "帮我查天气"))
    mid, card = fs.card("飞书要哪个城市？")
    assert "有个问题要你回答" in card["header"]["title"]["content"]
    btns = fs_buttons(card)
    assert [b[0] for b in btns] == ["北京", "上海", "不回答"]
    aid = int(btns[0][1].split(":")[1])
    fs.press(FS_STRANGER, btns[0][1])
    until(lambda: "没有权限" in fs.toasts, what="别人点按钮没用")
    fs.say(FS_STRANGER, "上海")
    time.sleep(0.5)
    assert pending(xsrv, aid)
    fs.press(FS_OWNER, btns[0][1])
    until(lambda: "已回答：北京" in fs.toasts, what="点选项的提示")
    until(lambda: "已回答：北京" in fs.done(mid), what="卡片改成已回答")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[fq1]", "用户回答：北京")

    # 直接说：交给 Bot，它用 resolve_approval 交给任务
    tid, = dispatch(xsrv, fake, chat, "FsAsk", ["[fq2] 查天气预报"], lambda: fs.say(FS_OWNER, "帮我查天气预报"))
    mid, card = fs.card("飞书要几天的？")
    assert "引用这条消息回复" in json.dumps(card, ensure_ascii=False)
    aid = int(fs_buttons(card)[0][1].split(":")[1])
    fake.script("router:FsAsk", Call("resolve_approval", {"id": aid, "approve": True, "note": "五天"}), "好的")
    fs.say(FS_OWNER, "五天")
    until(lambda: "已回答：五天" in fs.done(mid), what="Bot 判断是回答")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[fq2]", "用户回答：五天") and xsrv.find(chat, role="user", text="五天")

    # 引用卡片回复：直接交给任务，不经过 Bot
    tid, = dispatch(xsrv, fake, chat, "FsAsk", ["[fq4] 查酒店"], lambda: fs.say(FS_OWNER, "帮我查酒店"))
    mid, card = fs.card("飞书住几晚？")
    n_router = len(fake.bodies("router", "FsAsk"))
    fs.say(FS_OWNER, "两晚", parent_id=mid)
    until(lambda: "已回答：两晚" in fs.done(mid), what="引用回复算回答")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[fq4]", "用户回答：两晚")
    assert len(fake.bodies("router", "FsAsk")) == n_router and not xsrv.find(chat, role="user", text="两晚")

    tid, = dispatch(xsrv, fake, chat, "FsAsk", ["[fq3] 看情况发消息"], lambda: fs.say(FS_OWNER, "看情况发消息"))
    mid, card = fs.card("飞书要不要发？")
    btns = fs_buttons(card)
    assert [b[0] for b in btns] == ["不回答"]
    fs.press(FS_OWNER, btns[0][1])
    until(lambda: "没有回答" in fs.done(mid), what="卡片改成没有回答")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[fq3]", "用户没有回答")
    idle(xsrv)


@rust_only
def test_question_dingtalk(xsrv, fake, dd, chans):
    """钉钉没有按钮：“按钮”是替用户发出“回答 #12 上海”的链接；也能打字回复；直接说的交给 Bot 判断（钉钉没有引用回复）"""
    from urllib.parse import quote
    bot = chans["bots"]["dd"]
    chat = bot["chat_id"]
    plan = {"[dq1]": [Call("ask_user", {"question": "钉钉要哪个城市？", "options": ["北京", "上海"]}), "按上海查好了"],
            "[dq2]": [Call("ask_user", {"question": "钉钉要几天的？"}), "查了七天"],
            "[dq3]": [Call("ask_user", {"question": "钉钉要不要发？"}), "没发"]}
    fake.script("worker", *[by_goal(plan)] * 20)

    tid, = dispatch(xsrv, fake, chat, "DdAsk", ["[dq1] 查天气"], lambda: dd.say(DD_OWNER, "帮我查天气"))
    text = dd.card("钉钉要哪个城市？")
    aid = int(re.search(r"回答 #(\d+) 你的回答", text)[1])
    assert "有个问题要你回答" in text and f"“不回答 #{aid}”" in text
    assert "dtmd://dingtalkclient/sendMessage?content=" + quote(f"回答 #{aid} 上海", safe="") in text
    assert "dtmd://dingtalkclient/sendMessage?content=" + quote(f"不回答 #{aid}", safe="") in text
    dd.say(DD_STRANGER, f"回答 #{aid} 北京")
    time.sleep(0.5)
    assert pending(xsrv, aid)
    dd.say(DD_OWNER, f"回答 #{aid} 上海")   # 点“上海”的链接替用户发的就是这句
    until(lambda: any("已回答：上海" in t for t in dd.texts(DD_OWNER)), what="回答以后的通知")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[dq1]", "用户回答：上海")

    tid, = dispatch(xsrv, fake, chat, "DdAsk", ["[dq2] 查天气预报"], lambda: dd.say(DD_OWNER, "帮我查天气预报"))
    text = dd.card("钉钉要几天的？")
    assert "引用" not in text and "Bot 会看是不是在回答" in text
    aid = int(re.search(r"回答 #(\d+) 你的回答", text)[1])
    fake.script("router:DdAsk", Call("resolve_approval", {"id": aid, "approve": True, "note": "七天"}), "好的")
    dd.say(DD_OWNER, "七天")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[dq2]", "用户回答：七天") and xsrv.find(chat, role="user", text="七天")
    until(lambda: any("已回答：七天" in t and f"#{aid}" in t for t in dd.texts(DD_OWNER)), what="回答以后的通知")

    tid, = dispatch(xsrv, fake, chat, "DdAsk", ["[dq3] 看情况发消息"], lambda: dd.say(DD_OWNER, "看情况发消息"))
    text = dd.card("钉钉要不要发？")
    aid = int(re.search(r"回答 #(\d+) 你的回答", text)[1])
    dd.say(DD_OWNER, f"Skip #{aid}")
    until(lambda: any("没有回答" in t and f"#{aid}" in t for t in dd.texts(DD_OWNER)), what="不回答以后的通知")
    until(lambda: results(xsrv, chat, tid), what="结果")
    assert worker_saw(fake, "[dq3]", "用户没有回答")
    idle(xsrv)


def lines(path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def apps_up(srv):
    def up():
        apps = [a for a in srv.get("/api/apps")["apps"] if a["name"] in SLOW_APPS]
        return len(apps) == len(SLOW_APPS) and all(a.get("state") == "connected" for a in apps)
    until(up, timeout=60, what="假应用都连上")


@rust_only
def test_doubt_cards(xsrv, fake, tg2, fs, dd, chans, pushsvc):
    """强杀服务时九个任务的写操作做了一半：重启后各弹一张“有一步要你定”的卡片，
    Telegram、飞书点三个按钮，钉钉打字回复三种写法（中英文），别人点、别人回都不算。"""
    apps_up(xsrv)
    for app in SLOW_APPS:
        xsrv.post("/api/rules", {"pattern": f"{app}__slow_append", "action": "allow"})   # 平常不用确认
    log = xsrv.tmp / "slow.log"
    apps = iter(SLOW_APPS)
    cases = [(k, c, next(apps)) for k in ("tg", "fs", "dd") for c in ("redo", "made", "stop")]
    plan = {f"[d-{k}-{c}]": [Call(f"{app}__slow_append", {"text": f"{k}-{c}-1", "seconds": 15, "log": str(log)}),
                             Call(f"{app}__slow_append", {"text": f"{k}-{c}-2", "seconds": 0.1, "log": str(log)}),
                             f"收尾了 {k}-{c}"] for k, c, app in cases}
    fake.script("worker", *[by_goal(plan)] * 60)
    bots = chans["bots"]
    sayers = {"tg": lambda t: tg2.say(OWNER, t), "fs": lambda t: fs.say(FS_OWNER, t), "dd": lambda t: dd.say(DD_OWNER, t)}
    tids = {}
    for k, c, _ in cases:
        goal = f"[d-{k}-{c}] 追加一行记录"
        tids[k, c], = dispatch(xsrv, fake, bots[k]["chat_id"], bots[k]["name"], [goal], lambda: sayers[k]("帮我追加一行记录"))
        until(lambda: f"append-start {k}-{c}-1" in lines(log), what=f"{k}-{c} 的写操作开始执行")
    fs_n, dd_n = fs.conns, dd.conns
    priv, auth = chans["push"]
    seen = len(pushes(pushsvc, priv, auth))   # 重启以后确认请求的编号从 1 重新数，只看重启后的推送
    xsrv.restart(kill=True)   # 强杀：九个写操作都做了一半
    until(lambda: fs.connected(fs_n) and dd.connected(dd_n), timeout=20, what="飞书、钉钉重新连上")
    cards = until(lambda: (a := [a for a in xsrv.get("/api/bootstrap")["approvals"] if a.get("doubt")]) and len(a) == len(cases) and a,
                  timeout=30, what="九张说不准的卡片")
    aid = {(k, c): next(a["id"] for a in cards if a["task_id"] == tids[k, c]) for k, c, _ in cases}
    apps_up(xsrv)   # 再做一次要等应用重新连上

    # 一个任务了结了再处理下一个：两个任务同时结束（tasklog::close）和别的任务的一步正好结束（Log::finish）撞在一起
    # 会死锁，整个服务卡住。那是 tasklog.rs 的问题（见报告），这里避开
    tcards = {c: tg_card(tg2, f"tg-{c}-1") for c in ("redo", "made", "stop")}
    fcards = {c: fs.card(f"fs-{c}-1") for c in ("redo", "made", "stop")}
    dtexts = {c: dd.card(f"dd-{c}-1") for c in ("redo", "made", "stop")}

    # Telegram：三个按钮，一个一行，没有“总是允许”；写明是重启时正在做的、几点开始的、参数、Bot 查过什么
    t = tcards["redo"]["text"]
    assert "有一步要你定" in t and "服务重启时这一步正在执行" in t and "开始）" in t and "slow_append" in t and "Bot 查过" in t
    assert "再做一次的话用这些参数" in t and "tg-redo-2" in t
    for c in tcards:
        rows = tg_rows(tcards[c])
        assert [r[0] for r in rows] == ["再做一次", "已经做成了", "不做了，停下任务"]
        assert [r[1].rsplit(":", 1)[1] for r in rows] == ["redo", "made", "stop"]
    tg2.press(STRANGER, tg_rows(tcards["redo"])[0][1])
    tg_toast(tg2, "没有权限")
    # 引用说不准的卡片回复：不算回答，交给 Bot（它在状态里看得到这一步说不准）
    n = len(fake.bodies("router", "TgAsk"))
    fake.script("router:TgAsk", "这一步我再看看")
    tg2.say(OWNER, "这一步怎么说", reply_to=tg2.mid(tcards["redo"]))
    until(lambda: any("这一步我再看看" in t for t in tg2.texts(OWNER)), what="引用说不准卡片的回复交给了 Bot")
    asked = [b for b in fake.bodies("router", "TgAsk")[n:] if "这一步怎么说" in user_text(b)]
    assert asked and f"#{aid['tg', 'redo']}（任务 #{tids['tg', 'redo']}）有一步说不准做没做成，要不要再做：" in user_text(asked[0])
    assert all(pending(xsrv, x) for x in aid.values())
    # 飞书：三个按钮
    for c, (mid, card) in fcards.items():
        assert "有一步要你定" in card["header"]["title"]["content"]
        assert [b[0] for b in fs_buttons(card)] == ["再做一次", "已经做成了", "不做了，停下任务"]
    n = fs.toasts.count("没有权限")
    fs.press(FS_STRANGER, fs_buttons(fcards["made"][1])[1][1])
    until(lambda: fs.toasts.count("没有权限") > n, what="别人点按钮没用")
    # 钉钉：回复的写法写在卡片里；中英文都认，别人回的不算
    a = aid["dd", "redo"]
    assert f"“再做一次 #{a}”、“已经做成 #{a}”、“停下任务 #{a}”" in dtexts["redo"]
    dd.say(DD_STRANGER, f"已经做成 #{aid['dd', 'made']}")
    time.sleep(0.5)
    assert all(pending(xsrv, x) for x in aid.values())

    def press(k: str, c: str):
        if k == "tg":
            tg2.press(OWNER, tg_rows(tcards[c])[["redo", "made", "stop"].index(c)][1])
        elif k == "fs":
            fs.press(FS_OWNER, fs_buttons(fcards[c][1])[["redo", "made", "stop"].index(c)][1])
        else:
            dd.say(DD_OWNER, {"redo": f"再做一次 #{aid[k, c]}", "made": f"already done #{aid[k, c]}",
                              "stop": f"Stop task #{aid[k, c]}"}[c])

    for k in ("tg", "fs", "dd"):
        chat = bots[k]["chat_id"]
        # 再做一次的做了第二次
        press(k, "redo")
        res = until(lambda: results(xsrv, chat, tids[k, "redo"]), timeout=20, what=f"{k} 再做一次的结果")
        assert f"收尾了 {k}-redo" in res[0]["content"] and "又做了一次" in res[0]["content"]
        assert f"append-done {k}-redo-2" in lines(log)
        # 已经做成的没再做
        press(k, "made")
        res = until(lambda: results(xsrv, chat, tids[k, "made"]), timeout=20, what=f"{k} 已经做成的结果")
        assert "之前已经做成" in res[0]["content"] and f"append-start {k}-made-2" not in lines(log)
        # 停下任务
        press(k, "stop")
        until(lambda: [m for m in xsrv.find(chat, kind="cancel") if m["meta"]["task_id"] == tids[k, "stop"]], what=f"{k} 任务停下")
        assert xsrv.get(f"/api/tasks/{tids[k, 'stop']}")["task"]["status"] == "cancelled"
        assert f"append-start {k}-stop-2" not in lines(log)
    # 卡片改成结果
    tg_done(tg2, "再做一次")
    tg_done(tg2, "已经做成了，不再做")
    tg_done(tg2, "不做了，任务停下")
    for c, done in (("redo", "再做一次"), ("made", "已经做成了，不再做"), ("stop", "不做了，任务停下")):
        until(lambda: done in fs.done(fcards[c][0]), what=f"飞书 {c} 卡片改成结果")
        until(lambda: any(done in t and f"#{aid['dd', c]}" in t for t in dd.texts(DD_OWNER)), what=f"钉钉 {c} 的结果通知")

    # 推送：有一步要你定，写明是哪一步
    hit = until(lambda: [p for p in pushes(pushsvc, priv, auth)[seen:] if p["tag"] == f"approval-{aid['tg', 'redo']}"],
                what="推送")[0]
    assert hit["title"] == "TgAsk 有一步要你定" and "sa__slow_append" in hit["body"]
    idle(xsrv, timeout=20)
