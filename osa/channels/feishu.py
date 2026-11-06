"""飞书（和海外版 Lark）：企业自建应用 + 长连接收事件，不需要公网地址。

- 长连接的协议照官方 SDK（lark-oapi 的 ws/client.py）：先用 App ID / App Secret 换一个 wss 地址，
  连上后收发 protobuf 编码的帧（pbbp2.Frame，字段很少，这里手写编解码，不引入 protobuf 和 SDK）。
  · method=0 是控制帧：每 PingInterval 秒发一次 ping，pong 里可能带新的配置。
  · method=1 是数据帧：type=event 的 payload 就是事件 JSON（太大时按 sum/seq 分包）；
    处理完把原帧带上 {"code":200,"data":base64(回包)} 发回去，3 秒内没回会重推，所以耗时的事都放到后台做。
- 收：im.message.receive_v1（只要单聊）、card.action.trigger（卡片按钮，回包里带 toast）。
- 发：普通回复用富文本（post）的 md 标签；确认请求用消息卡片（interactive，update_multi，按完按钮再 PATCH 成结果）。
- “正在输入”：飞书机器人没有这个接口，在用户那条消息上加一个 Typing 表情，说完再去掉（没权限就算了）。
- 同一个应用连了多处时，飞书只把事件随机推给其中一处，所以同一个应用只在一个地方连。
"""
import asyncio
import base64
import json
import os
import random
import time
from collections import deque
from urllib.parse import parse_qs, urlparse
import httpx
import websockets
from .. import i18n
from . import Channel, fmt

DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
APPLINK = {"feishu": "https://applink.feishu.cn", "lark": "https://applink.larksuite.com"}
OVERRIDE = os.environ.get("OSA_FEISHU_API", "").rstrip("/")   # 测试时换成假的服务器


def base(domain: str) -> str:
    return OVERRIDE or DOMAINS.get(domain) or DOMAINS["feishu"]


class FeishuError(Exception):
    def __init__(self, code, msg: str):
        super().__init__(f"{code} {msg}")
        self.code, self.msg = code, msg


# ---------- pbbp2.Frame 的编解码 ----------
# message Header { required string key = 1; required string value = 2; }
# message Frame { required uint64 SeqID = 1; required uint64 LogID = 2; required int32 service = 3;
#   required int32 method = 4; repeated Header headers = 5; optional string payload_encoding = 6;
#   optional string payload_type = 7; optional bytes payload = 8; optional string LogIDNew = 9; }
def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
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


def _fields(b: bytes):
    i = 0
    while i < len(b):
        key, i = _read_varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(b, i)
        elif wt == 2:
            n, i = _read_varint(b, i)
            v, i = b[i:i + n], i + n
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"不认识的 protobuf 字段类型 {wt}")
        yield num, v


def _num(num: int, v: int) -> bytes:
    return _varint(num << 3) + _varint(v)


def _bytes(num: int, v: bytes) -> bytes:
    return _varint(num << 3 | 2) + _varint(len(v)) + v


def encode_frame(f: dict) -> bytes:
    out = _num(1, f.get("seq", 0)) + _num(2, f.get("log", 0)) + _num(3, f.get("service", 0)) + _num(4, f.get("method", 0))
    for k, v in f.get("headers", []):
        out += _bytes(5, _bytes(1, k.encode()) + _bytes(2, v.encode()))
    for num, key in ((6, "payload_encoding"), (7, "payload_type")):
        if f.get(key):
            out += _bytes(num, f[key].encode())
    if f.get("payload") is not None:
        out += _bytes(8, f["payload"])
    if f.get("log_new"):
        out += _bytes(9, f["log_new"].encode())
    return out


def decode_frame(b: bytes) -> dict:
    f = {"seq": 0, "log": 0, "service": 0, "method": 0, "headers": [], "payload": None}
    names = {1: "seq", 2: "log", 3: "service", 4: "method"}
    for num, v in _fields(b):
        if num in names:
            f[names[num]] = v
        elif num == 5:
            h = dict(_fields(v))
            f["headers"].append((h.get(1, b"").decode(), h.get(2, b"").decode()))
        elif num == 6:
            f["payload_encoding"] = v.decode()
        elif num == 7:
            f["payload_type"] = v.decode()
        elif num == 8:
            f["payload"] = bytes(v)
        elif num == 9:
            f["log_new"] = v.decode()
    return f


# ---------- 接口 ----------
async def _tenant_token(c: httpx.AsyncClient, domain: str, app_id: str, secret: str) -> tuple[str, int]:
    r = await c.post(base(domain) + "/open-apis/auth/v3/tenant_access_token/internal",
                     json={"app_id": app_id, "app_secret": secret})
    d = r.json()
    if d.get("code") != 0:
        raise FeishuError(d.get("code"), d.get("msg") or r.text[:200])
    return d["tenant_access_token"], int(d.get("expire") or 7200)


def _post_text(content: dict) -> tuple[str, list[str]]:
    """富文本消息（post）→ 纯文字 + 里面的图片。"""
    body = content.get("content") or next((v.get("content") for v in content.values() if isinstance(v, dict)), []) or []
    lines, images = [], []
    title = content.get("title") or ""
    for para in body:
        parts = []
        for el in para:
            tag = el.get("tag")
            if tag in ("text", "md"):
                parts.append(el.get("text", ""))
            elif tag == "a":
                parts.append(f"{el.get('text', '')}（{el.get('href', '')}）")
            elif tag == "code_block":
                parts.append(f"```\n{el.get('text', '')}\n```")
            elif tag == "img" and el.get("image_key"):
                images.append(el["image_key"])
        lines.append("".join(parts))
    return "\n".join(([title] if title else []) + lines).strip(), images


class Feishu(Channel):
    kind, title, max_len = "feishu", i18n.N_("飞书"), 6000

    def __init__(self, r: dict, secret: str):
        super().__init__(r, secret)
        self.http = httpx.AsyncClient(timeout=30)
        self._token, self._token_exp = "", 0.0
        self._seen: deque[str] = deque(maxlen=500)   # 飞书会重推事件，按 event_id 去重
        self._parts: dict[str, list] = {}            # 分包的数据帧
        self._send_lock = asyncio.Lock()
        self.last_msg = ""                           # 用户最新一条消息，“正在输入”的表情加在它上面
        self._reaction: tuple[str, str] | None = None

    async def aclose(self):
        await self.http.aclose()

    @property
    def domain(self) -> str:
        return self.row["config"].get("domain") or "feishu"

    async def token(self) -> str:
        if not self._token or self._token_exp < time.time() + 120:
            self._token, ttl = await _tenant_token(self.http, self.domain, self.row["config"]["app_id"], self.secret)
            self._token_exp = time.time() + ttl
        return self._token

    async def api(self, method: str, path: str, **kw) -> dict:
        for attempt in (0, 1):
            r = await self.http.request(method, base(self.domain) + path,
                                        headers={"Authorization": f"Bearer {await self.token()}"}, **kw)
            d = r.json()
            if d.get("code") in (99991661, 99991663, 99991668) and not attempt:   # token 失效：换一个再试
                self._token = ""
                continue
            if d.get("code") != 0:
                raise FeishuError(d.get("code"), d.get("msg") or r.text[:200])
            return d.get("data") or {}
        return {}

    # ----- 连接 -----
    @classmethod
    async def check(cls, fields: dict) -> dict:
        app_id, secret = (fields.get("app_id") or "").strip(), (fields.get("app_secret") or "").strip()
        domain = fields.get("domain") if fields.get("domain") in DOMAINS else "feishu"
        if not app_id.startswith("cli_"):
            raise ValueError(i18n.t("App ID 应该以 cli_ 开头，在开发者后台应用的“凭证与基础信息”里"))
        if not secret:
            raise ValueError(i18n.t("请填 App Secret"))
        async with httpx.AsyncClient(timeout=20) as c:
            try:
                tok, _ = await _tenant_token(c, domain, app_id, secret)
                r = await c.get(base(domain) + "/open-apis/bot/v3/info", headers={"Authorization": f"Bearer {tok}"})
                d = r.json()
            except FeishuError as e:
                raise ValueError(i18n.t("App ID 或 App Secret 不对（{error}）", error=e.msg))
            except (httpx.HTTPError, ValueError) as e:
                raise ValueError(i18n.t("连不上飞书（{error}）", error=type(e).__name__) if domain == "feishu"
                                 else i18n.t("连不上 Lark（{error}）", error=type(e).__name__))
        if d.get("code") != 0:
            raise ValueError(i18n.t("应用还没开启机器人能力：在开发者后台“添加应用能力”里添加“机器人”，再发布一个版本（{error}）",
                                    error=d.get("msg")))
        bot = d.get("bot") or {}
        return {"name": bot.get("app_name") or app_id,
                "config": {"app_id": app_id, "domain": domain, "open_id": bot.get("open_id", "")}, "secret": secret}

    @classmethod
    def same(cls, r: dict, info: dict) -> bool:
        return r["config"].get("app_id") == info["config"]["app_id"]

    @staticmethod
    def pair_link(r: dict) -> str:
        """飞书的 AppLink：打开和这个机器人的单聊。"""
        domain = r["config"].get("domain") or "feishu"
        return f"{APPLINK.get(domain, APPLINK['feishu'])}/client/bot/open?appId={r['config'].get('app_id')}"

    async def _endpoint(self) -> tuple[str, dict]:
        r = await self.http.post(base(self.domain) + "/callback/ws/endpoint", headers={"locale": "zh"},
                                 json={"AppID": self.row["config"]["app_id"], "AppSecret": self.secret})
        d = r.json()
        if d.get("code") != 0:
            raise FeishuError(d.get("code"), d.get("msg") or i18n.t("拿不到长连接地址"))
        return d["data"]["URL"], d["data"].get("ClientConfig") or {}

    async def run(self):
        backoff = 5
        while True:
            try:
                url, conf = await self._endpoint()
                service = int(parse_qs(urlparse(url).query).get("service_id", ["0"])[0])
                async with websockets.connect(url, proxy=None, max_size=None) as ws:
                    self.set_state("connected")
                    backoff = 5
                    pinger = asyncio.create_task(self._ping(ws, service, conf))
                    try:
                        async for raw in ws:
                            if isinstance(raw, bytes):
                                await self._frame(ws, raw)
                    finally:
                        pinger.cancel()
                self.set_state("error", i18n.later("长连接断了，正在重连"))
            except FeishuError as e:
                self.set_state("error", i18n.later("飞书拒绝了连接（{error}），稍后自动重试。App Secret 重置过的话，删掉这个连接重新添加", error=e.msg))
            except (OSError, httpx.HTTPError, websockets.WebSocketException, ValueError, KeyError) as e:
                self.set_state("error", i18n.later("连接飞书失败（{error}），稍后自动重试", error=type(e).__name__))
            await asyncio.sleep(backoff + random.random() * 5)
            backoff = min(backoff * 2, 120)

    async def _write(self, ws, frame: dict):
        async with self._send_lock:
            await ws.send(encode_frame(frame))

    async def _ping(self, ws, service: int, conf: dict):
        while True:
            await self._write(ws, {"service": service, "method": 0, "headers": [("type", "ping")]})
            await asyncio.sleep(int(conf.get("PingInterval") or 120))

    async def _frame(self, ws, raw: bytes):
        f = decode_frame(raw)
        hs = dict(f["headers"])
        if f["method"] == 0:   # 控制帧：pong 里可能带新的配置，用不上
            return
        if f["method"] != 1 or hs.get("type") != "event":
            return
        payload = f["payload"] or b""
        total, seq, mid = int(hs.get("sum") or 1), int(hs.get("seq") or 0), hs.get("message_id", "")
        if total > 1:   # 分包：凑齐了再处理
            buf = self._parts.setdefault(mid, [b""] * total)
            buf[seq] = payload
            if not all(buf):
                return
            payload = b"".join(self._parts.pop(mid))
        t0 = time.time()
        resp: dict = {"code": 200}
        try:
            result = await self._event(json.loads(payload))
            if result is not None:
                resp["data"] = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
        except Exception as e:
            print(f"[聊天软件 {self.id} 飞书] 处理事件出错：{e}", flush=True)
            resp = {"code": 500}
        f["headers"].append(("biz_rt", str(int((time.time() - t0) * 1000))))
        f["payload"] = json.dumps(resp).encode()
        await self._write(ws, f)

    async def _event(self, p: dict):
        h, ev = p.get("header") or {}, p.get("event") or {}
        if h.get("event_id"):
            if h["event_id"] in self._seen:
                return None
            self._seen.append(h["event_id"])
        et = h.get("event_type")
        if et == "im.message.receive_v1":
            msg, sender = ev.get("message") or {}, ev.get("sender") or {}
            if msg.get("chat_type") != "p2p" or sender.get("sender_type") not in (None, "user"):
                return None
            peer, mid, mtype = msg.get("chat_id", ""), msg.get("message_id", ""), msg.get("message_type")
            try:
                content = json.loads(msg.get("content") or "{}")
            except ValueError:
                content = {}
            text, files = "", []
            if mtype == "text":
                text = content.get("text", "")
            elif mtype == "post":
                text, images = _post_text(content)
                files = [(f"image_{k[-10:]}.png", (mid, k, "image")) for k in images]
            elif mtype == "image":
                files = [(f"image_{mid[-10:]}.png", (mid, content.get("image_key", ""), "image"))]
            elif mtype in ("file", "media"):
                files = [(content.get("file_name") or f"file_{mid[-10:]}", (mid, content.get("file_key", ""), "file"))]
            elif peer == self.row["peer"]:
                self.say(i18n.t("暂时只能收文字、图片和文件。语音请用输入法的语音转文字。"))
                return None
            if peer == self.row["peer"]:
                self.last_msg = mid
            asyncio.create_task(self._safe(self.inbound(peer, "", text, files)))
            return None
        if et == "card.action.trigger":
            ctx, action = ev.get("context") or {}, ev.get("action") or {}
            value = action.get("value") or {}
            data = value.get("osa", "") if isinstance(value, dict) else ""
            toast = await self.press(ctx.get("open_chat_id", ""), data)
            return {"toast": {"type": "info", "content": toast}}
        return None

    async def download(self, ref) -> bytes:
        mid, key, kind = ref
        r = await self.http.get(f"{base(self.domain)}/open-apis/im/v1/messages/{mid}/resources/{key}",
                                params={"type": kind}, headers={"Authorization": f"Bearer {await self.token()}"}, timeout=120)
        if "application/json" in r.headers.get("content-type", ""):
            d = r.json()
            raise ValueError(i18n.t("飞书不让下载（{error}），检查应用有没有“获取与上传图片或文件资源”权限", error=d.get("msg")))
        r.raise_for_status()
        return r.content

    # ----- 发 -----
    async def _create(self, peer: str, msg_type: str, content: dict) -> dict:
        return await self.api("POST", "/open-apis/im/v1/messages", params={"receive_id_type": "chat_id"},
                              json={"receive_id": peer, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)})

    async def send_text(self, peer: str, md: str):
        rows = [[{"tag": "md", "text": seg}] for seg in fmt.fence_segments(fmt.feishu_md(md))]
        try:
            await self._create(peer, "post", {"zh_cn": {"title": "", "content": rows}})
        except FeishuError:
            await self._create(peer, "text", {"text": fmt.plain(md)})

    @staticmethod
    def _card(card: dict) -> dict:
        els: list[dict] = [{"tag": "markdown", "content": card["body"]}]
        if card.get("done"):
            els.append({"tag": "markdown", "content": f"**{card['done']}**"})
        acts = [{"tag": "button", "text": {"tag": "plain_text", "content": label},
                 "type": {"primary": "primary", "danger": "danger"}.get(style, "default"),
                 "value": {"osa": f"ap:{card['id']}:{act}"}} for label, act, style in card["buttons"]]
        if card.get("link"):
            acts.append({"tag": "button", "text": {"tag": "plain_text", "content": card["link"][0]}, "type": "default",
                         "url": card["link"][1]})
        if acts:
            els.append({"tag": "action", "actions": acts})
        return {"config": {"wide_screen_mode": True, "update_multi": True},
                "header": {"title": {"tag": "plain_text", "content": card["title"]},
                           "template": "grey" if card.get("done") else "orange"},
                "elements": els}

    async def send_card(self, peer: str, card: dict) -> str:
        return (await self._create(peer, "interactive", self._card(card))).get("message_id", "")

    async def edit_card(self, peer: str, msg_id: str, card: dict):
        if msg_id:
            await self.api("PATCH", f"/open-apis/im/v1/messages/{msg_id}",
                           json={"content": json.dumps(self._card(card), ensure_ascii=False)})

    def start_typing(self):
        if self.last_msg and not self._reaction:
            mid = self.last_msg
            self._reaction = (mid, "")
            self.queue(lambda: self._react(mid))

    def stop_typing(self):
        if self._reaction:
            self.queue(self._unreact)

    async def _react(self, mid: str):
        try:
            d = await self.api("POST", f"/open-apis/im/v1/messages/{mid}/reactions",
                               json={"reaction_type": {"emoji_type": "Typing"}})
            self._reaction = (mid, d.get("reaction_id", ""))
        except Exception:
            self._reaction = None   # 没有表情权限也不影响收发消息

    async def _unreact(self):
        hit, self._reaction = self._reaction, None
        if hit and hit[1]:
            try:
                await self.api("DELETE", f"/open-apis/im/v1/messages/{hit[0]}/reactions/{hit[1]}")
            except Exception:
                pass
