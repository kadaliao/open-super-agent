# 开发

## 环境

```bash
uv sync                              # Python 依赖
(cd web && npm ci)                   # 前端依赖
uv run uvicorn osa.server:app --reload --port 8765
(cd web && npm run dev)              # 前端热更新，接口代理到 8765
```

前端改完运行 `npm run build`，产物在 `osa/static/dist`，不进 git。

## 不要用真实数据测试

复制一份数据库，在另一个端口启动：

```bash
mkdir -p /tmp/osa-test && cp data/osa.db /tmp/osa-test/
sed 's|db: data/osa.db|db: /tmp/osa-test/osa.db|' config.yaml > /tmp/osa-test/config.yaml
OSA_CONFIG=/tmp/osa-test/config.yaml uv run uvicorn osa.server:app --port 8766
```

本地电脑是共用的（容器 `osa-computer`），测试和正式实例会操作同一台电脑，不要同时跑。用 Docker Compose 跑的实例也用这个容器。

## Docker 镜像

改了 `Dockerfile`、`compose.yaml` 或依赖后，在一份**不含 `data/` 的副本**里验证（compose 会把当前目录的 `data/` 挂进去）：
`docker compose -p osatest up -d --build`，`.env` 里设 `OSA_PORT` 换个端口。测完 `docker compose -p osatest down -v`。

- 环境里设了 `DOCKER_DEFAULT_PLATFORM` 的话先 `unset`，否则会构建出要模拟运行的镜像。
- `--profile https` 可以用 `OSA_DOMAIN=osa.localhost` 在本机测（Caddy 会签一个本地证书）。本机的代理软件可能拦 443 端口，
  从 compose 网络里的容器访问 `https://caddy`（SNI 填 `osa.localhost`）更可靠。

## 端到端验证

- **对话**：通过 WebSocket 发送 `{"type":"send","chat_id":1,"text":"..."}`，观察 `message`、`task`、`stream_*` 事件。
- **界面**：用 Playwright 驱动系统 Chrome（`channel="chrome"`）截图检查，这样不用另外下载浏览器。
- **电脑**：在 Python 里调用 `osa.computer.get()`，可以直接测试 `start / open_url / screenshot / shell`；浏览器工具用 `await c.browser("open", url=...)`、`c.browser("snapshot")` 等。
- **应用**：`POST /api/market/filesystem` 从市场添加文件夹应用，再在对话里让 Bot 写文件，验证确认卡片和“总是允许”。
  登录授权的流程可以用 SDK 自带的授权服务器在本机测：写一个 `MCPServer(auth_server_provider=…)`，`authorize` 里直接带着授权码跳回 `redirect_uri`，
  再用自定义应用连它的地址，依次请求 `/oauth/start/名字` 和返回的跳转地址就能走完整个流程，不需要真实账号。
- **应用清单**：`uv run python -m osa.catalog` 检查所有清单；新清单先放进 `data/apps/`，刷新应用页就能看到，点连接后在对话里 `@` 它试一下。
- **模型**：`POST /api/providers/check {"base_url":"…","api_key":"…"}` 不保存，只试地址和 key；
  `GET /api/providers/<名字>/models` 看模型列表（带单价和能不能看图）；`POST /api/models/test {"provider":"deepseek","model":"deepseek-flash"}`
  真的调一次。没有 DeepSeek key 的新实例可以 `env -u DEEPSEEK_API_KEY` 启动，看对话页的提示和在界面里填 key 的流程。
- **记忆**：对 A 说“记住……”，`POST /api/chats/<id>/reset` 开新话题后再问 A；再问 B，B 只应该知道共享的事实。
  `GET /api/bots/<id>/memories` 看记下了什么。
- **群聊**：`POST /api/groups {"bot_ids":[1,2]}` 建群，然后通过 WebSocket 往群的 chat_id 发
  “@A 查……，查完交给 @B 写总结”，应该看到 A 派任务（带 handoff_to）、B 不抢话、任务结果出来后出现交接记录、B 写出总结。
  再让 A 派一个要 `ask_approval` 的任务，确认卡片应该出现在 A 的私聊里（`chat_id` 是私聊，`origin_chat_id` 是群）。
- **Bot 之间的私信**：再建一个 Bot（比如“研究员”），在主 Bot 的私聊里发“让研究员查一下……”。应该看到主 Bot 的对话里出现私信卡片，
  研究员的私聊里出现同一封来信、它派任务（卡片上写“做完把结果回给 …”），任务做完后回信出现在主 Bot 的对话里，主 Bot 再说一轮。
  在界面上停掉研究员的任务，主 Bot 应该收到“没做完，已取消：用户在界面上取消”，照实告诉用户而不是自己重做。
  在群里 @ 主 Bot 让群外的 Bot 做事，回信应该回到群里。
- **演示一遍**：`POST /api/bots/<id>/teach/start` 开始后，可以在电脑里直接跑浏览器控制脚本模拟用户操作：
  `echo '{"op":"open","url":"https://example.com"}' | docker exec -i osa-computer env DISPLAY=:1 python3 /tmp/osa-cdp-*.py`；
  再 `POST /api/teach/stop` 拿到 `steps` 和 `draft`，存成技能后在对话里 `/名字` 重放。
- **推送**：Playwright 默认的浏览器上下文算无痕模式，Chrome 在无痕里不支持推送；要用 `launch_persistent_context`，
  并 `grant_permissions(["notifications"])`。开启后 `POST /api/push/test`，用 `registration.getNotifications()` 确认收到。
- **手机**：Playwright 用 `is_mobile=True, has_touch=True` 的上下文（`pointer: coarse` 才会出现接手时的输入栏）。
- **聊天软件**：不需要真账号。`OSA_TELEGRAM_API`、`OSA_FEISHU_API` 指向本机的假服务器，就能把整个流程跑一遍：
  假的 Telegram 实现 `getMe / getUpdates / sendMessage / editMessageText / answerCallbackQuery / getFile` 几个方法；
  假的飞书实现换 token、`/callback/ws/endpoint` 和一个 WebSocket，帧用 lark-oapi 里的 `pbbp2_pb2.Frame` 编解码（顺便验证手写的编解码）。
  钉钉、企业微信、Discord 分别用 `OSA_DINGTALK_API`（OpenAPI 和 `/v1.0/gateway/connections/open`，返回的 endpoint 指向假的 WebSocket）、
  `OSA_WECOM_WS`（长连接地址）、`OSA_DISCORD_API`（REST，`/gateway/bot` 返回假的 Gateway）。帧格式以官方 SDK（`dingtalk-stream`、
  `wecom-aibot-sdk`）的源码为准；企业微信的附件要用 AES-256-CBC 加密后再给，测解密。
  从假服务器推一条带配对码的消息完成配对，再让 Bot 派一个要 `ask_approval` 的任务，模拟点按钮（Telegram 的 `callback_query`、飞书的 `card.action.trigger`、
  企业微信的 `template_card_event`、Discord 的 `INTERACTION_CREATE`，钉钉是回复“允许一次 #编号”），
  应该看到卡片更新成“已允许”（钉钉是另发一条结果）、任务结果发回来。陌生人的消息和按钮都应该被忽略。
  在同一个进程里起服务（`uvicorn.Server(...).serve()`）的话，可以直接调用 `approvals.request` 造确认请求，把 `channels.save_upload` 换掉就不用开电脑。
- **资料库**：让 Bot 把结果做成页面，`GET /api/pages` 看列表；HTML 页面在沙箱 iframe 里，
  可以写一个会 `fetch('/api/bootstrap')` 的探针页，确认请求被 CSP 拦下。

每个阶段的验收标准见 [roadmap.md](roadmap.md)，合并前请按它实测一遍。

## 修改电脑镜像

```bash
docker build --platform linux/arm64 --build-arg APT_MIRROR=mirrors.ustc.edu.cn -t osa-computer:1 osa/computer/image
docker rm -f osa-computer       # 删掉旧容器，下次使用时会用新镜像创建
```

`--platform` 要写成本机的架构：如果环境里设置了 `DOCKER_DEFAULT_PLATFORM`，会构建出需要模拟运行的镜像，非常慢。

## 常见问题

| 现象 | 原因和处理 |
|---|---|
| `400 reasoning_content must be passed back` | 新加的 assistant 消息没有带 `reasoning`；见架构文档里“模型接口（DeepSeek）”一节 |
| 中文输入丢字 | 镜像里缺少 `xclip`，请重建镜像 |
| Bot 回复为空 | Router 工具轮数用完了；已有兜底逻辑，复现时请检查 `_think` |
| 桌面弹出 fbsetbg 报错 | 镜像太旧，请重建 |
