"""收到新邮件（IMAP）：QQ、163、126、Gmail、Outlook、iCloud、阿里、139、飞书邮箱，或者自己填服务器。

只看不动：文件夹用 EXAMINE（只读）打开，取信用 BODY.PEEK，不会把邮件标成已读。
看到哪了记 UIDVALIDITY 和最大的 UID；UIDVALIDITY 变了（服务器重建了文件夹，UID 全换了）就重新记现状，不报。
一次最多看 MAX_NEW 封新邮件，每封只取前 MAX_FETCH 字节：发件人、主题、正文开头、附件名都够了，大附件不用下载。
163 / 126 登录后不先发 IMAP ID 命令就不让 SELECT（报 Unsafe Login），所以登录后先发一个 ID。
授权码按 secret 存进保险库。服务器解析到本机或内网地址的不连（和 fetch_url 一样挡），除非 config.yaml 里开了 tools.fetch_private。
imaplib 是同步的，放在线程里跑，每个 socket 操作都有超时。
"""
import asyncio
import base64
import email
import imaplib
import re
import ssl
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from urllib.parse import unquote
from .. import tools
from ..config import CFG
from ..i18n import N_
from ..registry import POLL, Source
from ._util import oneline, short, text_of

PROVIDERS = {
    "qq": ("imap.qq.com", 993), "163": ("imap.163.com", 993), "126": ("imap.126.com", 993),
    "gmail": ("imap.gmail.com", 993), "outlook": ("outlook.office365.com", 993), "icloud": ("imap.mail.me.com", 993),
    "aliyun": ("imap.aliyun.com", 993), "139": ("imap.139.com", 993), "feishu": ("imap.feishu.cn", 993),
}
DOMAINS = {
    "qq.com": "qq", "foxmail.com": "qq", "vip.qq.com": "qq", "163.com": "163", "vip.163.com": "163", "126.com": "126",
    "gmail.com": "gmail", "googlemail.com": "gmail", "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook",
    "msn.com": "outlook", "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud", "aliyun.com": "aliyun",
    "139.com": "139",
}
HINT = {"gmail": "Gmail 要先开两步验证，再生成“应用专用密码”", "icloud": "iCloud 要在 Apple 账户里生成 App 专用密码",
        "outlook": "Outlook / Hotmail 的个人邮箱现在大多只能用 OAuth 登录，授权码可能登不上"}
NEED_ID = ("163.com", "126.com", "yeah.net", "188.com")
LOGIN_FAILED = "登录失败：检查邮箱和授权码；QQ / 163 邮箱要先在网页版设置里开启 IMAP、生成授权码"
UNSAFE = "163 / 126 邮箱拒绝了这次登录（Unsafe Login）：到网页版设置里开启 IMAP 服务、用授权码登录，再试一次"
GBK = ("gb2312", "gbk", "gb_2312-80", "x-gbk", "cp936", "euc-cn")
TIMEOUT = 20
MAX_NEW = 20
MAX_FETCH = 200_000
SNIPPET = 1000

imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))   # imaplib 不认识 ID 命令，登记一下


# ---------- 服务器 ----------
def guess(user: str) -> str:
    return DOMAINS.get((user or "").rsplit("@", 1)[-1].strip().lower(), "")


def _host_port(host: str, port) -> tuple[str, int]:
    h = re.sub(r"(?i)^(imaps?|https?)://", "", (host or "").strip()).split("/")[0]
    if m := re.fullmatch(r"([^:\[\]]+):(\d+)", h):
        h, port = m[1], int(m[2])
    return h.lower(), int(port or 993)


def server(p: dict) -> tuple[str, int]:
    prov = p.get("provider") or "auto"
    if prov == "auto":
        prov = guess(p.get("user") or "")
    if prov in PROVIDERS:
        return PROVIDERS[prov]
    host, port = _host_port(p.get("host") or "", p.get("port"))
    if not host:
        raise ValueError("选了“其他”要填 IMAP 服务器")
    return host, port


# ---------- 解码 ----------
def _bytes(b: bytes, cs: str | None) -> str:
    cs = (cs or "utf-8").lower()
    if cs in GBK:
        cs = "gb18030"     # 标成 gb2312 的信里常有 GBK 才有的字
    try:
        return b.decode(cs, errors="replace")
    except LookupError:
        return b.decode("utf-8", errors="replace")


def _fix(s: str) -> str:
    """头里直接写的 8 位字节（解析时成了 surrogate）：先按 UTF-8，不行按 GB18030 解回来。"""
    if not re.search("[\udc80-\udcff]", s):
        return s
    b = s.encode("utf-8", "surrogateescape")
    for cs in ("utf-8", "gb18030"):
        try:
            return b.decode(cs)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", errors="replace")


def decode(s) -> str:
    """邮件头 → 文字：=?UTF-8?B?…?= 这类编码解开，gb2312 按 GB18030 解。"""
    s = _fix(str(s or ""))
    try:
        parts = decode_header(s)
    except Exception:
        return oneline(s, 1000)
    out = [b if isinstance(b, str) else b.decode("raw-unicode-escape", errors="replace") if cs is None else _bytes(b, cs)
           for b, cs in parts]
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _payload(part) -> str:
    data = part.get_payload(decode=True) or b""
    return _bytes(data, part.get_content_charset())


def _attachments_in(bs: bytes) -> list[str]:
    """BODYSTRUCTURE 里的附件名（信太大只取了开头时用）。"""
    out = []
    for m in re.finditer(rb'"(?:FILE)?NAME(\*)?"\s+"((?:[^"\\]|\\.)*)"', bs, re.I):
        v = m[2].decode("utf-8", errors="replace").replace('\\"', '"')
        if m[1]:     # RFC 2231：utf-8''%E5%8F%91%E7%A5%A8.pdf
            cs, _, rest = v.partition("''")
            v = unquote(rest or cs, encoding=(cs if rest else "") or "utf-8", errors="replace")
        else:
            v = decode(v)
        if v and v not in out:
            out.append(v)
    return out


def message(raw: bytes, size: int = 0, names: list | None = None, key: str = "") -> dict:
    """一封信（可能只有开头）→ 事件。"""
    msg = email.message_from_bytes(raw)
    heads = {}
    for k, v in msg.raw_items():     # 原样的头：直接写 8 位字节的头 msg.get 会变成乱码
        heads.setdefault(k.lower(), v)
    h = lambda k: decode(heads.get(k.lower()) or "")   # noqa: E731
    name, addr = parseaddr(h("From"))
    subject = h("Subject") or "（无主题）"
    plain, html, files = "", "", []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        disp = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
        if fn or disp == "attachment":
            if fn and not (disp != "attachment" and part.get("Content-ID")):   # 正文里内嵌的图片不算附件
                files.append(decode(fn))
            continue
        ct = part.get_content_type()
        if ct == "text/plain" and not plain.strip():
            plain = _payload(part)
        elif ct == "text/html" and not html:
            html = _payload(part)
    body = plain if plain.strip() else text_of(html) if html else ""
    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
    for n in names or []:
        if n not in files:
            files.append(n)
    try:
        when = parsedate_to_datetime(h("Date")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        when = h("Date")
    to = [f"{n} <{a}>" if n else a for n, a in getaddresses([h("To")]) if a][:5]
    cc = [f"{n} <{a}>" if n else a for n, a in getaddresses([h("Cc")]) if a][:5]
    sender = f"{name} <{addr}>" if name and addr else name or addr
    lines = [f"发件人：{sender}" if sender else "", f"收件人：{'、'.join(to)}" if to else "",
             f"抄送：{'、'.join(cc)}" if cc else "", f"时间：{when}" if when else "",
             f"附件：{'、'.join(files[:20])}" if files else "",
             "（信比较大，只看了开头）" if size and size > len(raw) and not body else "",
             f"\n正文：\n{short(body, SNIPPET)}" if body else ""]
    return {"id": key or h("Message-ID"), "title": f"{name or addr or '（不知道是谁）'} 来信：{oneline(subject, 200)}",
            "text": "\n".join(x for x in lines if x)}


# ---------- IMAP ----------
def _msg(x) -> str:
    items = x if isinstance(x, (list, tuple)) else [x]
    return " ".join(d.decode(errors="replace") if isinstance(d, bytes) else str(d) for d in items if d is not None).strip()


def _connect(host: str, port: int):
    ctx = ssl.create_default_context()
    if port == 143:
        m = imaplib.IMAP4(host, port, timeout=TIMEOUT)
        m.starttls(ctx)
        return m
    return imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=TIMEOUT)


def _send_id(m, host: str):
    if host.endswith(NEED_ID) or "ID" in (m.capabilities or ()):
        try:
            m._simple_command("ID", '("name" "open-super-agent" "version" "1.0" "vendor" "open-super-agent")')
        except Exception:
            pass


def utf7(s: str) -> str:
    """文件夹名 → IMAP 用的 modified UTF-7（“已发送” → “&XfJT0ZAB-”）。"""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b64}-")
            buf.clear()
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _num(resp) -> int | None:
    v = (resp or [None])[-1]
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _select(m, folder: str) -> tuple[int | None, int | None, int]:
    typ, data = m.select(_quote(utf7(folder)), readonly=True)
    if typ != "OK":
        why = _msg(data)
        raise ValueError(UNSAFE if "unsafe login" in why.lower() else f"打不开文件夹「{folder}」：{short(why, 150)}")
    v = _num(m.response("UIDVALIDITY")[1])
    nxt = _num(m.response("UIDNEXT")[1])
    return v, nxt, _num(data) or 0


def _highest(m, nxt: int | None, exists: int) -> int:
    if nxt:
        return nxt - 1
    if not exists:
        return 0
    _, data = m.fetch(str(exists), "(UID)")
    got = re.search(rb"UID (\d+)", b" ".join(x[0] if isinstance(x, tuple) else x for x in data or [] if x))
    return int(got[1]) if got else 0


def _fetch(m, uid: int, v) -> dict | None:
    _, data = m.uid("FETCH", str(uid), f"(RFC822.SIZE BODY.PEEK[]<0.{MAX_FETCH}>)")
    raw, meta = b"", b""
    for x in data or []:
        if isinstance(x, tuple):
            meta += x[0]
            raw = raw or x[1]
        elif isinstance(x, bytes):
            meta += x
    if not raw:
        return None      # 刚被删了
    size = int(s[1]) if (s := re.search(rb"RFC822\.SIZE (\d+)", meta)) else len(raw)
    names = []
    if size > len(raw):  # 没取全：附件名从 BODYSTRUCTURE 里找
        _, bs = m.uid("FETCH", str(uid), "(BODYSTRUCTURE)")
        names = _attachments_in(b" ".join(b" ".join(x) if isinstance(x, tuple) else x for x in bs or [] if x))
    try:
        return message(raw, size, names, key=f"{v}-{uid}")
    except Exception as e:
        return {"id": f"{v}-{uid}", "title": f"收到一封新邮件（UID {uid}）", "text": f"这封信解析出错：{e}"}


def _run(params: dict, host: str, port: int, state):
    folder = params.get("folder") or "INBOX"
    try:
        m = _connect(host, port)
    except (OSError, ssl.SSLError, imaplib.IMAP4.error) as e:
        raise ValueError(f"连不上 {host}:{port}：{_msg(e.args) or type(e).__name__}") from None
    try:
        try:
            m.login(params.get("user") or "", params.get("password") or "")
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as e:
            why = _msg(e.args)
            if "unsafe login" in why.lower():
                raise ValueError(UNSAFE) from None
            hint = HINT.get(params.get("provider") or guess(params.get("user") or ""))
            raise ValueError(LOGIN_FAILED + (f"。{hint}" if hint else "") + (f"（服务器说：{short(why, 150)}）" if why else "")) from None
        except UnicodeEncodeError:
            raise ValueError("邮箱地址和授权码里不能有中文或全角字符") from None
        _send_id(m, host)
        v, nxt, exists = _select(m, folder)
        if state is None or state.get("v") != v:
            # 第一次（或者文件夹重建了）：只记下现状。第一次顺手取最新的一封给“测试”当示例（引擎第一次不报）
            last = _highest(m, nxt, exists)
            sample = []
            if state is None and last:
                try:
                    sample = [e] if (e := _fetch(m, last, v)) else []
                except Exception:
                    sample = []
            return sample, {"v": v, "last": last}
        last = int(state.get("last") or 0)
        if nxt and nxt - 1 <= last:
            return [], state
        _, data = m.uid("SEARCH", None, f"UID {last + 1}:*")
        uids = sorted({int(x) for x in (data[0] or b"").split() if x.isdigit() and int(x) > last}) if data else []
        if not uids:
            return [], {"v": v, "last": last}
        events = [e for u in uids[-MAX_NEW:] if (e := _fetch(m, u, v))]
        if len(uids) > MAX_NEW and events:
            events[-1]["text"] += f"\n\n（这次一共来了 {len(uids)} 封，只看了最新的 {MAX_NEW} 封）"
        return events, {"v": v, "last": uids[-1]}
    except imaplib.IMAP4.abort as e:
        raise ValueError(f"和 {host} 的连接断了：{_msg(e.args)}") from None
    except OSError as e:
        raise ValueError(f"和 {host} 的连接出错：{e or type(e).__name__}") from None
    finally:
        try:
            m.logout()
        except Exception:
            pass


async def check(params: dict, state):
    host, port = server(params)
    if not (CFG.get("tools") or {}).get("fetch_private") and await tools.private_host(f"imap://{host}:{port}"):
        raise ValueError(f"没连：{host} 是本机或内网地址。要连内网的邮箱服务器，在 config.yaml 里设 tools.fetch_private: true")
    return await asyncio.to_thread(_run, params, host, port, state)


def validate(p: dict) -> dict:
    prov = p.get("provider") or "auto"
    if prov == "auto":
        prov = guess(p.get("user") or "")
        if not prov:
            raise ValueError("认不出是哪家的邮箱：在“邮箱服务商”里选一个，或者选“其他”自己填 IMAP 服务器")
        p["provider"] = prov
    if prov == "custom":
        host, port = _host_port(p.get("host") or "", p.get("port"))
        if not re.fullmatch(r"[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+", host):
            raise ValueError("选了“其他”要填 IMAP 服务器的域名，比如 imap.example.com")
        p["host"], p["port"] = host, port
    if "@" not in (p.get("user") or ""):
        raise ValueError("邮箱地址要写完整，比如 you@qq.com")
    p["folder"] = (p.get("folder") or "INBOX").strip()
    return p


def summary(p: dict) -> str:
    folder = p.get("folder") or "INBOX"
    return (p.get("user") or "") + (f" · {folder}" if folder.upper() != "INBOX" else "")


PROVIDER_OPTIONS = [
    {"value": "auto", "label": N_("按邮箱地址自动判断")}, {"value": "qq", "label": N_("QQ 邮箱")},
    {"value": "163", "label": N_("163 邮箱")}, {"value": "126", "label": N_("126 邮箱")}, {"value": "gmail", "label": "Gmail"},
    {"value": "outlook", "label": "Outlook / Hotmail"}, {"value": "icloud", "label": "iCloud"},
    {"value": "aliyun", "label": N_("阿里邮箱")}, {"value": "139", "label": N_("139 邮箱")},
    {"value": "feishu", "label": N_("飞书邮箱")}, {"value": "custom", "label": N_("其他（自己填服务器）")},
]

MAIL = Source(
    id="mail", mode=POLL, category=N_("生活"), icon="📬",
    title=N_("收到新邮件"),
    description=N_("用 IMAP 看你的邮箱（QQ、163、Gmail、Outlook、iCloud……），有新邮件时 Bot 处理一次：总结、挑出重要的、提醒你回复。只读，不会把邮件标成已读。"),
    fields=[
        {"key": "provider", "label": N_("邮箱服务商"), "type": "select", "default": "auto", "options": PROVIDER_OPTIONS,
         "help": N_("常见的邮箱选了就行；企业邮箱选“其他”自己填服务器（腾讯企业邮是 imap.exmail.qq.com，阿里企业邮是 imap.qiye.aliyun.com）")},
        {"key": "user", "label": N_("邮箱地址"), "type": "text", "required": True, "placeholder": N_("you@qq.com")},
        {"key": "password", "label": N_("授权码"), "type": "secret", "required": True,
         "help": N_("大多数邮箱要用授权码（也叫应用专用密码），不是登录密码：QQ 邮箱在“设置 → 账号”里开启 IMAP 服务后生成；"
                    "163 / 126 在“设置 → POP3/SMTP/IMAP”里开启后生成；Gmail 要先开两步验证再生成应用专用密码；"
                    "iCloud 在 Apple 账户里生成 App 专用密码。会存进保险库")},
        {"key": "host", "label": N_("IMAP 服务器（选“其他”时填）"), "type": "text", "placeholder": N_("imap.example.com")},
        {"key": "port", "label": N_("端口"), "type": "number", "default": 993, "min": 1, "max": 65535,
         "help": N_("一般是 993（SSL）；填 143 会用 STARTTLS 加密")},
        {"key": "folder", "label": N_("文件夹"), "type": "text", "default": "INBOX",
         "help": N_("默认看收件箱（INBOX）；也可以写别的文件夹名，比如“工作”")},
    ],
    check=check, every=5, min_every=2, validate=validate, summary=summary,
    sample={"title": "张三 来信：下周三的方案评审",
            "text": "发件人：张三 <zhangsan@example.com>\n收件人：me@qq.com\n时间：2026-11-09 10:32\n附件：方案-v2.pdf\n\n"
                    "正文：\n你好，方案第二版放在附件里了。下周三下午 2 点评审，麻烦周二前看一下，有问题直接在文档里批注。谢谢！"},
    scenarios=[
        {"title": N_("收到新邮件时总结"), "description": N_("每封新邮件用一两句话说清楚是谁、什么事"),
         "name": N_("邮件总结"),
         "prompt": N_("有新邮件。每封用一两句话说清楚是谁发的、什么事、要不要我做什么；需要回复或处理的放在最前面。广告和订阅一句带过。")},
        {"title": N_("重要邮件提醒"), "description": N_("只在真人写来、需要你回复或处理的邮件到了时提醒"),
         "name": N_("重要邮件"),
         "llm_filter": N_("只有真人写来、需要我回复或者处理的邮件才处理；广告、订阅、群发的通知、系统自动发的邮件都忽略"),
         "prompt": N_("这封邮件需要我处理。说清楚谁、什么事、有没有截止时间，再写一段可以直接用的回复草稿。")},
        {"title": N_("账单、快递通知整理"), "description": N_("信用卡账单、扣费、快递发货的邮件，整理出金额、日期、单号"),
         "name": N_("账单快递"),
         "llm_filter": N_("只处理账单、扣费、付款、退款、快递物流相关的邮件；验证码、广告、其他邮件忽略"),
         "prompt": N_("这是账单或快递的邮件。账单的说清楚多少钱、什么时候要还或者已经扣了；快递的说清楚是什么东西、哪家快递、单号、大概哪天到。一封一两行。")},
        {"title": N_("某人来信提醒"), "description": N_("指定的人来信时马上告诉你（关键词里填对方的邮箱地址）"),
         "name": N_("某人来信"), "include": ["boss@example.com"],
         "prompt": N_("这个人来信了。用两三句话告诉我他说了什么、要我做什么、急不急，需要回复的写一段回复草稿。")},
        {"title": N_("简历、求职邮件筛选"), "description": N_("收到简历时，Bot 先看一遍，整理出要点"),
         "name": N_("简历筛选"),
         "llm_filter": N_("只处理投递简历、求职、应聘的邮件"),
         "prompt": N_("有人投了简历。整理出姓名、应聘什么岗位、几年经验、主要技能和亮点；看得出和岗位匹配不匹配的给一句判断。附件里的简历能打开就看，打不开就按正文说。")},
    ])

SOURCES = [MAIL]
