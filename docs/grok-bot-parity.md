# 对齐 Grok Bot（已完成）

> [!NOTE]
> 这是项目第一阶段的计划，P1–P7 已在 v0.3.0（2026-11-04）全部完成，这里留作存档。
> 下文“暂不做”里的原生 App 已经改成计划要做。现在的计划见 [路线图](roadmap.md)。

参照对象：xAI 的 Grok Bot（https://x.ai/bot，2026-08 发布）。
资料来源：docs.x.ai/grok-bot 概览、flaviocopes 的深度评测，以及多篇第三方评测（见文末）。

## 1. Grok Bot 是什么（要对齐的东西）

| 概念 | Grok Bot 的做法 |
|---|---|
| **Bot** | 有名字、头像的常驻 AI 队友。第一条消息就是给它布置工作；之后它一直记得上下文。有一个“主 Bot”（星标）。最多 50 个 Bot + 群聊。 |
| **对话** | 每个 Bot 一个独立对话。Bot 在对话里汇报进度；结果、确认请求都以卡片形式出现。`@` 指定 Bot 或已连接的应用，`/` 调用技能。支持语音输入。 |
| **群聊** | 2–6 个 Bot 组成群聊，名字自动生成、可修改。`@某个 Bot` 或 `@everyone`。Bot 之间可以交接任务。确认请求、登录请求不在群里出现，而是在 Bot 的私聊里出现。 |
| **电脑** | 所有 Bot 共用一台常驻的云端 Linux 电脑（浏览器、文件、终端）。每个 Bot 有自己的屏幕，可以实时观看；遇到登录、验证码这类只能人做的步骤，你可以接手操作，做完再交还给 Bot。持久化目录是 `/workspace`。 |
| **技能（Skills）** | 可复用的做事方法，用 `/` 调用。“演示一遍”（Teach a task）：录制最多 10 分钟浏览器操作，生成技能草稿给你审核。 |
| **定时任务（Routines）** | 按计划或事件运行技能，关掉电脑也照常跑。每个 Bot 最多 50 个，每个保留最近 20 次运行记录。支持暂停、恢复、测试、编辑、删除。遇到掉线的应用会出“重新连接”卡片。 |
| **确认（Approvals）** | 自动审查：大多数操作直接执行，重要操作弹确认卡片，选项为允许一次、拒绝、总是允许（生成规则）。设置里管理规则，分“自动允许 / 先问我”两类，“先问我”优先。后台任务的卡片 30 分钟过期，聊天中的卡片一直等待。 |
| **应用与保险库** | 在插件浏览器里连接应用，用 `@` 提及。保险库（Vault）保存密码和卡号，API key 通过加密卡片录入，不进入聊天记录。 |
| **记忆** | 每个 Bot 有自己的记忆：稳定偏好、角色背景、过往工作摘要。少数关于你的事实（名字、时区）在 Bot 之间共享。支持“记住这个”和从其他助手导入。 |
| **资料库（Library）** | 文件和 Pages（Bot 生成的成品页面，可私有或分享），支持置顶。 |
| **通知与移动端** | 需要你处理时推送到手机；手机上可以聊天、语音、看屏幕、接手操作。 |
| **用量** | 每周用量额度，超出部分扣预付费余额。**没有模型选择器**。 |

## 2. 我们的取舍

- **要做的**：上表除团队、企业功能和 X 上的 @bot 之外的全部内容。
- **比 Grok Bot 多的**：模型可以自己配置（在设置里按 Bot、按角色选择，DeepSeek / OpenRouter / Ollama 都行）；按金额精确统计费用；DeepSeek Super-agent 的 Watchdog 和 Router/Worker 分层。
- **暂不做**：团队共享 Bot、企业管理、X 上的 @bot、iOS 原生 App（用 PWA 代替）。

## 3. 架构

```
浏览器（React SPA / PWA）
  侧栏：+新建 · Bot 和群聊列表 · 资料库 · 设置
  主区：对话（流式输出 · 进度/结果/确认卡片 · @ 和 / 补全 · 附件 · 语音）
  右栏：Bot 详情（技能 · 定时任务 · 记忆）· 电脑屏幕（noVNC，可接手）
        │  WebSocket（事件总线）+ REST
FastAPI 服务
  ├─ BotRuntime：每个 Bot 一个常驻会话（即原来的 Router），自带人设、记忆、技能
  │     └─ 派发 Worker（后台 ReAct 循环），所有调用经过 ToolGateway
  ├─ GroupChat：解析 @，选择回复的 Bot，处理交接
  ├─ ToolGateway：内置工具 + MCP 应用 + 电脑工具；统一做确认规则判断、记账、审计
  ├─ Approvals：卡片、规则（自动允许/先问我）、过期策略
  ├─ Routines：定时与事件（webhook）调度、运行记录、并发限额
  ├─ Memory：每个 Bot 的记忆 + 共享的用户事实；Inline & Spill
  ├─ Vault：密钥加密存储（主密钥来自环境变量），只把引用交给模型
  ├─ Watchdog：卡死、死循环、超预算巡检
  └─ Usage：费用统计与预算
SQLite（data/osa.db）
电脑接口（osa/computer）：所有 Bot 共用一台电脑（任务排队独占），两种后端，接口相同
  ├─ Instabox（默认，有 INSTABOX_API_KEY 时）：Firecracker 微虚拟机 + Xfce 桌面 + Chromium，
  │    REST 接口执行命令、读写文件、暂停/恢复；VNC 走 /v1/machines/{id}/vnc
  └─ 本地 Docker（备选，敏感任务或没有 key 时）：Xvfb + 窗口管理器 + Chromium + x11vnc
  桌面操作统一用 xdotool / scrot（和 Instabox SDK 的做法一致）
  实时画面：浏览器里的 noVNC ⇄ 我们服务端的 WebSocket 桥 ⇄ 电脑的 VNC
  控制权：Bot / 你，由我们的服务端加锁（Instabox 自带的锁只是建议性的，xdotool 不检查）
```

### 为什么用 Instabox 当“手”
- 和 Grok Bot 的电脑一一对应：常驻 Linux 电脑、带桌面和浏览器、能实时观看、能接手；暂停时保留内存，只收磁盘费；快照免费。
- Org Secrets 由出站代理在 HTTPS 请求头里注入密钥，机器里只有占位符，比自己做的保险库更安全。
- 费用低：4 核 8G 运行时约 $0.034/小时。
- 限制：只有托管服务，不能自部署；所以放在接口后面，保留本地 Docker 后端随时切换。

### 数据模型（SQLite，v2）
- `bots`（id, name, avatar, instructions, is_primary, model_overrides, created）
- `chats`（id, kind=direct|group, title, bot_ids, pinned, updated）
- `messages`（id, chat_id, sender=user|bot:<id>|system, kind=text|progress|result|approval|card, content, meta, ts）
- `tasks`（id, bot_id, chat_id, goal, status, …）
- `skills`（id, bot_id|null, name, description, body, source=chat|teach）
- `routines` / `routine_runs`（每个 routine 最多保留 20 条运行记录）
- `memories`（id, bot_id|null(共享), text, source, ts）
- `rules`（id, pattern, action=allow|ask）· `vault`（id, name, ciphertext）
- `pages`、`files` 索引 · `llm_usage`（沿用）
- 旧数据迁移：原来的单会话记录迁入默认的主 Bot。

### 前端
Vite + React + TypeScript，构建产物由 FastAPI 直接提供，不需要单独部署。风格参照 Grok Bot：深色优先、极简、卡片式消息、头像形状（方块、星形、四叶草、花、心形、房子……）。

## 4. 分阶段实现

每个阶段做完都能跑起来，并实测验证。

| 阶段 | 内容 | 验证 |
|---|---|---|
| **P1 Bot 与对话** | 数据模型 v2 和旧数据迁移；多 Bot（名字、头像、主 Bot）；每个 Bot 独立对话、流式输出；进度和结果卡片；新前端外壳（侧栏、对话、右栏）；设置页（模型、预算）。 | 新建两个 Bot 分别聊天，互不串话；刷新后记录还在；旧记录出现在主 Bot 里。 |
| **P2 电脑（原 P5，提前）✅** | 电脑接口；Instabox 与本地 Docker 两个后端；Worker 的电脑工具（截图、点击、输入、按键、滚动、打开网址、终端、读写文件、请你帮忙）；截图交给多模态模型判断下一步；右栏实时画面；接手 / 交还；闲置自动暂停。 | Bot 打开网页、搜索并读出结果，右栏能实时看到；接手后手动输入，交还后 Bot 继续。 |
| **P3 确认与应用 ✅** | 确认卡片 v2（允许一次 / 拒绝 / 总是允许）；规则管理；过期策略；应用浏览器（在界面里启用和配置 MCP）；保险库与加密密钥卡片；`@应用`。 | 在界面里连接文件系统应用并写文件；“总是允许”之后同一类操作不再询问。 |
| **P4 技能与定时任务 ✅** | 技能（`/` 调用、在聊天里保存为技能、编辑）；每个 Bot 的定时任务（计划和 webhook 事件；暂停、恢复、测试、编辑、删除；运行记录；限额），取代现在的 triggers。 | `/技能名` 能正确执行；定时任务按时运行，并有运行记录。 |
| **P5 记忆与资料库 ✅** | 每个 Bot 的记忆 + 共享的用户事实；“记住这个”；记忆面板（查看、删除、导入）；资料库：/workspace 文件、Pages（Bot 生成的页面，可分享链接）、置顶。 | 告诉 A 一个偏好，新开对话后 A 还记得，B 只知道共享事实。 |
| **P6 群聊 ✅** | 2–6 个 Bot 的群聊；`@` / `@everyone`；协调者分配任务和交接；确认卡片回到私聊。 | 群里让两个 Bot 分工，一个查资料、一个写总结，最后交接完成。 |
| **P7 演示一遍 + 移动端 ✅** | 在电脑屏幕上录制演示，生成技能草稿；语音输入；PWA 与推送通知；手机布局。 | 录一次操作生成技能并重放；手机上能聊天、看屏幕、处理确认。 |

## 进度记录
- 2026-11-04 P1 完成：多 Bot、流式输出、任务卡片、设置、费用统计，旧数据已迁移。
- 2026-11-04 P2 完成：Instabox 与本地 Docker 两个后端；所有 Bot 共用一台电脑，任务排队独占；实时画面、接手 / 交还、请你帮忙；
  Router 能直接看屏幕、做简单操作；验证码一律请人接手；中文输入改走剪贴板。
- 2026-11-04 P3 完成：确认卡片三选项（拒绝 / 允许一次 / 总是允许）；规则（自动允许 / 先问我，先问我优先）；
  后台任务的卡片过期、对话任务一直等；界面里添加、配置、启停应用（预设：文件系统、GitHub、Notion、网页抓取）；
  保险库（Fernet 加密，主密钥 OSA_VAULT_KEY 或 data/vault.key）；`@应用` 补全。
- 2026-11-04 P4 完成：技能（`/` 调用、对话里保存、右栏编辑、可共用）；每个 Bot 的定时任务（每天 / 每周几、间隔、webhook；
  暂停、恢复、测试、编辑、删除；最近 20 次运行记录；每 Bot 50 个）；旧 triggers 首次启动导入；对话里可直接建定时任务。
- 2026-11-04 P5 完成：每个 Bot 的长期记忆 + 共享的用户事实（Router 用 remember / forget 维护，Worker 也按偏好做事）；
  任务完成自动记“做过的事”；记忆面板（查看、修改、删除、导入）；新话题；资料库：页面（markdown / html，置顶、分享链接）、
  电脑 /data/workspace 的文件（浏览、上传、下载、置顶）、聊天附件。
- 2026-11-04 P6 完成：2–6 个 Bot 的群聊（名字自动生成、可改，成员和协调者可改）；`@` / `@所有人`，不 @ 时协调者接并用 handoff 分派；
  dispatch 可以设 handoff_to，任务做完自动交接（交接前对方的点名发言先跳过）；每轮最多 8 次发言；确认卡片和“请你帮忙”发到 Bot 的私聊，群里只留提示。
- 2026-11-04 P7 完成：演示一遍（在电脑上做一遍，录下浏览器里的点击、输入和网址，密码类只记 ******；最长 10 分钟；
  录完生成技能草稿，审核后保存，`/名字` 重放）；语音输入（浏览器自带识别）；PWA（添加到主屏幕、离线提示页）；
  推送通知（Web Push，没页面开着时推确认、帮忙、任务结果和回复）；手机布局（接手时的输入条、确认卡片、设置页）。
- 2026-11-04 电脑优化：网页操作改用 CDP（按元素编号操作、返回页面文字、识别弹窗），截图 + xdotool 作为备用。

## 资料来源
- https://docs.x.ai/grok-bot/overview
- https://flaviocopes.com/grok-bot/
- https://www.layer3labs.io/guides/grok-bot-review
- https://www.buildfastwithai.com/blogs/grok-bot-review
- https://www.eesel.ai/blog/grok-bot-review
