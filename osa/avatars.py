"""头像图片：Bot 和群聊可以用自己的图片当头像，上传的，或者让生图模型画的。

- 图片在浏览器里裁成正方形、缩到 256 像素再传上来，存在数据库的 avatars 表里，备份时跟着走。
- 用 /avatars/<id> 访问。id 是内容的哈希，同一张图只存一份。不用登录也能看（分享出去的页面也显示 Bot 的头像），
  所以只收 PNG / JPEG / WebP，不收 SVG 这种能带脚本的格式。
- 没人用的图片放一天后清掉：刚传上来、还没点保存的不能删。
- 生图的接口（各家参数不一样）在 imagegen.py，这里只管用哪个模型。
- 用哪个模型画（settings 表的 image_model）和对话里画图共用：设置 → 模型 → 画图 里选，生成头像时换了也记下来。
  以前叫 avatar_model，读不到新的就读旧的。照着图片画（对话里带了参考图）另选一个（image_edit_model），
  没选过就用能收参考图的模型里排最前的。每画一张在 llm_calls 记一行（role=image，0 token，费用按 pricing.images）。
"""
import base64
import hashlib
import json
import re
import time
from . import i18n, imagegen, prices, providers
from .config import CFG
from .memory import conn

conn.execute("create table if not exists avatars(id text primary key, mime text, data blob, created real)")
conn.commit()

MAX_BYTES = 512 * 1024          # 256×256 的 PNG 一般一两百 KB
KEEP_S = 86400
URL = re.compile(r"^/avatars/([0-9a-f]{24})$")

IMAGE_MODEL = imagegen.IMAGE_MODEL
# 按这个顺序推荐：Kolors 在硅基流动免费，Z-Image-Turbo 便宜
PREFER = ["kolors", "z-image-turbo", "gpt-image", "cogview", "qwen-image", "z-image", "flux", "seedream"]
# 照着图片画：会按说的改（换背景、换风格、加东西）的排前面；Kolors 只是图生图，改不了细节，放最后
REF_PREFER = ["qwen-image-edit-2", "gpt-image", "seedream", "qwen-image-edit", "kontext", "kolors"]


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
    mime = imagegen.sniff(b)
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


# ---------- 用哪个模型画 ----------
def _rank(model: str, order: list[str] = PREFER) -> int:
    x = model.lower()
    return next((i for i, k in enumerate(order) if k in x), len(order))


def _setting(key: str) -> dict:
    r = conn.execute("select value from settings where key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else {}


def _choice() -> dict:
    return _setting("image_model") or _setting("avatar_model")


def choose(provider: str, model: str, edit: bool = False):
    """记下画图用哪个模型（provider 为空就是不指定，用排在最前面的）。edit：照着图片画用的那个。"""
    conn.execute("insert or replace into settings values(?, ?)",
                 ("image_edit_model" if edit else "image_model", json.dumps({"provider": provider, "model": model})))
    conn.commit()


def choice(edit: bool = False) -> dict:
    """设置里选的画图模型：{provider, model}，没选过是空的。edit：照着图片画用的那个。"""
    c = _setting("image_edit_model") if edit else _choice()
    return {"provider": c.get("provider") or "", "model": c.get("model") or ""}


async def current(refs: int = 0) -> dict | None:
    """现在画图用的模型（选过的还能用就用它，否则排在最前面的），一个都没有返回 None。
    refs：带了几张参考图。带了就从能收这么多张的模型里挑（照着图片画选的那个，或者排最前的）。"""
    d = await models()
    if not refs:
        return d["default"]
    ok = [x for x in d["edit_models"] if x["refs"] >= refs]
    c = choice(edit=True)
    return next((x for x in ok if x["provider"] == c["provider"] and x["model"] == c["model"]), None) or (ok[0] if ok else None)


async def models() -> dict:
    """已经配好的模型服务里能画图的模型（带每张的价格，不知道是 None；refs 是最多收几张参考图）。
    models、default：直接画的（只能改图的不算）和现在用的那个；edit_models、edit_default：能照着图片画的。"""
    out = []
    for name in CFG["providers"]:
        if not providers.usable(name):
            continue
        try:
            r = await providers.models(name)
        except ValueError:
            continue
        title = providers.describe(name)["title"]
        for m in r.get("models") or []:
            if IMAGE_MODEL.search(m):
                s = imagegen.spec(name, m)
                out.append({"provider": name, "title": title, "model": m, "price": prices.image_price(name, m),
                            "refs": s.refs, "needs_ref": s.needs_ref})
    draw = sorted((x for x in out if not x["needs_ref"]), key=lambda x: (_rank(x["model"]), x["provider"], x["model"]))
    edit = sorted((x for x in out if x["refs"]), key=lambda x: (_rank(x["model"], REF_PREFER), x["provider"], x["model"]))

    def pick(items, c):
        return next((x for x in items if x["provider"] == c.get("provider") and x["model"] == c.get("model")), None) \
            or (items[0] if items else None)
    return {"models": draw, "default": pick(draw, _choice()), "edit_models": edit, "edit_default": pick(edit, choice(edit=True))}


async def generate(prompt: str, provider: str, model: str) -> str:
    """让生图模型画一张头像，返回原图的 data URL（浏览器再裁成 256 像素传回来）。"""
    if not (prompt or "").strip():
        raise ValueError(i18n.t("写一句想要什么样的头像"))
    b, mime = await draw(prompt, provider, model)
    return f"data:{mime};base64," + base64.b64encode(b).decode()


async def draw(prompt: str, provider: str, model: str, refs: list[imagegen.Ref] | None = None) -> tuple[bytes, str]:
    """让生图模型画一张，返回 (原图, mime)。直接画的（没带参考图）用了哪个模型记下来，下次默认用它。"""
    b, mime = await imagegen.draw(prompt, provider, model, refs)
    if not refs:
        choose(provider, model)
    return b, mime
