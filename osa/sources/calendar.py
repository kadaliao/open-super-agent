"""日程快开始了：订阅 Google / iCloud / Outlook / 飞书 / 钉钉 / 企业微信日历的 ICS 地址，开始前几分钟提醒一次。

每 5 分钟看一次，但 ICS 最多 FETCH_EVERY 秒才重新下载：下载下来马上展开成接下来两天的日程（重复的按 RRULE 展开，
去掉 EXDATE、取消了的、单独改过的那一次），只把这些存进 state，日历再大 state 也不大。
时间：结尾带 Z 的是 UTC；有 TZID 的按那个时区（Outlook 的 Windows 时区名，比如 “China Standard Time” 也认）；
都没有的（浮动时间）按日历的 X-WR-TIMEZONE，再没有就按这台机器的时区（和定时任务一样）。
没开始的才提醒，已经开始的不补；检查间隔比“提前几分钟”还长、下次检查时已经开始了的，宁可早点提醒也不漏。
订阅地址里带着密钥（Google 叫它“私密地址”），所以按 secret 存进保险库。
"""
import calendar as _cal
import hashlib
import re
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo
from .. import i18n, vault
from ..i18n import N_
from ..registry import POLL, Source
from . import _util
from ._util import oneline, short

FETCH_EVERY = 30 * 60      # 多久重新下载一次 ICS
STALE = 3 * 3600           # 下载失败时，多久以内下载的旧日程还能接着用
HORIZON = 2 * 86400        # 展开多远以内的日程（“提前几分钟”最多一天）
MAX_ITEMS = 100          # state 里最多存多少次日程（两天以内的，一般远远不到）
MAX_FIRED = 300
ALL_DAY_AT = 9             # 全天日程当天几点提醒
LOCAL: tzinfo | None = None   # 默认时区；None 是这台机器的时区。测试里换掉
WEEK = "一二三四五六日"
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# Outlook / Exchange 导出的 TZID 常是 Windows 的时区名
WINDOWS = {
    "China Standard Time": "Asia/Shanghai", "Taipei Standard Time": "Asia/Taipei", "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul", "Singapore Standard Time": "Asia/Singapore", "India Standard Time": "Asia/Kolkata",
    "SE Asia Standard Time": "Asia/Bangkok", "Arabian Standard Time": "Asia/Dubai", "Russian Standard Time": "Europe/Moscow",
    "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev", "GTB Standard Time": "Europe/Bucharest", "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago", "Mountain Standard Time": "America/Denver", "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles", "Alaskan Standard Time": "America/Anchorage", "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax", "E. South America Standard Time": "America/Sao_Paulo",
    "AUS Eastern Standard Time": "Australia/Sydney", "E. Australia Standard Time": "Australia/Brisbane",
    "W. Australia Standard Time": "Australia/Perth", "New Zealand Standard Time": "Pacific/Auckland", "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
}


def _now() -> float:
    return time.time()


# ---------- 读 ICS ----------
def _unfold(text: str) -> list[str]:
    """ICS 的长行会折成几行，后面的行以空格或 Tab 开头：接回去。"""
    out: list[str] = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if ln[:1] in (" ", "\t") and out:
            out[-1] += ln[1:]
        elif ln.strip():
            out.append(ln)
    return out


def _split(ln: str) -> tuple[str, dict, str]:
    """一行 → (名字, 参数, 值)。参数值可能带引号，引号里可能有冒号、分号（TZID="(UTC+08:00) Beijing"）。"""
    quoted, start, parts = False, 0, []
    for i, ch in enumerate(ln):
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == ";":
            parts.append(ln[start:i])
            start = i + 1
        elif not quoted and ch == ":":
            parts.append(ln[start:i])
            params = {}
            for p in parts[1:]:
                k, _, v = p.partition("=")
                params[k.strip().upper()] = v.strip().strip('"')
            return parts[0].strip().upper(), params, ln[i + 1:]
    return "", {}, ""


def _text(v: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], v or "").strip()


def _offset(v: str) -> int:
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", (v or "").strip())
    if not m:
        return 0
    s = int(m[2]) * 3600 + int(m[3]) * 60 + int(m[4] or 0)
    return -s if m[1] == "-" else s


def parse(text: str) -> dict:
    """ICS → {"tz": X-WR-TIMEZONE, "vtz": {TZID: 标准时间偏移的秒数}, "events": [{属性名: [(参数, 值), ...]}]}。"""
    cal: dict = {"tz": "", "name": "", "vtz": {}, "events": []}
    stack: list[str] = []
    ev, tzid = None, ""
    for ln in _unfold(text):
        name, params, value = _split(ln)
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                ev = {}
        elif name == "END":
            top = stack.pop() if stack else ""
            if top == "VEVENT" and ev is not None:
                cal["events"].append(ev)
                ev = None
            elif top == "VTIMEZONE":
                tzid = ""
        elif stack and stack[-1] == "VEVENT" and ev is not None:
            ev.setdefault(name, []).append((params, value))
        elif stack and stack[-1] == "VCALENDAR" and name in ("X-WR-TIMEZONE", "X-WR-CALNAME"):
            cal["tz" if name == "X-WR-TIMEZONE" else "name"] = value.strip()
        elif stack and stack[-1] == "VTIMEZONE" and name == "TZID":
            tzid = value.strip()
        elif stack and stack[-1] == "STANDARD" and name == "TZOFFSETTO" and tzid:
            cal["vtz"].setdefault(tzid, _offset(value))
    return cal


# ---------- 时间 ----------
def zone(tzid: str, vtz: dict | None = None) -> tzinfo | None:
    """TZID → 时区。IANA 名、Windows 名、带 IANA 名的怪写法（/mozilla.org/…/Asia/Shanghai）、“(UTC+08:00) …”，
    最后用日历里 VTIMEZONE 的标准时间偏移（不管夏令时）。都不认识返回 None。"""
    tzid = (tzid or "").strip().strip('"')
    if not tzid:
        return None
    for name in (tzid, WINDOWS.get(tzid), *re.findall(r"[A-Za-z]+/[A-Za-z_\-]+(?:/[A-Za-z_\-]+)?", tzid)):
        if name:
            try:
                return ZoneInfo(name)
            except Exception:
                pass
    if m := re.search(r"(?:UTC|GMT)\s*([+-])(\d{1,2})(?::?(\d{2}))?", tzid):
        s = int(m[2]) * 3600 + int(m[3] or 0) * 60
        return timezone(timedelta(seconds=-s if m[1] == "-" else s))
    if vtz and tzid in vtz:
        return timezone(timedelta(seconds=vtz[tzid]))
    return None


def _when(value: str, params: dict, vtz: dict, default: tzinfo | None):
    """DTSTART 这类的值 → (墙上时间, 时区)。全天的是 date；UTC 的时区是 timezone.utc；浮动时间用 default（None 是本机）。"""
    v = (value or "").strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        return date(int(v[:4]), int(v[4:6]), int(v[6:8])), default
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)", v)
    if not m:
        raise ValueError(f"看不懂的时间：{v}")
    dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0))
    if m[7]:
        return dt, timezone.utc
    if params.get("TZID") and (z := zone(params["TZID"], vtz)) is not None:
        return dt, z
    return dt, default


def _epoch(x, tz: tzinfo | None) -> float:
    if not isinstance(x, datetime):
        x = datetime(x.year, x.month, x.day)
    return x.replace(tzinfo=tz).timestamp() if tz is not None else x.timestamp()


def _local(ts: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)


def _duration(v: str) -> float | None:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", (v or "").strip())
    if not m:
        return None
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return (-1 if m[1] == "-" else 1) * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s).total_seconds()


# ---------- 重复 ----------
def _byday(s: str) -> list[tuple[int | None, int]]:
    out = []
    for x in (s or "").split(","):
        if m := re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", x.strip().upper()):
            out.append((int(m[1]) if m[1] else None, DAYS[m[2]]))
    return out


def _period(start, freq: str, k: int, byday: list, bymonthday: list) -> list:
    """第 k 个周期里的候选时间（按先后）。"""
    if freq == "DAILY":
        return [start + timedelta(days=k)]
    if freq == "WEEKLY":
        week = start - timedelta(days=start.weekday()) + timedelta(weeks=k)
        return [week + timedelta(days=wd) for wd in sorted({wd for _, wd in byday} or {start.weekday()})]
    if freq == "MONTHLY":
        m = start.month - 1 + k
        y, mo = start.year + m // 12, m % 12 + 1
        last = _cal.monthrange(y, mo)[1]
        if byday:
            days = set()
            for n, wd in byday:
                same = [d for d in range(1, last + 1) if date(y, mo, d).weekday() == wd]
                if n is None:
                    days.update(same)
                elif 0 < abs(n) <= len(same):
                    days.add(same[n - 1] if n > 0 else same[n])
        elif bymonthday:
            days = {d if d > 0 else last + 1 + d for d in bymonthday}
        else:
            days = {start.day}
        return [start.replace(year=y, month=mo, day=d) for d in sorted(days) if 1 <= d <= last]
    if freq == "YEARLY":
        try:
            return [start.replace(year=start.year + k)]
        except ValueError:      # 2 月 29 日，平年没有
            return []
    return []


def expand(start, rule: dict, lo: float, hi: float, to_epoch, until: float | None = None) -> list[tuple]:
    """重复日程里开始时间在 [lo, hi] 的那几次 → [(墙上时间, epoch)]。支持 FREQ、INTERVAL、COUNT、UNTIL、BYDAY、BYMONTHDAY。"""
    freq = (rule.get("FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        ep = to_epoch(start)
        return [(start, ep)] if lo <= ep <= hi else []
    try:
        step = max(1, int(rule.get("INTERVAL") or 1))
    except ValueError:
        step = 1
    count = int(rule["COUNT"]) if (rule.get("COUNT") or "").isdigit() else None
    byday = _byday(rule.get("BYDAY", ""))
    bymd = [int(x) for x in (rule.get("BYMONTHDAY") or "").split(",") if re.fullmatch(r"-?\d{1,2}", x.strip())]
    k0, s0 = 0, to_epoch(start)
    if count is None and lo > s0:      # 很久以前开始的：直接跳到窗口前面一点，不从头数
        per = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 31, "YEARLY": 366}[freq] * step
        k0 = max(0, int((lo - s0) / 86400 // per) - 1)
    out, n = [], 0
    for k in range(k0, k0 + 5000):
        stop = False
        for c in _period(start, freq, k * step, byday, bymd):
            if c < start:
                continue
            ep = to_epoch(c)
            n += 1
            if (until is not None and ep > until) or (count is not None and n > count) or ep > hi:
                stop = True
                break
            if ep >= lo:
                out.append((c, ep))
        if stop:
            break
    return out


# ---------- 展开成接下来的日程 ----------
def _first(ev: dict, key: str) -> str:
    return ev[key][0][1] if ev.get(key) else ""


def _person(params: dict, value: str) -> str:
    mail = re.sub(r"(?i)^mailto:", "", value or "").strip()
    cn = (params.get("CN") or "").strip()
    return f"{cn} <{mail}>" if cn and mail and cn != mail else (cn or mail)


def occurrences(cal: dict, now: float) -> list[dict]:
    """从 now 起 HORIZON 以内（没结束的）日程，每次一条：k 去重用、s / e 开始结束、d 是不是全天，还有标题、地点、说明……"""
    default = zone(cal.get("tz") or "") or LOCAL
    vtz = cal.get("vtz") or {}
    moved: dict[str, set] = {}      # 单独改过、取消了的那一次（RECURRENCE-ID）：主日程展开时跳过
    for ev in cal["events"]:
        if ev.get("RECURRENCE-ID"):
            p, v = ev["RECURRENCE-ID"][0]
            try:
                x, z = _when(v, p, vtz, default)
                moved.setdefault(_first(ev, "UID").strip(), set()).add(int(_epoch(x, z)))
            except ValueError:
                pass
    items = []
    for ev in cal["events"]:
        if _first(ev, "STATUS").strip().upper() == "CANCELLED" or not ev.get("DTSTART"):
            continue
        p, v = ev["DTSTART"][0]
        try:
            start, tz = _when(v, p, vtz, default)
        except ValueError:
            continue
        all_day = not isinstance(start, datetime)
        to_epoch = lambda x, tz=tz: _epoch(x, tz)   # noqa: E731
        s0 = to_epoch(start)
        dur = None
        if ev.get("DTEND"):
            try:
                end, etz = _when(ev["DTEND"][0][1], ev["DTEND"][0][0], vtz, default)
                dur = _epoch(end, etz) - s0
            except ValueError:
                pass
        if dur is None and ev.get("DURATION"):
            dur = _duration(_first(ev, "DURATION"))
        dur = max(0.0, dur if dur is not None else 86400.0 if all_day else 0.0)
        uid = _first(ev, "UID").strip() or f"{_first(ev, 'SUMMARY')}|{v}"
        lo, hi = now - dur, now + HORIZON
        if ev.get("RRULE") and not ev.get("RECURRENCE-ID"):
            rule = {}
            for part in ev["RRULE"][0][1].split(";"):
                k, _, val = part.partition("=")
                rule[k.strip().upper()] = val.strip()
            until = None
            if rule.get("UNTIL"):
                try:
                    u, utz = _when(rule["UNTIL"], {}, vtz, tz)
                    until = _epoch(u + timedelta(days=1), utz) - 1 if not isinstance(u, datetime) else _epoch(u, utz)
                except ValueError:
                    pass
            skip_ep, skip_day = set(moved.get(uid, ())), set()
            for xp, xv in ev.get("EXDATE") or []:
                for x in xv.split(","):
                    try:
                        xd, xz = _when(x, xp, vtz, default)
                    except ValueError:
                        continue
                    if isinstance(xd, datetime):
                        skip_ep.add(int(_epoch(xd, xz)))
                    else:
                        skip_day.add(xd)
            occ = [(c, e) for c, e in expand(start, rule, lo, hi, to_epoch, until)
                   if int(e) not in skip_ep and (c.date() if isinstance(c, datetime) else c) not in skip_day]
        else:
            occ = [(start, s0)] if lo <= s0 <= hi else []
        if not occ:
            continue
        people = [_person(*a) for a in (ev.get("ATTENDEE") or [])[:15]]
        org = _person(*ev["ORGANIZER"][0]) if ev.get("ORGANIZER") else ""
        base = {"d": all_day, "t": oneline(_text(_first(ev, "SUMMARY")), 200) or "（没有标题的日程）",
                "l": oneline(_text(_first(ev, "LOCATION")), 300), "x": short(_text(_first(ev, "DESCRIPTION")), 600),
                "u": _first(ev, "URL").strip() or _first(ev, "X-GOOGLE-CONFERENCE").strip(),
                "p": "、".join(x for x in people if x), "o": org}
        for _, e in occ:
            items.append({"k": f"{uid}|{int(e)}", "s": e, "e": e + dur} | base)
    items.sort(key=lambda i: i["s"])
    return items[:MAX_ITEMS]


# ---------- 该提醒的 ----------
def _ago(sec: float) -> str:
    m = max(0, round(sec / 60))
    if m < 60:
        return f"{m} 分钟后" if m else "马上"
    h, m = divmod(m, 60)
    return f"{h} 小时{f' {m} 分钟' if m else ''}后"


def _event(it: dict, now: float, tz: tzinfo | None) -> dict:
    a, b = _local(it["s"], tz), _local(it["e"], tz)
    day = f"{a:%Y-%m-%d} 周{WEEK[a.weekday()]}"
    if it["d"]:
        last = _local(it["e"] - 1, tz)
        when = day + " 全天" + (f"，到 {last:%m-%d} 周{WEEK[last.weekday()]}" if last.date() != a.date() else "")
        title = f"今天全天：「{it['t']}」"
    else:
        span = f"{a:%H:%M}–{b:%H:%M}" if b.date() == a.date() else f"{a:%H:%M} 到 {b:%Y-%m-%d %H:%M}"
        when = f"{day} {span}" if it["e"] > it["s"] else f"{day} {a:%H:%M}"
        title = f"「{it['t']}」{_ago(it['s'] - now)}开始（{a:%H:%M}）"
    lines = [f"时间：{when}", f"地点：{it['l']}" if it.get("l") else "", f"组织者：{it['o']}" if it.get("o") else "",
             f"参加的人：{it['p']}" if it.get("p") else "", f"说明：\n{it['x']}" if it.get("x") else ""]
    return {"id": it["k"], "title": title, "url": it.get("u") or "", "text": "\n".join(x for x in lines if x)}


def due(params: dict, st: dict, now: float) -> tuple[list, dict]:
    mb = params.get("minutes_before")
    mb = (15 if mb is None else mb) * 60
    # 估计的检查间隔：最近几次里第二长的（手动点一次“立即检查”、服务停过一阵都带不偏），再多算 1/4，宁早不漏
    if st.get("at"):
        st["gaps"] = (st.get("gaps") or [])[-4:] + [round(now - st["at"])]
    gaps = sorted(st.get("gaps") or [], reverse=True)
    gap = min(max(gaps[min(1, len(gaps) - 1)] * 1.25, 60), 3600) if gaps else 300
    tz = zone(st.get("tz") or "") or LOCAL
    fired = {k for k, _ in st.get("fired") or []}
    out, new = [], []
    for it in st.get("items") or []:
        if it["k"] in fired:
            continue
        if it["d"]:
            at = it["s"] + ALL_DAY_AT * 3600
            ok = bool(params.get("all_day")) and at <= now < it["s"] + 86400
        else:
            # 正常是 [开始前 mb, 开始)；下次检查时已经开始了的，这次就提醒（早一点也不漏）
            ok = now < it["s"] and (now >= it["s"] - mb or now + gap >= it["s"])
        if ok:
            out.append(_event(it, now, tz))
            new.append([it["k"], it["s"]])
    st["fired"] = ([x for x in st.get("fired") or [] if x[1] >= now - 2 * 86400] + new)[-MAX_FIRED:]
    st["items"] = [i for i in st.get("items") or [] if i["e"] >= now or (i["d"] and i["s"] + 86400 > now)]
    st["at"] = now
    return out, st


def norm_url(u: str) -> str:
    u = (u or "").strip()
    if re.match(r"(?i)webcals?://", u):
        u = "https://" + u.split("://", 1)[1]
    if not re.match(r"(?i)https?://[^\s/]+", u):
        raise ValueError("日历地址要以 https://、http:// 或 webcal:// 开头")
    return u


async def check(params: dict, state):
    st = dict(state or {})
    now = _now()
    url = norm_url(params.get("url") or "")
    src = hashlib.sha1(url.encode()).hexdigest()[:12]
    if st.get("src") != src or now - (st.get("fetched") or 0) >= FETCH_EVERY:
        try:
            r = await _util.get(url, timeout=30, max_bytes=20_000_000, headers={"Accept": "text/calendar, */*"})
            text = r.content.decode("utf-8", errors="replace")
            if "BEGIN:VCALENDAR" not in text[:5000].upper():
                raise ValueError("这个地址返回的不是日历（ICS）：检查一下是不是订阅地址，或者日历有没有设成公开 / 能订阅")
            cal = parse(text)
            st.update(src=src, fetched=now, items=occurrences(cal, now), tz=cal["tz"])
        except Exception:
            if st.get("src") != src or now - (st.get("fetched") or 0) > STALE:
                raise
            # 偶尔下载失败：不久前下载的日程接着用，下次再试
    return due(params, st, now)


def validate(p: dict) -> dict:
    ref = p.get("url") or ""
    raw = vault.get(ref[6:]) if ref.startswith("vault:") else ref
    if raw is None:
        raise ValueError(f"保险库里没有 {ref[6:]}")
    norm_url(raw)
    return p


def summary(p: dict) -> str:
    mb = p.get("minutes_before")
    s = i18n.t("提前 {n} 分钟", n=15 if mb is None else mb)
    return s + (i18n.t("，含全天日程") if p.get("all_day") else "")


CALENDAR = Source(
    id="calendar", mode=POLL, category=N_("生活"), icon="📅",
    title=N_("日程快开始了"),
    description=N_("订阅你的日历（Google、iCloud、Outlook、飞书、钉钉、企业微信都能导出订阅地址），日程开始前几分钟提醒一次。重复的日程、改期、取消都认。"),
    fields=[
        {"key": "url", "label": N_("日历订阅地址（ICS）"), "type": "secret", "required": True,
         "placeholder": N_("https://… 或 webcal://…"),
         "help": N_("Google 日历：设置 → 选中日历 → “iCal 格式的私密地址”；iCloud：日历 App 里把日历共享成“公开日历”，拷贝链接；"
                    "Outlook：设置 → 日历 → 共享日历 → 发布日历，拷贝 ICS 链接；飞书、钉钉、企业微信：在日历设置里找订阅 / 导出的 ICS 地址。"
                    "地址本身就是密钥，会存进保险库")},
        {"key": "minutes_before", "label": N_("提前几分钟提醒"), "type": "number", "default": 15, "min": 0, "max": 1440},
        {"key": "all_day", "label": N_("全天日程也提醒（当天早上 9 点）"), "type": "bool", "default": False},
    ],
    check=check, every=5, min_every=2, fire_first=True, validate=validate, summary=summary,
    sample={"title": "「产品周会」15 分钟后开始（10:00）",
            "text": "时间：2026-11-10 周二 10:00–11:00\n地点：3 楼大会议室\n组织者：王芳 <wangfang@example.com>\n"
                    "参加的人：张三 <zhang@example.com>、李四 <li@example.com>\n说明：\n过一下本周的上线计划和 bug 列表"},
    scenarios=[
        {"title": N_("日程开始前提醒"), "description": N_("开始前 15 分钟提醒你，带上地点和要准备的东西"),
         "name": N_("日程提醒"), "params": {"minutes_before": 15},
         "prompt": N_("日程快开始了。用一两句话提醒我几点开始、在哪（线上会议给出会议链接）；根据标题和说明提醒一句要准备什么。")},
        {"title": N_("会议前准备资料"), "description": N_("开会前半小时，Bot 查查参会的人和议题，整理几条要点"),
         "name": N_("会前准备"), "params": {"minutes_before": 30},
         "llm_filter": N_("只处理和别人一起开的会（有参加的人、会议链接或者看得出是会议）；私人提醒、全天日程忽略"),
         "prompt": N_("马上要开会了。根据标题、说明和参加的人，查一下相关资料（对方是谁、哪家公司、议题的背景），整理成三五条要点让我开会前看一眼；有会议链接的放在最前面。")},
        {"title": N_("出发提醒"), "description": N_("要出门的日程，提前估算路上要多久、几点该走"),
         "name": N_("出发提醒"), "params": {"minutes_before": 60},
         "llm_filter": N_("只处理写了实际地点、要过去的日程；线上会议和没写地点的忽略"),
         "prompt": N_("这个日程要出门。按地点估算从我常在的地方过去要多久（不知道我在哪就问我一次并记住），告诉我最晚几点出发、怎么去比较好。")},
    ])

SOURCES = [CALENDAR]
