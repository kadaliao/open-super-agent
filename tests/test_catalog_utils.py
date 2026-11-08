"""插件市场里的离线小工具：计算器、单位换算、时区、中国节假日、农历、抽签、字数统计。

逻辑函数直接测（不经过插件加载）；最后装上、启用全部七个，在 Router 的场合各调一次。节假日不联网：换掉插件里的 fetch。"""
import importlib.util
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
import pytest
from osa import memory, plugins, registry

NAMES = ("calc", "unit_convert", "world_clock", "cn_holiday", "lunar", "random_pick", "text_count")


def mod(name):
    spec = importlib.util.spec_from_file_location(f"cat_{name}", plugins.CATALOG / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "DIR", tmp_path / "plugins")
    plugins.DIR.mkdir()
    yield plugins.DIR
    for n in list(plugins._loaded):
        plugins.unload(n)
    plugins._errors.clear()
    memory.conn.execute("delete from plugins")
    memory.conn.commit()


# ---------- 计算器 ----------
def test_calc_exact_and_safe():
    c = mod("calc")
    ev = lambda e: c.calc({"expr": e})
    assert ev("0.1+0.2") == "0.1+0.2 = 0.3"                      # 十进制，不是 0.30000000000000004
    assert ev("(1200*0.85+30)/3").endswith("= 350")
    assert ev("2^10").endswith("= 1024") and ev("3²+4²").endswith("= 25")
    assert ev("2**100").endswith("= 1,267,650,600,228,229,401,496,703,205,376")
    assert ev("80*15%").endswith("= 12") and ev("10 % 3").endswith("= 1") and ev("-7 % 3").endswith("= 2")
    assert "420,000（42 万）" in ev("3.5万*12")
    assert ev("（3＋4）×5÷2").endswith("= 17.5")
    assert ev("1/3").endswith("≈ 0.33333333333333333333")
    assert ev("sqrt(16)").endswith("= 4") and "≈ 1.41421356237309504" in ev("sqrt(2)")
    assert ev("sin(30)").endswith("= 0.5") and ev("cos(90)").endswith("= 0") and ev("tan(45)").endswith("= 1")
    assert ev("log(100)").endswith("= 2") and ev("log(8, 2)").endswith("= 3") and ev("cbrt(27)").endswith("= 3")
    assert ev("round(2.675, 2)").endswith("= 2.68") and ev("round(2.5)").endswith("= 3")   # 四舍五入
    assert ev("comb(5, 2)").endswith("= 10") and ev("5!").endswith("= 120") and ev("avg(3, 4, 5)").endswith("= 4")
    assert ev("1,234,567*2").startswith("1234567*2 = 2,469,134")   # 没有函数时逗号是千分位
    for bad, why in [("1/0", "除数是 0"), ("tan(90)", "没有定义"), ("9**9**9", "指数太大"), ("__import__('os')", "不支持函数"),
                     ("(1).real", "不支持这种写法"), ("x+1", "不认识 x"), ("1" * 600, "太长"), ("factorial(10001)", "阶乘"),
                     ("(10**100000)**10", "超出范围"), ("sqrt(-1)", "负数"), ("1 +", "写得不对"), ("[1, 2]", "不支持")]:
        with pytest.raises(ValueError, match=why):
            ev(bad)


def test_calc_loan_and_compound():
    c = mod("calc")
    out = c.calc({"op": "loan", "principal": 1000000, "rate": 3.1, "years": 30})
    assert "每月还 4,270.16 元" in out                                 # 等额本息
    assert "第一个月还 5,361.11 元" in out and "每月少 7.18 元" in out and "总利息 466,291.67 元" in out   # 等额本金
    assert "等额本金" not in c.calc({"op": "loan", "principal": 120000, "rate": 0, "months": 12, "method": "等额本息"})
    assert "每月还 10,000.00 元" in c.calc({"op": "loan", "principal": 120000, "rate": 0, "months": 12})
    out = c.calc({"op": "compound", "principal": 10000, "rate": 3, "years": 10})
    assert "一共有 13,439.16 元" in out
    out = c.calc({"op": "compound", "deposit": 2000, "rate": 3, "years": 10, "per_year": 12})
    assert "一共有 279,482.84 元" in out and "自己投入 240,000.00 元" in out
    with pytest.raises(ValueError):
        c.calc({"op": "loan", "principal": 100, "rate": 3})


# ---------- 单位换算 ----------
def test_unit_convert():
    u = mod("unit_convert")
    cv = lambda value, f=None, t=None: u.convert({"value": value, "from": f, "to": t})
    assert cv(1, "斤", "千克, 克") == "1 斤 = 0.5 千克 = 500 克"
    assert cv(1, "亩", "平方米") == "1 亩 ≈ 666.66667 平方米"
    assert cv(100, "°F", "℃") == "100 华氏度 ≈ 37.777778 摄氏度" and cv(-40, "C", "F") == "-40 摄氏度 = -40 华氏度"
    assert cv(30, "度", "华氏度") == "30 摄氏度 = 86 华氏度"           # “度”按另一个单位挑：温度
    assert cv(1, "度", "焦耳") == "1 千瓦时 = 3,600,000 焦耳"          # 能量
    assert cv(2.5, "公斤", "斤") == "2.5 千克 = 5 斤" and "公斤力每平方厘米" in cv(2.5, "公斤", "psi")
    assert cv(100, "Mbps", "MB/s").startswith("100 Mbps = 12.5 MB/s") and "差 8 倍" in cv(100, "M宽带", "MB/s")
    out = cv(1, "TB", "GiB")
    assert out.startswith("1 TB ≈ 931.32257 GiB") and "Windows" in out
    assert cv(1, "kb", "B") == "1 KB = 1000 字节" and cv(1, "Kb", "B") == "1 Kb = 125 字节"   # 大小写：b 是比特
    assert cv("5 ft 11 in", t="厘米") == "5 英尺 11 英寸 = 180.34 厘米"
    assert cv("1小时30分", t="分钟") == "1 小时 30 分钟 = 90 分钟" and cv("120 m²", t="平方米") == "120 平方米 = 120 平方米"
    assert cv(1, "光年", "千米") == "1 光年 ≈ 9.4607305×10^12 千米"
    assert cv(1, "英寸", "厘米") == "1 英寸 = 2.54 厘米" and cv(1, "里", "公里") == "1 里 = 0.5 千米"
    assert "千克" in cv(1, "斤") and "磅" in cv(1, "斤")                # 不写 to：换成常用的几个
    for args, why in [((1, "斤", "米"), "不是一类"), ((1, "furlong", "米"), "不认识"), ((-300, "C", "K"), "绝对零度"),
                      ((5,), "没写原来的单位"), (("abc", "米", "厘米"), "不是数")]:
        with pytest.raises(ValueError, match=why):
            cv(*args)


# ---------- 时区 ----------
NOW = datetime(2026, 11, 9, 7, 12, tzinfo=timezone.utc)   # 北京 15:12，纽约已经是冬令时


def test_world_clock():
    w = mod("world_clock")
    out = w.calc({"places": "纽约, 伦敦, 东京"}, now=NOW)
    assert "纽约（America/New_York）：2026-11-09 02:12 星期一，UTC-05:00，冬令时（标准时间），比北京晚 13 小时" in out
    assert "东京（Asia/Tokyo）：2026-11-09 16:12 星期一，UTC+09:00，比北京早 1 小时" in out
    out = w.calc({"places": "纽约", "time": "明天 9:00"}, now=NOW)
    assert out.startswith("北京时间 2026-11-10 09:00 星期二") and "2026-11-09 20:00 星期一（前一天）" in out
    out = w.calc({"places": "北京", "time": "下午3点半", "from": "New York"}, now=NOW)
    assert "2026-11-10 04:30 星期二（次日）" in out
    out = w.calc({"places": "纽约", "time": "2026-03-01 09:00"}, now=NOW)
    assert "03-08 02:00 开始夏令时，时钟拨快 1 小时" in out          # 两周内要切换夏令时
    assert "没有 02:30 这个时间" in w.calc({"places": "北京", "time": "2026-03-08 02:30", "from": "nyc"}, now=NOW)
    assert "有两次" in w.calc({"places": "北京", "time": "2026-11-01 01:30", "from": "nyc"}, now=NOW)
    out = w.calc({"places": "UTC+5:30, asia/kathmandu, 美国"}, now=NOW)
    assert "UTC+05:30：2026-11-09 12:42" in out and "UTC+05:45" in out and out.count("America/") == 4
    assert "2026-11-10 00:00" in w.calc({"places": "悉尼", "time": "晚上12点"}, now=NOW)   # 晚上 12 点是第二天 0 点
    assert "比洛杉矶早 16 小时" in w.calc({"places": "北京", "time": "9:00"}, home="America/Los_Angeles", now=NOW)
    for args, why in [({"places": "火星"}, "不认识"), ({"places": "伦敦", "time": "25:00"}, "不对"), ({"places": "伦敦", "time": "随便"}, "看不懂")]:
        with pytest.raises(ValueError, match=why):
            w.calc(args, now=NOW)


# ---------- 中国节假日 ----------
def _days(*rows):
    return {"year": 2026, "days": [{"name": n, "date": d, "isOffDay": off} for n, d, off in rows]}


# holiday-cn 2026.json 里的一部分（2025 年 11 月国务院办公厅的通知），格式和真的一样
Y2026 = _days(("元旦", "2026-01-01", True), ("元旦", "2026-01-02", True), ("元旦", "2026-01-03", True), ("元旦", "2026-01-04", False),
              ("春节", "2026-02-14", False), *[("春节", f"2026-02-{d}", True) for d in range(15, 24)], ("春节", "2026-02-28", False),
              ("国庆节", "2026-09-20", False), *[("中秋节", f"2026-09-{d}", True) for d in (25, 26, 27)],
              *[("国庆节", f"2026-10-0{d}", True) for d in range(1, 8)], ("国庆节", "2026-10-10", False))
Y2027 = _days(("元旦", "2027-01-01", True), ("元旦", "2027-01-02", True), ("元旦", "2027-01-03", True))


def test_cn_holiday_logic():
    h = mod("cn_holiday")
    today = date(2026, 11, 9)
    years = {2026: h.parse(Y2026), 2027: h.parse(Y2027), 2028: "2028 年的放假安排还没公布"}

    def ask(**args):
        return h.calc(args, {y: years.get(y, f"{y} 年没有") for y in h.needed(args, today)}, today)
    assert ask(date="2026-02-14").startswith("2026-02-14（星期六）：要上班（春节调休上班）")
    out = ask(date="2026-10-03")
    assert "不上班（国庆节放假）" in out and "从 10-01（星期四）到 10-07（星期三），共 7 天，2026-10-08（星期四）上班" in out
    assert "下一个假期：元旦，2027-01-01（星期五）起放 3 天，还有 53 天" in ask()
    assert "要上班（工作日）" in ask(date="2026-11-10") and "不上班（周末）" in ask(date="2026-11-14")
    assert "往后第 1 个工作日是 2026-10-08（星期四）" in ask(op="add", date="2026-09-30")
    assert "往后第 3 个工作日是 2026-02-25（星期三）" in ask(op="add", date="2026-02-13", n=3)   # 14 号周六补班也算
    assert "往前第 1 个工作日是 2026-09-30" in ask(op="add", date="2026-10-08", n=-1)
    out = ask(op="count", **{"from": "2026-10-01", "to": "2026-10-31"})
    assert "含首尾共 31 天：工作日 18 天，休息 13 天（节假日 7 天、周末 6 天），其中调休上班 1 天：10-10（星期六）" in out
    out = ask(op="list", year=2026)
    assert out.index("中秋节") < out.index("国庆节")                    # 按放假的第一天排，不按调休那天
    assert "春节：02-15（星期日）到 02-23（星期一）放假，共 9 天；调休上班：02-14（星期六）、02-28（星期六）" in out
    assert ask(op="list", year=2028) == "2028 年的放假安排还没公布。"
    assert "2028 年的放假安排还没公布，这些日子按周一到周五上班算" in ask(date="2028-01-03")
    with pytest.raises(ValueError):
        ask(op="count", **{"from": "2000-01-01", "to": "2026-01-01"})


async def test_cn_holiday_cache(tmp_path, monkeypatch):
    h = mod("cn_holiday")
    today = date(2026, 11, 9)
    calls = []
    state = {"fail": False}

    async def fake_fetch(year):
        calls.append(year)
        if state["fail"]:
            raise RuntimeError("下载不了（ConnectError）")
        return {2026: Y2026, 2027: Y2027}.get(year)   # 别的年份当 404
    monkeypatch.setattr(h, "fetch", fake_fetch)
    got = await h.load_years(tmp_path, {2026, 2027}, today)
    assert got[2026][date(2026, 2, 14)] == ("春节", False) and sorted(calls) == [2026, 2027]
    assert json.loads((tmp_path / "2026.json").read_text())["data"]["days"][0]["name"] == "元旦"
    await h.load_years(tmp_path, {2026, 2027}, today)                 # 一天之内不再联网
    assert len(calls) == 2
    assert await h.load_year(tmp_path, 2030, today) == "2030 年的放假安排还没公布" and await h.load_year(tmp_path, 2005, today)
    assert len(calls) == 2                                            # 太早、太晚的年份不去下载

    old = json.loads((tmp_path / "2026.json").read_text())
    old["fetched"] = time.time() - 2 * 86400
    (tmp_path / "2026.json").write_text(json.dumps(old))
    state["fail"] = True
    got = await h.load_year(tmp_path, 2026, today)                    # 过期了、下载不了：用存着的
    assert calls[-1] == 2026 and isinstance(got, dict)
    assert "下载不了" in await h.load_year(tmp_path / "empty", 2026, today)
    state["fail"] = False
    assert await h.load_year(tmp_path, 2025, today) == "2025 年的放假安排还没公布"   # 404


# ---------- 农历 ----------
def test_lunar_known_dates():
    lu = mod("lunar")
    assert lu.to_lunar(date(2024, 2, 10)) == (2024, 1, 1, False)
    assert "甲辰龙年" in lu.calc({"op": "to_lunar", "date": "2024-02-10"}) and "春节" in lu.calc({"op": "to_lunar", "date": "2024-02-10"})
    assert "乙巳蛇年（2025）正月初一" in lu.calc({"op": "to_lunar", "date": "2025-01-29"})
    assert "丙午马年（2026）正月初一" in lu.calc({"op": "to_lunar", "date": "2026-02-17"})
    for y, d in [(1900, "1900-01-31"), (1949, "1949-01-29"), (1976, "1976-01-31"), (2000, "2000-02-05"), (2008, "2008-02-07"),
                 (2020, "2020-01-25"), (2023, "2023-01-22"), (2027, "2027-02-06"), (2100, "2100-02-09")]:
        assert lu.to_solar(y, 1, 1) == date.fromisoformat(d), y
    assert {y: lu.leap_month(y) for y in (2012, 2014, 2017, 2020, 2023, 2025, 2026, 2028, 2033)} == \
        {2012: 4, 2014: 9, 2017: 6, 2020: 4, 2023: 2, 2025: 6, 2026: 0, 2028: 5, 2033: 11}
    assert lu.to_solar(2023, 2, 1, True) == date(2023, 3, 22) and lu.to_solar(2025, 6, 1, True) == date(2025, 7, 25)
    assert lu.to_solar(2033, 11, 1, True) == date(2033, 12, 22)
    assert lu.ganzhi_day(date(2026, 4, 5)) == "己酉"
    fest = dict(lu.festivals(2025))
    assert fest["中秋节"] == date(2025, 10, 6) and fest["端午节"] == date(2025, 5, 31) and fest["除夕"] == date(2025, 1, 28)
    fest = dict(lu.festivals(2026))
    assert fest["春节"] == date(2026, 2, 17) and fest["端午节"] == date(2026, 6, 19) and fest["中秋节"] == date(2026, 9, 25)
    assert fest["清明节"] == date(2026, 4, 5) and fest["除夕"] == date(2026, 2, 16)
    with pytest.raises(ValueError, match="没有闰六月"):
        lu.to_solar(2026, 6, 1, True)
    with pytest.raises(ValueError, match="只有 29 天"):
        lu.to_solar(2024, 1, 30)
    with pytest.raises(ValueError):
        lu.to_lunar(date(1900, 1, 30))


def test_lunar_solar_terms():
    lu = mod("lunar")
    t = {(y, n): d for y in (2024, 2025, 2026) for n, d in lu.terms(y)}
    assert t[(2026, "立春")] == date(2026, 2, 4) and t[(2025, "冬至")] == date(2025, 12, 21)
    assert t[(2026, "冬至")] == date(2026, 12, 22) and t[(2026, "清明")] == date(2026, 4, 5)
    assert t[(2025, "清明")] == date(2025, 4, 4) and t[(2024, "夏至")] == date(2024, 6, 21)
    for y in range(1900, 2101):                                       # 每年 24 个，按顺序，在该在的月份
        days = [d for _, d in lu.terms(y)]
        assert days == sorted(days) and all(d.month == i // 2 + 1 for i, d in enumerate(days))
    out = lu.calc({"op": "to_lunar", "date": "2026-04-05"})
    assert "这天是节气：清明（清明节）" in out and "下一个节气：谷雨 2026-04-20" in out


def test_lunar_round_trip_and_birthday():
    lu = mod("lunar")
    d = lu.STARTS[0]
    while d < lu.STARTS[-1]:                                          # 1900–2100 每一天：农历 → 公历换回来还是它
        assert lu.to_solar(*lu.to_lunar(d)) == d
        d += timedelta(days=1)
    today = date(2026, 11, 9)
    out = lu.calc({"op": "birthday", "month": 12, "day": 30}, today)
    assert "2026-02-16（星期一）" in out and "按廿九算" in out and "下一次：2027-02-05" in out
    out = lu.calc({"op": "birthday", "month": 6, "day": 10, "leap": True, "year": 2026}, today)
    assert "这年没有闰六月，按六月算" in out
    assert "2026-09-25（星期五）" in lu.calc({"op": "to_solar", "year": 2026, "month": 8, "day": 15}, today)


# ---------- 抽签 ----------
def test_random_pick():
    r = mod("random_pick")
    assert isinstance(r.RNG, __import__("secrets").SystemRandom)
    people = [f"人{i}" for i in range(20)]
    for _ in range(20):
        got = r.pick({"items": people, "n": 5}).split("：")[1].split("、")
        assert len(set(got)) == 5 and set(got) <= set(people)        # 不重复
    assert r.items_of("张三，李四、王五\n赵六") == ["张三", "李四", "王五", "赵六"] and r.items_of("a b c") == ["a", "b", "c"]
    shuffled = r.shuffle({"items": people})
    assert sorted(x.split(". ")[1] for x in shuffled.splitlines()[1:]) == sorted(people)
    nums = [int(x) for x in r.number({"min": 1, "max": 35, "n": 7}).splitlines()[0].split("：")[1].split("、")]
    assert len(set(nums)) == 7 and all(1 <= x <= 35 for x in nums)
    total = int(r.dice({"dice": "3d6+2"}).rsplit("= ", 1)[1])
    assert 5 <= total <= 20
    assert r.coin({"n": 10}).count("次") == 3
    teams = r.group({"items": people, "groups": 3}).splitlines()[1:]
    assert [t.split("（")[1].split("）")[0] for t in teams] == ["7", "7", "6"]
    assert "（系统安全随机数抽的，真随机）" in r.calc({"op": "coin"})
    for args, why in [({"op": "pick", "items": ["a"], "n": 2}, "抽不出"), ({"op": "number", "min": 1, "max": 3, "n": 5}, "不重复"),
                      ({"op": "dice", "dice": "abc"}, "2d6"), ({"op": "group", "items": ["a"]}, "groups"), ({"op": "pick", "items": []}, "空")]:
        with pytest.raises(ValueError, match=why):
            r.calc(args)


# ---------- 字数 ----------
def test_text_count():
    t = mod("text_count")
    text = "今天天气很好，我们去 OpenAI 的 office 参观了 3 个小时。\n\nHello world, it's a test-case.\n第二段：价格是 1,299.50 元，涨了 15%！"
    s = t.stats(text)
    assert (s["cjk"], s["words"], s["numbers"], s["wide_punct"], s["word_count"]) == (25, 7, 3, 5, 40)
    assert (s["lines"], s["nonblank"], s["paragraphs"], s["chars"]) == (4, 3, 2, len(text))
    out = t.calc({"text": text, "keyword": "o"})
    assert out.startswith("字数（按 Word 的算法）：40") and "“o”出现 4 次（不分大小写），在第 1、3 行" in out
    assert t.stats("一行\n两行\n三行")["paragraphs"] == 3                 # 没有空行就一行一段
    assert "默读约 30 分钟" in t.calc({"text": "中" * 12000})
    with pytest.raises(ValueError):
        t.calc({"text": ""})


# ---------- 装上、启用，在对话里用 ----------
CALLS = {
    "calc": ({"expr": "0.1+0.2"}, "= 0.3"),
    "unit_convert": ({"value": "1", "from": "斤", "to": "克"}, "= 500 克"),
    "world_clock": ({"places": "UTC"}, "UTC"),
    "cn_holiday": ({"date": "2026-02-14"}, "春节调休上班"),
    "lunar": ({"op": "to_lunar", "date": "2024-02-10"}, "甲辰龙年"),
    "random_pick": ({"op": "pick", "items": ["甲", "乙"]}, "真随机"),
    "text_count": ({"text": "你好 world"}, "字数（按 Word 的算法）：3"),
}


async def test_install_enable_and_call(pdir):
    catalog = {p["name"]: p for p in plugins.listing()["catalog"]}
    for name in NAMES:
        assert catalog[name]["title"] and catalog[name]["description"] and catalog[name]["version"] == "1.0"
        await plugins.install(name)
        await plugins.enable(name)
        info = plugins.info(name)
        assert info["loaded"] and not info["error"] and info["tools"] == [name], (name, info["error"])
        tool = registry.get(name)
        assert tool.where == frozenset({registry.ROUTER, registry.WORKER}) and tool.describe and tool.progress(CALLS[name][0])

    async def fake_fetch(year):
        return {2026: Y2026, 2027: Y2027}.get(year)
    sys.modules["osa_plugin_cn_holiday"].fetch = fake_fetch          # 不联网
    for name, (args, want) in CALLS.items():
        out = await registry.get(name).run(registry.Call(registry.ROUTER, {"id": 1}, 0, 0, args))
        assert want in out, (name, out)
    assert (pdir / ".cn_holiday.data" / "2026.json").exists()          # 下载的数据存在插件自己的目录里
    assert (await registry.get("calc").run(registry.Call(registry.ROUTER, {"id": 1}, 0, 0, {"expr": "1/0"}))).startswith("没算：")

    await plugins.set_config("world_clock", {"home": "火星"})         # 设置写错了：加载失败，设置页上看得到
    assert "不认识" in plugins.info("world_clock")["error"] and registry.get("world_clock") is None
    for name in NAMES:
        await plugins.remove(name)
    assert not any(registry.get(n) for n in NAMES)
