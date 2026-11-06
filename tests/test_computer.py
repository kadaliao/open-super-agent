"""电脑的控制权：用户接手期间 Bot 的动作等着；交还后，依赖页面的动作不照原样做。"""
import asyncio

import pytest

from osa.computer.base import Computer, ComputerError, ExecResult


class FakeComputer(Computer):
    """不连真的电脑：浏览器控制脚本收到的动作记在 ops 里。"""
    backend = "fake"

    def __init__(self):
        super().__init__(0)
        self.ops: list[str] = []

    async def status(self) -> dict:
        return {"state": "running"}

    async def start(self):
        pass

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        return ExecResult(0, "", "")

    async def _helper_run(self, payload: dict) -> dict:
        self.ops.append(payload["op"])
        return {"window": 1, "browser": "b1", "ok": True}


async def test_page_actions_are_not_replayed_after_handback():
    c = FakeComputer()
    await c.browser("snapshot", key="chat-1")
    c.takeover()
    click = asyncio.create_task(c.browser("click", key="chat-1", ref=3))
    typing = asyncio.create_task(c.browser("type", key="chat-1", ref=4, text="你好"))
    read = asyncio.create_task(c.browser("read", key="chat-1"))
    opening = asyncio.create_task(c.browser("open", key="chat-1", url="https://example.com"))
    await asyncio.sleep(0.05)
    assert not any(t.done() for t in (click, typing, read, opening))   # 你接手期间都等着
    assert c.ops == ["window", "snapshot"]

    c.handback()
    for t in (click, typing):
        with pytest.raises(ComputerError, match="用户刚接手过电脑"):
            await t
    await read       # 只读的照常做
    await opening    # 打开网址不看当前页面，照常做
    assert "click" not in c.ops and "type" not in c.ops
    assert "read" in c.ops and "open" in c.ops

    await c.browser("click", key="chat-1", ref=3)   # 没人接手时照常做
    assert c.ops[-1] == "click"


async def test_human_actions_go_through_while_holding_control():
    c = FakeComputer()
    c.takeover()
    await asyncio.wait_for(c.browser("front", key="chat-1", as_human=True), 1)   # 你自己点的“看这个”不用等
    assert c.ops[-1] == "front"
    c.handback()
