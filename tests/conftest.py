"""测试环境：临时数据目录和配置、假模型、收集服务端发出的事件。

osa 的模块在导入时就按 config.yaml 打开数据库，所以环境变量要在导入 osa 之前设好。
测试只测框架（谁调了什么工具、消息进了哪个对话），不调真的模型，也不碰真的电脑。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="osa-test-"))

_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
_cfg["memory"]["db"] = str(TMP / "osa.db")
_cfg["computer"] = {**(_cfg.get("computer") or {}), "enabled": False, "backend": "local"}
_cfg["triggers"] = []
_cfg["mcp_servers"] = {}
_cfg["embedding"] = {"provider": "", "model": ""}
(TMP / "config.yaml").write_text(yaml.safe_dump(_cfg, allow_unicode=True))

os.environ["OSA_CONFIG"] = str(TMP / "config.yaml")
# 保险库主密钥不设：会在临时目录里新生成一个，碰不到真的保险库
for k in ("OSA_VAULT_KEY", "INSTABOX_API_KEY", "OSA_PASSWORD", "OSA_WEBHOOK_TOKEN", "DEEPSEEK_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from osa import bots, memory, providers, server  # noqa: E402
from fakellm import FakeLLM  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TMP, ignore_errors=True)


class Sink:
    """冒充一个打开着的网页：broadcast 发出的事件都收在这里。"""

    def __init__(self):
        self.events: list[dict] = []

    async def send_json(self, event: dict):
        self.events.append(event)

    def of(self, type_: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == type_]


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(providers, "client", lambda name, timeout=None: fake.client)
    return fake


@pytest.fixture
async def events():
    sink = Sink()
    server.clients.add(sink)
    server.watching[sink] = True   # 网页正被看着：不发手机推送
    yield sink
    server.clients.discard(sink)
    server.watching.pop(sink, None)
    await settle()


_n = 0


@pytest.fixture
def make_bot():
    """每个测试建自己的 Bot，名字不重复，互不干扰。"""
    def make(name: str = "", instructions: str = "") -> dict:
        global _n
        _n += 1
        return bots.create(name or f"Bot{_n}", None, instructions)
    return make


async def settle(timeout: float = 5):
    """等后台的轮次和任务都跑完，免得上一个测试的尾巴吃掉下一个测试的剧本。"""
    me = asyncio.current_task()
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if not [t for t in asyncio.all_tasks() if t is not me and not t.done()]:
            return
        await asyncio.sleep(0.02)


async def until(pred, timeout: float = 5, what: str = "条件"):
    """反复检查 pred()，成立就返回它的值；超时算失败。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        if v := pred():
            return v
        if loop.time() > end:
            raise AssertionError(f"{timeout} 秒内没等到：{what}")
        await asyncio.sleep(0.02)


def messages(chat_id: int) -> list[dict]:
    rows = memory.conn.execute("select role, content, meta, bot_id from messages where chat_id=? order by id",
                               (chat_id,)).fetchall()
    return [{"role": r, "content": c, "meta": json.loads(m or "{}"), "bot_id": b} for r, c, m, b in rows]


def find(chat_id: int, **want) -> list[dict]:
    """对话里符合条件的消息：role=…、kind=…（meta 里的）、bot_id=…、text=…（内容里包含）。"""
    out = []
    for m in messages(chat_id):
        if "role" in want and m["role"] != want["role"]:
            continue
        if "kind" in want and m["meta"].get("kind") != want["kind"]:
            continue
        if "bot_id" in want and m["bot_id"] != want["bot_id"]:
            continue
        if "text" in want and want["text"] not in m["content"]:
            continue
        out.append(m)
    return out
