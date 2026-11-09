"""Worker 的电脑工具。所有 Bot 共用一台电脑，几个任务同时用时各自一个浏览器窗口，满了排队；
computer_* 截的是自己窗口里的页面、按页面坐标点击，动作之后自动附上新截图，模型马上能看到结果。
本机电脑（host.py）的工具说明和提示词不一样：文件只在工作文件夹里，终端默认没有；
上传文件、写文件、提交、终端命令按确认规则先问用户（_confirm，规则见 approvals.decide）。
本机（或借来的 Mac）给了屏幕权限时还有 desktop_* 工具：看整个屏幕、用真实的鼠标键盘操作别的 App（_desktop，见 host.py）。"""
import asyncio
import contextvars
import base64
import re
import time
from .. import i18n
from . import ComputerError, get, macdesk
from .base import HOSTS

AUTO_SHOT = {"computer_click", "computer_type", "computer_key", "computer_scroll"}


BROWSER = {"browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_key",
           "browser_scroll", "browser_back", "browser_read", "browser_upload"}
# 要用浏览器窗口的工具：第一次用时占一个窗口（满了排队）；终端和读写文件不占
WINDOW = BROWSER | {"computer_screenshot", "computer_click", "computer_type", "computer_key", "computer_scroll", "ask_human"}


def page_text(d: dict) -> str:
    """把浏览器返回的页面信息排成给模型看的文字。"""
    if "elements" not in d:   # browser_read
        more = f"\n…（共 {d['total']} 字，用 offset={d['offset'] + len(d['text'])} 继续读）" \
            if d["offset"] + len(d["text"]) < d["total"] else ""
        return f"{d['title']} | {d['url']}\n{d['text']}{more}"
    els = "\n".join(d["elements"]) or "（没有可操作元素）"
    if d.get("dialog"):
        els = "（页面上有弹窗，下面只列弹窗里的元素；是登录、扫码或验证码就调用 ask_human）\n" + els
    return (f"当前页面：{d['title']} | {d['url']}（滚动 {d['scroll']}）\n可操作元素：\n{els}\n\n正文开头：{d['text'][:1200]}")


def _fn(name, desc, props: dict, required: list[str] | None = None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required if required is not None else list(props)}}}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
SCHEMAS = [
    _fn("browser_open", "在电脑的浏览器里打开网址（当前标签页），返回页面上可操作元素的编号列表和正文开头", {"url": S}),
    _fn("browser_snapshot", "查看浏览器当前页面：可操作元素（带编号 [n]）、正文开头、滚动位置。页面变了或不确定时用。", {}),
    _fn("browser_click", "点击页面元素，ref 是 browser_snapshot 里的编号", {"ref": I}),
    _fn("browser_type", "在输入框里输入文字（先清空原内容）。ref 是输入框编号；submit=true 时输入完按回车。",
        {"ref": I, "text": S, "submit": B}, ["ref", "text"]),
    _fn("browser_key", "在页面上按一个键：Enter、Tab、Escape、Backspace、ArrowDown、ArrowUp、PageDown 等", {"key": S}),
    _fn("browser_scroll", "页面滚动一屏", {"direction": {"type": "string", "enum": ["up", "down"]}}, ["direction"]),
    _fn("browser_back", "浏览器后退", {}),
    _fn("browser_read", "读取当前页面的完整正文（每次最多 12000 字，用 offset 往后翻）", {"offset": I}, []),
    _fn("browser_upload", "把电脑里的文件传到网页上：ref 是“上传 / 选择文件”按钮或文件输入框的编号，paths 是文件的完整路径",
        {"ref": I, "paths": {"type": "array", "items": S}}),
    _fn("computer_screenshot", "截取浏览器里当前页面的画面，返回图片。要看页面长什么样（图片、图表、布局、验证码、操作是否成功）时用；"
        "网页内容和元素用 browser_snapshot。save=true 时把截图存进电脑的工作文件夹（screenshots 子文件夹）并返回路径，"
        "要把网页截图发给用户就用 send_file 发这个文件。", {"save": B}, []),
    _fn("computer_click", "【备用】按截图上的坐标 (x, y) 点击页面。只在 browser_click 做不到时用（画布、没有编号的元素），"
        "坐标以 computer_screenshot 的截图为准。",
        {"x": I, "y": I, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "double": B}, ["x", "y"]),
    _fn("computer_type", "【备用】在页面当前焦点处输入文字。网页输入框用 browser_type", {"text": S}),
    _fn("computer_key", "在页面上按键或组合键，例如 Enter、Tab、ctrl+a、shift+Tab、Escape（浏览器自己的快捷键如新标签页不起作用）", {"keys": S}),
    _fn("computer_scroll", "【备用】在页面中间滚动鼠标滚轮。网页用 browser_scroll",
        {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "amount": I}, ["direction"]),
    _fn("computer_shell", "在电脑的终端执行 bash 命令（工作目录 /data/workspace，文件会保留）。适合下载、处理数据、运行脚本。",
        {"cmd": S, "timeout": I}, ["cmd"]),
    _fn("computer_read_file", "读取电脑里的文本文件", {"path": S}),
    _fn("computer_write_file", "在电脑里写入文本文件（会覆盖）", {"path": S, "content": S}),
    _fn("ask_human", "遇到只有人能完成的步骤（登录、验证码、扫码、支付验证、需要用户本人决定的选择）时调用："
        "用户会接手电脑处理，处理完交还给你后再继续。说明清楚需要用户做什么。", {"reason": S}),
    _fn("ask_approval", "在执行有后果的操作之前调用，例如提交表单、下单付款、发送消息或邮件、删除数据、发布内容。"
        "说明要做什么、影响是什么。用户同意后才能继续。", {"action": S}),
]
DESK_SCHEMAS = [
    _fn("desktop_screenshot", "截取用户这台 Mac 的整个屏幕（主显示器），返回图片，以及前台 App、屏幕上的窗口、鼠标位置。"
        "要操作浏览器以外的 App 时先用它看屏幕；desktop_click 等工具的坐标以这张截图为准。"
        "save=true 时把这张截图存进工作文件夹（screenshots 子文件夹），返回文件路径，要把截图发给用户就用 send_file 发这个文件。",
        {"save": B}, []),
    _fn("desktop_click", "按 desktop_screenshot 截图上的坐标点击屏幕（真实的鼠标，会把点到的窗口换到前面）",
        {"x": I, "y": I, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "double": B}, ["x", "y"]),
    _fn("desktop_drag", "按住鼠标左键从 (x, y) 拖到 (to_x, to_y)：拖文件、拖滑块、框选", {"x": I, "y": I, "to_x": I, "to_y": I}),
    _fn("desktop_type", "在屏幕上当前有焦点的地方输入文字（用粘贴输入，中文也行，不受输入法影响；不会按回车）。先点一下要输入的地方", {"text": S}),
    _fn("desktop_key", "按键或组合键，例如 enter、esc、tab、backspace、cmd+a、cmd+c、cmd+v、cmd+tab、cmd+space（聚焦搜索）、"
        "cmd+w（关窗口）、cmd+q（退出 App）、cmd+shift+g（在访达里前往文件夹）、up、down。"
        "要按顺序按几个键就用空格隔开，一次按完，例如 \"1 2 3 * 4 5 enter\"、\"down down enter\"", {"keys": S}),
    _fn("desktop_scroll", "把鼠标移到截图上的 (x, y) 再滚动",
        {"x": I, "y": I, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "amount": I}, ["x", "y", "direction"]),
    _fn("desktop_open_app", "打开一个 App，或者把它切到最前面（例如 微信、访达、备忘录、日历、Safari、系统设置），name 写 App 的名字", {"name": S}),
]
DESKTOP = {s["function"]["name"] for s in DESK_SCHEMAS}
NAMES = {s["function"]["name"] for s in SCHEMAS} | DESKTOP

_HOST_FILES = {
    "computer_read_file": "读取工作文件夹里的文本文件（路径可以写相对工作文件夹的）",
    "computer_write_file": "在工作文件夹里写入文本文件（会覆盖；路径可以写相对工作文件夹的）",
    "browser_upload": "把工作文件夹里的文件传到网页上：ref 是“上传 / 选择文件”按钮或文件输入框的编号，paths 是文件路径",
}


def desk_schemas(c=None) -> list[dict]:
    """操作整个屏幕的工具：只有本机（或借来的 Mac）是 Mac、给了两个权限才给。"""
    c = c or get()
    return DESK_SCHEMAS if c.backend in HOSTS and c.desktop_enabled() else []


def schemas(c=None) -> list[dict]:
    """给 Worker 的电脑工具。本机电脑：文件工具说明换成工作文件夹，终端没打开就不给 computer_shell，
    能操作整个屏幕时再加上 desktop_*。"""
    c = c or get()
    if c.backend not in HOSTS:
        return SCHEMAS
    out = []
    for s in SCHEMAS:
        f = s["function"]
        if f["name"] == "computer_shell":
            if not c.shell_enabled():
                continue
            s = _fn("computer_shell", f"在用户这台电脑上执行 shell 命令（工作目录是工作文件夹 {c.workspace}）。每条命令都要用户确认。",
                    {"cmd": S, "timeout": I}, ["cmd"])
        elif f["name"] in _HOST_FILES:
            s = {"type": "function", "function": {**f, "description": _HOST_FILES[f["name"]]}}
        out.append(s)
    return out + desk_schemas(c)


def _intro(c) -> str:
    if c.backend not in HOSTS:
        return """你有一台电脑（Linux + Chromium 浏览器 + 终端，和其他 Bot 共用）。你的任务有自己的浏览器窗口，
别的任务同时在别的窗口里做、互不干扰；登录状态和文件是大家共用的。用 browser_* / computer_* 工具操作："""
    shell = ("- 终端：computer_shell 在用户这台电脑上执行命令（工作目录是工作文件夹）。每条命令都要用户确认（用户“总是允许”过的命令开头除外，带 ; && | $( 重定向的命令每次都问），只在必要时用。"
             if c.shell_enabled() else "- 没有终端（用户没有打开）。下载文件用浏览器，读写文件用 computer_read_file / computer_write_file。")
    desk = DESK_HINT if c.desktop_enabled() else ""
    return f"""你有一台电脑：用户这台电脑上专门给 Bot 开的 Chrome 浏览器（和用户自己日常用的 Chrome 分开，所有 Bot 共用，
用户在里面登录过的网站你都能直接用）。你的任务有自己的浏览器窗口，别的任务同时在别的窗口里做、互不干扰。用 browser_* / computer_* 工具操作：
- 文件只能放在工作文件夹 {c.workspace} 里：computer_read_file / computer_write_file 只能读写这里面的文件（可以写相对路径），
  浏览器下载的文件在它的 downloads 子文件夹，browser_upload 也只能传这里面的文件。
{shell}
- 上传文件、写文件、点“发送 / 发布 / 提交 / 付款”这类按钮时，系统会自动弹确认卡片问用户，这些不用再调用 ask_approval；
  用户拒绝了就别再试，换个做法或者在结果里说明。{desk}"""


DESK_HINT = """
- 整台电脑：用户还允许你操作这台 Mac 上的其他 App（desktop_* 工具，用的是用户真实的屏幕、鼠标和键盘）。
  网页仍然优先用 browser_*（Bot 自己的 Chrome，又快又不占鼠标）；网页以外的事（微信等聊天软件、访达里的文件、备忘录、日历、
  系统设置、用户自己的浏览器……）才用 desktop_*：先 desktop_screenshot 看屏幕 → desktop_open_app 打开或切换 App →
  按截图坐标 desktop_click → desktop_type 输入 → desktop_key 按快捷键。每个动作之后会自动附上新的屏幕截图。
  第一次用会弹卡片请用户同意。鼠标键盘一次只能一个任务用，别的任务要等，所以用完尽快结束。
  用户一动鼠标或键盘你就会被暂停，用户交还后先重新 desktop_screenshot 再继续。
  在屏幕上点“发送 / 付款”这类按钮、在多行输入框里按回车时系统会自动弹确认卡片；别的有后果的操作（删除文件、改系统设置、
  在终端里执行命令）先用 ask_approval。不要操作 Open Super Agent 自己的窗口。屏幕锁着时用 ask_human 请用户解锁。
  要把屏幕截图给用户：desktop_screenshot(save=true) 存进工作文件夹，再 send_file；不要用系统的截屏快捷键或“截屏” App。"""


def system_hint(c=None) -> str:
    c = c or get()
    shell = "" if c.backend in HOSTS else "\n- 下载文件、处理数据、跑脚本用 computer_shell。"
    return _intro(c) + _BODY.replace("{shell}", shell)


def capability(c=None) -> str:
    """Router 的能力清单里那一句：电脑是什么、能做什么。"""
    c = c or get()
    if c.backend in HOSTS:
        return ("每个后台任务都能用用户这台电脑上专门给 Bot 开的 Chrome 浏览器（和用户自己的 Chrome 分开，所有 Bot 共用），"
                "可以打开网页、点击、输入、登录后操作、上传下载文件（文件放在工作文件夹）"
                + ("、在终端执行命令（每条都要用户确认）" if c.shell_enabled() else "") + "。几个任务可以同时用，各自一个浏览器窗口，"
                "登录状态共用；同时用的任务多了才排队。浏览器窗口就在用户的屏幕上，右栏也能看到画面。"
                "上传文件、写文件、点“发送 / 发布 / 付款”这类按钮时会自动弹确认卡片。"
                + ("任务还能看整个屏幕、用真实的鼠标键盘操作这台 Mac 上的其他 App（微信、访达、备忘录、"
                   "系统设置等，desktop_* 工具），每个任务第一次用会先问用户；鼠标键盘一次只能一个任务用。"
                   "你自己用 desktop_* 只做一两步的事（打开某个 App、看一眼屏幕）；要在 App 里按一串按钮、填东西、来回操作的，"
                   "派一个任务去做（你这一轮能调用的工具次数有限）。" if c.desktop_enabled() else ""))
    return ("每个后台任务都能用所有 Bot 共用的一台电脑（Linux 桌面 + Chromium 浏览器 + 终端），"
            "可以打开网页、点击、输入、登录后操作、上传下载文件、运行脚本。几个任务可以同时用，各自一个浏览器窗口，"
            "登录状态和文件共用；同时用的任务多了才排队。用户能在右栏实时看到屏幕。")


_BODY = """
- 操作网页优先用 browser_* 工具（直接控制浏览器，又快又准，不用看图估坐标）：
  browser_open 打开 → 返回的元素列表里找编号 → browser_click / browser_type（submit=true 回车提交）→ 结果里有新的元素列表。
  要看完整内容用 browser_read。元素编号每次返回都会刷新，用最新的。要往网页上传文件用 browser_upload。
- 需要看页面长什么样（图表、图片、布局、验证码、确认操作是否成功）时用 computer_screenshot（截的是你窗口里的页面）；
  browser_* 搞不定的（画布类页面、没有编号的元素、组合键）用 computer_click / computer_type / computer_key 按截图坐标操作，
  这些操作后会自动附上新截图。
- 只是查资料时，优先用 web_search / fetch_url，更快更省；需要登录、交互、看页面实际效果时才用浏览器。{shell}
- 看到验证码（滑块拼图、点选文字、图形验证、"环境异常"/"安全验证"页）、扫码登录、输密码登录、短信验证、
  人机校验时：**立刻**调用 ask_human，一次都不要自己尝试拖动或识别——这些就是为了拦自动程序设计的，试了只会失败、
  还可能让账号或 IP 被风控。ask_human 的 reason 写清楚要用户做什么（例如“拖动滑块完成拼图验证”）。
- 其他只能人做的步骤同样调用 ask_human；提交、付款、发送等有后果的操作前，调用 ask_approval。"""


# 当前 Worker 占用电脑用的标识（Heavy 模式下同一任务的几个 Worker 各不相同）
CLAIM: contextvars.ContextVar[str] = contextvars.ContextVar("osa_claim", default="")


async def release():
    key = CLAIM.get()
    if key:
        await get().release(key)


def _label(task, key: str) -> str:
    if isinstance(task.id, str):
        return task.id
    part = key.rpartition(":")[2]
    return i18n.t("任务 #{id}", id=task.id) + \
        (" · " + i18n.t("研究员 {n}", n=int(part[1:]) + 1) if part[:1] == "h" and part[1:].isdigit() else "")


def _progress_label(name: str, args: dict) -> str:
    """任务卡片和任务过程里显示的这一步在做什么。"""
    fixed = {"browser_snapshot": i18n.N_("查看页面"), "browser_back": i18n.N_("后退"), "browser_read": i18n.N_("读取页面"),
             "computer_screenshot": i18n.N_("截图")}
    if name in fixed:
        return i18n.t(fixed[name])
    if name == "browser_open":
        return i18n.t("打开 {url}", url=args.get("url", ""))
    if name == "browser_click":
        return i18n.t("点击 [{ref}]", ref=args.get("ref"))
    if name in ("browser_type", "computer_type"):
        return i18n.t("输入 “{text}”", text=str(args.get("text", ""))[:30])
    if name in ("browser_key", "computer_key"):
        return i18n.t("按键 {key}", key=args.get("key") if name == "browser_key" else args.get("keys"))
    if name in ("browser_scroll", "computer_scroll"):
        return i18n.t("滚动 {direction}", direction=args.get("direction"))
    if name == "browser_upload":
        return i18n.t("上传文件到 [{ref}]", ref=args.get("ref"))
    if name == "computer_click":
        return i18n.t("点击 ({x}, {y})", x=args.get("x"), y=args.get("y"))
    if name == "computer_shell":
        return i18n.t("终端：{cmd}", cmd=str(args.get("cmd", ""))[:60])
    if name == "computer_read_file":
        return i18n.t("读取 {path}", path=args.get("path"))
    if name == "computer_write_file":
        return i18n.t("写入 {path}", path=args.get("path"))
    if name == "desktop_screenshot":
        return i18n.t("看屏幕")
    if name == "desktop_click":
        return i18n.t("点击屏幕 ({x}, {y})", x=args.get("x"), y=args.get("y"))
    if name == "desktop_drag":
        return i18n.t("在屏幕上拖动")
    if name == "desktop_type":
        return i18n.t("输入 “{text}”", text=str(args.get("text", ""))[:30])
    if name == "desktop_key":
        return i18n.t("按键 {key}", key=args.get("keys"))
    if name == "desktop_scroll":
        return i18n.t("滚动 {direction}", direction=args.get("direction"))
    if name == "desktop_open_app":
        return i18n.t("打开 {app}", app=args.get("name", ""))
    if name == "ask_human":
        return i18n.t("请你帮忙：{reason}", reason=str(args.get("reason", ""))[:60])
    if name == "ask_approval":
        return i18n.t("请求确认：{action}", action=str(args.get("action", ""))[:60])
    return name


ENTER = {"enter", "return"}


async def _confirm(task, c, name: str, args: dict, key: str) -> str | None:
    """按确认规则先问用户：上传文件、写文件、终端命令，以及“提交”（点发送 / 发布 / 付款这类按钮、在发消息的输入框里回车，
    动作之前让浏览器看一眼要点的元素，见 cdp_helper.INSPECT_JS）。返回 None 表示可以做，否则是给模型的话。"""
    from .. import approvals
    if name == "browser_upload":
        tool, shown = name, {"要上传的文件": args.get("paths"), "上传到": f"[{args.get('ref')}]"}
    elif name == "computer_write_file":
        tool, shown = name, {"文件": args.get("path"), "内容开头": str(args.get("content") or "")[:500]}
    elif name == "computer_shell":
        tool, shown = name, {"命令": args.get("cmd")}
    else:
        mode = "click" if name in ("browser_click", "computer_click") else None
        if name == "browser_type" and args.get("submit") or name == "browser_key" and str(args.get("key") or "").lower() in ENTER \
                or name == "computer_key" and str(args.get("keys") or "").replace(" ", "").lower().rpartition("+")[2] in ENTER:
            mode = "enter"
        if not mode or not approvals.decide("browser_submit", c.backend)[0]:
            return None
        try:
            info = await c.browser("inspect", key=key, mode=mode, ref=args.get("ref"), x=args.get("x"), y=args.get("y"))
        except ComputerError:
            return None   # 看不了就照常做：元素找不到的话，动作本身会报错
        if not info.get("submit"):
            return None
        tool, shown = "browser_submit", {"操作": info.get("what"), "页面": f"{info.get('title') or ''} | {info.get('url') or ''}"}
    if not approvals.decide(tool, c.backend, args if tool != "browser_submit" else shown)[0]:
        return None
    ok, note = await approvals.request(task, tool, shown)   # 终端：卡片上能“总是允许这个命令前缀”（approvals.resolve）
    return None if ok else f"用户拒绝了这个操作，没有执行。{note}".strip()


async def call(task, name: str, args: dict, notify_progress, key: str = "") -> tuple[str, bytes | None]:
    """执行一个电脑工具，返回 (给模型的文字, 可选截图)。key 是谁在用：任务的 CLAIM，对话里直接操作的是 chat-<Bot id>。"""
    from .. import approvals
    c = get()
    key = key or CLAIM.get() or str(task.id)
    if name in DESKTOP:
        await notify_progress(_progress_label(name, args))
        try:
            return await _desktop(task, c, name, args, key, notify_progress)
        except ComputerError as e:
            return f"电脑操作失败：{e}", None
        except (TypeError, ValueError, KeyError) as e:
            return f"参数不对：{e}", None
    if name in WINDOW and not key.startswith("chat-"):
        await c.claim(key, _label(task, key), notify_progress,
                      {"task_id": task.id if isinstance(task.id, int) else None, "bot_id": task.bot_id})
    await notify_progress(_progress_label(name, args))
    try:
        if name == "browser_upload" and isinstance(args.get("paths"), str):
            args = {**args, "paths": [args["paths"]]}
        if name == "browser_upload":   # 先查文件在不在（本机电脑：在不在工作文件夹里），再问用户
            paths = c.upload_paths([str(x) for x in args.get("paths") or []][:10])
            if not paths:
                return "没有给要上传的文件路径", None
        if refused := await _confirm(task, c, name, args, key):
            return refused, None
        if name == "browser_upload":
            await c.browser("upload", key=key, ref=args.get("ref"), files=paths)
            return page_text(await c.browser("snapshot", key=key)), None
        if name in BROWSER:
            op = name.removeprefix("browser_")
            if op == "open" and not str(args.get("url", "")).startswith(("http://", "https://")):
                return "只能打开 http / https 网址", None
            kw = {k: v for k, v in args.items() if v is not None}
            if "key" in kw:   # browser_key 的按键名：key 这个名字 c.browser 自己要用（谁在操作）
                kw["name"] = kw.pop("key")
            return page_text(await c.browser(op, key=key, **kw)), None
        if name == "computer_screenshot":
            png = await c.page_shot(key)
            if not args.get("save"):
                return "这是浏览器里当前页面的截图。", png
            path = f"screenshots/网页 {time.strftime('%Y-%m-%d %H.%M.%S')}.png"
            await c.write_bytes(path if c.backend in HOSTS else f"{c.workspace}/{path}", png)
            return f"这是浏览器里当前页面的截图，已存到 {c.workspace}/{path}（发给用户用 send_file）。", png
        if name == "computer_click":
            button = args.get("button") or "left"
            if button not in ("left", "right", "middle"):
                return "button 只能是 left / right / middle", None
            await c.browser("click_xy", key=key, x=int(args["x"]), y=int(args["y"]), button=button, double=bool(args.get("double")))
            text = "已点击。"
        elif name == "computer_type":
            if not args.get("text"):
                return "没有要输入的文字", None
            await c.browser("type", key=key, text=str(args["text"]))
            text = "已输入。"
        elif name == "computer_key":
            await c.browser("press", key=key, keys=str(args.get("keys") or ""))
            text = "已按键。"
        elif name == "computer_scroll":
            await c.browser("wheel", key=key, direction=args.get("direction") or "down", amount=args.get("amount") or 3)
            text = "已滚动。"
        elif name == "computer_shell":
            return (await c.shell(args["cmd"], int(args.get("timeout") or 120))).text(), None
        elif name == "computer_read_file":
            data = await c.read_bytes(args["path"], 1 << 20)
            return data.decode(errors="replace")[:20000], None
        elif name == "computer_write_file":
            await c.write_bytes(args["path"], args["content"].encode())
            return f"已写入 {args['path']}（{len(args['content'])} 字）", None
        elif name == "ask_human":
            await c.start()
            # 把这个任务的窗口放到最前面，用户一接手就看到；用户正在帮别的任务时不打扰，等他点这张卡片的“接手”。
            # 本机电脑不抢：你可能正在用别的程序，点卡片上的“接手电脑”时才放到前面
            if c.controller == "bot" and c.backend not in HOSTS:
                try:
                    await c.front(key)
                except ComputerError:
                    pass
            c.asking[task.id] = key   # 从卡片上接手时，把这个（不是同一任务的别的研究员）的窗口放到前面
            try:
                ok, note = await approvals.request(task, "ask_human", {"需要你做的事": args["reason"]}, kind="help")
            finally:
                if c.asking.get(task.id) == key:
                    c.asking.pop(task.id, None)
            return (f"用户已处理完并交还电脑。{note}".strip() if ok else f"用户没有处理：{note}".strip()), None
        elif name == "ask_approval":
            ok, note = await approvals.request(task, "ask_approval", {"操作": args["action"]}, once=True)   # 每一步都是它自己判断要问的
            return (f"用户同意了。{note}".strip() if ok else f"用户拒绝了，不要执行这个操作。{note}".strip()), None
        else:
            return "未知的电脑工具", None
    except ComputerError as e:
        return f"电脑操作失败：{e}", None
    shot = await c.page_shot(key) if name in AUTO_SHOT else None
    return text, shot


class Shot(bytes):
    """带说明的截图（整个屏幕的截图和浏览器页面的截图，给模型的说明不一样）。"""
    caption = "当前页面截图"


def shot(png: bytes, caption: str) -> Shot:
    s = Shot(png)
    s.caption = caption
    return s


# 屏幕上点了就发出去、花钱的按钮（比浏览器里那份少了“删除、确认、回复”：App 里到处都是，比如计算器的退格键就叫“删除”）
DESK_SUBMIT = re.compile(r"发送|发布|发表|提交|支付|付款|购买|下单|结算|转账"
                         r"|\b(send|post|publish|submit|pay|buy|order|checkout|transfer|tweet)\b", re.I)
SETTLE = {"desktop_open_app": 1.5}   # 动作之后等一会儿再截图，界面才变过来


def _desk_text(d: dict) -> str:
    f = d["front"]
    wins = "；".join(f"{w['app']}" + (f" — {w['title']}" if w["title"] else "") for w in d["windows"]) or "无"
    return (f"屏幕截图 {d['width']}×{d['height']}（点击坐标按这个尺寸），鼠标在 ({d['cursor'][0]}, {d['cursor'][1]})。"
            f"前台：{f['app'] or '未知'}" + (f" — {f['title']}" if f["title"] else "") + f"。屏幕上的窗口（从前到后）：{wins}")


async def _desktop(task, c, name: str, args: dict, key: str, notify_progress) -> tuple[str, bytes | None]:
    """整个屏幕的工具：占住屏幕（一次一个任务）→ 这个任务第一次用先问用户 → 点“发送”类按钮、在输入框里回车先问 → 做 → 截图。"""
    from .. import approvals
    if c.backend not in HOSTS or not c.desktop_enabled():
        return "这台电脑现在不能操作整个屏幕（不是 Mac，或者还没给 Open Super Agent“辅助功能”和“屏幕录制”权限），只能用浏览器（browser_*）。", None
    ok_key = key if key.startswith("chat-") else f"task:{task.id}"   # 同意按任务记：Heavy 的几个研究员只问一次
    no = ("用户这次没同意你操作整个屏幕（在卡片上点了“不行”）。"
          "能用浏览器做的用 browser_*，做不了就在结果里说明，这个任务里别再试 desktop_*。")
    if time.time() - c.desk_denied.get(ok_key, 0) < 600:
        return no, None
    await c.desk_claim(key, _label(task, key), notify_progress)
    if not c.desk_allowed(ok_key):
        if approvals.decide("desktop", c.backend, {"任务": task.goal[:300]})[0]:
            ok, note = await approvals.request(task, "desktop", {"任务": task.goal[:300]})
            if not ok:
                c.desk_denied[ok_key] = time.time()
                await c.desk_release(key)
                return f"{no}{note}", None
        c.desk_ok[ok_key] = time.time()
    if name == "desktop_screenshot":
        d = await c.desk("screenshot", key)
        saved = ""
        if args.get("save"):
            path = f"screenshots/屏幕 {time.strftime('%Y-%m-%d %H.%M.%S')}.png"
            await c.write_bytes(path, d["png"])
            saved = f"截图已存到工作文件夹：{c.workspace}/{path}（发给用户用 send_file）。"
        return saved + _desk_text(d), shot(d["png"], "整个屏幕的截图")
    what = ""   # 不为空：这一步像是发出去（点“发送”、在输入框里回车），先问用户
    if name == "desktop_click":
        x, y = int(args["x"]), int(args["y"])
        button = args.get("button") or "left"
        if button not in ("left", "right", "middle"):
            return "button 只能是 left / right / middle", None
        el = await c.desk("inspect", key, x=x, y=y)
        label = el.get("label") or ""
        if button == "left" and len(label) <= 40 and DESK_SUBMIT.search(label):
            what = f"在「{el.get('app') or '?'}」里点击「{label}」"
        op, kw = "click", {"x": x, "y": y, "button": button, "double": bool(args.get("double"))}
        done = f"已点击 ({x}, {y})" + (f"「{label}」" if label else "") + "。"
    elif name == "desktop_drag":
        op, kw, done = "drag", {k: int(args[k]) for k in ("x", "y", "to_x", "to_y")}, "已拖动。"
    elif name == "desktop_type":
        if not args.get("text"):
            return "没有要输入的文字", None
        op, kw, done = "type", {"text": str(args["text"])[:20000]}, "已输入。"
    elif name == "desktop_key":
        keys = str(args.get("keys") or "")
        if any(macdesk.parse_keys(k)[1] in ("enter", "return") for k in keys.split()):
            f = await c.desk("focused", key)
            if f.get("role") == "AXTextArea":   # 多行输入框（聊天软件、终端）里按回车，多半是发出去、执行
                what = f"在「{f.get('app') or '?'}」的输入框里按 {keys}"
        op, kw, done = "key", {"keys": keys}, f"已按 {keys}。"
    elif name == "desktop_scroll":
        op, kw = "scroll", {"x": int(args["x"]), "y": int(args["y"]), "direction": args.get("direction") or "down",
                            "amount": args.get("amount") or 3}
        done = "已滚动。"
    elif name == "desktop_open_app":
        app = str(args.get("name") or "").strip()
        if not app:
            return "没有给 App 的名字", None
        op, kw, done = "open_app", {"name": app}, f"已打开 {app}。"
    else:
        return "未知的电脑工具", None
    if what and approvals.decide("desktop_submit", c.backend, {"操作": what})[0]:
        ok, note = await approvals.request(task, "desktop_submit", {"操作": what})
        if not ok:
            return f"用户拒绝了这个操作，没有执行。{note}".strip(), None
    await c.desk(op, key, **kw)
    await asyncio.sleep(SETTLE.get(name, 0.6))
    d = await c.desk("screenshot", key)
    return done + _desk_text(d), shot(d["png"], "整个屏幕的截图")


def image_message(png: bytes, caption: str | None = None, coords: bool = True) -> dict:
    caption = caption or getattr(png, "caption", "当前页面截图")
    w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")   # PNG 头里的宽高
    tail = "点击坐标按这个尺寸" if coords else "只用来看，不能按这张图的坐标点击"
    return {"role": "user", "content": [
        {"type": "text", "text": f"（系统附图：{caption}，{w}×{h}，{tail}）"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}


def prune_images(msgs: list[dict], keep: int = 2):
    """只保留最近 keep 张截图，更早的换成文字，避免上下文越来越长。"""
    seen = 0
    for m in reversed(msgs):
        if isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]):
            seen += 1
            if seen > keep:
                m["content"] = "（更早的截图已省略）"
