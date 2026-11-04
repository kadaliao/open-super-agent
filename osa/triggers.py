"""自动触发（对应 DeepSeek 设计里的 Trigger）。

事件来源：定时（schedule）和外部 webhook。
事件先过规则过滤（include / exclude 关键词），再过 LLM 过滤（便宜模型判断是否值得处理），
通过的事件作为系统通知交给主 Bot，由 Router 决定回复、派任务还是忽略。
"""
import asyncio
import json
import time
from .config import CFG, call_kwargs, client_for
from . import memory

log: list[dict] = []   # 最近的触发记录，给前端看


def _record(name: str, verdict: str, detail: str):
    log.insert(0, {"ts": time.strftime("%m-%d %H:%M:%S"), "trigger": name, "verdict": verdict, "detail": detail[:120]})
    del log[50:]


def get(name: str) -> dict | None:
    return next((t for t in CFG.get("triggers", []) if t["name"] == name and t.get("enabled", True)), None)


def rule_filter(t: dict, text: str) -> str | None:
    """返回拒绝原因；通过返回 None。"""
    low = text.lower()
    if any(w.lower() in low for w in t.get("exclude", [])):
        return "命中排除词"
    inc = t.get("include", [])
    if inc and not any(w.lower() in low for w in inc):
        return "没有命中关键词"
    return None


async def llm_filter(t: dict, text: str) -> tuple[bool, str]:
    rule = t.get("llm_filter")
    if not rule:
        return True, "无 LLM 过滤"
    client, model = client_for("filter")
    resp = await client.chat.completions.create(model=model, messages=[
        {"role": "system", "content": "你是事件过滤器。根据规则判断事件是否需要处理。"
                                      '只输出 JSON：{"handle": true/false, "reason": "一句话"}'},
        {"role": "user", "content": f"规则：{rule}\n\n事件：\n{text[:6000]}"}],
        response_format={"type": "json_object"}, **call_kwargs("filter"))
    memory.record_usage("filter", model, resp.usage)
    try:
        d = json.loads(resp.choices[0].message.content)
        return bool(d.get("handle")), d.get("reason", "")
    except Exception:
        return True, "过滤结果解析失败，默认放行"


def _primary_runtime():
    from . import bots
    from .router import runtime
    return runtime(bots.primary()["id"])


async def fire(t: dict, event: str) -> dict:
    """处理一个事件，返回过滤结果。"""
    if reason := rule_filter(t, event):
        _record(t["name"], "规则拦截", reason)
        return {"accepted": False, "stage": "rule", "reason": reason}
    ok, reason = await llm_filter(t, event)
    if not ok:
        _record(t["name"], "LLM 拦截", reason)
        return {"accepted": False, "stage": "llm", "reason": reason}
    _record(t["name"], "已交给 Router", reason)
    text = t.get("prompt", "{event}").replace("{event}", event)
    asyncio.create_task(_primary_runtime().handle_event(t["name"], text))
    return {"accepted": True, "reason": reason}


def _due(t: dict, last: float, now: float) -> bool:
    if every := t.get("every"):
        return now - last >= every
    hm = time.strftime("%H:%M", time.localtime(now))
    return hm in t.get("at", []) and time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) != time.strftime("%Y-%m-%d %H:%M", time.localtime(now))


async def scheduler():
    """每 20 秒检查一次定时触发器。every 类型从启动时开始计时。"""
    last = {t["name"]: time.time() for t in CFG.get("triggers", [])}
    for t in CFG.get("triggers", []):
        if t.get("at"):
            last[t["name"]] = 0
    while True:
        await asyncio.sleep(20)
        now = time.time()
        for t in CFG.get("triggers", []):
            if t.get("type") == "schedule" and t.get("enabled", True) and _due(t, last[t["name"]], now):
                last[t["name"]] = now
                await fire(t, f"定时触发：{t['name']}，时间 {time.strftime('%Y-%m-%d %H:%M')}")
