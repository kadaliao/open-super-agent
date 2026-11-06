# 开发

## 环境

```bash
uv sync                              # Python 依赖
(cd web && npm ci)                   # 前端依赖
uv run uvicorn osa.server:app --reload --port 8765
(cd web && npm run dev)              # 前端热更新，接口代理到 8765
```

前端改完运行 `npm run build`，产物在 `osa/static/dist`，不进 git。

## 界面文字和翻译

界面有中文和英文两种。**中文原文就是翻译的键**，英文写在一份字典里，没翻译的地方显示中文：

| | 写法 | 英文在 |
|---|---|---|
| 前端 | `t('保存')`、`t('最多 {n} MB', { n })`；句子里要嵌元素用 `tn('点 {b} 保存', { b: <b>…</b> })` | `web/src/locales/en.json` |
| 后端 | `i18n.t("没有这个 Bot")`、`i18n.t("最多 {n} MB", n=n)`；模块里的常量表用 `i18n.N_("…")` 标记，显示时再 `i18n.t(值)` | `osa/locales/en.json` |

- 改了或新加界面文字，在对应的 `en.json` 里补上英文，然后跑 `uv run python -m osa.i18n`：列出缺的、多余的、占位符对不上的翻译，
  以及前端还没包进 `t()` 的中文（CI 也会跑）。确实不用翻的那一行（要比对的标记、语言自己的名字）在行尾注释 `i18n-ignore`。
- 英文里要区分单复数时写成数组：`"{n} 个任务": ["{n} task", "{n} tasks"]`，按变量 `n` 是不是 1 选。
  同一句中文在不同地方要翻得不一样时，前端用 `tc('场景', '中文')`，字典里的键写成 `"场景::中文"`。
- 只翻用户看得到的：界面、接口报错、对话里的系统消息和卡片、推送、聊天软件里的消息和按钮。**写给模型的提示词、工具说明、
  工具结果保持中文**；英文界面下只在提示词末尾加一句 `i18n.model_hint()`，让 Bot 默认用英文回复。
- 存进数据库、之后还要按原文比对的标记（`[新话题]`、`[任务已派发]`、`[任务 #N 结果]`、`[定时任务 名字]`、`[附件] 路径`）不要翻，
  在显示的地方翻。用户在聊天软件里打字回复的命令（钉钉的“允许一次 #编号”），用 `i18n.both()` 两种语言都认。
- 语言存在 `settings` 表的 `language`，前后端共用；切换语言会重新加载页面。后端的语言能在运行中变，所以别在 import 时就翻好。
- 应用清单和 Bot 模板的英文写在各自的 `en:` 里，见 [apps.md](apps.md)、[templates.md](templates.md)。
- 英文界面在本机看：设置左下角点 English。也可以在浏览器的控制台里 `localStorage.setItem('osa.lang', 'en')` 再刷新，
  但页面加载后会跟服务端的设置对齐。

## 自动化测试

```bash
uv run pytest -q
```

测试只测框架，不调真的模型，也不碰真的电脑（`tests/conftest.py`）：

- 每次运行用一个新的临时数据目录和配置（电脑关掉、没有应用、没有旧版触发器），碰不到 `data/` 里的真实数据。
- `llm` 换掉模型客户端：请求经 `httpx.MockTransport` 交给 `tests/fakellm.py` 里的假模型，按剧本流式返回文字或工具调用，
  并记下每次收到的请求。剧本按角色排：`llm.script("router:Scout", Call("dispatch", {"goal": "…"}), "派出去了")`、
  `llm.script("worker", "结果")`；`llm.bodies("router", "Scout")` 取它收到的请求，用来检查提示词和上下文。
- `events` 冒充一个打开着的网页，收下 `broadcast` 发出的所有事件；`make_bot()` 建一个名字不重复的 Bot。
- `until(lambda: …)` 等到条件成立；`settle()` 等后台的轮次和任务都跑完；`find(chat_id, kind=…, text=…)` 查对话里的消息。
- 所有测试共用一个事件循环（Router、群聊的锁是常驻的）。

改了 Router、群聊、私信、确认、电脑分配这些有约定的地方，加一个测试把约定写下来（见 `tests/test_smoke.py`）。

## 不要用真实数据测试

复制一份数据库，在另一个端口启动：

```bash
mkdir -p /tmp/osa-test && cp data/osa.db /tmp/osa-test/
sed 's|db: data/osa.db|db: /tmp/osa-test/osa.db|' config.yaml > /tmp/osa-test/config.yaml
OSA_CONFIG=/tmp/osa-test/config.yaml uv run uvicorn osa.server:app --port 8766
```

本地电脑是共用的（容器 `osa-computer`），测试和正式实例会操作同一台电脑，不要同时跑。用 Docker Compose 跑的实例也用这个容器。

`computer.backend` 默认是 `auto`：环境里有 `INSTABOX_API_KEY` 就用 Instabox。本机设了这个变量时，测试实例和直接调 `osa.computer.get()` 的脚本
都会操作 Instabox 上那台（可能正是服务器上正式在用的）电脑。测试时在复制的配置里写 `backend: local`，或者 `env -u INSTABOX_API_KEY` 启动。
复制的数据库里有聊天软件连接和定时任务的话，先停掉（`update channels set enabled=0`、`update routines set enabled=0`），
免得测试实例去抢正式机器人的消息、跑定时任务。

## Docker 镜像

改了 `Dockerfile`、`compose.yaml` 或依赖后，在一份**不含 `data/` 的副本**里验证（compose 会把当前目录的 `data/` 挂进去）：
`docker compose -p osatest up -d --build`，`.env` 里设 `OSA_PORT` 换个端口。测完 `docker compose -p osatest down -v`。

- 环境里设了 `DOCKER_DEFAULT_PLATFORM` 的话先 `unset`，否则会构建出要模拟运行的镜像。
- `--profile https` 可以用 `OSA_DOMAIN=osa.localhost` 在本机测（Caddy 会签一个本地证书）。本机的代理软件可能拦 443 端口，
  从 compose 网络里的容器访问 `https://caddy`（SNI 填 `osa.localhost`）更可靠。

## 端到端验证

- **对话**：通过 WebSocket 发送 `{"type":"send","chat_id":1,"text":"..."}`，观察 `message`、`task`、`stream_*` 事件。
- **界面**：用 Playwright 驱动系统 Chrome（`channel="chrome"`）截图检查，这样不用另外下载浏览器。
- **电脑**：在 Python 里调用 `osa.computer.get()`，可以直接测试 `start / screenshot / shell`；浏览器工具不给 `key` 时用最前面的标签页：
  `await c.browser("open", url=...)`、`c.browser("snapshot")`。
- **几个任务同时用电脑**：用 `computer.tools.call` 模拟任务——先 `CLAIM.set("201:")`，再 `await call(SimpleNamespace(id=201, bot_id=1, chat_id=1), "browser_open", {...}, progress)`；
  两个任务用 `asyncio.gather` 同时开不同的网页，应该各自在自己的窗口里（`c.users[key]["window"]` 不同），用时和一个差不多。
  测试页可以在电脑里起：`docker exec -d osa-computer python3 -m http.server 8999 --bind 127.0.0.1`，页面里放输入框、文件框、
  `target=_blank` 链接和 `window.open` 弹窗，分别测 `computer_click`（坐标对不对）、`computer_key ctrl+a`、`browser_upload`、新标签页和弹出窗口。
  把 `CFG["computer"]["parallel"]` 改成 2 再开第三个，应该排队并收到“排队等电脑”。
  端到端：让 Bot“调用 dispatch 两次”派两个开网页的任务，右栏电脑上方应该同时列出两个任务；再派一个会 `ask_human` 的，接手期间另一个任务停住，交还后都做完。
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
