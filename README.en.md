<div align="center">

# open-super-agent

**A self-hosted, always-on AI teammate that chats, delegates work, and has a computer you can watch and take over.**

An open-source alternative to [Grok Bot](https://x.ai/bot). Bring your own model and pay per token.

[![CI](https://github.com/kadaliao/open-super-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kadaliao/open-super-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![Status](https://img.shields.io/badge/status-alpha-orange)

[简体中文](README.md) · English

<a href="docs/images/demo.mp4"><img src="docs/images/demo.gif" alt="Demo: Scout researches on the computer and hands off to Quill in a group chat; teaching a task; the library" width="860"></a>

<sub>Group handoff → teach a task → library (model wait times sped up) · <a href="docs/images/demo.mp4">HD MP4</a></sub>

</div>

## Features

- 🤖 **Multiple bots**: each with its own name, avatar, persona and conversation. Replies stream in, and the model's reasoning can be expanded or collapsed.
- 👥 **Group chats**: 2–6 Bots in one chat. `@` a Bot to have it answer, or `@everyone`; unaddressed messages go to the group's coordinator, who hands work to the right Bot. Bots hand off to each other (one researches, the next summarizes when the research is done). Approval requests stay in each Bot's private chat.
- ✉️ **Bots message each other**: no group needed. A Bot can DM another Bot for help ("ask Postman whether there's mail from Anthropic"); the other Bot works in its own chat, and the result comes back to the original conversation automatically.
- ⚡ **Background tasks**: workers run long jobs while you keep chatting. **Heavy mode** runs several workers on the same problem and has a reviewer merge their answers.
- 🖥️ **A computer**: one shared Linux desktop with Chromium and a terminal. Several tasks can use it at once, each in its own browser window, sharing logins and files instead of queueing. Web pages are driven directly over CDP (by element, reading page text), which is fast and precise. Watch it live and take over or hand back at any time; for captchas and logins, the bot asks you to step in. When it runs on your own computer, it can use your installed Chrome instead (with a separate profile, apart from your everyday one), no Docker needed.
- 🔌 **App market**: pick apps from the sidebar's Apps page. Notion, flomo, Tencent Docs, DingTalk, TickTick, Todoist and others connect with one click and an account login; Gmail, QQ Mail, Feishu, WeCom and GitHub take a single password, token or link; Slack takes a small app you create once; any MCP server works too. Mention one in chat with `@app`. Each app is a single manifest file, so [listing a new one](docs/apps.md) takes a PR, no code.
- ✅ **Approvals & vault**: write operations ask first (allow once / deny / always allow), with rules you manage in Settings; secrets are stored encrypted and never enter the chat.
- 🧩 **Skills**: reusable procedures invoked with `/name`. Ask a Bot to save what it just did as a skill, or edit skills in the side panel. With **Teach a task**, do it once yourself on the computer and the Bot turns the recording into a draft skill for you to review. Export a skill as a file or share it by link, and others install it by pasting the link; `SKILL.md` files from Claude and similar tools import as is.
- 🎁 **Bot templates**: start a new Bot from a ready-made template (news digest, inbox helper, GitHub watcher, trip planner) with skills and routines already set up. Turn a Bot you've tuned into a template and share it; [write one](docs/templates.md) (Chinese) and open a PR to make it built in.
- 🧠 **Memory**: each Bot remembers your preferences and the work it has done; basic facts about you (name, time zone) are shared by all Bots. Just say "remember ...", or view, edit, delete and import (e.g. from ChatGPT) in the side panel. Start a **new topic** to drop old messages from the context while keeping memories. Memories and the library are searchable by meaning, and Bots look up past notes and pages on their own.
- 📚 **Library**: finished reports and web pages are saved as Pages you can pin and share by link; browse, upload and download files on the shared computer, and attach files in chat.
- ⏰ **Routines**: per-Bot schedules (daily / weekdays, every N minutes, webhook) with pause, resume, test and the last 20 runs. Create one by saying "every day at 9, ...". Webhook events go through keyword filters, then a cheap model decides whether to act.
- 💬 **Chat apps**: talk to your Bots from Telegram, Feishu/Lark, DingTalk, WeCom or Discord, in the same conversation as the web UI. Approval requests arrive as buttons, and you can send files (and voice messages on DingTalk and WeCom). No public address needed ([setup guide](docs/channels.md), Chinese).
- 💻 **Desktop app (macOS preview)**: about a 42 MB download with its own runtime, no Docker or Python; Bots use the Chrome on your computer. It keeps running in the menu bar after you close the window and sends system notifications for approvals and finished tasks; it can also act as a client for your server.
- 📱 **Mobile**: install it to your home screen as an app, dictate messages by voice, and get push notifications for approvals and finished tasks when no page is open. Watch the screen, take over and type, and handle approvals from your phone.
- 💰 **Transparent cost**: token and money tracking per role, task and day, with a daily budget. A watchdog cancels tasks that stall, loop or exceed the budget.
- 📦 **Backup and migration**: export all data, secrets included, as one encrypted file and upload it on the new machine to carry on. Servers can also back up on a schedule from the command line.
- 🔄 **Any model**: add model providers in Settings (DeepSeek, OpenAI, OpenRouter, SiliconFlow, Alibaba Bailian, Zhipu, Kimi, Ollama, or any OpenAI-compatible endpoint), paste an API key and test it in one click; pick a model per role. Prices are fetched automatically.
- 🌐 **English and Chinese**: the UI, notifications and messaging-app messages come in both languages; switch in Settings. In the English UI, bots reply in English by default.

The architecture follows DeepSeek's Super-agent design: a Router that converses, Workers that execute, and a Watchdog that supervises.

## Who it's for

- **Install and go**: the macOS desktop app is about 42 MB and bundles its own runtime, so there is no Docker, Python or AI command-line tool to install first; Bots use the Chrome already on your computer. To keep it always on, one `docker compose up` puts it on a VPS or a machine at home, usable from the browser, your phone or a chat app, and the desktop app can connect to it.
- **Any model, with visible cost**: DeepSeek, Kimi, Zhipu, Alibaba Bailian, SiliconFlow, OpenRouter, Ollama on your own machine, or any OpenAI-compatible endpoint: paste a key in Settings and test it in one click. Chat and background tasks can use different models. Tokens and money are tracked per role, task and day, with a daily budget the watchdog enforces.
- **Works in mainland China**: talk to your Bots in Feishu/Lark, DingTalk or WeCom (Telegram and Discord too), with approvals as message buttons; Tencent Docs, flomo, TickTick and QQ Mail connect with a login or a token, no third-party relay. The UI comes in Chinese and English, and the default setup needs no paid foreign service.
- **One computer, shared, without queueing**: all Bots share one computer, so a site you sign in to once works for every Bot, and several tasks run at once, each in its own browser window. Pages are driven by element (reading page text, clicking numbered elements), which is faster, more precise and cheaper in tokens than guessing coordinates from screenshots. The computer can be your own Chrome (with a separate profile), a Linux desktop in local Docker, or an Instabox cloud machine; the first two are free.
- **Asks before acting**: write operations show a card first (allow once / deny / always allow), with rules you manage in Settings. With your own Chrome, uploading files, writing files and clicking "send / publish / pay" also ask first, and files stay inside the workspace folder. Secrets are encrypted in a vault and never enter the chat.
- **Works like a team**: Bots hand off work in group chats and DM each other for help; Heavy mode has several workers do the same job and cross-review; Teach a task turns what you do into a skill; finished reports go to the library with share links; and all your data exports as one encrypted file to move machines.

### Compared with OpenMausBot

[OpenMausBot](https://github.com/milind-soni/OpenMausBot) is another open-source Grok Bot alternative that makes different trade-offs:

| | open-super-agent | OpenMausBot |
|---|---|---|
| Models | Any OpenAI-compatible API (DeepSeek, Kimi, Zhipu, Ollama, …), pay per token, daily spend visible | Mainly the `claude`, `codex` and `grok` CLIs on your machine, using subscriptions you already have; other endpoints through config |
| Computer | One shared by all Bots, several tasks at once in their own windows; your Chrome, local Docker (both free) or an Instabox cloud machine; browser and terminal | One per Bot: a Boat cloud computer (paid after a trial), a local VM, or your own computer with whole-desktop control (macOS and Ubuntu Xorg) |
| Apps | One manifest file per app, connected with a login or token, including Feishu, DingTalk, WeCom and Tencent Docs; any MCP server too | 500+ apps through Composio (a third-party service with its own account); MCP too |
| Where you use it | Web, phone (home-screen app, push), the macOS desktop app, plus Telegram, Feishu, DingTalk, WeCom and Discord | Desktop app, web, iOS and Android apps |
| Deployment | `docker compose up` on a server; macOS desktop app (preview) | Desktop apps (macOS, Windows, Ubuntu); `npx openmausbot serve` or Docker on a server |
| Installer | 42–48 MB (Python runtime included) | 250–316 MB |
| Secrets | Encrypted in a vault | Plain text in `config.json` (file mode 0600) |

If you want Windows or Linux desktop apps, native phone apps or voice conversations, want to use an existing Claude or ChatGPT subscription instead of paying per token, or want Bots to control your whole desktop, OpenMausBot is the better fit. (Based on both projects' READMEs and release pages as of 2026-11-07.)

## Quick start

On a Mac, the quickest way is the **desktop app** (preview): download the dmg for your chip from the [releases page](https://github.com/kadaliao/open-super-agent/releases), open it and choose "Run on this computer". See [Desktop app](docs/desktop.md) (in Chinese).

To keep it always on a server, use Docker Compose; you only need Docker:

```bash
git clone https://github.com/kadaliao/open-super-agent.git && cd open-super-agent
cp .env.example .env     # optionally fill in DEEPSEEK_API_KEY, or add a key later in Settings → Models
docker compose up -d     # the first build takes 5–10 minutes; then open http://127.0.0.1:8765
```

The first time you open it, a short setup guide walks you through adding a model provider, meeting your Bot (or installing one from a template) and connecting an app.

See [Deploy with Docker Compose](docs/docker.md) (in Chinese) for automatic HTTPS, using Instabox as the computer, and backups.

Without Docker you need Python ≥ 3.11 with [uv](https://docs.astral.sh/uv/) and Node ≥ 20 (plus Docker for the local Docker computer; not needed with Instabox or your local Chrome):

```bash
export DEEPSEEK_API_KEY=sk-...              # optional, you can also add it in Settings → Models; see .env.example
(cd web && npm ci && npm run build)
uv run uvicorn osa.server:app --port 8765   # open http://127.0.0.1:8765
```

> [!WARNING]
> Login is off by default, so run it on localhost or a private network such as Tailscale. To put it on the public internet, set `OSA_PASSWORD` to turn on login and serve it over HTTPS. See [SECURITY.md](SECURITY.md).

## Documentation

The detailed docs are currently in Chinese:

- [Deploy with Docker Compose](docs/docker.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Desktop app (preview)](docs/desktop.md): the macOS app, running on your computer or connecting to your server
- [Chat apps](docs/channels.md): connecting Telegram, Feishu/Lark, DingTalk, WeCom and Discord
- [Adding an app to the market](docs/apps.md)
- [Writing a Bot template](docs/templates.md)
- [Roadmap](docs/roadmap.md): what comes next ([the Grok Bot parity plan](docs/grok-bot-parity.md) is archived)
- [Changelog](CHANGELOG.md)

## Roadmap

Everything needed to match Grok Bot shipped in v0.3.0 (see the [changelog](CHANGELOG.md)). Where it goes next:

- **App ecosystem**: new apps and Bot templates mostly come from community PRs; the project fills gaps in the manifest format and connection methods.
- **More clients**: a stable macOS desktop app, then Windows; native iOS and Android apps; using the bot in chat-app group chats.
- **Computers**: whole-desktop control (not just the browser), with Teach a task following along; letting Bots on a server borrow the browser on your computer; snapshots and rollback.
- **Bot abilities**: voice conversations.

The reasoning and priorities are in [docs/roadmap.md](docs/roadmap.md) (Chinese). Feature requests are welcome as issues.

## License

[MIT](LICENSE) © Kada Liao
