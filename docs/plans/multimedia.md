# 计划：多媒体消息、对话里画图、链接卡片（已完成）

> [!NOTE]
> 三步都已做完，合进了 main，这里留作存档。功能说明见 [功能一览](../features.md#资料库图片和链接)。

2026-11-07 定下方向，当天三步都做完了（分支 `multimedia`）。按下面的顺序分三步做，**每一步单独提交**，做完更新 `CHANGELOG.md` 的 `[未发布]`，
然后在这份文档的“进度”里勾掉。设计里标了“待定”的地方，动手前先问用户。

## 进度

- [x] 第一步：多媒体基础（对话里显示图片和文件，Bot 能直接看你发的图）
- [x] 第二步：Bot 在对话里画图（画之前先弹确认卡片）
- [x] 第三步：链接卡片和嵌入（YouTube、B 站、X、普通网页）

## 现状（2026-11-07）

- **消息只有文字**：`web/src/components/Markdown.tsx` 用 react-markdown 渲染。链接都是普通 `<a>`（资料库页面的链接在应用里打开）；
  `![](url)` 外链图片能显示，但 `styles.css` 里没有 `.md img` 的样式，宽图会撑出气泡。没有音视频播放器。
- **附件**：`Composer.tsx` 把文件 `PUT /api/files/content` 传到共用电脑的 `uploads` 目录（电脑没开会先开机），
  消息里只加一行 `[附件] 路径`。这个标记存进数据库、不翻译，Router 靠它找文件。
  用户消息在 `Chat.tsx` 的 `MessageList` 里按纯文本显示，没有缩略图。WebSocket 的 `send` 只带 `text`（`server.py` 里 `type == "send"`）。
- **Router 看不到附件**：提示词（`osa/router.py` 开头）让它派任务处理附件；`memory.context()` 拼出来的上下文全是字符串。
  Router 只有看电脑屏幕时才收图：`ctools.image_message()` 生成 `image_url` 消息，`prune_images()` 只留最近 2 张。
- **Bot 做出来的文件**在电脑里，用户去“资料库 → 文件”看（`GET /api/files/download?path=`，图片、PDF、纯文本直接打开）。
- **生图**：只有头像在用（`osa/avatars.py`）。`models()` 列出已配服务里能画图的模型（按名字认，`-Edit` 不算），
  `generate()` 调兼容 OpenAI 的 `/images/generations`，硅基流动单独处理（参数 `image_size`，返回 `images[].url`，一小时后失效）。
  上次用的模型记在 `settings` 表的 `avatar_model`。硅基流动的价格（2026-11）：Kolors 免费，Z-Image-Turbo 每张 0.1 元，
  Qwen-Image、Z-Image 每张 0.3 元；出图约 4 秒，1024×1024 的 PNG 约 900 KB，右下角有“AI 生成”水印。
  aihubmix 列出的生图模型只有 Gemini（走对话接口），没接。
- **头像图片**存在数据库的 `avatars` 表，`/avatars/<id>` 不用登录（分享页要用）。对话里的图片**不能**照搬“不用登录”这一点。
- **确认卡片**：`approvals.request(task, tool, shown)` 弹卡片并等用户决定；`approvals.decide(tool)` 按规则判断要不要问，
  默认要问的电脑工具在 `PC_ASK`。Router 自己调工具时没有真的任务，用 `SimpleNamespace` 冒充一个（见 `Router._direct`）。
- **聊天软件**（`osa/channels/`）只发文字，`channels.render()` 把消息转成文字。

## 第一步：多媒体基础

目标：你发的图片在对话里显示成缩略图，点开看大图；对话模型能看图时，Bot 直接看到图片，不用派任务；
Bot 回复里的图片、文件、音视频好好显示。

1. **存图片**：新建 `media` 表（`id` 用内容哈希，`mime`、`name`、`size`、`w`、`h`、`data`、`created`），只存图片。
   - 浏览器先把大图缩到最长边 1600 像素左右再传（参考 `AvatarTools.tsx` 的 `squareImage`，不裁剪）。服务端只收 PNG / JPEG / WebP / GIF，并限制大小。
   - `GET /api/media/<id>` **要登录**，加 `nosniff`，可以长缓存。
   - 原文件照旧传到电脑的 `uploads` 目录，任务还靠它处理。图片多存一份是为了显示：不用为了看缩略图去开电脑，
     电脑在 Instabox 上时也不用每次远程取。
   - 定了（2026-11-07）：存数据库，备份自动带上，和头像一致。
   - 没人引用的图片要清理（参考 `avatars.gc()`）。删对话、删 Bot 时一起清。
2. **消息带附件**：WebSocket 的 `send` 加一个 `attachments`（媒体 id、文件名、电脑里的路径），服务端存进用户消息的 `meta.attachments`。
   `content` 里照旧写 `[附件] 路径`，Router 和任务的现有逻辑不用动；界面上有 `meta.attachments` 时，把这几行藏起来，改成显示缩略图或文件卡片。
   非图片的文件只显示卡片（名字、大小，点开走 `/api/files/download`）。聊天软件进来的附件（`channels.save_upload`）也照这样处理。
3. **Bot 看图**：对话模型能看图时（`prices.lookup(provider, model)["vision"]`；设置里“测试”也能测出来），
   `memory.context()` 把带图片的用户消息拼成 `image_url` 片段（和 `ctools.image_message` 一样的格式），只带最近几张，更早的换成文字。
   模型不能看图时维持现状（派任务）。Router 提示词里那句“附件要派任务”改成按能不能看图来说。
   以后可选：给后台任务一个看图工具，比如 `view_image(path)`。
4. **Bot 回复里的媒体**：
   - 图片：`.md img` 限宽限高、圆角，点开大图（做一个通用的看大图组件，第二步也要用）。
   - 电脑里的文件：给 Router 和任务一个工具，比如 `send_file(path)`，把电脑里的文件发进对话。图片存进 `media` 直接显示，
     其他文件显示成卡片。不要让模型自己拼 `/api/files/download?...` 链接。
   - 音视频：链接是 `.mp3`、`.m4a`、`.wav`、`.mp4`、`.webm` 这类时，用 `<audio>`、`<video>` 播放。
5. 聊天软件发图（Telegram 的 `sendPhoto`、飞书上传图片后发 image 消息……）放到以后，这一步只保证文字里有能打开的链接。

验收：粘贴一张截图发给 Bot，对话里显示缩略图，问“图里写了什么”，Bot 直接回答、不派任务；
让 Bot 截图并把文件发过来，对话里显示出图片；刷新、换设备打开都正常；电脑关着时缩略图照样显示。

## 第二步：Bot 在对话里画图

目标：你说“画一张……”，Bot 先弹确认卡片，你允许后画出来，图片直接出现在对话里。

1. **画图模型在设置里统一选**：设置 → 模型 加一行“画图”（参考 embedding 那一行），列表用 `avatars.models()`。
   把 `avatar_model` 改名为 `image_model`，旧的那个值读进来，头像生成和对话画图共用。
2. **工具** `generate_image(prompt)`：先给 Router 加，后台任务以后再说。没有画图模型时，告诉用户去设置里添加，不要假装能画。
3. **先问用户**：用确认卡片，工具名 `generate_image`，默认每次都问（加进默认要问的集合，像 `PC_ASK` 那样）。
   卡片上写明描述、模型、价格（知道的话），可以点“总是允许”，比如用户选的是免费的 Kolors。
   Router 里冒充任务的写法照 `Router._direct`。
4. **结果**：原图存进 `media`（第一步建的），对话里发一条带图片的 Bot 消息（`meta.kind = "image"`，带上描述和模型），
   能点开大图、下载、“设为头像”（复用 `AvatarTools.tsx`）。工具只回一句“已画好，用户看得到”。
5. **费用**：models.dev 里没有按张计价的数据。至少在 `llm_usage` 记一行（模型、0 token），费用统计里能看到用了几次；
   定了（2026-11-07）：支持，写在 `pricing.images`，先按服务商（接口地址里的词）再按模型名字查，默认配置写了硅基流动的几个。
6. 以后：用户发一张图让它改（`Qwen/Qwen-Image-Edit` 这类，要把原图一起传）；聊天软件里发图片。

验收：说“画一只戴墨镜的猫”，弹出确认卡片，允许后几秒出图；点“总是允许”后下一次不再问；没配画图模型时 Bot 照实说；
画好的图能设为头像。

## 第三步：链接卡片和嵌入

目标：Bot 回复里单独占一行的链接，显示成卡片或播放器，像 Notion 的嵌入。

1. **哪些链接展开**：只展开单独占一段的链接（一段里只有一个裸网址，或只有一个 `[文字](网址)`），句子里的链接照旧；
   一条消息最多展开 3 个。用户自己发的消息也展开（2026-11-07 定）。
   上线后改了（2026-11-07）：用户发链接时习惯写在句子里（“我是来看的。https://youtu.be/…”），只认单独一段等于没展开。
   现在你自己发的消息里所有链接都展开；Bot 的消息里句子中的视频、推文链接也展开，放在那一段后面。b23.tv 这类短链接也认。
2. **YouTube**（`watch?v=`、`youtu.be/`、`shorts/`）：先显示封面 `https://i.ytimg.com/vi/<id>/hqdefault.jpg` 和播放按钮，
   点了才换成 `https://www.youtube-nocookie.com/embed/<id>?autoplay=1` 的 iframe。不点就不连 YouTube。
3. **B 站**（`BV…`）：点了加载 `https://player.bilibili.com/player.html?bvid=<id>&autoplay=0`；封面用下面的网页卡片信息。
4. **X / Twitter**（`x.com` 或 `twitter.com` 的 `/<用户>/status/<id>`）：用官方的嵌入页 `https://platform.twitter.com/embed/Tweet.html?id=<id>&theme=dark`
   放在 iframe 里，不在我们的页面里加载 `widgets.js`。高度要听它发来的 postMessage 调整，先确认消息格式；不行再改成服务器取推文内容、自己画卡片。
5. **其他网页**：书签卡片（标题、摘要、配图、站点名和图标）。
   - 后端 `GET /api/unfurl?url=`：抓之前用 `tools.blocked(url)` 拦本机和内网地址，超时 8 秒，最多读 512 KB，解析 `og:*` 和 `<title>`。
   - 结果缓存在数据库（比如 `unfurls(url, data, fetched)`，保留 7 天），失败也缓存一会儿。
   - 资料库页面这类站内链接不展开。
6. 主应用以后要是加 CSP，记得放行 `youtube-nocookie.com`、`platform.twitter.com`、`player.bilibili.com` 的 frame。
7. 聊天软件自己会展开链接，不用管。

注意：YouTube 和 X 的嵌入要浏览器能访问这些网站才显示，国内要翻墙；网页卡片是服务器去抓的，看服务器能不能访问。

验收：Bot 回复里放一个 YouTube 链接，显示封面，点了才播放；X 链接显示成推文；GitHub 仓库链接显示成卡片；
内网地址不会被服务器去抓；断网或抓不到时退回普通链接。

## 做的时候注意

- 先读 [架构 → 设计约定](../architecture.md) 和 [开发](../development.md)。界面文字用 `t()`，`en.json` 补英文，跑 `uv run python -m osa.i18n`。
- 测试：`uv run pytest -q`（本机环境里有 `INSTABOX_API_KEY` 的话加 `env -u INSTABOX_API_KEY`），前端 `cd web && npm run typecheck && npm run build`。
  前端没有单元测试，界面用 Playwright 驱动系统 Chrome 实际点一遍、截图看。
- 本机试用：复制数据库、另开端口（见 [开发 → 不要用真实数据测试](../development.md#不要用真实数据测试)），电脑类型写 `local`，测完把服务关掉。
- 生图先用假服务测：`tests/test_avatars.py` 用 `httpx.MockTransport`；界面上试用时，可以起一个返回固定 PNG 的假 `/v1/images/generations`，
  再在设置里加成“其他”服务（本机地址不用密钥）。需要真调一次时，用硅基流动的 Kolors（免费）。
- 新增的数据库表和字段，补进 [架构](../architecture.md) 的数据表说明。
