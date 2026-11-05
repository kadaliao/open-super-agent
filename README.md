<div align="center">

# open-super-agent

**可以自己部署的常驻 AI 队友：会聊天，会派活，还有一台能看、能接手的电脑。**

开源，可替代 [Grok Bot](https://x.ai/bot)。模型自选，按 token 计费。

[![CI](https://github.com/kadaliao/open-super-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kadaliao/open-super-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![Status](https://img.shields.io/badge/status-alpha-orange)

简体中文 · [English](README.en.md)

<a href="docs/images/demo.mp4"><img src="docs/images/demo.gif" alt="演示：群聊里 Scout 在电脑上查资料后交给 Quill 写介绍；演示一遍生成技能；资料库" width="860"></a>

<sub>群聊交接 → 演示一遍生成技能 → 资料库（等待模型的部分已加速）· <a href="docs/images/demo.mp4">看高清版 MP4</a></sub>

</div>

## 为什么做这个

Grok Bot 的体验很好，但价格高，模型也换不了。open-super-agent 的交互方式参照 Grok Bot：常驻的 Bot、后台任务、一台能实时观看的电脑。架构借鉴 DeepSeek 的 Super-agent 设计（Router / Worker / Watchdog）。部署在你自己的机器上，模型和花费由你决定。

## 功能

- 🤖 **多个 Bot**：每个 Bot 有名字、头像、人设和独立对话；回复流式输出，思考过程可以折叠。
- 👥 **群聊**：2–6 个 Bot 一个群。`@` 谁谁回答，`@所有人` 大家都说；不 @ 的消息由协调者接，它会分给合适的 Bot。Bot 之间能交接工作，比如一个查完资料，自动交给另一个写总结。要你确认的操作只出现在各自的私聊里。
- ⚡ **后台任务**：耗时的活交给 Worker 在后台做，你可以接着聊。**Heavy 模式**下多个 Worker 各自做一遍，再互审汇总。
- 🖥️ **电脑**：所有 Bot 共用一台 Linux 桌面（Chromium 和终端）。网页通过 CDP 直接控制，按元素操作、读页面文字，又快又准；你能实时看屏幕，随时接手和交还；遇到验证码、登录这类步骤，Bot 会请你来做。
- 🔌 **应用市场**：侧栏“应用”里挑应用连接。Notion、flomo、腾讯文档、滴答清单、Todoist 等点一下、用账号登录就行；Gmail、QQ 邮箱、GitHub 填一个密码或 token；也能接任意 MCP server。聊天里用 `@` 指定。每个应用是一份清单文件，[写一份提个 PR](docs/apps.md) 就能上架。
- ✅ **确认与保险库**：写操作先弹卡片问你（允许一次 / 拒绝 / 总是允许），规则可在设置里管理；密钥加密保存，不进聊天记录。
- 🧩 **技能**：输入 `/名字` 调用可复用的做事方法；可以让 Bot 把刚才的做法存成技能，也能在右栏编辑。点**演示一遍**，在电脑上亲手做一次，Bot 把录下的操作整理成技能草稿给你审核。
- 🧠 **记忆**：每个 Bot 记得你的偏好和它做过的事；名字、时区这类关于你的基本事实所有 Bot 共享。说“记住……”就行，也能在右栏查看、修改、删除，或从 ChatGPT 等助手导入。聊久了可以点“新话题”，之前的对话不再带进上下文，记忆还在。
- 📚 **资料库**：Bot 做好的报告和网页存成页面，可以置顶、生成分享链接；共用电脑里的文件可以浏览、上传、下载。聊天时也能直接发附件。
- ⏰ **定时任务**：每个 Bot 自己的定时任务（每天 / 每周几定时、每隔一段时间、webhook），支持暂停、恢复、测试，保留最近 20 次运行记录。在对话里说“每天 9 点帮我……”就能建。webhook 事件先过关键词过滤，再交给便宜的模型判断要不要处理。
- 💬 **聊天软件**：在 Telegram、飞书里直接和 Bot 说话，和网页上是同一个对话；要你确认的操作变成消息里的按钮，发文件也行。服务器不需要公网地址，[几步就能连上](docs/channels.md)。
- 📱 **手机**：能添加到主屏幕当应用用；有语音输入；不开着页面时，需要你确认的事、做完的任务会推送到手机。手机上也能看屏幕、接手电脑打字、处理确认。
- 💰 **费用透明**：按角色、任务和天统计 token 和金额，有每日预算；Watchdog 会自动取消卡住、死循环或超预算的任务。
- 🔄 **模型自选**：支持任何 OpenAI 兼容接口（DeepSeek、OpenRouter、Ollama、vLLM），每个角色可以单独配置模型。

## 快速开始

需要 Python ≥ 3.11 和 [uv](https://docs.astral.sh/uv/)、Node ≥ 20、Docker（本地电脑用；用 Instabox 时不需要）。

```bash
git clone https://github.com/kadaliao/open-super-agent.git && cd open-super-agent
export DEEPSEEK_API_KEY=sk-...              # 环境变量一览见 .env.example
(cd web && npm ci && npm run build)         # 构建前端
uv run uvicorn osa.server:app --port 8765   # 打开 http://127.0.0.1:8765
```

第一次用电脑时会自动构建本地镜像，需要几分钟。国内网络可以先手动构建，用镜像源加速：

```bash
docker build --build-arg APT_MIRROR=mirrors.ustc.edu.cn -t osa-computer:1 osa/computer/image
```

> [!WARNING]
> 默认**不用登录**，只能在本机或内网（例如 Tailscale）使用。要部署到公网，先设置 `OSA_PASSWORD` 打开登录，再用 HTTPS 访问，见 [配置 → 部署到公网服务器](docs/configuration.md#部署到公网服务器) 和 [SECURITY.md](SECURITY.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [配置](docs/configuration.md) | 模型和角色、限额与价格、电脑后端、应用、确认规则与保险库、定时任务、远程访问 |
| [架构](docs/architecture.md) | 各组件如何协作、代码目录、设计约定 |
| [开发](docs/development.md) | 本地开发、测试方法、调试技巧 |
| [聊天软件](docs/channels.md) | 连接 Telegram、飞书，配对，权限和常见问题 |
| [给应用市场加应用](docs/apps.md) | 应用清单的格式、图标、在本机试、提 PR |
| [路线图](docs/roadmap.md) | 接下来的方向和先后顺序；之前对齐 Grok Bot 的计划见 [这里](docs/grok-bot-parity.md) |
| [更新日志](CHANGELOG.md) | 各版本的变化 |

## 路线图

和 Grok Bot 对齐的功能已经全部做完（v0.3.0，见 [更新日志](CHANGELOG.md)）。接下来的方向：

- **应用生态**：收录更多应用（飞书、钉钉、Google 云端硬盘和日历……）；技能和 Bot 模板可以导出、分享、一键安装。
- **更多客户端**：macOS / Windows 桌面端（菜单栏、快捷键、系统通知，还能把你自己的电脑借给 Bot 用）；iOS / Android 原生 App；Telegram、飞书之外，再接企业微信、钉钉、Discord。
- **电脑**：多台电脑并行；“演示一遍”从浏览器扩展到整个桌面；快照与回滚。
- **Bot 能力**：记忆和资料库按语义检索；语音对话。
- **好部署、好上手**：Docker Compose 一键部署；界面英文版；数据备份与迁移。

每个方向的考虑和先后顺序见 [docs/roadmap.md](docs/roadmap.md)。想要的功能欢迎提 issue。

## 参与贡献

欢迎提 issue 和 PR，开始前请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 致谢

- DeepSeek Super-agent（2026-10 技术博客）：Router / Worker / Watchdog / Inline & Spill 的架构思路
- [Grok Bot](https://x.ai/bot)：产品形态和交互方式
- [Instabox](https://instabox.sh)、[noVNC](https://github.com/novnc/noVNC)、[Model Context Protocol](https://modelcontextprotocol.io)

## 许可证

[MIT](LICENSE) © Kada Liao
