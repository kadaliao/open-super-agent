"""应用（MCP server）：在界面里添加、配置、启用和停用，工具交给 Worker 使用。

应用配置存在数据库里；第一次启动时从 config.yaml 的 mcp_servers 导入。
工具名格式为 <应用名>__<工具>。deny 列表里的词出现在工具名中时，这个工具不开放。
环境变量的值可以写 vault:名字，启动时从保险库取明文注入，不落到配置和日志里。

每个应用跑在自己的后台协程里（MCP 的 stdio 连接不能跨协程关闭），停用时通知它退出。
"""
import asyncio
import json
import os
import re
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .config import CFG, ROOT
from .memory import conn
from . import vault

conn.execute("create table if not exists apps(name text primary key, config text, enabled integer, created real)")
conn.commit()

TOOLS: dict[str, tuple] = {}     # 名字 -> (异步调用函数, OpenAI 工具 schema)
status: dict[str, str] = {}      # 应用 -> 状态说明，给前端看
CONFIRM: set[str] = set()        # 默认需要用户确认的工具（规则可以覆盖）
_running: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
notify = None                    # 由 server 设置：async (event) -> None

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
WORKSPACE = ROOT / "data" / "workspace"

# 应用浏览器里的预设。fields 是要用户填的东西：arg 追加到命令参数，env 写进环境变量（secret 存进保险库）
PRESETS = {
    "filesystem": {"title": "文件系统", "desc": "读写指定目录里的文件",
                   "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                   "fields": [{"key": "path", "kind": "arg", "label": "允许访问的目录", "default": str(WORKSPACE)}]},
    "github": {"title": "GitHub", "desc": "仓库、issue、PR",
               "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
               "fields": [{"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "kind": "env", "label": "Personal access token", "secret": True}],
               "deny": ["delete"]},
    "notion": {"title": "Notion", "desc": "页面和数据库",
               "command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"],
               "fields": [{"key": "NOTION_TOKEN", "kind": "env", "label": "Integration token", "secret": True}],
               "deny": ["delete", "archive"]},
    "fetch": {"title": "网页抓取", "desc": "把网页转成 Markdown",
              "command": "uvx", "args": ["mcp-server-fetch"], "fields": []},
}


# ---------- 存储 ----------
def _seed():
    """把 config.yaml 里还没导入过的 mcp_servers 导入数据库。"""
    have = {r[0] for r in conn.execute("select name from apps")}
    for name, s in (CFG.get("mcp_servers") or {}).items():
        if name in have:
            continue
        env = {k: f"env:{k}" for k in s.get("env_from", [])}
        cfg = {"command": s["command"], "args": s.get("args", []), "env": env,
               "deny": s.get("deny", []), "confirm": s.get("confirm", "auto")}
        conn.execute("insert into apps values(?,?,?,?)", (name, json.dumps(cfg), int(bool(s.get("enabled"))), time.time()))
    conn.commit()


def all_apps() -> dict[str, dict]:
    return {n: json.loads(c) | {"enabled": bool(e)}
            for n, c, e in conn.execute("select name, config, enabled from apps order by created")}


def get(name: str) -> dict | None:
    return all_apps().get(name)


def public() -> list[dict]:
    out = []
    for n, a in all_apps().items():
        tools = sorted(t.split("__", 1)[1] for t in TOOLS if t.startswith(n + "__"))
        out.append({"name": n, "command": a["command"], "args": a.get("args", []),
                    "env": a.get("env", {}), "deny": a.get("deny", []), "confirm": a.get("confirm", "auto"),
                    "enabled": a["enabled"], "status": status.get(n, ""), "tools": tools,
                    "connected": n in _running and any(t.startswith(n + "__") for t in TOOLS),
                    "confirm_tools": sorted(t.split("__", 1)[1] for t in CONFIRM if t.startswith(n + "__"))})
    return out


def save(name: str, data: dict) -> dict:
    """新建或修改应用配置。保存后如果是启用状态就重连。"""
    if not NAME.match(name):
        raise ValueError("应用名只能用小写字母、数字、- 和 _，以字母开头，最多 24 个字符")
    old = get(name) or {}
    cfg = {"command": (data.get("command") or old.get("command") or "").strip(),
           "args": data.get("args", old.get("args", [])),
           "env": data.get("env", old.get("env", {})),
           "deny": data.get("deny", old.get("deny", [])),
           "confirm": data.get("confirm", old.get("confirm", "auto"))}
    if not cfg["command"]:
        raise ValueError("缺少启动命令")
    if not isinstance(cfg["args"], list) or not isinstance(cfg["env"], dict):
        raise ValueError("args 要是列表，env 要是对象")
    enabled = bool(data.get("enabled", old.get("enabled", True)))
    conn.execute("insert or replace into apps values(?,?,?,coalesce((select created from apps where name=?),?))",
                 (name, json.dumps(cfg), int(enabled), name, time.time()))
    conn.commit()
    return cfg | {"enabled": enabled}


def remove(name: str):
    conn.execute("delete from apps where name=?", (name,))
    conn.commit()
    status.pop(name, None)


# ---------- 连接 ----------
def _text(result) -> str:
    parts = [getattr(c, "text", None) or str(c) for c in result.content]
    out = "\n".join(parts)
    is_error = getattr(result, "is_error", None) or getattr(result, "isError", False)  # 兼容新旧 SDK 字段名
    return ("工具报错：" + out) if is_error else out


def _env(name: str, a: dict) -> dict:
    """解析环境变量：vault:X 取保险库，env:X 取服务进程的环境变量，其余原样。缺了就报错。"""
    env, missing = {}, []
    for k, v in (a.get("env") or {}).items():
        v = str(v)
        if v.startswith("vault:"):
            val = vault.get(v[6:])
        elif v.startswith("env:"):
            val = os.environ.get(v[4:])
        else:
            val = v
        if val is None or val == "":
            missing.append(f"{k}（{v}）")
        else:
            env[k] = val
    if missing:
        raise ValueError(f"缺少密钥：{', '.join(missing)}")
    return env


def _needs_confirm(name: str, a: dict, tool) -> bool:
    from .approvals import default_confirm
    return default_confirm(a.get("confirm", "auto"), tool)


async def _changed():
    if notify:
        await notify({"type": "apps_changed"})


async def _serve(name: str, a: dict, ready: asyncio.Future, stop: asyncio.Event):
    """一个应用的整个生命周期：连上、登记工具、等停用信号、清理。"""
    try:
        env = {**os.environ, **_env(name, a)}
        if name == "filesystem" or "server-filesystem" in " ".join(a.get("args", [])):
            WORKSPACE.mkdir(parents=True, exist_ok=True)
        params = StdioServerParameters(command=a["command"], args=a.get("args", []), env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 120)
            tools = (await session.list_tools()).tools
            deny = [w.lower() for w in a.get("deny", [])]
            n = c = 0
            for t in tools:
                if any(w in t.name.lower() for w in deny):
                    continue
                full = f"{name}__{t.name}"[:64]

                async def call(_tool=t.name, **args):
                    return _text(await session.call_tool(_tool, args))

                TOOLS[full] = (call, {"type": "function", "function": {
                    "name": full, "description": (t.description or "")[:1000],
                    "parameters": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                                  or {"type": "object", "properties": {}}}})
                if _needs_confirm(name, a, t):
                    CONFIRM.add(full)
                    c += 1
                n += 1
            status[name] = f"已连接，{n} 个工具（{c} 个写操作默认要确认）"
            if not ready.done():
                ready.set_result(None)
            await _changed()
            await stop.wait()
    except Exception as e:
        msg = str(e) or type(e).__name__
        if isinstance(e, ExceptionGroup):
            msg = "; ".join(str(x) for x in e.exceptions)
        status[name] = f"连接失败：{msg[:300]}"
        if not ready.done():
            ready.set_exception(e)
    finally:
        _drop_tools(name)
        _running.pop(name, None)
        if status.get(name, "").startswith("已连接"):
            status[name] = "已停用"
        await _changed()


def _drop_tools(name: str):
    for k in [k for k in TOOLS if k.startswith(name + "__")]:
        TOOLS.pop(k, None)
        CONFIRM.discard(k)


async def connect(name: str, timeout: float = 150) -> str:
    a = get(name)
    if not a:
        raise ValueError(f"没有应用 {name}")
    await disconnect(name)
    status[name] = "正在连接…"
    await _changed()
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
    _running[name] = (asyncio.create_task(_serve(name, a, ready, stop)), stop)
    try:
        await asyncio.wait_for(asyncio.shield(ready), timeout)
    except asyncio.TimeoutError:
        status[name] = "连接超时"
        await disconnect(name)
    except Exception:
        pass
    return status[name]


async def disconnect(name: str):
    item = _running.pop(name, None)
    if not item:
        return
    task, stop = item
    stop.set()
    try:
        await asyncio.wait_for(task, 10)
    except (asyncio.TimeoutError, Exception):
        task.cancel()
    _drop_tools(name)


async def start():
    _seed()
    for name, a in all_apps().items():
        if a["enabled"]:
            asyncio.create_task(connect(name))   # 不阻塞服务启动；npx 第一次下载可能要一会儿
        else:
            status[name] = "未启用"


async def stop():
    for name in list(_running):
        await disconnect(name)
