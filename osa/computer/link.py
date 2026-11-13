"""你的 Mac：连着服务器的桌面 App 可以让服务器上的 Bot 把任务派到它所在的 Mac 上做（任务在哪台电脑上做见 mac.py）。
App 在 Mac 上起一个小程序（agent.py），它主动连到这里的 /api/computer/link，服务器把这个任务的电脑操作（Bot 的 Chrome、
整个屏幕、工作文件夹里的文件，那台 Mac 打开了终端的话还有终端）经这条连接交给它在 Mac 上做。

- 凭据：在 Mac 的 App 里点“允许派任务到这台 Mac”，服务器发一个随机令牌，网页交给 App，App 在 Mac 上弹窗问你同意后存进钥匙串。
  这里只存令牌的 SHA-256。在电脑面板里移除、或者在 Mac 的菜单栏里停止，令牌就作废。
- 可以同时连好几台（你在几台电脑上都开着 App）；同一台断线重连时换成新连接。
- 每台一行 computer_links：名字（那台 Mac 的电脑名，连上时更新）、终端开没开（shell，默认关，电脑面板里每台单独开）。
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
if "shell" not in {r[1] for r in conn.execute("pragma table_info(computer_links)")}:
    conn.execute("alter table computer_links add column shell integer not null default 0")
conn.commit()

TIMEOUT = 90         # 一个操作最多等这么久（浏览器控制脚本自己最多 90 秒）
MAX_MSG = 64 << 20   # 最大的一条消息：文件最多 32 MB，base64 后再大一些
OFFLINE = i18n.N_("「{name}」没连上（那台 Mac 上的 App 没开、没联网，或者电脑睡着了）")

online: dict[int, "Link"] = {}   # 连着的：令牌编号 -> 连接
on_change = None     # 连上、断开、改了设置时：() -> None，computer/__init__.py 设置（通知前端）


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(name: str) -> tuple[int, str]:
    """发一个新的令牌，返回 (编号, 令牌)。令牌只在这里出现一次。"""
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


def rows() -> list[dict]:
    """登记过的 Mac（连没连着都在）。名字重了的后面加编号，派任务时按名字找得到。"""
    rs = [{"id": i, "name": n or "Mac", "created": c, "last_seen": s, "shell": bool(sh), "connected": i in online}
          for i, n, c, s, sh in conn.execute("select id, name, created, last_seen, shell from computer_links order by id")]
    names = [r["name"] for r in rs]
    for r in rs:
        if names.count(r["name"]) > 1:
            r["name"] = f"{r['name']} #{r['id']}"
    return rs


def row(lid: int) -> dict | None:
    return next((r for r in rows() if r["id"] == lid), None)


def find(name: str) -> dict | None:
    """派任务时按名字找那台 Mac：名字一样（不分大小写、去掉两头空格），或者写的是 #编号。"""
    key = (name or "").strip().lower()
    for r in rows():
        if r["name"].lower() == key or key == f"#{r['id']}":
            return r
    return None


def set_shell(lid: int, on: bool):
    conn.execute("update computer_links set shell=? where id=?", (int(bool(on)), lid))
    conn.commit()
    _changed()


async def revoke(lid: int):
    conn.execute("delete from computer_links where id=?", (lid,))
    conn.commit()
    if lk := online.get(lid):
        await lk.close(i18n.t("这台 Mac 已经移除"))
    _changed()


def connected(lid: int) -> "Link | None":
    return online.get(lid)


class Link:
    """连上来的一台 Mac。"""

    def __init__(self, ws, row: dict, hello: dict):
        self.ws, self.id = ws, row["id"]
        self.name = str(hello.get("name") or row["name"])[:80]
        self.desktop = bool(hello.get("desktop"))      # 是 Mac，能操作整个屏幕
        self.perms = dict(hello.get("perms") or {})   # 连上时那台 Mac 的两个权限
        self.workspace = str(hello.get("workspace") or "")
        self.can_shell = bool(hello.get("shell"))     # 那台 Mac 上的小程序能执行终端命令（旧版 App 的不能）
        self.can_range = bool(hello.get("ranges"))    # 能按段读文件（下载、播放视频用）；旧版只能整个读
        self.version = str(hello.get("version") or "")
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._closed = False

    async def call(self, op: str, timeout: float = TIMEOUT, **args):
        """让 Mac 做一件事，等它的回复。断了、超时都报 ComputerError；Mac 那边的 ValueError 原样报 ValueError（资料库要分辨）。"""
        if self._closed:
            raise ComputerError(i18n.t(OFFLINE, name=self.name))
        i = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        try:
            await self.ws.send_json({"id": i, "op": op, "args": args})
            r = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise ComputerError(i18n.t("「{name}」没有回应（超时）", name=self.name)) from None
        except (RuntimeError, OSError) as e:   # 发的时候连接已经断了
            raise ComputerError(i18n.t(OFFLINE, name=self.name)) from e
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
                fut.set_result({"error": why or i18n.t(OFFLINE, name=self.name)})
        try:
            await self.ws.close(code=4000 if why else 1000)
        except Exception:
            pass


async def serve(ws):
    """/api/computer/link：Mac 上的小程序连进来。令牌在 Authorization: Bearer 里。"""
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
    lk = Link(ws, row, hello)
    old, online[lk.id] = online.get(lk.id), lk
    if old:   # 同一台断线重连：旧的连接多半已经死了
        await old.close(i18n.t("重新连上了"))
    conn.execute("update computer_links set last_seen=?, name=? where id=?", (time.time(), lk.name, lk.id))
    conn.commit()
    _changed()
    try:
        while True:
            msg = await ws.receive_json()
            if "id" in msg:
                lk.resolve(msg)
    except Exception:
        pass
    finally:
        await lk.close()
        if online.get(lk.id) is lk:
            del online[lk.id]
            conn.execute("update computer_links set last_seen=? where id=?", (time.time(), lk.id))
            conn.commit()
        _changed()


def _changed():
    if on_change:
        on_change()
