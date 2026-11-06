# 配置

配置分两层：仓库里的 `config.yaml` 是默认值；在界面“设置”里改的值存进数据库，会覆盖默认值。
用 `OSA_CONFIG=/path/to/config.yaml` 可以换一份配置文件（测试时常用）。

## 环境变量

| 变量 | 必需 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 否 | 默认配置用的 DeepSeek 的 key。也可以不设，在 设置 → 模型 里填 |
| `OSA_PASSWORD` | 部署到公网时必需 | 登录密码，设置后要先登录才能用；不设置则不用登录，只适合本机或内网 |
| `INSTABOX_API_KEY` | 否 | 设置后，电脑改用 Instabox 云主机 |
| `OSA_WEBHOOK_TOKEN` | 否 | webhook 鉴权；没设置时只接受本机发来的请求 |
| `OSA_CONFIG` | 否 | 配置文件路径，默认 `config.yaml` |
| `HTTPS_PROXY` | 否 | 服务器在国内、要用 Telegram 或 Discord 时设置，见 [聊天软件](channels.md) |
| `OSA_BACKUP_PASSPHRASE` | 否 | 命令行备份、恢复时用的口令，定时备份用，见 [备份与迁移](#备份与迁移) |

密钥放在环境变量或保险库里（界面上填的进保险库），`config.yaml` 只写变量名。

## 模型

第一次打开时的引导会带你接上一个模型服务、选一个模型（对话、后台任务、汇总、整理都先用它）。之后在 **设置 → 模型** 里改，改了马上生效。分两步：

1. **添加模型服务**：点“添加”，选一个服务商（DeepSeek、OpenAI、OpenRouter、硅基流动、阿里云百炼、智谱、Kimi、Ollama），
   或者选“其他”填任何兼容 OpenAI 的地址（vLLM、LM Studio、one-api……）。填上 API Key，点“测试连接”，能连上就会列出它有哪些模型。
   API Key 加密存在保险库（名字是 `LLM_KEY_<服务名>`），保险库页面里也看得到。
2. **给每项工作选模型**：对话（Bot 本身）、后台任务、Heavy 汇总、整理和过滤，各自从下拉框里选，可以搜索，也可以直接填名字。
   旁边会显示单价和能不能看图；点“测试”会真的调一次（带一张小图），告诉你能不能用、多快、能不能看图。

电脑功能要求“对话”和“后台任务”用的模型**能看图**。对话或后台任务的模型没填 API Key 时，对话页上方会提示去设置里配。

服务和模型旁边显示品牌图标：模型按名字认（`deepseek-…`、`qwen…`、`claude-…`、OpenRouter 的 `openai/gpt-…` 这类都认得），
认不出的用服务的图标；“其他”里添加的服务按接口地址认（火山引擎、Anthropic、Groq……），也认不出的显示一个服务器图标。

每项工作还可以调思考模式：默认、少一些、多一些、最多（`reasoning_effort`），或者“不思考”（`thinking: false`，更快更便宜）。
不是所有模型都认这两个参数，改了以后点一下“测试”确认。

`config.yaml` 里的写法照样能用，作为默认值；界面上改过的存在数据库里，优先于它。
`config.yaml` 里写的服务在界面上标着“config.yaml”，能改地址、填 API Key，但要删的话得在文件里删。

```yaml
providers:
  deepseek: { base_url: https://api.deepseek.com, api_key_env: DEEPSEEK_API_KEY }
roles:
  router:   { provider: deepseek, model: deepseek-flash }   # 对话
  worker:   { provider: deepseek, model: deepseek-flash }   # 后台任务
  reviewer: { provider: deepseek, model: deepseek-flash }   # Heavy 模式里负责汇总
  filter:   { provider: deepseek, model: deepseek-flash }   # 定时任务 webhook 事件的过滤、导入记忆
```

API Key 先看保险库（界面上填的），没有再看 `api_key_env` 写的环境变量。在界面上删掉填的 key，就回到用环境变量。
本机和内网地址（`localhost`、`host.docker.internal`、`192.168.*` 这类）不填 API Key 也算能用。

## 单价和限额

**单价自动获取**，不用自己填：每天从 [models.dev](https://models.dev) 拉一次（两百多家服务商、几千个模型），存在 `data/model-prices.json`。

- 按接口地址对上服务商，再按模型名字找单价；对不上的（比如自己搭的转发）按模型名字到各家找，设置页上会注明“按某某的价格估算”。
- models.dev 的单价是美元，按 `pricing.usd_rate`（默认 7.1）换成人民币。
- `config.yaml` 的 `pricing.models` 里写了的以它为准。DeepSeek 分高峰和空闲两档（`[高峰, 空闲]`），models.dev 没有，所以默认配置里写着。
- 本机和内网的模型按 0 计。找不到单价的也按 0 计，设置页上显示“没有单价”。
- 连不上 models.dev 时用上一次存下的；一次都没取到过的话，只有 `pricing.models` 里写了的有单价。国内服务器可以设 `HTTPS_PROXY`，
  或者用 `pricing.source` 换一个地址。费用页的“单价”下面能看到上次更新的时间，也能点“现在更新”。

```yaml
pricing:
  currency: "¥"
  usd_rate: 7.1                                  # 1 美元换多少人民币
  # source: https://models.dev/api.json          # 单价从哪里取
  peak_hours: ["09:00-12:00", "14:00-18:00"]     # 只对下面分两档的模型有用；北京时间，周一至周五
  models:
    deepseek-flash: {cache_hit: [0.04, 0.02], input: [2, 1], output: [8, 4]}
```

**限额**在 设置 → 限制与预算 里改：每天最多用多少 token、每个任务最多几步、任务最长跑多久、后台任务等确认多久、Heavy 模式同时跑几个。
对应 `config.yaml` 的 `limits`（`daily_token_budget`、`worker_max_steps`、`task_timeout_s`、`approval_timeout_s`，另外还有 `watchdog_interval_s`）和 `heavy.workers`。

点“测试”产生的调用记在“测试模型”下面，几分钱以内。

## 电脑

```yaml
computer:
  enabled: true
  backend: auto          # auto：有 INSTABOX_API_KEY 就用 Instabox，否则用本地 Docker；也可以指定 instabox / local
  parallel: 3            # 几个任务可以同时用电脑，多了排队；设置 → 限制与预算 里也能改
  width: 1280
  height: 800
  idle_pause_s: 900      # 闲置多少秒后自动暂停
  instabox_template: agent-browser
  instabox_machine: ""   # 使用已有的 Instabox 机器（免费版只能有一台）
```

| 后端 | 说明 |
|---|---|
| Instabox | Firecracker 微虚拟机，电脑关机也照常跑；暂停时保留内存。需要权限为 `machines:write` 的 API key。 |
| 本地 Docker | 容器 `osa-computer`，数据在 `data/computers/shared`（用 Docker Compose 部署时在命名卷 `osa-computer-data`，由环境变量 `OSA_COMPUTER_DATA` 指定）。VNC 只监听容器内部、不映射到宿主机端口，由服务端经 `docker exec` 转发给浏览器。 |

**几个任务同时用电脑**：所有 Bot 共用一台电脑，但后台任务不用一个个排队。每个任务在自己的浏览器窗口里做，
截图、点击、输入都只作用在自己的窗口里，互不干扰；登录状态、电脑里的文件大家共用。同时用的任务最多 `parallel` 个，多了排队，
任务卡片上会显示“排队等电脑”。终端命令和读写文件不算在里面。

- 画面上一次只显示一个窗口，跟着最近的操作切换。右栏电脑上方列着正在用电脑的任务，点一下画面就固定在它的窗口上，再点一下取消。
- 你接手电脑时，所有任务的浏览器操作都先停下，交还后接着做。“请你帮忙”的任务会先把自己的窗口放到最前面。
- 每多一个窗口，电脑大概多用 100–300 MB 内存。Instabox 免费版或者内存小的电脑，可以把 `parallel` 调成 2，或者 1（和以前一样一个个来）。
- 第一次升级到这个版本时，电脑里已经开着的浏览器会重开一次（加上防止后台窗口降速的参数），登录状态不受影响。

## 应用（MCP）

在侧栏的 **应用** 页里管理。市场里的应用分三种连接方式：

| 方式 | 应用 | 用户要做的 |
|---|---|---|
| 登录授权 | Notion、flomo、腾讯文档、Evernote、Dropbox、滴答清单、TickTick、Todoist、Linear、Sentry、Airtable、Canva，钉钉的文档、AI 表格、日历、待办、日志 | 点“连接”，在弹窗里登录、同意，弹窗自己关掉 |
| 建应用后登录 | Slack | 按步骤在对方后台建一个应用，填客户端 ID 和密钥，再登录 |
| 填一个密钥 | Gmail、QQ 邮箱、163 邮箱、flomo 快速记录、语雀、GitHub、高德地图、飞书云文档、企业微信的文档、日程、待办、百度网盘、秘塔AI搜索、知乎、快递100 | 按卡片上的步骤拿到密码 / token / 链接，粘贴后点“连接” |
| 免配置 | 文件夹、网页抓取、Exa 搜索、DeepWiki、Context7、Wolfram、12306 火车票 | 点“连接” |

市场里每个应用是一份清单（`osa/catalog/apps/<名字>.yaml`），写明介绍、能做什么、示例说法和连接模板；模板里的 `{字段}` 换成用户填的值，
标了 `secret` 的字段存进保险库，配置里只留 `{vault:名字}`，删除应用时一起删掉。
想加自己的应用，把清单放进 `data/apps/` 就行，刷新页面就能看到；想让大家都能用，提个 PR，见 [给应用市场加一个应用](apps.md)。

**登录授权**用远程 MCP 地址（Streamable HTTP）加 OAuth：先按 MCP 规范发现对方的授权服务器，用动态客户端注册（DCR）
自动拿一个 client_id，所以不用去对方平台建开发者应用。回调地址是 `<你访问的地址>/oauth/callback`，令牌加密存在保险库
（`OAUTH_<应用名>`），过期前自动刷新。对方吊销授权或注册失效时，应用显示“需要重新登录”，点一下会从头注册和授权。
不支持动态注册的（Slack），先在对方后台建一个应用，把客户端 ID 和密钥填进来再登录；连接页会显示要填到对方那里的回调地址。

几个注意点：

- 回调地址跟着你访问的地址走。换了访问地址（比如从 `localhost` 换到域名）后第一次登录会重新注册客户端，不影响已有的令牌。
- 有些服务只接受 https 或 `localhost` 的回调地址。用 `http://内网 IP` 访问时，登录授权可能被对方拒绝，换成 https 或本机访问即可。
- flomo 的登录授权需要 MAX 会员；只有 PRO 的话用“flomo 快速记录”（只能新建笔记）。
- 邮箱走 IMAP/SMTP（`uvx mcp-email-server`），添加前会先试登录一次。Gmail 要用应用专用密码（需要开两步验证），QQ 和 163 用授权码。
- 有几个应用的授权会过期，到期要回对方那里重新授权：飞书云文档和企业微信 7 天，百度网盘 30 天。
- 钉钉要用加入了企业或组织的账号登录；企业微信要先在企业微信里建一个“智能机器人”。
- Google 云端硬盘、日历还没有收录：Google 官方的 MCP 只对加入了开发者预览的 Workspace 账号开放，社区的方案要另外做一套代为登录。

市场里没有的，点页面底部的“添加自定义应用”：远程的填地址（可选 Token 或登录授权），本机的填启动命令、参数和环境变量。

应用配置存在数据库里。`config.yaml` 的 `mcp_servers` 只在第一次启动时导入一次，之后改界面即可：

```yaml
mcp_servers:
  mytools:
    enabled: true
    command: npx
    args: ["-y", "some-mcp-server"]
    env_from: [SOME_TOKEN]     # 导入后变成 env:SOME_TOKEN，值从服务的环境变量读取
    deny: [delete, archive]    # 名字里含这些词的工具不会提供给模型
    confirm: auto              # auto：写操作需要确认 | all：全部需要确认 | none：都不需要 | 关键词列表
  linear:
    enabled: true
    url: https://mcp.linear.app/mcp
    oauth: true                # 导入后在“应用”页点“重新登录”完成授权；也可以写 headers 带 token
```

配置里的值有几种写法：明文、`env:名字`（服务的环境变量）、`vault:名字`（保险库），或在字符串中间嵌 `{vault:名字}`
（比如 `Authorization: Bearer {vault:GITHUB_TOKEN}`）。在界面里填的密钥都会自动存进保险库。

## 确认规则与保险库

- **确认规则**（设置 → 确认规则）：按工具全名匹配，支持 `*`，如 `filesystem__*`。“先问我”优先于“自动允许”；都不命中时按应用的 `confirm` 策略。确认卡片上点“总是允许”会自动加一条规则。
- **过期**：只有定时 / webhook 触发的后台任务，卡片在 `approval_timeout_s` 后按拒绝处理；对话里的任务一直等。
- **保险库**（设置 → 保险库）：Fernet 加密存在 SQLite。主密钥取 `OSA_VAULT_KEY`，没设置时自动生成 `data/vault.key`（权限 600）。换主密钥后旧密钥解不开，需要重新录入。

## 定时任务

定时任务属于各个 Bot，在右栏“定时”里管理，或在对话里说“每天 9 点帮我……”。触发后直接派一个后台任务，结果发到 Bot 的对话里。

- **每天 / 每周几**：一个或多个 `HH:MM`，可选周几（不选就是每天）。
- **每隔一段时间**：至少 5 分钟。
- **Webhook**：外部系统 POST 到 `/hooks/<名字>`。可以设关键词（至少包含一个 / 包含就忽略），以及一句话规则让 `filter` 角色的模型判断要不要处理。

每个 Bot 最多 50 个，每个保留最近 20 次运行记录；上一次还没跑完时这一次跳过。服务停着时错过的定时只补跑一次。

**没有新内容就不打扰**：到点运行和 webhook 触发时，Bot 判断这次没有值得告诉你的新东西（没有新邮件、和上次一样、事件不需要处理），
结果只写 `[无新内容]`。这时不发结果、不推送、不发到聊天软件，对话里也不显示这个任务，只在运行记录里记一笔“没有新内容”。
按时间触发的还会带上上次有内容的那次结果（最多 2000 字）给 Bot 对比，已经告诉过你的不再重复。
这两段只给后台任务看，不显示在任务卡片上。点“测试”时照常发出结果，也不和上次比。
旧版 `config.yaml` 里的 `triggers` 会在首次启动时导入成主 Bot 的定时任务，之后改配置文件不再生效。

```bash
curl -X POST localhost:8765/hooks/github -H 'content-type: application/json' \
  -H "X-OSA-Token: $OSA_WEBHOOK_TOKEN" -d '{"issue":{"title":"Bug: ..."}}'
```

## 手机、语音与推送

- **要用 https 访问**：推送、添加到主屏幕、语音输入都要求安全连接（本机 `localhost` 也算）。手机上建议用下面“远程访问”里 Tailscale 的 https 地址。
- **推送**：在每台设备的“设置 → 通知”里开启。iPhone / iPad 要先在 Safari 里“添加到主屏幕”，从主屏幕打开再开启（iOS 16.4 起支持）。
  VAPID 密钥第一次用时自动生成，存在数据库里；换数据库后各设备要重新开启。可以设置联系地址（推送服务出问题时可能用它联系你）：

  ```yaml
  push:
    subject: "mailto:you@example.com"   # 默认是项目的 GitHub 地址
  ```
- **语音输入**用浏览器自带的识别：Safari（含 iPhone）直接可用；Chrome 要能连上 Google 的语音服务，连不上时会提示。不支持的浏览器不显示麦克风。
- **演示一遍**最长 10 分钟（`osa/teach.py` 的 `MAX_S`），到时自动结束并生成草稿。

## 聊天软件

在 设置 → 聊天软件 里添加，不需要改配置文件。步骤、权限和常见问题见 [聊天软件](channels.md)。

## 群聊

不需要配置。限制：每个群 2–6 个 Bot，最多 50 个群；每一轮（用户的一条消息或一次任务交接）最多 8 次 Bot 发言（`osa/groups.py` 的 `MAX_TURNS`）。
群里的模型调用都记在各个 Bot 的 `router` 角色下。

## Bot 之间的私信

不需要配置。Bot 可以私信另一个 Bot 请它帮忙（不用拉群），对方的回复会自动回到原来的对话。
为了防止 Bot 之间来回停不下来，一串私信最多转 5 手（`osa/dm.py` 的 `MAX_DEPTH`），到了就停下来告诉你。每次回复都算用量。

## 记忆与资料库

不需要额外配置，下面这些是代码里的上限：

| 项目 | 上限 |
|---|---|
| 每个 Bot 自己的记忆 | 200 条，每条 500 字；80 条以内全部写进提示词，更多时写最近的 60 条，再加上和这次对话相关的更早记忆（最多 20 条） |
| 共享的用户事实 | 50 条 |
| 做过的事 | 每个 Bot 保留最近 20 条，提示词里带最近 10 条 |
| 页面 | 共 1000 个，每个 50 万字 |
| 上传文件 | 每个 20 MB，存到电脑的 `/data/workspace`（附件在 `uploads/` 下） |

“从其他助手导入”用 `filter` 角色的模型把粘贴的内容拆成条目，费用记在这个角色下。

**检索**：Bot 用 `search_memory` 按意思查记忆和资料库，用 `read_page` 读页面全文（对话和后台任务都能用）；
资料库和右栏“记忆”上面也有搜索框。

- 不用配置就能按关键词找：中文按两个字一组建索引（SQLite FTS5），英文、数字按词。
- 选一个 embedding 模型后，说法不同、意思相近的也能找到（设置 → 模型 最下面，可以点“测试”；或者 `config.yaml` 的 `embedding`）。
  推荐硅基流动的 `BAAI/bge-m3`（免费）、OpenAI 的 `text-embedding-3-small`、本机 Ollama 的 `nomic-embed-text`。
  向量在后台算，页面按段切成约 600 字一块，每个页面最多算前 200 块。换了模型会全部重算。
  向量服务出错时自动退回只按关键词找，设置页上会显示错误。
- 索引放在同一个数据库里（`search_docs`、`search_fts`），记忆和页面改了之后下一次检索时自动更新，不用手动重建。
`memory.inline_messages`（默认 20）控制对话里留在上下文的消息条数：每满这么多条，更早的那一批整体移出（只留编号和开头，Bot 要看原文时用 `recall`），所以上下文里有 20 到 39 条。不是每来一条就挪一格，这样前面的对话逐字不变，能一直命中模型服务的缓存。点“新话题”之后从零算起。

**任务过程**：后台任务每一步做了什么（思考、搜索、读网页、操作电脑、写结果）都会记下来，在右栏“任务”里实时看、事后回看。
默认留最近 90 天，`memory.task_log_days` 可以改，`0` 表示一直留着。任务结果在对话里，不受影响。

## 备份与迁移

设置 → **备份与迁移**。换机器、从本机搬到服务器、升级前留一份，都用它。

- **导出**：设一个口令（至少 8 个字符），下载一个 `.osabak` 文件。里面有 Bot、对话、记忆、技能、定时任务、资料库页面、应用配置、
  模型服务和各项工作用的模型、确认规则、聊天软件连接、费用记录、保险库里的密钥（含模型的 API Key），以及 `data/apps/` 里本机的应用清单。
  文件用口令加密（scrypt + AES-256-GCM），口令忘了就打不开，请另外记好。
- **恢复**：在新的那台上传文件、输口令，先解密检查，给你看里面有什么；点“恢复并重启”后服务自动重启，换上备份里的数据。
  - 原来的数据库留在 `data/osa.db.before-restore-<时间>`，恢复错了可以停掉服务换回去。
  - 密钥用新机器自己的主密钥重新加密，两台的 `OSA_VAULT_KEY`（或 `data/vault.key`）不一样也没关系。
  - 聊天软件连接会先停用：确认原来那台停掉以后，在 设置 → 聊天软件 里启用，不然两边抢同一个机器人。原来那台还开着的话，定时任务也会跑两遍。
  - 网页地址（设置 → 聊天软件 最下面）是原来那台的，换了域名记得改。
- **不在备份里**：电脑（共用电脑里的文件、浏览器的登录状态），新机器按自己的设置开电脑；登录用的 `session.key`；日志。
- 服务没有自己的守护进程时（直接 `uv run uvicorn`），重启是重新执行同样的命令；`--reload` 开发模式下要手动重启。

命令行（服务开着也能导出；恢复在下次启动时生效）：

```bash
uv run python -m osa.backup export -o ~/backups          # 会问口令；或者设 OSA_BACKUP_PASSPHRASE
uv run python -m osa.backup restore ~/backups/osa-backup-20261106-0835.osabak
```

命令行要和服务用同样的环境变量（`OSA_CONFIG`、`OSA_VAULT_KEY`），主密钥对不上时会拒绝，免得备份里带不上密钥。
每天定时备份，可以在 crontab 里写：

```bash
0 3 * * * cd /opt/osa/app && OSA_BACKUP_PASSPHRASE=… .venv/bin/python -m osa.backup export -o /opt/osa/backups
```

放在 nginx 后面时，网页上传的备份受 `client_max_body_size` 限制；备份比它大，就调大它，或者用命令行恢复。

## 登录

设置环境变量 `OSA_PASSWORD` 后，打开页面会先要求输入密码。登录保持 30 天，期间用着会自动续期；设置 → 右上角“退出登录”。

- 不用登录就能访问的只有：登录页、分享链接（页面 `/s/<token>`、技能 `/skill/<token>`、Bot 模板 `/template/<token>`）、webhook（`/hooks/*`，用 `OSA_WEBHOOK_TOKEN` 鉴权）、前端的静态文件。
- 同一个 IP 10 分钟内输错 5 次，之后 10 分钟内不能再登录。
- 会话签名用的密钥在 `data/session.key`（权限 600）。改密码或删掉这个文件，所有设备都要重新登录。
- 放在反向代理（Caddy、nginx）后面时，启动 uvicorn 要加 `--proxy-headers --forwarded-allow-ips 127.0.0.1`，
  这样限速按真实 IP 计算，cookie 也会带上 `Secure`。

## 远程访问（Tailscale）

服务只监听本机，由 Tailscale 提供 HTTPS：

```bash
uv run uvicorn osa.server:app --port 8765
tailscale serve --bg --https=8443 8765     # 访问 https://<机器名>.<tailnet>.ts.net:8443
```

如果改用 `--host 0.0.0.0` 直接访问，地址必须写 `http://`。浏览器自动升级成 https 时，服务端会报 `Invalid HTTP request`。

## 部署到公网服务器

用 Docker Compose 的话，自带一个自动申请证书的 Caddy，见 [用 Docker Compose 部署](docker.md#部署到公网自动-https)。下面是不用 Docker 的做法。

先设置 `OSA_PASSWORD`。服务仍然只监听本机，由 Caddy 之类的反向代理提供 HTTPS：

```bash
OSA_PASSWORD=... uv run uvicorn osa.server:app --host 127.0.0.1 --port 8765 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

```caddyfile
osa.example.com {
	reverse_proxy 127.0.0.1:8765 {
		flush_interval -1   # 回复是流式输出的，不要缓冲
	}
}
```

用 nginx 的话，证书自己申请（比如 `certbot certonly --nginx -d osa.example.com`），放在 `/etc/nginx/conf.d/osa.conf`：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name osa.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name osa.example.com;
    ssl_certificate     /etc/letsencrypt/live/osa.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/osa.example.com/privkey.pem;

    client_max_body_size 25m;          # 上传文件最大 20 MB，nginx 默认只收 1 MB

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;            # 对话和电脑画面走 WebSocket
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $http_host;                  # 要带端口：服务拿它和 Origin 比，判断是不是跨站请求
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;        # 服务据此给 cookie 加 Secure、生成 https 的授权回调地址
        proxy_buffering off;                               # 回复是流式输出的，不要缓冲
        proxy_read_timeout 1h;                             # 默认 60 秒，生成技能草稿这类慢请求会被掐断
        proxy_send_timeout 1h;
    }
}
```

改完 `nginx -t && systemctl reload nginx`。

服务器内存小或者不想装 Docker 时，电脑用 Instabox：设置 `INSTABOX_API_KEY`，并把 `computer.backend` 写成 `instabox`。

应用（MCP）里用 `npx` 的（文件系统、GitHub、Notion）要装 Node ≥ 20，用 `uvx` 的（网页抓取）要装 uv。
Ubuntu 18.04 这类 glibc 低于 2.28 的系统跑不了官方的 Node，可以用 [unofficial-builds](https://unofficial-builds.nodejs.org/download/release/) 里的 `linux-x64-glibc-217` 版本。
用 systemd 运行时，服务的 PATH 里默认没有这些命令所在的目录，要在 unit 里写上，例如：

```ini
Environment=HOME=/root PATH=/opt/node/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```
