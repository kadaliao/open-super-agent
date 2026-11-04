# open-super-agent

自己部署的常驻 AI 队友，对标 xAI 的 [Grok Bot](https://x.ai/bot)，但模型可以自己选（任何 OpenAI 兼容接口：DeepSeek / OpenRouter / Ollama / vLLM），费用按实际 token 计算。
架构借鉴 DeepSeek 2026-10 的 Super-agent 设计：Router 负责对话，Worker 在后台干活，Watchdog 负责巡检。

**当前进度**：P1（Bot 与对话）和 P2（电脑）已完成，下一步是 P3（确认与应用）。详见下文的[路线图](#路线图)和 [`docs/PLAN.md`](docs/PLAN.md)。

---

## 快速开始

```bash
export DEEPSEEK_API_KEY=...                  # 默认用 DeepSeek；换提供方改 config.yaml
(cd web && npm install && npm run build)     # 构建前端（前端代码改动后才需要重跑）
uv run uvicorn osa.server:app --port 8765    # 打开 http://127.0.0.1:8765
```

- 第一次让 Bot 用电脑时会构建本地镜像 `osa-computer:1`，需要几分钟。国内网络可以加镜像源：
  `docker build --build-arg APT_MIRROR=mirrors.ustc.edu.cn -t osa-computer:1 osa/computer/image`
- 设置了 `INSTABOX_API_KEY` 时，电脑改用 Instabox 云主机（见[电脑](#电脑)）。

## 能做什么

| 功能 | 说明 |
|---|---|
| 多个 Bot | 每个 Bot 有名字、头像、人设和独立对话；带星标的是主 Bot。回复流式输出，思考过程可以折叠。 |
| 后台任务 | 需要查资料、多步操作的活由 Router 派给后台 Worker，你可以接着聊。任务以卡片显示进度和结果。Heavy 模式下多个 Worker 各自做一遍再互审汇总，只用在难题上。 |
| 电脑 | 所有 Bot 共用一台 Linux 桌面（Chromium 和终端）。右栏能实时看屏幕，也能接手和交还。简单操作由 Bot 在对话里直接做，长任务交给后台 Worker。遇到验证码会请你接手。 |
| 应用（MCP） | Notion、GitHub、文件系统等通过 MCP 接入。写操作先弹确认卡片，你允许后才执行。 |
| 自动触发 | 支持定时任务和 webhook。事件先过关键词过滤，再由便宜的模型判断要不要处理。 |
| 费用与安全 | 按角色、任务和天统计 token 与金额，按 DeepSeek 高峰和空闲两档计价，有每日预算。Watchdog 会取消超时、卡住、死循环或超预算的任务。 |

## 架构

```
浏览器（React SPA）── WebSocket 事件 + REST ──► FastAPI（osa/server.py）
                                                  │
     ┌────────────────────────────────────────────┼─────────────────────────────┐
     │ Router：每个 Bot 一个（osa/router.py）        │ 后台                         │
     │  · 和用户对话、派任务、看屏幕、做简单电脑操作      │  Worker：ReAct 循环（worker.py）│
     │  · 工具：dispatch / cancel / recall /         │  Watchdog：巡检（watchdog.py）  │
     │    look_at_screen / computer_*                │  Triggers：定时与 webhook       │
     └────────────────────────────────────────────┴─────────────────────────────┘
                 │                     │                        │
           Memory（SQLite）      Approvals（确认卡片）      Computer 接口（osa/computer）
           Inline & Spill                                  ├─ Instabox（云端微虚拟机）
                                                           └─ 本地 Docker（Xvfb + x11vnc）
```

### 目录

```
osa/
  server.py        HTTP / WebSocket 入口、REST 接口、VNC 桥、前端静态文件
  router.py        Bot 的常驻会话：提示词、流式输出、Router 工具
  worker.py        后台任务：Task 数据结构、ReAct 循环、Heavy 模式
  memory.py        SQLite：消息、对话、用量；Inline & Spill 上下文；费用计算
  bots.py          Bot 和对话的增删改查，旧数据迁移
  settings.py      界面里改的设置（覆盖 config.yaml）
  approvals.py     确认卡片：写操作确认、请你帮忙（kind=help）
  mcp_hub.py       MCP 应用连接、工具命名（<server>__<tool>）、deny / confirm 规则
  tools.py         内置工具：web_search、fetch_url
  triggers.py      定时和 webhook 触发器
  watchdog.py      任务巡检
  config.py        读取 config.yaml，按角色创建模型客户端
  computer/
    base.py        电脑接口和桌面动作（xdotool / scrot），控制权，任务排队
    local.py       本地 Docker 后端
    instabox.py    Instabox 后端
    tools.py       computer_* 工具定义、截图消息、给 Worker 的操作说明
    image/         本地电脑镜像（Dockerfile、start-desktop.sh）
web/src/           前端：store.ts（状态和 WebSocket）、components/（Chat、Computer、Settings…）
docs/PLAN.md       对标 Grok Bot 的完整计划和分阶段验收标准
config.yaml        模型、限额、价格、触发器、MCP、电脑
data/              运行时数据（不进 git）：osa.db、电脑的持久化目录
```

### 几个关键约定

- **Router 和 Worker 分工**：Router 要随时响应，所以只做几秒内能完成的事，包括简单的电脑操作。多步、耗时、要用终端或要等人的活一律派任务。派任务时只写用户要求的内容，不自行扩大范围。
- **电脑是共享资源**：一个任务从第一次用电脑开始独占，直到结束（`Computer.claim/release`），其他任务排队。Router 的直接操作只占用一步，电脑被任务占着时不等待，而是让模型决定要不要先取消那个任务。
- **控制权**：你接手期间，Bot 的桌面动作会一直等，直到你交还（`Computer.controller`）。
- **验证码不自动解**：遇到验证码、扫码、登录、短信验证时，Worker 立刻调用 `ask_human`，不自己尝试。
- **DeepSeek 思考模式**：请求带工具时，历史回复的 `reasoning_content` 必须原样传回，所以存在消息的 meta 里。
- **截图**：Worker 只保留最近 2 张图（`prune_images`），文字说明里带上实际分辨率，点击坐标按截图像素计算。
- **中文输入**：非 ASCII 文本先写进剪贴板再粘贴，因为 `xdotool type` 打中文会丢字。

## 电脑

| 后端 | 何时使用 | 说明 |
|---|---|---|
| Instabox | 设置了 `INSTABOX_API_KEY`（`computer.backend: auto`） | Firecracker 微虚拟机，电脑关机也照常跑，闲置自动暂停。免费版只能有一台机器，可以用 `computer.instabox_machine` 指定已有的那台。 |
| 本地 Docker | 没有 key | 容器名 `osa-computer`，数据在 `data/computers/shared`。VNC 只监听 127.0.0.1，由服务端转发给浏览器。 |

后端只需要实现 `status / start / pause / exec / open_vnc`，桌面动作统一在 `base.py` 里用 xdotool 完成。

## 配置

`config.yaml` 是默认配置，界面“设置”里改的值存在数据库，会覆盖它。

- `roles`：router / worker / reviewer / filter 各用哪个模型（默认都用 `deepseek-flash`）
- `limits`：步数、超时、确认等待时间、每日 token 预算
- `pricing`：单价（元 / 百万 token），分高峰和空闲两档
- `triggers`、`mcp_servers`、`computer`：见文件里的注释

接入 Notion：先建一个 internal integration，把页面分享给它，然后 `export NOTION_TOKEN=ntn_...`，并把 `mcp_servers.notion.enabled` 设为 `true`。其他 MCP server 照同样的格式加一项。

## 远程访问（Tailscale）

页面本身没有登录，只适合在 tailnet 内部使用。推荐让服务只监听本机，由 Tailscale 提供 HTTPS：

```bash
tailscale serve --bg --https=8443 8765    # 访问 https://<机器名>.<tailnet>.ts.net:8443
```

Webhook 经代理转发时，请求头必须带 `X-OSA-Token`（值等于环境变量 `OSA_WEBHOOK_TOKEN`）。

## 开发

- 前端开发：`cd web && npm run dev`（接口代理到 8765）。改完运行 `npm run build`，产物在 `osa/static/dist`，不进 git。
- 测试时不要碰真实数据：复制 `data/osa.db`，用 `OSA_CONFIG` 指向一份把 `memory.db` 改成副本的配置，在另一个端口启动。
- 端到端验证：通过 WebSocket 发 `{type:"send", chat_id, text}`，观察 `message` / `task` / `stream` 事件；界面用 Playwright 加系统 Chrome（`channel="chrome"`）截图检查。
- 改了 `osa/computer/image/` 之后需要重建镜像，并删掉旧容器 `docker rm -f osa-computer`。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 Bot 与对话 | 多 Bot、流式输出、任务卡片、设置、费用统计 | ✅ |
| P2 电脑 | 两个后端、实时画面、接手 / 交还、请你帮忙、共用电脑和任务排队、Router 直接操作 | ✅ |
| **P3 确认与应用** | 确认卡片 v2（允许一次 / 拒绝 / 总是允许）和规则管理；在界面里启用和配置 MCP 应用；保险库（加密保存密钥，不进聊天记录）；`@应用` | **下一步** |
| P4 技能与定时任务 | `/技能`、把对话保存为技能；每个 Bot 的定时任务和运行记录，取代现在的 `triggers` | |
| P5 记忆与资料库 | 每个 Bot 的长期记忆和共享的用户信息；“记住这个”；`/workspace` 文件和 Bot 生成的页面 | |
| P6 群聊 | 2–6 个 Bot 一起聊；`@` 和 `@everyone`；分工与交接 | |
| P7 演示一遍和移动端 | 录一遍操作生成技能；语音输入；PWA 和推送通知 | |

### P3 从哪里开始

1. `approvals.py`：增加 `rules` 表（pattern、action=allow|ask），`needs_confirm` 先查规则；卡片增加“总是允许”按钮，点了就生成一条规则。
2. 设置页新增“确认规则”标签：列出规则，可以删除和改成“先问我”。
3. `mcp_hub.py` 支持运行时增删 server；设置页的“应用”标签可以填写 command、args 和需要的环境变量名，并能启用、停用、重连。
4. 保险库：`vault` 表用环境变量里的主密钥加密；聊天里出现“请输入 key”的卡片，填写的值不经过模型，MCP 启动时注入。

### 已知问题和待改进

- Instabox 后端只在借用现有机器时测过，“自动新建机器”的路径还没在付费账号上验证。
- 页面没有登录鉴权，只能放在 tailnet 内部使用。
- Router 的简单电脑操作和后台任务共用一个浏览器，可能出现标签页互相干扰（目前靠任务排队来避免）。
- 前端打包后超过 500 KB（主要是 noVNC 和代码高亮），以后可以按需加载。
