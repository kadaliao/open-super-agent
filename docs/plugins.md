# 插件（开发者，预览）

插件是放在 `data/plugins` 里的 Python 代码，和服务在同一个进程里运行，用来扩展 osa 自己：订阅事件、碰 osa 内部的状态，
给 Bot 加要用到这些的工具。这是给开发者的口子，还在预览，写法以后可能会变。设置里在 **高级 → 开发者插件（预览）**。

## 先想清楚是不是插件

插件在服务进程里跑，能做服务能做的一切，写坏了也会拖累服务。大多数“给 Bot 加个能力”的需求用不着它：

| 想做的事 | 用什么 | 为什么 |
|---|---|---|
| 接外部服务（GitHub、Notion、网盘、公司内部的接口……） | [应用](apps.md)（MCP） | 在单独的进程里跑，出了问题不影响服务；写操作照样先问你；别的 MCP 客户端也能用 |
| 教 Bot 一套做事的方法（周报怎么整理、资料怎么归档） | 技能 | 写成文字就行，不用写代码；输入 `/名字` 调用，定时任务、事件也能绑 |
| 算东西：算数、换单位、算日期、农历节气、处理一份数据 | `run_code` | Bot 现写一小段代码在沙箱里跑，比心算准，不用为每种算法装一个插件 |
| 某件事发生时让 Bot 处理一次 | [事件](events.md) | 先看内置的来源有没有（订阅源、网页变化、天气、行情、地震、公网 IP……） |
| 碰 osa 自己的东西：订阅任务结果、看 Bot 调了哪些工具、往对话里写消息、给别的插件提供服务 | **插件** | 只有在服务进程里才拿得到 |

拿不准就先用应用或技能做，做不出来再写插件。

## 先读这一段：插件能做什么

插件和服务是同一个进程，**能做服务能做的一切**：读写这台机器上的文件、访问网络、读保险库、改数据库。所以：

- **只启用你看过、信得过的插件。** 设置 → 高级 → 开发者插件（预览）里点“查看代码”就能看全文。
- **启用时会记下文件的指纹（sha256），文件改过就不再加载**，设置里显示“文件改过了”，要你看过改动再启用一次。
  文件夹插件里任何一个 `.py` 改了都算。Bot（或别的程序）往 `data/plugins` 里写东西，不经你在设置里点“启用”就跑不起来；Bot 也没有启用插件的工具。
- 列出插件**不执行代码**：名字、说明、版本、默认设置是从源码的语法树里读的（`META` 只认字面量）。
- 服务器上按 `config.yaml` 的 `security.plugins` 收紧（不写时按 `security.local_exec` / `security.demo` 推）：
  - `any`（默认，自己电脑上）：都能用。
  - `market`（服务器锁 `local_exec: false` 的默认）：**只认和插件市场里随版本发布的文件一模一样的**。能从插件市场装，
    启用和加载时比指纹（`plugins.digest`），和 `osa/catalog/plugins` 里同名插件不一样的不加载：改过的、自己写的、升级 osa 以后还是旧版本的
    （设置里那一行写着原因；旧版本在插件市场里点“更新”再启用）。
  - `off`（演示服务器的默认）：**插件整个关掉**，不加载、不能装、不能启用。
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

放好以后打开 设置 → 高级 → 开发者插件（预览），看一遍代码，打开开关。不用重启服务；改了代码点“启用”（会先让你确认）或者“重新加载”。

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
| `ctx.services` | `META` 的 `inject` 里写了的服务，按名字取：`ctx.services["messenger"]`，见下面 |
| `ctx.provide(name, value)` | 把一个对象作为服务提供给别的插件，名字要先写进 `META` 的 `provide` |
| `ctx.source(Source(...))` | 加一种事件来源，用户能在 Bot 的“事件”里选它，见下面“事件来源” |

`apply` 抛异常时，已经登记的会全部撤销，错误显示在设置页上。插件自己开的线程、改过的全局状态 osa 管不到，用 `ctx.effect` 登记清理。

## 工具

```python
from osa.registry import ROUTER, WORKER, APPLET, Tool

Tool(
    name="my_tool",                # 全局唯一。和别的插件、内置工具、应用的工具重名会加载失败（不能覆盖别人的）
    schema={...},                  # OpenAI 风格的 function 工具定义
    run=run,                       # async def run(call) -> str，或者 (str, 截图 png 的 bytes)
    where=frozenset({ROUTER, WORKER}),   # 给谁用：对话里（ROUTER）、后台任务（WORKER）、小应用的界面（APPLET）
    gated=False,                   # True：调用前先问用户（写操作、花钱的设成 True），只能配 WORKER / APPLET；用户点“总是允许”或加了规则后不再问
    describe="一句话说明",          # 出现在 Bot 的“后台 Worker 当前能用的工具”里
    progress=lambda args: "…",     # 后台任务卡片上这一步显示什么
)
```

- **对话里（ROUTER）的工具要快**：Router 得随时能回话，几秒内做不完的事只给 WORKER。Router 也不能停下来等你点确认，
  所以 `gated=True` 的工具不能给 ROUTER：`where` 里有 ROUTER 又标了 `gated`，注册时直接报错（`ValueError`），插件加载失败。
- `run` 拿到的 `call`：`call.args`（模型给的参数）、`call.bot`（谁在调，`call.bot_id`）、`call.chat_id`（结果发到的对话）、
  `call.scope`（router / worker / applet）、`call.task`（后台任务里才有）、`call.post`（对话里才有：`await call.post("assistant", 文字, chat_id)` 往对话里写一条）。
  别去碰 Router 或任务的其他字段，以后会变。
- 返回给模型的文字照项目的习惯写中文；出错时返回一句说明（“没算：日期格式不对”），比抛异常好，模型能据此改参数重试。

## 事件

| 事件 | 参数 | 什么时候 |
|---|---|---|
| `user_message` | `chat`（对话）、`text`、`via`（从哪个聊天软件来，网页上是空） | 用户发来一条消息（网页、聊天软件、群聊） |
| `task_done` | `task`（`id`、`goal`、`status`：done 或 failed、`bot_id`、`chat_id`……）、`bot`、`result` | 后台任务（包括定时任务）做完或失败、结果发进对话之后。取消的、定时任务这次没有新内容的不发 |
| `tool_done` | `scope`（router / worker）、`bot`、`chat_id`、`task`（后台任务里才有）、`name`、`args`、`result`（给模型的文字）、`status` | Bot 调完一个工具（对话里、后台任务里都算）。只读：拿到的是拷贝，改了不影响任何东西 |
| `pages_changed` | 无 | 资料库的页面变了 |
| `page_data` | `page_id`、`data` | 小应用的数据变了 |
| `plugins_changed` | 无 | 插件装了、启用、停用、删了 |

`user_message`、`task_done`、`tool_done` 是“发出去不等”的：监听器慢不会拖慢对话。监听器里的异常记进日志，不影响别的监听器。
监听器的参数以后可能会加，写成 `def on_done(task, bot, result, **_)` 留个 `**_`。

## 事件来源

[事件](events.md)是“某件事发生时 Bot 处理一次”。插件能加新的来源：启用后出现在“新建事件”的“插件”分类里，用户选它、填参数、写要 Bot 做什么；
插件停用时来源跟着撤掉，已经建好的事件还在，只是暂时不触发。osa 管调度、过滤、排队、派任务，插件只管“看有没有新的”。
大家都用得上的来源（以前市场里的 Epic 免费游戏、地震提醒、公网 IP 变了就是这样）更适合直接放进 [`osa/sources/`](../osa/sources/) 成为内置的，
不用每个人装插件；插件来源适合只有你自己用得上的（自己网站的后台、自家的服务）：

```python
"""网店新订单：自己网店的后台接口里来了新订单时触发。"""
from osa.registry import POLL, Source
from osa.sources import _util            # 联网用 _util.get：只抓公网地址、自己跟跳转、限大小

META = {"title": "网店新订单", "version": "1.0"}


async def check(params, state):
    """state 第一次是 None：只记下现状。返回 (新事件, 新 state)，state 要能存成 JSON。"""
    orders = (await _util.get(params["url"], headers={"Authorization": f"Bearer {params['token']}"})).json()
    seen = set(state or [])
    new = [{"id": f"order-{o['id']}", "title": f"新订单：{o['item']} × {o['qty']}", "text": f"买家留言：{o.get('note') or '无'}",
            "url": o["link"]} for o in orders if o["id"] not in seen]
    return new, [o["id"] for o in orders][:500]


def apply(ctx):
    ctx.source(Source(
        id="shop_orders", title="网店新订单", description="自己网店的后台接口里来了新订单时触发",
        mode=POLL, check=check, every=10, min_every=5, category="通用", icon="🛒",
        fields=[{"key": "url", "label": "订单接口", "type": "url", "required": True, "placeholder": "https://shop.example.com/api/orders"},
                {"key": "token", "label": "接口 token", "type": "secret", "required": True}],
        scenarios=[{"title": "新订单提醒", "name": "网店新订单",
                    "prompt": "有新订单。告诉我买了什么、多少件，买家留言里有要特别处理的放在最前面。"}],
    ))
```

- 三种：`POLL`（osa 隔一段时间调 `check`）、`PUSH`（外面 POST 到事件的地址，`parse(req, params)` 把请求整理成事件）、
  `INTERNAL`（订阅 osa 自己的事件，`on` 写事件名，`match(params, routine=…, **事件参数)`）。
- 事件是 `{"title": 一行标题, "text": 给 Bot 看的详情, "url": 链接}`。`fields` 是给用户填的参数（text、url、number、select、multi、bool、
  secret、list），网页按它画表单、服务端按它检查；secret 存进保险库，`check` 拿到的是解开的值。`scenarios` 是常见用法，点一下表单就填好了。
- `check` 抛异常时错误显示在事件的列表上（“出错：……”），写成用户看得懂的中文。
- 完整的字段说明在 `osa/registry.py` 的 `Source`，内置的来源在 `osa/sources/`，都是现成的例子。

## 服务和插件之间的依赖

插件可以把自己的东西（函数、对象）作为服务提供出去，别的插件声明要用它。在 `META` 里写清楚，设置页不执行代码就能看出谁依赖谁：

```python
# data/plugins/glossary.py：提供服务
META = {"title": "术语表", "provide": ["glossary"], "config": {"terms": {"OSA": "open-super-agent"}}}

def apply(ctx):
    ctx.provide("glossary", lambda word: ctx.config["terms"].get(word))
```

```python
# data/plugins/explain.py：用别人的服务
from osa.registry import ROUTER, Tool
META = {"title": "解释缩写", "inject": ["glossary"]}

def apply(ctx):
    lookup = ctx.services["glossary"]
    async def run(call):
        return lookup(call.args.get("word", "")) or "术语表里没有"
    ctx.tool(Tool("explain", {...}, run, where=frozenset({ROUTER})))
```

- 要用的服务还没有（提供它的插件没装、没启用、出错了），这个插件**先不加载**，设置页上标“等待依赖”，写着要先启用谁。
  提供者一加载好，等着的自动加载，不用再点。
- 提供者停了（停用、删除、重新加载、改设置）：**用它服务的插件先停下**，回到等待；提供者回来后自动恢复。
  停用提供者时设置页会先问你，列出哪些会跟着停。
- 两个插件互相等对方的服务（成环）永远加载不了，设置页上标“循环依赖”和那一圈是谁。
- 服务名不能抢：别人已经提供了同名服务的话，加载失败。

### 内置的服务

| 服务 | 用法 | 说明 |
|---|---|---|
| `llm` | `await ctx.services["llm"].complete("一句话" 或 messages, system=None, role="worker")` | 调这台服务配好的模型（`role` 选用对话还是后台任务的模型）。算进每天的 token 预算，用完了报错；费用记在“插件 名字”下，观测页能看到 |
| `messenger` | `await ctx.services["messenger"].post("文字", bot=None, chat_id=None)` | 以某个 Bot 的名义往对话里发一条消息（`bot` 是 id 或名字，不给就是主 Bot；`chat_id` 不给就是它的私聊）。消息下面标着来自哪个插件；Bot 看到的是一条系统通知，里面冒充“系统通知”的文字会被改成“转述的”，网页、订阅源里的原文带不跑它 |

## 插件不能做的

插件**不能改 Agent 本身的循环**：调模型之前改发出去的内容、调工具之前改参数或拦下、调完改结果，这些都不开放。
试过开这几个口子，又去掉了：提示词前缀逐字不变才能命中缓存、外部文字要过 defang、Router 要几秒内回话，这些约定插件都能悄悄破坏，
出了问题表现为“变贵了”“变慢了”“Bot 被网页里的话带跑了”，很难查到是哪个插件。要观察 Bot 做了什么用 `tool_done` 事件（只读）；
要禁止某个工具用设置里的确认规则。

## 第三方包

插件只能用 Python 标准库和 osa 自己装了的包（`httpx`、`pyyaml`、`cryptography`、`openai`、`mcp`……）。
桌面 App 自带的运行环境装不了别的包；用 Docker 或源码部署的可以自己往环境里装，但换台机器就要再装一遍。要用别的语言或者一堆依赖，写成 [应用](apps.md)。

## 插件市场

设置 → 高级 → 开发者插件（预览） → 插件市场里的插件随项目发布，在 [`osa/catalog/plugins/`](../osa/catalog/plugins/)，提 PR 时会有人看过代码。
点“安装”把它复制到 `data/plugins`，仍然要你看过代码、自己启用；市场里出了新版本显示“更新”，换上新版本后要重新启用。
不支持从任意网址安装插件。

现在有 7 个：

**看着点**

- **工具调用记录**（`audit_log`）：Bot 每调一次工具记一行（时间、Bot、工具、参数和结果的开头，像密钥的先抹掉），一天一个文件、留 `keep_days` 天；
  对话里能问“昨天谁调了 send_email”（`audit_search` 工具）。
- **花费提醒**（`budget_alert`）：每 10 分钟看一次 token 和今天的花费，到了每日 token 预算的 `warn_at_percent`（默认 80%、100%）或你定的 `daily_cost`，
  以 Bot 的名义提醒你，每条线每天最多一次。不调模型、不花钱。
- **关键词提醒**（`keyword_alert`）：你在哪个对话里（群聊、别的 Bot 的私聊、Telegram、钉钉……）说了 `keywords` 里的词，就在指定 Bot 的私聊里记一笔提醒；
  同一个对话同一个词 `cooldown_minutes` 分钟内只提醒一次。
- **订阅 RSS**（`rss_watch`）：定时看几个订阅源，有新文章就以 Bot 的名义原样发到它的对话里，不调模型、不花钱。
  要 Bot 读完、筛选、总结再告诉你，用 [事件](events.md) 里的“订阅源有新文章”。

**通知和发送**

- **推送到手机**（`push_phone`）：Bot 能把通知推到你手机上（Bark、Server酱、PushPlus、ntfy、Telegram、Gotify、企业微信、飞书、钉钉），
  `on_task_done` 设成 `failed` 或 `all` 后台任务做完也推。还提供 `notify` 服务给别的插件用。设置里选 `channel`，填它下面那一组（token 写 `vault:名字`）。
- **发邮件**（`smtp_mail`）：Bot 能用你的邮箱（SMTP）发纯文字邮件，只在后台任务里用，每封默认先弹卡片问你。
  设置里填 `user`、`password`（授权码，写 `vault:名字`），`host` 不填按邮箱后缀猜；`allowed_domains` 能限制只发给哪些域名。
- **任务完成推送到群机器人**（`task_webhook`）：后台任务做完或失败时推到飞书、钉钉、企业微信、Slack、Discord 的群机器人，或任意接收 JSON 的地址。
  设置里填 `url`（建议 `vault:名字`）和 `kind`。

以前市场里还有十几个，后来按“先想清楚是不是插件”归了位：

- **计算器、单位换算、日期计算、中国节假日、农历、时区换算、抽签、字数统计、汇率、天气、行情**下架了。算数、换算、算日期、处理数据让 Bot 用
  `run_code` 现写代码在沙箱里跑；汇率、天气、行情这类查询让 Bot 上网查（后台任务里的 `web_search`、`fetch_url`），或者接一个对应的[应用](apps.md)；
  要到时提醒的用内置的事件“天气提醒”“价格到线”。
  已经装在 `data/plugins` 里的不受影响，照旧能用，只是市场里不再更新。
- **Epic 免费游戏、地震提醒、公网 IP 变了**成了内置的事件来源，不用装，在 Bot 的“事件”里直接选。
  装过这几个插件的，停用、删掉就行（不删的话加载时会报“已经内置了”），用它们建好的事件照常触发。

想把自己的插件放进市场：先对一遍上面那张表，确定它要碰 osa 内部状态；放进 `osa/catalog/plugins/`，加上测试（参考 `tests/test_catalog_ops.py`
里的 `test_audit_log_plugin`），提 PR。纯计算、联网查询、接外部服务的不收（分别用 `run_code`、`web_search` / `fetch_url`、应用）；大家都用得上的事件来源
直接加进 `osa/sources/`。也不收：要绕过验证码或人机校验的、偷偷往外发数据的、要额外装包的。

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
