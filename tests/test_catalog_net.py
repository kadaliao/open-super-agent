"""插件市场里查网络、发通知的几个插件：汇率、天气、行情、推送到手机、发邮件。

只测纯逻辑：用录下来的回复（腾讯行情、CoinGecko、er-api、Open-Meteo 的真实格式）解析、币种和代码怎么认、
各个推送渠道拼出来的请求、邮件怎么拼。网络全换成 httpx.MockTransport，SMTP 换成假的，什么都不会真的发出去。
最后两个测试走一遍 osa.plugins 的安装、启用（插件目录换成临时目录）。"""
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


# ---------- 汇率 ----------
ER_USD = {"result": "success", "time_last_update_unix": 1794096151, "base_code": "USD",
          "rates": {"USD": 1, "CNY": 6.690826, "JPY": 158.318418, "EUR": 0.895793, "HKD": 7.839588}}


def test_fx_codes_and_amounts():
    fx = _catalog_module("exchange_rate")
    assert [fx.code(x) for x in ("美元", "usd", "RMB", "港币", "韩币", "日元", "EUR", "€")] == \
        ["USD", "USD", "CNY", "HKD", "KRW", "JPY", "EUR", "EUR"]
    with pytest.raises(ValueError, match="不认识的币种"):
        fx.code("火星币")
    assert fx.amount_of("3万") == 30000 and fx.amount_of("1,000") == 1000 and fx.amount_of(None) == 1
    with pytest.raises(ValueError):
        fx.amount_of("很多")
    assert fx.targets("人民币，日元、USD") == ["CNY", "JPY", "USD"] and fx.targets(None) == ["CNY"]


def test_fx_convert_text():
    fx = _catalog_module("exchange_rate")
    out = fx.convert(ER_USD, 100, "USD", ["CNY", "JPY"])
    assert "100.00 美元（USD） = 669.08 人民币（CNY）（1 USD = 6.69083 CNY，1 CNY = 0.149458 USD）" in out
    assert "15,831.84 日元（JPY）" in out and "更新于 2026-11-0" in out and "不是银行的买入卖出价" in out
    assert fx.money(0.0063) == "0.0063"
    with pytest.raises(ValueError, match="没有 THB"):
        fx.convert(ER_USD, 1, "USD", ["THB"])


async def test_fx_fetch_cache_and_errors(monkeypatch):
    fx = _catalog_module("exchange_rate")
    hits = []

    def handle(req: httpx.Request):
        hits.append(req.url.path)
        if req.url.path.endswith("/XYZ"):
            return httpx.Response(404, json={"result": "error", "error-type": "unsupported-code"})
        return httpx.Response(200, json=ER_USD)
    _mock_httpx(fx, monkeypatch, handle)
    assert "669.08 人民币" in await fx.run_convert({"amount": 100, "from": "美元"})
    assert "= 0.8958 欧元（EUR）" in await fx.run_convert({"from": "USD", "to": "欧元"})
    assert hits == ["/v6/latest/USD"]                       # 一小时内同一个基准只查一次
    assert "不支持 XYZ" in await fx.run_convert({"from": "XYZ"})

    def down(req):
        raise httpx.ConnectError("boom")
    _mock_httpx(fx, monkeypatch, down)
    fx._cache.clear()
    assert "暂时连不上" in await fx.run_convert({"from": "USD"})


# ---------- 天气 ----------
HANGZHOU = {"name": "杭州", "latitude": 30.29365, "longitude": 120.16142, "country": "中国", "admin1": "浙江", "admin2": "杭州市"}
FORECAST = {
    "current": {"time": "2026-11-09T05:00", "temperature_2m": 11.7, "apparent_temperature": 8.6, "relative_humidity_2m": 83,
                "weather_code": 3, "wind_speed_10m": 19.0, "wind_direction_10m": 307, "precipitation": 0.0},
    "daily": {"time": ["2026-11-09", "2026-11-10"], "weather_code": [51, 95], "temperature_2m_max": [15.2, 14.7],
              "temperature_2m_min": [6.8, 4.9], "precipitation_probability_max": [15, None], "precipitation_sum": [0.1, 0.0],
              "wind_speed_10m_max": [25.2, 0.5], "wind_direction_10m_dominant": [321, 338], "uv_index_max": [2.3, None],
              "sunrise": ["2026-11-09T06:19", "2026-11-10T06:20"], "sunset": ["2026-11-09T17:06", "2026-11-10T17:05"]}}


def test_weather_words():
    wx = _catalog_module("weather_now")
    assert wx.wind(19.0, 307) == "西北风 3 级（19 km/h）" and wx.wind(0.5, 0) == "无风" and wx.wind(None) == ""
    assert wx.wind(25.2, 90, peak=True) == "东风最大 4 级（25 km/h）"
    assert wx.weather_word(0) == "晴" and wx.weather_word(95) == "雷阵雨" and "代码" in wx.weather_word(42)
    assert [wx.uv_word(x) for x in (1, 4.85, 7.2, 10, 12)] == ["弱", "中等", "强", "很强", "极强"]
    assert wx.place_label(HANGZHOU) == "杭州（浙江，中国）"
    assert wx.place_label({"name": "北京", "admin1": "北京市", "country": "中国"}) == "北京（中国）"
    assert wx.days_of(None) == 3 and wx.days_of(30) == 7 and wx.days_of("x") == 3


def test_weather_report():
    wx = _catalog_module("weather_now")
    out = wx.report(HANGZHOU, FORECAST, 2).split("\n")
    assert out[0] == "杭州（浙江，中国），当地时间 2026-11-09 05:00：现在 11.7°C（体感 8.6°C），阴，湿度 83%，西北风 3 级（19 km/h）。"
    assert out[1] == ("11-09 周一（今天）：小毛毛雨，6.8°C～15.2°C，降水概率 15%（0.1 mm），西北风最大 4 级（25 km/h），"
                      "紫外线 2.3（弱），日出 06:19，日落 17:06")
    assert out[2] == "11-10 周二（明天）：雷阵雨，4.9°C～14.7°C，无风，日出 06:20，日落 17:05"   # null 的不写
    assert "Open-Meteo" in out[3] and len(wx.report(HANGZHOU, FORECAST, 1).split("\n")) == 3


async def test_weather_lookup_geocode_retry_and_cache(monkeypatch):
    wx = _catalog_module("weather_now")
    seen = []

    def handle(req: httpx.Request):
        seen.append((req.url.host, req.url.params.get("name")))
        if req.url.host.startswith("geocoding"):
            return httpx.Response(200, json={"results": [HANGZHOU]} if req.url.params["name"] == "杭州" else {})
        assert req.url.params["forecast_days"] == "2" and req.url.params["timezone"] == "auto"
        return httpx.Response(200, json=FORECAST)
    _mock_httpx(wx, monkeypatch, handle)
    out = await wx.lookup({"city": "杭州市", "days": 2})
    assert out.startswith("杭州（浙江，中国）") and "（明天）" in out
    assert [s for s in seen if s[0].startswith("geocoding")] == [("geocoding-api.open-meteo.com", "杭州市"),
                                                                 ("geocoding-api.open-meteo.com", "杭州")]
    seen.clear()
    await wx.lookup({"days": 2}, default_city="杭州市")         # 查过的城市不再查位置
    assert [s[0] for s in seen] == ["api.open-meteo.com"]
    assert "找不到叫「火星」" in await wx.lookup({"city": "火星"})
    assert "没说是哪个城市" in await wx.lookup({})

    _mock_httpx(wx, monkeypatch, lambda req: httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"}))
    wx._places["坏地方"] = HANGZHOU
    assert "Latitude must be in range" in await wx.lookup({"city": "坏地方"})


# ---------- 行情 ----------
def _tq(sym, name, code, price, prev, open_, when, chg, pct, high, low, n=50):
    f = ["0"] * n
    f[1:6] = [name, code, price, prev, open_]
    f[30:35] = [when, chg, pct, high, low]
    return f'v_{sym}="' + "~".join(f) + '";'


TENCENT = "\n".join([
    _tq("sh600519", "贵州茅台", "600519", "1453.00", "1452.00", "1450.00", "20261106161458", "1.00", "0.07", "1457.72", "1436.00"),
    _tq("hk00700", "腾讯控股", "00700", "419.800", "430.400", "421.000", "2026/11/06 16:08:54", "-10.600", "-2.46", "428.600", "416.200"),
    _tq("usAAPL", "苹果", "AAPL.OQ", "342.37", "344.17", "338.73", "2026-11-06 16:00:01", "-1.80", "-0.52", "345.79", "338.30"),
    _tq("usIXIC", "纳斯达克", ".IXIC", "24579.42", "24315.00", "24602.48", "2026-11-06 17:15:00", "264.42", "1.09", "24825.55", "24507.63"),
    _tq("sz000002", "万科A", "000002", "0.00", "6.50", "0.00", "20261106150000", "0.00", "0.00", "0.00", "0.00"),
]) + "\n"
COINGECKO = {"bitcoin": {"usd": 75687, "usd_24h_change": 0.0820369929605908, "cny": 505221, "cny_24h_change": 0.082,
                         "last_updated_at": 1794171599}}


def test_quote_normalize():
    sq = _catalog_module("stock_quote")
    cases = {"茅台": "sh600519", "600519": "sh600519", "SH600519": "sh600519", "600519.SS": "sh600519",
             "000001.SZ": "sz000001", "300750": "sz300750", "430047": "bj430047", "00700": "hk00700", "700.HK": "hk00700",
             "hk700": "hk00700", "hkHSI": "hkHSI", "AAPL": "usAAPL", "usaapl": "usAAPL", "us.IXIC": "usIXIC",
             "BRK.B": "usBRK.B", "USB": "usUSB", "usSOL": "usSOL", "恒指": "hkHSI", "纳指": "usIXIC"}
    assert {k: sq.normalize(k) for k in cases} == {k: ("stock", v) for k, v in cases.items()}
    assert [sq.normalize(x) for x in ("BTC", "btc", "比特币", "ETH-USD", "SOLUSDT", "USDT")] == \
        [("coin", "bitcoin")] * 3 + [("coin", "ethereum"), ("coin", "solana"), ("coin", "tether")]
    for bad in ("", "12345678", "苹果手机", "$$"):
        with pytest.raises(ValueError):
            sq.normalize(bad)


def test_quote_parse_and_lines():
    sq = _catalog_module("stock_quote")
    rows = sq.parse_tencent(TENCENT + 'v_pv_none_match="1";\n')
    assert list(rows) == ["sh600519", "hk00700", "usAAPL", "usIXIC", "sz000002"]
    assert sq.stock_line("sh600519", rows["sh600519"]) == (
        "贵州茅台（sh600519）：1,453.00 元，+1.00（+0.07%），今开 1,450.00，最高 1,457.72，最低 1,436.00，昨收 1,452.00；"
        "2026-11-06 16:14（北京时间）")
    assert "419.80 港元，-10.60（-2.46%）" in sq.stock_line("hk00700", rows["hk00700"])
    assert "342.37 美元" in sq.stock_line("usAAPL", rows["usAAPL"]) and "（美东时间）" in sq.stock_line("usAAPL", rows["usAAPL"])
    assert "24,579.42 点，+264.42（+1.09%）" in sq.stock_line("usIXIC", rows["usIXIC"])
    assert "现在没有成交" in sq.stock_line("sz000002", rows["sz000002"]) and "昨收 6.50 元" in sq.stock_line("sz000002", rows["sz000002"])
    line = sq.coin_line("bitcoin", COINGECKO["bitcoin"])
    assert line.startswith("比特币（BTC）：$75,687.00，¥505,221.00，24 小时 +0.08%；更新于 2026-11-")
    assert sq.num(0.123) == "0.123" and sq.num(419.8) == "419.80"


async def test_quote_lookup(monkeypatch):
    sq = _catalog_module("stock_quote")
    asked = {}

    def handle(req: httpx.Request):
        if req.url.host == "qt.gtimg.cn":
            asked["tencent"] = str(req.url)
            return httpx.Response(200, content=TENCENT.encode("gbk"), headers={"content-type": "text/html; charset=GBK"})
        asked["coins"] = req.url.params["ids"]
        return httpx.Response(200, json=COINGECKO)
    _mock_httpx(sq, monkeypatch, handle)
    out = await sq.lookup({"symbols": ["茅台", "0700.HK", "AAPL", "sh601111", "比特币", "DOGE", "什么鬼"]})
    assert asked["tencent"].endswith("q=sh600519,hk00700,usAAPL,sh601111") and asked["coins"] == "bitcoin,dogecoin"
    assert "贵州茅台（sh600519）：1,453.00 元" in out and "腾讯控股" in out and "苹果（usAAPL）" in out and "比特币（BTC）" in out
    assert "没找到「sh601111」" in out and "没找到「DOGE」" in out and "认不出「什么鬼」" in out
    assert out.endswith("不构成投资建议。")
    assert "最多 20 个" in await sq.lookup({"symbols": ["AAPL"] * 21})
    assert "没给代码" in await sq.lookup({"symbols": []})

    _mock_httpx(sq, monkeypatch, lambda req: httpx.Response(429, json={}) if req.url.host != "qt.gtimg.cn"
                else httpx.Response(200, content=TENCENT.encode("gbk")))
    out = await sq.lookup({"symbols": "AAPL, BTC"})          # 一边出错不影响另一边
    assert "苹果（usAAPL）" in out and "查得太频繁" in out


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
