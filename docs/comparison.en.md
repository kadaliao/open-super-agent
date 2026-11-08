# Compared with OpenMausBot

[简体中文](comparison.md) · English

[OpenMausBot](https://github.com/milind-soni/OpenMausBot) is another open-source Grok Bot alternative that makes different trade-offs:

| | open-super-agent | OpenMausBot |
|---|---|---|
| Models | Any OpenAI-compatible API (DeepSeek, Kimi, Zhipu, Ollama, …), pay per token, daily spend visible | Mainly the `claude`, `codex` and `grok` CLIs on your machine, using subscriptions you already have; other endpoints through config |
| Computer | One shared by all Bots, several tasks at once in their own windows; your Chrome, local Docker (both free) or an Instabox cloud machine; browser and terminal, plus the whole desktop on a Mac; Bots on a server can borrow your Mac | One per Bot: a Boat cloud computer (paid after a trial), a local VM, or your own computer with whole-desktop control (macOS and Ubuntu Xorg) |
| Apps | One manifest file per app, connected with a login or token, including Feishu, DingTalk, WeCom and Tencent Docs; any MCP server too | 500+ apps through Composio (a third-party service with its own account); events in those apps can start a Bot; MCP too |
| Where you use it | Web, the macOS desktop app, the iPhone app (TestFlight, not public yet), plus Telegram, Feishu, DingTalk, WeCom and Discord | Desktop apps (macOS, Windows, Ubuntu), web, the iPhone app (on the App Store), an Android app (APK) |
| Phone notifications | Arrive while the app is closed and the phone locked, with the content on the lock screen; end-to-end encrypted through a relay that can't read them | Arrive while the app is open or briefly after it goes to the background; not once iOS suspends or closes it (their docs say this version doesn't do it) |
| Deployment | `docker compose up` on a server keeps it always on, so your phone and chat apps work with your computer off; or just the macOS desktop app on your computer | The desktop app embeds the server (phones connect to that computer, which must stay on and awake); `npx openmausbot serve` or Docker on a server; the paid MausBot Cloud |
| Voice | Dictation in the web UI | Spoken replies, voice calls with a Bot, dictation (ElevenLabs, Fish Audio, xAI, built-in Mac voices) |
| Calling it from elsewhere | Routines, webhooks | Routines, webhooks; an MCP server (direct Bots from Claude Desktop or Cursor); a CLI for scripts and CI |
| Installer | 43–49 MB (Python runtime included) | 250–317 MB |
| Secrets | Encrypted in a vault | Plain text in `config.json` (file mode 0600) |
| Usage analytics | None | Anonymous usage analytics, can be turned off in Settings |
| License | MIT | Apache 2.0 (`enterprise/` has its own license) |

OpenMausBot is the better fit if you:

- need Windows or Linux desktop apps, an Android app, or an iPhone app you can install from the App Store today;
- want voice conversations: spoken replies and calls with your Bots;
- want to use an existing Claude, ChatGPT or Grok subscription instead of paying per token;
- want Bots to control the whole desktop on Windows or Ubuntu, or each Bot to have its own computer;
- want to direct Bots from Claude Desktop, Cursor or scripts;
- want 500+ apps through Composio, or Bots that start when a new email or message arrives in an app.

open-super-agent is the better fit if you:

- want cheap models such as DeepSeek or Kimi, paid per token, with daily spend visible;
- want to talk to your Bots from Feishu, DingTalk, WeCom, Telegram or Discord, or connect Chinese apps;
- want it always on a server: it keeps working with your computer off, and the phone app gets notifications while closed;
- care about a small installer, encrypted secrets and no usage analytics.

Based on both projects' READMEs, docs (OpenMausBot's `docs/ios-companion.md`), release pages (OpenMausBot v0.1.146) and the App Store as of 2026-11-07.
