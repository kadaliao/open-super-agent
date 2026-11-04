"""Worker 的电脑工具。所有 Bot 共用一台电脑，一个任务用的时候其他任务排队；动作之后自动附上新截图，模型马上能看到结果。"""
import contextvars
import base64
from . import ComputerError, get

AUTO_SHOT = {"computer_open_url", "computer_click", "computer_type", "computer_key", "computer_scroll"}


def _fn(name, desc, props: dict, required: list[str] | None = None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required if required is not None else list(props)}}}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
SCHEMAS = [
    _fn("computer_screenshot", "截取电脑屏幕，返回图片（附带实际分辨率）。看不清当前状态时用。", {}),
    _fn("computer_open_url", "在电脑的浏览器里打开网址（会新开一个标签页）", {"url": S}),
    _fn("computer_click", "在屏幕坐标 (x, y) 点击。坐标以截图像素为准。",
        {"x": I, "y": I, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "double": B}, ["x", "y"]),
    _fn("computer_type", "在当前焦点处输入文字（先点击输入框）", {"text": S}),
    _fn("computer_key", "按键或组合键，例如 Enter、Tab、ctrl+l、ctrl+a、Escape", {"keys": S}),
    _fn("computer_scroll", "滚动鼠标所在位置的页面",
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
- 操作网页：computer_open_url 打开 → 看截图 → computer_click 点击（坐标取截图像素）→ computer_type 输入 → computer_key 回车。
  点击、输入、按键、滚动、打开网址后会自动附上新截图，不用再单独截图。
- 只是查资料时，优先用 web_search / fetch_url，更快更省；需要登录、交互、看页面实际效果时才用浏览器。
- 下载文件、处理数据、跑脚本用 computer_shell。
- 碰到登录、验证码等只能人做的步骤，调用 ask_human；提交、付款、发送等有后果的操作前，调用 ask_approval。"""


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
        await c.claim(CLAIM.get() or str(task.id), f"任务 #{task.id}", notify_progress)
    labels = {
        "computer_screenshot": "截图", "computer_open_url": f"打开 {args.get('url', '')}",
        "computer_click": f"点击 ({args.get('x')}, {args.get('y')})", "computer_type": f"输入 “{str(args.get('text', ''))[:30]}”",
        "computer_key": f"按键 {args.get('keys')}", "computer_scroll": f"滚动 {args.get('direction')}",
        "computer_shell": f"终端：{str(args.get('cmd', ''))[:60]}", "computer_read_file": f"读取 {args.get('path')}",
        "computer_write_file": f"写入 {args.get('path')}", "ask_human": "请你帮忙：" + str(args.get("reason", ""))[:60],
        "ask_approval": "请求确认：" + str(args.get("action", ""))[:60],
    }
    await notify_progress(labels.get(name, name))
    try:
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
