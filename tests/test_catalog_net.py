"""插件市场里发通知的两个插件：推送到手机、发邮件。

只测纯逻辑：各个推送渠道拼出来的请求、邮件怎么拼。网络全换成 httpx.MockTransport，SMTP 换成假的，什么都不会真的发出去。
两个插件各有一个测试走一遍 osa.plugins 的安装、启用（插件目录换成临时目录）。"""
import importlib.util
import smtplib
import sys
from types import SimpleNamespace
import httpx
import pytest
from osa import approvals, memory, plugins, registry, vault


def _catalog_module(name):
    spec = importlib.util.spec_from_file_location(f"cat_{name}", plugins.CATALOG / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mock_httpx(module, monkeypatch, handle):
    """只换掉插件模块里的 httpx（全局换会弄坏模型客户端）。"""
    monkeypatch.setattr(module, "httpx", SimpleNamespace(
        AsyncClient=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handle), **kw),
        HTTPError=httpx.HTTPError))


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "DIR", tmp_path / "plugins")
    plugins.DIR.mkdir()
    yield plugins.DIR
    for n in list(plugins._loaded):
        plugins.unload(n)
    plugins._errors.clear()
    plugins._waiting.clear()
    memory.conn.execute("delete from plugins")
    memory.conn.commit()


def _call(args, scope=registry.ROUTER, task=None):
    return registry.Call(scope, {"id": 1, "name": "Tester"}, 0, 0, args, task=task)


# ---------- 推送到手机 ----------
def test_push_check_config():
    pp = _catalog_module("push_phone")
    with pytest.raises(ValueError, match="先在设置里选 channel"):
        pp.check("", {})
    with pytest.raises(ValueError, match="bark.device_key"):
        pp.check("bark", {"device_key": ""})
    assert pp.check("bark", {"device_key": "k"})["server"] == "https://api.day.app"
    assert pp.check("ntfy", {"server": "https://ntfy.example/", "topic": "osa-abc"})["server"] == "https://ntfy.example"
    with pytest.raises(ValueError, match="ntfy.topic"):
        pp.check("ntfy", {"topic": "有中文"})
    with pytest.raises(ValueError, match="chat_id"):
        pp.check("telegram", {"bot_token": "1:x", "chat_id": "me"})
    with pytest.raises(ValueError, match="http 或 https"):
        pp.check("feishu", {"webhook": "ftp://x"})
    with pytest.raises(ValueError, match="AgentId"):
        pp.check("wecom_app", {"corp_id": "c", "agent_id": "abc", "secret": "s"})


def test_push_request_bodies():
    pp = _catalog_module("push_phone")
    b = lambda ch, conf, **kw: pp.build(ch, pp.check(ch, conf), "正文", **kw)   # noqa: E731
    assert b("bark", {"device_key": "K"}, title="标题", url="https://x.example") == {
        "url": "https://api.day.app/push",
        "json": {"device_key": "K", "body": "正文", "group": "osa", "title": "标题", "url": "https://x.example"}}
    assert b("serverchan", {"sendkey": "SCT123"}, title="t")["url"] == "https://sctapi.ftqq.com/SCT123.send"
    sc3 = b("serverchan", {"sendkey": "sctp4567tabc"}, url="https://x.example")
    assert sc3["url"] == "https://4567.push.ft07.com/send/sctp4567tabc.send"
    assert sc3["json"] == {"title": "正文", "desp": "正文\n\n[打开链接](https://x.example)"}
    assert b("pushplus", {"token": "T"}, title="t")["json"] == {"token": "T", "title": "t", "content": "正文", "template": "markdown"}
    nt = b("ntfy", {"topic": "osa-abc", "token": "tk"}, title="标题", url="https://x.example")
    assert nt == {"url": "https://ntfy.sh", "json": {"topic": "osa-abc", "message": "正文", "title": "标题", "click": "https://x.example"},
                  "headers": {"Authorization": "Bearer tk"}}
    assert b("ntfy", {"topic": "t"})["headers"] == {}
    assert b("telegram", {"bot_token": "1:AA", "chat_id": "-100"}, title="标题") == {
        "url": "https://api.telegram.org/bot1:AA/sendMessage",
        "json": {"chat_id": "-100", "text": "标题\n正文", "disable_web_page_preview": True}}
    gt = b("gotify", {"server": "https://g.example", "app_token": "A"}, url="https://x.example")
    assert gt["url"] == "https://g.example/message" and gt["headers"] == {"X-Gotify-Key": "A"}
    assert gt["json"]["extras"] == {"client::notification": {"click": {"url": "https://x.example"}}}
    assert b("wecom", {"webhook": "https://qyapi.weixin.qq.com/hook"})["json"] == {"msgtype": "text", "text": {"content": "正文"}}
    assert b("dingtalk", {"webhook": "https://oapi.dingtalk.com/x"}, title="t")["json"] == {"msgtype": "text", "text": {"content": "t\n正文"}}
    assert b("feishu", {"webhook": "https://open.feishu.cn/x"})["json"] == {"msg_type": "text", "content": {"text": "正文"}}
    wa = pp.build("wecom_app", pp.check("wecom_app", {"corp_id": "c", "agent_id": "1000002", "secret": "s"}), "正文", token="TOK")
    assert wa["url"].endswith("message/send?access_token=TOK")
    assert wa["json"] == {"touser": "@all", "msgtype": "text", "agentid": 1000002, "text": {"content": "正文"}}
    long = pp.build("wecom", {"webhook": "https://h"}, "字" * 5000)["json"]["text"]["content"]
    assert len(long) == 600 and long.endswith("…")       # 企业微信一条最多 2048 字节


def test_push_failure_codes():
    pp = _catalog_module("push_phone")
    assert pp.failure("bark", 200, {"code": 200, "message": "success"}) == ""
    assert pp.failure("bark", 200, {"code": 400, "message": "failed to get device token"}) == "failed to get device token"
    assert pp.failure("serverchan", 200, {"code": 0}) == "" and pp.failure("serverchan", 200, {"code": 40001, "message": "bad pushkey"})
    assert pp.failure("telegram", 400, {"ok": False, "description": "chat not found"}) == "HTTP 400：chat not found"
    assert pp.failure("wecom", 200, {"errcode": 93000, "errmsg": "invalid webhook url"}) == "invalid webhook url"
    assert pp.failure("feishu", 200, {"StatusCode": 0, "code": 0}) == "" and pp.failure("feishu", 200, {"code": 19021, "msg": "sign"})
    assert pp.failure("ntfy", 200, None) == "" and pp.failure("bark", 200, None)   # 该回 JSON 的回了别的，算没成功
    assert pp.task_message({"id": 7, "status": "failed", "goal": "抓取报表"}, {"name": "Ann"}, "网站打不开") == \
        ("❌ Ann 的任务没完成", "抓取报表\n\n网站打不开")


async def test_push_phone_plugin(pdir, monkeypatch):
    sent = []

    def handle(req: httpx.Request):
        sent.append((str(req.url), req.headers.get("authorization"), req.read().decode()))
        return httpx.Response(200, json={"id": "x"})
    await plugins.install("push_phone")
    with pytest.raises(RuntimeError, match="先在设置里选 channel"):
        await plugins.enable("push_phone")
    assert plugins.info("push_phone")["provide"] == ["notify"]
    vault.put("TEST_NTFY", "tk_secret")
    conf = dict(plugins.info("push_phone")["default_config"])
    conf |= {"channel": "ntfy", "on_task_done": "all", "ntfy": {"server": "https://ntfy.example", "topic": "osa-test", "token": "vault:TEST_NTFY"}}
    try:
        await plugins.set_config("push_phone", conf)
        assert plugins.info("push_phone")["loaded"] and not plugins.info("push_phone")["error"]
        assert registry.SERVICES["notify"][0] == "plugin:push_phone"
        tool = registry.get("push_to_phone")
        assert tool.where == frozenset({registry.ROUTER, registry.WORKER}) and not tool.gated
        _mock_httpx(sys.modules["osa_plugin_push_phone"], monkeypatch, handle)

        assert await tool.run(_call({"text": "该出门了", "title": "提醒"})) == "推送好了（ntfy）。"
        url, auth, body = sent[-1]
        assert url == "https://ntfy.example" and auth == "Bearer tk_secret" and '"title":"提醒"' in body.replace(" ", "")
        assert "url 要 http" in await tool.run(_call({"text": "x", "url": "javascript:alert(1)"}))

        # 后台任务做完：推一条；任务里自己推过的不重复推
        await registry.emit("task_done", task={"id": 41, "status": "done", "goal": "整理周报"}, bot={"name": "Ann"}, result="好了")
        assert "Ann 的任务完成了" in sent[-1][2] and "整理周报" in sent[-1][2]
        n = len(sent)
        await tool.run(_call({"text": "周报好了"}, registry.WORKER, task=SimpleNamespace(id=42)))
        await registry.emit("task_done", task={"id": 42, "status": "done", "goal": "x"}, bot={"name": "Ann"}, result="")
        assert len(sent) == n + 1

        await registry.service("notify").send("别的插件发的", title="服务")   # notify 服务
        assert "别的插件发的" in sent[-1][2]
        _mock_httpx(sys.modules["osa_plugin_push_phone"], monkeypatch, lambda req: httpx.Response(403, json={"error": "forbidden"}))
        assert await tool.run(_call({"text": "x"})) == "没推送：ntfy没收下：HTTP 403：forbidden"

        await plugins.set_config("push_phone", conf | {"on_task_done": "sometimes"})
        assert "on_task_done" in plugins.info("push_phone")["error"] and "notify" not in registry.SERVICES
    finally:
        vault.delete("TEST_NTFY")
        await plugins.remove("push_phone")
    assert registry.get("push_to_phone") is None


# ---------- 发邮件 ----------
class FakeSMTP:
    """冒充 smtplib.SMTP / SMTP_SSL：记下做了什么，不连网。"""
    log: list = []
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None, ssl=False):
        self.log.append(("connect", "ssl" if ssl else "plain", host, port, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.log.append(("quit",))

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.log.append(("starttls",))

    def login(self, user, password):
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"Login fail")
        self.log.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.log.append(("send", from_addr, list(to_addrs), msg))
        return {}


def _fake_smtplib(monkeypatch, module):
    FakeSMTP.log, FakeSMTP.fail_login = [], False
    fake = SimpleNamespace(**{k: getattr(smtplib, k) for k in dir(smtplib) if k.startswith("SMTP")})
    fake.SMTP = lambda host, port, timeout=None: FakeSMTP(host, port, timeout)
    fake.SMTP_SSL = lambda host, port, timeout=None, context=None: FakeSMTP(host, port, timeout, context, ssl=True)
    monkeypatch.setattr(module, "smtplib", fake)
    return FakeSMTP


def test_smtp_config_and_recipients():
    sm = _catalog_module("smtp_mail")
    assert sm.server_of({"user": "me@qq.com"}) == ("smtp.qq.com", 465, "ssl")
    assert sm.server_of({"user": "me@hotmail.com"}) == ("smtp-mail.outlook.com", 587, "starttls")
    assert sm.server_of({"user": "me@x.com", "host": "163"}) == ("smtp.163.com", 465, "ssl")
    assert sm.server_of({"user": "me@x.com", "host": "smtp.x.com", "port": "587"}) == ("smtp.x.com", 587, "starttls")
    with pytest.raises(ValueError, match="猜不出 corp.example"):
        sm.server_of({"user": "me@corp.example"})
    with pytest.raises(ValueError, match="port"):
        sm.server_of({"user": "me@qq.com", "port": "abc"})
    assert sm.addresses("a@x.com, B@y.com；a@X.com <c@z.org>") == ["a@x.com", "B@y.com", "c@z.org"]
    assert sm.addresses(["a@x.com"]) == ["a@x.com"] and sm.addresses(None) == []
    with pytest.raises(ValueError, match="「张三」不是邮箱地址"):
        sm.addresses("张三 a@x.com")
    with pytest.raises(ValueError, match="最多 10"):
        sm.check_recipients([f"u{i}@x.com" for i in range(8)], ["a@x.com", "b@x.com", "c@x.com"], [])
    sm.check_recipients(["boss@mycompany.com", "ops@cn.mycompany.com"], [], ["mycompany.com"])
    with pytest.raises(ValueError, match="evil@notmycompany.com"):
        sm.check_recipients(["evil@notmycompany.com"], [], ["mycompany.com"])
    assert sm.domains_of(["@MyCompany.com "]) == ["mycompany.com"]
    with pytest.raises(ValueError, match="allowed_domains"):
        sm.domains_of("mycompany.com")


def test_smtp_message_and_deliver(monkeypatch):
    sm = _catalog_module("smtp_mail")
    msg = sm.build_message("me@qq.com", "小助手", ["a@x.com"], ["b@y.com"], "周报\r\nBcc: evil@z.com", "第一行\n第二行")
    assert msg["Subject"] == "周报 Bcc: evil@z.com" and msg["To"] == "a@x.com" and msg["Cc"] == "b@y.com"
    assert "me@qq.com" in msg["From"] and "小助手" in str(msg["From"]) and msg["Bcc"] is None
    assert msg.get_content().strip() == "第一行\n第二行" and msg["Message-ID"].endswith("@qq.com>")
    for subject, body, why in (("", "x", "没有主题"), ("s", " ", "正文是空的"), ("s", "x" * 20_001, "正文太长")):
        with pytest.raises(ValueError, match=why):
            sm.build_message("me@qq.com", "", ["a@x.com"], [], subject, body)

    fake = _fake_smtplib(monkeypatch, sm)
    sm.deliver(("smtp.qq.com", 465, "ssl"), "me@qq.com", "pw", msg, ["a@x.com", "b@y.com"])
    assert fake.log[0] == ("connect", "ssl", "smtp.qq.com", 465, 10) and fake.log[1] == ("login", "me@qq.com", "pw")
    assert fake.log[2][:3] == ("send", "me@qq.com", ["a@x.com", "b@y.com"])
    fake.log.clear()
    sm.deliver(("smtp-mail.outlook.com", 587, "starttls"), "me@hotmail.com", "pw", msg, ["a@x.com"])
    assert [x[0] for x in fake.log] == ["connect", "starttls", "login", "send", "quit"]   # 先加密再登录
    assert "授权码" in sm.explain(smtplib.SMTPAuthenticationError(535, b"x"), ("h", 465, "ssl"))
    assert "明文" in sm.explain(smtplib.SMTPNotSupportedError(), ("h", 587, "starttls"))
    assert "连不上 h:465" in sm.explain(TimeoutError(), ("h", 465, "ssl"))


async def test_smtp_mail_plugin(pdir, monkeypatch):
    await plugins.install("smtp_mail")
    with pytest.raises(RuntimeError, match="先在设置里填 user"):
        await plugins.enable("smtp_mail")
    vault.put("TEST_SMTP", "auth-code")
    try:
        await plugins.set_config("smtp_mail", {"user": "me@qq.com", "password": "vault:TEST_SMTP", "from_name": "助手",
                                               "allowed_domains": ["x.com"]})
        assert plugins.info("smtp_mail")["loaded"], plugins.info("smtp_mail")["error"]
        tool = registry.get("send_email")
        assert tool.gated and tool.where == frozenset({registry.WORKER})
        assert approvals.decide("send_email")[0]                 # 没有规则也先问
        assert not registry.available("send_email", registry.ROUTER)
        fake = _fake_smtplib(monkeypatch, sys.modules["osa_plugin_smtp_mail"])
        out = await tool.run(_call({"to": "a@x.com", "cc": "b@x.com", "subject": "你好", "body": "测试"}, registry.WORKER))
        assert out == "发出去了：收件人 a@x.com，抄送 b@x.com，主题「你好」。"
        assert ("login", "me@qq.com", "auth-code") in fake.log
        assert "只允许发给 x.com" in await tool.run(_call({"to": "a@y.com", "subject": "s", "body": "b"}, registry.WORKER))
        fake.fail_login = True
        assert "邮箱登录失败" in await tool.run(_call({"to": "a@x.com", "subject": "s", "body": "b"}, registry.WORKER))
    finally:
        vault.delete("TEST_SMTP")
        await plugins.remove("smtp_mail")
    assert "send_email" not in approvals.ASK and registry.get("send_email") is None
