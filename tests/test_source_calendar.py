"""日程快开始了：读 ICS（折行、转义、各种时区）、重复日程展开（RRULE、EXDATE、改期、取消）、什么时候提醒、
ICS 隔一段时间才重新下载。不联网，时间固定。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from osa import events, vault
from osa.sources import _util
from osa.sources import calendar as cal

SH = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")


def at(*a, tz=SH) -> float:
    return datetime(*a, tzinfo=tz).timestamp()


def ics(*vevents: str, head: str = "") -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{v.strip()}\r\nEND:VEVENT\r\n" for v in vevents)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{head}{body}END:VCALENDAR\r\n"


MEETING = """UID:weekly-1
DTSTART;TZID=Asia/Shanghai:20261110T100000
DTEND;TZID=Asia/Shanghai:20261110T110000
SUMMARY:产品周会\\, 第 12 次
LOCATION:3 楼
 大会议室
DESCRIPTION:第一行\\n第二行
ATTENDEE;CN=张三;ROLE=REQ-PARTICIPANT:mailto:zhang@example.com
ATTENDEE:mailto:li@example.com
ORGANIZER;CN="王芳":mailto:wangfang@example.com
URL:https://meet.example.com/abc
BEGIN:VALARM
TRIGGER:-PT10M
DESCRIPTION:这是提醒，不是说明
END:VALARM"""


@pytest.fixture(autouse=True)
def local_tz(monkeypatch):
    monkeypatch.setattr(cal, "LOCAL", SH)


@pytest.fixture
def net(monkeypatch):
    box = {"ics": ics(MEETING), "status": 200, "seen": []}

    def handler(req: httpx.Request) -> httpx.Response:
        box["seen"].append(str(req.url))
        return httpx.Response(box["status"], content=box["ics"].encode())

    async def open_(url):
        return None
    monkeypatch.setattr(_util, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(_util, "blocked", open_)
    return box


def clock(monkeypatch, ts: float):
    monkeypatch.setattr(cal, "_now", lambda: ts)


def items(text: str, now: float) -> list[dict]:
    return cal.occurrences(cal.parse(text), now)


# ---------- 读 ----------
def test_parse_unfold_escape_people():
    [it] = items(ics(MEETING), at(2026, 11, 10, 9, 0))
    assert it["t"] == "产品周会, 第 12 次" and it["l"] == "3 楼大会议室" and it["x"] == "第一行\n第二行"
    assert it["p"] == "张三 <zhang@example.com>、li@example.com" and it["o"] == "王芳 <wangfang@example.com>"
    assert it["u"] == "https://meet.example.com/abc"
    assert it["s"] == at(2026, 11, 10, 10, 0) and it["e"] == at(2026, 11, 10, 11, 0) and not it["d"]


@pytest.mark.parametrize("dtstart,head", [
    ("DTSTART:20261110T020000Z", ""),
    ("DTSTART;TZID=America/New_York:20261109T210000", ""),
    ('DTSTART;TZID="China Standard Time":20261110T100000', ""),
    ('DTSTART;TZID="(UTC+08:00) Beijing, Chongqing, Hong Kong, Urumqi":20261110T100000', ""),
    ("DTSTART;TZID=/mozilla.org/20050126_1/Asia/Tokyo:20261110T110000", ""),
    ("DTSTART;TZID=Custom Zone:20261110T100000",
     "BEGIN:VTIMEZONE\r\nTZID:Custom Zone\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nTZOFFSETFROM:+0800\r\n"
     "TZOFFSETTO:+0800\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"),
    ("DTSTART:20261110T110000", "X-WR-TIMEZONE:Asia/Tokyo\r\n"),     # 浮动时间：按日历的时区
    ("DTSTART:20261110T100000", ""),                                  # 浮动时间：按这台机器的时区
])
def test_timezones(dtstart, head):
    [it] = items(ics(f"UID:x\r\n{dtstart}\r\nSUMMARY:同一时刻", head=head), at(2026, 11, 10, 9, 0))
    assert it["s"] == at(2026, 11, 10, 10, 0)


def test_all_day_and_duration():
    text = ics("UID:d\r\nDTSTART;VALUE=DATE:20261110\r\nDTEND;VALUE=DATE:20261112\r\nSUMMARY:出差",
               "UID:p\r\nDTSTART:20261110T020000Z\r\nDURATION:PT1H30M\r\nSUMMARY:评审")
    d, p = items(text, at(2026, 11, 10, 8, 0))
    assert d["d"] and d["s"] == at(2026, 11, 10, 0, 0) and d["e"] == at(2026, 11, 12, 0, 0)
    assert p["e"] - p["s"] == 5400


# ---------- 重复 ----------
def _dates(start, rule: str, lo, hi, tz=SH, until=None):
    r = dict(x.split("=", 1) for x in rule.split(";"))
    return [c for c, _ in cal.expand(start, r, at(*lo, tz=tz), at(*hi, tz=tz), lambda x: cal._epoch(x, tz), until)]


def test_rrule_basics():
    d = datetime
    assert _dates(d(2026, 11, 1, 9), "FREQ=DAILY;INTERVAL=2;COUNT=5", (2026, 10, 1), (2027, 1, 1)) == \
        [d(2026, 11, n, 9) for n in (1, 3, 5, 7, 9)]
    assert _dates(d(2026, 11, 2, 10), "FREQ=WEEKLY;BYDAY=MO,WE", (2026, 10, 1), (2027, 1, 1),
                  until=at(2026, 11, 18, 23, 59)) == [d(2026, 11, n, 10) for n in (2, 4, 9, 11, 16, 18)]
    assert _dates(d(2026, 11, 3, 10), "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", (2026, 10, 1), (2026, 12, 2)) == \
        [d(2026, 11, 3, 10), d(2026, 11, 17, 10), d(2026, 12, 1, 10)]
    assert _dates(d(2026, 11, 10, 15), "FREQ=MONTHLY;BYDAY=2TU", (2026, 10, 1), (2027, 1, 31)) == \
        [d(2026, 11, 10, 15), d(2026, 12, 8, 15), d(2027, 1, 12, 15)]
    assert _dates(d(2026, 11, 27, 15), "FREQ=MONTHLY;BYDAY=-1FR", (2026, 10, 1), (2026, 12, 31)) == \
        [d(2026, 11, 27, 15), d(2026, 12, 25, 15)]
    assert _dates(d(2026, 11, 30, 18), "FREQ=MONTHLY;BYMONTHDAY=-1", (2026, 10, 1), (2027, 3, 1)) == \
        [d(2026, 11, 30, 18), d(2026, 12, 31, 18), d(2027, 1, 31, 18), d(2027, 2, 28, 18)]
    assert _dates(d(2027, 1, 31, 8), "FREQ=MONTHLY", (2027, 1, 1), (2027, 6, 1)) == \
        [d(2027, 1, 31, 8), d(2027, 3, 31, 8), d(2027, 5, 31, 8)]             # 没有 31 号的月份跳过
    assert _dates(date(2024, 2, 29), "FREQ=YEARLY", (2024, 1, 1), (2029, 1, 1)) == [date(2024, 2, 29), date(2028, 2, 29)]


def test_rrule_started_long_ago():
    got = _dates(datetime(2016, 1, 1, 10), "FREQ=DAILY", (2026, 11, 10), (2026, 11, 12))
    assert got == [datetime(2026, 11, 10, 10), datetime(2026, 11, 11, 10)]
    got = _dates(datetime(2016, 1, 4, 10), "FREQ=WEEKLY;BYDAY=MO,TH", (2026, 11, 10), (2026, 11, 17))
    assert got == [datetime(2026, 11, 12, 10), datetime(2026, 11, 16, 10)]


def test_rrule_keeps_local_time_across_dst():
    text = ics("UID:ny\r\nDTSTART;TZID=America/New_York:20261026T100000\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:standup")
    got = items(text, at(2026, 10, 30, 0, 0, tz=NY))
    assert got == []      # 两天的窗口里没有
    got = items(text, at(2026, 11, 1, 12, 0, tz=NY))
    assert [datetime.fromtimestamp(i["s"], NY) for i in got] == [datetime(2026, 11, 2, 10, tzinfo=NY)]


def test_exdate_moved_and_cancelled():
    master = ("UID:m\r\nDTSTART;TZID=Asia/Shanghai:20261109T100000\r\nDTEND;TZID=Asia/Shanghai:20261109T103000\r\n"
              "RRULE:FREQ=DAILY;COUNT=10\r\nEXDATE;TZID=Asia/Shanghai:20261110T100000,20261111T100000\r\nSUMMARY:站会")
    moved = "UID:m\r\nRECURRENCE-ID;TZID=Asia/Shanghai:20261112T100000\r\nDTSTART;TZID=Asia/Shanghai:20261112T140000\r\nSUMMARY:站会（改到下午）"
    cancelled = "UID:m\r\nRECURRENCE-ID:20261113T020000Z\r\nDTSTART:20261113T020000Z\r\nSTATUS:CANCELLED\r\nSUMMARY:站会"
    gone = "UID:g\r\nDTSTART:20261112T030000Z\r\nSTATUS:CANCELLED\r\nSUMMARY:取消了的会"
    got = items(ics(master, moved, cancelled, gone), at(2026, 11, 11, 12, 0))
    assert [(datetime.fromtimestamp(i["s"], SH), i["t"]) for i in got] == [
        (datetime(2026, 11, 12, 14, tzinfo=SH), "站会（改到下午）")]
    got = items(ics(master, moved, cancelled, gone), at(2026, 11, 13, 12, 0))   # 13 号取消了，14 号照常
    assert [datetime.fromtimestamp(i["s"], SH) for i in got] == [datetime(2026, 11, 14, 10, tzinfo=SH),
                                                               datetime(2026, 11, 15, 10, tzinfo=SH)]


# ---------- 什么时候提醒 ----------
P = {"url": "https://cal.example.com/me.ics", "minutes_before": 15, "all_day": False}


async def test_reminds_once_before_start(net, monkeypatch):
    clock(monkeypatch, at(2026, 11, 10, 9, 40))
    evs, st = await cal.check(P, None)
    assert evs == [] and len(st["items"]) == 1 and net["seen"] == ["https://cal.example.com/me.ics"]
    clock(monkeypatch, at(2026, 11, 10, 9, 46))
    evs, st = await cal.check(P, st)
    assert len(evs) == 1
    e = evs[0]
    assert e["title"] == "「产品周会, 第 12 次」14 分钟后开始（10:00）" and e["url"] == "https://meet.example.com/abc"
    assert "时间：2026-11-10 周二 10:00–11:00" in e["text"] and "地点：3 楼大会议室" in e["text"]
    assert "参加的人：张三 <zhang@example.com>" in e["text"] and "说明：\n第一行\n第二行" in e["text"]
    clock(monkeypatch, at(2026, 11, 10, 9, 51))
    assert (await cal.check(P, st))[0] == []
    assert len(net["seen"]) == 1                       # 30 分钟内不重新下载
    clock(monkeypatch, at(2026, 11, 10, 10, 20))
    evs, st = await cal.check(P, st)
    assert evs == [] and len(net["seen"]) == 2


async def test_never_after_start(net, monkeypatch):
    clock(monkeypatch, at(2026, 11, 10, 10, 1))
    evs, st = await cal.check(P, None)
    assert evs == []


async def test_first_check_inside_window_fires(net, monkeypatch):
    assert cal.CALENDAR.fire_first
    clock(monkeypatch, at(2026, 11, 10, 9, 50))
    evs, _ = await cal.check(P, None)
    assert [e["title"] for e in evs] == ["「产品周会, 第 12 次」10 分钟后开始（10:00）"]


async def test_long_interval_reminds_early_instead_of_missing(net, monkeypatch):
    """每小时才看一次、提前 15 分钟：下次看的时候已经开始了，这次就提醒。"""
    clock(monkeypatch, at(2026, 11, 10, 8, 50))
    _, st = await cal.check(P, None)
    clock(monkeypatch, at(2026, 11, 10, 9, 20))
    st["at"] = at(2026, 11, 10, 8, 20)
    evs, _ = await cal.check(P, st)
    assert [e["title"] for e in evs] == ["「产品周会, 第 12 次」40 分钟后开始（10:00）"]


async def test_one_manual_check_does_not_shrink_the_interval(net, monkeypatch):
    """每半小时看一次，9:29 手动点了一次：10:00 的会照样在开始前提醒一次，不会因为间隔估短了漏掉。"""
    st, got = None, []
    for hm in ((8, 30), (9, 0), (9, 29), (9, 30), (10, 0)):
        clock(monkeypatch, at(2026, 11, 10, *hm))
        evs, st = await cal.check(P, st)
        got += [(hm, e["title"]) for e in evs]
    assert len(got) == 1 and got[0][0] < (10, 0)
    # 只是每半小时看一次（没有手动）：9:30 那次就提醒
    st, got = None, []
    for hm in ((8, 30), (9, 0), (9, 30), (10, 0)):
        clock(monkeypatch, at(2026, 11, 10, *hm))
        evs, st = await cal.check(P, st)
        got += [(hm, e["title"]) for e in evs]
    assert got == [((9, 30), "「产品周会, 第 12 次」30 分钟后开始（10:00）")]


async def test_minutes_before_hour(net, monkeypatch):
    clock(monkeypatch, at(2026, 11, 10, 8, 55))
    evs, _ = await cal.check(P | {"minutes_before": 90}, None)
    assert evs[0]["title"].startswith("「产品周会, 第 12 次」1 小时 5 分钟后开始")


async def test_all_day(net, monkeypatch):
    net["ics"] = ics("UID:h\r\nDTSTART;VALUE=DATE:20261110\r\nSUMMARY:小王生日")
    clock(monkeypatch, at(2026, 11, 10, 9, 30))
    assert (await cal.check(P, None))[0] == []
    clock(monkeypatch, at(2026, 11, 10, 8, 30))
    evs, st = await cal.check(P | {"all_day": True}, None)
    assert evs == []
    clock(monkeypatch, at(2026, 11, 10, 9, 5))
    evs, st = await cal.check(P | {"all_day": True}, st)
    assert evs[0]["title"] == "今天全天：「小王生日」" and "2026-11-10 周二 全天" in evs[0]["text"]
    clock(monkeypatch, at(2026, 11, 10, 12, 0))
    assert (await cal.check(P | {"all_day": True}, st))[0] == []


async def test_webcal_and_url_change(net, monkeypatch):
    clock(monkeypatch, at(2026, 11, 10, 9, 0))
    _, st = await cal.check(P | {"url": "webcal://p01-calendars.icloud.com/published/2/abc"}, None)
    assert net["seen"] == ["https://p01-calendars.icloud.com/published/2/abc"]
    await cal.check(P, st)                              # 换了地址：马上重新下载
    assert net["seen"][-1] == "https://cal.example.com/me.ics"


async def test_not_a_calendar(net, monkeypatch):
    net["ics"] = "<html><body>请先登录</body></html>"
    clock(monkeypatch, at(2026, 11, 10, 9, 0))
    with pytest.raises(ValueError, match="不是日历"):
        await cal.check(P, None)


async def test_download_failure_uses_recent_copy(net, monkeypatch):
    clock(monkeypatch, at(2026, 11, 10, 8, 0))
    _, st = await cal.check(P, None)
    net["status"] = 500
    clock(monkeypatch, at(2026, 11, 10, 9, 50))       # 该重新下载了，但下载失败：用 2 小时前的
    evs, st = await cal.check(P, st)
    assert len(evs) == 1 and len(net["seen"]) == 2
    clock(monkeypatch, at(2026, 11, 10, 11, 30))      # 太旧了：报错
    with pytest.raises(httpx.HTTPStatusError):
        await cal.check(P, st)


def test_validate_stores_url_in_vault():
    p = events.clean_params(cal.CALENDAR, {"url": "webcal://example.com/a.ics"})
    assert p["url"].startswith("vault:event_") and vault.get(p["url"][6:]) == "webcal://example.com/a.ics"
    assert p["minutes_before"] == 15 and p["all_day"] is False
    assert events.resolve(cal.CALENDAR, p)["url"] == "webcal://example.com/a.ics"
    assert "url" not in events.portable({"kind": "event", "source": "calendar", "params": p})["params"]
    with pytest.raises(ValueError, match="webcal"):
        events.clean_params(cal.CALENDAR, {"url": "ftp://example.com/a.ics"})
    with pytest.raises(ValueError, match="要填"):
        events.clean_params(cal.CALENDAR, {})
    for sc in cal.CALENDAR.scenarios:
        assert set(sc["params"]) <= {f["key"] for f in cal.CALENDAR.fields}
