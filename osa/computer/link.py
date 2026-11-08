"""服务器借用你的 Mac：桌面 App 连着服务器时，在 Mac 上起一个小程序（agent.py），它主动连到这里的 /api/computer/link，
服务器把电脑的操作（Bot 的 Chrome、整个屏幕、工作文件夹里的文件）经这条连接交给它在 Mac 上做（电脑类型 linked，见 linked.py）。

- 借用凭据：电脑面板里点“让服务器上的 Bot 用这台 Mac”，服务器发一个随机令牌，网页交给 App，App 在 Mac 上弹窗问你同意后存进钥匙串。
  这里只存令牌的 SHA-256。在电脑面板或 Mac 的菜单栏里断开就作废。
- 一次只借一台：同一台断线重连时换成新连接；借着一台时另一台连不上（4409），要先在电脑面板里断开。
- 消息是 JSON：服务器发 {"id", "op", "args"}，Mac 回 {"id", "result"} 或 {"id", "error", "kind"}；Mac 连上先发 {"type": "hello", ...}。
  截图、文件用 base64。
"""
import asyncio
import hashlib
import hmac
import itertools
import secrets
import time

from .. import i18n
from ..memory import conn
from .base import ComputerError

conn.execute("""create table if not exists computer_links(
  id integer primary key, name text, token_hash text, created real, last_seen real)""")
conn.commit()

TIMEOUT = 90         # 一个操作最多等这么久（浏览器控制脚本自己最多 90 秒）
MAX_MSG = 64 << 20   # 最大的一条消息：文件最多 32 MB，base64 后再大一些

current: "Link | None" = None
on_change = None     # 连上、断开时：() -> None，computer/__init__.py 设置（通知前端）


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(name: str) -> tuple[int, str]:
    """发一个新的借用令牌，返回 (id, 令牌)。令牌只在这里出现一次。"""
    token = secrets.token_urlsafe(32)
    cur = conn.execute("insert into computer_links(name, token_hash, created, last_seen) values(?,?,?,0)",
                       ((name or "Mac").strip()[:80], _hash(token), time.time()))
    conn.commit()
    return int(cur.lastrowid or 0), token


def check(token: str) -> dict | None:
    if not token:
        return None
    h = _hash(token)
    for i, name, th in conn.execute("select id, name, token_hash from computer_links"):
        if hmac.compare_digest(th, h):
            return {"id": i, "name": name}
    return None


def links() -> list[dict]:
    return [{"id": i, "name": n, "created": c, "last_seen": s, "connected": bool(current and current.id == i)}
            for i, n, c, s in conn.execute("select id, name, created, last_seen from computer_links order by id")]


async def revoke(lid: int):
    conn.execute("delete from computer_links where id=?", (lid,))
    conn.commit()
    if current and current.id == lid:
        await current.close(i18n.t("借用已经断开"))


def connected() -> "Link | None":
    return current


class Link:
    """连上来的一台 Mac。"""

    def __init__(self, ws, row: dict, hello: dict):
        self.ws, self.id = ws, row["id"]
        self.name = str(hello.get("name") or row["name"])[:80]
        self.desktop = bool(hello.get("desktop"))      # 是 Mac，能操作整个屏幕
        self.perms = dict(hello.get("perms") or {})   # 连上时那台 Mac 的两个权限
        self.workspace = str(hello.get("workspace") or "")
        self.version = str(hello.get("version") or "")
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._closed = False

    async def call(self, op: str, timeout: float = TIMEOUT, **args):
        """让 Mac 做一件事，等它的回复。断了、超时都报 ComputerError；Mac 那边的 ValueError 原样报 ValueError（资料库要分辨）。"""
        if self._closed:
            raise ComputerError(i18n.t("你的 Mac 没连上（App 没开、没联网，或者电脑睡着了）"))
        i = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        try:
            await self.ws.send_json({"id": i, "op": op, "args": args})
            r = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise ComputerError(i18n.t("你的 Mac 没有回应（超时）")) from None
        except (RuntimeError, OSError) as e:   # 发的时候连接已经断了
            raise ComputerError(i18n.t("你的 Mac 没连上（App 没开、没联网，或者电脑睡着了）")) from e
        finally:
            self._pending.pop(i, None)
        if "error" in r:
            raise (ValueError if r.get("kind") == "value" else ComputerError)(str(r["error"]))
        return r.get("result")

    def resolve(self, msg: dict):
        fut = self._pending.get(int(msg.get("id") or 0))
        if fut and not fut.done():
            fut.set_result(msg)

    async def close(self, why: str = ""):
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"error": why or i18n.t("你的 Mac 断开了")})
        try:
            await self.ws.close(code=4000 if why else 1000)
        except Exception:
            pass


async def serve(ws):
    """/api/computer/link：Mac 上的小程序连进来。令牌在 Authorization: Bearer 里。"""
    global current
    row = check(ws.headers.get("authorization", "").removeprefix("Bearer ").strip())
    await ws.accept()   # 先接受再关：握手时就拒绝的话，Mac 那边只看到 403，拿不到下面的原因码
    if not row:
        await ws.close(code=4401)   # 令牌不对或者已经作废：Mac 那边隔几分钟才再试
        return
    try:
        hello = await asyncio.wait_for(ws.receive_json(), 20)
    except Exception:
        await ws.close(code=1008)
        return
    if hello.get("type") != "hello":
        await ws.close(code=1008)
        return
    if current and current.id != row["id"]:   # 已经借了另一台：先在电脑面板里断开它
        await ws.close(code=4409)
        return
    link = Link(ws, row, hello)
    old, current = current, link
    if old:   # 同一台断线重连：旧的连接多半已经死了
        await old.close(i18n.t("重新连上了"))
    conn.execute("update computer_links set last_seen=?, name=? where id=?", (time.time(), link.name, link.id))
    conn.commit()
    _changed()
    try:
        while True:
            msg = await ws.receive_json()
            if "id" in msg:
                link.resolve(msg)
    except Exception:
        pass
    finally:
        await link.close()
        if current is link:
            current = None
            conn.execute("update computer_links set last_seen=? where id=?", (time.time(), link.id))
            conn.commit()
        _changed()


def _changed():
    if on_change:
        on_change()
