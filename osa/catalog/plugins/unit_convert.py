"""单位换算：长度、重量、面积、体积、温度、速度、时间、数据大小和网速、能量、功率、压强，认中文和英文的单位名，也认斤、两、亩、尺、寸、里这些市制单位。

大模型换算单位常记错倍数（1 英寸多少厘米、1 亩多少平方米、100M 宽带下载多快）。装上以后 Bot 换算单位就调这个工具，对话里和后台任务里都能用。

- 能一次换成好几个单位（to 写“千克, 磅”），不写 to 就换成这一类里常用的几个。
- 能写“5尺3寸”“5 ft 11 in”“1小时30分”这种几个单位加在一起的（value 写这一串，from 不写）。
- 用十进制精确算，结果最多保留 8 位有效数字，除不尽、截断了的前面标“≈”。
- 同一个名字有两种意思的按另一个单位挑：“度”可以是摄氏度也可以是千瓦时，“公斤”可以是重量也可以是胎压（公斤力每平方厘米），
  “码”可以是长度（码）也可以是车速（公里每小时）。只给一个单位时按第一种。
- 加仑、品脱、杯、液量盎司是美制，英制加仑单独写“英制加仑”；月按 30.436875 天、年按 365.2425 天（公历平均）。
"""
import re
import unicodedata
from decimal import Context, Decimal, DecimalException, localcontext
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "单位换算", "version": "1.0", "author": "open-super-agent"}
CTX = Context(prec=40)
DIGITS = 8

# 类别 | 名字 | 换算成这一类基本单位（第一行）的倍数 | 别名（逗号分开）
TABLE = """
长度 | 米 | 1 | m, meter, meters, metre, metres, 公尺
长度 | 千米 | 1000 | km, 公里, kilometer, kilometers, kilometre
长度 | 分米 | 0.1 | dm
长度 | 厘米 | 0.01 | cm, 公分, centimeter, centimeters
长度 | 毫米 | 0.001 | mm, millimeter, millimeters
长度 | 微米 | 1e-6 | μm, um, micron, micrometer
长度 | 纳米 | 1e-9 | nm, nanometer
长度 | 英里 | 1609.344 | mi, mile, miles
长度 | 码 | 0.9144 | yd, yard, yards
长度 | 英尺 | 0.3048 | ft, foot, feet, ′, '
长度 | 英寸 | 0.0254 | in, inch, inches, 吋, ″, "
长度 | 海里 | 1852 | nmi, nautical mile
长度 | 里 | 500 | 市里, 华里
长度 | 丈 | 10/3 | 市丈
长度 | 尺 | 1/3 | 市尺
长度 | 寸 | 1/30 | 市寸
长度 | 光年 | 9460730472580800 | ly, light year
长度 | 天文单位 | 149597870700 | au, AU
重量 | 千克 | 1 | kg, 公斤, kilogram, kilograms, kilo, kilos
重量 | 克 | 0.001 | g, gram, grams
重量 | 毫克 | 1e-6 | mg, milligram
重量 | 微克 | 1e-9 | μg, ug, mcg
重量 | 吨 | 1000 | t, 公吨, tonne, tonnes
重量 | 斤 | 0.5 | 市斤
重量 | 两 | 0.05 | 市两
重量 | 钱 | 0.005 | 市钱
重量 | 台斤 | 0.6 |
重量 | 磅 | 0.45359237 | lb, lbs, pound, pounds
重量 | 盎司 | 0.028349523125 | oz, ounce, ounces, 安士
重量 | 金衡盎司 | 0.0311034768 | ozt, troy oz, troy ounce, 金衡制盎司
重量 | 克拉 | 0.0002 | ct, carat, carats
重量 | 英石 | 6.35029318 | st, stone
重量 | 美吨 | 907.18474 | short ton, 短吨
重量 | 英吨 | 1016.0469088 | long ton, 长吨
面积 | 平方米 | 1 | m2, sqm, 平米, 平, 平方
面积 | 平方千米 | 1e6 | km2, 平方公里
面积 | 平方分米 | 0.01 | dm2
面积 | 平方厘米 | 1e-4 | cm2
面积 | 平方毫米 | 1e-6 | mm2
面积 | 公顷 | 10000 | ha, hectare, hectares
面积 | 公亩 | 100 | are
面积 | 亩 | 10000/15 | 市亩
面积 | 英亩 | 4046.8564224 | acre, acres, ac
面积 | 平方英里 | 2589988.110336 | mi2, sqmi
面积 | 平方码 | 0.83612736 | yd2
面积 | 平方英尺 | 0.09290304 | ft2, sqft, sq ft
面积 | 平方英寸 | 0.00064516 | in2, sqin
体积 | 升 | 1 | L, l, liter, liters, litre, 公升, 立方分米, dm3
体积 | 毫升 | 0.001 | mL, ml, cc, 立方厘米, cm3, milliliter
体积 | 立方米 | 1000 | m3, 方
体积 | 立方毫米 | 1e-6 | mm3
体积 | 加仑 | 3.785411784 | gal, gallon, gallons, 美制加仑, us gal
体积 | 英制加仑 | 4.54609 | imp gal, uk gal
体积 | 夸脱 | 0.946352946 | qt, quart, quarts
体积 | 品脱 | 0.473176473 | pt, pint, pints
体积 | 杯 | 0.2365882365 | cup, cups, 量杯
体积 | 液量盎司 | 0.0295735295625 | fl oz, floz, 液盎司
体积 | 汤匙 | 0.01478676478125 | tbsp, tablespoon, 大勺
体积 | 茶匙 | 0.00492892159375 | tsp, teaspoon, 小勺
体积 | 立方英尺 | 28.316846592 | ft3, cuft, cu ft
体积 | 立方英寸 | 0.016387064 | in3, cuin
体积 | 桶 | 158.987294928 | bbl, barrel, 石油桶
体积 | 斗 | 10 | 市斗
速度 | 米每秒 | 1 | m/s, mps, 米/秒
速度 | 公里每小时 | 1000/3600 | km/h, kmh, kph, 千米每小时, 千米/小时, 公里/小时, 公里/时, 时速, 码
速度 | 千米每秒 | 1000 | km/s, 公里每秒
速度 | 英里每小时 | 0.44704 | mph, 英里/小时
速度 | 节 | 1852/3600 | kn, knot, knots, kt
速度 | 英尺每秒 | 0.3048 | ft/s, fps
速度 | 光速 | 299792458 |
时间 | 秒 | 1 | s, sec, second, seconds, 秒钟
时间 | 毫秒 | 0.001 | ms, millisecond, milliseconds
时间 | 微秒 | 1e-6 | μs, us, microsecond
时间 | 纳秒 | 1e-9 | ns, nanosecond
时间 | 分钟 | 60 | min, mins, minute, minutes, 分
时间 | 小时 | 3600 | h, hr, hrs, hour, hours, 钟头
时间 | 天 | 86400 | d, day, days, 日
时间 | 周 | 604800 | wk, week, weeks, 星期, 礼拜
时间 | 个月 | 2629746 | 月, month, months, mo
时间 | 年 | 31556952 | yr, year, years
时间 | 刻钟 | 900 | 刻
时间 | 时辰 | 7200 |
时间 | 世纪 | 3155695200 | century
数据 | 字节 | 1 | B, byte, bytes
数据 | 比特 | 0.125 | b, bit, bits, 位
数据 | KB | 1000 | kB, 千字节, kilobyte
数据 | MB | 1e6 | 兆字节, 兆, megabyte
数据 | GB | 1e9 | 吉字节, gigabyte
数据 | TB | 1e12 | 太字节, terabyte
数据 | PB | 1e15 | petabyte
数据 | KiB | 1024 |
数据 | MiB | 2^20 |
数据 | GiB | 2^30 |
数据 | TiB | 2^40 |
数据 | PiB | 2^50 |
数据 | Kb | 125 | kbit, kilobit, 千比特
数据 | Mb | 125000 | Mbit, megabit, 兆比特, 兆位
数据 | Gb | 1.25e8 | Gbit, gigabit
数据 | Tb | 1.25e11 | Tbit, terabit
网速 | B/s | 1 | Bps, 字节/秒
网速 | KB/s | 1000 | kB/s, KBps
网速 | MB/s | 1e6 | MBps, 兆字节/秒, 兆/秒
网速 | GB/s | 1e9 | GBps
网速 | KiB/s | 1024 |
网速 | MiB/s | 2^20 |
网速 | bps | 0.125 | bit/s, b/s, 比特/秒
网速 | Kbps | 125 | kbps, kbit/s, Kb/s
网速 | Mbps | 125000 | mbps, Mbit/s, Mb/s, 兆比特/秒, 兆位/秒, M宽带, 兆宽带
网速 | Gbps | 1.25e8 | gbps, Gbit/s, Gb/s
能量 | 焦耳 | 1 | J, joule, joules, 焦
能量 | 千焦 | 1000 | kJ, kilojoule
能量 | 兆焦 | 1e6 | MJ
能量 | 卡 | 4.184 | cal, calorie, calories, 卡路里
能量 | 千卡 | 4184 | kcal, Cal, 大卡, kilocalorie
能量 | 瓦时 | 3600 | Wh
能量 | 千瓦时 | 3.6e6 | kWh, kwh, 度, 度电
能量 | 电子伏特 | 1.602176634e-19 | eV, 电子伏
能量 | 英热单位 | 1055.05585262 | BTU, Btu
能量 | 吨标准煤 | 29307600000 | tce
功率 | 瓦 | 1 | W, watt, watts, 瓦特
功率 | 毫瓦 | 0.001 | mW
功率 | 千瓦 | 1000 | kW, kw
功率 | 兆瓦 | 1e6 | MW
功率 | 马力 | 735.49875 | 公制马力, PS, ps, cv
功率 | 英制马力 | 745.69987158227022 | hp, HP, bhp
功率 | 英热单位每小时 | 0.29307107017222222 | BTU/h, Btu/h
功率 | 千卡每小时 | 4184/3600 | kcal/h, 大卡/小时
压强 | 帕 | 1 | Pa, pa, 帕斯卡
压强 | 百帕 | 100 | hPa, hpa, 毫巴, mbar
压强 | 千帕 | 1000 | kPa, kpa
压强 | 兆帕 | 1e6 | MPa, mpa
压强 | 巴 | 1e5 | bar
压强 | 标准大气压 | 101325 | atm, 大气压
压强 | 毫米汞柱 | 133.322387415 | mmHg, mmhg
压强 | 磅每平方英寸 | 6894.757293168 | psi, PSI, 磅/平方英寸
压强 | 公斤力每平方厘米 | 98066.5 | kgf/cm2, 公斤, 千克力/平方厘米
温度 | 摄氏度 | C | ℃, °C, C, c, celsius, 摄氏, 度
温度 | 华氏度 | F | ℉, °F, F, f, fahrenheit, 华氏
温度 | 开尔文 | K | K, k, kelvin, 开
"""
DEFAULTS = {
    "长度": ["米", "厘米", "千米", "英尺", "英寸", "尺"], "重量": ["千克", "克", "斤", "磅", "盎司"],
    "面积": ["平方米", "亩", "公顷", "平方千米", "平方英尺", "英亩"], "体积": ["升", "毫升", "立方米", "加仑"],
    "速度": ["公里每小时", "米每秒", "英里每小时", "节"], "时间": ["秒", "分钟", "小时", "天"],
    "数据": ["字节", "KB", "MB", "GB", "MiB", "GiB"], "网速": ["Mbps", "MB/s", "KB/s"],
    "能量": ["焦耳", "千焦", "千卡", "千瓦时"], "功率": ["瓦", "千瓦", "马力", "英制马力"],
    "压强": ["千帕", "巴", "磅每平方英寸", "标准大气压", "公斤力每平方厘米"], "温度": ["摄氏度", "华氏度", "开尔文"],
}
BINARY = {"KiB", "MiB", "GiB", "TiB", "PiB", "KiB/s", "MiB/s"}
DECIMAL_DATA = {"KB", "MB", "GB", "TB", "PB", "KB/s", "MB/s", "GB/s"}

SCHEMA = {"type": "function", "function": {
    "name": "unit_convert",
    "description": "单位换算，换算单位时一定用它，不要自己记倍数：长度、重量、面积、体积、温度、速度、时间、数据大小、网速"
                   "（100M 宽带是 Mbps）、能量、功率、压强，中文英文单位名都认，也认斤、两、亩、尺、寸、里。"
                   "value 是数，from 是原来的单位，to 是要换成的单位（几个用逗号分开，不写就换成常用的几个）。"
                   "几个单位加在一起的（5尺3寸、5 ft 11 in、1小时30分）整串写进 value，from 不写。",
    "parameters": {"type": "object", "properties": {
        "value": {"type": "string", "description": "数值（比如 3.5），或者“5尺3寸”这种带单位的一串"},
        "from": {"type": "string", "description": "原来的单位，比如 斤、km/h、°F、GB"},
        "to": {"type": "string", "description": "换成什么单位，几个用逗号分开；不写就换成常用的几个"}},
        "required": ["value"]}}}


def _key(s: str) -> str:
    """单位名统一写法：全角半角、上标（m² → m2）、空格、“每”和“/”都不计较。"""
    s = unicodedata.normalize("NFKC", str(s)).strip().replace("每", "/").replace("／", "/")
    s = re.sub(r"\s+", "", s)
    return s[1:] if s.startswith("个") and len(s) > 1 else s   # 3个月、2个小时


def _factor(s: str) -> Decimal:
    with localcontext(CTX):
        if "/" in s:
            a, b = s.split("/")
            return Decimal(a) / Decimal(b)
        if "^" in s:
            a, b = s.split("^")
            return Decimal(a) ** int(b)
        return Decimal(s)


class Unit:
    def __init__(self, cat: str, name: str, factor: str):
        self.cat, self.name = cat, name
        self.temp = factor if cat == "温度" else ""
        self.factor = None if self.temp else _factor(factor)

    def to_base(self, v: Decimal) -> Decimal:
        """换成这一类的基本单位（温度是开尔文）。"""
        if self.temp == "C":
            return v + Decimal("273.15")
        if self.temp == "F":
            return (v - 32) * 5 / 9 + Decimal("273.15")
        if self.temp == "K":
            return v
        return v * self.factor

    def from_base(self, v: Decimal) -> Decimal:
        if self.temp == "C":
            return v - Decimal("273.15")
        if self.temp == "F":
            return (v - Decimal("273.15")) * 9 / 5 + 32
        if self.temp == "K":
            return v
        return v / self.factor


UNITS: dict[str, Unit] = {}
EXACT: dict[str, list[Unit]] = {}
LOWER: dict[str, list[Unit]] = {}
for _line in TABLE.strip().splitlines():
    _cat, _name, _factor_s, _aliases = (x.strip() for x in _line.split("|"))
    _u = UNITS[_name] = Unit(_cat, _name, _factor_s)
    for _a in [_name, *(_x.strip() for _x in _aliases.split(",") if _x.strip())]:
        for _d, _k in ((EXACT, _key(_a)), (LOWER, _key(_a).lower())):
            if _u not in _d.setdefault(_k, []):
                _d[_k].append(_u)


def find(name: str) -> list[Unit]:
    """这个名字可能是哪些单位：先按大小写一样找，找不到再不分大小写（kb 当 KB）。"""
    k = _key(name)
    if not k:
        raise ValueError("单位是空的")
    got = EXACT.get(k) or LOWER.get(k.lower())
    if not got:
        raise ValueError(f"不认识单位“{name}”")
    return got


def pick(cands: list[Unit], cat: str | None) -> Unit | None:
    return next((u for u in cands if cat is None or u.cat == cat), None)


def _num(v) -> Decimal:
    try:
        return Decimal(str(v).replace(",", "").strip())
    except DecimalException:
        raise ValueError(f"“{v}”不是数") from None


PAIR = re.compile(r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([^\d\s+-][^\d]*?)\s*(?=[-+]?\d|$)")


def parse_value(value, unit: str | None) -> list[tuple[Decimal, list[Unit], str]]:
    """value + from → [(数, 可能的单位, 原文)]。from 没写时从 value 里拆“5尺3寸”。"""
    if unit:
        return [(_num(value), find(unit), unit.strip())]
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if m := re.fullmatch(r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(\D.*)", text):
        try:
            return [(_num(m.group(1)), find(m.group(2)), m.group(2).strip())]   # 120 m2、3 kgf/cm2：单位里带数字
        except ValueError:
            pass
    pairs = PAIR.findall(text)
    if not pairs or "".join(n + u for n, u in pairs).replace(" ", "") != text.replace(" ", ""):
        raise ValueError("没写原来的单位（from），value 里也没拆出“数 + 单位”")
    return [(_num(n), find(u), u.strip()) for n, u in pairs]


def show(x: Decimal) -> tuple[str, bool]:
    """最多 8 位有效数字；大数加千分位，很大很小的用科学计数法。返回 (文字, 是不是截断了)。"""
    with localcontext(CTX):
        if x == 0:
            return "0", False
        r = Decimal(f"{x:.{DIGITS}g}")
        approx = r != x
        r = r.normalize()
        if r.adjusted() >= 15 or r.adjusted() < -6 or (approx and r.adjusted() >= DIGITS):   # 别写出一串假的 0
            return f"{r:E}".replace("E", "×10^").replace("^+", "^"), approx
        return (f"{r:,f}" if abs(r) >= 10000 else f"{r:f}"), approx


def convert(args: dict) -> str:
    items = parse_value(args.get("value"), args.get("from"))
    targets = [t.strip() for t in re.split(r"[,，、;；]|和", str(args.get("to") or "")) if t.strip()]
    # 定类别：to 写了就按 to 和 from 都能对上的那一类挑（“度”换成华氏度就是温度，换成焦耳就是能量）
    first = items[0][1]
    cat = None
    if targets:
        tcands = find(targets[0])
        cat = next((u.cat for u in first if any(t.cat == u.cat for t in tcands)), None)
        if cat is None:
            raise ValueError(f"{items[0][2]}（{first[0].cat}）和 {targets[0]}（{tcands[0].cat}）不是一类，换不了")
    cat = cat or first[0].cat
    srcs = []
    for n, cands, raw in items:
        u = pick(cands, cat)
        if not u:
            raise ValueError(f"{raw} 不是{cat}单位，不能和前面的加在一起")
        srcs.append((n, u))
    if len(srcs) > 1 and cat == "温度":
        raise ValueError("温度不能加在一起")
    with localcontext(CTX):
        base = sum((u.to_base(n) for n, u in srcs), Decimal(0))
        if cat == "温度" and base < 0:
            raise ValueError("比绝对零度（-273.15 °C）还低，不存在")
        outs = []
        for t in targets or [n for n in DEFAULTS[cat] if n != srcs[0][1].name]:
            u = pick(find(t), cat)
            if not u:
                raise ValueError(f"{t} 不是{cat}单位，换不了")
            outs.append(u)
        left = " ".join(f"{show(n)[0]} {u.name}" for n, u in srcs)
        parts = []
        for u in outs:
            text, approx = show(u.from_base(base))
            parts.append(f"{'≈' if approx else '='} {text} {u.name}")
    out = f"{left} " + " ".join(parts)
    names = {u.name for _, u in srcs} | {u.name for u in outs}
    if names & BINARY and names & DECIMAL_DATA:
        out += "\n（KB、MB、GB 按 1000 进，KiB、MiB、GiB 按 1024 进。硬盘标的 1 TB 是 10¹² 字节，Windows 里显示的“GB”其实是 GiB，所以看着少了。）"
    if cat == "网速" and names & {"Mbps", "Kbps", "Gbps", "bps"} and names & {"MB/s", "KB/s", "GB/s", "B/s", "KiB/s", "MiB/s"}:
        out += "\n（宽带、网卡说的“兆”是 Mbps，下载软件显示的是 MB/s，差 8 倍；实际还要再打点折扣。）"
    return out


def apply(ctx):
    async def run(call):
        try:
            return convert(call.args)
        except (ValueError, TypeError, ArithmeticError) as e:
            return f"没换算：{e}"
    ctx.tool(Tool("unit_convert", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="单位换算：长度、重量、面积、体积、温度、速度、时间、数据大小、网速、能量、功率、压强，含斤两亩尺寸",
                  progress=lambda a: f"换算 {a.get('value')} {a.get('from') or ''} → {a.get('to') or '常用单位'}"[:60]))
