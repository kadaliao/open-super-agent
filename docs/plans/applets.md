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
2. **工具注册表**：把 Router 和 Worker 里几套工具来源（`tools.TOOLS`、`ctools.NAMES`、`botadmin.TOOLS`、`mcp_hub.TOOLS`、一串 `if name ==`）
   收成一个注册表，加一个带 disposer 的 `Context`；先把 unfurl、画图、send_file 迁上去验证接口。前端按 `meta.kind` 做卡片注册表。
3. **MCP Apps 宿主**：应用市场里的应用可以自带界面，小应用也能导出成标准形式。
4. **外部 Python 插件**（`data/plugins/*.py`）和市场：有社区需求再做。Bot 自己产出的扩展只能是沙箱里的东西（小应用、技能、MCP 连接），
   不能是进程内代码。

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
