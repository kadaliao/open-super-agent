# open-super-agent

一个可以自己部署的个人 AI 助手，用来替代 Grok。底层大模型可以随意换（任何 OpenAI 兼容接口：DeepSeek / OpenRouter / Ollama / vLLM）。
架构参考 DeepSeek 2026-10 发布的 Super-agent 设计：

- **Router**（`osa/router.py`）：常驻会话，负责和你对话、派发任务。它本身不执行工具。
- **Worker**（`osa/worker.py`）：后台并行执行任务的 ReAct 循环，可以搜索网页、抓取网页。你随时可以接着聊。
- **Heavy 模式**：N 个 Worker 各自独立解题，再由 Reviewer 互审汇总（相当于 Grok Heavy，只在难题上用）。
- **Watchdog**（`osa/watchdog.py`）：定时巡检，遇到超时、没有进展、死循环或超预算就取消任务。
- **自动触发**（`osa/triggers.py`）：定时任务和 webhook 事件。先按关键词过滤，再让便宜模型判断要不要处理，通过的交给 Router 决定怎么做。
- **费用统计**：每次调用模型都按缓存命中、未命中、输出分别记 token，按 DeepSeek 高峰和空闲两档价格算钱，按角色、按任务、按天汇总。
- **外部能力**（`osa/mcp_hub.py`）：通过 MCP 接入外部服务（Notion、GitHub、文件系统……），工具自动交给 Worker 使用，Router 也会知道多了哪些能力。
- **写操作确认**（`osa/approvals.py`）：插件要写入、修改或删除外部数据前先暂停，页面上弹出确认卡片（显示工具和参数），你点允许或拒绝，也可以直接在聊天里说。超时按拒绝处理。
- **记忆**（`osa/memory.py`）：SQLite 持久化。最近的消息留在上下文里，更早的只保留索引，需要时用 `recall(id)` 取回原文。

## 运行
```bash
export DEEPSEEK_API_KEY=...        # 或在 config.yaml 里换成别的提供方
(cd web && npm install && npm run build)   # 构建前端，只在前端代码改动后需要
uv run uvicorn osa.server:app --port 8765
# 浏览器打开 http://127.0.0.1:8765
```
开发前端时可以运行 `cd web && npm run dev`，它会把接口代理到 8765。

界面参照 xAI 的 Grok Bot：多个有名字、带头像的 Bot，每个 Bot 一个对话，回复流式输出、思考过程可以折叠，后台任务和确认请求以卡片形式出现。完整的开发计划见 `docs/PLAN.md`。

## 电脑（Bot 的“手”）
所有 Bot 共用一台电脑（Linux 桌面 + Chromium + 终端），同一时间一个任务独占、其他排队，浏览器登录状态也共享。后台任务会用它打开网页、点击、输入、下载文件、运行脚本。你在右栏“电脑”标签里能实时看到屏幕，点“接手”就能自己操作，点“交还”Bot 继续做。遇到登录、验证码这类只能人做的步骤，Bot 会在对话里弹出“需要你帮忙”的卡片。

两种后端，在 `config.yaml` 的 `computer.backend` 里选（默认 `auto`）：
- **Instabox**（设置了 `INSTABOX_API_KEY` 时自动使用）：云端的 Firecracker 微虚拟机，关掉电脑也照常跑，闲置自动暂停。在 https://instabox.sh 注册后创建个人 API key，需要 `machines:write` 权限。
- **本地 Docker**（没有 key 时使用）：每个 Bot 一个容器，数据在 `data/computers/bot-<id>`。第一次启动会构建镜像 `osa-computer:1`，需要几分钟。

截图交给 `deepseek-flash` 看（它支持图像理解），每张图大约只占几百个 token。

## 从其他设备访问（Tailscale）
推荐用 HTTPS：服务仍然只监听本机，由 Tailscale 提供证书。
```bash
uv run uvicorn osa.server:app --port 8765          # 只监听 127.0.0.1
tailscale serve --bg 8765                         # 然后访问 https://<机器名>.<tailnet>.ts.net
```
也可以加 `--host 0.0.0.0`，再访问 `http://<Tailscale IP>:8765`。地址必须明确写 `http://`：如果浏览器自动升级成 https，服务端会报 `Invalid HTTP request received`。
页面本身没有登录，只适合在 tailnet 内部使用，不要暴露到公网。经代理转发的 webhook 请求必须带 `X-OSA-Token`。

## 接入 Notion
1. 在 https://www.notion.so/profile/integrations 创建一个 internal integration，复制 token（以 `ntn_` 开头）。
2. 在 Notion 里把要让助手访问的页面或数据库分享给这个 integration（页面右上角 ··· → Connections）。
3. `export NOTION_TOKEN=ntn_...`（建议放进你的 shell 环境配置），然后在 `config.yaml` 里把 `mcp_servers.notion.enabled` 改成 `true`。
4. 重启后，侧栏“触发与插件”里应显示 `notion：已连接，N 个工具`。`deny` 默认挡掉删除和归档类工具。

接其他 MCP server 也一样：在 `mcp_servers` 里加一项 `command` / `args` / `env_from`。

## 自动触发
- 定时：`triggers` 里 `type: schedule`，用 `at: ["09:00"]`（每天）或 `every: 3600`（秒）。
- Webhook：`type: webhook`，外部系统 POST 到 `/hooks/<name>`。设置了环境变量 `OSA_WEBHOOK_TOKEN` 时，请求头 `X-OSA-Token` 必须一致；没设置时只接受本机请求。
  ```bash
  curl -X POST localhost:8765/hooks/github -H 'content-type: application/json' -d '{"issue":{"title":"Bug: ..."}}'
  ```

## 配置与扩展
- `config.yaml`：给每个角色配模型、设置 Heavy 的并行数、SearxNG 地址、步数/超时/每日 token 预算。
- 新增工具：在 `osa/tools.py` 写一个 async 函数，再注册到 `TOOLS`。

## 后续计划
远程 MCP（OAuth，如 Notion 官方托管版）、接 Hermes Agent 当 Worker、IM 渠道。
