# 架构

## 总览

```
浏览器（React SPA）── WebSocket 事件 + REST ──► FastAPI（osa/server.py）
                                                  │
   ┌──────────────────────────────────────────────┼────────────────────────────────┐
   │ Router：每个 Bot 一个（router.py）              │ 后台                             │
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
4. Worker 每走一步都推送一个 `task` 事件，前端的任务卡片随之更新。结束时写入一条 `kind=result` 的消息，以结果卡片显示。
5. Watchdog 每隔 `watchdog_interval_s` 秒巡检一次，取消超时、没有进展、死循环或超预算的任务。

## 代码目录

```
osa/
  server.py        HTTP / WebSocket 入口、REST 接口、VNC 桥、前端静态文件
  router.py        Bot 的常驻会话：提示词、流式输出、Router 工具
  worker.py        后台任务：Task、ReAct 循环、Heavy 模式
  memory.py        SQLite 表、对话记录、Inline & Spill 上下文、新话题、用量和费用
  memories.py      长期记忆：每个 Bot 的记忆、共享的用户事实、做过的事、从其他助手导入
  library.py       资料库：页面（存储、分享链接、save_page 工具）、电脑里的文件（列目录、删除、置顶）
  bots.py          Bot 和对话的增删改查、旧数据迁移
  settings.py      界面里改的设置（覆盖 config.yaml）
  approvals.py     确认卡片：写操作确认（approval）、请你帮忙（help）
  mcp_hub.py       MCP 连接、工具命名（<server>__<tool>）、deny / confirm 规则
  tools.py         内置工具：web_search、fetch_url
  skills.py        技能：增删改、/名字 解析、展开成给模型的说明
  routines.py      定时任务：触发方式、调度、webhook 过滤、运行记录、旧 triggers 导入
  watchdog.py      任务巡检
  config.py        读取 config.yaml，按角色创建模型客户端
  computer/
    base.py        电脑接口、桌面动作（xdotool / scrot）、浏览器动作（CDP）、控制权、任务排队
    cdp_helper.py  在电脑里运行的浏览器控制脚本（只用标准库），通过 DevTools 端口操作 Chromium
    local.py       本地 Docker 后端
    instabox.py    Instabox 后端
    tools.py       browser_* / computer_* 工具定义、截图消息、给 Worker 的操作说明
    image/         本地电脑镜像
web/src/
  store.ts         全局状态、REST 封装、WebSocket 事件处理
  components/      Chat、Composer、Sidebar、BotEditor、Computer、Settings、Routines（技能和定时任务面板）、
                   Memories（记忆面板）、Library（资料库和分享页）、Markdown
```

## 数据

运行时数据都在 `data/`（不进 git）：

| 表 | 用途 |
|---|---|
| `bots`、`chats` | Bot 和对话；每个 Bot 有一个私聊对话（`kind=direct`），以后的群聊 `bot_ids` 会有多个 |
| `messages` | 所有消息。`role` 是 user / assistant / task；`meta`（JSON）里放 reasoning、任务编号、卡片类型等 |
| `llm_usage` | 每次模型调用的 token 数和费用 |
| `memories` | 长期记忆。`bot_id` 为空的是共享的用户事实；`kind=work` 是自动记的“做过的事” |
| `pages`、`file_pins` | 资料库的页面（`share_token` 不为空表示已分享）和置顶的文件路径 |
| `settings` | 界面里改过的设置 |
| `computers` | 电脑后端的引用，例如 Instabox 的机器 id |

## 设计约定

改动核心行为前请先读这一节，这些规则都是踩过坑之后定下来的。

### Router 和 Worker 的分工
- Router 必须随时能响应。它只做几秒内能完成的事：回答、派任务、看屏幕、执行一步电脑操作。
- 多步、耗时、需要终端或需要等人的活，一律派给 Worker。
- 派任务时只写用户要求的内容，不自行扩大范围。
- 后台任务的结果已经以卡片形式展示给用户，Router 不需要复述（上下文里有相应的标注）。

### 电脑
- **所有 Bot 共用一台电脑。** 任务第一次用电脑时开始独占，结束（包括取消和失败）时释放（`Computer.claim / release`），其他任务排队等待。Heavy 模式下每个 Worker 各用自己的标识排队。
- Router 的直接操作每次只占用一步。电脑被某个任务占着时，Router 不等待，而是让模型决定要不要先取消那个任务。
- **控制权**：用户接手期间（`controller=human`），Bot 的所有桌面动作都会等到用户交还。
- **验证码不自动解**：遇到验证码、扫码、登录、短信验证时，立刻调用 `ask_human`。项目不会加入任何绕过验证码的功能。
- 不同后端的分辨率不一样，所以截图的文字说明里写的是实际宽高，点击坐标以截图像素为准。
- 非 ASCII 文本先写进剪贴板再粘贴（`xclip`），因为 `xdotool type` 打中文会丢字。
- **网页优先走 CDP。** `browser_*` 工具把页面上可操作的元素编上号（`data-osa-ref`），模型按编号点击、输入，
  返回的是页面文字而不是截图：更快、更省 token，也不会点偏。输入用 `Input.insertText`，中文不丢字。
  页面上有弹窗时只列弹窗里的元素并提示（登录、验证码就请人接手）。截图和 xdotool 留给浏览器以外的窗口、画布类页面和组合键。
- `cdp_helper.py` 第一次用时由服务端写进电脑的 `/tmp`（文件名带内容哈希，改了会自动更新），每个动作执行一次，
  所以两个后端都不需要额外开端口。DevTools 端口只监听电脑内部的 127.0.0.1；旧浏览器没开端口时脚本会重启它（登录状态在配置目录里，不会丢）。
- Worker 的上下文里只保留最近 2 张截图（`prune_images`），用来控制 token 成本。

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
继承 `Computer`，实现 `status / start / pause / exec / open_vnc` 这几个方法，然后在 `computer/__init__.py` 的 `get()` 里注册。桌面动作都基于 `exec` 加 xdotool，不需要重新实现。
