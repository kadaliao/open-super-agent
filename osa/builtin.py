"""内置工具的注册：网页、资料库、小应用数据、按意思检索、技能、发文件、画图、生成视频、定时任务和事件、在沙箱里跑代码；
内置的事件来源（osa/sources/）。

每个工具只在这里写一次，Router 和 Worker 都从注册表（registry.py）找；在哪些场合给模型用、要不要走确认规则，都在 Tool 上写明。
电脑工具、Router 自己的几个、botadmin 还没迁过来（见 registry.py 开头）。
"""
from . import i18n, images, library, media, registry, routines, sandbox, search, skills, tools, videos
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


async def _generate_video(call: Call) -> str:
    return await videos.generate(call.bot, call.chat_id, call.private_chat_id, call.args, call.post)


async def _save_page(call: Call) -> str:
    out = library.tool_save_page(call.bot_id, call.args, call.task.id if call.task else None)
    await registry.emit("pages_changed")
    return out


async def _search_posts(call: Call) -> str:
    a = call.args
    return await tools.search_posts(str(a.get("platform") or ""), str(a.get("query") or ""), a.get("days"), a.get("count"),
                                    browser=bool(call.env.get("use_pc")))


async def _generate_image(call: Call) -> str:
    return await images.generate(call.bot, call.chat_id, call.private_chat_id, str(call.args.get("prompt") or ""), call.post,
                                 call.args.get("images"))


WEB_SEARCH = {"type": "function", "function": {
    "name": "web_search",
    "description": "搜索网页，返回标题、链接、日期和摘要。要新闻、近况、最近的讨论时填 days（只要最近几天的）。"
                   "搜索词里可以写 site:域名 只搜某个网站。查资料用这个，不要在浏览器里打开搜索引擎的网站去搜",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "搜索词"},
        "days": {"type": "integer", "description": "可选：只要最近几天的结果，比如 7、30"}},
        "required": ["query"]}}}

SEARCH_POSTS = {"type": "function", "function": {
    "name": "search_posts",
    "description": "搜社交平台上的帖子，看大家怎么说（口碑、讨论、最近的反应）：返回作者、日期、点赞评论转发数、链接和正文开头。"
                   "platform 是 x（推特）、reddit、xiaohongshu（小红书）之一。要最近的填 days。按条收费的平台别反复搜同一个词",
    "parameters": {"type": "object", "properties": {
        "platform": {"type": "string", "description": "x、reddit 或 xiaohongshu"},
        "query": {"type": "string", "description": "搜索词。X 的搜索词可以用 lang:zh、from:用户名、-is:reply、has:links 这类写法（min_faves 这类网页上的写法接口不认）"},
        "days": {"type": "integer", "description": "可选：只要最近几天的，比如 7、30"},
        "count": {"type": "integer", "description": "可选：要几条，默认 10"}},
        "required": ["platform", "query"]}}}


def load():
    """注册全部内置工具（只做一次）。顺序就是给模型的顺序，别随手调换：换了模型服务的前缀缓存就不命中了。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    web = frozenset({WORKER, APPLET})
    # parallel：只读的工具，Worker 一轮里要了好几个时一起跑。搜索只给 2：垫底的 DuckDuckGo 一下子来太多会限流
    ctx.simple("web_search", WEB_SEARCH, tools.web_search,
               where=web, gated=True, parallel=2, describe="搜索网页，返回标题、链接、日期和摘要；能只要最近几天的（days）",
               progress=lambda a: i18n.t("搜索：{what}", what=str(a.get("query") or "")[:60]))
    ctx.tool(Tool("search_posts", SEARCH_POSTS, _search_posts, where=web, gated=True, parallel=2,
               describe="搜 X、Reddit、小红书上的帖子，看大家怎么说（带点赞、评论数）；能只要最近几天的",
                  progress=lambda a: i18n.t("搜 {platform}：{what}", platform=str(a.get("platform") or ""), what=str(a.get("query") or "")[:60])))
    ctx.simple("fetch_url", tools.schema("fetch_url", "读网页正文（截断到 8000 字）。要跑脚本才显示内容的网页一般也能读", {"url": "网址"}),
               tools.fetch_url, where=web, gated=True, parallel=3, describe="读网页正文（截断到 8000 字）",
               progress=lambda a: i18n.t("读取网页：{what}", what=str(a.get("url") or "")[:60]))
    ctx.tool(Tool("save_page", library.SAVE_PAGE, _save_page, when=_not_draft,
                  progress=lambda a: i18n.t("保存页面：{title}", title=str(a.get("title", ""))[:40])))
    ctx.tool(Tool("read_page", library.READ_PAGE, lambda c: _sync(library.tool_read_page(c.args)), parallel=3,
                  progress=lambda a: i18n.t("读页面 #{id}", id=a.get("page_id"))))
    ctx.tool(Tool("page_data", library.PAGE_DATA, lambda c: library.tool_page_data(c.bot_id, c.args),
                  progress=lambda a: i18n.t("小应用 #{id} 的数据", id=a.get("page_id"))))
    ctx.tool(Tool("send_file", media.SEND_FILE, _send_file, when=_not_draft,
                  progress=lambda a: i18n.t("发文件：{path}", path=str(a.get("path", ""))[:60])))
    ctx.tool(Tool("generate_image", images.GENERATE_IMAGE, _generate_image, where=frozenset({ROUTER})))
    ctx.tool(Tool("generate_video", videos.GENERATE_VIDEO, _generate_video, where=frozenset({ROUTER})))
    ctx.tool(Tool("search_memory", search.SEARCH_TOOL, lambda c: search.tool(c.bot_id, c.args), parallel=3,
                  progress=lambda a: i18n.t("查记忆和资料库：{query}", query=str(a.get("query", ""))[:40])))
    # 技能：Bot 自己取做法和带的文件（用户 /名字 调用时做法已经附在消息里了）。只读，不用确认
    ctx.tool(Tool("use_skill", skills.USE_SKILL, lambda c: _sync(skills.tool_use(c.bot_id, c.scope, c.args)), parallel=3,
                  describe="按技能名取技能的做法和它带的文件（脚本、参考资料）",
                  progress=lambda a: i18n.t("看技能 /{name}", name=str(a.get("name", "")).lstrip("/")[:40])))
    # 定时任务和事件：只在对话里用（Worker 跑的就是它们，不让它自己改）。自己的随便管，主 Bot 管所有 Bot 的
    router = frozenset({ROUTER})
    ctx.tool(Tool("create_routine", routines.CREATE_ROUTINE, lambda c: routines.tool_create(c.bot, c.args), where=router))
    ctx.tool(Tool("create_event", routines.CREATE_EVENT, lambda c: routines.tool_create_event(c.bot, c.args), where=router))
    ctx.tool(Tool("list_routines", routines.LIST_ROUTINES, lambda c: routines.tool_list(c.bot, c.args), where=router))
    ctx.tool(Tool("update_routine", routines.UPDATE_ROUTINE, lambda c: routines.tool_update(c.bot, c.args), where=router))
    ctx.tool(Tool("delete_routine", routines.DELETE_ROUTINE, lambda c: routines.tool_delete(c.bot, c.args), where=router))
    # 跑代码：在沙箱里（电脑里，或者本机的受限子进程），所以不走确认。Router 那个只跑几秒的 Python、不联网，算数换单位用
    ctx.tool(Tool("run_code", sandbox.RUN_CODE, sandbox.tool_run, where=frozenset({WORKER}), when=lambda env: bool(sandbox.place()),
                  progress=sandbox.progress, describe="在沙箱里跑 Python / bash / node 代码：算数、处理表格和数据、画图表、转换格式"))
    ctx.tool(Tool("quick_code", sandbox.QUICK_CODE, sandbox.tool_quick, where=router, when=lambda env: bool(sandbox.place(quick=True))))
    from . import sources
    sources.register(ctx)


async def _sync(v):
    return v


load()
