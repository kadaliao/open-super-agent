# 和 OpenMausBot 比

简体中文 · [English](comparison.en.md)

[OpenMausBot](https://github.com/milind-soni/OpenMausBot) 也是开源的 Grok Bot 替代品，两边的取舍不一样：

| | open-super-agent | OpenMausBot |
|---|---|---|
| 模型 | 任何兼容 OpenAI 的接口（DeepSeek、Kimi、智谱、Ollama……），按 token 付费，每天花多少看得到 | 主要跑在本机的 `claude`、`codex`、`grok` 命令行上，用你已有的订阅；其他接口改配置接入 |
| 电脑 | 所有 Bot 共用一台，几个任务各开一个窗口同时做；用本机、本地 Docker（都免费）或 Instabox 云电脑；操作浏览器和终端，Mac 上还能操作整个屏幕；服务器上的 Bot 能用你的 Mac | 每个 Bot 一台：Boat 云电脑（试用后付费）、本地虚拟机，或者你的电脑（能控制整个桌面，支持 macOS 和 Ubuntu Xorg） |
| 应用 | 每个应用一份清单文件，登录或填 token 直接连，包括飞书、钉钉、企业微信、腾讯文档等国内应用；也能接任意 MCP | 经 Composio 接 500 多个应用（第三方服务，要它的账号）；应用里发生某件事时能叫 Bot 干活；也能接 MCP |
| 在哪用 | 网页、macOS 桌面 App、iPhone App（TestFlight 内测，还没公开），以及 Telegram、飞书、钉钉、企业微信、Discord | 桌面 App（macOS、Windows、Ubuntu）、网页、iPhone App（已上架 App Store）、Android App（APK） |
| 手机推送 | App 关着、手机锁着也能收到，锁屏上显示内容；经推送中转端到端加密，中转看不到内容 | App 开着、或者刚退到后台时能收到；被系统挂起或关掉后收不到（他们的文档写明这一版没有） |
| 部署 | `docker compose up` 放到服务器上一直开着，电脑关了手机和聊天软件照样能用；也可以只用 macOS 桌面 App 在本机跑 | 桌面 App 自带服务（手机连的是这台电脑，要开着、不能睡眠）；`npx openmausbot serve` 或 Docker 放到服务器上；付费的 MausBot Cloud |
| 语音 | 网页上的语音输入 | 念出回复、和 Bot 打电话、语音输入（ElevenLabs、Fish Audio、xAI、Mac 自带的声音） |
| 从别处调用 | 定时任务、webhook | 定时任务、webhook；MCP 服务（在 Claude Desktop、Cursor 里指挥 Bot）；命令行（脚本、CI 里跑） |
| 安装包 | 43–49 MB（自带 Python 运行环境） | 250–317 MB |
| 密钥 | 加密存在保险库里 | 明文存在 `config.json` 里（文件权限 0600） |
| 使用统计 | 不收集 | 匿名使用统计，可以在设置里关掉 |
| 许可证 | MIT | Apache 2.0（`enterprise/` 目录另有许可） |

这些情况选 OpenMausBot 更合适：

- 要 Windows、Linux 桌面 App，或者 Android App、现在就能从 App Store 装的 iPhone App；
- 要语音对话：念出回复、和 Bot 打电话；
- 想用现成的 Claude、ChatGPT、Grok 订阅，而不是按 token 付费；
- 想在 Windows、Ubuntu 上让 Bot 控制整个桌面，或者每个 Bot 一台单独的电脑；
- 想在 Claude Desktop、Cursor 里，或者用脚本指挥 Bot；
- 要用 Composio 接 500 多个应用，或者应用里有新邮件、新消息时自动叫 Bot 干活。

这些情况选 open-super-agent 更合适：

- 想用 DeepSeek、Kimi 这类便宜的模型，按 token 付费，每天花多少看得到；
- 要在飞书、钉钉、企业微信、Telegram、Discord 里直接和 Bot 说话，或者要连国内的应用；
- 想放在服务器上一直开着：电脑关了照样能用，手机上的 App 关着也能收到推送；
- 在意安装包大小、密钥加密保存，不想有使用统计。

对比依据两边 2026-11-07 的 README、文档（OpenMausBot 的 `docs/ios-companion.md`）、发布页（OpenMausBot v0.1.146）和 App Store。
