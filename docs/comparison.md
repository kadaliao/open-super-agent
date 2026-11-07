# 和 OpenMausBot 比

简体中文 · [English](comparison.en.md)

[OpenMausBot](https://github.com/milind-soni/OpenMausBot) 也是开源的 Grok Bot 替代品，两边的取舍不一样：

| | open-super-agent | OpenMausBot |
|---|---|---|
| 模型 | 任何兼容 OpenAI 的接口（DeepSeek、Kimi、智谱、Ollama……），按 token 付费，每天花多少看得到 | 主要跑在本机的 `claude`、`codex`、`grok` 命令行上，用你已有的订阅；其他接口改配置接入 |
| 电脑 | 所有 Bot 共用一台，几个任务各开一个窗口同时做；用你的 Chrome、本地 Docker（都免费）或 Instabox 云电脑；操作浏览器和终端 | 每个 Bot 一台：Boat 云电脑（试用后付费）、本地虚拟机，或者你的电脑（能控制整个桌面，支持 macOS 和 Ubuntu Xorg） |
| 应用 | 每个应用一份清单文件，登录或填 token 直接连，包括飞书、钉钉、企业微信、腾讯文档等国内应用；也能接任意 MCP | 经 Composio 接 500 多个应用（第三方服务，要它的账号）；也能接 MCP |
| 在哪用 | 网页、macOS 桌面 App（预览）、iPhone App（开发中），以及 Telegram、飞书、钉钉、企业微信、Discord | 桌面 App、网页、iOS / Android App |
| 部署 | `docker compose up` 放到服务器上；macOS 桌面 App（预览） | 桌面 App（macOS、Windows、Ubuntu）；`npx openmausbot serve` 或 Docker 放到服务器上 |
| 安装包 | 42–48 MB（自带 Python 运行环境） | 250–316 MB |
| 密钥 | 加密存在保险库里 | 明文存在 `config.json` 里（文件权限 0600） |

这些情况选 OpenMausBot 更合适：

- 现在就要 Windows、Linux 桌面 App，或者能装的 iOS、Android App（我们的 iPhone App 还在做）；
- 要语音对话；
- 想用现成的 Claude、ChatGPT 订阅，而不是按 token 付费；
- 想让 Bot 控制你的整个桌面。

对比依据两边 2026-11-07 的 README 和发布页。
