"""头像图片：Bot 和群聊可以用自己的图片当头像，上传的，或者让生图模型画的。

- 图片在浏览器里裁成正方形、缩到 256 像素再传上来，存在数据库的 avatars 表里，备份时跟着走。
- 用 /avatars/<id> 访问。id 是内容的哈希，同一张图只存一份。不用登录也能看（分享出去的页面也显示 Bot 的头像），
  所以只收 PNG / JPEG / WebP，不收 SVG 这种能带脚本的格式。
- 没人用的图片放一天后清掉：刚传上来、还没点保存的不能删。
- 生图走兼容 OpenAI 的 /images/generations。硅基流动的参数名和返回格式不一样（image_size、images[].url），单独处理。
"""
import base64
import hashlib
import json
import re
import time
import httpx
from . import i18n, providers
from .config import CFG
from .memory import conn

conn.execute("create table if not exists avatars(id text primary key, mime text, data blob, created real)")
conn.commit()

MAX_BYTES = 512 * 1024          # 256×256 的 PNG 一般一两百 KB
MAX_GENERATED = 20 << 20        # 生图服务给回来的原图
KEEP_S = 86400
URL = re.compile(r"^/avatars/([0-9a-f]{24})$")

# 能画图的模型，按名字认；-Edit 这类改图的模型要传原图，不算。按这个顺序推荐：Kolors 在硅基流动免费，Z-Image-Turbo 便宜
IMAGE_MODEL = re.compile(r"kolors|qwen-image|z-image|flux|stable-diffusion|sdxl|dall-e|gpt-image|cogview|seedream|imagen-|wanx|hidream|hunyuan-image", re.I)
PREFER = ["kolors", "z-image-turbo", "gpt-image", "cogview", "qwen-image", "z-image", "flux", "seedream"]
# 硅基流动每个模型要的尺寸（文档里推荐的正方形尺寸）
SF_SIZE = {"qwen-image": "1328x1328", "z-image": "512x512"}


def _mime(b: bytes) -> str | None:
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def save(data_url: str) -> str:
    """存一张头像（data:image/...;base64,...），返回它的地址。"""
    m = re.match(r"^data:image/[\w.+-]+;base64,(.+)$", data_url or "", re.S)
    try:
        b = base64.b64decode(m.group(1), validate=True) if m else b""
    except ValueError:
        b = b""
    if not b:
        raise ValueError(i18n.t("没收到图片"))
    if len(b) > MAX_BYTES:
        raise ValueError(i18n.t("图片太大了（最多 {n} KB）", n=MAX_BYTES // 1024))
    mime = _mime(b)
    if not mime:
        raise ValueError(i18n.t("只支持 PNG、JPG、WebP 图片"))
    aid = hashlib.sha256(b).hexdigest()[:24]
    # 已经有的同一张图：刷新时间，免得刚选上就被当成没人用的清掉
    conn.execute("insert into avatars(id, mime, data, created) values(?,?,?,?) on conflict(id) do update set created=excluded.created",
                 (aid, mime, b, time.time()))
    conn.commit()
    return f"/avatars/{aid}"


def get(aid: str) -> tuple[str, bytes] | None:
    r = conn.execute("select mime, data from avatars where id=?", (aid,)).fetchone()
    return (r[0], r[1]) if r else None


def valid(url) -> bool:
    m = URL.match(url) if isinstance(url, str) else None
    return bool(m and conn.execute("select 1 from avatars where id=?", (m.group(1),)).fetchone())


def gc():
    """清掉一天前传上来、现在没有 Bot 或群聊在用的图片。"""
    used = set()
    for (a,) in conn.execute("select avatar from bots"):
        if (m := URL.match(str((json.loads(a or "{}") or {}).get("image") or ""))):
            used.add(m.group(1))
    for (u,) in conn.execute("select image from chats where image is not null"):
        if (m := URL.match(u)):
            used.add(m.group(1))
    old = [r[0] for r in conn.execute("select id from avatars where created < ?", (time.time() - KEEP_S,)) if r[0] not in used]
    if old:
        conn.executemany("delete from avatars where id=?", [(x,) for x in old])
        conn.commit()


# ---------- AI 生成 ----------
def _rank(model: str) -> int:
    x = model.lower()
    return next((i for i, k in enumerate(PREFER) if k in x), len(PREFER))


def _choice() -> dict:
    r = conn.execute("select value from settings where key='avatar_model'").fetchone()
    return json.loads(r[0]) if r else {}


async def models() -> dict:
    """已经配好的模型服务里能画图的模型，和上次用的那个。"""
    out = []
    for name in CFG["providers"]:
        if not providers.usable(name):
            continue
        try:
            r = await providers.models(name)
        except ValueError:
            continue
        title = providers.describe(name)["title"]
        out += [{"provider": name, "title": title, "model": m} for m in r.get("models") or []
                if IMAGE_MODEL.search(m) and "edit" not in m.lower()]
    out.sort(key=lambda x: (_rank(x["model"]), x["provider"], x["model"]))
    c = _choice()
    last = next((x for x in out if x["provider"] == c.get("provider") and x["model"] == c.get("model")), None)
    return {"models": out, "default": last or (out[0] if out else None)}


def _error(r: httpx.Response) -> str:
    try:
        d = r.json()
        msg = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else d.get("error")
        msg = msg or d.get("message") or d.get("detail")
    except ValueError:
        msg = r.text[:200]
    return f"HTTP {r.status_code}" + (f"：{msg}" if msg else "")


async def generate(prompt: str, provider: str, model: str) -> str:
    """让生图模型画一张，返回原图的 data URL（浏览器再裁成 256 像素传回来）。"""
    prompt = (prompt or "").strip()[:1000]
    if not prompt:
        raise ValueError(i18n.t("写一句想要什么样的头像"))
    if provider not in CFG["providers"] or not providers.usable(provider):
        raise ValueError(i18n.t("没有这个模型服务：{name}", name=provider))
    if not model or not IMAGE_MODEL.search(model):
        raise ValueError(i18n.t("{model} 不是画图的模型", model=model))
    base = CFG["providers"][provider]["base_url"].rstrip("/")
    key = providers.api_key(provider)
    if "siliconflow" in base:
        size = next((v for k, v in SF_SIZE.items() if k in model.lower()), "1024x1024")
        body = {"model": model, "prompt": prompt, "image_size": size, "batch_size": 1}
    else:
        body = {"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"}
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
        try:
            r = await c.post(f"{base}/images/generations", json=body, headers={"Authorization": f"Bearer {key}"} if key else {})
        except httpx.TimeoutException:
            raise ValueError(i18n.t("画图失败：{e}", e=i18n.t("连接超时：{url}", url=base)))
        except httpx.HTTPError:
            raise ValueError(i18n.t("画图失败：{e}", e=i18n.t("连不上 {url}", url=base)))
        if r.status_code >= 400:
            raise ValueError(i18n.t("画图失败：{e}", e=_error(r)))
        d = r.json()
        item = ((d.get("data") or d.get("images") or [None])[0]) or {}
        if item.get("b64_json"):
            b = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            got = await c.get(item["url"])   # 硅基流动的图片地址一小时后失效，当场取回来
            if got.status_code >= 400:
                raise ValueError(i18n.t("画图失败：{e}", e=f"HTTP {got.status_code}"))
            b = got.content
        else:
            raise ValueError(i18n.t("画图服务没有返回图片"))
    mime = _mime(b)
    if not mime or len(b) > MAX_GENERATED:
        raise ValueError(i18n.t("画图服务没有返回图片"))
    conn.execute("insert or replace into settings values('avatar_model', ?)", (json.dumps({"provider": provider, "model": model}),))
    conn.commit()
    return f"data:{mime};base64," + base64.b64encode(b).decode()
