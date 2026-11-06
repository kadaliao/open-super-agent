# 架构

## 总览

```
浏览器（React SPA）── WebSocket 事件 + REST ──► FastAPI（osa/server.py）
                                                  │
   ┌──────────────────────────────────────────────┼────────────────────────────────┐
   │ Groups：群聊里轮到谁说（groups.py）              │ 后台                             │
   │ Router：每个 Bot 一个（router.py）              │                                  │
   │  · 和用户对话、派发任务、看屏幕、做简单电脑操作      │  Worker：ReAct 循环（worker.py）   │
   │  · 工具：dispatch / cancel_task / recall /      │  Watchdog：巡检（watchdog.py）     │
   │    remember / forget / save_page /              │  Routines：定时与 webhook          │
   │    look_at_screen / browser_* / computer_*      │                                  │
   └──────────────────────────────────────────────┴────────────────────────────────┘
              │                     │                          │
   Memory（SQLite）        Approvals（确认卡片）        Computer 接口（osa/computer）
   Inline & Spill + 长期记忆  Library（页面、文件）         ├─ Instabox（云端微虚拟机）
                                                           └─ 本地 Docker（Xvfb + x11vnc）
```

一条消息的处理过程：

1. 浏览器通过 WebSocket 发送 `{type: "send", chat_id, text}`。
2. 对应 Bot 的 Router 把这条消息写入数据库，组装上下文（人设 + 能力说明 + 长期记忆 + 当前任务 + 最近的消息），然后流式调用模型。
   “最近的消息”从最后一次“新话题”之后算起。
3. 模型要么直接回答，要么调用工具：
   - `dispatch`：创建一个 Task，在后台运行 Worker，Router 马上回复。
   - `browser_*` / `computer_*` / `look_at_screen`：Router 自己做一步电脑操作，看页面文字或截图后回答。
4. Worker 流式调用模型，每走一步都推送一个 `task` 事件，前端的任务卡片随之更新；同时把这一步记进任务过程（`tasklog.py`）：
   思考、调工具前说的话、每次工具调用的参数和返回、写出的结果，以 `task_step` 事件推给前端（流式文字最多每 0.3 秒一段，带起始位置 `at` 去重）。
   右栏“任务”列出 Bot 做过的任务，点开实时看过程，做完也能回看。结束时写入一条 `kind=result` 的消息，以结果卡片显示。
5. Watchdog 每隔 `watchdog_interval_s` 秒巡检一次，取消超时、没有进展、死循环或超预算的任务。

## 代码目录

```
osa/
  server.py        HTTP / WebSocket 入口、REST 接口、VNC 桥、前端静态文件
  auth.py          登录（OSA_PASSWORD）：登录页、签名 cookie、拦截未登录的请求和 WebSocket、跨站请求检查
  router.py        Bot 的常驻会话：提示词、流式输出、Router 工具；每一轮都明确在哪个对话（私聊或群聊）里
  groups.py        群聊：解析 @、协调者、handoff 队列、任务做完自动交接、每轮发言上限
  dm.py            Bot 之间的私信：message_bot 发信、回信自动送回发信的对话、深度上限、排队中的一轮合并
  teach.py         演示一遍：开始 / 结束录制、把录到的事件排成步骤、让模型写技能草稿
  push.py          手机推送：VAPID 密钥、RFC 8291 加密、订阅、哪些事件要推
  channels/        聊天软件：连接和配对、界面事件转成聊天软件里的消息和卡片、收消息走和网页一样的入口
    telegram.py    Telegram Bot API：长轮询、内联按钮
    feishu.py      飞书 / Lark：长连接（手写的 protobuf 帧编解码）、富文本和消息卡片
    dingtalk.py    钉钉：Stream 长连接、会话回复地址 / batchSend 发 markdown、dtmd:// 链接代替按钮
    wecom.py       企业微信智能机器人：长连接、按钮交互卡片、附件 AES 解密
    discord.py     Discord：Gateway（心跳、Resume）、embed 卡片和按钮
    fmt.py         Markdown 转各家能显示的格式、按长度切消息
  worker.py        后台任务：Task、ReAct 循环、Heavy 模式
  tasklog.py       任务过程：每一步的记录、实时推送、任务列表和详情、重启后标记“已中断”、按天数清理
  memory.py        SQLite 表、对话记录、Inline & Spill 上下文、新话题、用量和费用
  memories.py      长期记忆：每个 Bot 的记忆、共享的用户事实、做过的事、从其他助手导入
  library.py       资料库：页面（存储、分享链接、save_page 工具）、电脑里的文件（列目录、删除、置顶）
  bots.py          Bot 和对话的增删改查、旧数据迁移
  settings.py      界面里改的设置（覆盖 config.yaml）
  i18n.py          界面语言：t() 按中文原文查 locales/en.json；python -m osa.i18n 检查前后端的翻译
  backup.py        备份与迁移：加密的备份文件、恢复前检查、启动时换上、命令行
  approvals.py     确认卡片：写操作确认（approval）、请你帮忙（help）
  mcp_hub.py       MCP 连接（本机命令 / 远程地址）、OAuth 登录授权与令牌刷新、工具命名（<server>__<tool>）、deny / confirm 规则
  catalog/         应用市场的目录：读取和检查清单，按模板生成连接配置（python -m osa.catalog 检查所有清单）
    apps/          每个应用一份清单（<名字>.yaml），格式见 docs/apps.md；本机还会读 data/apps/
    checks.py      添加应用前的检查（邮箱先试登录一次）
  tools.py         内置工具：web_search、fetch_url
  skills.py        技能：增删改、/名字 解析、展开成给模型的说明；导出成文件、分享链接、从文件或链接导入
  templates.py     Bot 模板：检查、内置模板（templates/*.yaml）、从 Bot 导出、分享、安装
  search.py        按意思检索记忆和页面：中文两字一组的 FTS5、可选的 embedding、排名融合、search_memory 工具
  routines.py      定时任务：触发方式、调度、webhook 过滤、运行记录、旧 triggers 导入
  watchdog.py      任务巡检
  config.py        读取 config.yaml，按角色创建模型客户端
  computer/
    base.py        电脑接口、谁在用和排队、每个任务的浏览器窗口、控制权、截整个屏幕和你接手时打字（xdotool / scrot）
    cdp_helper.py  在电脑里运行的浏览器控制脚本（只用标准库），通过 DevTools 端口操作 Chromium：给任务挑窗口、按元素或坐标操作页面、
                   上传文件；也负责演示录制
    local.py       本地 Docker 后端（VNC 经 docker exec 转发）
    instabox.py    Instabox 后端
    tools.py       browser_* / computer_* 工具定义、截图消息、给 Worker 的操作说明
    image/         本地电脑镜像
Dockerfile          服务镜像：后端、构建好的前端、应用要用的 Node 和 uv
compose.yaml        Docker Compose 部署（deploy/Caddyfile 是 --profile https 的反向代理），见 docs/docker.md
web/public/         PWA：manifest.json、sw.js（推送和离线提示页）、图标
web/src/
  store.ts         全局状态、REST 封装、WebSocket 事件处理
  i18n.ts          界面语言：t() 按中文原文查 locales/en.json
  components/      Chat、Composer、Sidebar、BotEditor、Computer、Settings、Routines（技能和定时任务面板）、
                   Memories（记忆面板）、Library（资料库和分享页）、Groups（新建群聊和群聊面板）、Tasks（任务列表和过程）、Markdown
```

## 数据

运行时数据都在 `data/`（不进 git）：

| 表 | 用途 |
|---|---|
| `bots`、`chats` | Bot 和对话；每个 Bot 有一个私聊（`kind=direct`）；群聊（`kind=group`）的 `bot_ids` 有 2–6 个，`lead` 是协调者 |
| `messages` | 所有消息。`role` 是 user / assistant / task；`meta`（JSON）里放 reasoning、任务编号、卡片类型等 |
| `llm_usage` | 每次模型调用的 token 数和费用 |
| `task_steps` | 后台任务的过程：每一步的类型（思考 / 工具 / 结果……）、Heavy 里是哪个研究员、标题、内容、状态、用时。`task_id` 是“任务已派发”那条消息的 id；只留最近 `memory.task_log_days` 天 |
| `memories` | 长期记忆。`bot_id` 为空的是共享的用户事实；`kind=work` 是自动记的“做过的事” |
| `pages`、`file_pins` | 资料库的页面（`share_token` 不为空表示已分享）和置顶的文件路径 |
| `push_subs` | 开启了推送的设备（推送服务地址和密钥）；VAPID 私钥存在 `settings` 的 `vapid` 里 |
| `channels` | 聊天软件连接：平台、绑定的对话（`chat_id`）、配对的私聊（`peer`）、配对码；token / App Secret 在保险库的 `channel_<id>` |
| `settings` | 界面里改过的设置；`language` 是界面语言（zh / en） |
| `computers` | 电脑后端的引用，例如 Instabox 的机器 id |

## 设计约定

改动核心行为前请先读这一节，这些规则都是踩过坑之后定下来的。

### Router 和 Worker 的分工
- Router 必须随时能响应。它只做几秒内能完成的事：回答、派任务、看屏幕、执行一步电脑操作。
- 多步、耗时、需要终端或需要等人的活，一律派给 Worker。
- 派任务时只写用户要求的内容，不自行扩大范围。
- 后台任务的结果已经以卡片形式展示给用户，Router 不需要复述（上下文里有相应的标注）。

### 提示词和缓存
- 模型服务对“和上次请求开头一样”的部分有缓存，命中的部分便宜得多（DeepSeek 便宜几十倍）。所以发给模型的内容要**前面逐字不变，变的放最后**。
- Router 的系统提示词只放不常变的：规则、人设、工具说明、其他 Bot、技能、定时任务、长期记忆、群聊说明、日期。
  每轮都会变的（当前任务、待确认的写操作、相关的更早记忆、最近做过的事、这一轮的来由）用 `STATE_PROMPT` 接在最新一条用户消息后面（`router.with_state`），
  不存进数据库。不另起一条 system（Qwen 等模型的对话模板不接受出现在对话中间的 system），也不另起一条 user（有的接口不接受连着两条 user）。
- 对话历史每满 `memory.inline_messages` 条才把更早的一批整体移出（`memory.context` 里的 `cut`），不是每来一条就挪一格。
- 往系统提示词里加东西之前先想：它每轮会不会变？会变就放进 `STATE_PROMPT`。`tests/test_prompt_cache.py` 检查连续几轮的系统提示词和历史前缀逐字不变。

### 外部文字
- 给 Router 的“（系统通知：……）”只是普通文字。别的 Bot 的发言、私信和回信、交接备注、任务结果都可能带着网页、邮件里的原文，
  所以 `memory.context` 组装上下文时用 `memory.defang` 把里面的“系统通知”“系统附上的当前状态”“系统附图”改成“转述的‘……’”，系统自己写的前缀不动。
  用户自己说的话不改。新加一种来自别处的消息进上下文时，记得也过一遍 `defang`。
- webhook 的正文进任务目标时（`routines.goal_for`）包在 `<<<事件原文开始>>>` 和 `<<<事件原文结束>>>` 之间，注明是数据不是指令，正文里冒充的边界先去掉。

### 电脑
- **所有 Bot 共用一台电脑，几个任务可以同时用。** 任务第一次用浏览器时占一个位置（`Computer.claim`，最多 `computer.parallel` 个，多了排队），
  分到一个自己的浏览器窗口；结束（包括取消和失败）时释放（`release`），窗口比并行数多就关掉它的。Heavy 模式下每个 Worker 各占一个。
  终端和读写文件不占位置。
- **任务不碰真实的鼠标键盘。** 同一个浏览器（登录状态共用）开几个窗口，任务的 `browser_*`、`computer_*` 都经 CDP 发给自己窗口里的页面：
  `computer_screenshot` 截的是页面（`Page.captureScreenshot`），`computer_click / type / key / scroll` 按截图坐标发 `Input.*` 事件。
  所以同时做也点不到别人的窗口。代价是浏览器自己的界面（地址栏、标签栏）和浏览器以外的窗口操作不到；上传文件用 `browser_upload`
  （拦下文件选择框，`DOM.setFileInputFiles`）。
- **窗口怎么分**（`cdp_helper.Ctx`）：服务端记着每个任务的窗口 id。第一次在 `_assign` 锁里挑一个没人用的（`avoid` 是别人的），
  都有人用就新开一个，大小、位置和最前面的窗口一样；挑好之后的动作不用锁，真正并行。
  窗口编号和浏览器的标识（`/json/version` 里的 id）一起记：浏览器重开后编号会和以前的撞上。动作之前发现窗口没了
  （页面自己关的、浏览器重开过）报 `WINDOW_GONE`，服务端重新挑再做；**动作之后**才没的（登录弹窗提交后自己关掉）不能重做，
  脚本回到一个没人用的窗口（一般就是打开它的那个），否则同一个提交会在别的页面上再做一遍。
  从自己窗口里的页面弹出的新窗口（登录窗口这类）就跟过去，别的任务弹出的不管。任务做完关窗口时（后台、也在 `_assign` 里）
  先看它是不是已经分给了别人。
- **被挡住的窗口不能降速**：浏览器默认把被挡住的窗口当成隐藏的，动画帧停掉、定时器降到每秒一次，网页会加载不全。
  启动参数要带 `--disable-backgrounding-occluded-windows` 等（`FLAGS`）；已经开着的老浏览器没带的话，第一个任务独自用电脑、
  你没在操作时，按原来的命令行加上参数重开一次（`restart_ok`、`_upgrade`）。命令行里没写配置目录的，从浏览器打开的文件里找
  （要有 `Local State`，缓存目录里也有 `Default/`），写明 `--user-data-dir`，登录状态不会丢；找不到、别的用户开的、关不掉就不动它。
  对话里的直接操作只在打开网页时才可能重开（重开会关掉屏幕上的页面）。浏览器只是忙、端口一时没响应时多等一会儿，不杀。
  容器里的 1 号进程不回收子进程，退出的浏览器会变成僵尸进程，判断“退出了没有”要看进程状态，不能只看 `/proc/<pid>` 在不在。
  有的 Chromium（Instabox 上的）会把自己的 `/proc/<pid>/cmdline` 改写成用空格连起来的一整串，找浏览器进程时要按空格拆开。
- **画面**：屏幕只有一块，显示最前面的窗口。新任务的窗口会到前面，CDP 点击也会把窗口带到前面，所以画面跟着最近的操作走。
  用户点了“看这个”（`/api/computer/watch`）就固定在那个任务上：别的任务点击后脚本再把它放回前面（`keep_front`）。
  `ask_human` 先把自己的窗口放到前面（你正在帮别的任务时不动）；从“请你帮忙”卡片接手时带上 `task_id`，
  按 `Computer.asking` 找到请你帮忙的那个（Heavy 的几个研究员共用一个任务 id），也先放到前面。
- **对话里的直接操作**（Router）不占位置：在屏幕上看到的那个窗口里做（`chat-<Bot id>`，10 分钟内不分给任务）。
  那个窗口正被任务用着时不等，让模型决定要不要先取消任务；打开网页例外，会换一个窗口。`look_at_screen` 截整个屏幕，只能看。
- **控制权**：用户接手期间（`controller=human`），Bot 的浏览器操作都等到用户交还（CDP 点击会把窗口带到前面，打断用户）。
  交还后，等过的点击、输入、按键、上传、滚动、后退（`STALE_OPS`）**不照原样做**，返回“用户刚接手过电脑……先看一下页面再继续”：
  用户可能换了页面、动了页面，按接手前看到的元素编号或坐标点下去会点错地方。打开网址、读页面、截图不看当前页面，照常做。
- **演示录制独占整台电脑**（`claim_exclusive`）：有任务在用就不能开始，录制期间任务排队。
- **验证码不自动解**：遇到验证码、扫码、登录、短信验证时，立刻调用 `ask_human`。项目不会加入任何绕过验证码的功能。
- 不同后端、不同窗口的大小不一样，所以截图的文字说明里写的是实际宽高，点击坐标以截图像素为准。
- 你接手时从网页打字（手机上的输入栏）走 xdotool：非 ASCII 先写进剪贴板再粘贴（`xclip`），因为 `xdotool type` 打中文会丢字。
- **网页优先走 CDP。** `browser_*` 工具把页面上可操作的元素编上号（`data-osa-ref`），模型按编号点击、输入，
  返回的是页面文字而不是截图：更快、更省 token，也不会点偏。输入用 `Input.insertText`，中文不丢字。
  页面上有弹窗时只列弹窗里的元素并提示（登录、验证码就请人接手）。截图和按坐标操作留给画布类页面、没有编号的元素和组合键。
- `cdp_helper.py` 第一次用时由服务端写进电脑的 `/tmp`（文件名带内容哈希，改了会自动更新），每个动作执行一次，
  所以两个后端都不需要额外开端口。DevTools 端口只监听电脑内部的 127.0.0.1；旧浏览器没开端口时脚本会重启它（登录状态在配置目录里，不会丢）。
  几个动作同时发现要开浏览器时用文件锁，只开一次。
- Worker 的上下文里只保留最近 2 张截图（`prune_images`），用来控制 token 成本。

### 群聊
- **一个 Bot 一个 Router**，私聊和群聊共用；`Router.lock` 让同一个 Bot 一次只说一轮。每轮开始时设置 `self.chat`，
  工具（dispatch、handoff、recall）都用它；任务记下自己的 `chat_id`，结果回到派任务的那个对话。
- **谁来说由 `groups.py` 决定**，Router 不自己抢话：@ 到的按顺序 → 没 @ 就是协调者 → 发言中 handoff 出去的排到最前面。
  同一个群一次只跑一轮（`Group.lock`）；用户的新消息会先显示出来，等上一轮说完再处理。
- 群聊上下文按“谁在看”组装（`memory.context(chat_id, viewer)`）：自己的回复保持 assistant（带 reasoning），
  别的 Bot 的发言变成“【名字】……”，交接记录写明谁交给谁。
- **任务交接**：`dispatch(handoff_to=…)` 时先 `groups.expect`，这一轮里点名给对方的发言跳过（免得它空等着表态）；
  任务做完 `after_task` 写一条交接记录，再让对方说一轮。失败或取消就不交接。
- 每一轮最多 `MAX_TURNS`（8）次发言，用完就停下来等用户。
- **确认卡片只进私聊**：任务带 `approval_chat_id`（Bot 的私聊），确认请求的 `chat_id` 是私聊，`origin_chat_id` 是任务所在的群；
  前端在群里只显示“去处理”的提示。
- 群聊里不解析 `/技能`；技能、定时任务、记忆都属于单个 Bot，在它的私聊和右栏里管理。

### Bot 之间的私信
- 不用拉群也能让别的 Bot 帮忙：`message_bot(to, text)` 在发信的对话里记一条 `kind=dm`，收信 Bot 的私聊里记一条同样的（`meta.chat` 是发信的对话），
  再叫醒收信的 Bot 在**它自己的私聊**里说一轮。群里的成员之间仍然用 handoff，`message_bot` 只能发给群外的 Bot。
- **回信自动送回，不靠模型记得回**。因私信而起的一轮带着 `turn.reply`（等回信的 Bot、它在哪个对话里等、深度、它自己欠谁回信 `then`）：
  这一轮没派任务、没再私信别人，回复就是回信；派了任务，任务带上 `reply`，结束时 `dm.after_task` 把结果（或“没做完，已取消：谁取消的”）送回；
  又私信了别人，对方的回信回来时把 `then` 还给它，它那一轮的回复再接着往回送。
- 回信在等回信的那个对话里记一条 `kind=dm_reply`，写回信的一边留一条 `sent=true` 的记录；等回信的 Bot 在原对话里接着说一轮，群聊就按群的规矩排队（`groups.wake`）。
  它已经不在那个群里了就送到它的私聊。
- **深度**：用户的消息引起的私信是 1，因私信、回信而起的一轮再发就加 1，超过 `dm.MAX_DEPTH`（5）拒绝；群里交接沿用这一串的深度，所以绕群也停得下来。
- 同一个 Bot 在同一个对话里已经排着一轮、还没开始读对话（`turn["started"]` 在组装上下文前调用），又来了同样来由的私信或回信，就不再多排一轮。
  这样派的两个任务前后脚做完，等回信的 Bot 只说一次。
- 上下文里按正在说话的 Bot 的角度写（`memory.context(..., me=)`）：“X 私信了你”“你私信了 Y，等它回信”“Y 回了你的私信”。
- 因私信而起的一轮里 Bot 自己 `cancel_task`：原因写成“X 收到 Y 的新消息后自己取消的”，不写成用户取消；被取消的任务和这一轮回给同一个 Bot 的话，不再单独送“已取消”（这一轮的回复或新派的任务会接着回它）。
- 等回信只在内存里：服务重启后没回的私信不会再回；服务关闭时被取消的任务也不送“已取消”。

### 技能文件与分享
- 一个技能一个 Markdown 文件：开头的 YAML 写 `name`、`description`，导出时加上 `apps`（做法里 `@` 到的应用），后面是做法。
  和 Claude 等用的 `SKILL.md` 一样，导入别处的技能时不认识的键直接忽略；名字里的空格、点换成 `-`。
- 分享和页面一样：生成不可猜的 `share_token`，公开页是 `/skill/<token>`（前端只渲染这一页），原文件是 `/api/shared/skills/<token>/raw`。
  这两个前缀不用登录；取消分享就把 token 清掉。
- 导入（`POST /api/bots/<id>/skills/parse`）只解析不保存，结果交给技能编辑器给用户审核：技能就是 Bot 照着做的指令，
  来路不明的可能让 Bot 把资料发到别处。粘贴链接时由服务器去取：别人的 `/skill/<token>` 换成原文件地址，
  GitHub 的文件、文件夹链接换成 raw.githubusercontent.com（文件夹取里面的 `SKILL.md`，名字用文件夹名）；网页、超过 200 KB 的都拒绝。
- 做法最长 2 万字（别处的 SKILL.md 常有几千字），只在 `/名字` 调用时才放进提示词。

### 演示一遍
- 录制在电脑里跑：`cdp_helper.py --record` 是一个后台进程，用浏览器级的 DevTools 连接 `Target.setAutoAttach` 附加到所有标签页（包括之后新开的），
  给每页加 `__osaRec` 绑定，并用 `Page.addScriptToEvaluateOnNewDocument` 注入监听脚本。**必须先 `Page.enable`**，否则新打开的页面不会注入。
- 只记浏览器里的操作：点击（按元素文字描述）、输入（失去焦点或回车时记一次）、选择、勾选、Escape、网址变化。密码和名字像验证码、卡号的输入框只记 `******`。
- 开始前先占住电脑（`Computer.claim`），后台任务排队；然后交给用户（`takeover`）。开始和结束录制的命令用 `as_human=True`，不等控制权。
- 结束后先交还电脑，再调模型写草稿，所以画面上马上能看到交还。草稿不自动保存，在技能面板里审核。

### 聊天软件
- **只是另一个客户端。** 收到的文字走和网页一样的入口（`server.safe_handle`，带上 `via`），所以 Router、群聊、技能都不用改；
  发出去的东西来自 `broadcast`：每个界面事件都交给 `channels.on_event`，按连接绑定的对话挑出要发的（`render`、`card_for`）。
  新加的事件类型想在聊天软件里出现，就在这两处加上。
- `on_event` 只排队不等网络；每个连接一个发送队列（`outbox`），保证顺序，也保证卡片发出去之后才会被更新。
- **只认配对的私聊**：消息和按钮都先比对 `peer`，不是就不理。确认卡片的按钮只能处理这个连接发出去的请求（`carding`）。
- 确认请求本来就发到 Bot 的私聊；绑定群聊的连接也会收到在这个群里派的任务的卡片（看 `origin_chat_id`）。
- 飞书 3 秒内没收到回包会重推事件：收到事件马上回包，真正的处理放到后台任务里，再按 `event_id` 去重。
- **打字等于按按钮**：“允许 #12”“拒绝 12”这样的话，编号是这个连接发出去的待确认请求时，按点了对应按钮处理，不交给 Router。
  钉钉的确认就靠它（卡片上是 `dtmd://dingtalkclient/sendMessage?content=…` 链接，点了替用户发出这句话）。
- 改不了卡片的时候发一条结果：钉钉发出去的消息不能改；企业微信只能在点按钮后 5 秒内改，在网页上处理的也只能另发一条。
- 企业微信同一个机器人只能一处连着，被别处顶掉后等 2 分钟再连，免得两边来回抢。
- 平台都实现 `check / run / send_text / send_card / edit_card`，再在 `PLATFORMS` 里注册，就能加新的聊天软件。
  可选：`typing`（正在输入）、`download`（附件，文件名要下载后才知道的返回 `(内容, 文件名)`）、`pair_link` / `setup_link`（配对页上的按钮）。

### 检索
- 两路：FTS5 关键词（中文切成相邻两个字一组，英文、数字按词，BM25）和可选的向量（OpenAI 兼容 `/embeddings`，归一化后点积，
  `math.sumprod` 算，几千段以内几十毫秒）。两路的排名用 RRF（1/(60+名次)）合并，同一个页面只取最相关的一段。
- 不在写入处埋钩子：`search.sync()` 每次检索前比较 `memories`、`pages` 的 `(id, updated)` 和索引里记的，
  变了的重新切块、分词，删了的去掉。备份恢复、删 Bot 这类绕开模块的改动也不会漏。
- 向量在后台算（`embed_loop`，每批 32 段），按 `model` 区分，换了模型就重算。还没算出向量、或者一分钟内刚失败过时，查询不去调向量服务。
- 记忆超过 80 条时，Router 每一轮用用户最新一条消息找出相关的更早记忆，和最近 60 条一起写进提示词；其余的让它用 `search_memory` 查。

### Bot 模板
- 模板是 YAML：名字、头像、职责、`apps`（只有名字）、技能、定时任务。不带对话、记忆、资料库、密钥。
- 预览和安装分开：`/api/templates/preview` 检查（内置的、粘贴的、链接）后返回整理过的 `text`，安装时原样交回 `/api/templates/install` 再检查一次。
  先 `parse` 完整检查（技能名、做法长度、定时任务的技能在不在、触发方式），通过了才建，建的过程中出错就把建了一半的 Bot 删掉。
- 导出的是 Bot 现在的样子（`from_bot`）：自己的技能，加上定时任务用到的共用技能；`apps` 由职责、技能、定时任务里 `@` 到的应用得出。
- 分享和技能一样：`bots.template_token`，公开页 `/template/<token>`，公开接口不透露这台连了哪些应用。
- 装好的技能来源记为 `import`；webhook 类定时任务的名字全局唯一，重名时加 `-2`。

### 备份与迁移
- 导出先在事件循环里用 SQLite 的 backup 接口做快照（服务照常运行），解密密钥、压缩、加密放到线程里。
  快照里的 `vault` 表清空，密钥明文放进同一个加密包；恢复时用这台的主密钥重新加密，所以主密钥不用跟着走。
- 文件格式：`OSABAK1` + 一行 JSON 头（scrypt 参数、salt、nonce 前缀），后面是 zip 按 1 MiB 分块的 AES-256-GCM 密文。
  nonce 里有块号和“是不是最后一块”，头作为附加数据，所以口令不对、截短、改过都解不开。分块是为了大备份也不占内存（服务器 MemoryMax 600M）。
- 恢复不在运行中换数据库：`stage` 解密、`integrity_check`、重加密密钥、清掉 `computers`、停用聊天软件，放到 `data/restore/`；
  确认后写上 `ready`，向自己发 SIGTERM。uvicorn 正常关闭，lifespan 收尾的最后 `os.execv(sys.executable, sys.orig_argv)` 用同样的命令行重新执行，
  `server.py` 在导入任何会打开数据库的模块之前调用 `backup.apply_pending()` 换上新库（旧库连同 `-wal`、`-shm` 改名留着）。
  systemd、Docker（`python -m uvicorn`）、直接运行都能这样重启；`--reload` 的子进程重新执行不了，只能退出等手动重启。
- 命令行和服务各自打开数据库；命令行先检查主密钥能不能解开保险库里的值，对不上（没带 `OSA_VAULT_KEY`）就拒绝。

### 界面语言
- 中文原文就是翻译的键，英文在 `en.json`，没翻译的地方显示中文。中文界面和加翻译之前完全一样。
- 语言是全局的一个设置（单用户），前后端共用：服务端发的报错、卡片、推送、聊天软件里的消息都按它来。前端在 localStorage 留一份，
  打开页面不用等接口；和服务端的不一样时重新加载一次。没设过时，新装的按第一次打开时浏览器的语言，升级上来的老用户定为中文。
- 写给模型的提示词保持中文，英文界面只在末尾加一句默认用英文回复；中文界面的提示词一个字不变。
- 存进数据库、之后还要比对的标记（`[任务已派发]` 等）存中文原文，在显示的地方翻。

### 推送与 PWA
- 推送按 RFC 8291（aes128gcm）加密、RFC 8292（VAPID）签名，只用 `cryptography` 和 `httpx`。推送服务只看得到密文。
- 什么时候推：所有页面都没被看着时（前端在切前后台时发 `visibility`），把确认、帮忙、任务结果、Bot 回复推出去；流式输出之类的过程事件不推。
- 开了推送的设备不再弹页面自己的系统通知（`localStorage osa.push`），免得重复。
- Service Worker 不缓存接口和页面，只在连不上服务器时返回一个提示页。

### 记忆与资料库
- **两种“记忆”别混**：`memory.py` 是对话记录（Inline & Spill，只在本对话里有效）；`memories.py` 是长期记忆，
  写进每轮的系统提示词，跨新话题、跨对话保留。
- 记忆由 Router 自己用 `remember` / `forget` 维护，不额外调模型去“提取”。共享的只放名字、时区这类关于用户本人的基本事实，
  其余都记在 Bot 自己名下，所以 B 不会知道你对 A 提的要求。
- Worker 的系统提示词带上用户事实和这个 Bot 的记忆（不含做过的事），这样派出去的任务也按偏好做。
- “做过的事”只在对话里派的任务完成时记一笔（取任务描述第一行，不调模型）；定时任务有自己的运行记录，不重复记。
- 页面的链接写成 `/library/pages/<id>`，对话里点击会在资料库里打开；分享链接是 `/s/<token>`，前端只渲染这一页。
- **HTML 页面只能通过 `/raw` 地址显示**，响应头带 CSP：`sandbox`（来源变成 null）、`connect-src 'none'`（不能请求接口或开 WebSocket）。
  WebSocket 另外拒绝来源为 `null` 的连接。不要把 HTML 页面内容直接塞进应用的 DOM。
- 文件都在共用电脑的 `/data/workspace` 里，经 `exec` 读写，所以两个后端一样；路径一律先过 `library.norm`，跑出这个目录就拒绝。
  电脑没开时列目录不会自动开机，由用户点“开机查看”。
- Heavy 模式下并行的几个 Worker 拿不到 `save_page`，免得每个都存一份页面。

### 模型接口（DeepSeek）
- 思考模式下，如果请求带了工具，历史回复的 `reasoning_content` 必须原样传回，所以它存在消息的 `meta.reasoning` 里。
- 截图消息放在本轮所有 `tool` 消息之后，并且只在当轮使用，不写进数据库。

### 新增电脑后端
继承 `Computer`，实现 `status / start / pause / exec / open_vnc` 这几个方法，然后在 `computer/__init__.py` 的 `get()` 里注册。浏览器动作（`cdp_helper.py`）、截屏都基于 `exec`，不需要重新实现。
