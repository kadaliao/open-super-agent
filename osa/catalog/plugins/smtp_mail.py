"""发邮件：Bot 能用你的邮箱（SMTP）给别人发纯文字邮件，每封发之前都先弹卡片问你。

设置里填 user（你的邮箱地址）和 password（建议存进保险库，写成 "vault:名字"）。
QQ、163、126、Gmail 这些的 password 是邮箱设置里生成的“授权码”或“应用专用密码”，不是登录密码。
host 不填就按邮箱后缀猜（QQ、163、126、Gmail、Outlook、iCloud、新浪、阿里云……），也可以写 qq / 163 / gmail / outlook 这样的简称，
或者完整的服务器地址；port 465 走 SSL，587 走 STARTTLS，不加密的连接不发（密码会明文传过去）。
from_name 是收件人看到的发件人名字。allowed_domains 填了（比如 ["mycompany.com"]）就只能发给这些域名的邮箱，不填不限制。
一封最多 10 个收件人（加上抄送），正文最多 2 万字，不带附件；一小时最多发 30 封，防止 Bot 出错时一直发。
只在后台任务里能用。默认每封都先问你；卡片上点“总是允许”以后就不再问（设置 → 确认规则里能改回来）。
Outlook、Hotmail 的个人邮箱现在多半不让用密码登录 SMTP，发不出去就换一个邮箱。
"""
import asyncio
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from osa.registry import WORKER, Tool

META = {"title": "发邮件", "version": "1.0", "author": "open-super-agent",
        "config": {"user": "", "password": "", "host": "", "port": 0, "from_name": "", "allowed_domains": []}}
PRESETS = {
    "qq": ("smtp.qq.com", 465), "163": ("smtp.163.com", 465), "126": ("smtp.126.com", 465), "yeah": ("smtp.yeah.net", 465),
    "gmail": ("smtp.gmail.com", 465), "outlook": ("smtp-mail.outlook.com", 587), "icloud": ("smtp.mail.me.com", 587),
    "sina": ("smtp.sina.com", 465), "aliyun": ("smtp.aliyun.com", 465), "139": ("smtp.139.com", 465),
    "yahoo": ("smtp.mail.yahoo.com", 465), "exmail": ("smtp.exmail.qq.com", 465), "qiye163": ("smtp.qiye.163.com", 465),
}
DOMAINS = {
    "qq.com": "qq", "vip.qq.com": "qq", "foxmail.com": "qq", "163.com": "163", "126.com": "126", "yeah.net": "yeah",
    "gmail.com": "gmail", "googlemail.com": "gmail", "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook",
    "msn.com": "outlook", "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud", "sina.com": "sina",
    "aliyun.com": "aliyun", "139.com": "139", "yahoo.com": "yahoo",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
MAX_RCPT = 10
MAX_BODY = 20_000
MAX_SUBJECT = 200
PER_HOUR = 30
TIMEOUT = 10

SCHEMA = {"type": "function", "function": {
    "name": "send_email",
    "description": "用用户自己的邮箱给别人发一封纯文字邮件（不带附件）。只在用户明确要发邮件、给了收件人时用；"
                   "发之前会弹卡片让用户确认。收件人写邮箱地址，几个用逗号分开。",
    "parameters": {"type": "object", "properties": {
        "to": {"type": "string", "description": "收件人邮箱，几个用逗号分开"},
        "subject": {"type": "string", "description": "主题"},
        "body": {"type": "string", "description": "正文（纯文字）"},
        "cc": {"type": "string", "description": "抄送，可选，几个用逗号分开"}},
        "required": ["to", "subject", "body"]}}}


def server_of(cfg: dict) -> tuple[str, int, str]:
    """设置 -> (服务器, 端口, "ssl" 或 "starttls")。host 可以不填（按邮箱后缀猜）、写简称、写完整地址。"""
    host = str(cfg.get("host") or "").strip().lower()
    if not host:
        domain = str(cfg.get("user") or "").rsplit("@", 1)[-1].lower()
        host = DOMAINS.get(domain, "")
        if not host:
            raise ValueError(f"猜不出 {domain or '这个邮箱'} 的发信服务器：先填 host（比如 smtp.example.com），"
                             f"或者写简称 {' / '.join(PRESETS)}")
    preset = PRESETS.get(host)
    name, port = preset if preset else (host, 465)
    if cfg.get("port"):
        try:
            port = int(cfg["port"])
        except (TypeError, ValueError):
            raise ValueError("port 要是数字（465 或 587）") from None
    if not 0 < port < 65536:
        raise ValueError("port 要是数字（465 或 587）")
    if not re.fullmatch(r"[a-z0-9.-]+", name):
        raise ValueError(f"host「{name}」不像服务器地址")
    return name, port, "ssl" if port == 465 else "starttls"


def domains_of(v) -> list[str]:
    if not v:
        return []
    if not isinstance(v, list) or not all(isinstance(d, str) for d in v):
        raise ValueError('allowed_domains 要是域名的列表，比如 ["mycompany.com"]，不限制就写 []')
    return [d.strip().lstrip("@").lower() for d in v if d.strip()]


def addresses(v) -> list[str]:
    """收件人：列表，或者用逗号、分号、空格分开的文字。只收邮箱地址本身（不带名字），重复的去掉。"""
    items = v if isinstance(v, list) else re.split(r"[,，;；、\s]+", str(v or ""))
    out = []
    for x in items:
        x = str(x).strip().strip("<>")
        if not x:
            continue
        if not EMAIL.fullmatch(x):
            raise ValueError(f"「{x}」不是邮箱地址")
        if x.lower() not in (o.lower() for o in out):
            out.append(x)
    return out


def check_recipients(to: list[str], cc: list[str], allowed: list[str]):
    if not to:
        raise ValueError("没有收件人")
    if len(to) + len(cc) > MAX_RCPT:
        raise ValueError(f"收件人加抄送最多 {MAX_RCPT} 个")
    if allowed:
        bad = [a for a in to + cc
               if not any((d := a.rsplit("@", 1)[1].lower()) == x or d.endswith("." + x) for x in allowed)]
        if bad:
            raise ValueError(f"设置里只允许发给 {'、'.join(allowed)} 的邮箱，{'、'.join(bad)} 不在里面")


def build_message(user: str, from_name: str, to: list[str], cc: list[str], subject: str, body: str) -> EmailMessage:
    subject = re.sub(r"[\r\n]+", " ", str(subject or "")).strip()   # 换行会变成新的邮件头
    body = str(body or "")
    if not subject:
        raise ValueError("没有主题")
    if len(subject) > MAX_SUBJECT:
        raise ValueError(f"主题太长（最多 {MAX_SUBJECT} 字）")
    if not body.strip():
        raise ValueError("正文是空的")
    if len(body) > MAX_BODY:
        raise ValueError(f"正文太长（最多 {MAX_BODY} 字），删短一点或者分几封")
    msg = EmailMessage()
    msg["From"] = formataddr((re.sub(r"[\r\n]+", " ", from_name).strip(), user)) if from_name else user
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=user.rsplit("@", 1)[1])
    msg.set_content(body)
    return msg


def deliver(server: tuple[str, int, str], user: str, password: str, msg: EmailMessage, rcpts: list[str]) -> dict:
    """真的发（在线程里跑，smtplib 是同步的）。返回被拒收的收件人；一个都没收下会抛 SMTPRecipientsRefused。"""
    host, port, mode = server
    tls = ssl.create_default_context()
    s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=tls) if mode == "ssl" else smtplib.SMTP(host, port, timeout=TIMEOUT)
    with s:
        if mode == "starttls":
            s.ehlo()
            s.starttls(context=tls)   # 服务器不支持就抛 SMTPNotSupportedError：不加密就不登录
            s.ehlo()
        s.login(user, password)
        return s.send_message(msg, from_addr=user, to_addrs=rcpts)


def explain(e: Exception, server: tuple[str, int, str]) -> str:
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "邮箱登录失败，检查 user 和 password（QQ、163、Gmail 要用授权码或应用专用密码，不是登录密码）"
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return "服务器不支持加密连接（STARTTLS），密码会明文传过去，没发。换成 465 端口试试"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return f"收件人都被服务器拒收了：{'、'.join(e.recipients)}"
    if isinstance(e, smtplib.SMTPSenderRefused):
        return f"服务器不让用 {e.sender} 发信：{e.smtp_error.decode(errors='replace')[:200]}"
    if isinstance(e, smtplib.SMTPResponseException):
        return f"服务器拒绝了（{e.smtp_code}）：{e.smtp_error.decode(errors='replace')[:200]}"
    if isinstance(e, (smtplib.SMTPException, OSError)):
        return f"连不上 {server[0]}:{server[1]}（{type(e).__name__}），检查 host 和 port"
    return f"{type(e).__name__}: {e}"


def apply(ctx):
    cfg = ctx.config
    user = str(cfg.get("user") or "").strip()
    if not EMAIL.fullmatch(user):
        raise ValueError("先在设置里填 user（你的邮箱地址，比如 me@qq.com）")
    password = ctx.secret(cfg.get("password"))
    if not password:
        raise ValueError("先在设置里填 password（授权码，建议写 vault:名字 从保险库取；写的是 vault: 的话看看保险库里有没有这一项）")
    server = server_of(cfg)
    from_name = cfg.get("from_name") or ""
    if not isinstance(from_name, str) or len(from_name) > 64:
        raise ValueError("from_name 要是不超过 64 个字的名字")
    allowed = domains_of(cfg.get("allowed_domains"))
    sent: list[float] = []   # 最近一小时发的时间

    # gated=True：插件的 gated 工具默认每次都问（approvals.decide），用户点“总是允许”后不再问

    async def run(call):
        a = call.args
        try:
            to, cc = addresses(a.get("to")), addresses(a.get("cc"))
            check_recipients(to, cc, allowed)
            msg = build_message(user, from_name, to, cc, a.get("subject"), a.get("body"))
        except ValueError as e:
            return f"没发：{e}"
        now = time.time()
        sent[:] = [t for t in sent if now - t < 3600]
        if len(sent) >= PER_HOUR:
            return f"没发：一小时内已经发了 {PER_HOUR} 封，先停一下，过一会儿再发"
        try:
            refused = await asyncio.to_thread(deliver, server, user, password, msg, to + cc)
        except Exception as e:
            return f"没发出去：{explain(e, server)}"
        sent.append(now)
        out = f"发出去了：收件人 {', '.join(to)}" + (f"，抄送 {', '.join(cc)}" if cc else "") + f"，主题「{msg['Subject']}」。"
        if refused:
            out += f"其中 {'、'.join(refused)} 被服务器拒收了。"
        return out

    ctx.tool(Tool("send_email", SCHEMA, run, where=frozenset({WORKER}), gated=True,
                  describe="用用户的邮箱发邮件（SMTP，每封先问用户）",
                  progress=lambda a: f"发邮件给 {str(a.get('to') or '')[:60]}：{str(a.get('subject') or '')[:30]}"))
