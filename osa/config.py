import os
from pathlib import Path
import yaml
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / os.environ.get("OSA_CONFIG", "config.yaml")).read_text())

_clients: dict[str, AsyncOpenAI] = {}


def client_for(role: str) -> tuple[AsyncOpenAI, str]:
    """返回某个角色（router/worker/reviewer）对应的客户端和模型名。"""
    r = CFG["roles"][role]
    name = r["provider"]
    if name not in _clients:
        p = CFG["providers"][name]
        _clients[name] = AsyncOpenAI(base_url=p["base_url"], api_key=os.environ.get(p["api_key_env"], "none"))
    return _clients[name], r["model"]


def call_kwargs(role: str) -> dict:
    """某个角色调用模型时的额外参数。config.yaml 里可以给角色加 thinking: false 或 reasoning_effort。"""
    r = CFG["roles"][role]
    kw = {}
    if r.get("thinking") is False:
        kw["extra_body"] = {"thinking": {"type": "disabled"}}
    if r.get("reasoning_effort"):
        kw["reasoning_effort"] = r["reasoning_effort"]
    return kw
