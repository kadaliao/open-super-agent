# 插件

插件是放在 `data/plugins` 里的 Python 代码，和服务在同一个进程里运行：给 Bot 加工具，在有新消息、任务做完时做点事。
要接一个外部服务，先看看能不能写成 [应用](apps.md)（MCP）：应用在单独的进程里跑，出了问题不影响服务，也能给别的 MCP 客户端用。
插件适合那些要碰 osa 自己的东西的扩展：在对话里（Router）直接能用的快工具、订阅任务结果、往对话里写消息。

## 先读这一段：插件能做什么

插件和服务是同一个进程，**能做服务能做的一切**：读写这台机器上的文件、访问网络、读保险库、改数据库。所以：

- **只启用你看过、信得过的插件。** 设置 → 插件里点“查看代码”就能看全文。
- **启用时会记下文件的指纹（sha256），文件改过就不再加载**，设置里显示“文件改过了”，要你看过改动再启用一次。
  文件夹插件里任何一个 `.py` 改了都算。Bot（或别的程序）往 `data/plugins` 里写东西，不经你在设置里点“启用”就跑不起来；Bot 也没有启用插件的工具。
- 列出插件**不执行代码**：名字、说明、版本、默认设置是从源码的语法树里读的（`META` 只认字面量）。
- 服务器锁（`config.yaml` 的 `security.local_exec: false`）和演示服务器上**插件整个关掉**：不加载、不能装、不能启用。
- 备份不带插件文件，只带“启用了哪些、它们的指纹和设置”。换机器后把文件拷到 `data/plugins`，再启用一次。

## 最小的插件

`data/plugins/hello.py`：

```python
"""打招呼：Bot 能用 hello 工具跟人打招呼。

第一段是设置页上显示的说明，后面的不显示。
"""
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "打招呼", "version": "1.0", "author": "你", "config": {"greeting": "你好"}}


def apply(ctx):
    async def run(call):
        return f"{ctx.config['greeting']}，{call.args.get('name', '朋友')}！"

    ctx.tool(Tool("hello", {"type": "function", "function": {
        "name": "hello", "description": "跟人打招呼",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}}},
        run, where=frozenset({ROUTER, WORKER}), describe="跟人打招呼"))
```

放好以后打开 设置 → 插件，看一遍代码，打开开关。不用重启服务；改了代码点“启用”（会先让你确认）或者“重新加载”。

要拆成几个文件就用文件夹：`data/plugins/hello/__init__.py` 里写 `apply`，同一个文件夹里的模块用相对导入（`from .helper import x`）。

## apply(ctx)

插件只需要一个 `apply(ctx)`（也可以是 `async def`，异步的要在 15 秒内返回）。它在启用、服务启动、重新加载、改设置时调用。
**插件做的所有事都通过 ctx 登记**，停用、删除、重新加载时 osa 按相反的顺序撤销，不留下没人管的工具和监听器：

| 方法 | 做什么 |
|---|---|
| `ctx.tool(Tool(...))` | 注册一个工具，见下面 |
| `ctx.simple(name, schema, fn, **kw)` | 注册按 `await fn(**参数)` 调的工具（参数直接是模型给的） |
| `ctx.on(event, fn)` | 订阅事件，`fn` 可以是普通函数也可以是 `async def`，见下面 |
| `ctx.task(coro)` | 在后台跑一个协程（轮询、定时），停用时取消 |
| `ctx.effect(fn)` | 登记一个停用时要调用的清理函数（关连接、删临时文件） |
| `ctx.config` | 这个插件的设置（`META["config"]` 是默认值，设置页上能改，改了会重新加载） |
| `ctx.secret(value)` | 设置里写 `"vault:名字"` 的值从保险库取。webhook 地址、token 别明文写在设置里 |
| `ctx.data_dir` | 插件自己放文件的目录（`data/plugins/.<名字>.data`，要用先 `mkdir`），删插件时一起删 |
| `ctx.log` | 日志（`ctx.log.warning(...)`），出现在服务的日志里 |

`apply` 抛异常时，已经登记的会全部撤销，错误显示在设置页上。插件自己开的线程、改过的全局状态 osa 管不到，用 `ctx.effect` 登记清理。

## 工具

```python
from osa.registry import ROUTER, WORKER, APPLET, Tool

Tool(
    name="date_calc",              # 全局唯一。和别的插件、内置工具、应用的工具重名会加载失败（不能覆盖别人的）
    schema={...},                  # OpenAI 风格的 function 工具定义
    run=run,                       # async def run(call) -> str，或者 (str, 截图 png 的 bytes)
    where=frozenset({ROUTER, WORKER}),   # 给谁用：对话里（ROUTER）、后台任务（WORKER）、小应用的界面（APPLET）
    gated=False,                   # True：后台任务和小应用调用前按确认规则问用户（写操作、花钱的设成 True）
    describe="一句话说明",          # 出现在 Bot 的“后台 Worker 当前能用的工具”里
    progress=lambda args: "…",     # 后台任务卡片上这一步显示什么
)
```

- **对话里（ROUTER）的工具要快**：Router 得随时能回话，几秒内做不完的事只给 WORKER。Router 里调工具不会按 `gated` 弹卡片（它不能等），
  要确认的写操作别给 ROUTER。
- `run` 拿到的 `call`：`call.args`（模型给的参数）、`call.bot`（谁在调，`call.bot_id`）、`call.chat_id`（结果发到的对话）、
  `call.scope`（router / worker / applet）、`call.task`（后台任务里才有）、`call.post`（对话里才有：`await call.post("assistant", 文字, chat_id)` 往对话里写一条）。
  别去碰 Router 或任务的其他字段，以后会变。
- 返回给模型的文字照项目的习惯写中文；出错时返回一句说明（“没算：日期格式不对”），比抛异常好，模型能据此改参数重试。

## 事件

| 事件 | 参数 | 什么时候 |
|---|---|---|
| `user_message` | `chat`（对话）、`text`、`via`（从哪个聊天软件来，网页上是空） | 用户发来一条消息（网页、聊天软件、群聊） |
| `task_done` | `task`（`id`、`goal`、`status`：done 或 failed、`bot_id`、`chat_id`……）、`bot`、`result` | 后台任务（包括定时任务）做完或失败、结果发进对话之后。取消的、定时任务这次没有新内容的不发 |
| `pages_changed` | 无 | 资料库的页面变了 |
| `page_data` | `page_id`、`data` | 小应用的数据变了 |
| `plugins_changed` | 无 | 插件装了、启用、停用、删了 |

`user_message`、`task_done` 是“发出去不等”的：监听器慢不会拖慢对话。监听器里的异常记进日志，不影响别的监听器。
监听器的参数以后可能会加，写成 `def on_done(task, bot, result, **_)` 留个 `**_`。

## 依赖

插件只能用 Python 标准库和 osa 自己装了的包（`httpx`、`pyyaml`、`cryptography`、`openai`、`mcp`……）。
桌面 App 自带的运行环境装不了别的包；用 Docker 或源码部署的可以自己往环境里装，但换台机器就要再装一遍。要用别的语言或者一堆依赖，写成 [应用](apps.md)。

## 插件市场

设置 → 插件 → 插件市场里的插件随项目发布，在 [`osa/catalog/plugins/`](../osa/catalog/plugins/)，提 PR 时会有人看过代码。
点“安装”把它复制到 `data/plugins`，仍然要你看过代码、自己启用；市场里出了新版本显示“更新”，换上新版本后要重新启用。
不支持从任意网址安装插件。

现在有两个：

- **日期计算**（`date_calc`）：相差几天、几个工作日，加减天数和月数，星期几。大模型算日期常出错，装上以后对话里直接能算对。
- **任务完成推送到群机器人**（`task_webhook`）：后台任务做完或失败时推到飞书、钉钉、企业微信、Slack、Discord 的群机器人，或任意接收 JSON 的地址。
  设置里填 `url`（建议 `vault:名字`）和 `kind`。

想把自己的插件放进市场：放进 `osa/catalog/plugins/`，加上测试（参考 `tests/test_plugins.py` 里的 `test_date_calc_logic`），提 PR。
不收：要绕过验证码或人机校验的、偷偷往外发数据的、要额外装包的。

## 在测试里用

```python
from osa import plugins

async def test_my_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "DIR", tmp_path)      # 别碰真的 data/plugins
    (tmp_path / "hello.py").write_text(SOURCE)
    await plugins.enable("hello")
    ...
    await plugins.remove("hello")
```
