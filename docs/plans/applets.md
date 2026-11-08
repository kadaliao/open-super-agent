# 小应用：Bot 按要求做出能操作的界面，以及之后的插件化

## 为什么

用户想要的不只是“把结果做成页面”，而是“给我一个能用的东西”：报名表、看板、计算器、按一下就去查的面板、每天自动更新的仪表盘。
以前 Bot 能做 html 页面，但页面是死的：在带 CSP 的沙箱里，连不上接口，填的东西没地方去，也没法回头让 Bot 做事。

调研过 DeepSeek Harness（dsh，2026-08 开源的 TypeScript agent runtime，“一切皆插件”，底层是 Cordis）。结论：

- **不改成 TypeScript，也不接 Cordis**。后端 1.7 万行 Python，架构约定（CDP 窗口分配、提示词缓存前缀、群聊轮次、私信链）是踩坑换来的，
  重写没有用户看得见的收益。Cordis 的“时空可插拔”本质是两条纪律：每个注册返回一个 disposer、卸载时反向撤销；依赖声明式。这不依赖语言。
  dsh 自己还是 developer preview、rc 版本、明说会破坏兼容。
- **“Bot 造界面”不需要插件框架**，需要的是一座桥：沙箱里的页面能安全地调 Bot 的能力、存自己的数据、给 Bot 发消息。
  这和 MCP Apps（MCP 服务器返回 `ui://` 资源、宿主在沙箱 iframe 里渲染、postMessage 通信）是同一个模型，以后能对接。
- 插件化本身分开做（第二步起），抄原则不抄框架。

## 分几步

1. **小应用运行时**（本计划，已做）：清单、数据、宿主桥、对话里原地显示、两个工具改动。
2. **工具注册表**（已做，2026-11-08）：`osa/registry.py` 的 `Tool` / `Context` / 事件，内置工具都在 `osa/builtin.py` 注册一次，
   MCP 应用每个一个 `Context`，断开时 dispose。前端 `web/src/cards.tsx` 按 `meta.kind` 注册卡片。
   还没迁：电脑工具、Router 自己的几个、botadmin（见架构文档“工具注册表与插件”）。
3. **MCP Apps 宿主**（已做，2026-11-09）：应用（MCP 服务器）自带的界面显示在任务结果下面，设计见下面“第 3 步的设计”。
   原来写的“小应用导出成标准形式”挪到以后：那要 osa 自己当一个 MCP 服务器，是另一件事。
4. **外部 Python 插件**（已做，2026-11-09）：`data/plugins` 里的 Python 代码，用同一个 `Context` 注册工具、订阅事件；市场是随项目发布、审查过的 `osa/catalog/plugins`。
   Bot 自己产出的扩展只能是沙箱里的东西（小应用、技能、MCP 连接），不能是进程内代码：插件只有用户在设置里能启用。设计见下面“第 4 步的设计”。

## 第 1 步的设计

### 数据

小应用就是资料库里的一个 html 页面，多两样东西：

- `pages.app`：清单（JSON）。`tools` 是页面里允许调用的工具名；`chat` 为 true 时页面能替用户给 Bot 发消息。只对 html 有效。
- `page_data(page_id, key, value)`：这个小应用自己的键值数据。界面里填的、Bot 用 `page_data` 工具写的都在这里，两边都能读。
  限制：100 个键、每个值 32K 字、总共 256K 字。

复用了页面已有的一切：资料库列表、置顶、分享、按意思检索、`read_page`、删除。

### 页面里的 SDK

服务器返回小应用的原文（`/api/pages/<id>/raw`）时把 SDK 内联进 `<head>`（`applets.inject`），Bot 写页面不用引任何脚本：

```js
osa.call(tool, args)            // 调清单里允许的工具，返回文字；要确认的会弹卡片，这个 Promise 一直等
osa.send(text)                  // 替用户给 Bot 发一条消息（清单 chat: true）
osa.store.get(key) / all() / set(key, value) / remove(key)
osa.on('data', fn)              // 数据被 Bot 或别的窗口改了
osa.resize(height)              // 一般不用，高度自动跟着内容
```

### 安全边界

和普通 html 页面一样：沙箱 iframe（没有 `allow-same-origin`，origin 是 null）、CSP `connect-src 'none'`，页面碰不到站点的任何数据，
也连不上网。它唯一的出口是 postMessage 给外面的网页。

网页里的宿主（`web/src/applet.ts`）只认来自那个 iframe 窗口的消息，只做四件事：调工具、读写数据、替用户发消息、调整高度；
每件事都再去调服务器的接口（`/api/pages/<id>/call`、`/data`、`/say`），服务器按清单放行。

- 工具调用照旧走确认规则（`approvals.decide`）：命中“先问我”的，卡片出现在这个 Bot 的私聊里，用户同意才执行；拒绝的返回错误。
  清单只能列 Bot 这一轮能用的工具（`save_page` 时检查），所以小应用拿不到 Bot 自己没有的能力。
- 替用户发的消息标着来自哪个小应用：界面上有标签，给模型的上下文里是“（来自小应用「标题」）”。
- 分享出去的页面（`/s/<token>`）没有宿主，SDK 的调用都会失败；数据接口开了登录就要登录。
- 一个小应用同时最多 4 个工具调用在跑。

### 对话里显示

Bot 回复里的 `/library/pages/<id>` 链接展开（`Markdown.tsx` → `Applet.tsx`；单独一行的原地换掉，句子里的放在那一段后面）：小应用直接显示出来能操作，高度跟着内容走；
普通页面显示成一张卡片，点了在资料库里打开。Bot 更新了页面（`pages_changed`）会重新加载。
资料库里打开小应用也带宿主，分享页不带。

### 工具

- `save_page` 多一个 `app` 参数；更新时不给就保留原清单。
- 新工具 `page_data(page_id, set?)`：读或写小应用的数据。别的 Bot 建的只能读。
- Router 和 Worker 的提示词里加一段怎么做小应用（`applets.GUIDE`），放在不变的部分，不影响缓存。

### 验收

- `tests/test_applets.py`：清单校验、注入、数据和上限、工具只认清单、确认卡片在 Bot 私聊里、替用户发消息进对话并标明来源。
- 真的浏览器里：Bot 做一个报名表小应用，填表存进数据，Bot 用 `page_data` 读到；按钮调 `web_search` 返回结果；`osa.send` 后 Bot 接着说。

## 第 3 步的设计：MCP Apps 宿主

按 MCP Apps 扩展（`io.modelcontextprotocol/ui`，2026-01-26 版）做宿主，应用不用为 osa 写任何东西。

### 流程

1. 连应用时在 `initialize` 里声明 `extensions: {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}`，
   有的应用看到这个才登记带界面的工具。
2. 工具的 `_meta.ui.resourceUri` 指向一个 `ui://` 资源（也认废弃的 `_meta["ui/resourceUri"]`）。`_meta.ui.visibility`：
   没有 `model` 的不给模型、也不给小应用，只给它自己的界面调；没有 `app` 的界面不能调。
3. 后台任务调了带界面的工具：给模型的结果后面加一句“用户会直接看到界面，答案只写结论”，
   同时把这次调用（应用、工具、ui:// 地址、参数、完整的 `CallToolResult`）记在任务上，结果消息的 `meta.apps` 里带着。
   一个任务最多挂 3 个，结果太大（超过 200K 字）就只给文字。Heavy 的草稿不挂。
4. 前端在任务结果下面为每一个开一个 iframe，地址是 `/api/mcp-apps/<应用>/view?uri=...`。服务端从应用读这个资源，
   按资源的 `_meta.ui.csp` 拼 CSP，同一次连接里缓存，应用断开时清掉（挂在应用的 `Context` 上）。
5. 网页里的宿主（`web/src/mcpapp.ts`）和界面走 postMessage 上的 JSON-RPC 2.0：
   - 界面发 `ui/initialize`，宿主回协议版本、能力（openLinks、serverTools、serverResources、logging、message）和 hostContext
     （主题、语言、时区、平台、容器大小、只有 inline 一种显示方式）。
   - 收到 `ui/notifications/initialized` 之后才发 `tool-input` 和 `tool-result`（规范要求之前不能发任何东西）。
   - `tools/call` → 服务端只放行同一个应用里 visibility 含 app 的工具，命中确认规则的在 Bot 的私聊里弹卡片；
     `resources/read` → 同一个应用的资源；`ui/message` → 替用户给结果所属的 Bot 发消息，标明来自哪个应用的界面；
     `ui/open-link` → 只开 http(s)；`ui/notifications/size-changed` → 调 iframe 高度；主题变了发 `host-context-changed`；
     卸载时发 `ui/resource-teardown`。
   - 没做的：全屏 / 画中画（`ui/request-display-mode` 总是回 inline）、`ui/update-model-context` 收下但还没带进模型的上下文、
     `ui/download-file`、`sampling/createMessage`、权限（摄像头、麦克风这些不给）。

### 和规范不一样的一处：没有“沙箱代理”

规范要求网页宿主把界面套在一个“沙箱代理”里：代理是另一个源上的 iframe，界面在代理里面。自己部署的服务往往只有一个地址
（`127.0.0.1:8765`、一个域名、桌面 App 里的本机地址），没有第二个源可用。所以界面直接放在一个 iframe 里：

- iframe 不给 `allow-same-origin`，响应头的 CSP 开头也有 `sandbox`，文档是不透明源，碰不到这个站点的 cookie 和接口；
  直接在新标签页打开也一样。
- CSP 只认声明过的域名：`resourceDomains` 进 script / style / img / font / media，`connectDomains` 进 connect，
  `frameDomains`、`baseUriDomains` 同理；没声明的都是 `'none'`。不给 `'self'`（这里的 self 是本站，带着登录的接口）。
  域名只认干净的 `https://` / `wss://` 写法（和本机的 http），别的写法丢掉，防止往 CSP 里塞指令。
- 协议上看不出差别：代理本来只是原样转发，官方的 App SDK 照常能用（下面的验收用的就是它）。
- 代价：界面里没有 `localStorage`，界面发出的请求 `Origin` 是 `null`，只放行特定来源的接口会拒绝它。
  以后有部署需要时可以加一个可选的第二个地址（比如 `sandbox.<域名>`），在那上面做代理。

### 验收

- `tests/test_mcp_apps.py` 接一个真的 stdio 应用（`tests/fixtures/mcp_ui_server.py`）：工具按 visibility 分给谁、
  界面的 CSP、只认声明过的 ui:// 地址、界面调工具（只给模型的拒绝、确认卡片、拒绝后 403）、读资源、任务结果挂上界面、
  界面替用户发消息、断开后工具和缓存的界面都撤掉、Router 的能力说明里不列只给界面用的工具。
- 真的浏览器里，界面用官方的 `@modelcontextprotocol/ext-apps` 2.0.4（从 jsDelivr 加载）：握手、收到参数和结果、拿到深色主题、
  调只给界面用的工具、调只给模型的工具被拒绝、替用户发消息进对话、高度跟着内容走。
- 真模型（deepseek-flash）：“用 @uidemo 查一下南京的天气”，Worker 自己调了带界面的工具，结果下面显示界面，答案只写了一句结论。

## 第 4 步的设计：插件

写法和安全说明面向插件作者的版本在 [插件](../plugins.md)，这里记为什么这样定。

### 和 Cordis / dsh 对照

- 拿过来的：插件就是一个 `apply(ctx)`；**所有注册都经 ctx，dispose 时反向撤销**（工具、事件监听、后台任务、清理函数），
  所以启用、停用、改设置、重新加载都不用重启服务，也不留孤儿状态。这就是 Cordis 的“时间”那一半。
- 后来补上的（2026-11-09）：服务和依赖，也就是 Cordis 的“空间”那一半。`META` 的 `provide` / `inject` 声明（字面量，
  不执行代码就能画出依赖图），要用的服务没有就等着，提供者加载好了自动叫醒，提供者停了依赖它的先停、回到等待，成环的标出来。
  内置提供 `llm`（调模型，算预算、记在插件名下）和 `messenger`（以 Bot 的名义发消息，Bot 看到的是 defang 过的系统通知）。
  实现在 `plugins.py`（`_waiting`、`_wake`、`unload` 里的级联）和 `registry.Context.provide`。
- 试过又去掉的：插件改 Agent 的循环（调模型前改消息和模型、调工具前改参数或拦下、调完改结果三个钩子）。
  能用的地方不多，风险却落在最脆弱的地方：提示词缓存、defang、Router 要快这些约定插件都能悄悄破坏，出了问题很难查；
  而且钩子一公开，以后改 Router / Worker 都要兼容它。osa 是成品不是框架，核心循环由项目自己负责。
  要观察用只读的 `tool_done` 事件，要禁止工具用确认规则。顺带做的“发给模型前打码”也一起去掉了，以后需要再做成内置功能。
- 同名工具**不能覆盖**：覆盖了再撤销，原来那个就没了（插件盖掉内置的 `save_page` 再卸载）。`Context.tool` 遇到别人注册过的名字直接报错。

### 安全

插件是进程内代码，能做服务能做的一切，所以防线放在“谁能让它跑起来”上：

1. **列出不执行**：`ast` 读 `META`（只认字面量）和有没有 `apply`。
2. **启用记指纹**：用户在设置里点启用（前面有确认框），记下 sha256（文件夹插件是所有 `.py`）；以后每次加载都比对，
   不一样就不加载，设置页上显示“文件改过了”，要用户看过再启用。重新加载被拒时，正在跑的还是启用时看过的那份。
3. **只有用户能启用**：没有给 Bot 的工具，接口和别的设置一样（开了登录就要登录）。Bot 用电脑往 `data/plugins` 里写文件，没有用户点击跑不起来。
4. **服务器锁、演示服务器整个关掉**（`security.local_exec`），和本机终端、自定义启动命令的应用同一个开关。
5. **市场不收任意网址**：只有随项目发布、过了 PR 审查的；装上还是要用户启用。
6. 备份不带插件文件。

### 事件

新加三个“发出去不等”的事件（`registry.fire`）：`tool_done`（Router、Worker 调完工具，参数是拷贝，只读）、`user_message`（`server.safe_handle`，网页、聊天软件、群聊都经过这里）、
`task_done`（任务做完或失败、结果写进对话之后；取消的和定时任务这次没有新内容的不发）。插件慢、出错都不影响对话。

### 验收

- `tests/test_plugins.py`：列出不执行、启用注册 / 停用撤销（工具、监听、后台任务、`sys.modules`）、改设置重新加载、
  改了文件要重新启用（文件夹插件改了里面别的文件也算）、`apply` 出错整个撤销、不能盖掉内置工具、服务器锁和演示服务器、
  两个市场插件（日期计算的逻辑、Router 真的调到它、webhook 在任务做完时推出去、从保险库取地址）、`user_message` 事件、接口。
- 浏览器里：设置 → 插件，从市场安装日期计算、看代码、确认后启用、显示运行中和它的工具；重启服务后自动加载。
- 真模型（deepseek-flash）：“今天到 2027 年 2 月 17 日还有多少天？其中多少个工作日？”，Router 直接调了 `date_calc`，答案和插件算的一致（100 天，72 个工作日）。
- 服务和依赖（补做的）：测试覆盖缺服务时等待、提供者启用后自动加载、提供者重新加载 / 停用时依赖它的先停再恢复、重启服务不管先后都能加载上、
  成环标出来、没声明的服务不能提供、不能抢内置服务名、内置 `llm`（预算、记在插件名下）和 `messenger`（defang 过的系统通知）、
  `tool_done` 事件只读、订阅 RSS 第一次只记下之后只发新的。浏览器里：消费者显示“等服务 glossary：先启用「术语表」”，
  启用提供者后自动运行，停用提供者前列出会跟着停的插件、停了以后回到等待。
