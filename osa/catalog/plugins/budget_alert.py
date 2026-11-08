"""花费提醒：今天的 token 快用完、或者花的钱到了你定的数，以 Bot 的名义在它的对话里提醒你。

每 10 分钟看一次。用的数和别处一样：token 是“每日 token 预算”算的那个（最近 24 小时，用完了 Bot 就停下不干活），
钱是观测页上“今天”的花费（从今天零点起，按 config.yaml 里 pricing 的币种）。不调模型、不花钱，用内置的 messenger 服务发消息。
设置：bot（发给哪个 Bot，名字，不填是主 Bot）；warn_at_percent（到预算的百分之几提醒，默认 [80, 100]）；
daily_cost（每天花多少钱算多，比如 5，0 是不看钱）。每个线每天最多提醒一次，记在插件自己的数据目录里。
"""
import asyncio
import json
import time
from osa import memory
from osa.config import CFG

META = {"title": "花费提醒", "version": "1.0", "author": "open-super-agent", "inject": ["messenger"],
        "config": {"bot": "", "warn_at_percent": [80, 100], "daily_cost": 0}}
EVERY = 10 * 60


def usage() -> tuple[int, float]:
    """(最近 24 小时的 token, 今天零点以来的花费)：和每日预算、观测页的“今天”是同一套数。"""
    day0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    cost = memory.conn.execute("select coalesce(sum(cost),0) from llm_calls where ts >= ?", (day0,)).fetchone()[0]
    return memory.tokens_today(), float(cost)


def crossed(value: float, limit: float, percents: list, sent: set, kind: str) -> list[int]:
    """到了、但今天还没提醒过的线（从低到高）。limit 不大于 0 就是不看。"""
    if not limit or limit <= 0:
        return []
    return sorted(p for p in percents if value >= limit * p / 100 and f"{kind}:{p}" not in sent)


def _tokens(n: float) -> str:
    return f"{n / 10000:.1f} 万" if n >= 10000 else f"{n:.0f}"


def token_message(p: int, tokens: int, budget: int, cost: float, cur: str) -> str:
    used = f"{_tokens(tokens)} / {_tokens(budget)}"
    if p >= 100:
        return (f"⛔ 今天的 token 预算用完了（{used}，今天花了 {cur}{cost:.2f}）：Bot 们现在不接新活、后台任务会停下，"
                "等最近 24 小时的用量降下来自动恢复。急用的话去设置里调高“每日 token 预算”。")
    return (f"⚠️ 今天的 token 已经用了 {tokens / budget * 100:.0f}%（{used}，今天花了 {cur}{cost:.2f}）。"
            "用到 100% Bot 们就会停下，要么省着点用，要么去设置里调高“每日 token 预算”。")


def cost_message(p: int, cost: float, limit: float, cur: str) -> str:
    if p >= 100:
        return f"💸 今天已经花了 {cur}{cost:.2f}，超过了你定的每天 {cur}{limit:g}。只是提醒，Bot 不会因此停下。"
    return f"💸 今天已经花了 {cur}{cost:.2f}，到了你定的每天 {cur}{limit:g} 的 {cost / limit * 100:.0f}%。"


def _percents(v) -> list[int]:
    if not isinstance(v, list) or not v or not all(isinstance(x, (int, float)) and 0 < x <= 1000 for x in v):
        raise ValueError("warn_at_percent 要是百分比的列表，比如 [80, 100]")
    return sorted({int(x) for x in v})


async def check(ctx, tokens: int | None = None, cost: float | None = None, today: str | None = None) -> list[str]:
    """看一次，该提醒的发出去，返回发了哪些消息。tokens / cost / today 是测试时用的。"""
    if tokens is None or cost is None:
        tokens, cost = usage()
    today = today or time.strftime("%Y-%m-%d")
    f = ctx.data_dir / "sent.json"
    state = json.loads(f.read_text()) if f.exists() else {}
    sent = set(state.get("sent") or []) if state.get("day") == today else set()
    percents = _percents(ctx.config.get("warn_at_percent", [80, 100]))
    budget = int(CFG["limits"].get("daily_token_budget") or 0)
    limit = float(ctx.config.get("daily_cost") or 0)
    cur = CFG["pricing"].get("currency") or ""
    todo = []
    # 一下子跨过好几条线（比如从 50% 直接到 105%）只发最高的那条，低的一起记成提醒过
    if hit := crossed(tokens, budget, percents, sent, "tokens"):
        todo.append((token_message(hit[-1], tokens, budget, cost, cur), {f"tokens:{p}" for p in hit}))
    if hit := crossed(cost, limit, percents, sent, "cost"):
        todo.append((cost_message(hit[-1], cost, limit, cur), {f"cost:{p}" for p in hit}))
    out = []
    try:
        for text, keys in todo:   # 发成功了才记成提醒过：发不出去（Bot 名字写错了）下次再试
            await ctx.services["messenger"].post(text, bot=ctx.config.get("bot") or None)
            sent |= keys
            out.append(text)
    finally:
        if out or state.get("day") != today:
            ctx.data_dir.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"day": today, "sent": sorted(sent)}))
    return out


def apply(ctx):
    _percents(ctx.config.get("warn_at_percent", [80, 100]))
    try:
        if float(ctx.config.get("daily_cost") or 0) < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("daily_cost 要是一个数（每天花多少钱算多，0 是不看钱）") from None

    async def loop():
        await asyncio.sleep(30)   # 服务刚启动、刚启用时先等一会儿
        while True:
            try:
                await check(ctx)
            except Exception as e:
                ctx.log.warning("看花费出错：%s", e)
            await asyncio.sleep(EVERY)

    ctx.task(loop())
