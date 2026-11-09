# 配置

配置分两层：仓库里的 `config.yaml` 是默认值；在界面“设置”里改的值存进数据库，会覆盖默认值。
用 `OSA_CONFIG=/path/to/config.yaml` 可以换一份配置文件（测试时常用）。

## 环境变量

| 变量 | 必需 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 否 | 默认配置用的 DeepSeek 的 key。也可以不设，在 设置 → 模型 里填 |
| `OSA_PASSWORD` | 部署到公网时必需 | 登录密码，设置后要先登录才能用；不设置则不用登录，只适合本机或内网 |
| `INSTABOX_API_KEY` | 否 | 设置后，电脑改用 Instabox 云主机 |
| `OSA_WEBHOOK_TOKEN` | 否 | 旧版 webhook 地址 `/hooks/<名字>` 的鉴权；没设置时旧地址只接受本机发来的请求。新的事件地址自带密钥，不用它（见 [事件](events.md)） |
| `OSA_ALLOWED_HOSTS` | 否 | 没设 `OSA_PASSWORD` 时，额外放行的访问地址，逗号分隔，和 `server.allowed_hosts` 一样（见 [登录](#登录)） |
| `OSA_CONFIG` | 否 | 配置文件路径，默认 `config.yaml` |
| `OSA_COOKIE` | 否 | 登录 cookie 的名字，默认 `osa_session`。同一台机器上跑两个服务时起不同的名字，免得互相把登录顶掉（cookie 不分端口） |
| `OSA_PARENT_PID` | 否 | 桌面版用：这个进程退出后服务跟着退出 |
| `OSA_DATA_DIR` | 否 | 数据目录，默认仓库里的 `data/`。数据库、密钥、自己加的应用清单、工作目录都放在这里；设了以后，相对路径的 `memory.db` 只取文件名放进这个目录（绝对路径照旧） |
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
“对话”的模型能看图时，你发的图片 Bot 直接看（点“测试”测出来的为准，没测过按 models.dev 的记录）；服务实际不收图片的话，
那一轮去掉图片重试，之后按不能看图处理。

**画图**（设置 → 模型 最下面）：你让 Bot 画图、生成头像时用哪个模型。列表是已经配好的服务里能画图的模型
（硅基流动的 Kolors、Qwen-Image、Z-Image，OpenAI 的 gpt-image，智谱的 CogView……，按名字认；`-Edit` 这类改图的不算），
选“自动”就用排在最前面的（Kolors 免费，优先）。生成头像时换了模型也会记下来。没有能画图的模型时，Bot 会照实告诉你去添加。

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
  filter:   { provider: deepseek, model: deepseek-flash }   # 判断事件要不要处理、导入记忆
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
  或者用 `pricing.source` 换一个地址。设置 → 模型 最下面能看到上次更新的时间，也能点“现在更新”。

```yaml
pricing:
  currency: "¥"
  usd_rate: 7.1                                  # 1 美元换多少人民币
  # source: https://models.dev/api.json          # 单价从哪里取
  peak_hours: ["09:00-12:00", "14:00-18:00"]     # 只对下面分两档的模型有用；北京时间，周一至周五
  models:
    deepseek-flash: {cache_hit: [0.04, 0.02], input: [2, 1], output: [8, 4]}
  images:                                        # 画图：每张多少钱
    siliconflow:                                 # 服务商：接口地址里有这个词，或者就是服务的名字
      Kolors: 0                                  # 模型：写完整 id，或者只写最后一段（不分大小写）
      Z-Image-Turbo: 0.1
```

**画图按张计价**：models.dev 没有按张的价格，所以写在 `pricing.images` 里，按实际用的服务和模型查。默认配置写了硅基流动的几个
（2026-11 抄自官网：Kolors 免费，Z-Image-Turbo 每张 0.1 元，Qwen-Image、Z-Image 每张 0.3 元）。用别家的画图模型，照这个格式加上。
没写的在设置页和确认卡片上显示“价格未知”，费用按 0 记；本机的按 0 计。每画一张（对话里画的、生成头像的）在观测页“按用途”的“画图”下面记一次。

**限额**在 设置 → 限制与预算 里改：每天最多用多少 token、每个任务最多几步、任务最长跑多久、后台任务等确认多久、Heavy 模式同时跑几个。
对应 `config.yaml` 的 `limits`（`daily_token_budget`、`worker_max_steps`、`task_timeout_s`、`approval_timeout_s`，另外还有 `watchdog_interval_s`）和 `heavy.workers`。

点“测试”产生的调用记在“测试模型”下面，几分钱以内。

## 电脑

```yaml
computer:
  enabled: true
  backend: auto          # Bot 电脑用哪种：auto（桌面 App 独立运行时是本机；否则有 Instabox 的 API Key 用 Instabox、没有用本地 Docker），也可以写 instabox / local / host
  parallel: 3            # 几个任务可以同时用电脑，多了排队；设置 → 限制与预算 里也能改
  width: 1280
  height: 800
  idle_pause_s: 900      # 闲置多少秒后自动暂停
  instabox_template: agent-browser
  instabox_machine: ""   # 使用已有的 Instabox 机器（免费版只能有一台）
  chrome_path: ""        # 本机：Bot 的浏览器用哪个 Chrome，留空按常见位置找
  host_shell: false      # 本机的终端，默认关
  linked_shell: false    # 借来的 Mac 的终端，默认关
  host_desktop: true     # 本机是 Mac 时，给了权限就能操作整个屏幕；false 只用浏览器
  # choices: [instabox]  # 电脑面板里给哪几种（host / local / instabox），不写就是全部
```

电脑面板上方能换电脑（存在设置里，覆盖 `backend`）：

- 桌面 App 独立运行时：**本机**（这台 Mac）、**Docker 沙盒**（`local`）、**云电脑**（`instabox`）三选一，默认本机。
- 服务器上：**Docker 沙盒**、**云电脑** 二选一。不给“服务器本机”：服务器没有屏幕，而且多半用 root 跑，Chrome 要关掉自己的沙箱才能以 root 运行，不安全；
  `config.yaml` 里写了 `host` 的照常能用。App 连着服务器时还能选 **本机**，就是把这台 Mac 借给服务器上的 Bot（Mac 上会弹窗问你），
  换回别的就收回。见下面“借给服务器”。
- 云电脑要 Instabox 的 API Key：第一次选时在面板里填，服务先试一下能不能用，能用才加密存进保险库（名字 `INSTABOX_API_KEY`）；
  面板里填的优先，没填用环境变量 `INSTABOX_API_KEY`。换了 Key（可能是另一个账号）会接着用那个账号里以前给这里建的机器。
- 换电脑时旧的那台会暂停（本机关掉 Bot 的 Chrome，Docker 容器暂停，Instabox 挂起），下次换回来接着用。
- 不想让人在面板里选某种（比如服务器内存小、不想在上面跑 Docker），在 `computer.choices` 里只列要给的，比如 `[instabox]`。
  只剩一种时面板上不再出现下拉框；正在用的那种不受影响。“借给服务器”的 Mac 不归它管。

| backend | 说明 |
|---|---|
| `instabox` | Firecracker 微虚拟机，电脑关机也照常跑；暂停时保留内存。需要权限为 `machines:write` 的 API key。 |
| `local` | 本地 Docker 容器 `osa-computer`，数据在 `data/computers/shared`（用 Docker Compose 部署时在命名卷 `osa-computer-data`，由环境变量 `OSA_COMPUTER_DATA` 指定）。VNC 只监听容器内部、不映射到宿主机端口，由服务端经 `docker exec` 转发给浏览器。 |
| `host` | 服务所在那台电脑上的浏览器（比如在自己电脑上直接 `uv run`、在浏览器里用）。规则和本机一样。 |

**本机**（桌面 App 独立运行，或者 `backend: host`）：

- 画面跟着 Bot 走：它在操作屏幕时显示整个屏幕，在浏览器里做事时显示它的浏览器窗口（每一两秒一张截图，手机上也能看）。
  “接手”就是让 Bot 暂停，你直接用电脑，完了点“交还”。
- 浏览器：Bot 有自己的浏览器窗口，用的是你装的 Chrome，但配置目录是单独的（`data/computers/host/chrome`），和你日常用的分开。
  遇到要登录的网站，Bot 会请你帮忙，你在它的窗口里登录一次，之后所有 Bot 共用。调试端口由 Chrome 随机选、只听 127.0.0.1，网页连不上。
  Chrome 装在外接硬盘上（`/Applications` 里只是个链接）时，macOS 要先允许 App 访问“可移除的宗卷”（系统设置 → 隐私与安全性 → 文件与文件夹），
  没允许时电脑面板会提示。服务停止时这个浏览器跟着关掉。
- 屏幕和鼠标键盘（只有 macOS）：给 Open Super Agent 打开**辅助功能**（发鼠标键盘事件、读按钮上的字）和**屏幕录制**（截到别的 App 的窗口）
  两个权限后，Bot 还能看整个屏幕、用你的鼠标键盘操作这台 Mac 上的其他 App（微信、访达、备忘录、系统设置……，`desktop_*` 工具）。
  权限就是开关：没给就只用浏览器，电脑面板上有一行提示和“去打开”按钮（打开后还显示这句，就退出 App 再打开）；不想要可以写 `host_desktop: false`。
  自己用 `uv run` 跑时，权限给的是你启动它的那个终端。
  - 每个任务第一次用屏幕先弹卡片问你（`desktop`），对话里同意过的 10 分钟内不再问。在屏幕上点“发送 / 发布 / 提交 / 付款 / 购买 / 转账”这类按钮、
    在多行输入框（聊天软件、终端）里按回车也先问（`desktop_submit`）。点“总是允许”后不再问，可以在 设置 → 确认规则 里撤销。
  - Bot 在用屏幕时，你动一下鼠标或键盘它就自己停下（在 App 里点确认卡片、点“交还”不算），等你点“交还”再接着做，交还后它先重新看一眼屏幕。
    屏幕锁着时什么都不做。Bot 不点、也不往 Open Super Agent 自己的窗口（桌面 App，和浏览器里开着的界面）里打字，免得它自己点“允许”。
  - 鼠标键盘只有一套，一次只给一个任务用，别的任务排队（任务卡片上写“排队等屏幕”）；对话里 Bot 直接操作的，一轮说完就让出来。
  - 只截主显示器。给模型的截图缩到 1280×800 以内，坐标按截图算。打字是放进剪贴板再按 ⌘V（中文输入法开着也不会被拦），
    粘贴完把原来的剪贴板放回去，剪贴板管理器不记这一条。App 的名字中英文都认（“计算器”“Calculator”）。
  - 要把截图发给你时，Bot 用 `desktop_screenshot`（整个屏幕）或 `computer_screenshot`（它的浏览器页面）的 `save` 存进工作文件夹的 `screenshots`，再发进对话。
- 文件：只放在工作文件夹 `data/workspace`（和“文件系统”应用是同一个目录）。Bot 读写文件、往网页上传文件都限定在里面，
  浏览器下载的文件存到 `data/workspace/downloads`。技能里写的 `/data/workspace/…` 会自动换成这个目录。
- 终端：默认没有。在电脑面板勾上“允许 Bot 用终端”（或 `host_shell: true`）后，Bot 能在这台电脑上执行命令，
  工作目录是工作文件夹，但命令能访问你能访问的所有文件，所以**每条命令都要你确认**。卡片上可以点“本次任务内允许”（这个任务里不再问），
  或者“总是允许这个前缀”：前缀默认是命令的第一个词，能改成 `git status`、`npm run` 这样更窄的，生成一条 `computer_shell:git *` 规则。
  带 `;` `&&` `|` `$(` 重定向这类符号的命令按前缀放行管不到，照样每次问；`bash`、`python`、`sudo` 这类前缀等于全放开，别用。命令拿不到名字里带
  KEY、TOKEN、SECRET、PASSWORD 的环境变量。
- 先问你：操作屏幕（`desktop`、`desktop_submit`，见上面）、往网页上传文件（`browser_upload`）、写文件（`computer_write_file`）、提交（`browser_submit`：点“发送 / 发布 / 提交 / 付款 / 删除”这类按钮、
  POST 表单的提交按钮，或者在发消息的多行输入框、POST 表单里回车；真链接和搜索框不算）都先弹确认卡片。点“总是允许”以后同类操作不再问，
  可以在 设置 → 确认规则 里撤销。用 Bot 电脑时这些默认不问，想问也可以在确认规则里加“先问我”。

**借给服务器**（App 连着服务器时选“本机”，后台的电脑类型叫 `linked`）：和本机一样，只是 Bot 在服务器上、经 Mac 主动连过去的连接操作这台 Mac。
不同的地方：终端的开关是 `linked_shell`（电脑面板里那个勾，哪台设备上都能开关；`host_shell` 和服务器锁都管不到它），每条命令照样先问；
借出时 Mac 上弹窗同意的就包括屏幕；服务器锁（`security.local_exec: false`、`security.exec`）不影响它，演示模式下不能用；
Mac 掉线时电脑面板显示“你的 Mac 没连上”。细节见 [桌面 App](desktop.md)。

**几个任务同时用电脑**：所有 Bot 共用一台电脑，但后台任务不用一个个排队。每个任务在自己的浏览器窗口里做，
截图、点击、输入都只作用在自己的窗口里，互不干扰；登录状态、电脑里的文件大家共用。同时用的任务最多 `parallel` 个，多了排队，
任务卡片上会显示“排队等电脑”。终端命令和读写文件不算在里面。

- 画面上一次只显示一个窗口，跟着最近的操作切换。右栏电脑上方列着正在用电脑的任务，点一下画面就固定在它的窗口上，再点一下取消。
- 你接手电脑时，所有任务的浏览器操作都先停下，交还后接着做。“请你帮忙”的任务会先把自己的窗口放到最前面。
- 每多一个窗口，电脑大概多用 100–300 MB 内存。Instabox 免费版或者内存小的电脑，可以把 `parallel` 调成 2，或者 1（和以前一样一个个来）。
- 第一次升级到这个版本时，电脑里已经开着的浏览器会重开一次（加上防止后台窗口降速的参数），登录状态不受影响。

## 在沙箱里跑代码

Bot 算数、算日期、换单位、处理数据时不心算，而是跑一段代码。两个工具，都在沙箱里跑，所以不弹确认卡片：

- `run_code`（后台任务）：python / bash / node，默认 60 秒、最多 600 秒；可以带几个文字文件进去（数据、技能里的脚本）。
  当前目录是工作文件夹里的 `.run/<编号>/`，用相对路径写的文件留在那里，结果里列出路径，Bot 可以接着 `send_file` 发给你。
  没有产出的运行文件夹跑完就删，超过 7 天的下次跑的时候清掉。输出最多给模型 2 万字。
- `quick_code`（对话里）：只跑 Python，5 秒内跑完、不能联网，用来算数、换单位、算日期；超时就让 Bot 改派后台任务。

在哪跑：

| 电脑 | run_code | quick_code |
|---|---|---|
| Docker / Instabox | 电脑里（电脑本身就是隔离的，能联网） | 本机的受限子进程 |
| 本机、借来的 Mac、电脑关着 | 本机的受限子进程 | 本机的受限子进程 |

本机的受限子进程：

- **macOS** 用系统自带的 `sandbox-exec`：只能写工作文件夹和一个用完就删的临时目录；`/Users`、`/Volumes`、`/tmp` 和服务自己的代码、数据目录
  （`config.yaml`、数据库、保险库）读不到，只露出工作文件夹和解释器自己的目录；连不了本机的服务（localhost、unix socket），
  叫不起别的 App（`open`、AppleScript 都不行）。`run_code` 能连外网，`quick_code` 不能联网。
- **Linux** 优先用 `bwrap`（`apt install bubblewrap`），没有就用 `unshare`（要系统允许普通用户建用户命名空间；Docker 容器里一般不行）。
  沙箱里只有系统目录（`/usr`、`/bin`、`/lib`……，`/etc` 只留 `passwd`、`hosts`、`ssl` 这些公开的文件）、工作文件夹和解释器，
  根目录下别的目录（`/root`、`/home`、`/opt`、`/var`、`/run`、`/tmp`……）都是空的，服务的代码和数据目录也看不到；不能联网（共用网络就能连本机上只对 127.0.0.1 开放的服务）。
  服务用 root 跑时这些尤其要紧：沙箱里虽然还是 root，但挂载全是只读、拿不到任何特权，也看不到别的进程。
- 两边都拿不到名字里带 KEY、TOKEN、SECRET、PASSWORD 的环境变量；`HOME` 指到临时目录。python 用的是服务自己的 Python（带着它装的包），
  node、bash 用这台机器上装的。
- 都没有（Windows，或者容器里建不了命名空间）：没有 `quick_code`；`run_code` 只在有 Docker / Instabox 电脑时有，不然 Bot 会说明跑不了。
  本机电脑打开了终端时，Bot 可以改用 `computer_shell`（每条命令都要你确认）。

`config.yaml` 的 `security.exec` 管能不能执行代码：`off` 两个工具都没有；`sandbox` 只能在沙箱里跑；`host` 另外还允许本机终端。
不写时跟着服务器锁走：`local_exec: true` 是 `host`，`false` 是 `sandbox`；演示服务器最多是 `sandbox`。

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
  也可以写成 `工具:参数`，冒号后面和工具的主参数比（也支持 `*`）：终端是命令，其他工具是 schema 里排第一个、值是文字的参数，
  例如 `computer_shell:git *`（`git` 开头的命令，单独一个 `git` 也算）、`filesystem__write_file:/data/workspace/reports/*`。以 `/` 开头的参数先把 `..` 化掉再比。
  本机终端只认这种带命令前缀的“自动允许”规则，命令里带 `;` `&` `|` `` ` `` `$` `(` `)` `<` `>` 或换行的不算命中；
  “先问我”的终端规则反过来，命令按 `;` `&&` `|` `$(` 切开后任何一段命中都问（`computer_shell:rm *` 也管 `ls && rm -rf x`）。
- **确认卡片上的选择**：拒绝、允许一次、本次任务内允许（这个任务里同一个工具不再问，任务做完、失败或取消就失效，只记在内存里）、
  总是允许（加一条规则；终端是“总是允许这个前缀”，前缀能在卡片上改）。新建、修改、删除 Bot 和 Worker 自己发起的 `ask_approval` 只能一次一次地允许；
  对话里画图这类不属于某个任务的卡片没有“本次任务内允许”。聊天软件里的卡片也有“本次任务内允许”按钮（打字回复“本次任务内允许 #编号”也行），
  终端的“总是允许”按命令的第一个词。在对话里用文字回复确认时，Bot 也能按“本次任务内”或“接下来 N 分钟”处理（`resolve_approval` 的 `scope`：`task`、`minutes:N`）。
  对话里画图（`generate_image`）默认每次都问，卡片上写着描述、模型和价格；点“总是允许”以后就不问了。
- **过期**：只有定时任务和事件触发的后台任务，卡片在 `approval_timeout_s` 后按拒绝处理；对话里的任务一直等。
- **保险库**（设置 → 保险库）：Fernet 加密存在 SQLite。主密钥取 `OSA_VAULT_KEY`，没设置时自动生成 `data/vault.key`（权限 600）。换主密钥后旧密钥解不开，需要重新录入。

## 定时任务和事件

定时任务属于各个 Bot，在右栏“定时”里管理，或在对话里说“每天 9 点帮我……”“明天下午 3 点提醒我……”；查看、修改、暂停、删除也都能在对话里说。
触发后直接派一个后台任务，结果发到 Bot 的对话里。

- **某个时间点一次**：提醒用，跑过就不再跑。
- **每天 / 每周几**：一个或多个 `HH:MM`，可选周几（不选就是每天）。
- **每月几号**：一个或多个日子和时间，这个月没有的日子（31 号）在月底那天跑。
- **每隔一段时间**：至少 5 分钟。

某件事发生时才跑的（收到 webhook、订阅源有新文章、网页变了、收到邮件、价格到了线……）是**事件**，在右栏“事件”里，见 [事件](events.md)。
以前的“webhook 定时任务”升级后自动变成事件，旧地址照旧能用。

定时任务和事件每个 Bot 合计最多 50 个，每个保留最近 20 次运行记录；定时任务上一次还没跑完时这一次跳过（事件是排队）。服务停着时错过的定时只补跑一次。

**没有新内容就不打扰**：到点运行和事件触发时，Bot 判断这次没有值得告诉你的新东西（没有新邮件、和上次一样、事件不需要处理），
结果只写 `[无新内容]`。这时不发结果、不推送、不发到聊天软件，对话里也不显示这个任务，只在运行记录里记一笔“没有新内容”。
按时间触发的还会带上上次有内容的那次结果（最多 2000 字）给 Bot 对比，已经告诉过你的不再重复。
这两段只给后台任务看，不显示在任务卡片上。点“测试”时照常发出结果，也不和上次比。
旧版 `config.yaml` 里的 `triggers` 会在首次启动时导入成主 Bot 的定时任务，之后改配置文件不再生效。


## 手机、语音与推送

- **iPhone App**（TestFlight 内测）：在电脑上打开 设置 → 手机 App，用 iPhone 的相机扫二维码就能连上并登录。
  网页要用手机能打开的地址打开（局域网 IP、Tailscale 或域名），`localhost` 不行。推送经推送中转送到手机，见 [iPhone App](mobile.md)。
- **要用 https 访问**：浏览器里的推送、添加到主屏幕、语音输入都要求安全连接（本机 `localhost` 也算）。手机上建议用下面“远程访问”里 Tailscale 的 https 地址。
- **推送**：在每台设备的“设置 → 通知”里开启；iPhone App 里点“在这台手机上开启推送”。用 Safari 的 iPhone / iPad 要先“添加到主屏幕”，从主屏幕打开再开启（iOS 16.4 起支持）。
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
| 上传文件 | 每个 20 MB，存到电脑的工作文件夹（Docker / Instabox 是 `/data/workspace`，本机是 `data/workspace`；附件在 `uploads/` 下） |
| 对话里的图片 | 浏览器先缩到最长边 1600 像素，每张最多 10 MB，存在数据库里；没有消息引用的一天后清掉。对话模型能看图时带上最近 4 张（PNG / JPEG / WebP） |
| `send_file` | 一轮对话、一个任务最多发 5 个文件；10 MB 以内的图片另存一份直接显示 |
| 链接卡片 | 你发的链接、Bot 消息里单独一段的链接展开成卡片或播放器，YouTube、B 站、X 的链接写在句子里也展开，一条消息最多 3 个。网页卡片由服务器去抓（8 秒、最多 512 KB），缓存 7 天，抓不到的过 1 小时再试；和 `fetch_url` 一样不抓本机和内网地址（`tools.fetch_private` 也管它）。YouTube、X 要浏览器能访问才显示；B 站会拦一些机房的 IP，服务器访问不了 B 站时只显示播放按钮、没有封面和标题，点了照样能播 |

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

- 不用登录就能访问的只有：登录页、分享链接（页面 `/s/<token>`、技能 `/skill/<token>`、Bot 模板 `/template/<token>`）、webhook（`/hooks/*`：事件的地址自带随机密钥，旧版地址用 `OSA_WEBHOOK_TOKEN` 鉴权）、前端的静态文件。
- 同一个 IP 10 分钟内输错 5 次，之后 10 分钟内不能再登录。
- 桌面 App 自动登录用一次性的票：`POST /login/ticket` 带上 `Authorization: Bearer <密码>`，换一张 30 秒内有效、只能用一次的票，再打开 `/login/ticket?t=<票>&next=/` 就登录了。输错和登录页一起限速。
- 手机 App 扫码登录也用这种票：已经登录的页面 `POST /api/pair` 换一张 5 分钟内有效、只能用一次的票，放进 设置 → 手机 App 的二维码里。
- 不管开没开登录，别的网站发来的 WebSocket 和写请求（包括 webhook）都会被拒绝：浏览器会带上那个网站的地址（Origin），所以你在浏览器里打开的网页没法替你给 Bot 发消息、点确认。curl、脚本和 webhook 服务发来的请求不带这个，不受影响。
- **没设密码时，只接受用这些地址访问**：`localhost`、IP 地址（`127.0.0.1`、`192.168.x.x`、Tailscale 的 `100.x.x.x`）、`*.local`、`*.ts.net`。用别的名字访问（比如局域网里的机器名、自己的域名）会看到“没有放行这个地址”，在 `config.yaml` 的 `server.allowed_hosts` 里加上它（支持 `*.example.com`），或者用环境变量 `OSA_ALLOWED_HOSTS`；更好的办法是设 `OSA_PASSWORD`，设了密码就不检查这一项。这一条是为了防 DNS rebinding：别人把自己的域名解析到 127.0.0.1，你一打开他的网页，他就能借你的浏览器读写本机的服务。事件自己的地址（`/hooks/<随机密钥>`）和设了 `OSA_WEBHOOK_TOKEN` 时的旧地址不受这一条限制。
- 会话签名用的密钥在 `data/session.key`（权限 600）。改密码或删掉这个文件，所有设备都要重新登录。
- 放在反向代理（Caddy、nginx）后面时，启动 uvicorn 要加 `--proxy-headers --forwarded-allow-ips 127.0.0.1`，
  这样限速按真实 IP 计算，cookie 也会带上 `Secure`。

## 服务器锁和演示模式

登录密码只管“能不能用这个应用”。有几样功能等于把服务所在的机器交出去，自己电脑上、桌面 App 里正常，放到公网服务器上，
密码一旦泄露就是这台服务器的权限。`config.yaml` 的 `security` 用来关掉它们，只能在服务器上改，网页上改不了：

```yaml
security:
  local_exec: false   # 服务器锁，默认 true
  demo: false         # 演示服务器，默认 false
```

| 设置 | 关掉什么 |
|---|---|
| `local_exec: false` | 电脑选“本机”、本机终端；自定义启动命令的应用，以及改应用的启动命令、参数、环境变量；“文件夹”应用访问工作文件夹以外的目录；应用配置里引用服务的环境变量（`env:名字`）。应用市场里的应用照常能用：只认随项目发布的清单（`osa/catalog/apps`）里原样的命令，填的值只能进模板里留的位置。`mcp_servers` 里写的应用不受限 |
| `demo: true` | 给登录密码公开出去的演示服务器，包含上一行，另外还关掉：所有在服务器上运行程序的应用（npx / uvx，应用市场里的也不行）、自定义应用和应用的连接配置、本机或内网的模型服务地址、改用量上限（每天的 token 上限、Heavy 并行数）、备份的导出和恢复 |

这两个开关背后是两个策略，想分开管可以直接写（写了以写的为准，写错的值按最严的算；`off` 带不带引号都行）：

```yaml
security:
  exec: sandbox      # host | sandbox | off
  apps: market       # any | market
```

| 键 | 取值 | 管什么 |
|---|---|---|
| `exec` | `host`：能在这台机器上直接跑命令（电脑选“本机”、本机终端、看服务器上的文件夹）；`sandbox`：只在沙箱里跑（Docker / Instabox 的电脑，或者受限子进程）；`off`：都不行 | 命令在哪跑 |
| `apps` | `any`：都能配；`market`：本地进程的应用只认应用市场里原样的命令，不能新加 `env:` 引用，“文件夹”只能在工作文件夹里 | 应用 |

不写时按两个开关推：

| | `exec` | `apps` |
|---|---|---|
| 默认（`local_exec: true`） | `host` | `any` |
| `local_exec: false` | `sandbox` | `market` |
| `demo: true` | `sandbox` | `market` |

演示服务器上两个键最宽到 `sandbox` / `market`，写得更宽也不认，在服务器上运行程序的应用照样都不能用。
`apps: any` 等于能在服务器上跑任意代码，放开前和 `exec: host` 一样想清楚。以前的 `plugins` 键已经没有了，留在配置里也不起作用。

检查放在启动应用之前，不管配置是从界面、恢复备份还是别的途径进的数据库，都一样挡住。界面上用不了的选项会藏起来或变灰。
另外不分模式：模型服务换了地址（主机或端口变了）要重新填 API Key，原来的密钥不会发给新地址；应用市场里填的值不能写 `env:` / `vault:` 引用。

## 远程访问（Tailscale）

服务只监听本机，由 Tailscale 提供 HTTPS：

```bash
uv run uvicorn osa.server:app --port 8765
tailscale serve --bg --https=8443 8765     # 访问 https://<机器名>.<tailnet>.ts.net:8443
```

如果改用 `--host 0.0.0.0` 直接访问，地址必须写 `http://`。浏览器自动升级成 https 时，服务端会报 `Invalid HTTP request`。

## 部署到公网服务器

用 Docker Compose 的话，自带一个自动申请证书的 Caddy，见 [用 Docker Compose 部署](docker.md#部署到公网自动-https)。下面是不用 Docker 的做法。

先设置 `OSA_PASSWORD`，并在 `config.yaml` 里设 `security.local_exec: false`（见 [服务器锁和演示模式](#服务器锁和演示模式)）。
服务仍然只监听本机，由 Caddy 之类的反向代理提供 HTTPS：

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

服务器内存小或者不想装 Docker 时，电脑用 Instabox：在电脑面板上选“云电脑”、填 API Key；或者设置环境变量 `INSTABOX_API_KEY`，并把 `computer.backend` 写成 `instabox`。

应用（MCP）里用 `npx` 的（文件系统、GitHub、Notion）要装 Node ≥ 20，用 `uvx` 的（网页抓取）要装 uv。
Ubuntu 18.04 这类 glibc 低于 2.28 的系统跑不了官方的 Node，可以用 [unofficial-builds](https://unofficial-builds.nodejs.org/download/release/) 里的 `linux-x64-glibc-217` 版本。
用 systemd 运行时，服务的 PATH 里默认没有这些命令所在的目录，要在 unit 里写上，例如：

```ini
Environment=HOME=/root PATH=/opt/node/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```
