"""模型单价和能力：自动从 models.dev 获取，每天更新一次，存一份在数据目录（离线时用上一次的）。

models.dev 收录了两百多家服务商的模型，每家带接口地址，按地址就能和我们配的模型服务对上；
单价是美元 / 百万 token，按 pricing.usd_rate 换成人民币。config.yaml 的 pricing.models 里写了的模型以它为准
（DeepSeek 分高峰、空闲两档，models.dev 没有）。本机和内网的模型不收钱，按 0 计。
"""
import asyncio
import json
import re
import time
import httpx
from .config import CFG
from .memory import db_path

SOURCE = CFG["pricing"].get("source") or "https://models.dev/api.json"
FILE = db_path.parent / "model-prices.json"
TTL = 86400
# 按地址对不上时，按服务商类型找（OpenAI 官方在 models.dev 里没写地址）
ALIAS = {"openai": "openai", "deepseek": "deepseek", "openrouter": "openrouter", "dashscope": "alibaba-cn",
         "zhipu": "zhipuai", "moonshot": "moonshotai-cn", "siliconflow": "siliconflow-cn"}
# 自定义的服务（比如自己搭的转发）按模型名字到这几家找，前面的优先
PREFER = ["openai", "deepseek", "alibaba-cn", "zhipuai", "moonshotai-cn", "siliconflow-cn", "volcengine", "openrouter"]

_data: dict = {"ts": 0, "providers": {}}
status = {"error": "", "ts": 0.0}


def _rate() -> float:
    return float(CFG["pricing"].get("usd_rate") or (1 if CFG["pricing"].get("currency") in ("$", "USD") else 7.1))


def _norm(url: str) -> str:
    u = (url or "").lower().split("://", 1)[-1].rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def _compact(raw: dict) -> dict:
    out = {}
    for pid, p in raw.items():
        ms = {}
        for mid, m in (p.get("models") or {}).items():
            c = m.get("cost") or {}
            if c.get("input") is None and m.get("type") != "embedding":
                continue
            ms[mid] = {"i": c.get("input"), "o": c.get("output"), "c": c.get("cache_read"),
                       "v": int("image" in ((m.get("modalities") or {}).get("input") or [])), "t": m.get("type") or "", "n": m.get("name") or ""}
        out[pid] = {"api": p.get("api") or "", "name": p.get("name") or pid, "models": ms}
    return out


def _load_file():
    global _data
    try:
        _data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        pass


async def refresh() -> dict:
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(SOURCE)
            r.raise_for_status()
            data = {"ts": time.time(), "providers": _compact(r.json())}
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(FILE)
        _data.update(data)
        status.update(error="", ts=time.time())
    except Exception as e:
        status.update(error=f"{type(e).__name__}: {str(e)[:160]}", ts=time.time())
    return info()


async def loop():
    while True:
        if time.time() - _data.get("ts", 0) > TTL:
            await refresh()
        await asyncio.sleep(3600 if not status["error"] else 600)


def info() -> dict:
    return {"source": SOURCE, "updated": _data.get("ts", 0), "error": status["error"], "rate": _rate(),
            "currency": CFG["pricing"]["currency"], "models": sum(len(p["models"]) for p in _data["providers"].values())}


# ---------- 对上服务商和模型 ----------
_matched: dict[tuple, str | None] = {}


def match(provider: str | None) -> str | None:
    """我们的模型服务在 models.dev 里是哪一家。"""
    from .providers import _local
    p = (CFG["providers"].get(provider or "") or {})
    if not p or _local(p.get("base_url") or ""):
        return None
    k = (p["base_url"], p.get("preset") or provider, _data.get("ts"))
    if k not in _matched:
        url = _norm(p["base_url"])
        pid = next((pid for pid, x in _data["providers"].items() if x["api"] and _norm(x["api"]) == url), None)
        if not pid and "api.openai.com" in url:
            pid = "openai"
        if not pid:
            pid = ALIAS.get(p.get("preset") or provider or "")
            pid = pid if pid in _data["providers"] else None
        _matched[k] = pid
    return _matched[k]


def _find(provider: str | None, model: str) -> tuple[dict | None, str]:
    ps = _data["providers"]
    pid = match(provider)
    if pid:
        m = ps[pid]["models"].get(model)
        return (m, pid) if m else (None, "")
    # 对不上的（自己搭的转发之类）：按名字找，去掉 openai/ 这样的前缀也试一下
    names = [model] + ([model.split("/", 1)[1]] if "/" in model else [])
    for name in names:
        for p in PREFER + [x for x in ps if x not in PREFER]:
            m = ps.get(p, {}).get("models", {}).get(name)
            if m and m.get("i") is not None:
                return m, p
    return None, ""


def _manual(model: str):
    return CFG["pricing"]["models"].get(model)


def image_price(provider: str, model: str) -> float | None:
    """画一张图多少钱（pricing.images，单位和 currency 一样）；本机的算 0，没写的返回 None。"""
    from .providers import _local
    base = ((CFG["providers"].get(provider) or {}).get("base_url") or "").lower()
    if _local(base):
        return 0.0
    tail = model.rsplit("/", 1)[-1].lower()
    for host, table in (CFG["pricing"].get("images") or {}).items():
        if host.lower() not in base and host != provider:
            continue
        for name, price in (table or {}).items():
            n = str(name).lower()
            if n == model.lower() or ("/" not in n and n == tail):   # 写完整 id，或者只写最后一段
                return float(price)
    return None


def lookup(provider: str | None, model: str) -> dict:
    """界面上显示用：单价（元 / 百万 token）、能不能看图、是不是 embedding、单价从哪来。"""
    from .providers import _local
    p = CFG["providers"].get(provider or "") or {}
    out: dict = {"source": ""}
    m, pid = _find(provider, model)
    if m:
        r = _rate()
        out.update(vision=bool(m["v"]), kind=m["t"] or "chat", name=m["n"])
        if m.get("i") is not None:
            out.update(input=round(m["i"] * r, 4), output=round((m["o"] or 0) * r, 4),
                       cache=round(m["c"] * r, 4) if m.get("c") is not None else None, source="models.dev",
                       via="" if match(provider) else _data["providers"][pid]["name"])
    if (man := _manual(model)):
        out.update(input=man["input"][0], output=man["output"][0], cache=man["cache_hit"][0],
                   offpeak=[man["input"][1], man["output"][1]], source="config")
    if p and _local(p.get("base_url") or ""):
        out.update(input=0, output=0, cache=0, source="local")
    return out


def _guess_kind(mid: str) -> str:
    x = mid.lower()
    if re.search(r"embed|bge-|/bge|^bge|m3e|gte-|\be5-", x):
        return "embedding"
    if "rerank" in x:
        return "reranking"
    if re.search(r"tts|whisper|dall-e|gpt-image|sora|wanx|cogview|seedream|seedance|kolors|flux|stable-diffusion|asr|speech|audio", x):
        return "other"
    return "chat"


def annotate(provider: str, ids: list[str]) -> list[dict]:
    """模型列表加上单价、能不能看图、是对话还是 embedding，给选模型的下拉框用。"""
    from .providers import _local
    local = _local((CFG["providers"].get(provider) or {}).get("base_url") or "")
    r, out = _rate(), []
    for mid in ids:
        item = {"id": mid, "kind": _guess_kind(mid)}
        m, _ = (None, "") if local else _find(provider, mid)
        if m:
            item.update(vision=bool(m["v"]), kind=m["t"] if m["t"] in ("embedding", "chat", "reranking") else ("other" if m["t"] else "chat"))
            if m.get("i") is not None:
                item.update(input=round(m["i"] * r, 4), output=round((m["o"] or 0) * r, 4))
        if (man := _manual(mid)):
            item.update(input=man["input"][0], output=man["output"][0])
        if local:
            item.update(input=0, output=0)
        out.append(item)
    return out


def models_of(provider: str) -> list[str]:
    """服务本身不列模型时，用 models.dev 里这一家的模型列表顶上。"""
    pid = match(provider)
    return sorted(_data["providers"][pid]["models"]) if pid else []


def cost(provider: str | None, model: str, hit: int, miss: int, out: int, peak: bool) -> float:
    if (man := _manual(model)):
        k = 0 if peak else 1
        return (hit * man["cache_hit"][k] + miss * man["input"][k] + out * man["output"][k]) / 1e6
    from .providers import _local
    p = CFG["providers"].get(provider or "") or {}
    if p and _local(p.get("base_url") or ""):
        return 0.0
    m, _ = _find(provider, model)
    if not m or m.get("i") is None:
        return 0.0
    cache = m["c"] if m.get("c") is not None else m["i"]
    return (hit * cache + miss * m["i"] + out * (m["o"] or 0)) / 1e6 * _rate()


_load_file()
