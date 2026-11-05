# 配置

配置分两层：仓库里的 `config.yaml` 是默认值；在界面“设置”里改的值存进数据库，会覆盖默认值。
用 `OSA_CONFIG=/path/to/config.yaml` 可以换一份配置文件（测试时常用）。

## 环境变量

| 变量 | 必需 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 默认配置下必需 | 模型提供方的 key；换用别的提供方时，改 `providers.*.api_key_env` |
| `OSA_PASSWORD` | 部署到公网时必需 | 登录密码，设置后要先登录才能用；不设置则不用登录，只适合本机或内网 |
| `INSTABOX_API_KEY` | 否 | 设置后，电脑改用 Instabox 云主机 |
| `OSA_WEBHOOK_TOKEN` | 否 | webhook 鉴权；没设置时只接受本机发来的请求 |
| `OSA_CONFIG` | 否 | 配置文件路径，默认 `config.yaml` |

密钥只放在环境变量里，`config.yaml` 只写变量名。

## 模型

```yaml
providers:
  deepseek: { base_url: https://api.deepseek.com, api_key_env: DEEPSEEK_API_KEY }
roles:
  router:   { provider: deepseek, model: deepseek-flash }
  worker:   { provider: deepseek, model: deepseek-flash }
  reviewer: { provider: deepseek, model: deepseek-flash }   # Heavy 模式里负责汇总
  filter:   { provider: deepseek, model: deepseek-flash }   # 定时任务 webhook 事件的 LLM 过滤
```

只要是 OpenAI 兼容的接口都能用。电脑功能要求 `worker` 和 `router` 用的模型**支持图片输入**。

## 限额和价格

- `limits`：`worker_max_steps`、`task_timeout_s`、`watchdog_interval_s`、`approval_timeout_s`、`daily_token_budget`
- `pricing`：单价，单位是元 / 百万 token，分高峰和空闲两档（`[高峰, 空闲]`）。官方调价时改这里。

## 电脑

```yaml
computer:
  enabled: true
  backend: auto          # auto：有 INSTABOX_API_KEY 就用 Instabox，否则用本地 Docker；也可以指定 instabox / local
  width: 1280
  height: 800
  idle_pause_s: 900      # 闲置多少秒后自动暂停
  instabox_template: agent-browser
  instabox_machine: ""   # 使用已有的 Instabox 机器（免费版只能有一台）
```

| 后端 | 说明 |
|---|---|
| Instabox | Firecracker 微虚拟机，电脑关机也照常跑；暂停时保留内存。需要权限为 `machines:write` 的 API key。 |
| 本地 Docker | 容器 `osa-computer`，数据在 `data/computers/shared`。VNC 只监听 127.0.0.1，由服务端转发给浏览器。 |

## 应用（MCP）

在侧栏的 **应用** 页里管理。市场里的应用分三种连接方式：

| 方式 | 应用 | 用户要做的 |
|---|---|---|
| 登录授权 | Notion、flomo、腾讯文档、Evernote、Dropbox、滴答清单、TickTick、Todoist、Linear、Sentry | 点“连接”，在弹窗里登录、同意，弹窗自己关掉 |
| 填一个密钥 | Gmail、QQ 邮箱、163 邮箱、flomo 快速记录、语雀、GitHub、高德地图 | 按卡片上的步骤拿到密码 / token，粘贴后点“连接” |
| 免配置 | 文件夹、网页抓取、Exa 搜索、DeepWiki、Context7、12306 火车票 | 点“连接” |

目录在 `osa/catalog.py`。每一项写明介绍、能做什么、示例说法和连接模板；模板里的 `{字段}` 换成用户填的值，
标了 `secret` 的字段存进保险库，配置里只留 `{vault:名字}`，删除应用时一起删掉。

**登录授权**用远程 MCP 地址（Streamable HTTP）加 OAuth：先按 MCP 规范发现对方的授权服务器，用动态客户端注册（DCR）
自动拿一个 client_id，所以不用去对方平台建开发者应用。回调地址是 `<你访问的地址>/oauth/callback`，令牌加密存在保险库
（`OAUTH_<应用名>`），过期前自动刷新。对方吊销授权或注册失效时，应用显示“需要重新登录”，点一下会从头注册和授权。

几个注意点：

- 回调地址跟着你访问的地址走。换了访问地址（比如从 `localhost` 换到域名）后第一次登录会重新注册客户端，不影响已有的令牌。
- 有些服务只接受 https 或 `localhost` 的回调地址。用 `http://内网 IP` 访问时，登录授权可能被对方拒绝，换成 https 或本机访问即可。
- flomo 的登录授权需要 MAX 会员；只有 PRO 的话用“flomo 快速记录”（只能新建笔记）。
- 邮箱走 IMAP/SMTP（`uvx mcp-email-server`），添加前会先试登录一次。Gmail 要用应用专用密码（需要开两步验证），QQ 和 163 用授权码。
- Google 云端硬盘、日历这类必须用 Google OAuth 的应用还没有收录：Google 不支持动态注册，需要你自己在 Google Cloud 建 OAuth 客户端。

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

## 群聊

不需要配置。限制：每个群 2–6 个 Bot，最多 50 个群；每一轮（用户的一条消息或一次任务交接）最多 8 次 Bot 发言（`osa/groups.py` 的 `MAX_TURNS`）。
群里的模型调用都记在各个 Bot 的 `router` 角色下。

## 记忆与资料库

不需要额外配置，下面这些是代码里的上限：

| 项目 | 上限 |
|---|---|
| 每个 Bot 自己的记忆 | 200 条，每条 500 字；写进提示词的是最近更新的 80 条 |
| 共享的用户事实 | 50 条 |
| 做过的事 | 每个 Bot 保留最近 20 条，提示词里带最近 10 条 |
| 页面 | 共 1000 个，每个 50 万字 |
| 上传文件 | 每个 20 MB，存到电脑的 `/data/workspace`（附件在 `uploads/` 下） |

“从其他助手导入”用 `filter` 角色的模型把粘贴的内容拆成条目，费用记在这个角色下。
`memory.inline_messages` 仍然控制对话里留在上下文的消息条数；点“新话题”之后从零算起。

## 登录

设置环境变量 `OSA_PASSWORD` 后，打开页面会先要求输入密码。登录保持 30 天，期间用着会自动续期；设置 → 右上角“退出登录”。

- 不用登录就能访问的只有：登录页、分享链接（`/s/<token>`）、webhook（`/hooks/*`，用 `OSA_WEBHOOK_TOKEN` 鉴权）、前端的静态文件。
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

服务器内存小或者不想装 Docker 时，电脑用 Instabox：设置 `INSTABOX_API_KEY`，并把 `computer.backend` 写成 `instabox`。

应用（MCP）里用 `npx` 的（文件系统、GitHub、Notion）要装 Node ≥ 20，用 `uvx` 的（网页抓取）要装 uv。
Ubuntu 18.04 这类 glibc 低于 2.28 的系统跑不了官方的 Node，可以用 [unofficial-builds](https://unofficial-builds.nodejs.org/download/release/) 里的 `linux-x64-glibc-217` 版本。
用 systemd 运行时，服务的 PATH 里默认没有这些命令所在的目录，要在 unit 里写上，例如：

```ini
Environment=HOME=/root PATH=/opt/node/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```
