"""Epic 免费游戏：Epic 游戏商城送新游戏时，Bot 收到一个事件（游戏名、免费到什么时候、商店链接）。

在 Bot 的“事件”里选“Epic 免费游戏”。默认半天看一次，第一次只记下正在送的，之后有新的才报。
用的是 Epic 商店自己的免费游戏接口，不用登录、不调模型。时间按服务器的时区写。
"""
from datetime import datetime, timezone
from ..i18n import N_
from ..registry import POLL, Source
from . import _util

API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
STORE = "https://store.epicgames.com/zh-CN"
MAX_SEEN = 200
COUNTRIES = [("CN", N_("中国大陆")), ("HK", N_("香港")), ("TW", N_("台湾")), ("US", N_("美国")), ("JP", N_("日本"))]


def _when(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _free_offers(groups) -> list[tuple[datetime, datetime]]:
    """促销里“打 0 折”的那几段（Epic 的 discountPercentage 是剩下几成，0 就是免费）。"""
    out = []
    for g in groups or []:
        for o in (g or {}).get("promotionalOffers") or []:
            if (o.get("discountSetting") or {}).get("discountPercentage") != 0:
                continue
            a, b = _when(o.get("startDate")), _when(o.get("endDate"))
            if a and b:
                out.append((a, b))
    return out


def link(el: dict) -> str:
    slug = ""
    for m in ((el.get("catalogNs") or {}).get("mappings") or []) + (el.get("offerMappings") or []):
        if m.get("pageSlug") and m.get("pageType") == "productHome":
            slug = m["pageSlug"]
            break
    slug = slug or (el.get("productSlug") or "").removesuffix("/home")
    if not slug:
        return f"{STORE}/free-games"
    return f"{STORE}/{'bundles' if el.get('offerType') == 'BUNDLE' else 'p'}/{slug}"


def _day(d: datetime) -> str:
    d = d.astimezone()
    return f"{d.month}月{d.day}日 {d:%H:%M}"


def games(data: dict, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """接口返回的 JSON → (正在免费送的, 预告的)：[{key, title, start, end, url, price, description}]。"""
    now = now or datetime.now(timezone.utc)
    store = ((((data or {}).get("data") or {}).get("Catalog") or {}).get("searchStore") or {})
    els = store.get("elements") or []
    current, upcoming = [], []
    for el in els:
        promos = el.get("promotions") or {}
        price = ((el.get("price") or {}).get("totalPrice") or {})
        base = {"title": (el.get("title") or "").strip(), "url": link(el),
                "price": (price.get("fmtPrice") or {}).get("originalPrice") or "",
                "description": _util.oneline(el.get("description") or "", 200)}
        for a, b in _free_offers(promos.get("promotionalOffers")):
            if a <= now < b and price.get("discountPrice", 0) == 0:
                current.append(base | {"key": f"{el.get('id')}@{a.isoformat()}", "start": a, "end": b})
        for a, b in _free_offers(promos.get("upcomingPromotionalOffers")):
            if a > now:
                upcoming.append(base | {"key": f"{el.get('id')}@{a.isoformat()}", "start": a, "end": b})
    return current, sorted(upcoming, key=lambda g: g["start"])


def event(g: dict, upcoming: list[dict]) -> dict:
    lines = [f"免费领取：{_day(g['start'])} 到 {_day(g['end'])}（过了时间就恢复原价，领到的永久归你）"]
    if g["price"] and g["price"] != "0":
        lines.append(f"原价：{g['price']}")
    if g["description"] and g["description"] != g["title"]:
        lines.append(f"简介：{g['description']}")
    if upcoming:
        lines.append("下一批预告：" + "；".join(f"{u['title']}（{_day(u['start'])} 起）" for u in upcoming[:5]))
    return {"id": f"epic-{g['key']}", "title": f"Epic 免费送：{g['title']}", "text": "\n".join(lines), "url": g["url"]}


async def check(params: dict, state):
    country = params.get("country") or "CN"
    r = await _util.get(API, params={"locale": "zh-CN", "country": country, "allowCountries": country})
    current, upcoming = games(r.json())
    seen = list((state or {}).get("seen") or [])
    new = [g for g in current if g["key"] not in seen]
    # 第一次也把正在送的返回去：不会发给 Bot（只记下现状），但能当“测试”用的真实例子
    seen = ([g["key"] for g in new] + seen)[:MAX_SEEN]
    return [event(g, upcoming) for g in new], {"seen": seen}


EPIC_FREE = Source(
    id="epic_free", mode=POLL, category=N_("生活"), icon="🎮",
    title=N_("Epic 免费游戏"),
    description=N_("Epic 游戏商城开始送新的免费游戏时触发：游戏名、免费到什么时候、商店链接。"),
    fields=[{"key": "country", "label": N_("商店地区"), "type": "select", "default": "CN",
             "options": [{"value": v, "label": l} for v, l in COUNTRIES],
             "help": N_("不同地区送的游戏偶尔不一样")}],
    check=check, every=720, min_every=60,
    summary=lambda p: "" if (p.get("country") or "CN") == "CN" else dict(COUNTRIES).get(p["country"], p["country"]) + "商店",
    sample={"title": "Epic 免费送：浮游世界", "url": "https://store.epicgames.com/zh-CN/p/flotsam-266287",
            "text": "免费领取：11月6日 00:00 到 11月13日 00:00（过了时间就恢复原价，领到的永久归你）\n原价：¥75.00\n"
                    "简介：漂浮垃圾城生存游戏！从海洋表面尽可能搜集珍贵的垃圾，发展你的城市。\n"
                    "下一批预告：斗阵骑士 Knightica（11月13日 00:00 起）"},
    scenarios=[
        {"title": N_("Epic 送新游戏时告诉我"), "description": N_("每周送的免费游戏一出来就告诉你，附上领取链接"),
         "name": N_("Epic 免费游戏"),
         "prompt": N_("Epic 在送新的免费游戏。用一两句话介绍这是什么类型的游戏、口碑大概怎样，告诉我领取截止时间，附上商店链接。"
                      "有下一批预告的也顺带提一句。")},
    ])

SOURCES = [EPIC_FREE]
