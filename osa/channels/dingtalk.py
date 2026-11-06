"""钉钉：企业内部应用的机器人 + Stream 模式收消息，不需要公网地址。

- 长连接照官方 SDK（dingtalk-stream）：用 AppKey / AppSecret 换一个 wss 地址和 ticket，连上后收发 JSON 帧
  {specVersion, type, headers:{messageId, topic, ...}, data:"JSON 字符串"}，每一帧都要带着 messageId 回包。
  · type=SYSTEM：topic=ping 把 data 原样回去；topic=disconnect 是服务端要断开，断了重连就行。
  · type=CALLBACK，topic=/v1.0/im/bot/messages/get：机器人收到的消息。
- 只要单聊（conversationType=1）。senderStaffId 是用户在企业里的 userId，主动发消息要用它。
- 发：消息里带的 sessionWebhook 没过期就用它回（不需要额外权限）；过期了（比如任务做完才发结果）用 OpenAPI 的
  oToMessages/batchSend（要开通“企业内机器人发送消息”权限）。都发 markdown。
- 钉钉的卡片要么按钮只能跳链接，要么得先在卡片平台建模板，所以确认请求是一条 markdown：“按钮”是 dtmd:// 链接，
  点一下就替用户发出“允许一次 #12”这样一句话，由 Channel.inbound 当成按按钮处理；也可以直接打字。
  处理完发一条结果，不改原来那条。
- 语音消息带着钉钉转好的文字（content.recognition），当文字收。
"""
import asyncio
import json
import os
import random
import time
from collections import deque
from urllib.parse import quote, quote_plus
import httpx
import websockets
from .. import i18n
from . import Channel, fmt, word_command

API = os.environ.get("OSA_DINGTALK_API", "https://api.dingtalk.com").rstrip("/")   # 测试时换成假的服务器
TOPIC_MSG = "/v1.0/im/bot/messages/get"


class DingError(Exception):
    def __init__(self, status: int, code: str, msg: str):
        super().__init__(f"{status} {code} {msg}")
        self.status, self.code, self.msg = status, code, msg


async def _post(c: httpx.AsyncClient, path: str, body: dict, token: str = "") -> dict:
    r = await c.post(API + path, json=body, headers={"x-acs-dingtalk-access-token": token} if token else {})
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code >= 400:
        raise DingError(r.status_code, d.get("code") or "", d.get("message") or r.text[:200])
    return d


async def _token(c: httpx.AsyncClient, app_key: str, secret: str) -> tuple[str, int]:
    d = await _post(c, "/v1.0/oauth2/accessToken", {"appKey": app_key, "appSecret": secret})
    return d["accessToken"], int(d.get("expireIn") or 7200)


def _link(label: str, text: str) -> str:
    return f"[{label}](dtmd://dingtalkclient/sendMessage?content={quote(text, safe='')})"


class DingTalk(Channel):
    kind, title, max_len = "dingtalk", i18n.N_("钉钉"), 3000

    def __init__(self, r: dict, secret: str):
        super().__init__(r, secret)
        self.http = httpx.AsyncClient(timeout=30)
        self._token, self._token_exp = "", 0.0
        self._seen: deque[str] = deque(maxlen=500)   # 回包没送到时钉钉会重推，按 msgId 去重
        self._webhooks: dict[str, tuple[str, float]] = {}   # 每个人最近一条消息带的回复地址和过期时间
        self.robot_code = r["config"].get("app_key", "")     # 企业内部应用的 robotCode 一般就是 AppKey

    async def aclose(self):
        await self.http.aclose()

    @property
    def app_key(self) -> str:
        return self.row["config"]["app_key"]

    async def token(self) -> str:
        if not self._token or self._token_exp < time.time() + 300:
            self._token, ttl = await _token(self.http, self.app_key, self.secret)
            self._token_exp = time.time() + ttl
        return self._token

    async def api(self, path: str, body: dict) -> dict:
        try:
            return await _post(self.http, path, body, await self.token())
        except DingError as e:
            if e.status != 401:
                raise
            self._token = ""   # token 失效：换一个再试一次
            return await _post(self.http, path, body, await self.token())

    # ----- 连接 -----
    @classmethod
    async def check(cls, fields: dict) -> dict:
        app_key, secret = (fields.get("app_key") or "").strip(), (fields.get("app_secret") or "").strip()
        if not app_key or not secret:
            raise ValueError(i18n.t("请填 Client ID（AppKey）和 Client Secret（AppSecret），在开发者后台应用的“凭证与基础信息”里"))
        async with httpx.AsyncClient(timeout=20) as c:
            try:
                await _token(c, app_key, secret)
            except DingError as e:
                raise ValueError(i18n.t("Client ID 或 Client Secret 不对（{error}）", error=e.msg or e.code))
            except (httpx.HTTPError, ValueError, KeyError) as e:
                raise ValueError(i18n.t("连不上钉钉（{error}）", error=type(e).__name__))
        name = (fields.get("name") or "").strip()[:40] or app_key
        return {"name": name, "config": {"app_key": app_key}, "secret": secret}

    @classmethod
    def same(cls, r: dict, info: dict) -> bool:
        return r["config"].get("app_key") == info["config"]["app_key"]

    async def run(self):
        backoff = 5
        while True:
            try:
                d = await _post(self.http, "/v1.0/gateway/connections/open", {
                    "clientId": self.app_key, "clientSecret": self.secret,
                    "subscriptions": [{"type": "CALLBACK", "topic": TOPIC_MSG}],
                    "ua": "open-super-agent", "localIp": ""})
                url = f"{d['endpoint']}?ticket={quote_plus(d['ticket'])}"
                async with websockets.connect(url, proxy=None, max_size=None) as ws:
                    self.set_state("connected")
                    backoff = 5
                    async for raw in ws:
                        asyncio.create_task(self._safe(self._frame(ws, raw)))
                self.set_state("error", i18n.later("长连接断了，正在重连"))
            except DingError as e:
                if e.status in (400, 401, 403):
                    self.set_state("error", i18n.later("钉钉拒绝了连接（{error}）。确认应用开了机器人能力、消息接收模式选的是 Stream 模式；"
                                                   "Client Secret 重置过的话，删掉这个连接重新添加", error=e.msg or e.code))
                else:
                    self.set_state("error", i18n.later("钉钉返回错误（{error}），稍后自动重试", error=e.msg or e.status))
            except (OSError, httpx.HTTPError, websockets.WebSocketException, ValueError, KeyError) as e:
                self.set_state("error", i18n.later("连接钉钉失败（{error}），稍后自动重试", error=type(e).__name__))
            await asyncio.sleep(backoff + random.random() * 5)
            backoff = min(backoff * 2, 120)

    async def _frame(self, ws, raw):
        f = json.loads(raw)
        h = f.get("headers") or {}
        ack = {"code": 200, "headers": {"contentType": "application/json", "messageId": h.get("messageId", "")},
               "message": "OK", "data": f.get("data") or ""}
        if f.get("type") == "SYSTEM":
            if h.get("topic") == "ping":
                await ws.send(json.dumps(ack))
            return   # disconnect：服务端会关掉连接，run 里重连
        if f.get("type") != "CALLBACK":
            return
        ack["data"] = json.dumps({"response": None})
        await ws.send(json.dumps(ack))   # 先回包，免得处理慢了被重推
        if h.get("topic") == TOPIC_MSG:
            await self.on_message(json.loads(f.get("data") or "{}"))

    # ----- 收 -----
    async def on_message(self, m: dict):
        if m.get("msgId"):
            if m["msgId"] in self._seen:
                return
            self._seen.append(m["msgId"])
        if str(m.get("conversationType")) != "1":
            return   # 只要单聊
        peer = m.get("senderStaffId") or m.get("senderId") or ""
        if peer and m.get("sessionWebhook"):
            if len(self._webhooks) > 50:
                self._webhooks.clear()
            self._webhooks[peer] = (m["sessionWebhook"], (m.get("sessionWebhookExpiredTime") or 0) / 1000)
        if m.get("robotCode"):
            self.robot_code = m["robotCode"]
        mt, content = m.get("msgtype"), m.get("content") or {}
        text, files, n = "", [], (m.get("msgId") or str(int(time.time())))[-10:]
        if mt == "text":
            text = (m.get("text") or {}).get("content", "")
        elif mt == "richText":
            parts = []
            for i, el in enumerate(content.get("richText") or []):
                if el.get("text"):
                    parts.append(el["text"])
                elif el.get("downloadCode") or el.get("pictureDownloadCode"):
                    files.append((f"image_{n}_{i}.png", el.get("downloadCode") or el["pictureDownloadCode"]))
            text = "".join(parts)
        elif mt == "picture":
            files.append((f"image_{n}.png", content.get("downloadCode") or content.get("pictureDownloadCode")))
        elif mt == "file":
            files.append((content.get("fileName") or f"file_{n}", content.get("downloadCode")))
        elif mt == "video":
            files.append((f"video_{n}.mp4", content.get("downloadCode")))
        elif mt == "audio":
            text = content.get("recognition") or ""
            if not text:
                if peer == self.row["peer"]:
                    self.say(i18n.t("没听清这条语音，请再说一遍或者打字。"))
                return
        elif peer == self.row["peer"]:
            self.say(i18n.t("暂时只能收文字、语音、图片和文件。"))
            return
        await self.inbound(peer, m.get("senderNick") or "", text, [f for f in files if f[1]])

    async def download(self, code: str) -> bytes:
        try:
            d = await self.api("/v1.0/robot/messageFiles/download", {"downloadCode": code, "robotCode": self.robot_code})
        except DingError as e:
            raise ValueError(i18n.t("钉钉不让下载（{error}）", error=e.msg or e.code))
        r = await self.http.get(d["downloadUrl"], timeout=120)
        r.raise_for_status()
        return r.content

    # ----- 发 -----
    async def _markdown(self, peer: str, title: str, text: str):
        url, exp = self._webhooks.get(peer, ("", 0.0))
        if url and exp > time.time() + 30:
            try:
                r = await self.http.post(url, json={"msgtype": "markdown", "markdown": {"title": title, "text": text}})
                if r.status_code == 200 and (r.json().get("errcode") or 0) == 0:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            self._webhooks.pop(peer, None)   # 不好用了，以后走 OpenAPI
        try:
            d = await self.api("/v1.0/robot/oToMessages/batchSend", {
                "robotCode": self.robot_code, "userIds": [peer], "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False)})
        except DingError as e:
            if e.status == 403 or "permission" in (e.code + e.msg).lower() or "权限" in e.msg:
                self.set_state("error", i18n.later("发不出消息：应用还没开通“企业内机器人发送消息”权限（开发者后台 → 权限管理），开通后重新发布"))
            raise
        if d.get("invalidStaffIdList") or d.get("flowControlledStaffIdList"):
            raise DingError(200, "", f"钉钉没发出去：{d}")

    async def send_text(self, peer: str, md: str):
        await self._markdown(peer, fmt.title_line(md), fmt.dingtalk_md(md))

    async def send_card(self, peer: str, card: dict) -> str:
        links = [_link(label, word_command(act, card["id"])) for label, act, _ in card["buttons"]]
        if card.get("link"):
            links.append(f"[{card['link'][0]}]({card['link'][1]})")
        say = i18n.t("、").join(i18n.t("“{text}”", text=word_command(act, card["id"])) for _, act, _ in card["buttons"][:2])
        text = (f"### {card['title']}\n\n{card['body']}\n\n" + " ｜ ".join(links) +
                "\n\n" + i18n.t("点上面的链接，或者直接回复{say}。", say=say))
        await self._markdown(peer, card["title"], fmt.dingtalk_md(text))
        return ""

    async def edit_card(self, peer: str, msg_id: str, card: dict):
        """钉钉改不了这条消息：发一条结果。"""
        what = fmt.title_line(card["body"], 40)
        await self._markdown(peer, card["done"], f"**{card['done']}**" + i18n.t("（#{id} {what}）", id=card["id"], what=what))
