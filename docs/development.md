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

本地电脑是共用的（容器 `osa-computer`），测试和正式实例会操作同一台电脑，不要同时跑。

## 端到端验证

- **对话**：通过 WebSocket 发送 `{"type":"send","chat_id":1,"text":"..."}`，观察 `message`、`task`、`stream_*` 事件。
- **界面**：用 Playwright 驱动系统 Chrome（`channel="chrome"`）截图检查，这样不用另外下载浏览器。
- **电脑**：在 Python 里调用 `osa.computer.get()`，可以直接测试 `start / open_url / screenshot / shell`；浏览器工具用 `await c.browser("open", url=...)`、`c.browser("snapshot")` 等。
- **应用**：`PUT /api/apps/filesystem` 连接文件系统应用，再在对话里让 Bot 写文件，验证确认卡片和“总是允许”。
- **记忆**：对 A 说“记住……”，`POST /api/chats/<id>/reset` 开新话题后再问 A；再问 B，B 只应该知道共享的事实。
  `GET /api/bots/<id>/memories` 看记下了什么。
- **群聊**：`POST /api/groups {"bot_ids":[1,2]}` 建群，然后通过 WebSocket 往群的 chat_id 发
  “@A 查……，查完交给 @B 写总结”，应该看到 A 派任务（带 handoff_to）、B 不抢话、任务结果出来后出现交接记录、B 写出总结。
  再让 A 派一个要 `ask_approval` 的任务，确认卡片应该出现在 A 的私聊里（`chat_id` 是私聊，`origin_chat_id` 是群）。
- **演示一遍**：`POST /api/bots/<id>/teach/start` 开始后，可以在电脑里直接跑浏览器控制脚本模拟用户操作：
  `echo '{"op":"open","url":"https://example.com"}' | docker exec -i osa-computer env DISPLAY=:1 python3 /tmp/osa-cdp-*.py`；
  再 `POST /api/teach/stop` 拿到 `steps` 和 `draft`，存成技能后在对话里 `/名字` 重放。
- **推送**：Playwright 默认的浏览器上下文算无痕模式，Chrome 在无痕里不支持推送；要用 `launch_persistent_context`，
  并 `grant_permissions(["notifications"])`。开启后 `POST /api/push/test`，用 `registration.getNotifications()` 确认收到。
- **手机**：Playwright 用 `is_mobile=True, has_touch=True` 的上下文（`pointer: coarse` 才会出现接手时的输入栏）。
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
