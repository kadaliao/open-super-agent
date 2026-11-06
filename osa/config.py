import os
from pathlib import Path
import yaml
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / os.environ.get("OSA_CONFIG", "config.yaml")).read_text())


def client_for(role: str) -> tuple[AsyncOpenAI, str]:
    """返回某个角色（router/worker/reviewer/filter）对应的客户端和模型名。"""
    from . import providers
    r = CFG["roles"][role]
    return providers.client(r["provider"]), r["model"]


def call_kwargs(role: str) -> dict:
    """某个角色调用模型时的额外参数。config.yaml 里可以给角色加 thinking: false 或 reasoning_effort。"""
    r = CFG["roles"][role]
    kw = {}
    if r.get("thinking") is False:
        kw["extra_body"] = {"thinking": {"type": "disabled"}}
    if r.get("reasoning_effort"):
        kw["reasoning_effort"] = r["reasoning_effort"]
    return kw
