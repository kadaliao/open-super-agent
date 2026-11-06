import os
from pathlib import Path
import yaml
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / os.environ.get("OSA_CONFIG", "config.yaml")).read_text())

# 数据目录：数据库、密钥、自己加的应用清单、工作目录都在这里，默认是仓库里的 data/。
# 桌面版的代码目录是只读的，用 OSA_DATA_DIR 指到用户自己的目录；这时相对路径的 memory.db 只取文件名，放进这个目录
DATA = Path(os.environ["OSA_DATA_DIR"]).expanduser() if os.environ.get("OSA_DATA_DIR") else ROOT / "data"


def _db_path() -> Path:
    db = Path(CFG["memory"]["db"])
    if db.is_absolute():
        return db
    return DATA / db.name if os.environ.get("OSA_DATA_DIR") else ROOT / db

DB_PATH = _db_path()


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
