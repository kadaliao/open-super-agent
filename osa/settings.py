"""界面上可修改的设置，存在数据库里，覆盖 config.yaml 的同名项。"""
import json
import os
from .config import CFG
from .memory import conn

EDITABLE_LIMITS = ["daily_token_budget", "worker_max_steps", "task_timeout_s", "approval_timeout_s"]


def _load() -> dict:
    r = conn.execute("select value from settings where key='overrides'").fetchone()
    return json.loads(r[0]) if r else {}


def apply():
    o = _load()
    for role, v in o.get("roles", {}).items():
        if role in CFG["roles"]:
            CFG["roles"][role].update({k: v[k] for k in ("provider", "model", "thinking", "reasoning_effort") if k in v})
    for k, v in o.get("limits", {}).items():
        if k in EDITABLE_LIMITS:
            CFG["limits"][k] = v
    if "heavy_workers" in o:
        CFG["heavy"]["workers"] = o["heavy_workers"]


def public() -> dict:
    return {
        "providers": [{"name": n, "base_url": p["base_url"], "key_env": p["api_key_env"],
                       "key_set": bool(os.environ.get(p["api_key_env"]))} for n, p in CFG["providers"].items()],
        "roles": CFG["roles"],
        "limits": {k: CFG["limits"].get(k) for k in EDITABLE_LIMITS},
        "heavy_workers": CFG["heavy"]["workers"],
        "known_models": sorted(CFG["pricing"]["models"]),
    }


def save(data: dict) -> dict:
    o = _load()
    if "roles" in data:
        o.setdefault("roles", {})
        for role, v in data["roles"].items():
            if role not in CFG["roles"]:
                continue
            if v.get("provider") and v["provider"] not in CFG["providers"]:
                raise ValueError(f"没有这个模型提供方：{v['provider']}")
            o["roles"].setdefault(role, {}).update(v)
    if "limits" in data:
        o.setdefault("limits", {}).update({k: int(v) for k, v in data["limits"].items() if k in EDITABLE_LIMITS})
    if "heavy_workers" in data:
        o["heavy_workers"] = max(1, min(8, int(data["heavy_workers"])))
    conn.execute("insert or replace into settings values('overrides', ?)", (json.dumps(o),))
    conn.commit()
    apply()
    return public()
