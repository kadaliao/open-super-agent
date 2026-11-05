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
- ⚡ **Background tasks**: workers run long jobs while you keep chatting. **Heavy mode** runs several workers on the same problem and has a reviewer merge their answers.
- 🖥️ **A computer**: one shared Linux desktop with Chromium and a terminal. Web pages are driven directly over CDP (by element, reading page text), which is fast and precise. Watch it live and take over or hand back at any time; for captchas and logins, the bot asks you to step in.
- 🔌 **App market**: pick apps from the sidebar's Apps page. Notion, flomo, Tencent Docs, TickTick, Todoist and others connect with one click and an account login; Gmail, QQ Mail and GitHub take a single password or token; any MCP server works too. Mention one in chat with `@app`.
- ✅ **Approvals & vault**: write operations ask first (allow once / deny / always allow), with rules you manage in Settings; secrets are stored encrypted and never enter the chat.
- 🧩 **Skills**: reusable procedures invoked with `/name`. Ask a Bot to save what it just did as a skill, or edit skills in the side panel. With **Teach a task**, do it once yourself on the computer and the Bot turns the recording into a draft skill for you to review.
- 🧠 **Memory**: each Bot remembers your preferences and the work it has done; basic facts about you (name, time zone) are shared by all Bots. Just say "remember ...", or view, edit, delete and import (e.g. from ChatGPT) in the side panel. Start a **new topic** to drop old messages from the context while keeping memories.
- 📚 **Library**: finished reports and web pages are saved as Pages you can pin and share by link; browse, upload and download files on the shared computer, and attach files in chat.
- ⏰ **Routines**: per-Bot schedules (daily / weekdays, every N minutes, webhook) with pause, resume, test and the last 20 runs. Create one by saying "every day at 9, ...". Webhook events go through keyword filters, then a cheap model decides whether to act.
- 📱 **Mobile**: install it to your home screen as an app, dictate messages by voice, and get push notifications for approvals and finished tasks when no page is open. Watch the screen, take over and type, and handle approvals from your phone.
- 💰 **Transparent cost**: token and money tracking per role, task and day, with a daily budget. A watchdog cancels tasks that stall, loop or exceed the budget.
- 🔄 **Any model**: any OpenAI-compatible API (DeepSeek, OpenRouter, Ollama, vLLM), configurable per role.

The architecture follows DeepSeek's Super-agent design: a Router that converses, Workers that execute, and a Watchdog that supervises.

## Quick start

Requirements: Python ≥ 3.11 with [uv](https://docs.astral.sh/uv/), Node ≥ 20, and Docker (for the local computer backend).

```bash
git clone https://github.com/kadaliao/open-super-agent.git && cd open-super-agent
export DEEPSEEK_API_KEY=sk-...              # see .env.example for all variables
(cd web && npm ci && npm run build)
uv run uvicorn osa.server:app --port 8765   # open http://127.0.0.1:8765
```

> [!WARNING]
> Login is off by default, so run it on localhost or a private network such as Tailscale. To put it on the public internet, set `OSA_PASSWORD` to turn on login and serve it over HTTPS. See [SECURITY.md](SECURITY.md).

## Documentation

The detailed docs are currently in Chinese:

- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Roadmap](docs/roadmap.md)
- [Changelog](CHANGELOG.md)

## Roadmap

- [x] P1 Bots & chat
- [x] P2 Computer (Instabox and local Docker backends, live view, takeover, task queueing)
- [x] P3 Approval rules, app management, a vault and `@app` mentions; browser control over CDP
- [x] P4 Skills & routines
- [x] P5 Memory & library
- [x] P6 Group chat
- [x] P7 Teach-a-task & mobile

## License

[MIT](LICENSE) © Kada Liao
