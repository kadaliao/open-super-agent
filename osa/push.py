"""手机推送（Web Push）：没人开着页面时，需要你处理的事推送到已订阅的设备。

- VAPID 密钥第一次用时生成，私钥存在数据库 settings 表里；公钥交给浏览器订阅用。
- 消息按 RFC 8291（aes128gcm）加密，VAPID 按 RFC 8292 签名，只用 cryptography 和 httpx。
- 推送什么：等你确认 / 请你帮忙、任务做完、Bot 的回复、演示录制自动结束。只有在没有任何页面正被看着时才推。
- 推送服务返回 404 / 410 的订阅已经失效，直接删掉。
"""
import asyncio
import base64
import json
import os
import re
import time
from urllib.parse import urlparse
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from . import i18n
from .config import CFG
from .memory import conn

conn.execute("""create table if not exists push_subs(
  endpoint text primary key, p256dh text, auth text, ua text default '', created real)""")
conn.commit()

SUBJECT = (CFG.get("push") or {}).get("subject") or "https://github.com/kadaliao/open-super-agent"
_key: ec.EllipticCurvePrivateKey | None = None


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _point(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_key() -> ec.EllipticCurvePrivateKey:
    global _key
    if _key:
        return _key
    r = conn.execute("select value from settings where key='vapid'").fetchone()
    if r:
        _key = serialization.load_pem_private_key(r[0].encode(), None)
    else:
        _key = ec.generate_private_key(ec.SECP256R1())
        pem = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        conn.execute("insert or replace into settings values('vapid', ?)", (pem.decode(),))
        conn.commit()
    return _key


def public_key() -> str:
    """给浏览器 pushManager.subscribe 的 applicationServerKey。"""
    return b64u(_point(vapid_key().public_key()))


def vapid_header(endpoint: str) -> str:
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": SUBJECT}).encode())
    r, s = decode_dss_signature(vapid_key().sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={public_key()}"


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291：用订阅的公钥和 auth 加密，返回 aes128gcm 格式的请求体（单条记录）。"""
    ua_pub_bytes = unb64u(p256dh)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = _point(eph.public_key())
    secret = eph.exchange(ec.ECDH(), ua_pub)
    ikm = HKDF(hashes.SHA256(), 32, unb64u(auth), b"WebPush: info\x00" + ua_pub_bytes + as_pub).derive(secret)
    salt = os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)    # \x02：最后一条记录，不加填充
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + body


# ---------- 订阅 ----------
def subscribe(sub: dict, ua: str = "") -> int:
    ep, keys = sub.get("endpoint", ""), sub.get("keys") or {}
    if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError(i18n.t("订阅信息不完整"))
    conn.execute("insert or replace into push_subs values(?,?,?,?,?)", (ep, keys["p256dh"], keys["auth"], ua[:200], time.time()))
    conn.commit()
    return count()


def unsubscribe(endpoint: str):
    conn.execute("delete from push_subs where endpoint=?", (endpoint,))
    conn.commit()


def count() -> int:
    return conn.execute("select count(*) from push_subs").fetchone()[0]


async def _send_one(c: httpx.AsyncClient, ep: str, p256dh: str, auth: str, data: bytes, urgency: str) -> str:
    try:
        r = await c.post(ep, content=encrypt(data, p256dh, auth), headers={
            "Authorization": vapid_header(ep), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
            "TTL": "86400", "Urgency": urgency})
    except httpx.HTTPError as e:
        return i18n.t("发送失败：{e}", e=e)
    if r.status_code in (404, 410):
        unsubscribe(ep)
        return i18n.t("订阅已失效，已删除")
    return "ok" if r.status_code < 300 else i18n.t("推送服务返回 {code}：{text}", code=r.status_code, text=r.text[:200])


async def send(title: str, body: str, url: str = "/", tag: str = "", urgency: str = "normal") -> list[str]:
    subs = conn.execute("select endpoint, p256dh, auth from push_subs").fetchall()
    if not subs:
        return []
    data = json.dumps({"title": title, "body": body[:200], "url": url, "tag": tag}, ensure_ascii=False).encode()
    async with httpx.AsyncClient(timeout=15) as c:
        return list(await asyncio.gather(*[_send_one(c, ep, k, a, data, urgency) for ep, k, a in subs]))


# ---------- 什么时候推 ----------
def _bot_name(bot_id) -> str:
    r = conn.execute("select name from bots where id=?", (bot_id,)).fetchone() if bot_id else None
    return r[0] if r else "Bot"


def _plain(text: str) -> str:
    text = re.sub(r"^\[[^\]]*\]\s*", "", text or "")
    return re.sub(r"[#*`>|_~]+", "", text).replace("\n", " ").strip()


def message_for(event: dict) -> tuple[str, str, str, str] | None:
    """把一个界面事件翻译成 (标题, 正文, 打开的地址, 合并用的 tag)；不需要推的返回 None。"""
    t, cid = event.get("type"), event.get("chat_id")
    url = f"/?chat={cid}" if cid else "/"
    if t == "approval" and event.get("status") == "pending":
        who = _bot_name(event.get("bot_id"))
        if event.get("kind") == "help":
            return i18n.t("{who} 需要你帮忙", who=who), i18n.t("任务暂停了，等你接手电脑处理"), url, f"approval-{event['id']}"
        return i18n.t("{who} 需要你确认", who=who), i18n.t("{tool}：{goal}", tool=event.get("tool", ""), goal=event.get("goal", "")), \
            url, f"approval-{event['id']}"
    if t == "message":
        m = event.get("message") or {}
        kind = (m.get("meta") or {}).get("kind")
        if m.get("role") == "assistant" and m.get("content"):
            return _bot_name(m.get("bot_id")), _plain(m["content"])[:120], url, f"chat-{cid}"
        if m.get("role") == "task" and kind == "result":
            ok = (m.get("meta") or {}).get("status") == "done"
            who = _bot_name(m.get("bot_id"))
            return (i18n.t("{who} 做完了任务", who=who) if ok else i18n.t("{who} 的任务失败了", who=who)), \
                _plain(m["content"])[:120], url, f"chat-{cid}"
    if t == "teach" and event.get("draft"):
        return i18n.t("演示录制已结束"), i18n.t("技能草稿「{name}」等你审核", name=event["draft"]["name"]), "/", "teach"
    return None
