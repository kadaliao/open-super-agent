"""天气查询：某个城市现在的天气和未来几天的预报（天气、最高最低气温、降水概率、风力、紫外线、日出日落）。

数据来自 Open-Meteo（免费、不用 key），全世界的城市都能查，城市名写中文或英文都行。
设置里的 default_city 是没说城市时查哪里（比如 "杭州"），不填就要每次说城市。
预报是按经纬度算的数值预报，和当地气象台发布的可能有出入；查过的城市位置在内存里记着，不重复查。
"""
import asyncio
from datetime import date
import httpx
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "天气查询", "version": "1.0", "author": "open-super-agent", "config": {"default_city": ""}}
GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
CURRENT = "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m,precipitation"
DAILY = ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,"
         "wind_speed_10m_max,wind_direction_10m_dominant,uv_index_max,sunrise,sunset")
MAX_PLACES = 200
TOTAL = 8   # 对话里也能用：查位置、再试一次、查预报加起来最多等这么久
WEEK = "一二三四五六日"

# WMO 天气代码（Open-Meteo 的 weather_code）
CODES = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴有小冰雹", 99: "雷阵雨伴有大冰雹",
}
# 蒲福风级的下限（米/秒）：第 n 个数是 n+1 级的起点
BEAUFORT = (0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7, 37.0, 41.5, 46.2, 51.0, 56.1)
DIRS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")

SCHEMA = {"type": "function", "function": {
    "name": "weather",
    "description": "查天气：某个城市现在的天气和未来几天的预报（气温、下不下雨、风、紫外线、日出日落）。"
                   "用户问天气、要不要带伞、穿什么、出行安排时用它，不要凭印象说天气。"
                   "问明天、周末、这几天就把 days 给够（今天算第 1 天）。",
    "parameters": {"type": "object", "properties": {
        "city": {"type": "string", "description": "城市名，中文或英文，比如 杭州、Tokyo；不写就用设置里的默认城市"},
        "days": {"type": "integer", "description": "预报几天，1 到 7，默认 3（今天、明天、后天）"}}}}}

_places: dict[str, dict] = {}   # 城市名 -> 地理编码的结果


def weather_word(c) -> str:
    return CODES.get(int(c), f"天气代码 {c}") if c is not None else "天气未知"


def wind(kmh, deg=None, peak: bool = False) -> str:
    """风速（km/h）和风向（度，风从哪边吹来）-> “西北风 3 级（19 km/h）”；peak 是一天里最大的风。"""
    if kmh is None:
        return ""
    level = sum(kmh / 3.6 >= b for b in BEAUFORT)
    if level == 0:
        return "无风"
    name = DIRS[round(deg / 45) % 8] + "风" if deg is not None else "风力"
    return f"{name}{'最大' if peak else ''} {level} 级（{round(kmh)} km/h）"


def uv_word(uv) -> str:
    for top, word in ((2, "弱"), (5, "中等"), (7, "强"), (10, "很强")):
        if uv < top + 1:
            return word
    return "极强"


def place_label(p: dict) -> str:
    name = p.get("name") or ""
    more = [x for x in (p.get("admin1"), p.get("country")) if x and not x.startswith(name)]
    return name + (f"（{'，'.join(dict.fromkeys(more))}）" if more else "")


def _t(x) -> str:
    return f"{x:g}°C" if x is not None else "?"


def day_line(i: int, d: dict) -> str:
    """daily 里的第 i 天写成一行。值是 null 的（远一点的日子有时没有降水概率、紫外线）就不写。"""
    def v(k):
        arr = d.get(k) or []
        return arr[i] if i < len(arr) else None
    day = date.fromisoformat(v("time"))   # 第一天是城市当地的今天（timezone=auto）
    when = f"{day:%m-%d} 周{WEEK[day.weekday()]}" + (("（今天）", "（明天）", "（后天）")[i] if i < 3 else "")
    parts = [weather_word(v("weather_code")), f"{_t(v('temperature_2m_min'))}～{_t(v('temperature_2m_max'))}"]
    if (p := v("precipitation_probability_max")) is not None:
        rain = v("precipitation_sum")
        parts.append(f"降水概率 {p}%" + (f"（{rain:g} mm）" if rain else ""))
    if w := wind(v("wind_speed_10m_max"), v("wind_direction_10m_dominant"), peak=True):
        parts.append(w)
    if (uv := v("uv_index_max")) is not None:
        parts.append(f"紫外线 {uv:g}（{uv_word(uv)}）")
    sun = [f"{k} {s[11:16]}" for k, s in (("日出", v("sunrise")), ("日落", v("sunset"))) if s]
    return f"{when}：" + "，".join(parts + sun)


def report(place: dict, data: dict, days: int) -> str:
    """Open-Meteo 的预报 -> 给模型看的几行字。"""
    lines = []
    if cur := data.get("current"):
        now = [f"{_t(cur.get('temperature_2m'))}" + (f"（体感 {_t(cur['apparent_temperature'])}）"
                                                    if cur.get("apparent_temperature") is not None else ""),
               weather_word(cur.get("weather_code"))]
        if cur.get("relative_humidity_2m") is not None:
            now.append(f"湿度 {cur['relative_humidity_2m']}%")
        if w := wind(cur.get("wind_speed_10m"), cur.get("wind_direction_10m")):
            now.append(w)
        if cur.get("precipitation"):
            now.append(f"刚才有降水（{cur['precipitation']:g} mm）")
        t = str(cur.get("time") or "").replace("T", " ")
        lines.append(f"{place_label(place)}，当地时间 {t}：现在 " + "，".join(now) + "。")
    daily = data.get("daily") or {}
    for i in range(min(days, len(daily.get("time") or []))):
        lines.append(day_line(i, daily))
    lines.append("预报来自 Open-Meteo（按经纬度算的数值预报），和当地气象台发布的可能有出入。")
    return "\n".join(lines)


def _candidates(city: str) -> list[str]:
    """“杭州市”在 Open-Meteo 里查不到、“杭州”能查到：带市、区、县、省的再去掉试一次。"""
    out = [city]
    if len(city) > 2 and city[-1] in "市区县省镇":
        out.append(city[:-1])
    return out


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    r = await client.get(url, params=params)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"天气服务返回的不是 JSON（HTTP {r.status_code}）") from None
    if r.status_code != 200 or data.get("error"):
        raise RuntimeError(data.get("reason") or f"HTTP {r.status_code}")
    return data


async def locate(client: httpx.AsyncClient, city: str) -> dict | None:
    if city in _places:
        return _places[city]
    for name in _candidates(city):
        found = (await _get(client, GEO, {"name": name, "count": 1, "language": "zh", "format": "json"})).get("results")
        if found:
            if len(_places) >= MAX_PLACES:
                _places.pop(next(iter(_places)))
            _places[city] = found[0]
            return found[0]
    return None


async def forecast(client: httpx.AsyncClient, place: dict, days: int) -> dict:
    return await _get(client, FORECAST, {"latitude": place["latitude"], "longitude": place["longitude"],
                                         "current": CURRENT, "daily": DAILY, "timezone": "auto", "forecast_days": days})


def days_of(v) -> int:
    try:
        return max(1, min(7, int(v or 3)))
    except (TypeError, ValueError):
        return 3


async def lookup(args: dict, default_city: str = "") -> str:
    city = str(args.get("city") or default_city or "").strip()
    if not city:
        return "没查：没说是哪个城市（设置里也没填 default_city），问一下用户在哪"
    days = days_of(args.get("days"))
    try:
        async with asyncio.timeout(TOTAL), \
                httpx.AsyncClient(timeout=5, headers={"User-Agent": "open-super-agent weather_now"}) as c:
            place = await locate(c, city)
            if not place:
                return f"没查到：找不到叫「{city}」的地方，换个写法（比如去掉“市”，或者写英文名）"
            return report(place, await forecast(c, place, days), days)
    except TimeoutError:
        return f"没查到：天气服务 {TOTAL} 秒内没回话，过一会儿再试"
    except (httpx.HTTPError, RuntimeError) as e:
        return f"没查到：天气服务暂时连不上（{e or type(e).__name__}），过一会儿再试"


def apply(ctx):
    default_city = ctx.config.get("default_city") or ""
    if not isinstance(default_city, str):
        raise ValueError("default_city 要是城市名（文字），比如 \"杭州\"")

    async def run(call):
        return await lookup(call.args, default_city.strip())

    ctx.tool(Tool("weather", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="查天气：现在的天气和未来几天的预报",
                  progress=lambda a: f"查天气 {a.get('city') or default_city}"))
