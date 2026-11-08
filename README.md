<div align="center">

# open-super-agent

**可以自己部署的常驻 AI 队友：会聊天，会派活，还有一台能看、能接手的电脑。**

开源，可替代 [Grok Bot](https://x.ai/bot)。模型自选，按 token 计费。

[![CI](https://github.com/kadaliao/open-super-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kadaliao/open-super-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![Status](https://img.shields.io/badge/status-alpha-orange)

简体中文 · [English](README.en.md)

<a href="docs/images/promo.mp4"><img src="docs/images/demo.webp" alt="功能一览：对话里的链接卡片和视频、针对一条消息开讨论串、后台任务边做边看、Bot 用自己的电脑填表、关键操作先问你、几个 Bot 拉群分工、Bot 之间私信、一句话画海报、定时任务、连接 40 个应用、教一遍就会的技能、记住你的偏好、每一笔花费" width="860"></a>

<sub>功能一览（无声，已加速）：链接卡片 · 讨论串 · 后台任务 · Bot 的电脑 · 先问再做 · 群聊 · Bot 私信 · 画图 · 定时任务 · 应用 · 技能 · 记忆 · 费用 · <a href="docs/images/promo.mp4">看完整宣传片（2 分钟，有配音）</a></sub>

</div>

## 亮点

- **会聊天，也会派活**：每个 Bot 有自己的人设、记忆和对话。耗时的活交给后台任务，你接着聊；几个 Bot 能拉群，一个查完资料，自动交给另一个写总结。
- **一台能看、能接手的电脑**：Bot 在电脑上查资料、填表、下载文件，你能实时看屏幕；遇到验证码、登录这类步骤，它请你来做。几个任务同时做，各开一个浏览器窗口，不用排队。
  电脑可以是 Bot 自己的，也可以就是你的 Mac：给了权限，它能用鼠标键盘操作微信、访达、备忘录这些 App，你一动鼠标它就停下。
- **连上你在用的应用**：应用市场收录了 40 个，Notion、Gmail、GitHub、飞书、钉钉、腾讯文档……登录或填 token 就能接，也能接任意 MCP。
- **先问再做**：发邮件、提交表单这类操作先弹卡片问你；密钥加密存在保险库里，不进聊天记录。
- **模型随便换，花多少看得到**：DeepSeek、Kimi、智谱、OpenAI、本机的 Ollama，任何兼容 OpenAI 的接口都行。按任务和天统计费用，有每日预算。
- **教一遍就会**：点“演示一遍”，在电脑上亲手做一次，Bot 把操作整理成技能，以后输入 `/名字` 就能重做；说“每天 9 点帮我……”就建好定时任务。
- **在国内好用**：在飞书、钉钉、企业微信里直接和 Bot 说话；默认配置不需要国外的付费服务；界面有中文和英文。
- **数据在你手里**：跑在你自己的机器上；全部数据（连同密钥）能导出成一个加密文件，换台机器接着用。

完整的功能见 [功能一览](docs/features.md)。

## 在哪用

| 用法 | 说明 |
|---|---|
| 💻 **macOS 桌面 App** | 下载 dmg 打开就能用：约 45 MB，自带运行环境，不用装 Docker 和 Python，Bot 直接用这台电脑：浏览器，给了权限还有屏幕和鼠标键盘。关掉窗口后在菜单栏接着跑；也能连到你的服务器，让服务器上的 Bot 用这台 Mac。见 [桌面 App](docs/desktop.md) |
| 🖥️ **服务器** | 一条 `docker compose up` 放到 VPS 或家里的机器上，一直开着；浏览器、桌面 App、手机、聊天软件都能连。见 [用 Docker Compose 部署](docs/docker.md) |
| 📱 **iPhone App**（TestFlight 内测） | 用相机扫电脑上的二维码就连上；要你确认的事、做完的任务推送到手机，内容加密，锁屏上就能看。见 [iPhone App](docs/mobile.md) |
| 💬 **聊天软件** | 在 Telegram、飞书、钉钉、企业微信、Discord 里直接和 Bot 说话，要你确认的操作变成消息里的按钮。见 [聊天软件](docs/channels.md) |

<p align="center">
  <img src="docs/images/macos-app.png" width="860" alt="macOS 桌面 App：左边是 Bot 列表，中间是 Nova 把出游清单写进文本编辑后的回复，右边的电脑面板是“本机”，实时显示这台 Mac 的屏幕">
</p>
<p align="center">
  <img src="docs/images/ios-chat.png" width="200" alt="iPhone App：对话">
  <img src="docs/images/ios-task.png" width="200" alt="iPhone App：后台任务做完，结果是一张杭州三个景点的对比表">
  <img src="docs/images/ios-approval.png" width="200" alt="iPhone App：确认卡片，允许一次、总是允许或拒绝">
  <img src="docs/images/ios-computer.png" width="200" alt="iPhone App：实时看 Bot 电脑的画面，随时接手">
</p>
<p align="center"><sub>上：macOS 桌面 App（<a href="docs/images/mac-control.mp4">看 Bot 操作整台 Mac 的录屏</a>）。下：iPhone App（TestFlight 内测），对话 · 后台任务 · 确认卡片 · 看 Bot 的电脑</sub></p>

同类项目 OpenMausBot 的取舍不一样，见 [和 OpenMausBot 比](docs/comparison.md)。

## 快速开始

在 Mac 上最快的办法是装**桌面 App**：到 [发布页](https://github.com/kadaliao/open-super-agent/releases) 下载对应芯片的 dmg，或者 `brew install --cask kadaliao/tap/open-super-agent`，打开后选“在这台电脑上运行”。

放到服务器上一直开着，用 Docker Compose，只需要装 Docker：

```bash
git clone https://github.com/kadaliao/open-super-agent.git && cd open-super-agent
cp .env.example .env     # 可以先填上 DEEPSEEK_API_KEY，也可以打开后在 设置 → 模型 里填
docker compose up -d     # 第一次要构建镜像，大约 5–10 分钟；打开 http://127.0.0.1:8765
```

第一次打开会有引导：接上一个模型服务、认识你的 Bot（或从模板装一个）、连一个常用应用，三步就能开始用。
自动 HTTPS、电脑改用 Instabox、国内镜像源、备份，见 [用 Docker Compose 部署](docs/docker.md)。

<details>
<summary>不用 Docker 运行</summary>

需要 Python ≥ 3.11 和 [uv](https://docs.astral.sh/uv/)、Node ≥ 20；本地 Docker 电脑仍要 Docker（用 Instabox 或本机时不需要，见 [配置 → 电脑](docs/configuration.md#电脑)）。

```bash
export DEEPSEEK_API_KEY=sk-...              # 可选，也可以打开后在 设置 → 模型 里填；环境变量一览见 .env.example
(cd web && npm ci && npm run build)         # 构建前端
uv run uvicorn osa.server:app --port 8765   # 打开 http://127.0.0.1:8765
```

第一次用电脑时会自动构建本地镜像，需要几分钟。国内网络可以先手动构建，用镜像源加速：

```bash
docker build --build-arg APT_MIRROR=mirrors.ustc.edu.cn -t osa-computer:1 osa/computer/image
```

</details>

> [!WARNING]
> 默认**不用登录**，只能在本机或内网（例如 Tailscale）使用。要部署到公网，先设置 `OSA_PASSWORD` 打开登录，再用 HTTPS 访问，见 [配置 → 部署到公网服务器](docs/configuration.md#部署到公网服务器) 和 [SECURITY.md](SECURITY.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [功能一览](docs/features.md) | 全部功能，按主题分组 |
| [用 Docker Compose 部署](docs/docker.md) | 一条命令跑起来、自动 HTTPS、数据和备份、国内网络 |
| [配置](docs/configuration.md) | 模型服务和各项工作用的模型、限额与单价、电脑、应用、确认规则与保险库、定时任务、推送、远程访问 |
| [桌面 App](docs/desktop.md) | 在这台电脑上运行或连接服务器、让服务器上的 Bot 用这台 Mac、系统通知、菜单栏、自动更新 |
| [iPhone App（内测）](docs/mobile.md) | 扫码连接、推送怎么送到手机、推送中转看得到什么、开发 |
| [聊天软件](docs/channels.md) | 连接 Telegram、飞书、钉钉、企业微信、Discord，配对，权限和常见问题 |
| [给应用市场加应用](docs/apps.md) | 应用清单的格式、图标、在本机试、提 PR |
| [写一个 Bot 模板](docs/templates.md) | 模板的格式、从现有 Bot 导出、加进内置 |
| [插件](docs/plugins.md) | 用 Python 给 Bot 加工具、订阅事件；安全说明和插件市场 |
| [架构](docs/architecture.md) | 各组件如何协作、代码目录、设计约定 |
| [开发](docs/development.md) | 本地开发、测试方法、调试技巧 |
| [和 OpenMausBot 比](docs/comparison.md) | 两个开源 Grok Bot 替代品的取舍 |
| [路线图](docs/roadmap.md) | 接下来的方向和先后顺序；做完的计划在 [docs/plans](docs/plans/) |
| [更新日志](CHANGELOG.md) | 各版本的变化 |
| [安全说明](SECURITY.md) | 现在的安全边界、怎么报告漏洞 |

## 路线图

- **客户端**：iPhone App 公开测试、上架 App Store；之后是 Windows 和 Android；在聊天软件的群里使用机器人。
- **电脑**：Windows 上控制整个桌面；快照与回滚。
- **应用生态**：应用和 Bot 模板主要靠社区提 PR 收录，项目补齐清单格式和连接方式上缺的能力。
- **Bot 能力**：语音对话。

每个方向的考虑和先后顺序见 [路线图](docs/roadmap.md)。想要的功能欢迎提 issue。

## 参与贡献

欢迎提 issue 和 PR，开始前请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 致谢

- DeepSeek Super-agent（2026-10 技术博客）：Router / Worker / Watchdog / Inline & Spill 的架构思路
- [Grok Bot](https://x.ai/bot)：产品形态和交互方式
- [Instabox](https://instabox.sh)、[noVNC](https://github.com/novnc/noVNC)、[Model Context Protocol](https://modelcontextprotocol.io)、[Tauri](https://tauri.app)

## 许可证

[MIT](LICENSE) © Kada Liao
