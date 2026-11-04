"""Worker 的电脑工具。所有 Bot 共用一台电脑，一个任务用的时候其他任务排队；动作之后自动附上新截图，模型马上能看到结果。"""
import contextvars
import base64
from . import ComputerError, get

AUTO_SHOT = {"computer_open_url", "computer_click", "computer_type", "computer_key", "computer_scroll"}


BROWSER = {"browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_key",
           "browser_scroll", "browser_back", "browser_read"}


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
    _fn("computer_screenshot", "截取电脑屏幕，返回图片。要看页面画面（图片、图表、布局、验证码）时用；网页内容和元素用 browser_snapshot。", {}),
    _fn("computer_click", "【备用】在屏幕坐标 (x, y) 点击。只在 browser_click 做不到时用（浏览器外的窗口、画布）。",
        {"x": I, "y": I, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "double": B}, ["x", "y"]),
    _fn("computer_type", "【备用】在当前焦点处输入文字。网页输入框用 browser_type", {"text": S}),
    _fn("computer_key", "按键或组合键，例如 Enter、Tab、ctrl+l、ctrl+a、Escape", {"keys": S}),
    _fn("computer_scroll", "【备用】滚动鼠标所在位置。网页用 browser_scroll",
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
NAMES = {s["function"]["name"] for s in SCHEMAS}

SYSTEM_HINT = """你有一台电脑（和其他 Bot 共用，你的任务用着时别人在排队，登录状态也是共享的）（Linux 桌面 + Chromium 浏览器 + 终端），用 computer_* 工具操作：
- 操作网页优先用 browser_* 工具（直接控制浏览器，又快又准，不用看图估坐标）：
  browser_open 打开 → 返回的元素列表里找编号 → browser_click / browser_type（submit=true 回车提交）→ 结果里有新的元素列表。
  要看完整内容用 browser_read。元素编号每次返回都会刷新，用最新的。
- 需要看页面长什么样（图表、图片、布局、验证码、确认操作是否成功）时用 computer_screenshot；
  browser_* 搞不定的（浏览器以外的窗口、文件对话框、画布类页面、组合键）用 computer_click / computer_type / computer_key，
  这些操作后会自动附上新截图。
- 只是查资料时，优先用 web_search / fetch_url，更快更省；需要登录、交互、看页面实际效果时才用浏览器。
- 下载文件、处理数据、跑脚本用 computer_shell。
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


async def call(task, name: str, args: dict, notify_progress) -> tuple[str, bytes | None]:
    """执行一个电脑工具，返回 (给模型的文字, 可选截图)。"""
    from .. import approvals
    c = get()
    if name not in ("ask_approval",):
        await c.claim(CLAIM.get() or str(task.id), task.id if isinstance(task.id, str) else f"任务 #{task.id}", notify_progress)
    labels = {
        "browser_open": f"打开 {args.get('url', '')}", "browser_snapshot": "查看页面",
        "browser_click": f"点击 [{args.get('ref')}]", "browser_type": f"输入 “{str(args.get('text', ''))[:30]}”",
        "browser_key": f"按键 {args.get('key')}", "browser_scroll": f"滚动 {args.get('direction')}",
        "browser_back": "后退", "browser_read": "读取页面",
        "computer_screenshot": "截图", "computer_open_url": f"打开 {args.get('url', '')}",
        "computer_click": f"点击 ({args.get('x')}, {args.get('y')})", "computer_type": f"输入 “{str(args.get('text', ''))[:30]}”",
        "computer_key": f"按键 {args.get('keys')}", "computer_scroll": f"滚动 {args.get('direction')}",
        "computer_shell": f"终端：{str(args.get('cmd', ''))[:60]}", "computer_read_file": f"读取 {args.get('path')}",
        "computer_write_file": f"写入 {args.get('path')}", "ask_human": "请你帮忙：" + str(args.get("reason", ""))[:60],
        "ask_approval": "请求确认：" + str(args.get("action", ""))[:60],
    }
    await notify_progress(labels.get(name, name))
    try:
        if name in BROWSER:
            op = name.removeprefix("browser_")
            if op == "open" and not str(args.get("url", "")).startswith(("http://", "https://")):
                return "只能打开 http / https 网址", None
            kw = {k: v for k, v in args.items() if v is not None}
            return page_text(await c.browser(op, **kw)), None
        if name == "computer_screenshot":
            return "这是当前屏幕截图。", await c.screenshot()
        if name == "computer_open_url":
            await c.open_url(args["url"])
            text = "已打开。"
        elif name == "computer_click":
            await c.click(args["x"], args["y"], args.get("button") or "left", bool(args.get("double")))
            text = "已点击。"
        elif name == "computer_type":
            await c.type(args["text"])
            text = "已输入。"
        elif name == "computer_key":
            await c.press(args["keys"])
            text = "已按键。"
        elif name == "computer_scroll":
            await c.scroll(args.get("direction", "down"), args.get("amount") or 3)
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
            ok, note = await approvals.request(task, "ask_human", {"需要你做的事": args["reason"]}, kind="help")
            return (f"用户已处理完并交还电脑。{note}".strip() if ok else f"用户没有处理：{note}".strip()), None
        elif name == "ask_approval":
            ok, note = await approvals.request(task, "ask_approval", {"操作": args["action"]})
            return (f"用户同意了。{note}".strip() if ok else f"用户拒绝了，不要执行这个操作。{note}".strip()), None
        else:
            return "未知的电脑工具", None
    except ComputerError as e:
        return f"电脑操作失败：{e}", None
    shot = await c.screenshot() if name in AUTO_SHOT else None
    return text, shot


def image_message(png: bytes, caption: str = "当前屏幕截图") -> dict:
    w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")   # PNG 头里的宽高
    return {"role": "user", "content": [
        {"type": "text", "text": f"（系统附图：{caption}，{w}×{h}，点击坐标按这个尺寸）"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}


def prune_images(msgs: list[dict], keep: int = 2):
    """只保留最近 keep 张截图，更早的换成文字，避免上下文越来越长。"""
    seen = 0
    for m in reversed(msgs):
        if isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]):
            seen += 1
            if seen > keep:
                m["content"] = "（更早的截图已省略）"
