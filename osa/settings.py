"""界面上可修改的设置，存在数据库里，覆盖 config.yaml 的同名项。模型服务（地址、密钥）在 providers.py。"""
import json
import time
from . import i18n, prices, providers
from .config import CFG
from .memory import conn

EDITABLE_LIMITS = ["daily_token_budget", "worker_max_steps", "task_timeout_s", "approval_timeout_s"]
ROLE_NAMES = {"router": i18n.N_("对话"), "worker": i18n.N_("后台任务"), "reviewer": i18n.N_("Heavy 汇总"),
              "filter": i18n.N_("整理和过滤"), "embedding": i18n.N_("按意思检索")}


def _load() -> dict:
    r = conn.execute("select value from settings where key='overrides'").fetchone()
    return json.loads(r[0]) if r else {}


def apply():
    providers.apply()
    o = _load()
    for role, v in o.get("roles", {}).items():
        if role in CFG["roles"]:
            CFG["roles"][role].update({k: v[k] for k in ("provider", "model", "thinking", "reasoning_effort") if k in v})
    for k, v in o.get("limits", {}).items():
        if k in EDITABLE_LIMITS:
            CFG["limits"][k] = v
    if "heavy_workers" in o:
        CFG["heavy"]["workers"] = o["heavy_workers"]
    if "computer_parallel" in o:
        CFG.setdefault("computer", {})["parallel"] = o["computer_parallel"]
    if "embedding" in o:
        CFG["embedding"] = o["embedding"]


def problem() -> str:
    """对话或后台任务用的模型服务没法用时，说一句为什么；能用返回空。"""
    for role in ("router", "worker"):
        name = CFG["roles"][role]["provider"]
        if name not in CFG["providers"]:
            return i18n.t("{role}用的模型服务 {name} 不存在了", role=i18n.t(ROLE_NAMES[role]), name=name)
        if not providers.usable(name):
            return i18n.t("{name} 还没填 API Key", name=providers.describe(name)["title"])
    return ""


def onboarding() -> bool:
    """要不要显示第一次打开时的引导：没走完、没跳过，而且还没跟 Bot 说过话（老用户升级上来不打扰）。"""
    if conn.execute("select 1 from settings where key='onboarding'").fetchone():
        return False
    return not conn.execute("select 1 from messages where role='user' limit 1").fetchone()


def finish_onboarding():
    conn.execute("insert or replace into settings values('onboarding', ?)", (json.dumps({"done": time.time()}),))
    conn.commit()


def public() -> dict:
    emb = CFG.get("embedding") or {}
    used = [(v["provider"], v["model"]) for v in CFG["roles"].values()]
    if emb.get("provider") and emb.get("model"):
        used.append((emb["provider"], emb["model"]))
    return {
        "providers": providers.public(),
        "presets": providers.presets(),
        "roles": CFG["roles"],
        "limits": {k: CFG["limits"].get(k) for k in EDITABLE_LIMITS},
        "heavy_workers": CFG["heavy"]["workers"],
        "computer_parallel": (CFG.get("computer") or {}).get("parallel", 3),
        "embedding": {"provider": emb.get("provider") or "", "model": emb.get("model") or ""},
        "model_info": {f"{p}/{m}": prices.lookup(p, m) for p, m in used},
        "prices": prices.info(),
        "problem": problem(),
        "language": i18n.lang(),
    }


def save(data: dict) -> dict:
    if "language" in data:
        i18n.set_lang(data["language"])
    o = _load()
    if "roles" in data:
        o.setdefault("roles", {})
        for role, v in data["roles"].items():
            if role not in CFG["roles"]:
                continue
            v = {k: v[k] for k in ("provider", "model", "thinking", "reasoning_effort") if k in v}
            if "provider" in v and v["provider"] not in CFG["providers"]:
                raise ValueError(i18n.t("没有这个模型服务：{name}", name=v["provider"]))
            if "model" in v:
                v["model"] = (v["model"] or "").strip()[:200]
                if not v["model"]:
                    raise ValueError(i18n.t("模型名字不能为空"))
            o["roles"].setdefault(role, {}).update(v)
    if "limits" in data:
        o.setdefault("limits", {}).update({k: int(v) for k, v in data["limits"].items() if k in EDITABLE_LIMITS})
    if "heavy_workers" in data:
        o["heavy_workers"] = max(1, min(8, int(data["heavy_workers"])))
    if "computer_parallel" in data:
        o["computer_parallel"] = max(1, min(8, int(data["computer_parallel"])))
    if "embedding" in data:
        e = data["embedding"] or {}
        provider, model = (e.get("provider") or "").strip(), (e.get("model") or "").strip()[:200]
        if provider and provider not in CFG["providers"]:
            raise ValueError(i18n.t("没有这个模型服务：{name}", name=provider))
        if provider and not model:
            raise ValueError(i18n.t("选了服务就要填 embedding 模型的名字"))
        o["embedding"] = {"provider": provider, "model": model}
    conn.execute("insert or replace into settings values('overrides', ?)", (json.dumps(o),))
    conn.commit()
    apply()
    if "embedding" in data:
        from . import search
        search._wake.set()   # 换了模型：后台重新算向量
    return public()
