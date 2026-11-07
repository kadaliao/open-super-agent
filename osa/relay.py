"""手机 App 的推送中转：自己部署的服务器拿不到我们的苹果推送密钥，推送经这里转给苹果（APNs）。

对服务器来说它就是一个网页推送服务（RFC 8030）：
- App 在这里登记推送令牌，拿到一个地址 https://<中转>/w/<id>，连同自己生成的公钥交给服务器的 /api/push/subscribe。
- 服务器照常按 RFC 8291 加密、按 RFC 8292（VAPID）签名后发到这个地址（osa/push.py，不用改）。
- 这里验签名（只认登记时那台服务器的公钥）、限速，把密文原样转给苹果；App 的通知扩展解密后显示。
- 看不到内容。看得到的：推送令牌、手机和服务器的 IP、服务器的公钥、推送的时间和大小。不写访问日志。

单独运行：uvicorn osa.relay:app --port 8790 --no-access-log --proxy-headers。不导入 osa 的其他模块（它们一导入就打开服务的数据库）。
环境变量：
  OSA_RELAY_ORIGIN  对外地址，比如 https://push.cclsp.top（校验 VAPID 的 aud、拼订阅地址）
  OSA_RELAY_DB      SQLite 文件，默认 data/relay.db
  APNS_KEY_FILE、APNS_KEY_ID、APNS_TEAM_ID  苹果推送密钥（.p8）、它的 Key ID、开发者团队 ID
  APNS_TOPIC        App 的 bundle id
"""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

ORIGIN = os.environ.get("OSA_RELAY_ORIGIN", "https://push.cclsp.top").rstrip("/")
DB_PATH = os.environ.get("OSA_RELAY_DB") or str(Path(__file__).resolve().parent.parent / "data" / "relay.db")
KEY_FILE = os.environ.get("APNS_KEY_FILE", "")
KEY_ID = os.environ.get("APNS_KEY_ID", "")
TEAM_ID = os.environ.get("APNS_TEAM_ID", "HMDAVG9B8J")
TOPIC = os.environ.get("APNS_TOPIC", "com.kadaliao.opensuperagent")
APNS_HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}

MAX_BODY = 2900            # 苹果推送的内容最多 4096 字节，密文转成 base64 会大三分之一，还要留出 JSON 的外壳
MAX_TTL = 28 * 86400
PER_MINUTE, PER_DAY = 60, 2000        # 每个地址
IP_PER_MINUTE = 600                   # 每个来源 IP 发推送
REG_PER_MINUTE, REG_PER_DAY = 10, 100  # 每个 IP 登记地址
MAX_PER_TOKEN = 20                    # 一台手机最多登记几台服务器
STALE = 90 * 86400                    # 这么久没成功推过的地址清掉
_transport: httpx.AsyncBaseTransport | None = None   # 测试时换成假的苹果服务
_db: sqlite3.Connection | None = None


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.execute("""create table if not exists endpoints(
          id text primary key, secret_hash text, token text, env text, vapid text,
          created real, last_ok real default 0, fails integer default 0)""")
        _db.execute("create index if not exists endpoints_token on endpoints(token)")
        _db.commit()
    return _db


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _cleanup():
    cut = time.time() - STALE
    db().execute("delete from endpoints where max(created, last_ok) < ?", (cut,))
    db().commit()


# ---------- 限速 ----------
class Limiter:
    """每个键每分钟、每天最多多少次。只放在内存里：中转重启后重新计数。"""

    def __init__(self):
        self.minute: dict[str, list[float]] = {}
        self.day: dict[str, tuple[int, int]] = {}

    def hit(self, key: str, per_minute: int, per_day: int = 0) -> bool:
        now = time.time()
        if len(self.minute) > 50000:
            self.minute = {k: v for k, v in self.minute.items() if v and now - v[-1] < 60}
        recent = [t for t in self.minute.get(key, []) if now - t < 60]
        if len(recent) >= per_minute:
            self.minute[key] = recent
            return False
        if per_day:
            today = int(now // 86400)
            d, n = self.day.get(key, (today, 0))
            n = n if d == today else 0
            if n >= per_day:
                return False
            self.day[key] = (today, n + 1)
        self.minute[key] = recent + [now]
        return True


_limits = Limiter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# ---------- VAPID（RFC 8292） ----------
def verify_vapid(header: str, want_key: str) -> tuple[int, str] | None:
    """校验服务器的签名。没问题返回 None，否则返回 (状态码, 原因)。want_key：登记时那台服务器的公钥。"""
    m = re.fullmatch(r"\s*vapid\s+(.+)", header or "", re.I)
    if not m:
        return 401, "missing vapid authorization"
    params = dict(re.findall(r"(\w+)\s*=\s*([^,\s]+)", m.group(1)))
    token, k = params.get("t", ""), params.get("k", "")
    try:
        key = unb64u(k)
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)
        head_b64, claims_b64, sig_b64 = token.split(".")
        head, claims, sig = json.loads(unb64u(head_b64)), json.loads(unb64u(claims_b64)), unb64u(sig_b64)
    except (ValueError, TypeError):
        return 401, "malformed vapid token"
    if not hmac.compare_digest(key, unb64u(want_key)):
        return 403, "key does not match this endpoint"
    if head.get("alg") != "ES256" or len(sig) != 64:
        return 401, "vapid token must be ES256"
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        pub.verify(der, f"{head_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return 401, "bad vapid signature"
    now = time.time()
    if claims.get("aud") != ORIGIN:
        return 401, "wrong audience"
    if not isinstance(claims.get("exp"), (int, float)) or not now < claims["exp"] <= now + 86400:
        return 401, "vapid token expired or too long-lived"
    if not claims.get("sub"):
        return 401, "vapid token has no subject"
    return None


# ---------- 苹果推送 ----------
class Apns:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._jwt, self._jwt_at = "", 0.0

    def client(self) -> httpx.AsyncClient:
        if self._client is None:   # 苹果只认 HTTP/2；一条长连接一直用
            self._client = httpx.AsyncClient(http2=_transport is None, transport=_transport, timeout=15)
        return self._client

    def token(self, fresh: bool = False) -> str:
        """给苹果的令牌（JWT），40 分钟换一次：苹果要求 20 到 60 分钟之间，换得太勤会被拒。"""
        if fresh or not self._jwt or time.time() - self._jwt_at > 40 * 60:
            key = serialization.load_pem_private_key(Path(KEY_FILE).read_bytes(), None)
            head = b64u(json.dumps({"alg": "ES256", "kid": KEY_ID}).encode())
            claims = b64u(json.dumps({"iss": TEAM_ID, "iat": int(time.time())}).encode())
            r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
            self._jwt, self._jwt_at = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}", time.time()
        return self._jwt

    async def send(self, device: str, env: str, payload: bytes, headers: dict) -> tuple[int, str]:
        """返回 (状态码, 苹果给的原因)。"""
        for attempt in range(2):
            try:
                auth = f"bearer {self.token(fresh=attempt > 0)}"
            except (OSError, ValueError):   # 推送密钥没配好
                return 502, "apns key not configured"
            try:
                r = await self.client().post(f"{APNS_HOSTS[env]}/3/device/{device}", content=payload, headers={**headers, "authorization": auth})
            except httpx.HTTPError as e:
                return 502, type(e).__name__
            reason = ""
            if r.status_code != 200:
                try:
                    reason = r.json().get("reason", "")
                except ValueError:
                    pass
            if r.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken") and attempt == 0:
                continue
            return r.status_code, reason
        return 502, "provider token rejected"


apns = Apns()


# ---------- 接口 ----------
async def register(request: Request):
    """App 登记：{token, env, vapid} → {id, secret, endpoint}。secret 只返回这一次，改和删都要带上。"""
    if not _limits.hit("reg:" + _ip(request), REG_PER_MINUTE, REG_PER_DAY):
        return JSONResponse({"detail": "too many registrations"}, 429)
    try:
        data = await request.json()
        fields = _fields(data, need=("token", "env", "vapid"))
    except (ValueError, TypeError) as e:
        return JSONResponse({"detail": str(e)}, 400)
    if db().execute("select count(*) from endpoints where token=?", (fields["token"],)).fetchone()[0] >= MAX_PER_TOKEN:
        return JSONResponse({"detail": "too many endpoints for this device"}, 429)
    _cleanup()
    eid, secret = b64u(secrets.token_bytes(16)), secrets.token_urlsafe(32)
    db().execute("insert into endpoints(id, secret_hash, token, env, vapid, created) values(?,?,?,?,?,?)",
                 (eid, _hash(secret), fields["token"], fields["env"], fields["vapid"], time.time()))
    db().commit()
    return JSONResponse({"id": eid, "secret": secret, "endpoint": f"{ORIGIN}/w/{eid}"}, 201)


def _fields(data: dict, need: tuple = ()) -> dict:
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    out = {}
    if "token" in data or "token" in need:
        t = str(data.get("token", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64,200}", t):
            raise ValueError("bad device token")
        out["token"] = t
    if "env" in data or "env" in need:
        if data.get("env") not in APNS_HOSTS:
            raise ValueError("env must be production or sandbox")
        out["env"] = data["env"]
    if "vapid" in data or "vapid" in need:
        v = str(data.get("vapid", ""))
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(v))
        except (ValueError, TypeError):
            raise ValueError("bad vapid key") from None
        out["vapid"] = b64u(unb64u(v))
    return out


def _owned(request: Request) -> str | None:
    """带对了 secret 的地址 id；不对返回 None。"""
    eid = request.path_params["eid"]
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    row = db().execute("select secret_hash from endpoints where id=?", (eid,)).fetchone()
    if not row or not given or not hmac.compare_digest(row[0], _hash(given)):
        return None
    return eid


async def update(request: Request):
    """App 每次启动都调一下：令牌、环境、服务器公钥变了就改。地址不存在（比如中转的数据丢了）回 404，App 重新登记。"""
    if not db().execute("select 1 from endpoints where id=?", (request.path_params["eid"],)).fetchone():
        return JSONResponse({"detail": "unknown endpoint"}, 404)
    if not (eid := _owned(request)):
        return JSONResponse({"detail": "wrong secret"}, 401)
    try:
        fields = _fields(await request.json())
    except (ValueError, TypeError) as e:
        return JSONResponse({"detail": str(e)}, 400)
    for k, v in fields.items():
        db().execute(f"update endpoints set {k}=?, fails=0 where id=?", (v, eid))   # k 只会是上面三个字段名
    db().commit()
    return JSONResponse({"id": eid, "endpoint": f"{ORIGIN}/w/{eid}"})


async def remove(request: Request):
    if not db().execute("select 1 from endpoints where id=?", (request.path_params["eid"],)).fetchone():
        return Response(status_code=204)
    if not (eid := _owned(request)):
        return JSONResponse({"detail": "wrong secret"}, 401)
    db().execute("delete from endpoints where id=?", (eid,))
    db().commit()
    return Response(status_code=204)


async def push(request: Request):
    """服务器发来的一条网页推送：验签名、限速，转给苹果。"""
    eid = request.path_params["eid"]
    row = db().execute("select token, env, vapid from endpoints where id=?", (eid,)).fetchone()
    if not row:
        return JSONResponse({"detail": "unknown endpoint"}, 404)
    device, env, vapid = row
    if bad := verify_vapid(request.headers.get("authorization", ""), vapid):
        return JSONResponse({"detail": bad[1]}, bad[0])
    if request.headers.get("content-encoding", "").lower() != "aes128gcm":
        return JSONResponse({"detail": "content-encoding must be aes128gcm"}, 400)
    try:
        ttl = min(int(request.headers["ttl"]), MAX_TTL)
        assert ttl >= 0
    except (KeyError, ValueError, AssertionError):
        return JSONResponse({"detail": "missing or bad TTL"}, 400)
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return JSONResponse({"detail": "payload too large"}, 413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "payload too large"}, 413)
    if len(body) < 21 + 65 + 17 or body[20] != 65:   # aes128gcm 的头：salt、记录大小、65 字节的公钥，再加至少一个分组
        return JSONResponse({"detail": "not an aes128gcm message"}, 400)
    if not _limits.hit("ip:" + _ip(request), IP_PER_MINUTE) or not _limits.hit("ep:" + eid, PER_MINUTE, PER_DAY):
        return JSONResponse({"detail": "too many pushes"}, 429, headers={"retry-after": "60"})

    headers = {"apns-push-type": "alert", "apns-topic": TOPIC,
               "apns-priority": "5" if request.headers.get("urgency", "normal").lower() in ("low", "very-low") else "10",
               "apns-expiration": str(int(time.time()) + ttl if ttl else 0)}
    if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", topic := request.headers.get("topic", "")):
        headers["apns-collapse-id"] = topic   # 同一个对话的新通知替换旧的
    # 解密失败时显示占位文案（App 里的 PUSH_PLACEHOLDER，“你有一条新通知”）；e 是密文，s 告诉通知扩展用哪把钥匙
    payload = json.dumps({"aps": {"alert": {"loc-key": "PUSH_PLACEHOLDER"}, "mutable-content": 1, "sound": "default"},
                          "e": base64.b64encode(body).decode(), "s": eid}, separators=(",", ":")).encode()
    status, reason = await apns.send(device, env, payload, headers)
    if status == 400 and reason == "BadDeviceToken":   # 开发版和正式版的令牌不通用：换另一个环境试一次，成了就记住
        other = "sandbox" if env == "production" else "production"
        status, reason = await apns.send(device, other, payload, headers)
        if status == 200:
            db().execute("update endpoints set env=? where id=?", (other, eid))
    if status == 200:
        db().execute("update endpoints set last_ok=?, fails=0 where id=?", (time.time(), eid))
        db().commit()
        return Response(status_code=201, headers={"location": f"{ORIGIN}/m/{b64u(secrets.token_bytes(12))}"})
    if status == 410 or reason in ("BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"):
        db().execute("delete from endpoints where id=?", (eid,))
        db().commit()
        return JSONResponse({"detail": reason or "device gone"}, 410)   # 服务器收到 410 会删掉这个订阅
    db().execute("update endpoints set fails=fails+1 where id=?", (eid,))
    db().commit()
    if status in (413, 429):
        return JSONResponse({"detail": reason}, status)
    return JSONResponse({"detail": f"apns {status} {reason}".strip()}, 502)


async def health(request: Request):
    return JSONResponse({"ok": True, "apns": bool(KEY_FILE and KEY_ID and Path(KEY_FILE).exists()),
                         "http2": importlib.util.find_spec("h2") is not None})


app = Starlette(routes=[
    Route("/v1/endpoints", register, methods=["POST"]),
    Route("/v1/endpoints/{eid}", update, methods=["PUT"]),
    Route("/v1/endpoints/{eid}", remove, methods=["DELETE"]),
    Route("/w/{eid}", push, methods=["POST"]),
    Route("/healthz", health, methods=["GET"]),
])
