"""企业微信：智能机器人的长连接（API 模式选“长连接”），不需要公网地址。

- 协议照官方 SDK（@wecom/aibot-node-sdk、wecom-aibot-sdk）和文档：连上 wss://openws.work.weixin.qq.com 后发
  aibot_subscribe（Bot ID + Secret）认证，每 30 秒发一次 ping；帧都是 JSON {cmd, headers:{req_id}, body}，
  我们发的每一帧都有一个带同样 req_id、写着 errcode 的回执。
  同一个机器人只能有一处连着：别处连上后，这边会收到 disconnected_event 然后被断开。
- 收：aibot_msg_callback，只要单聊（chattype=single），发消息的人是 from.userid。
  图片、文件、视频是加密的下载地址（5 分钟有效），用消息里各自的 aeskey 按 AES-256-CBC 解密；
  文件名只在下载响应的 Content-Disposition 里。语音消息带着企业微信转好的文字。
- 发：都用 aibot_send_msg 主动推送（用户先发过消息才能推；每个会话 30 条/分钟）。普通回复是 markdown；
  确认请求是按钮交互卡片（button_interaction），按钮的 key 是 ap:<确认请求 id>:<动作>。点按钮收到 template_card_event，
  5 秒内用 aibot_respond_update_msg（带着事件的 req_id）把卡片改成结果。在网页上处理的确认改不了卡片，发一条文字说明结果。
  卡片不支持 markdown，详细内容太长时先单独发一条。
"""
import asyncio
import base64
import json
import os
import random
import re
import secrets
import time
from collections import deque
from urllib.parse import unquote
import httpx
import websockets
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .. import i18n
from . import Channel, fmt, word_command

WS_URL = os.environ.get("OSA_WECOM_WS", "wss://openws.work.weixin.qq.com")   # 测试时换成假的服务器
PING_EVERY = 30
CARD_TEXT = 110   # 卡片副标题建议不超过 112 个字


class WecomError(Exception):
    def __init__(self, code, msg: str):
        super().__init__(f"{code} {msg}")
        self.code, self.msg = code, msg


def _req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def decrypt(data: bytes, aeskey: str) -> bytes:
    """AES-256-CBC，IV 是密钥的前 16 字节，PKCS#7 按 32 字节补齐。"""
    key = base64.b64decode(aeskey + "=" * (-len(aeskey) % 4))
    dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    out = dec.update(data) + dec.finalize()
    pad = out[-1] if out else 0
    if not 1 <= pad <= 32 or out[-pad:] != bytes([pad]) * pad:
        raise ValueError(i18n.t("文件解密失败"))
    return out[:-pad]


def _filename(disposition: str) -> str:
    if m := re.search(r"filename\*=UTF-8''([^;\s]+)", disposition, re.I):
        return unquote(m[1])
    if m := re.search(r'filename="?([^";]+)"?', disposition, re.I):
        return unquote(m[1]).strip()
    return ""


async def _subscribe(ws, bot_id: str, secret: str, timeout: float = 10):
    """认证；不对就抛 WecomError。"""
    rid = _req_id("aibot_subscribe")
    await ws.send(json.dumps({"cmd": "aibot_subscribe", "headers": {"req_id": rid},
                              "body": {"bot_id": bot_id, "secret": secret}}))
    end = time.time() + timeout
    while True:
        f = json.loads(await asyncio.wait_for(ws.recv(), max(end - time.time(), 0.1)))
        if (f.get("headers") or {}).get("req_id") == rid:
            if f.get("errcode"):
                raise WecomError(f.get("errcode"), f.get("errmsg") or "")
            return


class WeCom(Channel):
    kind, title, max_len = "wecom", i18n.N_("企业微信"), 6000

    def __init__(self, r: dict, secret: str):
        super().__init__(r, secret)
        self.http = httpx.AsyncClient(timeout=120)
        self.ws = None
        self._acks: dict[str, asyncio.Future] = {}
        self._seen: deque[str] = deque(maxlen=500)
        self._events: dict[str, tuple[str, float]] = {}   # 卡片 task_id -> (点按钮事件的 req_id, 收到的时间)
        self._missed = 0

    async def aclose(self):
        await self.http.aclose()

    @property
    def bot_id(self) -> str:
        return self.row["config"]["bot_id"]

    # ----- 连接 -----
    @classmethod
    async def check(cls, fields: dict) -> dict:
        bot_id, secret = (fields.get("bot_id") or "").strip(), (fields.get("secret") or "").strip()
        if not bot_id or not secret:
            raise ValueError(i18n.t("请填 Bot ID 和 Secret，在智能机器人的配置页里，API 模式选“长连接”后能看到"))
        try:
            async with websockets.connect(WS_URL, proxy=None, open_timeout=15) as ws:
                await _subscribe(ws, bot_id, secret)
        except WecomError as e:
            raise ValueError(i18n.t("Bot ID 或 Secret 不对，或者机器人的 API 模式没选“长连接”（{error}）", error=e.msg or e.code))
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            raise ValueError(i18n.t("连不上企业微信（{error}）", error=type(e).__name__))
        name = (fields.get("name") or "").strip()[:40] or bot_id
        return {"name": name, "config": {"bot_id": bot_id}, "secret": secret}

    @classmethod
    def same(cls, r: dict, info: dict) -> bool:
        return r["config"].get("bot_id") == info["config"]["bot_id"]

    async def run(self):
        backoff = 5
        while True:
            pause = 0
            try:
                async with websockets.connect(WS_URL, proxy=None, max_size=None, ping_interval=None) as ws:
                    await _subscribe(ws, self.bot_id, self.secret)
                    self.ws, self._missed = ws, 0
                    self.set_state("connected")
                    backoff = 5
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            if await self._frame(json.loads(raw)) == "kicked":
                                pause = 120
                                break
                    finally:
                        pinger.cancel()
                        self.ws = None
                        for fut in self._acks.values():
                            if not fut.done():
                                fut.set_exception(ConnectionError("长连接断了"))
                        self._acks.clear()
                if pause:
                    self.set_state("error", i18n.later("这个机器人在别处也连上了（比如另一台服务器上的 open-super-agent），同一个机器人只能一处用，2 分钟后重试"))
                else:
                    self.set_state("error", i18n.later("长连接断了，正在重连"))
            except WecomError as e:
                self.set_state("error", i18n.later("企业微信拒绝了连接（{error}）。Secret 重置过或者机器人改成了回调模式的话，删掉这个连接重新添加",
                                               error=e.msg or e.code))
                pause = 300
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException, ValueError) as e:
                self.set_state("error", i18n.later("连接企业微信失败（{error}），稍后自动重试", error=type(e).__name__))
            await asyncio.sleep(pause or backoff + random.random() * 5)
            backoff = min(backoff * 2, 120)

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(PING_EVERY)
            if self._missed >= 2:   # 连着两次没回执：连接已经死了
                await ws.close()
                return
            self._missed += 1
            await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": _req_id("ping")}}))

    async def request(self, cmd: str, body: dict, req_id: str = "") -> dict:
        """发一帧并等回执。"""
        if not self.ws:
            raise ConnectionError("还没连上企业微信")
        rid = req_id or _req_id(cmd)
        fut = asyncio.get_running_loop().create_future()
        self._acks[rid] = fut
        try:
            await self.ws.send(json.dumps({"cmd": cmd, "headers": {"req_id": rid}, "body": body}, ensure_ascii=False))
            f = await asyncio.wait_for(fut, 10)
        finally:
            self._acks.pop(rid, None)
        if f.get("errcode"):
            raise WecomError(f.get("errcode"), f.get("errmsg") or "")
        return f

    async def _frame(self, f: dict):
        cmd, rid = f.get("cmd"), (f.get("headers") or {}).get("req_id", "")
        if not cmd:   # 回执
            if rid.startswith("ping"):
                self._missed = 0
            elif fut := self._acks.get(rid):
                if not fut.done():
                    fut.set_result(f)
            return None
        body = f.get("body") or {}
        if body.get("msgid"):
            if body["msgid"] in self._seen:
                return None
            self._seen.append(body["msgid"])
        if cmd == "aibot_msg_callback":
            asyncio.create_task(self._safe(self.on_message(body)))
        elif cmd == "aibot_event_callback":
            ev = body.get("event") or {}
            et = ev.get("eventtype")
            if et == "disconnected_event":
                return "kicked"
            if et == "template_card_event":
                asyncio.create_task(self._safe(self.on_card(rid, body, ev.get("template_card_event") or ev)))
            elif et == "enter_chat" and not self.row["peer"]:
                asyncio.create_task(self._safe(self.request("aibot_respond_welcome_msg", {
                    "msgtype": "text", "text": {"content": i18n.t("你好！把 open-super-agent 网页上 设置 → 聊天软件 里的 6 位配对码发给我，就能开始用了。")}},
                    rid)))
        return None

    # ----- 收 -----
    async def on_message(self, b: dict):
        if b.get("chattype") != "single":
            return
        peer = (b.get("from") or {}).get("userid", "")
        mt, n = b.get("msgtype"), (b.get("msgid") or "")[-10:]
        text, files = "", []
        if mt == "text":
            text = (b.get("text") or {}).get("content", "")
        elif mt == "voice":
            text = (b.get("voice") or {}).get("content", "")
            if not text:
                if peer == self.row["peer"]:
                    self.say(i18n.t("没听清这条语音，请再说一遍或者打字。"))
                return
        elif mt == "mixed":
            parts = []
            for i, it in enumerate((b.get("mixed") or {}).get("msg_item") or []):
                if it.get("msgtype") == "text":
                    parts.append((it.get("text") or {}).get("content", ""))
                elif it.get("msgtype") == "image" and (it.get("image") or {}).get("url"):
                    files.append((f"image_{n}_{i}.jpg", it["image"]))
            text = "\n".join(p for p in parts if p)
        elif mt in ("image", "file", "video"):
            item = b.get(mt) or {}
            if item.get("url"):
                files.append(({"image": f"image_{n}.jpg", "video": f"video_{n}.mp4"}.get(mt, f"file_{n}"), item))
        elif peer == self.row["peer"]:
            self.say(i18n.t("暂时只能收文字、语音、图片和文件。"))
            return
        await self.inbound(peer, "", text, files)

    async def download(self, item: dict) -> tuple[bytes, str]:
        r = await self.http.get(item["url"])
        r.raise_for_status()
        data = decrypt(r.content, item["aeskey"]) if item.get("aeskey") else r.content
        return data, _filename(r.headers.get("content-disposition", ""))

    async def on_card(self, rid: str, b: dict, ev: dict):
        task_id = ev.get("task_id") or ""
        self._events[task_id] = (rid, time.time())
        for k, (_, t) in list(self._events.items()):
            if t < time.time() - 60:
                self._events.pop(k, None)
        ok, toast = await self._press((b.get("from") or {}).get("userid", ""), ev.get("event_key") or "")
        if not ok:
            self._events.pop(task_id, None)
            self.say(toast)

    # ----- 发 -----
    async def send_text(self, peer: str, md: str):
        await self.request("aibot_send_msg", {"chatid": peer, "chat_type": 1, "msgtype": "markdown",
                                              "markdown": {"content": md}})

    def _card(self, card: dict, task_id: str) -> dict:
        desc = fmt.plain(card["body"])
        desc = re.sub(r"```\w*\n?|`", "", desc)
        desc = re.sub(r"\s*\n\s*", " ｜ ", desc).strip(" ｜")
        more = i18n.t("…（详情见上条）")
        if len(desc) > CARD_TEXT:
            desc = desc[:CARD_TEXT - len(more)] + more
        zh = i18n.lang() == "zh"   # 长度建议是按中文字算的，英文放宽一些
        title = re.sub(r"^\W+", "", card["title"])[:26 if zh else 52]
        btn = 10 if zh else 24
        if card.get("done"):
            buttons = [{"text": card["done"].lstrip("✓✕ ")[:btn], "style": 2, "key": f"ap:{card['id']}:over"}]
        else:
            buttons = [{"text": label[:btn], "style": 1 if style == "primary" else 2, "key": f"ap:{card['id']}:{act}"}
                       for label, act, style in card["buttons"]]
        out = {"card_type": "button_interaction", "main_title": {"title": title, "desc": f"#{card['id']}"},
               "sub_title_text": desc, "button_list": buttons, "task_id": task_id}
        if card.get("link") and not card.get("done"):
            out["card_action"] = {"type": 1, "url": card["link"][1]}
        return out

    async def send_card(self, peer: str, card: dict) -> str:
        if len(fmt.plain(card["body"])) > CARD_TEXT:
            await self.send_text(peer, f"**{card['title']}**\n\n{card['body']}")
        task_id = f"osa{self.id}_{card['id']}_{secrets.token_hex(4)}"
        try:
            await self.request("aibot_send_msg", {"chatid": peer, "chat_type": 1, "msgtype": "template_card",
                                                  "template_card": self._card(card, task_id)})
            return task_id
        except WecomError as e:   # 卡片发不出去：退回文字，打字回复也能处理
            print(f"[聊天软件 {self.id} 企业微信] 卡片发送失败，改发文字：{e}", flush=True)
        say = i18n.t("、").join(i18n.t("“{text}”", text=word_command(act, card["id"])) for _, act, _ in card["buttons"])
        link = f"\n\n[{card['link'][0]}]({card['link'][1]})" if card.get("link") else ""
        await self.send_text(peer, f"**{card['title']}**\n\n{card['body']}{link}\n\n" + i18n.t("回复{say}。", say=say))
        return ""

    async def edit_card(self, peer: str, msg_id: str, card: dict):
        rid, t = self._events.pop(msg_id, ("", 0.0))
        if rid and time.time() - t < 4.5:   # 刚点过按钮：5 秒内可以改卡片
            try:
                await self.request("aibot_respond_update_msg", {"response_type": "update_template_card",
                                                                "template_card": self._card(card, msg_id)}, rid)
                return
            except (WecomError, ConnectionError, asyncio.TimeoutError):
                pass
        await self.send_text(peer, f"**{card['done']}**" + i18n.t("（#{id} {what}）", id=card["id"], what=fmt.title_line(card["body"], 40)))
