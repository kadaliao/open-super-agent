"""内置给插件用的服务（registry.provide，插件在 META 的 inject 里写要用的）。

- llm：调这台服务配好的模型。算进当天的 token 预算，费用记在“插件”名下（观测页看得到是哪个插件花的）。
- messenger：以某个 Bot 的名义往对话里发一条消息（RSS 有新文章、监控报警这类主动推送）。
  消息标着来自哪个插件；给模型的上下文里是一条“系统通知”，里面的文字过一遍 defang，网页、邮件里的原文冒充不了系统。

插件拿到的是 bind(插件名) 之后的那份，所以记账和标记来源不用插件自己写。
"""
import time
from . import memory, providers
from .config import CFG, call_kwargs, client_for

MAX_POST = 20_000


class LLM:
    """await llm.complete("一句话" 或 messages, system=None, role="worker") -> 文字"""

    def __init__(self, plugin: str = ""):
        self.plugin = plugin

    def bind(self, plugin: str) -> "LLM":
        return LLM(plugin)

    async def complete(self, prompt, system: str | None = None, role: str = "worker") -> str:
        if role not in ("router", "worker"):
            raise ValueError("role 只能是 router 或 worker（用哪项工作配的模型）")
        if memory.tokens_today() > CFG["limits"]["daily_token_budget"]:
            raise RuntimeError("今日 token 预算已用完")
        msgs = prompt if isinstance(prompt, list) else [{"role": "user", "content": str(prompt)}]
        if system:
            msgs = [{"role": "system", "content": system}, *msgs]
        client, model = client_for(role)
        provider = CFG["roles"][role]["provider"]
        tag = f"plugin:{self.plugin}" if self.plugin else "plugin"
        t0, text, usage = time.monotonic(), "", None
        try:
            stream = await client.chat.completions.create(model=model, messages=msgs, stream=True,
                                                          stream_options={"include_usage": True}, **call_kwargs(role))
            async for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                if chunk.choices and chunk.choices[0].delta.content:
                    text += chunk.choices[0].delta.content
        except Exception as e:
            memory.record_error(tag, model, providers.brief(e), provider=provider, ms=(time.monotonic() - t0) * 1000)
            raise
        memory.record_usage(tag, model, usage, provider=provider, ms=(time.monotonic() - t0) * 1000)
        return text


class Messenger:
    """await messenger.post(文字, bot=None, chat_id=None) -> 消息 id。
    bot 是 Bot 的 id 或名字，不给就是主 Bot；chat_id 不给就是这个 Bot 的私聊（给的话 Bot 要在那个对话里）。"""

    def __init__(self, plugin: str = ""):
        self.plugin = plugin

    def bind(self, plugin: str) -> "Messenger":
        return Messenger(plugin)

    async def post(self, text: str, bot: int | str | None = None, chat_id: int | None = None) -> int:
        from . import bots, router
        b = bots.primary() if bot is None else bots.get(bot) if isinstance(bot, int) else bots.by_name(str(bot))
        if not b:
            raise LookupError(f"没有 Bot {bot}")
        cid = chat_id or b["chat_id"]
        chat = bots.chat(cid)
        if not chat or b["id"] not in chat["bot_ids"]:
            raise LookupError(f"{b['name']} 不在对话 #{cid} 里")
        text = str(text or "").strip()[:MAX_POST]
        if not text:
            raise ValueError("消息不能为空")
        return await router.runtime(b["id"]).add("task", text, cid, kind="plugin", plugin=self.plugin)


def register(ctx):
    ctx.provide("llm", LLM(), "调这台服务配好的模型（花钱，算进每天的预算）")
    ctx.provide("messenger", Messenger(), "以 Bot 的名义往对话里发消息")
