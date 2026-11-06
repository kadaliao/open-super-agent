# 给应用市场加一个应用

应用市场里的每个应用都是一份 YAML 清单，放在 [`osa/catalog/apps/`](../osa/catalog/apps/)，文件名就是应用名（聊天里 `@` 的那个名字）。
加一个应用只要写一份清单、提一个 PR，不用改任何代码。

## 先确认三件事

1. **它有 MCP server**。优先用官方的远程地址（Streamable HTTP，只有旧版 SSE 地址的也行），其次是 npm / PyPI 上维护得比较好的社区包。
2. **你自己连上过、用过**。清单写得再对，对方服务连不上也没用。
3. **用户不用绕过验证码或人机校验**。这类应用不收（见 [CONTRIBUTING.md](../CONTRIBUTING.md)）。

## 三种连接方式

连接方式由清单自动判断：`connect` 里写了 `oauth` 是**登录授权**；有 `secret: true` 的字段是**填一个密钥**；都没有就是**免配置**。

### 登录授权（最省事，首选）

对方的远程 MCP 支持 OAuth 和动态客户端注册（DCR）时，用户点一下、在弹窗里登录就连上了，不用复制任何东西。

```yaml
title: Linear
category: tasks
order: 40
by: Linear 官方
icon:
  color: '#5E6AD2'
  svg: M2.886 4.18A11.982 11.982 0 0 1 11.99 0C18.624 0 24 5.376 24 12.009c0 ...
desc: 查 issue、建 issue、更新项目进度
about: Linear 官方的连接。登录授权后，Bot 能搜索 issue、新建和更新 issue、写评论。
can:
- 搜索和阅读 issue、项目
- 新建 issue，指派、设优先级
- 更新状态、写评论
example: '@linear 把刚才发现的三个 bug 建成 issue，指派给我'
en:                     # 英文界面用的文案，可以不写，见下面「英文文案」
  by: Official (Linear)
  desc: Find and create issues, update project progress
  about: Linear's official connection. Once you sign in, the bot can search issues, create and update issues, and leave comments.
  can:
  - Search and read issues and projects
  - Create issues, assign them, set priority
  - Update status, leave comments
  example: '@linear file the three bugs we just found as issues and assign them to me'
connect:
  type: http
  url: https://mcp.linear.app/mcp
  oauth: true
```

怎么知道支不支持：用“添加自定义应用”填远程地址、勾选登录授权，能弹出对方的授权页就行。

### 自己建 OAuth 客户端再登录

不支持 DCR 的（Slack、Google），要用户先在对方的开发者后台建一个 OAuth 应用，把客户端 ID 和密钥填进来，再点登录。
`oauth` 写成一组键：

```yaml
fields:
- key: client_id
  label: Client ID
- key: client_secret
  label: Client Secret
  secret: true
connect:
  type: http
  url: https://mcp.slack.com/mcp
  oauth:
    client_id: '{client_id}'
    client_secret: '{client_secret}'
    scope: search:read.public channels:history chat:write   # 只申请这些；不写就按对方公布的全要
    params: {access_type: offline}                          # 授权页要额外带的参数，可以不写
```

写了 `client_id` 时，连接页会显示**回调地址**和复制按钮，`steps` 里告诉用户把它填到对方的“Redirect URL”里。
对方支持“粘贴一段配置建应用”的（Slack 的 App Manifest），用 `copy` 给出整段配置，里面的 `{callback}` 会换成回调地址，
用户点一下复制、粘贴过去，scope 和回调地址就都配好了，见 [`slack.yaml`](../osa/catalog/apps/slack.yaml)。
回调地址是 `https://你的域名/oauth/callback`，大多数服务只接受 https，所以要先按 [部署到公网服务器](configuration.md#部署到公网服务器) 配好 https。

`scope`、`params` 也可以单独用在动态注册的应用上，比如对方公布的权限太多、只想要只读的。

### 填一个密钥

用户按 `steps` 去对方网站拿 token，粘贴进来。标了 `secret: true` 的字段存进保险库（加密），配置里只留引用，删除应用时一起删掉。

```yaml
title: 高德地图
category: life
order: 10
by: 高德官方
icon: {glyph: pin, color: '#0091FF'}
how: 填一个 Key
desc: 查地点、规划路线、看天气
about: 高德地图官方的远程 MCP。填一个高德 Web 服务 Key，Bot 就能查地点、算路线和时间、看天气。
can:
- 搜索地点、查地址和坐标
- 规划驾车、公交、步行、骑行路线
example: '@amap 周六从望京到颐和园，地铁和开车各要多久？'
steps:
- 登录高德开放平台控制台，进入「应用管理 → 我的应用」，新建一个应用
- 给应用「添加 Key」，服务平台选「Web 服务」
- 把生成的 Key 复制到下面
link: https://console.amap.com/dev/key/app
link_label: 打开高德控制台
fields:
- key: key
  label: Web 服务 Key
  secret: true
en:
  title: Amap
  by: Official (Amap)
  how: Needs a key
  desc: Find places, plan routes, check the weather
  # about、can、example 也要写，这里省略
  steps:
  - Sign in to the Amap Open Platform console, go to "App management → My apps" (应用管理 → 我的应用), and create an app
  - Click "Add Key" (添加 Key) on the app and choose "Web service" (Web 服务) as the platform
  - Paste the generated key below
  link_label: Open Amap console
  fields:
    key:
      label: Web service key
connect:
  type: http
  url: https://mcp.amap.com/mcp?key={key}
```

`connect` 里的 `{key}` 会换成用户填的值。本机运行的应用一般把密钥放进 `env`，远程的放进 `url` 或 `headers`
（比如 `Authorization: Bearer {token}`）。每个用户的地址都不一样时（地址里带着授权），`url` 可以整个写成 `'{url}'`，
字段用 `pattern` 检查一下是不是对方的地址。

### 免配置

```yaml
title: 12306 火车票
category: life
order: 20
by: 社区
icon: {glyph: train, color: '#1A63C4'}
desc: 查火车票余票、车次和中转方案
about: 查询 12306 的车次、余票和票价，也能找中转方案。只能查，不能买票。
example: '@train 下周五下午北京到上海还有二等座吗？'
connect:
  type: stdio
  command: npx
  args: [-y, 12306-mcp]
```

本机运行的应用（`type: stdio`）由服务端启动，用 `npx`（Node）或 `uvx`（uv）都可以，部署时两样都要装（见 [部署到公网服务器](configuration.md#部署到公网服务器)）。

只提供旧版 SSE 地址（一般以 `/sse` 结尾）的远程应用，把 `type` 写成 `sse`，其余和 `http` 一样。

## 字段一览

| 键 | 必填 | 说明 |
|---|---|---|
| `title` | ✓ | 显示的名字 |
| `category` | ✓ | 分类：`notes` 笔记与文档、`mail` 邮件与日历、`files` 文件、`tasks` 任务与协作、`design` 设计、`dev` 开发、`web` 搜索与网页、`life` 生活。要加新分类，改 `osa/catalog/__init__.py` 里的 `CATEGORIES` |
| `by` | ✓ | 谁做的这个 MCP：“××官方”或“社区” |
| `desc` | ✓ | 卡片上的一句话 |
| `connect` | ✓ | 连接模板，见下面 |
| `about` | | 详情页的介绍：用的是谁的连接、连上以后 Bot 能干什么 |
| `can` | | 能做什么，一条一句 |
| `example` | | “试试这样说”，用 `@应用名` 开头，写一句用户真会说的话 |
| `icon` | | 图标，见下面。不写就用名字的第一个字 |
| `order` | | 同一分类里从小到大排，不写算 100 |
| `how` | | 卡片左下角的说明，不写按连接方式显示“登录即用 / 填好后登录 / 填一个密钥 / 免配置” |
| `steps` | | 拿密钥的步骤，一步一句 |
| `link`、`link_label` | | 步骤下面的按钮：打开对方的设置页 |
| `copy` | | 让用户整段复制到对方那里的配置：`label`（标题）和 `text`（内容，`{callback}` 换成回调地址） |
| `note` | | 要提醒用户的事，比如需要哪种会员 |
| `fields` | | 要用户填的东西 |
| `check` | | 添加前先试一下填的对不对 |
| `deny` | | 工具名里含这些词的工具不开放（不区分大小写），比如 `[delete]` |
| `confirm` | | 哪些工具调用前先问用户：`auto`（默认：对方标了只读的放行，名字里有 create、send、delete 这类词的先问）、`all`、`none`，或者自己列一组词 |
| `en` | | 英文界面用的文案，见下面「英文文案」 |

`fields` 里每一项：

| 键 | 说明 |
|---|---|
| `key` | 小写字母、数字、`_`。在 `connect` 和 `check` 里写成 `{key}` |
| `label` | 输入框上面的名字 |
| `secret` | `true`：密码类，输入时不显示，存进保险库 |
| `placeholder` | 输入框里的提示 |
| `default` | 默认值。可以用 `{workspace}`：服务器上的工作目录 |
| `optional` | `true`：可以不填 |
| `nospace` | `true`：去掉用户粘贴时带进来的空格（授权码常常带空格） |
| `pattern`、`invalid` | 正则，不匹配时显示 `invalid` 这句话 |

`connect`：

| 键 | 说明 |
|---|---|
| `type` | `http`（远程地址）、`sse`（只有旧版 SSE 地址的远程应用）或 `stdio`（在服务器上启动一个进程） |
| `url`、`headers`、`oauth` | 远程应用用。`headers` 的值都要是文字。`oauth` 写 `true`，或者写 `client_id`、`client_secret`、`scope`、`params`（见上面） |
| `command`、`args`、`env` | 本机运行的应用用。数字要加引号，比如 `'993'` |

`check` 目前有 `imap`（邮箱先试登录一次），用法见 [`checks.py`](../osa/catalog/checks.py)。需要别的检查时，在那里加一个函数。

## 英文文案

界面切到英文时，应用页显示清单里 `en` 写的英文；没写 `en`，或者 `en` 里没写的那一项，照旧显示中文。
`en` 可以不写，但写上的话英文用户能看懂，很欢迎。放在中文文案后面、`deny` / `confirm` / `connect` 前面。

能写的键和中文的同名键一一对应，写了就替换那一项，只能换中文里已经有的：

| 键 | 说明 |
|---|---|
| `title` | 名字。英文品牌名（Notion、Slack）不用写；中文名的写对方官方的英文名：高德地图 → Amap，钉钉 → DingTalk，滴答清单 → Dida365 |
| `by` | 统一写成 `Official (Airtable)`；社区的写 `Community`，IMAP 的写 `IMAP · Community` |
| `desc`、`about`、`note`、`how`、`link_label` | 一段文字 |
| `can`、`steps` | 一组文字，整组替换，条数可以和中文不一样 |
| `example` | 开头的 `@应用名` 要和中文的一模一样（检查时会核对），后面的话翻成英文 |
| `copy` | 只写 `label`；要复制的 `text` 中英文一样 |
| `fields` | 写成「字段的 key: 要换的键」，能换 `label`、`placeholder`、`invalid` |
| `check` | 写成「检查名: {hint: …}」，换登录失败时接在后面的提示 |

```yaml
en:
  desc: Read and search email, tidy your inbox, draft and send for you
  fields:
    email:
      label: Email address
      invalid: That doesn't look like an email address
    password:
      label: App password
  check:
    imap:
      hint: Use the app password, not your regular sign-in password
```

英文写得简短、口语化，用美式英语、句首大写（`Open Amap console`，不是 `Open Amap Console`）。
步骤里要点的按钮在只有中文界面的网站上时，英文后面括号里留上中文原文，方便对着找：`click "Create" (创建)`。

## 图标

品牌图标从 [simple-icons](https://simpleicons.org)（CC0）找：搜到品牌后，把 SVG 里 `<path d="…">` 的 `d` 填进 `svg`，品牌色填进 `color`。

```yaml
icon:
  color: '#0061FF'
  svg: M6 1.807L0 5.629l6 3.822 ...
```

没有的话，用线条图标加品牌色：`icon: {glyph: book, color: '#00B96B'}`。
可选的 `glyph`：`globe`、`folder`、`mail`、`search`、`train`、`pin`、`book`、`doc`、`feather`、`plug`、`chat`、`hash`、`package`、`cloud`、
`palette`、`table`、`calendar`、`check`、`notebook`（图形见 [`web/src/appIcons.ts`](../web/src/appIcons.ts)）。黑色品牌会自动换成白底。
商标归各自所有者，图标只用来让用户认出是哪个应用。

## 在本机试

1. 把清单放进 `data/apps/`（这个目录不进 git）。和内置应用同名时，会覆盖内置的那份。
2. 打开或刷新“应用”页就能看到，不用重启。
3. 点“连接”，然后在对话里 `@应用名` 让 Bot 用一下，写操作要能弹出确认卡片。
4. 检查清单：

   ```bash
   uv run python -m osa.catalog
   ```

   有问题会列出是哪个文件、哪一行或哪个字段。有错的清单会被跳过，不影响其他应用。

## 提 PR

1. 把清单从 `data/apps/` 移到 `osa/catalog/apps/`，再跑一次 `uv run python -m osa.catalog`（CI 里也会跑）。
2. 在 `CHANGELOG.md` 的 `[未发布]` 里加一行。
3. PR 里附上连接成功的截图和一段实际对话。

收录时会看这几点：

- 优先用官方的 MCP；社区包要有人在维护。
- 删除这类没法撤回的操作，放进 `deny`，除非这正是这个应用的用途。
- 密钥字段一定要标 `secret: true`。
- 文案用简洁、口语化的中文，`about` 写清楚用的是谁的连接、Bot 能干什么。
- 最好也写上 `en`（见 [英文文案](#英文文案)）。英文不熟的话可以不写，维护者会补上。
