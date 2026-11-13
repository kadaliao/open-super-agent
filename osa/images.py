"""对话里画图：Router 的 generate_image 工具。

- 用设置 → 模型 → 画图 里选的模型（和生成头像共用，见 avatars.py）；一个能画图的模型都没有时照实告诉用户去设置里加。
- 画图要花钱，默认每次先弹确认卡片（approvals.ASK），卡片上写描述、模型和每张的价格；点“总是允许”以后就不问了。
- 要问的时候不让 Router 干等：工具马上返回“等用户确认”，卡片和画图在后台进行，Router 这一轮照常说完，
  用户也能在对话里说“画吧”（Router 用 resolve_approval 处理）。不用问的直接画，几秒后返回“已画好”。
- 画好的原图存进 media，对话里记一条 role=task、kind=image 的消息（带描述、模型、价格和附件），显示成 Bot 发的图片。
- 参考图（images 参数）：照着改、照着画。写对话里图片的编号（上下文里“[图片] 编号”，见 memory.context）或电脑里的路径
  （“[附件] 路径”）。路径先在消息的附件里找存好的那份，找不到再去电脑里读。带了参考图就用“照着图片画”的模型
  （avatars.current(refs)），各家怎么传按配方（generation.yaml）。参考了哪几张记在消息的 meta.refs 里。
"""
import asyncio
import re
from types import SimpleNamespace
from . import approvals, avatars, generation, i18n, library, media, prices
from .config import CFG
from .memory import conn

TOOL = "generate_image"
MAX_REFS = 16
GENERATE_IMAGE = {"type": "function", "function": {
    "name": TOOL,
    "description": "按描述画一张图，画好直接显示在对话里。默认会先弹卡片请用户确认（上面写着模型和价格）。"
                   "描述写具体：主体、风格、构图、颜色、光线；图里要有文字就写清楚是哪几个字。"
                   "用户要照着图片改、照着画（换风格、换背景、加减东西、照这个人物或商品画）时，把那几张图放进 images，"
                   "描述里写清楚怎么改、每张图是干什么用的。",
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "画面描述，最多 1000 字"},
        "images": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_REFS,
                   "description": "参考图（可选）：对话里图片的编号（“[图片]”后面那串）或者电脑里图片的路径（“[附件]”后面那个）"}},
        "required": ["prompt"]}}}

NO_MODEL = ("没画：还没有能画图的模型。照实告诉用户：去 设置 → 模型 添加有画图模型的服务（硅基流动的 Kolors 免费，"
            "还有 OpenAI 的 gpt-image、智谱的 CogView 等），再在“画图”那一行选模型。不要假装画了，也不要派任务去画。")
NO_REF_MODEL = ("没画：现在的画图模型都不能照着图片画。照实告诉用户：去 设置 → 模型 添加能照着图片画的服务（硅基流动的 Qwen-Image-Edit、"
                "Kolors，OpenAI 的 gpt-image，火山方舟的 Seedream 4 以后的版本），再在“画图”里选“照着图片画”用的模型。"
                "也可以不带参考图、只按描述画。不要假装画了。")

_running: set[asyncio.Task] = set()   # 后台等确认、画图的，留个引用免得被回收


def price_text(price: float | None) -> str:
    if price is None:
        return i18n.t("价格未知")
    if price == 0:
        return i18n.t("免费")
    return i18n.t("{price} / 张", price=f"{CFG['pricing']['currency']}{price:g}")


def _name(prompt: str) -> str:
    """图片的文件名：描述的前几个字。"""
    words = re.sub(r"[\\/:*?\"<>|\s]+", "-", prompt.strip())[:24].strip("-")
    return (words or "image") + ".png"


def _by_path(path: str) -> dict | None:
    """消息附件里存过的这张图（用户上传的、send_file 发过的），不用开电脑。"""
    try:
        cands = {path, library.norm(path)}
    except ValueError:
        cands = {path}
    for p in cands:
        r = conn.execute("""select json_extract(a.value, '$.media') from messages m, json_each(m.meta, '$.attachments') a
            where m.meta like '%"media"%' and json_extract(a.value, '$.path') = ? and json_extract(a.value, '$.media') is not null
            order by m.id desc limit 1""", (p,)).fetchone()
        if r and (got := media.info(r[0])):
            return got
    return None


async def find_refs(items) -> list[dict]:
    """参考图 → [{id, mime, name, ...}]（media 里的图片）。写的是编号或路径，找不到、不是图片时报 ValueError。"""
    out: list[dict] = []
    for x in (items if isinstance(items, list) else [items] if items else [])[:MAX_REFS]:
        x = str(x or "").strip().removeprefix("[图片]").removeprefix("[附件]").strip()   # i18n-ignore：上下文里的标记
        if not x:
            continue
        if re.fullmatch(r"[0-9a-f]{24}", x):
            got = media.info(x)
            if not got:
                raise ValueError(f"对话里没有编号是 {x} 的图片")
        elif not (got := _by_path(x)):
            att = await media.from_computer(x)
            if not att.get("media"):
                raise ValueError(f"{att['name']} 不是图片（只认 PNG、JPG、WebP、GIF，最大 {media.MAX_BYTES >> 20} MB）")
            got = media.info(att["media"])
        if got["mime"] not in media.MODEL_MIMES:
            raise ValueError(f"{got['name']} 是 {got['mime']}，画图模型只收 PNG、JPG、WebP")
        if all(r["id"] != got["id"] for r in out):
            out.append(got)
    return out


def _load(refs: list[dict]) -> list[generation.Ref]:
    out = []
    for r in refs:
        got = media.get(r["id"])
        if not got:
            raise ValueError(i18n.t("参考图已经被清理掉了：{name}", name=r["name"]))
        out.append(generation.Ref(got[1], got[0], r["name"], role="image", w=r.get("w") or 0, h=r.get("h") or 0))
    return out


async def _make(chat_id: int, prompt: str, m: dict, post, refs: list[dict]) -> str:
    """真的画，画好发进对话。返回给模型的话；失败时报 ValueError。"""
    data, _ = await avatars.draw(prompt, m["provider"], m["model"], _load(refs))
    item = media.save(data, _name(prompt), limit=generation.MAX_IMAGE)
    price = prices.image_price(m["provider"], m["model"])
    att = {"name": item["name"], "size": item["size"], "media": item["id"], "mime": item["mime"], "w": item["w"], "h": item["h"]}
    await post("task", f"[画图] {prompt}", chat_id, kind="image", prompt=prompt, model=m["model"], provider=m["provider"],
               price=price, attachments=[att], **({"refs": [{"media": r["id"], "name": r["name"]} for r in refs]} if refs else {}))
    return "已画好，图片已经显示在对话里，用户看得到。不用描述画面，简短说一句就行。"


async def generate(bot: dict, chat_id: int, private_chat_id: int, prompt: str, post, images=None) -> str:
    """generate_image 工具。bot：谁在画；chat_id：图片发到哪个对话；private_chat_id：确认卡片发到哪（Bot 的私聊）。
    post 是 Router.add：(role, content, chat_id, **meta)。images：参考图（编号或路径）。"""
    prompt = (prompt or "").strip()[:1000]
    if not prompt:
        return "没画：描述是空的"
    try:
        refs = await find_refs(images)
    except Exception as e:
        return f"没画：参考图有问题：{e}"
    m = await avatars.current(len(refs))
    if not m:
        if not refs:
            return NO_MODEL
        most = max((x["refs"] for x in (await avatars.models())["edit_models"]), default=0)
        if not most:
            return NO_REF_MODEL
        return f"没画：能用的画图模型最多参考 {most} 张图，你给了 {len(refs)} 张。挑最要紧的 {most} 张再画，或者问用户用哪几张。"
    price = prices.image_price(m["provider"], m["model"])
    if not approvals.decide(TOOL)[0]:
        try:
            return await _make(chat_id, prompt, m, post, refs)
        except ValueError as e:
            return f"没画成：{e}"

    # 冒充一个任务弹确认卡片（和 Router._direct 一样）；卡片总在 Bot 的私聊里，群里、讨论串里有提示
    fake = SimpleNamespace(id=f"对话·{bot['name']}", bot_id=bot["id"], chat_id=chat_id, approval_chat_id=private_chat_id,
                           goal=i18n.t("在对话里画图"), background=False, waiting_approval=None)
    shown = {"描述": prompt, **({"参考图": "、".join(r["name"] for r in refs)} if refs else {}),
             "模型": f"{m['model']} · {m['title']}", "价格": price_text(price)}

    async def wait_and_draw():
        ok, _ = await approvals.request(fake, TOOL, shown)
        if not ok:
            return
        try:
            await _make(chat_id, prompt, m, post, refs)
        except Exception as e:
            await post("task", i18n.t("[没画成：{error}]", error=e), chat_id, kind="note")

    t = asyncio.create_task(wait_and_draw())
    _running.add(t)
    t.add_done_callback(_running.discard)
    return (f"已弹出确认卡片（模型 {m['model']}，{price_text(price)}），用户同意后会自动画好、显示在对话里，你不用再回来。"
            "简短告诉用户确认一下就行，不要说已经画好。")
