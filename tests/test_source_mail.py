"""收到新邮件：第一次只记现状、新邮件解析（编码、正文、附件名）、只读不标已读、UIDVALIDITY 变了重新记、
163 要先发 ID、登录失败的提示、内网服务器不连。用假的 IMAP 服务器，不联网。"""
import imaplib
import re
from email.message import EmailMessage

import pytest

from osa import events, vault
from osa.sources import mail


def letter(subject="周报", sender="张三 <zhang@example.com>", body="这周做完了登录页。", html=None, attach=(), big=False) -> bytes:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, sender, "我 <me@qq.com>"
    m["Date"] = "Mon, 09 Nov 2026 10:32:00 +0800"
    if body is not None:
        m.set_content(body)
    if html:
        if body is None:
            m.set_content(html, subtype="html")
        else:
            m.add_alternative(html, subtype="html")
    for name in attach:
        m.add_attachment(b"%PDF-1.4 " + (b"0" * 300_000 if big else b""), maintype="application", subtype="pdf", filename=name)
    return m.as_bytes()


class Box:
    """假邮箱：uid -> 原始邮件。"""

    def __init__(self, n=3, validity=777):
        self.msgs = {i: letter(f"旧邮件 {i}") for i in range(1, n + 1)}
        self.validity = validity
        self.uidnext = True             # SELECT 时给不给 UIDNEXT
        self.password = "right"
        self.need_id = False            # 163：不先发 ID 就拒绝 SELECT
        self.log: list = []
        self.connects: list = []

    def add(self, raw: bytes) -> int:
        uid = max(self.msgs, default=0) + 1
        self.msgs[uid] = raw
        return uid


class FakeIMAP:
    def __init__(self, box: Box, host, port, **kw):
        self.box, self.capabilities, self.id_sent = box, ("IMAP4REV1", "IDLE"), False
        self.untagged: dict = {}
        box.connects.append((host, port, kw.get("timeout")))

    def login(self, user, pw):
        self.box.log.append(("LOGIN", user))
        if pw != self.box.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"LOGIN completed"]

    def _simple_command(self, name, *args):
        self.box.log.append((name, *args))
        if name == "ID":
            self.id_sent = True
        return "OK", [b"ID completed"]

    def select(self, mailbox, readonly=False):
        self.box.log.append(("SELECT", mailbox, readonly))
        if self.box.need_id and not self.id_sent:
            return "NO", [b"SELECT Unsafe Login. Please contact kefu@188.com for help"]
        self.untagged = {"UIDVALIDITY": [str(self.box.validity).encode()]}
        if self.box.uidnext:
            self.untagged["UIDNEXT"] = [str(max(self.box.msgs, default=0) + 1).encode()]
        return "OK", [str(len(self.box.msgs)).encode()]

    def response(self, code):
        return code, self.untagged.pop(code, [None])

    def fetch(self, seq, items):
        self.box.log.append(("FETCH", seq, items))
        uid = sorted(self.box.msgs)[int(seq) - 1]
        return "OK", [f"{seq} (UID {uid})".encode()]

    def uid(self, cmd, *args):
        self.box.log.append(("UID", cmd, *args))
        if cmd == "SEARCH":
            lo = int(re.search(r"UID (\d+):\*", args[1])[1])
            got = [u for u in sorted(self.box.msgs) if u >= lo] or sorted(self.box.msgs)[-1:]   # n:* 总带上最大的那个
            return "OK", [" ".join(map(str, got)).encode()]
        uid, items = int(args[0]), args[1]
        raw = self.box.msgs.get(uid)
        if raw is None:
            return "OK", [None]
        if "BODYSTRUCTURE" in items:
            return "OK", [f'1 (UID {uid} BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 10 1)'
                          f'("application" "pdf" ("name" "=?utf-8?b?5ZCI5ZCMLnBkZg==?=") NIL NIL "base64" 400000 NIL '
                          f'("attachment" ("filename*" "utf-8\'\'%E5%90%88%E5%90%8C.pdf"))) "mixed"))'.encode()]
        n = int(re.search(r"<0\.(\d+)>", items)[1])
        part = raw[:n]
        return "OK", [(f"1 (UID {uid} RFC822.SIZE {len(raw)} BODY[]<0> {{{len(part)}}}".encode(), part), b")"]

    def logout(self):
        self.box.log.append(("LOGOUT",))


@pytest.fixture
def box(monkeypatch):
    b = Box()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port, **kw: FakeIMAP(b, host, port, **kw))

    async def public(url):
        return False
    monkeypatch.setattr(mail.tools, "private_host", public)
    return b


P = {"provider": "qq", "user": "me@qq.com", "password": "right", "host": "", "port": 993, "folder": "INBOX"}


async def test_baseline_then_new_mail(box):
    evs, st = await mail.check(P, None)
    assert st == {"v": 777, "last": 3}
    assert [e["title"] for e in evs] == ["张三 来信：旧邮件 3"]       # 给“测试”当示例，引擎第一次不报
    assert not mail.MAIL.fire_first
    assert box.connects == [("imap.qq.com", 993, mail.TIMEOUT)]
    box.add(letter("下周三的方案评审", "=?utf-8?b?5p2O5Zub?= <li@example.com>", "你好，\n\n\n\n方案在附件里，周二前看一下。",
                   attach=["方案-v2.pdf"]))
    box.add(letter("通知", "noreply@example.com", None, html="<html><style>p{}</style><p>您的账单已<b>出</b></p></html>"))
    box.log.clear()
    evs, st = await mail.check(P, st)
    assert st == {"v": 777, "last": 5}
    a, b = evs
    assert a["id"] == "777-4" and a["title"] == "李四 来信：下周三的方案评审"
    assert "发件人：李四 <li@example.com>" in a["text"] and "收件人：我 <me@qq.com>" in a["text"]
    assert "附件：方案-v2.pdf" in a["text"] and "正文：\n你好，\n\n方案在附件里，周二前看一下。" in a["text"]
    assert b["title"] == "noreply@example.com 来信：通知" and "您的账单已 出" in b["text"] and "p{}" not in b["text"]
    # 只读：EXAMINE（readonly）+ BODY.PEEK，不会标成已读
    assert ("SELECT", '"INBOX"', True) in box.log
    fetches = [x for x in box.log if x[:2] == ("UID", "FETCH")]
    assert fetches and all("BODY.PEEK[]" in x[3] for x in fetches)
    assert box.log[-1] == ("LOGOUT",)


async def test_nothing_new_skips_search(box):
    _, st = await mail.check(P, None)
    box.log.clear()
    evs, st2 = await mail.check(P, st)
    assert evs == [] and st2 == st and not [x for x in box.log if x[:2] == ("UID", "SEARCH")]


async def test_without_uidnext(box):
    box.uidnext = False
    _, st = await mail.check(P, None)
    assert st["last"] == 3
    evs, st = await mail.check(P, st)            # n:* 会带上最大的那封（3），要去掉
    assert evs == [] and st["last"] == 3
    box.add(letter("新的"))
    evs, st = await mail.check(P, st)
    assert [e["title"] for e in evs] == ["张三 来信：新的"] and st["last"] == 4


async def test_uidvalidity_changed_rebaselines(box):
    _, st = await mail.check(P, None)
    box.validity = 888
    box.add(letter("重建以后"))
    evs, st = await mail.check(P, st)
    assert evs == [] and st == {"v": 888, "last": 4}


async def test_many_new_only_newest(box):
    _, st = await mail.check(P, None)
    for i in range(25):
        box.add(letter(f"批量 {i}"))
    evs, st = await mail.check(P, st)
    assert len(evs) == mail.MAX_NEW and evs[0]["title"].endswith("批量 5") and evs[-1]["title"].endswith("批量 24")
    assert "一共来了 25 封" in evs[-1]["text"] and st["last"] == 28


async def test_big_mail_attachment_names_from_bodystructure(box, monkeypatch):
    monkeypatch.setattr(mail, "MAX_FETCH", 5000)
    _, st = await mail.check(P, None)
    box.add(letter("合同", body="合同见附件", attach=["合同.pdf"], big=True))
    evs, _ = await mail.check(P, st)
    assert "附件：合同.pdf" in evs[0]["text"] and "正文：\n合同见附件" in evs[0]["text"]
    assert [x for x in box.log if x[:2] == ("UID", "FETCH") and "BODYSTRUCTURE" in x[3]]


async def test_163_sends_id_first(box):
    box.need_id = True
    _, st = await mail.check(P | {"provider": "163", "user": "me@163.com"}, None)
    assert st["last"] == 3
    names = [x[0] for x in box.log]
    assert names.index("ID") < names.index("SELECT") and box.connects[-1][0] == "imap.163.com"


async def test_unsafe_login_message(box, monkeypatch):
    box.need_id = True
    monkeypatch.setattr(mail, "_send_id", lambda m, host: None)
    with pytest.raises(ValueError, match="Unsafe Login"):
        await mail.check(P | {"provider": "163", "user": "me@163.com"}, None)


async def test_login_failed(box):
    box.password = "other"
    with pytest.raises(ValueError) as e:
        await mail.check(P, None)
    assert str(e.value).startswith(mail.LOGIN_FAILED) and "AUTHENTICATIONFAILED" in str(e.value)
    with pytest.raises(ValueError, match="应用专用密码"):
        await mail.check(P | {"provider": "gmail", "user": "me@gmail.com"}, None)


async def test_connect_error(box, monkeypatch):
    def boom(host, port, **kw):
        raise ConnectionRefusedError(61, "Connection refused")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", boom)
    with pytest.raises(ValueError, match="连不上 imap.qq.com:993"):
        await mail.check(P, None)


async def test_private_server_refused(box, monkeypatch):
    async def private(url):
        assert url == "imap://mail.lan:993"
        return True
    monkeypatch.setattr(mail.tools, "private_host", private)
    with pytest.raises(ValueError, match="内网"):
        await mail.check(P | {"provider": "custom", "host": "mail.lan"}, None)
    assert box.connects == []
    monkeypatch.setitem(mail.CFG, "tools", {"fetch_private": True})
    await mail.check(P | {"provider": "custom", "host": "mail.lan"}, None)
    assert box.connects == [("mail.lan", 993, mail.TIMEOUT)]


async def test_non_ascii_folder(box):
    await mail.check(P | {"folder": "已发送"}, None)
    assert ("SELECT", '"&XfJT0ZAB-"', True) in box.log


def test_utf7():
    assert mail.utf7("INBOX") == "INBOX" and mail.utf7("已发送") == "&XfJT0ZAB-" and mail.utf7("A&B") == "A&-B"


def test_decode_headers():
    assert mail.decode("=?gb2312?B?1cXI/Q==?= <z@x.com>") == "张三 <z@x.com>"
    assert mail.decode("Re: =?utf-8?q?=E5=91=A8=E4=BC=9A?= notes") == "Re: 周会 notes"
    raw = "Subject: 直接写的中文\nFrom: a@b.com\nContent-Type: text/plain; charset=gb2312\n\n".encode() + "镕基".encode("gbk")
    e = mail.message(raw)
    assert e["title"] == "a@b.com 来信：直接写的中文" and e["text"].endswith("镕基")


def test_validate():
    p = events.clean_params(mail.MAIL, {"user": "me@foxmail.com", "password": "abcd"})
    assert p["provider"] == "qq" and p["folder"] == "INBOX" and p["port"] == 993
    assert p["password"].startswith("vault:event_") and vault.get(p["password"][6:]) == "abcd"
    assert mail.MAIL.summary(p) == "me@foxmail.com"
    assert mail.MAIL.summary(p | {"folder": "工作"}) == "me@foxmail.com · 工作"
    with pytest.raises(ValueError, match="认不出"):
        events.clean_params(mail.MAIL, {"user": "me@company.com", "password": "x"})
    with pytest.raises(ValueError, match="IMAP 服务器"):
        events.clean_params(mail.MAIL, {"provider": "custom", "user": "me@company.com", "password": "x"})
    with pytest.raises(ValueError, match="要填授权码"):
        events.clean_params(mail.MAIL, {"provider": "qq", "user": "me@qq.com"})
    p = events.clean_params(mail.MAIL, {"provider": "custom", "user": "me@company.com", "password": "x",
                                        "host": "imaps://Mail.Company.com:1993/"})
    assert (p["host"], p["port"]) == ("mail.company.com", 1993) and mail.server(p) == ("mail.company.com", 1993)
    with pytest.raises(ValueError, match="完整"):
        events.clean_params(mail.MAIL, {"provider": "qq", "user": "me", "password": "x"})
    # 编辑时没填授权码：沿用原来的
    again = events.clean_params(mail.MAIL, {"folder": "工作"}, old=p)
    assert again["password"] == p["password"] and again["folder"] == "工作"


def test_scenarios_have_valid_params():
    keys = {f["key"] for f in mail.MAIL.fields}
    for sc in mail.MAIL.scenarios:
        assert set(sc.get("params") or {}) <= keys
