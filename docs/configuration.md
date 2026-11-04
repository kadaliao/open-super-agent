# 配置

配置分两层：仓库里的 `config.yaml` 是默认值；在界面“设置”里改的值存进数据库，会覆盖默认值。
用 `OSA_CONFIG=/path/to/config.yaml` 可以换一份配置文件（测试时常用）。

## 环境变量

| 变量 | 必需 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 默认配置下必需 | 模型提供方的 key；换用别的提供方时，改 `providers.*.api_key_env` |
| `INSTABOX_API_KEY` | 否 | 设置后，电脑改用 Instabox 云主机 |
| `NOTION_TOKEN` | 否 | Notion MCP 应用 |
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
  filter:   { provider: deepseek, model: deepseek-flash }   # 触发器的 LLM 过滤
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

推荐在界面里管理：**设置 → 应用**，选预设（文件系统、GitHub、Notion、网页抓取）或自定义命令，填好密钥后点“保存并连接”。
应用配置存在数据库里。`config.yaml` 的 `mcp_servers` 只在第一次启动时导入一次，之后改界面即可：

```yaml
mcp_servers:
  notion:
    enabled: false
    command: npx
    args: ["-y", "@notionhq/notion-mcp-server"]
    env_from: [NOTION_TOKEN]   # 导入后变成 env:NOTION_TOKEN，值从服务的环境变量读取
    deny: [delete, archive]    # 名字里含这些词的工具不会提供给模型
    confirm: auto              # auto：写操作需要确认 | all：全部需要确认 | none：都不需要 | 关键词列表
```

环境变量的值有三种写法：明文、`env:名字`（服务的环境变量）、`vault:名字`（保险库）。
在界面里填的密钥会自动存进保险库。

## 确认规则与保险库

- **确认规则**（设置 → 确认规则）：按工具全名匹配，支持 `*`，如 `filesystem__*`。“先问我”优先于“自动允许”；都不命中时按应用的 `confirm` 策略。确认卡片上点“总是允许”会自动加一条规则。
- **过期**：只有定时 / webhook 触发的后台任务，卡片在 `approval_timeout_s` 后按拒绝处理；对话里的任务一直等。
- **保险库**（设置 → 保险库）：Fernet 加密存在 SQLite。主密钥取 `OSA_VAULT_KEY`，没设置时自动生成 `data/vault.key`（权限 600）。换主密钥后旧密钥解不开，需要重新录入。

## 触发器

- **定时**：`type: schedule`，用 `at: ["09:00"]` 表示每天几点，或用 `every: 3600` 表示每隔多少秒。
- **Webhook**：`type: webhook`，外部系统 POST 到 `/hooks/<name>`。可以用 `include` / `exclude` 关键词过滤，`llm_filter` 让模型判断要不要处理，`prompt` 写交给 Bot 时的说明。

```bash
curl -X POST localhost:8765/hooks/github -H 'content-type: application/json' \
  -H "X-OSA-Token: $OSA_WEBHOOK_TOKEN" -d '{"issue":{"title":"Bug: ..."}}'
```

## 远程访问（Tailscale）

服务只监听本机，由 Tailscale 提供 HTTPS：

```bash
uv run uvicorn osa.server:app --port 8765
tailscale serve --bg --https=8443 8765     # 访问 https://<机器名>.<tailnet>.ts.net:8443
```

如果改用 `--host 0.0.0.0` 直接访问，地址必须写 `http://`。浏览器自动升级成 https 时，服务端会报 `Invalid HTTP request`。
