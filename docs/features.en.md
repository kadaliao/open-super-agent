# Features

[简体中文](features.md) · English

The README lists the highlights; this is the full list. For setup see [Configuration](configuration.md), and for how it works see [Architecture](architecture.md) (both in Chinese).

## Bots and conversations

- **Multiple Bots**: each has its own name, avatar, persona and conversation. Replies stream in, and the model's reasoning can be expanded or collapsed.
  Upload an avatar, or have AI draw one from the Bot's name and role.
- **Threads**: reply to any message in a side thread to dig into it without derailing the main chat.
- **Group chats**: 2–6 Bots in one chat.
  - `@` a Bot to have it answer, or `@everyone`; unaddressed messages go to the group's coordinator, who hands work to the right Bot.
  - Bots hand off to each other: one researches, and the next writes the summary when the research is done.
  - Approval requests stay in each Bot's private chat.
- **Bots message each other**: no group needed. A Bot can DM another Bot for help ("ask Postman whether there's mail from Anthropic").
  The other Bot works in its own chat, and the result comes back to the original conversation automatically.
- **The main Bot creates Bots**: tell the main Bot "make a Bot that finds resources and saves them to my cloud drive". It writes the name, role and skills,
  shows you a card, and creates the Bot only when you click Create. Other Bots that need a new Bot ask the main Bot, and you still confirm.
- **Background tasks**: workers run long jobs while you keep chatting. **Heavy mode** has several workers do the same job, then cross-review and merge.
- **Memory**:
  - Each Bot remembers your preferences and the work it has done; basic facts about you (name, time zone) are shared by all Bots.
  - Just say "remember ...", or view, edit, delete and import (for example from ChatGPT) in the side panel.
  - Start a **new topic** to drop old messages from the context while keeping memories.
  - Memories and the library are searchable by meaning, and Bots look up past notes and pages on their own.

## The computer

- All Bots share one computer. Several tasks can use it at once, each in its own browser window, sharing logins and files instead of queueing.
- Web pages are driven directly over CDP, by element and by reading page text, which is faster, more precise and cheaper in tokens than guessing coordinates from screenshots.
- Watch it live and take over or hand back at any time; for captchas and logins, the Bot asks you to step in.
- The computer is one of these three; the first two are free:
  - this computer: the default in the desktop app, no Docker needed. Bots get their own browser window (a profile apart from your everyday one) and, once you grant the permissions, the whole screen, mouse and keyboard;
    when the service runs on a server, choose "This computer" in the Mac desktop app and the Bots on the server use that Mac;
  - a Linux desktop (Chromium and a terminal) in local Docker;
  - an [Instabox](https://instabox.sh) cloud machine.
- On this computer it is more careful: uploading files, writing files and clicking "send / publish / pay" all ask first, files stay inside the workspace folder, and the terminal is off by default;
  each task asks before it first uses the screen, and the Bot stops as soon as you move the mouse or press a key.

## Apps, skills and templates

- **App market**: pick apps from the sidebar's Apps page; 40 are listed today.
  - Notion, flomo, Tencent Docs, DingTalk, TickTick, Todoist and others: one click and an account login.
  - Gmail, QQ Mail, Feishu, WeCom and GitHub: a single password, token or link.
  - Slack: a small app you create once. Any MCP server works too.
  - Mention one in chat with `@app`. Each app is a single manifest file, so [listing a new one](apps.md) (Chinese) takes a PR, no code.
- **Skills**: reusable procedures invoked with `/name`.
  - Ask a Bot to save what it just did as a skill, or edit skills in the side panel.
  - **Teach a task**: do it once yourself on the computer, and the Bot turns the recording into a draft skill for you to review.
  - Export a skill as a file or share it by link, and others install it by pasting the link; `SKILL.md` files from Claude and similar tools import as is.
- **Bot templates**: start a new Bot from a ready-made template (news digest, inbox helper, GitHub watcher, trip planner) with skills and routines already set up.
  Turn a Bot you've tuned into a template and share it; [write one](templates.md) (Chinese) and open a PR to make it built in.

## Library, images and links

- **Library**: finished reports and web pages are saved as pages you can pin and share by link; browse, upload and download files on the shared computer.
- **Images and files**:
  - Attach files in chat: images show as thumbnails, other files as cards.
  - When the chat model can see images, the Bot looks at your image directly instead of starting a task.
  - Bots can also send screenshots, spreadsheets and documents from the computer into the chat.
- **Drawing**: say "draw a ...", and the Bot first shows a card with the prompt, model and price; once you allow it, the image arrives in seconds. Download it or use it as an avatar.
- **Link cards**: links in chat expand. YouTube and Bilibili play inline when clicked, posts on X show as embeds, and other pages show a title, summary and image.

## Routines

- Each Bot has its own routines: daily / on weekdays at a set time, every N minutes, or on a webhook.
- Pause, resume and test them; the last 20 runs are kept.
- Create one by saying "every day at 9, ...".
- Webhook events go through keyword filters, then a cheap model decides whether to act.

## Safety and cost

- **Asks before acting**: write operations show a card first (allow once / deny / always allow), with rules you manage in Settings.
- **Vault**: secrets are stored encrypted and never enter the chat.
- **Visible cost**: tokens and money tracked per role, task and day, with a daily budget.
- **Watchdog**: cancels tasks that stall, loop or exceed the budget.
- **Login**: set `OSA_PASSWORD` to require a login. With or without it, other websites can't use your browser to reach the service. See [SECURITY.md](../SECURITY.md).

## Where you use it

- **Web**: any browser, desktop or phone. On a phone you can also watch the screen, take over and type, handle approvals, and dictate messages.
- **macOS desktop app**:
  - About a 45 MB download with its own runtime, no Docker or Python; Bots use this computer (a browser, plus the screen, mouse and keyboard once you grant the permissions).
  - Keeps running in the menu bar after you close the window, with system notifications for approvals and finished tasks.
  - Can also act as a client for your server. See [Desktop app](desktop.md) (Chinese).
- **iPhone app** (TestFlight): connect by scanning a QR code on your computer with the Camera; pushes go through an encrypted relay, arrive even when the app is closed, show their content on the lock screen, and open the right conversation when tapped. See [iPhone app](mobile.md) (Chinese).
- **Chat apps**: talk to your Bots from Telegram, Feishu/Lark, DingTalk, WeCom or Discord, in the same conversation as the web UI.
  - Approval requests arrive as buttons, and you can send files (and voice messages on DingTalk and WeCom).
  - No public address needed ([setup guide](channels.md), Chinese).
- **Push notifications**: when no page is open, approvals and finished tasks are pushed to your phone or computer.

## Models, languages and data

- **Any model**: add model providers in Settings (DeepSeek, OpenAI, OpenRouter, SiliconFlow, Alibaba Bailian, Zhipu, Kimi, Ollama,
  or any OpenAI-compatible endpoint), paste an API key and test it in one click. Pick a model per role; prices are fetched automatically.
- **English and Chinese**: the UI, notifications and chat-app messages come in both languages; switch in Settings. In the English UI, Bots reply in English by default.
- **Backup and migration**: export all data, secrets included, as one encrypted file and upload it on the new machine to carry on;
  servers can also back up on a schedule from the command line.
