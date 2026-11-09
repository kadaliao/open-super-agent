"""地震提醒：全球或你附近发生地震时，Bot 收到一个事件（几级、在哪、什么时候、多深、离你多远）。

数据来自美国地质调查局（USGS）的公开数据，全球的地震都有，一般震后十几分钟到半小时内出现。
在 Bot 的“事件”里选“地震提醒”，填最低震级；填了“在哪”（城市名，或者“纬度,经度”）就只报这个范围内的。
城市名用 Open-Meteo 的地名查询换成经纬度。第一次只记下现状，之后有新的才报。时间按服务器的时区写。
"""
import math
import re
from datetime import datetime
from ..i18n import N_
from ..registry import POLL, Source
from . import _util

FEED = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{}_day.geojson"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
KEEP_MS = 3 * 86400 * 1000      # 记多久之内报过的（源只有最近一天的）
LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,，\s]\s*(-?\d+(?:\.\d+)?)\s*$")
ALERT = {"yellow": "黄色（可能有伤亡和损失）", "orange": "橙色（伤亡和损失可能较大）", "red": "红色（伤亡和损失可能很大）"}


def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点之间的大圆距离（公里）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))


def latlon(s: str) -> tuple[float, float] | None:
    m = LATLON.match(s or "")
    if not m:
        return None
    lat, lon = float(m[1]), float(m[2])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("纬度要在 -90 到 90 之间，经度在 -180 到 180 之间")
    return lat, lon


async def locate(near: str, cached: dict | None) -> dict | None:
    """“在哪” → {q, lat, lon, name（你填的）, place（查到的是哪，带国家、省份）}。城市名查一次就记在 state 里，改了才重新查。
    同名的地方挑人口最多的；中文名查不准（“东京”会查到江苏的一个村）时事件里看得出来，换成英文名或经纬度。"""
    near = (near or "").strip()
    if not near:
        return None
    if cached and cached.get("q") == near:
        return cached
    if ll := latlon(near):
        return {"q": near, "lat": ll[0], "lon": ll[1], "name": near, "place": f"{ll[0]:g},{ll[1]:g}"}
    r = await _util.get(GEOCODE, params={"name": near, "count": 5, "language": "zh", "format": "json"})
    hits = (r.json() or {}).get("results") or []
    if not hits:
        raise ValueError(f"找不到“{near}”在哪：换个写法，试试英文名（Tokyo），或者直接填“纬度,经度”")
    h = max(hits, key=lambda x: x.get("population") or 0)
    name = h.get("name") or near
    extra = [x for x in dict.fromkeys((h.get("country"), h.get("admin1"))) if x and x != name]
    return {"q": near, "lat": float(h["latitude"]), "lon": float(h["longitude"]), "name": near,
            "place": name + (f"（{' '.join(extra)}）" if extra else "")}


def _time(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000).astimezone()
    return f"{d.year}年{d.month}月{d.day}日 {d:%H:%M}"


def quakes(data: dict, min_mag: float, where: dict | None, radius: float) -> list[dict]:
    """USGS 的 GeoJSON → 符合条件的地震（按时间从早到晚），每条带上离你多远。"""
    out = []
    for f in (data or {}).get("features") or []:
        p, coords = f.get("properties") or {}, ((f.get("geometry") or {}).get("coordinates") or [])
        mag = p.get("mag")
        if p.get("type") != "earthquake" or mag is None or mag < min_mag or len(coords) < 2 or not f.get("id"):
            continue
        q = {"id": f["id"], "mag": mag, "mag_type": p.get("magType") or "", "place": p.get("place") or "",
             "time": int(p.get("time") or 0), "lon": coords[0], "lat": coords[1],
             "depth": coords[2] if len(coords) > 2 else None, "url": p.get("url") or "",
             "tsunami": bool(p.get("tsunami")), "alert": p.get("alert") or "", "felt": p.get("felt")}
        if where:
            q["distance"] = distance(where["lat"], where["lon"], q["lat"], q["lon"])
            if q["distance"] > radius:
                continue
        out.append(q)
    return sorted(out, key=lambda q: q["time"])


def _spaced(s: str) -> str:
    """中文里夹英文、数字时两边空一格：“离 Tokyo 约”“离成都约”。"""
    return (" " if s[:1].isascii() else "") + s + (" " if s[-1:].isascii() else "")


def event(q: dict, where: dict | None) -> dict:
    far = f"，离{_spaced(where['name'])}约 {q['distance']:.0f} 公里" if where else ""
    lines = [f"震级：M{q['mag']:.1f}" + (f"（{q['mag_type']}）" if q["mag_type"] else ""),
             f"地点：{q['place'] or '未知'}（{q['lat']:.2f}, {q['lon']:.2f}）",
             f"时间：{_time(q['time'])}"]
    if q["depth"] is not None:
        lines.append(f"深度：{q['depth']:.0f} 公里")
    if where:
        lines.append(f"离你：约 {q['distance']:.0f} 公里（按“{where.get('place') or where['name']}”算）")
    if q["tsunami"]:
        lines.append("海啸：USGS 标了海啸相关信息，留意当地的海啸预警")
    if q["alert"] in ALERT:
        lines.append(f"影响评估（PAGER）：{ALERT[q['alert']]}")
    if q["felt"]:
        lines.append(f"有感报告：{q['felt']} 人")
    lines.append("数据来自 USGS，刚发生的震级、位置之后可能会修正。")
    return {"id": f"quake-{q['id']}", "title": f"M{q['mag']:.1f} 地震：{q['place'] or '未知地点'}{far}",
            "text": "\n".join(lines), "url": q["url"]}


async def check(params: dict, state):
    state = state or {}
    min_mag = float(params.get("min_magnitude") or 5)
    radius = float(params.get("radius_km") or 500)
    where = await locate(params.get("near") or "", state.get("where"))
    r = await _util.get(FEED.format("4.5" if min_mag >= 4.5 else "2.5"))
    found = quakes(r.json(), min_mag, where, radius)
    seen = dict(state.get("seen") or {})
    # 只记报过的：震级后来修正到线以上的，下次还能报
    new = [q for q in found if q["id"] not in seen]
    newest = max([q["time"] for q in found] + list(seen.values()) + [0])
    seen = {k: t for k, t in (seen | {q["id"]: q["time"] for q in new}).items() if t > newest - KEEP_MS}
    return [event(q, where) for q in new], {"seen": seen, "where": where}


def validate(p: dict) -> dict:
    if p.get("min_magnitude") is None:
        p["min_magnitude"] = 5
    if p.get("radius_km") is None:
        p["radius_km"] = 500
    latlon(p.get("near") or "")      # 写成了“纬度,经度”的话检查一下范围
    return p


def summary(p: dict) -> str:
    mag = f"{p.get('min_magnitude') or 5:g} 级以上"
    if (p.get("near") or "").strip():
        return f"{p['near'].strip()} {p.get('radius_km') or 500:g} 公里内、{mag}"
    return f"全球 {mag}"


EARTHQUAKE = Source(
    id="earthquake", mode=POLL, category=N_("生活"), icon="🌏",
    title=N_("地震提醒"),
    description=N_("全球或你附近发生地震时触发：震级、地点、时间、深度、离你多远。数据来自 USGS。"),
    fields=[
        {"key": "min_magnitude", "label": N_("最低震级"), "type": "number", "default": 5, "min": 2.5, "max": 10,
         "help": N_("低于这个震级的不报。4.5 级以下的只有部分地区（主要是美国）收得全")},
        {"key": "near", "label": N_("在哪"), "type": "text", "placeholder": N_("比如 成都，或者 30.66,104.07"),
         "help": N_("城市名或者“纬度,经度”，不填就是全球。中文名有时查不准（事件里会写按哪算的），换英文名或经纬度")},
        {"key": "radius_km", "label": N_("范围（公里）"), "type": "number", "default": 500, "min": 10, "max": 20000,
         "help": N_("填了“在哪”才有用：离那里多远以内的才报")},
    ],
    check=check, validate=validate, summary=summary, every=10, min_every=5,
    sample={"title": "M5.8 地震：43 km E of Onagawa Chō, Japan，离东京约 340 公里",
            "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000twz2",
            "text": "震级：M5.8（mww）\n地点：43 km E of Onagawa Chō, Japan（38.38, 141.94）\n时间：2026年11月9日 14:23\n"
                    "深度：56 公里\n离你：约 340 公里（按“東京（日本 东京都）”算）\n数据来自 USGS，刚发生的震级、位置之后可能会修正。"},
    scenarios=[
        {"title": N_("附近发生地震时提醒"), "description": N_("你所在的城市附近有感地震，第一时间告诉你（把“在哪”改成你的城市）"),
         "name": N_("附近地震"), "params": {"min_magnitude": 4, "near": "", "radius_km": 300},
         "prompt": N_("附近发生了地震。用两三句话告诉我：几级、在哪、离我多远、大概会不会有感或者有影响；"
                      "有海啸信息或影响评估是橙色、红色的放在最前面。地点是英文的翻成中文。")},
        {"title": N_("全球 6 级以上地震"), "description": N_("世界上任何地方发生 6 级以上的强震"),
         "name": N_("全球强震"), "params": {"min_magnitude": 6},
         "prompt": N_("发生了 6 级以上的强震。告诉我在哪个国家、哪个地区，几级、多深，可能的影响（人口多不多、有没有海啸风险）。"
                      "地点是英文的翻成中文，两三句话就够。")},
    ])

SOURCES = [EARTHQUAKE]
