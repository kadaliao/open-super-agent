"""订阅 RSS：定时看几个 RSS / Atom 订阅源，有新文章就以 Bot 的名义发到它的对话里。

不调模型、不花钱。设置里填 feeds（订阅源地址的列表）、bot（发给哪个 Bot，名字，不填就是主 Bot）、every_minutes（多久看一次，至少 10）。
第一次只记下现有的文章，不发；之后每次最多发 10 篇新的。看过哪些记在插件自己的数据目录里，停用、重启都不会重复发。
"""
import asyncio
import json
import xml.etree.ElementTree as ET
import httpx

META = {"title": "订阅 RSS", "version": "1.0", "author": "open-super-agent", "inject": ["messenger"],
        "config": {"feeds": [], "bot": "", "every_minutes": 60}}
MAX_BYTES = 2_000_000
MAX_POST = 10
MAX_SEEN = 500   # 每个订阅源记多少条看过的
A = "{http://www.w3.org/2005/Atom}"


def parse(xml: bytes) -> tuple[str, list[dict]]:
    """RSS 2.0 或 Atom → (订阅源标题, [{id, title, link}])，新的在前（按订阅源自己的顺序）。"""
    root = ET.fromstring(xml)
    if root.tag == A + "feed":
        title = (root.findtext(A + "title") or "").strip()
        items = []
        for e in root.findall(A + "entry"):
            link = next((l.get("href") for l in e.findall(A + "link") if l.get("rel") in (None, "alternate")), None) or ""
            items.append({"id": (e.findtext(A + "id") or link).strip(), "title": (e.findtext(A + "title") or "").strip(), "link": link})
        return title, items
    ch = root.find("channel")
    if ch is None:
        raise ValueError("不是 RSS 或 Atom")
    items = [{"id": (i.findtext("guid") or i.findtext("link") or i.findtext("title") or "").strip(),
              "title": (i.findtext("title") or "").strip(), "link": (i.findtext("link") or "").strip()} for i in ch.findall("item")]
    return (ch.findtext("title") or "").strip(), items


async def fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "open-super-agent rss_watch"}) as c:
        r = await c.get(url)
        r.raise_for_status()
        if len(r.content) > MAX_BYTES:
            raise ValueError("订阅源太大")
        return r.content


def message(title: str, url: str, new: list[dict]) -> str:
    lines = [f"- [{i['title'] or i['link']}]({i['link']})" if i["link"] else f"- {i['title']}" for i in new[:MAX_POST]]
    more = f"\n…还有 {len(new) - MAX_POST} 篇" if len(new) > MAX_POST else ""
    return f"📰 **{title or url}** 有 {len(new)} 篇新文章：\n" + "\n".join(lines) + more


async def poll(ctx) -> int:
    """看一遍所有订阅源，发新文章。返回发了几条消息。"""
    state_file = ctx.data_dir / "seen.json"
    seen = json.loads(state_file.read_text()) if state_file.exists() else {}
    sent = 0
    for url in ctx.config.get("feeds") or []:
        try:
            title, items = parse(await fetch(url))
        except Exception as e:
            ctx.log.warning("订阅源 %s 读不了：%s", url, e)
            continue
        ids = [i["id"] for i in items if i["id"]]
        if url in seen:
            old = set(seen[url])
            new = [i for i in items if i["id"] and i["id"] not in old]
            if new:
                await ctx.services["messenger"].post(message(title, url, new), bot=ctx.config.get("bot") or None)
                sent += 1
            seen[url] = (ids + [x for x in seen[url] if x not in ids])[:MAX_SEEN]
        else:
            seen[url] = ids[:MAX_SEEN]   # 第一次：只记下，不发
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(seen, ensure_ascii=False))
    return sent


def apply(ctx):
    feeds = ctx.config.get("feeds")
    if not isinstance(feeds, list) or not all(isinstance(u, str) and u.startswith(("https://", "http://")) for u in feeds):
        raise ValueError("feeds 要是订阅源地址的列表（http 或 https 开头）")
    every = max(10, int(ctx.config.get("every_minutes") or 60)) * 60

    async def loop():
        await asyncio.sleep(10)   # 服务刚启动、刚启用时先等一会儿
        while True:
            try:
                await poll(ctx)
            except Exception as e:
                ctx.log.warning("看订阅源出错：%s", e)
            await asyncio.sleep(every)

    if feeds:
        ctx.task(loop())
