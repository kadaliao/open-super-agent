"""对话里画图：Router 的 generate_image 工具。

- 用设置 → 模型 → 画图 里选的模型（和生成头像共用，见 avatars.py）；一个能画图的模型都没有时照实告诉用户去设置里加。
- 画图要花钱，默认每次先弹确认卡片（approvals.ASK），卡片上写描述、模型和每张的价格；点“总是允许”以后就不问了。
- 要问的时候不让 Router 干等：工具马上返回“等用户确认”，卡片和画图在后台进行，Router 这一轮照常说完，
  用户也能在对话里说“画吧”（Router 用 resolve_approval 处理）。不用问的直接画，几秒后返回“已画好”。
- 画好的原图存进 media，对话里记一条 role=task、kind=image 的消息（带描述、模型、价格和附件），显示成 Bot 发的图片。
"""
import asyncio
import re
from types import SimpleNamespace
from . import approvals, avatars, i18n, media, prices
from .config import CFG

TOOL = "generate_image"
GENERATE_IMAGE = {"type": "function", "function": {
    "name": TOOL,
    "description": "按描述画一张图，画好直接显示在对话里。默认会先弹卡片请用户确认（上面写着模型和价格）。"
                   "描述写具体：主体、风格、构图、颜色、光线；图里要有文字就写清楚是哪几个字。",
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "画面描述，最多 1000 字"}}, "required": ["prompt"]}}}

NO_MODEL = ("没画：还没有能画图的模型。照实告诉用户：去 设置 → 模型 添加有画图模型的服务（硅基流动的 Kolors 免费，"
            "还有 OpenAI 的 gpt-image、智谱的 CogView 等），再在“画图”那一行选模型。不要假装画了，也不要派任务去画。")

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


async def _make(chat_id: int, prompt: str, m: dict, post) -> str:
    """真的画，画好发进对话。返回给模型的话；失败时报 ValueError。"""
    data, _ = await avatars.draw(prompt, m["provider"], m["model"])
    item = media.save(data, _name(prompt), limit=avatars.MAX_GENERATED)
    price = prices.image_price(m["provider"], m["model"])
    att = {"name": item["name"], "size": item["size"], "media": item["id"], "mime": item["mime"], "w": item["w"], "h": item["h"]}
    await post("task", f"[画图] {prompt}", chat_id, kind="image", prompt=prompt, model=m["model"], provider=m["provider"],
               price=price, attachments=[att])
    return "已画好，图片已经显示在对话里，用户看得到。不用描述画面，简短说一句就行。"


async def generate(bot: dict, chat_id: int, private_chat_id: int, prompt: str, post) -> str:
    """generate_image 工具。bot：谁在画；chat_id：图片发到哪个对话；private_chat_id：确认卡片发到哪（Bot 的私聊）。
    post 是 Router.add：(role, content, chat_id, **meta)。"""
    prompt = (prompt or "").strip()[:1000]
    if not prompt:
        return "没画：描述是空的"
    m = await avatars.current()
    if not m:
        return NO_MODEL
    price = prices.image_price(m["provider"], m["model"])
    if not approvals.decide(TOOL)[0]:
        try:
            return await _make(chat_id, prompt, m, post)
        except ValueError as e:
            return f"没画成：{e}"

    # 冒充一个任务弹确认卡片（和 Router._direct 一样）；卡片总在 Bot 的私聊里，群里、讨论串里有提示
    fake = SimpleNamespace(id=f"对话·{bot['name']}", bot_id=bot["id"], chat_id=chat_id, approval_chat_id=private_chat_id,
                           goal=i18n.t("在对话里画图"), background=False, waiting_approval=None)
    shown = {"描述": prompt, "模型": f"{m['model']} · {m['title']}", "价格": price_text(price)}

    async def wait_and_draw():
        ok, _ = await approvals.request(fake, TOOL, shown)
        if not ok:
            return
        try:
            await _make(chat_id, prompt, m, post)
        except Exception as e:
            await post("task", i18n.t("[没画成：{error}]", error=e), chat_id, kind="note")

    t = asyncio.create_task(wait_and_draw())
    _running.add(t)
    t.add_done_callback(_running.discard)
    return (f"已弹出确认卡片（模型 {m['model']}，{price_text(price)}），用户同意后会自动画好、显示在对话里，你不用再回来。"
            "简短告诉用户确认一下就行，不要说已经画好。")
