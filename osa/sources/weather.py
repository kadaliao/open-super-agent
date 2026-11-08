"""天气提醒：明天下雨、降温、高温、低温、大风、下雪，还有周五报一次周末天气。

数据用 Open-Meteo（免费、不用 key）：先按城市名查经纬度（记在 state 里，城市没改就不再查），再取每天的预报。
每种情况对同一天只报一次（state 里记着报过的“情况:日期”）。按那个城市的当地时间，晚上 10 点到早上 7 点不报，
等到早上再说，免得半夜推送。
"""
import re
import time
from datetime import date
from ..i18n import N_
from ..registry import POLL, Source
from . import _util

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
DAILY = ("weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max",
         "precipitation_sum", "wind_speed_10m_max", "snowfall_sum")
QUIET = (22, 7)        # 当地时间几点到几点不报

# WMO 天气代码 → 中文
WMO = {0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
       56: "冻毛毛雨", 57: "强冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪",
       75: "大雪", 77: "雪粒", 80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪", 95: "雷阵雨",
       96: "雷阵雨伴有冰雹", 99: "强雷阵雨伴有冰雹"}
RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOW = {71, 73, 75, 77, 85, 86}
WEEK = "一二三四五六日"


def _now() -> float:
    return time.time()


# ---------- 数据 ----------
async def geocode(city: str) -> dict:
    """城市名 → {"q", "lat", "lon", "name"}。也能直接写经纬度“30.27,120.15”。"""
    if m := re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*", city):
        return {"q": city, "lat": float(m[1]), "lon": float(m[2]), "name": city.strip()}
    names = [city.strip()]
    if (short := re.sub(r"(市|区|县|省)$", "", names[0])) != names[0] and short:
        names.append(short)    # “杭州市”查不到时再试“杭州”
    for name in names:
        r = await _util.get(GEO, params={"name": name, "count": 1, "language": "zh", "format": "json"},
                            timeout=15, max_bytes=200_000)
        if res := (r.json() or {}).get("results"):
            g = res[0]
            where = "，".join(x for x in (g.get("admin1"), g.get("country")) if x and x != g.get("name"))
            return {"q": city, "lat": g["latitude"], "lon": g["longitude"],
                    "name": g.get("name", name) + (f"（{where}）" if where else "")}
    raise ValueError(f"找不到城市：{city}。换个写法试试，比如“杭州”或“Hangzhou”，也可以直接写经纬度")


async def forecast(lat: float, lon: float) -> dict:
    r = await _util.get(FORECAST, timeout=15, max_bytes=500_000,
                        params={"latitude": lat, "longitude": lon, "daily": ",".join(DAILY),
                                "timezone": "auto", "forecast_days": 4})
    d = r.json() or {}
    if not (d.get("daily") or {}).get("time"):
        raise ValueError(f"Open-Meteo 没返回预报：{d.get('reason') or d}")
    return d


def days_of(data: dict) -> list[dict]:
    """每天一个 dict：date、code、tmax、tmin、prob、rain、wind、snow。"""
    d = data["daily"]
    keys = {"weather_code": "code", "temperature_2m_max": "tmax", "temperature_2m_min": "tmin",
            "precipitation_probability_max": "prob", "precipitation_sum": "rain", "wind_speed_10m_max": "wind",
            "snowfall_sum": "snow"}
    out = []
    for i, day in enumerate(d["time"]):
        out.append({"date": day} | {short: ((d.get(k) or [])[i] if i < len(d.get(k) or []) else None) for k, short in keys.items()})
    return out


# ---------- 判断 ----------
def _f(x) -> str:
    return "—" if x is None else f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


def describe_day(label: str, d: dict) -> str:
    dt = date.fromisoformat(d["date"])
    parts = [WMO.get(d.get("code"), f"天气代码 {d.get('code')}") if d.get("code") is not None else "",
             f"{_f(d.get('tmin'))}~{_f(d.get('tmax'))}°C"]
    if d.get("prob") is not None:
        parts.append(f"降水概率 {_f(d['prob'])}%")
    if d.get("rain"):
        parts.append(f"降水 {_f(d['rain'])} mm")
    if d.get("snow"):
        parts.append(f"降雪 {_f(d['snow'])} cm")
    if d.get("wind") is not None:
        parts.append(f"最大风速 {_f(d['wind'])} km/h")
    return f"{label} {dt.month}月{dt.day}日 周{WEEK[dt.weekday()]}：" + "，".join(p for p in parts if p)


def _rainy(d: dict, prob: float) -> bool:
    if d.get("prob") is not None:
        return d["prob"] >= prob
    return (d.get("rain") or 0) >= 0.5 or d.get("code") in RAIN


def hits(params: dict, days: list[dict], today: str, weekday: int) -> list[tuple[str, str]]:
    """哪些情况成立：[(“情况:日期”, 一句说明)]。"""
    i = next((n for n, d in enumerate(days) if d["date"] == today), None)
    if i is None or i + 1 >= len(days):
        return []
    t, m = days[i], days[i + 1]
    out = []
    for c in params.get("conditions") or []:
        key = f"{c}:{m['date']}"
        if c == "rain_tomorrow" and _rainy(m, params.get("rain_prob") or 50):
            what = WMO.get(m.get("code"), "有雨") if m.get("code") in RAIN else "可能有雨"
            out.append((key, f"明天{what}" + (f"（降水概率 {_f(m['prob'])}%）" if m.get("prob") is not None else "")))
        elif c == "temp_drop" and None not in (t.get("tmax"), m.get("tmax"), t.get("tmin"), m.get("tmin")):
            drop = max(t["tmax"] - m["tmax"], t["tmin"] - m["tmin"])
            if drop >= (params.get("drop") or 6):
                out.append((key, f"明天降温 {_f(drop)}°C（{_f(m['tmin'])}~{_f(m['tmax'])}°C）"))
        elif c == "hot" and m.get("tmax") is not None and m["tmax"] >= (35 if params.get("hot") is None else params["hot"]):
            out.append((key, f"明天高温 {_f(m['tmax'])}°C"))
        elif c == "cold" and m.get("tmin") is not None and m["tmin"] <= (0 if params.get("cold") is None else params["cold"]):
            out.append((key, f"明天最低 {_f(m['tmin'])}°C"))
        elif c == "wind" and m.get("wind") is not None and m["wind"] >= (params.get("wind") or 50):
            out.append((key, f"明天大风，最大风速 {_f(m['wind'])} km/h"))
        elif c == "snow" and ((m.get("snow") or 0) > 0 or m.get("code") in SNOW):
            out.append((key, f"明天{WMO.get(m.get('code'), '有雪') if m.get('code') in SNOW else '可能下雪'}"
                             + (f"（{_f(m['snow'])} cm）" if m.get("snow") else "")))
        elif c == "weekend" and weekday == 4:
            out.append((f"weekend:{m['date']}", "周末天气"))
    return out


def judge(params: dict, geo: dict, data: dict, state: dict | None, now: float) -> tuple[list, dict]:
    st = dict(state or {})
    off = int(data.get("utc_offset_seconds") or 0)
    local = time.gmtime(now + off)
    today = time.strftime("%Y-%m-%d", local)
    fired = [k for k in st.get("fired") or [] if k.split(":", 1)[-1] >= today]   # 过去的日子不用记了
    st["fired"] = fired
    if QUIET[1] <= local.tm_hour < QUIET[0]:
        new = [(k, s) for k, s in hits(params, days_of(data), today, local.tm_wday) if k not in fired]
    else:
        new = []
    if not new:
        return [], st
    st["fired"] = fired + [k for k, _ in new]
    days = days_of(data)
    i = next(n for n, d in enumerate(days) if d["date"] == today)
    lines = [describe_day(lb, d) for lb, d in zip(("今天", "明天", "后天"), days[i:i + 3])]
    return [{"id": f"weather-{geo['name']}-{'-'.join(k for k, _ in new)}",
             "title": f"{geo['name']}：" + "；".join(s for _, s in new),
             "text": "\n".join(lines)}], st


async def check(params: dict, state):
    st = dict(state or {})
    city = (params.get("city") or "").strip()
    geo = st.get("geo") if (st.get("geo") or {}).get("q") == city else None
    if not geo:
        geo = await geocode(city)
    st["geo"] = geo
    data = await forecast(geo["lat"], geo["lon"])
    return judge(params, geo, data, st, _now())


def validate(p: dict) -> dict:
    if not p.get("conditions"):
        raise ValueError("至少选一种要提醒的情况")
    return p


CONDITIONS = [
    {"value": "rain_tomorrow", "label": N_("明天有雨")}, {"value": "temp_drop", "label": N_("明天降温")},
    {"value": "hot", "label": N_("明天高温")}, {"value": "cold", "label": N_("明天低温")},
    {"value": "wind", "label": N_("明天大风")}, {"value": "snow", "label": N_("明天下雪")},
    {"value": "weekend", "label": N_("周末天气（周五报一次）")},
]

WEATHER = Source(
    id="weather", mode=POLL, category=N_("生活"), icon="🌦️",
    title=N_("天气提醒"),
    description=N_("明天下雨、降温、高温、大风、下雪时提醒一次，也能每周五报周末天气。数据来自 Open-Meteo，全球城市都能查；晚上 10 点到早上 7 点不打扰。"),
    fields=[
        {"key": "city", "label": N_("城市"), "type": "text", "required": True, "placeholder": N_("杭州 / Beijing / 30.27,120.15"),
         "help": N_("中文、英文城市名都行，查不到可以直接写经纬度")},
        {"key": "conditions", "label": N_("什么情况提醒"), "type": "multi", "default": ["rain_tomorrow"], "options": CONDITIONS,
         "help": N_("每种情况对同一天只提醒一次")},
        {"key": "rain_prob", "label": N_("降水概率达到（%）"), "type": "number", "default": 50, "min": 1, "max": 100},
        {"key": "drop", "label": N_("降温超过（°C）"), "type": "number", "default": 6, "min": 1, "max": 40,
         "help": N_("明天的最高或最低气温比今天低这么多")},
        {"key": "hot", "label": N_("高温线（°C）"), "type": "number", "default": 35, "min": -50, "max": 60},
        {"key": "cold", "label": N_("低温线（°C）"), "type": "number", "default": 0, "min": -60, "max": 40},
        {"key": "wind", "label": N_("大风线（km/h）"), "type": "number", "default": 50, "min": 10, "max": 300,
         "help": N_("6 级风大约 39–49 km/h，7 级 50–61，8 级 62–74")},
    ],
    check=check, every=180, min_every=60, fire_first=True, validate=validate,
    summary=lambda p: p.get("city") or "",
    sample={"title": "杭州（浙江，中国）：明天中雨（降水概率 85%）；明天降温 7°C（8~12°C）",
            "text": "今天 11月9日 周一：多云，13~19°C，降水概率 10%，最大风速 14 km/h\n"
                    "明天 11月10日 周二：中雨，8~12°C，降水概率 85%，降水 12.4 mm，最大风速 28 km/h\n"
                    "后天 11月11日 周三：阴，7~13°C，降水概率 30%，最大风速 16 km/h"},
    scenarios=[
        {"title": N_("明天下雨提醒带伞"), "description": N_("明天有雨时提前一天告诉你"),
         "name": N_("带伞提醒"), "params": {"city": "杭州", "conditions": ["rain_tomorrow"]},
         "prompt": N_("明天要下雨。用一两句话提醒我带伞，说清楚大概什么时候、雨大不大；雨很大的提醒我注意出行。")},
        {"title": N_("降温提醒加衣"), "description": N_("明天比今天冷很多时提醒你加衣服"),
         "name": N_("降温提醒"), "params": {"city": "杭州", "conditions": ["temp_drop", "cold"]},
         "prompt": N_("明天降温了。告诉我明天的气温，建议穿什么；家里有老人小孩的话也提醒一句注意保暖。")},
        {"title": N_("高温提醒"), "description": N_("明天特别热时提醒防暑"),
         "name": N_("高温提醒"), "params": {"city": "杭州", "conditions": ["hot"], "hot": 35},
         "prompt": N_("明天高温。提醒我防暑：少在中午出门、多喝水、注意防晒，一两句话就好。")},
        {"title": N_("大风、降雪提醒"), "description": N_("明天有大风或下雪时提前告诉你"),
         "name": N_("大风降雪"), "params": {"city": "北京", "conditions": ["wind", "snow"]},
         "prompt": N_("明天有大风或下雪。说清楚有多大，提醒我出行、收衣服、关窗这些要注意的事。")},
        {"title": N_("出行天气（周末）"), "description": N_("每周五告诉你周末的天气，适不适合出去玩"),
         "name": N_("周末天气"), "params": {"city": "杭州", "conditions": ["weekend"]},
         "prompt": N_("这是周末两天的天气预报。告诉我周末适不适合出门、哪天更好，给一两条安排建议（户外还是室内、要带什么）。")},
    ])

SOURCES = [WEATHER]
