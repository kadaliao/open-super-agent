"""工具注册表和插件上下文：Router、Worker、小应用都从这里找工具。

以前工具散在五处（tools.TOOLS、mcp_hub.TOOLS、ctools、botadmin、Router / Worker 里一长串 if name == ...），
加一个工具要改好几个文件，Router 和 Worker 各写一遍。现在：

- 一个工具一个 Tool：schema、怎么跑（run）、在哪些场合能用（where：router / worker / applet）、这一轮给不给模型（when）、
  跑之前要不要按确认规则问用户（gated）。
- 谁提供工具谁拿一个 Context 注册，每次注册返回一个 disposer，Context.dispose() 反向撤销全部注册，不留孤儿状态
  （MCP 应用断开时就是这么把它的工具收走的）。插件的边界就是 Context。
- 事件也在这里：on(event, fn) / emit(event, **kw)。服务端用它把“页面变了”“小应用数据变了”广播给网页，
  工具的实现不用知道有 WebSocket 这回事。
- 服务：provide(name, value) 把一个对象按名字登记出去（内置的 llm、messenger，插件自己提供的），插件在 META 里 inject 要用的；
  依赖谁、谁先加载、提供者停了依赖它的先停，这些在 plugins.py。
- 事件来源：source(Source(...)) 登记一种“某件事发生了”的来源（收到 webhook、订阅源有新文章、网页变了……），
  用户在 Bot 的“事件”里选它、填参数，事件来了 Bot 在后台处理一次。调度、过滤、派任务在 events.py。
- 插件不能改 Agent 的循环（调模型、调工具的那一圈）：只能注册工具、订阅事件（只读）、提供和使用服务。
  试过在调模型、调工具前后开钩子，去掉了：提示词缓存、defang、Router 要快这些约定插件都能悄悄破坏，出了问题很难查。

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
    gated: bool = False                                 # 跑之前按确认规则（approvals.decide）问不问用户；Router 不能等，所以不能和 ROUTER 一起用（注册时报错）
    progress: Callable[[dict], str] | None = None       # Worker 任务卡片上这一步的说明
    describe: str = ""                                  # 给 Router 看的一句话能力说明（“后台 Worker 当前能用的工具”）
    group: str = ""                                     # 来自哪个应用（MCP 服务器名），空是内置
    meta: dict = field(default_factory=dict)            # 提供者自己的信息（MCP 工具：原名、界面、visibility）
    owner: str = "builtin"                              # 哪个 Context 注册的

    def offered(self, scope: str, env: dict) -> bool:
        return scope in self.where and (self.when is None or self.when(env))


PUSH, POLL, INTERNAL = "push", "poll", "internal"


@dataclass
class Source:
    """事件来源。事件是 dict：{"id": 去重用（可选）, "title": 一行标题, "text": 给 Bot 看的详情, "url": 链接（可选）}。

    三种：
    - push：外面发过来（POST /hooks/<密钥>）。parse(req, params) -> 事件列表，或者一句话（这次不处理的原因，比如 GitHub 的 ping）。
      req = {"headers": 小写的请求头, "body": bytes, "json": 解析好的 JSON 或 None, "query": 网址参数}。
    - poll：osa 每隔几分钟去看一次。async check(params, state) -> (事件列表, 新 state)。state 第一次是 None；
      第一次看到的默认只记下现状、不报（fire_first=True 的来源除外，比如价格一开始就到了线）。state 要能存成 JSON。
    - internal：osa 自己的事件（registry 里 fire / emit 的，比如 task_done）。on 写事件名，match(params, **事件参数) -> 事件列表。

    fields 是给用户填的参数，网页按它画表单、服务端按它检查：
      {"key", "label", "type": text / textarea / url / number / select / multi / bool / secret / list,
       "required", "placeholder", "default", "options": [{"value", "label"}], "help", "min", "max"}
    secret 类型存进保险库，参数里只留 "vault:名字"；list 是一行一个（或逗号分开）的文字列表。
    文字（title、description、label、help、场景）写中文，英文写进 locales/en.json。
    """
    id: str
    title: str
    description: str
    mode: str                                           # push | poll | internal
    fields: list = field(default_factory=list)
    category: str = "通用"                               # 网页上分组：开发、资讯、生活、金融、系统、通用
    icon: str = ""                                      # 网页上的小图标（一个 emoji）
    check: Callable | None = None                       # poll
    parse: Callable | None = None                       # push
    on: str = ""                                        # internal：订阅哪个事件
    match: Callable | None = None                       # internal
    every: int = 30                                     # poll：默认多少分钟看一次
    min_every: int = 5                                  # poll：最短间隔
    fire_first: bool = False                            # poll：第一次看到的也报
    validate: Callable | None = None                    # (params) -> params：通用检查之后再做自己的检查，不对就 raise ValueError
    summary: Callable | None = None                     # (params) -> 一句话说明这个事件盯着什么，比如“少数派的 RSS”
    setup: str = ""                                     # push：怎么在对方那里填地址（{url} 换成这个事件的地址）
    sample: dict | None = None                          # 点“测试”时用的示例事件
    scenarios: list = field(default_factory=list)       # 常见用法：{"title", "description", "name", "params", "prompt", "include", "exclude", "llm_filter"}
    available: Callable | None = None                   # () -> 不能用的原因，能用返回空
    owner: str = "builtin"


TOOLS: dict[str, Tool] = {}
_LISTENERS: dict[str, list[Callable]] = {}
SERVICES: dict[str, tuple[str, Any, str]] = {}   # 名字 -> (谁提供的, 对象, 一句话说明)
SOURCES: dict[str, Source] = {}
internal_hook: Callable | None = None            # events.py 设置：internal 来源的事件到了交给它


class Context:
    """插件拿到的句柄：注册工具、订阅事件，每个注册都返回 disposer；dispose() 反向撤销全部。"""

    def __init__(self, name: str):
        self.name = name
        self._disposers: list[Callable[[], None]] = []

    def effect(self, dispose: Callable[[], None]) -> Callable[[], None]:
        self._disposers.append(dispose)
        return dispose

    def tool(self, tool: Tool) -> Callable[[], None]:
        """同名工具已经由别的 Context 注册时报错，不覆盖：覆盖了再撤销，原来那个就没了（插件盖掉内置的 save_page 再卸载）。
        gated 的工具不能给 ROUTER：Router 要几秒内回话，不能停下来等用户点确认，在那里 gated 等于没写。"""
        if tool.gated and ROUTER in tool.where:
            raise ValueError(f"工具 {tool.name} 标了 gated（跑之前要用户确认），不能给 ROUTER：对话里 Router 不能停下来等确认，"
                             "标了也不会问。要确认的操作只给 WORKER / APPLET，对话里要用的快工具别标 gated")
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

    def provide(self, name: str, value, describe: str = "") -> Callable[[], None]:
        """按名字登记一个服务。别的 Context 已经登记过这个名字就报错（和工具一样不覆盖）。"""
        if (old := SERVICES.get(name)) and old[0] != self.name:
            raise ValueError(f"服务 {name} 已经由 {old[0]} 提供了")
        SERVICES[name] = (self.name, value, describe)

        def undo():
            if SERVICES.get(name, (None,))[0] == self.name:
                del SERVICES[name]
        return self.effect(undo)

    def source(self, src: Source) -> Callable[[], None]:
        """登记一种事件来源。和工具一样不覆盖别人的同名来源。停用时已经建好的事件还在，只是暂时不会触发。"""
        if src.mode not in (PUSH, POLL, INTERNAL):
            raise ValueError(f"事件来源 {src.id} 的 mode 只能是 push / poll / internal")
        if (src.mode == POLL and not src.check) or (src.mode == PUSH and not src.parse) or \
                (src.mode == INTERNAL and not (src.on and src.match)):
            raise ValueError(f"事件来源 {src.id}：poll 要写 check，push 要写 parse，internal 要写 on 和 match")
        src.owner = self.name
        if (old := SOURCES.get(src.id)) and old.owner != self.name:
            if old.owner == "builtin":   # 以前插件市场里的来源（epic_free 这几个）后来内置了：旧插件停用、删掉就行，建好的事件照常
                raise ValueError(f"事件来源 {src.id} 已经内置了，不用再装插件：停用、删掉这个插件就行，用它建的事件照常触发")
            raise ValueError(f"事件来源 {src.id} 已经由 {old.owner} 登记了，换个名字")
        SOURCES[src.id] = src

        def undo():
            if SOURCES.get(src.id) is src:
                del SOURCES[src.id]
        self.effect(undo)
        if src.mode == INTERNAL:
            def relay(**kw):
                if internal_hook:
                    return internal_hook(src.id, kw)
            self.on(src.on, relay)
        return undo

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


def service(name: str):
    return SERVICES[name][1] if name in SERVICES else None


def listening(event: str) -> bool:
    """有没有插件在听这个事件：没有的话，发事件前那些准备工作（查 Bot、拼参数）都可以省掉。"""
    return bool(_LISTENERS.get(event))




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
