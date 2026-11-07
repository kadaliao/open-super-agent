# Compared with OpenMausBot

[简体中文](comparison.md) · English

[OpenMausBot](https://github.com/milind-soni/OpenMausBot) is another open-source Grok Bot alternative that makes different trade-offs:

| | open-super-agent | OpenMausBot |
|---|---|---|
| Models | Any OpenAI-compatible API (DeepSeek, Kimi, Zhipu, Ollama, …), pay per token, daily spend visible | Mainly the `claude`, `codex` and `grok` CLIs on your machine, using subscriptions you already have; other endpoints through config |
| Computer | One shared by all Bots, several tasks at once in their own windows; your Chrome, local Docker (both free) or an Instabox cloud machine; browser and terminal | One per Bot: a Boat cloud computer (paid after a trial), a local VM, or your own computer with whole-desktop control (macOS and Ubuntu Xorg) |
| Apps | One manifest file per app, connected with a login or token, including Feishu, DingTalk, WeCom and Tencent Docs; any MCP server too | 500+ apps through Composio (a third-party service with its own account); MCP too |
| Where you use it | Web, the macOS desktop app (preview), the iPhone app (in development), plus Telegram, Feishu, DingTalk, WeCom and Discord | Desktop app, web, iOS and Android apps |
| Deployment | `docker compose up` on a server; macOS desktop app (preview) | Desktop apps (macOS, Windows, Ubuntu); `npx openmausbot serve` or Docker on a server |
| Installer | 42–48 MB (Python runtime included) | 250–316 MB |
| Secrets | Encrypted in a vault | Plain text in `config.json` (file mode 0600) |

OpenMausBot is the better fit if you:

- need Windows or Linux desktop apps, or iOS and Android apps you can install today (our iPhone app is still in development);
- want voice conversations;
- want to use an existing Claude or ChatGPT subscription instead of paying per token;
- want Bots to control your whole desktop.

Based on both projects' READMEs and release pages as of 2026-11-07.
