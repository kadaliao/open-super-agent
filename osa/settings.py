"""界面上可修改的设置，存在数据库里，覆盖 config.yaml 的同名项。模型服务（地址、密钥）在 providers.py。"""
import json
import os
import time
from . import avatars, computer, i18n, prices, providers, security
from .config import CFG
from .memory import conn

EDITABLE_LIMITS = ["daily_token_budget", "worker_max_steps", "task_timeout_s", "approval_timeout_s"]
_PC = dict(CFG.get("computer") or {})   # config.yaml 里原来的电脑设置：界面上选回“自动”时用
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
    pc = CFG.setdefault("computer", {})
    b = o.get("computer_backend")
    if b == "host" and not security.local_exec():   # 比如从别处恢复的备份：服务器锁下不认
        b = None
    pc["backend"] = b or _PC.get("backend", "auto")
    pc["host_shell"] = o.get("computer_host_shell", bool(_PC.get("host_shell", False)))
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


def _info(provider: str, model: str) -> dict:
    """单价和能力；能不能看图以设置里测出来的为准（见 media.can_see）。"""
    from . import media
    out = prices.lookup(provider, model)
    if (seen := media.tested(provider, model)) is not None:
        out["vision"] = seen
    return out


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
        "computer_backend": (CFG.get("computer") or {}).get("backend", "auto"),
        "computer_backends": computer.available(),
        "computer_on_desktop": bool(os.environ.get("OSA_DESKTOP")),   # 服务是桌面 App 自带的：“本机”就是用户面前这台电脑
        "computer_host_shell": bool((CFG.get("computer") or {}).get("host_shell")),
        "embedding": {"provider": emb.get("provider") or "", "model": emb.get("model") or ""},
        "image_model": avatars.choice(),   # 画图用哪个模型（对话里画图、生成头像共用），没选过是空的
        "model_info": {f"{p}/{m}": _info(p, m) for p, m in used},
        "prices": prices.info(),
        "problem": problem(),
        "language": i18n.lang(),
        "security": security.public(),   # 服务器锁、演示模式：界面上藏掉用不了的
    }


def save(data: dict) -> dict:
    if security.demo() and ({"limits", "heavy_workers", "computer_parallel"} & data.keys()):
        raise ValueError(i18n.t("演示服务器上不能改用量上限"))
    if not security.local_exec() and (data.get("computer_backend") == "host" or data.get("computer_host_shell")):
        raise ValueError(security.no_local_exec())
    if security.demo() and data.get("computer_backend") == "linked":
        raise ValueError(i18n.t("演示服务器上不能借用你的电脑"))
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
    if "computer_backend" in data:
        b = data["computer_backend"] or "auto"
        if b != "auto" and b not in computer.BACKENDS:
            raise ValueError(i18n.t("没有这种电脑：{name}", name=b))
        c = computer.get()
        if (c.users or c.exclusive) and b != CFG["computer"].get("backend"):
            raise ValueError(i18n.t("电脑正在被用着（{who}），等用完再换", who=c.busy()))
        if b == "auto":
            o.pop("computer_backend", None)
        else:
            o["computer_backend"] = b
    if "computer_host_shell" in data:
        o["computer_host_shell"] = bool(data["computer_host_shell"])
    if "embedding" in data:
        e = data["embedding"] or {}
        provider, model = (e.get("provider") or "").strip(), (e.get("model") or "").strip()[:200]
        if provider and provider not in CFG["providers"]:
            raise ValueError(i18n.t("没有这个模型服务：{name}", name=provider))
        if provider and not model:
            raise ValueError(i18n.t("选了服务就要填 embedding 模型的名字"))
        o["embedding"] = {"provider": provider, "model": model}
    if "image_model" in data:
        e = data["image_model"] or {}
        provider, model = (e.get("provider") or "").strip(), (e.get("model") or "").strip()[:200]
        if provider and provider not in CFG["providers"]:
            raise ValueError(i18n.t("没有这个模型服务：{name}", name=provider))
        if provider and not avatars.IMAGE_MODEL.search(model):
            raise ValueError(i18n.t("{model} 不是画图的模型", model=model))
        avatars.choose(provider, model)
    conn.execute("insert or replace into settings values('overrides', ?)", (json.dumps(o),))
    conn.commit()
    apply()
    if "embedding" in data:
        from . import search
        search._wake.set()   # 换了模型：后台重新算向量
    return public()
