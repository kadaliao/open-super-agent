"""时区换算：某个城市、时区现在几点；某地的某个时间是另外几个地方的几点（“北京时间明天 9 点是纽约几点”），会说清夏令时和差了几天。

大模型算时差常忘了夏令时、算错日期（跨到前一天、次日）。装上以后 Bot 遇到时区问题就调这个工具，对话里和后台任务里都能用。

- 认大约 100 个常用城市和国家的中文、英文名（北京、纽约、旧金山、伦敦、悉尼……），也认 IANA 时区名（America/New_York）、
  UTC+8、GMT-5:30，还有 EST、PST、JST、美东、美西这些说法（都按当地实际时间算，夏天自动是夏令时）。
- 美国、加拿大、澳大利亚这些有好几个时区的国家，列出主要的几个。
- 设置里的 home 是你所在的时区（默认 Asia/Shanghai）：time 没说是哪里的时间时按它算，时差也是跟它比。
- 用的是这台服务器的时区数据库（zoneinfo）；没有的话（极少数精简系统）装一下 tzdata。
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "时区换算", "version": "1.0", "author": "open-super-agent", "config": {"home": "Asia/Shanghai"}}
WEEK = "一二三四五六日"
MAX_PLACES = 12

# 名字（中文、英文、简称，空格分开） = IANA 时区
CITIES = """
北京 beijing 上海 shanghai 广州 guangzhou 深圳 shenzhen 成都 chengdu 重庆 chongqing 杭州 hangzhou 武汉 wuhan 西安 xian 南京 nanjing 天津 tianjin 中国 china 北京时间 bjt = Asia/Shanghai
新疆时间 = Asia/Urumqi
香港 hongkong hong_kong hk hkt = Asia/Hong_Kong
澳门 macau macao = Asia/Macau
台北 taipei 台湾 taiwan = Asia/Taipei
东京 tokyo 大阪 osaka 日本 japan jst = Asia/Tokyo
首尔 seoul 韩国 korea kst = Asia/Seoul
平壤 pyongyang 朝鲜 = Asia/Pyongyang
乌兰巴托 ulaanbaatar 蒙古 = Asia/Ulaanbaatar
海参崴 符拉迪沃斯托克 vladivostok = Asia/Vladivostok
新加坡 singapore sgt = Asia/Singapore
吉隆坡 kualalumpur 马来西亚 malaysia = Asia/Kuala_Lumpur
曼谷 bangkok 泰国 thailand = Asia/Bangkok
雅加达 jakarta 印尼 印度尼西亚 indonesia = Asia/Jakarta
马尼拉 manila 菲律宾 philippines = Asia/Manila
河内 hanoi 胡志明市 hochiminh 西贡 越南 vietnam = Asia/Ho_Chi_Minh
金边 phnompenh 柬埔寨 = Asia/Phnom_Penh
仰光 yangon 缅甸 = Asia/Yangon
新德里 newdelhi 德里 delhi 孟买 mumbai 班加罗尔 bangalore 印度 india ist = Asia/Kolkata
加德满都 kathmandu 尼泊尔 = Asia/Kathmandu
卡拉奇 karachi 伊斯兰堡 islamabad 巴基斯坦 pakistan = Asia/Karachi
达卡 dhaka 孟加拉 = Asia/Dhaka
科伦坡 colombo 斯里兰卡 = Asia/Colombo
阿拉木图 almaty 哈萨克斯坦 = Asia/Almaty
塔什干 tashkent 乌兹别克斯坦 = Asia/Tashkent
迪拜 dubai 阿布扎比 abudhabi 阿联酋 uae = Asia/Dubai
多哈 doha 卡塔尔 qatar = Asia/Qatar
利雅得 riyadh 沙特 saudi = Asia/Riyadh
德黑兰 tehran 伊朗 iran = Asia/Tehran
耶路撒冷 jerusalem 特拉维夫 telaviv 以色列 israel = Asia/Jerusalem
伊斯坦布尔 istanbul 土耳其 turkey = Europe/Istanbul
莫斯科 moscow 圣彼得堡 saintpetersburg 俄罗斯 russia msk = Europe/Moscow
伦敦 london 英国 uk britain 英国时间 bst = Europe/London
都柏林 dublin 爱尔兰 ireland = Europe/Dublin
巴黎 paris 法国 france = Europe/Paris
柏林 berlin 法兰克福 frankfurt 慕尼黑 munich 德国 germany cet cest 欧洲中部时间 = Europe/Berlin
罗马 rome 米兰 milan 意大利 italy = Europe/Rome
马德里 madrid 巴塞罗那 barcelona 西班牙 spain = Europe/Madrid
阿姆斯特丹 amsterdam 荷兰 netherlands = Europe/Amsterdam
布鲁塞尔 brussels 比利时 belgium = Europe/Brussels
苏黎世 zurich 日内瓦 geneva 瑞士 switzerland = Europe/Zurich
维也纳 vienna 奥地利 austria = Europe/Vienna
斯德哥尔摩 stockholm 瑞典 sweden = Europe/Stockholm
奥斯陆 oslo 挪威 norway = Europe/Oslo
哥本哈根 copenhagen 丹麦 denmark = Europe/Copenhagen
赫尔辛基 helsinki 芬兰 finland = Europe/Helsinki
华沙 warsaw 波兰 poland = Europe/Warsaw
布拉格 prague 捷克 = Europe/Prague
雅典 athens 希腊 greece = Europe/Athens
里斯本 lisbon 葡萄牙 portugal = Europe/Lisbon
基辅 kyiv kiev 乌克兰 ukraine = Europe/Kyiv
开罗 cairo 埃及 egypt = Africa/Cairo
约翰内斯堡 johannesburg 开普敦 capetown 南非 = Africa/Johannesburg
内罗毕 nairobi 肯尼亚 kenya = Africa/Nairobi
拉各斯 lagos 尼日利亚 nigeria = Africa/Lagos
卡萨布兰卡 casablanca 摩洛哥 morocco = Africa/Casablanca
纽约 newyork nyc ny 华盛顿 washington dc 波士顿 boston 迈阿密 miami 亚特兰大 atlanta 费城 philadelphia 美东 美国东部 东部时间 est edt et = America/New_York
芝加哥 chicago 休斯顿 houston 达拉斯 dallas 美中 美国中部 中部时间 ct = America/Chicago
丹佛 denver 盐湖城 saltlakecity 山地时间 mst mt = America/Denver
凤凰城 phoenix 菲尼克斯 亚利桑那 arizona = America/Phoenix
洛杉矶 losangeles la 旧金山 sanfrancisco sf 西雅图 seattle 圣何塞 sanjose 硅谷 siliconvalley 拉斯维加斯 lasvegas 美西 美国西部 太平洋时间 pst pdt pt = America/Los_Angeles
安克雷奇 anchorage 阿拉斯加 alaska = America/Anchorage
檀香山 火奴鲁鲁 honolulu 夏威夷 hawaii = Pacific/Honolulu
多伦多 toronto 渥太华 ottawa 蒙特利尔 montreal = America/Toronto
温哥华 vancouver = America/Vancouver
卡尔加里 calgary 埃德蒙顿 edmonton = America/Edmonton
哈利法克斯 halifax = America/Halifax
墨西哥城 mexicocity 墨西哥 mexico = America/Mexico_City
圣保罗 saopaulo 里约 里约热内卢 riodejaneiro 巴西 brazil = America/Sao_Paulo
布宜诺斯艾利斯 buenosaires 阿根廷 argentina = America/Argentina/Buenos_Aires
圣地亚哥 santiago 智利 chile = America/Santiago
利马 lima 秘鲁 peru = America/Lima
波哥大 bogota 哥伦比亚 colombia = America/Bogota
悉尼 sydney 堪培拉 canberra aest aedt = Australia/Sydney
墨尔本 melbourne = Australia/Melbourne
布里斯班 brisbane = Australia/Brisbane
珀斯 perth = Australia/Perth
阿德莱德 adelaide = Australia/Adelaide
奥克兰 auckland 惠灵顿 wellington 新西兰 newzealand nz = Pacific/Auckland
世界时 协调世界时 utc gmt z 格林尼治 = UTC
"""
# 有好几个时区的国家：列出主要的几个
COUNTRIES = {
    "美国": ["纽约", "芝加哥", "丹佛", "洛杉矶"], "usa": ["纽约", "芝加哥", "丹佛", "洛杉矶"],
    "us": ["纽约", "芝加哥", "丹佛", "洛杉矶"], "america": ["纽约", "芝加哥", "丹佛", "洛杉矶"],
    "加拿大": ["多伦多", "温哥华"], "canada": ["多伦多", "温哥华"],
    "澳大利亚": ["悉尼", "布里斯班", "珀斯"], "澳洲": ["悉尼", "布里斯班", "珀斯"], "australia": ["悉尼", "布里斯班", "珀斯"],
}
OLD_NAMES = {"Europe/Kyiv": "Europe/Kiev"}   # 老一点的时区数据库里叫 Kiev

PLACES: dict[str, tuple[str, str]] = {}   # 小写名字 -> (显示名, IANA)
NAMES: dict[str, str] = {}                # IANA -> 中文名（Asia/Shanghai 显示成北京）
for _line in CITIES.strip().splitlines():
    _names, _zone = _line.split("=")
    _words = _names.split()
    for _w in _words:   # 显示中文名；“北京时间”“bjt”这种显示成这一行的第一个名字
        PLACES.setdefault(_w.lower(), (_w if not _w.isascii() and not _w.endswith("时间") else _words[0], _zone.strip()))
    NAMES.setdefault(_zone.strip(), _words[0])
_IANA: dict[str, str] = {}


def _iana(name: str) -> str | None:
    """不分大小写找 IANA 名字（asia/tokyo 也行）。第一次用时才扫一遍时区数据库。"""
    if not _IANA:
        _IANA.update({n.lower(): n for n in available_timezones()})
    return _IANA.get(name.replace(" ", "_").lower())

SCHEMA = {"type": "function", "function": {
    "name": "world_clock",
    "description": "时区换算，问别的城市现在几点、某个时间换成别处是几点、两地时差时一定用它，不要自己算（会错在夏令时和日期上）。"
                   "places 写要看的地方（城市、国家、IANA 时区名或 UTC+8，几个用逗号分开）。"
                   "只问现在几点就不写 time；要换算就把 time 写成 YYYY-MM-DD HH:MM（也认 HH:MM、明天 9:00、下午3点），"
                   "from 是 time 所在的地方，不写就是用户自己的时区（设置里的 home，默认北京）。",
    "parameters": {"type": "object", "properties": {
        "places": {"type": "string", "description": "要看的地方，几个用逗号分开，比如 纽约, 伦敦, Asia/Tokyo"},
        "time": {"type": "string", "description": "要换算的时间，不写就是现在"},
        "from": {"type": "string", "description": "time 是哪里的时间，不写就是用户自己的时区"}},
        "required": []}}}


def _key(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).strip()
    s = re.sub(r"(时间|时区|time)$", "", s, flags=re.I) if s not in PLACES and len(s) > 2 else s
    return re.sub(r"[\s_\-.']", "", s).lower()


def zone(tz: str):
    """IANA 名字 → 时区对象，带上老名字的兜底。"""
    for name in (tz, OLD_NAMES.get(tz)):
        if name:
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError:
                continue
    raise ValueError(f"这台服务器的时区数据库里没有 {tz}（装一下 tzdata 包）")


def resolve(name: str) -> list[tuple[str, object, str]]:
    """一个地名 → [(显示名, 时区对象, IANA 名或 UTC±N)]。国家有几个时区的返回几个。"""
    raw = str(name).strip()
    if not raw:
        raise ValueError("地名是空的")
    k = _key(raw)
    if k in COUNTRIES:
        return [x for c in COUNTRIES[k] for x in resolve(c)]
    if hit := PLACES.get(k):
        return [(hit[0], zone(hit[1]), hit[1])]
    m = re.fullmatch(r"(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", unicodedata.normalize("NFKC", raw).lower())
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h > 14 or mi >= 60:
            raise ValueError(f"{raw} 不是有效的时差")
        delta = timedelta(hours=h, minutes=mi) * (1 if m.group(1) == "+" else -1)
        label = f"UTC{m.group(1)}{h:02d}:{mi:02d}"
        return [(label, timezone(delta), label)]
    if "/" in raw and (iana := _iana(raw)):
        return [(NAMES.get(iana, iana), zone(iana), iana)]
    raise ValueError(f"不认识“{raw}”：写城市名（中文或英文）、IANA 时区名（America/New_York）或 UTC+8")


def _offset(d: datetime) -> str:
    off = d.utcoffset() or timedelta(0)
    sign = "+" if off >= timedelta(0) else "-"
    m = abs(int(off.total_seconds())) // 60
    return f"UTC{sign}{m // 60:02d}:{m % 60:02d}"


def _gap(a: timedelta, b: timedelta) -> str:
    """a 比 b 早 / 晚多少：东边的时间“早”（数字大）。"""
    diff = int((a - b).total_seconds()) // 60
    if diff == 0:
        return "时间一样"
    h, m = divmod(abs(diff), 60)
    return ("早" if diff > 0 else "晚") + (f" {h} 小时" if h else "") + (f" {m} 分" if m else "")


def _dst(tz, d: datetime) -> str:
    """这地方用不用夏令时、现在是不是。"""
    if not isinstance(tz, ZoneInfo):
        return ""
    jan, jul = datetime(d.year, 1, 15, tzinfo=tz), datetime(d.year, 7, 15, tzinfo=tz)
    if not (jan.dst() or jul.dst()):
        return ""
    return "夏令时" if d.dst() else "冬令时（标准时间）"


def _next_change(tz, d: datetime, days: int = 14) -> str:
    """接下来 days 天里会不会切换夏令时：按小时往后找，找到就说当地哪天几点、拨快还是拨慢。"""
    if not isinstance(tz, ZoneInfo):
        return ""
    utc = d.astimezone(timezone.utc)
    off = d.utcoffset()
    for h in range(1, days * 24 + 1):
        t = (utc + timedelta(hours=h)).astimezone(tz)
        if t.utcoffset() != off:
            # 精确到分钟：在这一小时里往回找
            lo = utc + timedelta(hours=h - 1)
            for mi in range(0, 61, 1):
                tt = (lo + timedelta(minutes=mi)).astimezone(tz)
                if tt.utcoffset() != off:
                    t = tt
                    break
            way = "拨快" if t.utcoffset() > off else "拨慢"
            n = abs(int((t.utcoffset() - off).total_seconds())) // 60
            amount = f"{n // 60} 小时" if n % 60 == 0 else f"{n} 分钟"
            what = "开始夏令时" if t.dst() else "结束夏令时"
            wall = t.astimezone(timezone(off))   # 切换那一刻按旧时间是几点（“2 点拨回 1 点”说的是 2 点）
            return f"注意：当地 {wall:%m-%d %H:%M} {what}，时钟{way} {amount}"
    return ""


def _fmt(d: datetime) -> str:
    return f"{d:%Y-%m-%d %H:%M} 星期{WEEK[d.weekday()]}"


DAY_WORDS = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "今天": 0, "今日": 0, "今晚": 0, "昨天": -1, "昨日": -1, "前天": -2}
CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
          "十一": 11, "十二": 12}


def parse_time(text: str, now: datetime) -> datetime:
    """“2026-11-10 09:00”“11月10日 9:00”“明天上午9点半”“9pm”→ 当地的时间（不带时区）。now 是那个地方的现在。"""
    s = unicodedata.normalize("NFKC", str(text)).strip().lower().replace("：", ":")
    s = re.sub(r"(十[一二]?|[一二两三四五六七八九])(?=点)", lambda m: str(CN_NUM[m.group(1)]), s)
    day = now.date()
    for w, n in DAY_WORDS.items():
        if w in s:
            day = now.date() + timedelta(days=n)
            s = s.replace(w, " ", 1)
            if w == "今晚":
                s = "晚上" + s
            break
    if m := re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?", s):
        day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        s = s[:m.start()] + " " + s[m.end():]
    elif m := re.search(r"(?<!\d)(\d{1,2})[-/月](\d{1,2})(?:日|号)?(?![\d:])", s):
        day = datetime(now.year, int(m.group(1)), int(m.group(2))).date()
        s = s[:m.start()] + " " + s[m.end():]
    period = next((p for p in ("凌晨", "早上", "早晨", "上午", "中午", "下午", "傍晚", "晚上", "夜里", "半夜", "am", "pm") if p in s), "")
    if m := re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?", s):
        h, mi = int(m.group(1)), int(m.group(2))
    elif m := re.search(r"(\d{1,2})\s*(?:点|时)\s*(?:(\d{1,2})\s*分?|(半)|(一刻)|(三刻))?", s):
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 30 if m.group(3) else 15 if m.group(4) else 45 if m.group(5) else 0
    elif m := re.search(r"(\d{1,2})\s*(am|pm)", s):
        h, mi = int(m.group(1)), 0
    else:
        raise ValueError(f"看不懂时间“{text}”，写成 YYYY-MM-DD HH:MM")
    if (period in ("下午", "傍晚", "晚上", "夜里", "pm") and h < 12) or (period == "中午" and h < 6):   # 中午 1 点是 13 点
        h += 12
    elif period in ("凌晨", "半夜", "am") and h == 12:
        h = 0
    elif period in ("晚上", "夜里") and h == 12:
        h = 24
    if h == 24 and mi == 0:   # 晚上 12 点、24:00 是第二天 0 点
        return datetime.combine(day + timedelta(days=1), datetime.min.time())
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError(f"时间“{text}”不对：小时 0–23，分钟 0–59")
    return datetime.combine(day, datetime.min.time()).replace(hour=h, minute=mi)


def _when(day_shift: int) -> str:
    return {0: "", 1: "（次日）", -1: "（前一天）", 2: "（后天）", -2: "（前两天）"}.get(day_shift, f"（{day_shift:+d} 天）")


def _line(label: str, tz, iana: str, t: datetime, ref: datetime, ref_label: str, ref_date=None) -> str:
    local = t.astimezone(tz)
    parts = [f"- {label}" + (f"（{iana}）" if iana != label else "") + f"：{_fmt(local)}"
             + (_when((local.date() - ref_date).days) if ref_date else ""),
             _offset(local)]
    if d := _dst(tz, local):
        parts.append(d)
    if label != ref_label:
        parts.append(f"比{ref_label}{_gap(local.utcoffset(), ref.utcoffset())}")
    out = "，".join(parts)
    if note := _next_change(tz, local):
        out += f"。{note}"
    return out


def calc(args: dict, home: str = "Asia/Shanghai", now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    home_label, home_tz, home_iana = resolve(home)[0]
    want = [p for p in re.split(r"[,，、;；\n]|和", str(args.get("places") or "")) if p.strip()]
    if len(want) > MAX_PLACES:
        raise ValueError(f"一次最多看 {MAX_PLACES} 个地方")
    targets = [x for p in want for x in resolve(p)]
    if args.get("time"):
        src_label, src_tz, src_iana = resolve(args["from"])[0] if args.get("from") else (home_label, home_tz, home_iana)
        wall = parse_time(args["time"], now.astimezone(src_tz))
        t = wall.replace(tzinfo=src_tz)
        notes = []
        if isinstance(src_tz, ZoneInfo):
            back = t.astimezone(timezone.utc).astimezone(src_tz).replace(tzinfo=None)
            if back != wall:
                notes.append(f"{src_label}当地没有 {wall:%H:%M} 这个时间（夏令时拨快跳过了那一小时），按 {back:%H:%M} 算")
                t = back.replace(tzinfo=src_tz)
            elif t.replace(fold=1).utcoffset() != t.utcoffset():
                notes.append(f"{src_label}当地 {wall:%H:%M} 有两次（夏令时结束时钟拨回），按第一次（还是夏令时）算")
        head = f"{src_label}{'' if src_label.endswith('时间') or src_label.startswith('UTC') else '时间'} {_fmt(t)}（{_offset(t)}" + (f"，{_dst(src_tz, t)}" if _dst(src_tz, t) else "") + "）是："
        lines = [head] + [_line(lb, tz, ia, t, t, src_label, t.date()) for lb, tz, ia in targets
                          if not (ia == src_iana and lb == src_label)]
        if not targets:
            lines.append(f"- UTC：{_fmt(t.astimezone(timezone.utc))}")
        return "\n".join(lines + notes)
    ref = now.astimezone(home_tz)
    lines = [f"现在是 UTC {now.astimezone(timezone.utc):%Y-%m-%d %H:%M}："]
    for lb, tz, ia in ([(home_label, home_tz, home_iana)] if not targets else []) + targets:
        lines.append(_line(lb, tz, ia, now, ref, home_label))
    return "\n".join(lines)


def apply(ctx):
    home = str(ctx.config.get("home") or "Asia/Shanghai")
    resolve(home)   # 设置写错了，启用时就报出来

    async def run(call):
        try:
            return calc(call.args, home)
        except (ValueError, TypeError, OverflowError) as e:
            return f"没算：{e}"
    ctx.tool(Tool("world_clock", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="时区换算：某地现在几点、某个时间换成别处几点、时差、夏令时",
                  progress=lambda a: f"看时间：{a.get('time') or '现在'} {a.get('places') or ''}"[:60]))
