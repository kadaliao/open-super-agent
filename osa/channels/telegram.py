"""Telegram：用 Bot API 的长轮询（getUpdates）收消息，不需要公网地址。

- 在 @BotFather 里 /newbot 拿到 token 就能连。配对用深链接 t.me/<机器人>?start=<配对码>，点开按“开始”就发过来了。
- 只处理私聊。确认卡片是带内联按钮的消息，按钮数据是 ap:<确认请求 id>:<动作>（不超过 64 字节）。
- 消息按 Telegram 的 HTML 格式发；解析失败（400）就退回纯文本再发一次。
- 服务器连不上 Telegram 时，设置 HTTPS_PROXY 环境变量（httpx 会自动用）。
"""
import asyncio
import html
import os
import re
import httpx
from .. import i18n
from . import Channel, fmt

API = os.environ.get("OSA_TELEGRAM_API", "https://api.telegram.org").rstrip("/")   # 测试时换成假的服务器
COMMANDS = [("new", i18n.N_("开始新话题")), ("stop", i18n.N_("停下正在做的任务")), ("help", i18n.N_("怎么用"))]
MAX_FILE = 20 << 20   # Bot API 只能下载 20 MB 以内的文件


class TgError(Exception):
    def __init__(self, code: int, desc: str, retry: int = 0):
        super().__init__(f"{code} {desc}")
        self.code, self.desc, self.retry = code, desc, retry


async def _call(client: httpx.AsyncClient, token: str, method: str, http_timeout: float = 30, **params):
    r = await client.post(f"{API}/bot{token}/{method}", json={k: v for k, v in params.items() if v is not None},
                          timeout=http_timeout)
    try:
        d = r.json()
    except ValueError:
        raise TgError(r.status_code, r.text[:200])
    if not d.get("ok"):
        raise TgError(d.get("error_code") or r.status_code, d.get("description") or "",
                      (d.get("parameters") or {}).get("retry_after") or 0)
    return d["result"]


class Telegram(Channel):
    kind, title, max_len = "telegram", "Telegram", 3800

    def __init__(self, r: dict, secret: str):
        super().__init__(r, secret)
        self.http = httpx.AsyncClient(timeout=30)

    async def aclose(self):
        await self.http.aclose()

    async def call(self, method: str, http_timeout: float = 30, **params):
        return await _call(self.http, self.secret, method, http_timeout, **params)

    # ----- 连接 -----
    @classmethod
    async def check(cls, fields: dict) -> dict:
        token = (fields.get("token") or "").strip()
        if not re.fullmatch(r"\d+:[\w-]{20,}", token):
            raise ValueError(i18n.t("token 格式不对：应该像 123456789:AAH…，在 Telegram 的 @BotFather 里拿"))
        async with httpx.AsyncClient(timeout=20) as c:
            try:
                me = await _call(c, token, "getMe")
            except TgError as e:
                raise ValueError(i18n.t("token 不对，或者这个机器人已经被删除") if e.code in (401, 404)
                                 else i18n.t("Telegram 返回错误：{error}", error=e.desc))
            except httpx.HTTPError as e:
                raise ValueError(i18n.t("连不上 Telegram（{error}）。服务器在国内的话，要给 open-super-agent 设置 HTTPS_PROXY",
                                        error=type(e).__name__))
        return {"name": "@" + me["username"], "config": {"username": me["username"], "bot_id": me["id"]}, "secret": token}

    @classmethod
    def same(cls, r: dict, info: dict) -> bool:
        return r["config"].get("bot_id") == info["config"]["bot_id"]

    @staticmethod
    def pair_link(r: dict) -> str:
        return f"https://t.me/{r['config'].get('username')}?start={r['pair_code']}"

    async def run(self):
        offset, ready, backoff = None, False, 3
        while True:
            try:
                if not ready:
                    await self.call("deleteWebhook")      # 设过 webhook 的机器人收不到 getUpdates
                    await self.call("setMyCommands", commands=[{"command": c, "description": i18n.t(d)} for c, d in COMMANDS])
                    ready = True
                    self.set_state("connected")   # 长轮询要等到有消息或 50 秒才返回，不等它
                ups = await self.call("getUpdates", http_timeout=70, offset=offset, timeout=50,
                                      allowed_updates=["message", "callback_query"])
                self.set_state("connected")
                backoff = 3
                for u in ups:
                    offset = u["update_id"] + 1
                    asyncio.create_task(self._safe(self.on_update(u)))
            except TgError as e:
                if e.code in (401, 404):
                    self.set_state("error", i18n.later("token 失效了（机器人被删除或 token 被重置），删掉这个连接重新添加"))
                    return
                if e.code == 409:
                    self.set_state("error", i18n.later("这个机器人在别处也在收消息（比如另一台服务器上的 open-super-agent），同一个 token 只能一处用"))
                    await asyncio.sleep(30)
                    continue
                self.set_state("error", i18n.later("Telegram 返回错误：{error}", error=e.desc))
                await asyncio.sleep(e.retry or backoff)
                backoff = min(backoff * 2, 60)
            except httpx.HTTPError as e:
                self.set_state("error", i18n.later("连不上 Telegram（{error}），稍后自动重试", error=type(e).__name__))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ----- 收 -----
    async def on_update(self, u: dict):
        if cq := u.get("callback_query"):
            peer = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
            toast = await self.press(peer, cq.get("data") or "")
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast[:190])
            return
        m = u.get("message")
        if not m or (m.get("chat") or {}).get("type") != "private":
            return
        peer = str(m["chat"]["id"])
        f = m.get("from") or {}
        who = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x) or f.get("username") or ""
        text = m.get("text") or m.get("caption") or ""
        files, n = [], m["message_id"]
        if d := m.get("document"):
            files.append((d.get("file_name") or f"file_{n}", d["file_id"]))
        if p := m.get("photo"):
            files.append((f"photo_{n}.jpg", p[-1]["file_id"]))   # 最后一个是最大的尺寸
        for k, ext in (("video", ".mp4"), ("audio", ".mp3")):
            if v := m.get(k):
                files.append((v.get("file_name") or f"{k}_{n}{ext}", v["file_id"]))
        if (m.get("voice") or m.get("video_note")) and not text and not files:
            if self.row["peer"] == peer:
                self.say(i18n.t("暂时听不了语音消息，请打字（可以用输入法的语音转文字）。"))
            return
        await self.inbound(peer, who, text, files)

    async def download(self, file_id: str) -> bytes:
        f = await self.call("getFile", file_id=file_id)
        if (f.get("file_size") or 0) > MAX_FILE:
            raise ValueError(i18n.t("Telegram 机器人只能接收 20 MB 以内的文件"))
        r = await self.http.get(f"{API}/file/bot{self.secret}/{f['file_path']}", timeout=120)
        r.raise_for_status()
        return r.content

    # ----- 发 -----
    async def send_text(self, peer: str, md: str):
        try:
            await self.call("sendMessage", chat_id=peer, text=fmt.telegram_html(md), parse_mode="HTML",
                            link_preview_options={"is_disabled": True})
        except TgError as e:
            if e.code != 400:
                raise
            await self.call("sendMessage", chat_id=peer, text=fmt.plain(md)[:4096], link_preview_options={"is_disabled": True})

    def _card(self, card: dict) -> tuple[str, dict]:
        text = f"<b>{html.escape(card['title'])}</b>\n\n{fmt.telegram_html(card['body'])}"
        if card.get("done"):
            text += f"\n\n<b>{html.escape(card['done'])}</b>"
        rows = []
        if card["buttons"]:
            rows.append([{"text": label, "callback_data": f"ap:{card['id']}:{act}"} for label, act, _ in card["buttons"]])
        if card.get("link") and card["link"][1].startswith("https://"):   # Telegram 不接受 http 和内网地址的按钮
            rows.append([{"text": card["link"][0], "url": card["link"][1]}])
        return text, {"inline_keyboard": rows}

    async def send_card(self, peer: str, card: dict) -> str:
        text, kb = self._card(card)
        try:
            r = await self.call("sendMessage", chat_id=peer, text=text, parse_mode="HTML", reply_markup=kb)
        except TgError as e:
            if e.code != 400:
                raise
            r = await self.call("sendMessage", chat_id=peer, text=f"{card['title']}\n\n{fmt.plain(card['body'])}",
                                reply_markup={"inline_keyboard": kb["inline_keyboard"][:1]})
        return str(r["message_id"])

    async def edit_card(self, peer: str, msg_id: str, card: dict):
        text, kb = self._card(card)
        try:
            await self.call("editMessageText", chat_id=peer, message_id=int(msg_id), text=text, parse_mode="HTML",
                            reply_markup=kb)
        except TgError as e:
            if "not modified" in e.desc:
                return
            if e.code != 400:
                raise
            await self.call("editMessageText", chat_id=peer, message_id=int(msg_id),
                            text=f"{card['title']}\n\n{card['done']}", reply_markup=kb)

    async def typing(self, peer: str):
        await self.call("sendChatAction", chat_id=peer, action="typing")
