"""内置工具的注册：网页、资料库、小应用数据、按意思检索、发文件、画图、定时任务。

每个工具只在这里写一次，Router 和 Worker 都从注册表（registry.py）找；在哪些场合给模型用、要不要走确认规则，都在 Tool 上写明。
电脑工具、Router 自己的几个、botadmin 还没迁过来（见 registry.py 开头）。
"""
from . import i18n, images, library, media, registry, routines, search, tools
from .registry import APPLET, ROUTER, WORKER, Call, Tool

ctx = registry.Context("builtin")
_loaded = False


def _not_draft(env: dict) -> bool:
    # 对话里随时能用；后台任务只有正式的（不是 Heavy 的草稿）才能存页面、发文件
    return env.get("scope") == ROUTER or bool(env.get("pages"))


async def _send_file(call: Call) -> str:
    if len(call.files) >= media.MAX_SEND:
        return f"没发：一次最多发 {media.MAX_SEND} 个文件" if call.scope == ROUTER else f"没发：一个任务最多发 {media.MAX_SEND} 个文件"
    try:
        att = await media.from_computer(str(call.args.get("path") or ""))
    except Exception as e:
        return f"没发：{e}"
    call.files.append(att)
    text = media.sent_text(att)
    return text if call.scope == ROUTER else text.replace("对话里", "任务结果下面")


async def _save_page(call: Call) -> str:
    out = library.tool_save_page(call.bot_id, call.args, call.task.id if call.task else None)
    await registry.emit("pages_changed")
    return out


async def _generate_image(call: Call) -> str:
    return await images.generate(call.bot, call.chat_id, call.private_chat_id, str(call.args.get("prompt") or ""), call.post)


def load():
    """注册全部内置工具（只做一次）。顺序就是给模型的顺序，别随手调换：换了模型服务的前缀缓存就不命中了。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    web = frozenset({WORKER, APPLET})
    ctx.simple("web_search", tools.schema("web_search", "搜索网页，返回标题、链接和摘要", {"query": "搜索词"}), tools.web_search,
               where=web, gated=True, describe="搜索网页，返回标题、链接和摘要",
               progress=lambda a: i18n.t("搜索：{what}", what=str(a.get("query") or "")[:60]))
    ctx.simple("fetch_url", tools.schema("fetch_url", "抓取网页正文（截断到 8000 字）", {"url": "网址"}), tools.fetch_url,
               where=web, gated=True, describe="抓取网页正文（截断到 8000 字）",
               progress=lambda a: i18n.t("读取网页：{what}", what=str(a.get("url") or "")[:60]))
    ctx.tool(Tool("save_page", library.SAVE_PAGE, _save_page, when=_not_draft,
                  progress=lambda a: i18n.t("保存页面：{title}", title=str(a.get("title", ""))[:40])))
    ctx.tool(Tool("read_page", library.READ_PAGE, lambda c: _sync(library.tool_read_page(c.args)),
                  progress=lambda a: i18n.t("读页面 #{id}", id=a.get("page_id"))))
    ctx.tool(Tool("page_data", library.PAGE_DATA, lambda c: library.tool_page_data(c.bot_id, c.args),
                  progress=lambda a: i18n.t("小应用 #{id} 的数据", id=a.get("page_id"))))
    ctx.tool(Tool("send_file", media.SEND_FILE, _send_file, when=_not_draft,
                  progress=lambda a: i18n.t("发文件：{path}", path=str(a.get("path", ""))[:60])))
    ctx.tool(Tool("generate_image", images.GENERATE_IMAGE, _generate_image, where=frozenset({ROUTER})))
    ctx.tool(Tool("search_memory", search.SEARCH_TOOL, lambda c: search.tool(c.bot_id, c.args),
                  progress=lambda a: i18n.t("查记忆和资料库：{query}", query=str(a.get("query", ""))[:40])))
    # 定时任务：只在对话里用，只管自己的（Worker 跑的就是定时任务，不让它自己改）
    router = frozenset({ROUTER})
    ctx.tool(Tool("create_routine", routines.CREATE_ROUTINE, lambda c: routines.tool_create(c.bot_id, c.args), where=router))
    ctx.tool(Tool("list_routines", routines.LIST_ROUTINES, lambda c: routines.tool_list(c.bot_id, c.args), where=router))
    ctx.tool(Tool("update_routine", routines.UPDATE_ROUTINE, lambda c: routines.tool_update(c.bot_id, c.args), where=router))
    ctx.tool(Tool("delete_routine", routines.DELETE_ROUTINE, lambda c: routines.tool_delete(c.bot_id, c.args), where=router))


async def _sync(v):
    return v


load()
