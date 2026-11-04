"""外部能力接入：连接 config.yaml 里的 MCP server，把它们的工具交给 Worker 使用。

工具名格式为 <server>__<tool>。deny 列表里的词出现在工具名中时，这个工具不开放。
"""
import os
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .config import CFG
from .approvals import needs_confirm

stack = AsyncExitStack()
TOOLS: dict[str, tuple] = {}     # 名字 -> (异步调用函数, OpenAI 工具 schema)
status: dict[str, str] = {}      # server -> 状态说明，给前端看
CONFIRM: set[str] = set()        # 调用前需要用户确认的工具


def _text(result) -> str:
    parts = [getattr(c, "text", None) or str(c) for c in result.content]
    out = "\n".join(parts)
    is_error = getattr(result, "is_error", None) or getattr(result, "isError", False)  # 兼容新旧 SDK 字段名
    return ("工具报错：" + out) if is_error else out


async def start():
    for name, s in (CFG.get("mcp_servers") or {}).items():
        try:
            await _connect(name, s)
        except Exception as e:  # 单个插件出错不影响服务启动
            status[name] = f"出错：{e}"


async def _connect(name: str, s: dict):
    if not s.get("enabled"):
        status[name] = "未启用"
        return
    missing = [k for k in s.get("env_from", []) if not os.environ.get(k)]
    if missing:
        status[name] = f"缺少环境变量 {', '.join(missing)}"
        return
    env = {**os.environ, **{k: os.environ[k] for k in s.get("env_from", [])}}
    try:
        read, write = await stack.enter_async_context(
            stdio_client(StdioServerParameters(command=s["command"], args=s.get("args", []), env=env)))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools = (await session.list_tools()).tools
    except Exception as e:
        status[name] = f"连接失败：{e}"
        return
    deny = [w.lower() for w in s.get("deny", [])]
    n = 0
    for t in tools:
        if any(w in t.name.lower() for w in deny):
            continue
        full = f"{name}__{t.name}"[:64]

        async def call(_session=session, _tool=t.name, **args):
            return _text(await _session.call_tool(_tool, args))

        TOOLS[full] = (call, {"type": "function", "function": {
            "name": full, "description": (t.description or "")[:1000],
            "parameters": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                          or {"type": "object", "properties": {}}}})
        if needs_confirm(name, t):
            CONFIRM.add(full)
        n += 1
    c = sum(1 for k in CONFIRM if k.startswith(name + "__"))
    status[name] = f"已连接，{n} 个工具（{c} 个写操作需确认）"


async def stop():
    await stack.aclose()
