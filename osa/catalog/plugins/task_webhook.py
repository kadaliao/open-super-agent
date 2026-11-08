"""任务做完推到群机器人：飞书、钉钉、企业微信、Slack、Discord 的 webhook，或者任意接收 JSON 的地址。

每个后台任务（包括定时任务）做完或失败时，把“哪个 Bot、做了什么、结果开头一段”发过去。
设置里填 url（建议存进保险库，写成 vault:名字），kind 选格式：feishu、dingtalk、wecom、slack、discord、json。
only_failed 为 true 时只推失败的。钉钉机器人开了“加签”的不支持，用“自定义关键词”就行。
"""
import httpx

META = {"title": "任务完成推送到群机器人", "version": "1.0", "author": "open-super-agent",
        "config": {"url": "", "kind": "feishu", "only_failed": False}}
KINDS = ("feishu", "dingtalk", "wecom", "slack", "discord", "json")
MAX_TEXT = 1500


def body(kind: str, text: str, task: dict) -> dict:
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if kind in ("dingtalk", "wecom"):
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "slack":
        return {"text": text}
    if kind == "discord":
        return {"content": text[:2000]}
    return {"text": text, "task": task}


def message(task: dict, bot: dict, result: str) -> str:
    ok = task.get("status") == "done"
    head = f"{'✅' if ok else '❌'} {bot.get('name', 'Bot')} 的任务 #{task.get('id')} {'完成' if ok else '没完成'}：{task.get('goal', '')[:80]}"
    result = (result or "").strip()
    return head + ("\n\n" + (result[:MAX_TEXT] + ("…" if len(result) > MAX_TEXT else "")) if result else "")


def apply(ctx):
    url = ctx.secret(ctx.config.get("url"))
    kind = ctx.config.get("kind") or "json"
    if not url or not str(url).startswith(("https://", "http://")):
        raise ValueError("先在设置里填 url（webhook 地址，可以写 vault:名字 从保险库取）")
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {' / '.join(KINDS)}")

    async def on_done(task: dict, bot: dict, result: str, **_):
        if ctx.config.get("only_failed") and task.get("status") == "done":
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:   # 一个任务推一次，不常用，不留着连接
                r = await client.post(url, json=body(kind, message(task, bot, result), task))
            r.raise_for_status()
        except httpx.HTTPError as e:
            ctx.log.warning("推送任务 #%s 失败：%s", task.get("id"), e)

    ctx.on("task_done", on_done)
