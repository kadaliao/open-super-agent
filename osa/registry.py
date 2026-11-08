"""工具注册表和插件上下文：Router、Worker、小应用都从这里找工具。

以前工具散在五处（tools.TOOLS、mcp_hub.TOOLS、ctools、botadmin、Router / Worker 里一长串 if name == ...），
加一个工具要改好几个文件，Router 和 Worker 各写一遍。现在：

- 一个工具一个 Tool：schema、怎么跑（run）、在哪些场合能用（where：router / worker / applet）、这一轮给不给模型（when）、
  跑之前要不要按确认规则问用户（gated）。
- 谁提供工具谁拿一个 Context 注册，每次注册返回一个 disposer，Context.dispose() 反向撤销全部注册，不留孤儿状态
  （MCP 应用断开时就是这么把它的工具收走的）。插件的边界就是 Context。
- 事件也在这里：on(event, fn) / emit(event, **kw)。服务端用它把“页面变了”“小应用数据变了”广播给网页，
  工具的实现不用知道有 WebSocket 这回事。

还没迁过来的：电脑工具（computer/tools.py，和任务、窗口、控制权绑得紧，自成一套）、Router 自己的几个（dispatch、cancel_task 这类
要改 Router 状态的）、botadmin（要弹卡片、叫醒 Bot）。它们仍由 Router / Worker 直接处理，注册表是最后一道。
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ROUTER, WORKER, APPLET = "router", "worker", "applet"


@dataclass
class Call:
    """一次工具调用的上下文。Router、Worker、小应用各自填，工具的 run 只看这个。"""
    scope: str                      # router | worker | applet
    bot: dict                       # 谁在调（bots.get 的结果）
    chat_id: int                    # 结果、图片发到哪个对话
    private_chat_id: int            # 确认卡片发到哪（Bot 的私聊）
    args: dict
    task: Any = None                # Worker 的 Task；Router 和小应用没有
    post: Callable | None = None    # async (role, content, chat_id, **meta) -> int：往对话里写一条消息
    files: list = field(default_factory=list)   # 本轮 / 本任务 send_file 发的附件，调用方挂到回复或结果下面
    apps: list = field(default_factory=list)    # 本任务用到的应用界面（MCP Apps，见 mcpapps.py），挂在任务结果下面
    env: dict = field(default_factory=dict)     # 这一轮的环境：pages（能不能存页面）、use_pc（有没有电脑）

    @property
    def bot_id(self) -> int:
        return self.bot["id"]


@dataclass
class Tool:
    name: str
    schema: dict                                        # OpenAI 的 function 工具 schema
    run: Callable[[Call], Awaitable]                    # 返回给模型的文字，或 (文字, 截图 png)
    where: frozenset = frozenset({ROUTER, WORKER})      # 在哪些场合能用
    when: Callable[[dict], bool] | None = None          # 按这一轮的 env 决定给不给模型；None 就是总给
    gated: bool = False                                 # 跑之前按确认规则（approvals.decide）问不问用户（Router 里不问，它不能等）
    progress: Callable[[dict], str] | None = None       # Worker 任务卡片上这一步的说明
    describe: str = ""                                  # 给 Router 看的一句话能力说明（“后台 Worker 当前能用的工具”）
    group: str = ""                                     # 来自哪个应用（MCP 服务器名），空是内置
    meta: dict = field(default_factory=dict)            # 提供者自己的信息（MCP 工具：原名、界面、visibility）
    owner: str = "builtin"                              # 哪个 Context 注册的

    def offered(self, scope: str, env: dict) -> bool:
        return scope in self.where and (self.when is None or self.when(env))


TOOLS: dict[str, Tool] = {}
_LISTENERS: dict[str, list[Callable]] = {}


class Context:
    """插件拿到的句柄：注册工具、订阅事件，每个注册都返回 disposer；dispose() 反向撤销全部。"""

    def __init__(self, name: str):
        self.name = name
        self._disposers: list[Callable[[], None]] = []

    def effect(self, dispose: Callable[[], None]) -> Callable[[], None]:
        self._disposers.append(dispose)
        return dispose

    def tool(self, tool: Tool) -> Callable[[], None]:
        """同名工具已经由别的 Context 注册时报错，不覆盖：覆盖了再撤销，原来那个就没了（插件盖掉内置的 save_page 再卸载）。"""
        tool.owner = self.name
        if (old := TOOLS.get(tool.name)) and old.owner != self.name:
            raise ValueError(f"工具 {tool.name} 已经由 {old.owner} 注册了，换个名字")
        TOOLS[tool.name] = tool

        def undo():
            if TOOLS.get(tool.name) is tool:
                del TOOLS[tool.name]
        return self.effect(undo)

    def simple(self, name: str, schema: dict, fn: Callable[..., Awaitable], **kw) -> Callable[[], None]:
        """按 fn(**args) 调的工具（内置的网页工具、MCP 工具都是这样）。"""
        async def run(call: Call):
            return await fn(**call.args)
        return self.tool(Tool(name, schema, run, **kw))

    def task(self, coro) -> "asyncio.Task":
        """在后台跑一个协程（轮询、定时之类），dispose 时取消。"""
        t = asyncio.create_task(coro)
        self.effect(lambda: t.cancel() if not t.done() else None)
        return t

    def on(self, event: str, fn: Callable) -> Callable[[], None]:
        _LISTENERS.setdefault(event, []).append(fn)

        def undo():
            if fn in _LISTENERS.get(event, []):
                _LISTENERS[event].remove(fn)
        return self.effect(undo)

    def dispose(self):
        while self._disposers:
            try:
                self._disposers.pop()()
            except Exception:
                log.exception("%s 撤销注册时出错", self.name)


def get(name: str) -> Tool | None:
    return TOOLS.get(name)


def available(name: str, scope: str, env: dict | None = None) -> Tool | None:
    t = TOOLS.get(name)
    return t if t and t.offered(scope, env or {}) else None


def schemas(scope: str, env: dict | None = None) -> list[dict]:
    """这一轮给模型的工具，按注册顺序（顺序稳定，模型服务的前缀缓存才命中）。"""
    env = env or {}
    return [t.schema for t in TOOLS.values() if t.offered(scope, env)]


def by_group() -> dict[str, list[Tool]]:
    """按应用分组的 MCP 工具（group 非空的）。"""
    out: dict[str, list[Tool]] = {}
    for t in TOOLS.values():
        if t.group:
            out.setdefault(t.group, []).append(t)
    return out


_firing: set = set()


def fire(event: str, **kw):
    """发事件但不等监听器跑完：用在对话、任务这些不能被插件拖慢的地方。"""
    if not _LISTENERS.get(event):
        return
    t = asyncio.get_running_loop().create_task(emit(event, **kw))
    _firing.add(t)
    t.add_done_callback(_firing.discard)


async def emit(event: str, **kw):
    for fn in list(_LISTENERS.get(event, [])):
        try:
            r = fn(**kw)
            if inspect.isawaitable(r):
                await r
        except Exception:
            log.exception("处理事件 %s 时出错", event)
