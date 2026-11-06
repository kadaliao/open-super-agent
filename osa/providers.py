"""模型服务：一个兼容 OpenAI 的接口地址，加一个密钥。

config.yaml 的 providers 是默认值；在 设置 → 模型 里添加或改过的，存在 settings 表的 providers 一行，
密钥存进保险库（LLM_KEY_<名字>）。用密钥时先看保险库，没有再看 config.yaml 里写的环境变量。
"""
import base64
import copy
import ipaddress
import json
import os
import re
import struct
import time
import zlib
from urllib.parse import urlparse
import openai
from openai import AsyncOpenAI
from . import i18n, vault
from .config import CFG
from .memory import conn

BASE = copy.deepcopy(CFG.get("providers") or {})   # config.yaml 里写的
MODELS_TTL = 600

# 添加时可以选的服务商。key_url：去哪里拿密钥；embedding：推荐的 embedding 模型（没有就是这家不提供）；
# models：推荐的对话模型，前面的优先，第一次打开的引导里默认选服务列出来的第一个（都要能看图，2026-11 抄自 models.dev）
PRESETS = [
    {"id": "deepseek", "title": "DeepSeek", "base_url": "https://api.deepseek.com", "color": "#4d6bfe",
     "key_url": "https://platform.deepseek.com/api_keys", "models": ["deepseek-flash", "deepseek-v4-flash"],
     "desc": i18n.N_("便宜，中文好；默认配置用它")},
    {"id": "openai", "title": "OpenAI", "base_url": "https://api.openai.com/v1", "color": "#111111",
     "key_url": "https://platform.openai.com/api-keys", "embedding": "text-embedding-3-small", "models": ["gpt-6.1-terra", "gpt-6-sol", "gpt-5.6-terra"],
     "desc": i18n.N_("GPT 系列")},
    {"id": "openrouter", "title": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "color": "#6467f2",
     "key_url": "https://openrouter.ai/settings/keys", "models": ["deepseek/deepseek-v4.1-flash", "google/gemini-3.8-flash", "openai/gpt-6.1-terra"],
     "desc": i18n.N_("一个密钥用几百个模型")},
    {"id": "siliconflow", "title": i18n.N_("硅基流动"), "base_url": "https://api.siliconflow.cn/v1", "color": "#7c3aed",
     "key_url": "https://cloud.siliconflow.cn/account/ak", "embedding": "BAAI/bge-m3", "models": ["zai-org/GLM-5.3-Flash", "Qwen/Qwen3.8-27B", "Pro/moonshotai/Kimi-K2.6"],
     "desc": i18n.N_("开源模型多，有免费的 embedding")},
    {"id": "dashscope", "title": i18n.N_("阿里云百炼"), "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "color": "#ff6a00",
     "key_url": "https://bailian.console.aliyun.com/?apiKey=1", "embedding": "text-embedding-v4", "models": ["qwen3.8-flash", "qwen3.7-plus", "qwen3.6-flash"],
     "desc": i18n.N_("通义千问")},
    {"id": "zhipu", "title": i18n.N_("智谱"), "base_url": "https://open.bigmodel.cn/api/paas/v4", "color": "#3859ff",
     "key_url": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys", "embedding": "embedding-3", "models": ["glm-5.3-flash", "glm-5v-turbo", "glm-4.6v"],
     "desc": i18n.N_("GLM 系列")},
    {"id": "moonshot", "title": "Kimi", "base_url": "https://api.moonshot.cn/v1", "color": "#16191e",
     "key_url": "https://platform.moonshot.cn/console/api-keys", "models": ["kimi-k3", "kimi-k2.6"],
     "desc": i18n.N_("月之暗面")},
    {"id": "ollama", "title": "Ollama", "base_url": "http://localhost:11434/v1", "color": "#5b5b63", "no_key": True,
     "embedding": "nomic-embed-text", "desc": i18n.N_("本机跑的模型，不用密钥"),
     "note": i18n.N_("用 Docker 部署时，地址写 http://host.docker.internal:11434/v1")},
    {"id": "custom", "title": i18n.N_("其他"), "base_url": "", "color": "#5b5b63",
     "desc": i18n.N_("任何兼容 OpenAI 的接口：vLLM、LM Studio、one-api……")},
]
PRESET = {p["id"]: p for p in PRESETS}
_PRESET_TITLES = {p["title"] for p in PRESETS}


def presets() -> list[dict]:
    """给界面的服务商列表：名字、说明按界面语言翻好（新的副本，不改 PRESETS）。"""
    return [p | {k: i18n.t(p[k]) for k in ("title", "desc", "note") if p.get(k)} for p in PRESETS]


def _title(t: str) -> str:
    """服务商的中文名（硅基流动……）按界面语言显示；用户自己起的名字原样。"""
    return i18n.t(t) if t in _PRESET_TITLES else t


def _stored_title(t: str) -> str:
    """界面上填回来的是翻好的名字（SiliconFlow）：存原文，换回中文界面时还能显示成“硅基流动”。"""
    return next((zh for zh in _PRESET_TITLES if t == i18n.t(zh, in_lang="en")), t)


_clients: dict[tuple, AsyncOpenAI] = {}
_models: dict[str, dict] = {}   # 名字 → {ts, models, error, note}


def _load() -> dict:
    r = conn.execute("select value from settings where key='providers'").fetchone()
    return json.loads(r[0]) if r else {}


def _store(d: dict):
    conn.execute("insert or replace into settings values('providers', ?)", (json.dumps(d),))
    conn.commit()


def apply():
    """config.yaml 里的加上界面里添加、改过的，放进 CFG['providers']。"""
    merged = {n: {**p, "source": "config"} for n, p in copy.deepcopy(BASE).items()}
    for n, p in _load().items():
        if n in merged:
            merged[n].update({k: v for k, v in p.items() if k in ("title", "base_url", "preset")})
        else:
            merged[n] = {**p, "source": "ui"}
    CFG["providers"] = merged
    _clients.clear()


# ---------- 密钥 ----------
def key_ref(name: str) -> str:
    return "LLM_KEY_" + re.sub(r"\W", "_", name.upper())


def api_key(name: str) -> str:
    v = vault.get(key_ref(name))
    if v:
        return v
    env = (CFG["providers"].get(name) or {}).get("api_key_env")
    return os.environ.get(env, "") if env else ""


def _local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def needs_key(name: str) -> bool:
    p = CFG["providers"].get(name) or {}
    return not (PRESET.get(p.get("preset") or "", {}).get("no_key") or _local(p.get("base_url") or ""))


def exists(name: str) -> bool:
    return name in CFG["providers"]


def usable(name: str) -> bool:
    return name in CFG["providers"] and (bool(api_key(name)) or not needs_key(name))


def _hint(k: str) -> str:
    return "••••" + k[-4:] if len(k) >= 12 else "••••"


def describe(name: str) -> dict:
    p = CFG["providers"][name]
    in_vault = bool(vault.get(key_ref(name)))
    env = p.get("api_key_env") or ""
    from_env = not in_vault and bool(env and os.environ.get(env))
    k = api_key(name)
    m = _models.get(name) or {}
    return {
        "name": name, "title": _title(p.get("title") or PRESET.get(p.get("preset") or name, {}).get("title") or name),
        "preset": p.get("preset") or (name if name in PRESET else "custom"), "base_url": p["base_url"],
        "source": p.get("source", "config"), "key_env": env,
        "key_source": "vault" if in_vault else "env" if from_env else "",
        "key_hint": _hint(k) if k else "", "needs_key": needs_key(name),
        "models": len(m.get("models") or []), "error": m.get("error", ""), "checked": m.get("ts", 0),
    }


def public() -> list[dict]:
    return [describe(n) for n in CFG["providers"]]


# ---------- 客户端 ----------
def client(name: str, timeout: float | None = None) -> AsyncOpenAI:
    p = CFG["providers"].get(name)
    if not p:
        raise ValueError(i18n.t("没有这个模型服务：{name}", name=name))
    k = (p["base_url"], api_key(name) or "none", timeout)
    if k not in _clients:
        _clients[k] = AsyncOpenAI(base_url=k[0], api_key=k[1], **({"timeout": timeout} if timeout else {}))
    return _clients[k]


def explain(e: Exception, base_url: str = "") -> str:
    """把接口报错翻成一句人话。"""
    detail = ""
    if isinstance(e, openai.APIStatusError):
        body = e.body if isinstance(e.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        detail = str((err or {}).get("message") or e.message or "")[:200]
    if isinstance(e, openai.AuthenticationError):
        return i18n.t("密钥不对，或者已经失效（401）")
    if isinstance(e, openai.PermissionDeniedError):
        return i18n.t("没有权限（403）：可能没开通这个模型，或者账户欠费。{detail}", detail=detail)
    if isinstance(e, openai.NotFoundError):
        return i18n.t("找不到（404）：接口地址或模型名字不对。{detail}", detail=detail)
    if isinstance(e, openai.RateLimitError):
        return i18n.t("请求太多或余额不足（429）。{detail}", detail=detail)
    if isinstance(e, openai.APITimeoutError):
        return i18n.t("连接超时：{url}", url=base_url)
    if isinstance(e, openai.APIConnectionError):
        return i18n.t("连不上 {url}", url=base_url)
    if isinstance(e, openai.APIStatusError):
        return i18n.t("出错了（{code}）：{detail}", code=e.status_code, detail=detail)
    return f"{type(e).__name__}: {str(e)[:200]}"


# ---------- 模型列表 ----------
async def _list(base_url: str, key: str) -> list[str]:
    c = AsyncOpenAI(base_url=base_url, api_key=key or "none", timeout=15, max_retries=0)
    page = await c.models.list()
    return sorted({m.id for m in page.data if getattr(m, "id", None)})


async def check(base_url: str, key: str) -> dict:
    """拉一次模型列表，顺便验证地址和密钥。有的服务不提供列表（404），这时只能自己填模型名字。"""
    try:
        models = await _list(base_url, key)
        return {"ok": True, "models": models, "error": "", "note": "" if models else i18n.t("连上了，但是没有列出模型，选模型时直接填名字")}
    except openai.NotFoundError:
        return {"ok": True, "models": [], "error": "", "note": i18n.t("这个服务不提供模型列表，选模型时直接填名字")}
    except openai.AuthenticationError as e:
        return {"ok": False, "models": [], "error": explain(e) if key else i18n.t("要填 API Key 才能用"), "note": ""}
    except Exception as e:
        return {"ok": False, "models": [], "error": explain(e, base_url), "note": ""}


async def models(name: str, refresh: bool = False) -> dict:
    """这个服务有哪些模型，带上单价和能力。服务自己不列的，用 models.dev 里这一家的列表。"""
    from . import prices
    if name not in CFG["providers"]:
        raise ValueError(i18n.t("没有这个模型服务：{name}", name=name))
    m = _models.get(name)
    if refresh or not m or time.time() - m["ts"] > MODELS_TTL:
        if needs_key(name) and not api_key(name):
            m = {"ok": False, "models": [], "error": i18n.t("还没填 API Key"), "note": ""}
        else:
            m = await check(CFG["providers"][name]["base_url"], api_key(name))
        if m["ok"] and not m["models"] and (known := prices.models_of(name)):
            m.update(models=known, note=i18n.t("这个服务没有列出模型，下面是 models.dev 收录的，也可以直接填名字"))
        m["ts"] = time.time()
        _models[name] = m
    return {**m, "name": name, "items": prices.annotate(name, m["models"])}


# ---------- 增删改 ----------
def _clean_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", url):
        raise ValueError(i18n.t("接口地址要以 http:// 或 https:// 开头"))
    return url


def _new_name(base: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "", base.lower()) or "custom"
    if not base[0].isalpha():
        base = "p" + base
    base, n, name = base[:28], 2, base[:28]
    while name in CFG["providers"]:
        name, n = f"{base}_{n}", n + 1
    return name


def save(name: str | None, data: dict) -> str:
    """name 为空是新建。api_key 留空表示不改；clear_key 删掉界面里填的密钥（回到环境变量）。"""
    d = _load()
    title = _stored_title((data.get("title") or "").strip()[:40])
    url = _clean_url(data.get("base_url", ""))
    preset = data.get("preset") if data.get("preset") in PRESET else None
    if name is None:
        name = _new_name(preset if preset and preset != "custom" else title or "custom")
        d[name] = {"title": title or PRESET.get(preset or "", {}).get("title") or name, "base_url": url, "preset": preset or "custom"}
    else:
        if name not in CFG["providers"]:
            raise ValueError(i18n.t("没有这个模型服务：{name}", name=name))
        d.setdefault(name, {}).update({"base_url": url, **({"title": title} if title else {})})
    key = (data.get("api_key") or "").strip()
    if key:
        vault.put(key_ref(name), key)
    elif data.get("clear_key"):
        vault.delete(key_ref(name))
    _store(d)
    apply()
    _models.pop(name, None)
    return name


def users(name: str) -> list[str]:
    """哪些用途在用这个服务。"""
    out = [r for r, v in CFG["roles"].items() if v.get("provider") == name]
    if (CFG.get("embedding") or {}).get("provider") == name:
        out.append("embedding")
    return out


def delete(name: str, role_names: dict):
    if name not in CFG["providers"]:
        raise ValueError(i18n.t("没有这个模型服务：{name}", name=name))
    if CFG["providers"][name].get("source") == "config":
        raise ValueError(i18n.t("这个是 config.yaml 里写的，要在那里删"))
    if u := users(name):
        raise ValueError(i18n.t("{roles}还在用它，先换成别的模型", roles=i18n.t("、").join(i18n.t(role_names.get(x, x)) for x in u)))
    d = _load()
    d.pop(name, None)
    _store(d)
    vault.delete(key_ref(name))
    _models.pop(name, None)
    apply()


# ---------- 测试一个模型 ----------
def _png(w: int, h: int, rgb: tuple) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


RED = "data:image/png;base64," + base64.b64encode(_png(64, 64, (220, 30, 30))).decode()


async def test(provider: str, model: str, kind: str = "chat", role: str = "") -> dict:
    """真的调一次：chat 先带一张红色图片问颜色，看能不能看图；不支持图片再只发文字。embedding 算一条向量。"""
    from . import memory
    if provider not in CFG["providers"]:
        raise ValueError(i18n.t("没有这个模型服务：{name}", name=provider))
    if not (model or "").strip():
        raise ValueError(i18n.t("先选一个模型"))
    base = CFG["providers"][provider]["base_url"]
    c = AsyncOpenAI(base_url=base, api_key=api_key(provider) or "none", timeout=60, max_retries=0)
    t0 = time.time()
    ms = lambda: int((time.time() - t0) * 1000)
    if kind == "embedding":
        try:
            r = await c.embeddings.create(model=model, input=["测试一下"])
            return {"ok": True, "dim": len(r.data[0].embedding), "ms": ms()}
        except Exception as e:
            return {"ok": False, "error": explain(e, base), "ms": ms()}

    from .config import call_kwargs
    kw = call_kwargs(role) if role in CFG["roles"] else {}   # 按这个用途的设置（思考模式等）来测

    async def ask(content) -> str:
        r = await c.chat.completions.create(model=model, max_tokens=512, messages=[{"role": "user", "content": content}], **kw)
        memory.record_usage("test", model, r.usage, provider=provider)
        msg = r.choices[0].message if r.choices else None
        return ((msg.content if msg else "") or getattr(msg, "reasoning_content", "") or "").strip()

    try:
        reply = await ask([{"type": "text", "text": "这张图片是什么颜色？只回答颜色。"}, {"type": "image_url", "image_url": {"url": RED}}])
        seen = "红" in reply or "red" in reply.lower()
        if not seen:   # 请求没报错，但没认出颜色：可能是服务把图片丢掉了，参考 models.dev 的记录
            from . import prices
            seen = {False: "no", True: "unsure"}.get(prices.lookup(provider, model).get("vision"), "unsure")
        return {"ok": True, "vision": "yes" if seen is True else seen, "reply": reply[:80], "ms": ms()}
    except (openai.AuthenticationError, openai.PermissionDeniedError, openai.APIConnectionError, openai.RateLimitError) as e:
        return {"ok": False, "error": explain(e, base), "ms": ms()}
    except Exception as e:
        img_err = explain(e, base)
    try:
        t0 = time.time()
        reply = await ask("回复“好”这一个字。")
        return {"ok": True, "vision": "no", "vision_error": img_err, "reply": reply[:80], "ms": ms()}
    except Exception as e:
        return {"ok": False, "error": explain(e, base), "ms": ms()}
