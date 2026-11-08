"""农历：公历和农历互相换算（1900–2100 年），闰月、干支、生肖、二十四节气、传统节日，农历生日今年是公历哪天。

大模型记不住农历，常把日子说错一两天，闰月更是靠猜。装上以后问农历、节气、过节、农历生日，Bot 都调这个工具，对话里和后台任务里都能用。

- 农历按常见的 lunarInfo 表（每年一个数：哪个月大、哪个月小、闰几月），和寿星天文历（lunar-python）逐日对过。
  只有 2057 年八月和九月交界那次朔在北京时间午夜前后几秒，各家算法说法不一，这里按常见的表：2057-09-28 是九月初一。
- 节气是用寿星天文历算好的每个节气在当月的哪一天（北京时间），压成每年 12 个十六进制数字；又用 ephem 独立算了一遍，
  1900–2100 年只有 1951 年冬至（正好在午夜前后几秒）两边不一样，这里按寿星天文历的 12 月 23 日。
- 干支年、生肖按农历正月初一换（不是按立春）。八字、黄历宜忌不管。
- 农历生日：那个月只有 29 天、生日是三十的，按廿九算；闰月生的，那年没有这个闰月就过正常的那个月。
- “今天”按服务器的时区算。
"""
from bisect import bisect_right
from datetime import date, timedelta
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "农历", "version": "1.0", "author": "open-super-agent"}

# 每年一个数（农历年）：低 4 位是闰几月（0 是没有），第 16 到第 5 位依次是正月到腊月大不大（1 是 30 天），第 17 位是闰月大不大。
INFO = (
    0x04bd8, 0x04ae0, 0x0a570, 0x054d5, 0x0d260, 0x0d950, 0x16554, 0x056a0, 0x09ad0, 0x055d2,  # 1900–1909
    0x04ae0, 0x0a5b6, 0x0a4d0, 0x0d250, 0x1d255, 0x0b540, 0x0d6a0, 0x0ada2, 0x095b0, 0x14977,  # 1910–1919
    0x04970, 0x0a4b0, 0x0b4b5, 0x06a50, 0x06d40, 0x1ab54, 0x02b60, 0x09570, 0x052f2, 0x04970,  # 1920–1929
    0x06566, 0x0d4a0, 0x0ea50, 0x16a95, 0x05ad0, 0x02b60, 0x186e3, 0x092e0, 0x1c8d7, 0x0c950,  # 1930–1939
    0x0d4a0, 0x1d8a6, 0x0b550, 0x056a0, 0x1a5b4, 0x025d0, 0x092d0, 0x0d2b2, 0x0a950, 0x0b557,  # 1940–1949
    0x06ca0, 0x0b550, 0x15355, 0x04da0, 0x0a5b0, 0x14573, 0x052b0, 0x0a9a8, 0x0e950, 0x06aa0,  # 1950–1959
    0x0aea6, 0x0ab50, 0x04b60, 0x0aae4, 0x0a570, 0x05260, 0x0f263, 0x0d950, 0x05b57, 0x056a0,  # 1960–1969
    0x096d0, 0x04dd5, 0x04ad0, 0x0a4d0, 0x0d4d4, 0x0d250, 0x0d558, 0x0b540, 0x0b6a0, 0x195a6,  # 1970–1979
    0x095b0, 0x049b0, 0x0a974, 0x0a4b0, 0x0b27a, 0x06a50, 0x06d40, 0x0af46, 0x0ab60, 0x09570,  # 1980–1989
    0x04af5, 0x04970, 0x064b0, 0x074a3, 0x0ea50, 0x06b58, 0x05ac0, 0x0ab60, 0x096d5, 0x092e0,  # 1990–1999
    0x0c960, 0x0d954, 0x0d4a0, 0x0da50, 0x07552, 0x056a0, 0x0abb7, 0x025d0, 0x092d0, 0x0cab5,  # 2000–2009
    0x0a950, 0x0b4a0, 0x0baa4, 0x0ad50, 0x055d9, 0x04ba0, 0x0a5b0, 0x15176, 0x052b0, 0x0a930,  # 2010–2019
    0x07954, 0x06aa0, 0x0ad50, 0x05b52, 0x04b60, 0x0a6e6, 0x0a4e0, 0x0d260, 0x0ea65, 0x0d530,  # 2020–2029
    0x05aa0, 0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0, 0x1d0b6, 0x0d250, 0x0d520, 0x0dd45,  # 2030–2039
    0x0b5a0, 0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0, 0x0aa50, 0x1b255, 0x06d20, 0x0ada0,  # 2040–2049
    0x14b63, 0x09370, 0x049f8, 0x04970, 0x064b0, 0x168a6, 0x0ea50, 0x06aa0, 0x1a6c4, 0x0aae0,  # 2050–2059
    0x092e0, 0x0d2e3, 0x0c960, 0x0d557, 0x0d4a0, 0x0da50, 0x05d55, 0x056a0, 0x0a6d0, 0x055d4,  # 2060–2069
    0x052d0, 0x0a9b8, 0x0a950, 0x0b4a0, 0x0b6a6, 0x0ad50, 0x055a0, 0x0aba4, 0x0a5b0, 0x052b0,  # 2070–2079
    0x0b273, 0x06930, 0x07337, 0x06aa0, 0x0ad50, 0x14b55, 0x04b60, 0x0a570, 0x054e4, 0x0d160,  # 2080–2089
    0x0e968, 0x0d520, 0x0daa0, 0x16aa6, 0x056d0, 0x04ae0, 0x0a9d4, 0x0a2d0, 0x0d150, 0x0f252,  # 2090–2099
    0x0d520,  # 2100
)
FIRST = date(1900, 1, 31)          # 农历 1900 年正月初一
LAST_YEAR = 1900 + len(INFO) - 1   # 2100

TERM_NAMES = ("小寒", "大寒", "立春", "雨水", "惊蛰", "春分", "清明", "谷雨", "立夏", "小满", "芒种", "夏至",
              "小暑", "大暑", "立秋", "处暑", "白露", "秋分", "寒露", "霜降", "立冬", "小雪", "大雪", "冬至")
# 第 i 个节气在第 i // 2 + 1 月；1900–2100 年里最早是这个月的几号。TERMS 每年 12 个十六进制数字（48 位），
# 两位一个节气，存的是比 TERM_BASE 晚几天（0–3）
TERM_BASE = (4, 19, 3, 18, 4, 19, 4, 19, 4, 20, 4, 20, 6, 22, 6, 22, 6, 22, 7, 22, 6, 21, 6, 21)
TERMS = (
    "95a59a599aa5a5a6aa9aaaa9a9aaaeaaaaaaaafaeeaeeaaaeaa59a599aa5a5a6aa9aaaa9a9aaaaaaaaaaaafaeeaeeaaaeaa59a599aa5a5a6aa9aaaa9"  # 1900
    "a9aaaaaaaaaaaafaeeaeeaaaeaa59a599a9595a6aa9a9aa9a5a6aaaaaaaaaabaaeaaeaaaaaa59a59969595a69a9a9aa9a5a6aaaaaaa9aabaaeaaeaaa"  # 1910
    "aaa59a59969595a59a9a9aa5a5a6aaaaaaa9a9aaaeaaeaaaaaa59a59969595a59a9a9aa5a5a6aa9aaaa9a9aaaeaaeaaaaaa59a59955595a59a599aa5"  # 1920
    "a5a6aa9aaaa9a9aaaeaaaaaaaaa59959955595a59a599aa5a5a6aa9aaaa9a9aaaaaaaaaaaaa59959955595a59a599aa5a5a6aa9aaaa9a9aaaaaaaaaa"  # 1930
    "aaa59959955595a59a599aa5a5a6aa9aaaa9a9aaaaaaaaaaaaa55959955595a59a59969595a6aa9a9aa9a5a6aaaaaaaaaa655955955555a59a599695"  # 1940
    "95a59a9a9aa9a5a6aaaaaaaaaa655955955555a59a59969595a59a9a9aa5a5a6aa9aaaa9aa555955955555a59a59969595a59a599aa5a5a6aa9aaaa9"  # 1950
    "a9555955555555a59959955595a59a599aa5a5a6aa9aaaa9a9555955555555a59959955595a59a599aa5a5a6aa9aaaa9a9555555555555a599599555"  # 1960
    "95a59a599aa5a5a6aa9aaaa9a9555555555555a55959955595a59a599aa5a5a6aa9a9aa9a9515555555555a55955955595a59a5996a595a69a9a9aa9"  # 1970
    "a9515555555555a55955955595a59a59969595a59a9a9aa9a5515545555555655955955555a59a59969595a59a5a9aa5a55155455554555559555555"  # 1980
    "55a59959969595a59a599aa5a5515545555454555955555555a59959955595a59a599aa5a5515545555454555555555555a59959955595a59a599aa5"  # 1990
    "a5515545555454555555555555a59959955595a59a599aa5a5515545555454555555555555a55955955595a59a599aa5a55155454554545155555555"  # 2000
    "55a55955955595a59a5996a5a5514545455454515545555555a55955955595a59a59969595504545455450515545555555655955955555a599599695"  # 2010
    "95504504455450515545555455555955555555a59959969595504504455050515545555454555555555555a599599555955045044550505155455554"  # 2020
    "54555555555555a59959955595504504455050515545555454555555555555a55955955595504504455050515545555454555555555555a559559555"  # 2030
    "95504504455050514545455454515555555555a55955955595504504415050504545455454515545555555a559559555955044044140405045044554"  # 2040
    "505155455555555559555555555044044140405045044554505155455555555555555555555044044140405045044550505155455554555555555555"  # 2050
    "555044044000405045044550505155455554545555555555555044044000405045044550505155455554545555555555555004004000405045044550"  # 2060
    "505145454554545555555555555004004000405045044150505145454554545155455555555004004000405045044150505045054554545155455555"  # 2070
    "555004004000405044044140505045044554505155455555550004000000005044044140405045044554505155455555550000000000005044044140"  # 2080
    "405045044550505155455554550000000000005044044000405045044550505155455554540000000000005044004000405045044550505155455554"  # 2090
    "545555555555"  # 2100
)

STEMS, BRANCHES, ANIMALS = "甲乙丙丁戊己庚辛壬癸", "子丑寅卯辰巳午未申酉戌亥", "鼠牛虎兔龙蛇马羊猴鸡狗猪"
MONTHS = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊")
WEEK = "一二三四五六日"
# (月, 日, 名字)；除夕是腊月最后一天，单独算
FESTIVALS = ((1, 1, "春节"), (1, 15, "元宵节"), (2, 2, "龙抬头"), (5, 5, "端午节"), (7, 7, "七夕"),
             (7, 15, "中元节"), (8, 15, "中秋节"), (9, 9, "重阳节"), (12, 8, "腊八节"), (12, 23, "北方小年"), (12, 24, "南方小年"))
TERM_FESTIVALS = {"清明": "清明节", "冬至": "冬至"}

SCHEMA = {"type": "function", "function": {
    "name": "lunar",
    "description": "农历（1900–2100 年），问农历日子、节气、传统节日、农历生日时一定用它，不要自己推算。"
                   "op=to_lunar：公历 date 是农历几月几日、什么年（干支、生肖）、是不是节气或节日、下一个节气；"
                   "op=to_solar：农历 year 年 month 月 day 日（闰月 leap=true）是公历哪天；"
                   "op=festivals：公历 year 年的传统节日（春节、元宵、端午、七夕、中秋、重阳、腊八、小年、除夕、清明、冬至）各是哪天；"
                   "op=terms：公历 year 年二十四节气各是哪天；"
                   "op=birthday：农历 month 月 day 日的生日，公历 year 年是哪天（不给 year 就是今年），还有从今天起下一次是哪天。"
                   "日期写 YYYY-MM-DD，date 不写就是今天；农历月份写数字，冬月是 11，腊月是 12。",
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["to_lunar", "to_solar", "festivals", "terms", "birthday"]},
        "date": {"type": "string", "description": "to_lunar 用：公历日期 YYYY-MM-DD，默认今天"},
        "year": {"type": "integer", "description": "to_solar 是农历年；festivals、terms、birthday 是公历年，默认今年"},
        "month": {"type": "integer", "description": "农历月 1–12"},
        "day": {"type": "integer", "description": "农历日 1–30"},
        "leap": {"type": "boolean", "description": "是不是闰月"}},
        "required": ["op"]}}}


def _info(y: int) -> int:
    if not 1900 <= y <= LAST_YEAR:
        raise ValueError(f"只能算 1900 到 {LAST_YEAR} 年")
    return INFO[y - 1900]


def leap_month(y: int) -> int:
    """这年闰几月，没有闰月是 0。"""
    return _info(y) & 0xF


def months(y: int) -> list[tuple[int, bool, int]]:
    """农历 y 年的月份，按顺序：(几月, 是不是闰月, 几天)。"""
    v, leap = _info(y), leap_month(y)
    out = []
    for m in range(1, 13):
        out.append((m, False, 30 if v & (0x10000 >> m) else 29))
        if m == leap:
            out.append((m, True, 30 if v & 0x10000 else 29))
    return out


def _year_starts() -> list[date]:
    starts, d = [], FIRST
    for y in range(1900, LAST_YEAR + 1):
        starts.append(d)
        d += timedelta(days=sum(n for _, _, n in months(y)))
    starts.append(d)   # 2101 年正月初一：最后一年到哪天为止
    return starts


STARTS = _year_starts()


def to_lunar(d: date) -> tuple[int, int, int, bool]:
    """公历 → 农历 (年, 月, 日, 是不是闰月)。"""
    if not STARTS[0] <= d < STARTS[-1]:
        raise ValueError(f"只能算 {STARTS[0]} 到 {STARTS[-1] - timedelta(days=1)}")
    i = bisect_right(STARTS, d) - 1
    y, left = 1900 + i, (d - STARTS[i]).days
    for m, leap, n in months(y):
        if left < n:
            return y, m, left + 1, leap
        left -= n
    raise AssertionError("算漏了")


def to_solar(y: int, m: int, day: int, leap: bool = False) -> date:
    """农历 → 公历。这年没有这个闰月、这个月没有这一天的，报错说明。"""
    if not 1 <= m <= 12:
        raise ValueError("农历月份是 1 到 12（冬月 11，腊月 12）")
    if leap and leap_month(y) != m:
        lm = leap_month(y)
        raise ValueError(f"农历 {y} 年没有闰{MONTHS[m - 1]}月" + (f"（这年闰{MONTHS[lm - 1]}月）" if lm else "（这年没有闰月）"))
    d = STARTS[y - 1900]
    for mm, lp, n in months(y):
        if mm == m and lp == bool(leap):
            if not 1 <= day <= n:
                raise ValueError(f"农历 {y} 年{'闰' if leap else ''}{MONTHS[m - 1]}月只有 {n} 天")
            return d + timedelta(days=day - 1)
        d += timedelta(days=n)
    raise AssertionError("算漏了")


def day_name(day: int) -> str:
    if day == 10:
        return "初十"
    if day == 20:
        return "二十"
    if day == 30:
        return "三十"
    return ("初", "十", "廿")[(day - 1) // 10] + "一二三四五六七八九"[(day - 1) % 10]


def month_name(m: int, leap: bool = False) -> str:
    return ("闰" if leap else "") + MONTHS[m - 1] + "月"


def ganzhi_year(y: int) -> str:
    return STEMS[(y - 4) % 10] + BRANCHES[(y - 4) % 12]


def animal(y: int) -> str:
    return ANIMALS[(y - 4) % 12]


def ganzhi_day(d: date) -> str:
    i = (d.toordinal() + 14) % 60      # 0 是甲子日（公历 1 年 1 月 1 日起数，和儒略日 +49 一样）
    return STEMS[i % 10] + BRANCHES[i % 12]


def terms(y: int) -> list[tuple[str, date]]:
    """公历 y 年的 24 个节气。"""
    if not 1900 <= y <= 2100:
        raise ValueError("节气只能算 1900 到 2100 年")
    k = (y - 1900) * 12
    v = int(TERMS[k:k + 12], 16)
    return [(TERM_NAMES[i], date(y, i // 2 + 1, TERM_BASE[i] + ((v >> (2 * (23 - i))) & 3))) for i in range(24)]


def _terms_around(d: date) -> list[tuple[str, date]]:
    return [t for y in (d.year, d.year + 1) if y <= 2100 for t in terms(y)]


def festivals(y: int) -> list[tuple[str, date]]:
    """公历 y 年里的传统节日，按日期排。"""
    out = []
    for ly in (y - 1, y):
        if not 1900 <= ly <= LAST_YEAR:
            continue
        for m, day, name in FESTIVALS:
            out.append((name, to_solar(ly, m, day)))
        out.append(("除夕", STARTS[ly - 1900 + 1] - timedelta(days=1)))
    out += [(TERM_FESTIVALS[n], d) for n, d in terms(y) if n in TERM_FESTIVALS]
    return sorted((x for x in out if x[1].year == y), key=lambda x: x[1])


def _fmt(d: date) -> str:
    return f"{d.isoformat()}（星期{WEEK[d.weekday()]}）"


def _away(d: date, today: date) -> str:
    n = (d - today).days
    return "就是今天" if n == 0 else f"还有 {n} 天" if n > 0 else f"已经过去 {-n} 天"


def lunar_str(y: int, m: int, day: int, leap: bool) -> str:
    return f"农历{ganzhi_year(y)}{animal(y)}年（{y}）{month_name(m, leap)}{day_name(day)}"


def festival_of(d: date) -> list[str]:
    """这天是哪些农历节日（闰月里的不算：闰五月初五不是端午）。清明、冬至是节气，另外说。"""
    y, m, day, leap = to_lunar(d)
    names = [n for mm, dd, n in FESTIVALS if not leap and (mm, dd) == (m, day)]
    if d + timedelta(days=1) == STARTS[y - 1900 + 1]:
        names.append("除夕")
    return names


def term_on(d: date) -> str:
    return next((n for n, td in terms(d.year) if td == d), "") if 1900 <= d.year <= 2100 else ""


def _birthday_in(ly: int, m: int, day: int, leap: bool) -> tuple[date, str]:
    """农历 ly 年的这个生日是公历哪天，加一句怎么处理的（闰月、三十）。"""
    note = ""
    use_leap = leap and leap_month(ly) == m
    if leap and not use_leap:
        note = f"这年没有闰{MONTHS[m - 1]}月，按{month_name(m)}算"
    n = next(n for mm, lp, n in months(ly) if mm == m and lp == use_leap)
    if day > n:
        note = (note + "；" if note else "") + f"这年{month_name(m, use_leap)}只有 {n} 天，按{day_name(n)}算"
        day = n
    return to_solar(ly, m, day, use_leap), note


def _date(s, today: date) -> date:
    return date.fromisoformat(str(s).strip()) if s else today


def calc(args: dict, today: date | None = None) -> str:
    today = today or date.today()
    op = args.get("op") or "to_lunar"
    if op == "to_lunar":
        d = _date(args.get("date"), today)
        y, m, day, leap = to_lunar(d)
        n = next(n for mm, lp, n in months(y) if mm == m and lp == leap)
        lines = [f"{_fmt(d)}是{lunar_str(y, m, day, leap)}，{ganzhi_day(d)}日。"
                 f"这个月是{'大' if n == 30 else '小'}月（{n} 天）" + (f"，{y} 年闰{MONTHS[leap_month(y) - 1]}月。" if leap_month(y) else "，这年没有闰月。")]
        if fest := festival_of(d):
            lines.append("这天是" + "、".join(fest) + "。")
        if term := term_on(d):
            lines.append(f"这天是节气：{term}" + ("（清明节）" if term == "清明" else "") + "。")
        around = _terms_around(d)
        if nxt := next(((n, td) for n, td in around if td > d), None):
            lines.append(f"下一个节气：{nxt[0]} {_fmt(nxt[1])}，{_away(nxt[1], d)}。")
        return "\n".join(lines)
    if op == "to_solar":
        y, m, day = (int(args.get(k) or 0) for k in ("year", "month", "day"))
        leap = bool(args.get("leap"))
        if not y:
            y = to_lunar(today)[0]
        d = to_solar(y, m, day, leap)
        fest = festival_of(d) + ([term_on(d)] if term_on(d) else [])
        return (f"{lunar_str(y, m, day, leap)} 是公历 {_fmt(d)}，{_away(d, today)}。"
                + (f"这天是{'、'.join(fest)}。" if fest else ""))
    if op == "festivals":
        y = int(args.get("year") or today.year)
        rows = [f"- {n}：{_fmt(d)}，{lunar_str(*to_lunar(d))[2:]}" for n, d in festivals(y)]
        return f"{y} 年的传统节日（公历日期）：\n" + "\n".join(rows)
    if op == "terms":
        y = int(args.get("year") or today.year)
        return f"{y} 年二十四节气（北京时间）：\n" + "\n".join(f"- {n}：{_fmt(d)}" for n, d in terms(y))
    if op == "birthday":
        m, day = int(args.get("month") or 0), int(args.get("day") or 0)
        leap = bool(args.get("leap"))
        if not 1 <= m <= 12 or not 1 <= day <= 30:
            raise ValueError("农历生日要写 month（1–12）和 day（1–30）")
        label = f"农历{month_name(m, leap)}{day_name(day)}的生日"
        y = int(args.get("year") or today.year)
        hits = []
        for ly in (y - 1, y):
            if 1900 <= ly <= LAST_YEAR:
                d, note = _birthday_in(ly, m, day, leap)
                if d.year == y:
                    hits.append(f"{_fmt(d)}，那天是{lunar_str(*to_lunar(d))[2:]}" + (f"（{note}）" if note else ""))
        lines = [f"{label}，公历 {y} 年是 " + "；还有 ".join(hits) + "。" if hits else f"{label}，公历 {y} 年里没有（落在前一年年底或后一年年初）。"]
        ly = to_lunar(today)[0]
        for cand in (ly - 1, ly, ly + 1):
            if 1900 <= cand <= LAST_YEAR:
                d, note = _birthday_in(cand, m, day, leap)
                if d >= today:
                    lines.append(f"从今天起下一次：{_fmt(d)}，{_away(d, today)}" + (f"（{note}）" if note else "") + "。")
                    break
        return "\n".join(lines)
    raise ValueError(f"不认识的 op：{op}")


def apply(ctx):
    async def run(call):
        try:
            return calc(call.args)
        except (ValueError, TypeError) as e:
            return f"没算：{e}"
    ctx.tool(Tool("lunar", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="农历：公历农历互换、闰月、干支生肖、二十四节气、传统节日、农历生日",
                  progress=lambda a: f"查农历：{a.get('date') or a.get('year') or '今天'}"))
