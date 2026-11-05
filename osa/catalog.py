"""应用市场的目录：每一项说明这个应用是什么、能做什么、怎么连。

kind：oauth（远程地址 + 登录授权，点一下就行）/ token（填一个密钥）/ none（免配置）。
config 是连接配置的模板：字符串里的 {字段名} 换成用户填的值。secret 字段先存进保险库，
配置里只留 {vault:名字}，记在 owned 里，删除应用时一起删掉。
"""
import asyncio
import imaplib
import re
from . import vault
from .mcp_hub import WORKSPACE

ITEMS: list[dict] = []   # 见文件末尾


def _items() -> dict[str, dict]:
    return {i["id"]: i for i in ITEMS}


def public() -> list[dict]:
    out = []
    for i in ITEMS:
        o = {k: v for k, v in i.items() if k not in ("config", "check")}
        o["fields"] = [{k: v for k, v in f.items() if k not in ("pattern", "nospace")} for f in i.get("fields", [])]
        o.setdefault("fields", [])
        o.setdefault("steps", [])
        o.setdefault("can", [])
        o.setdefault("example", "")
        o.setdefault("about", "")
        o["remote"] = i["config"].get("type") == "http"
        out.append(o)
    return out


def title(name: str) -> str:
    from . import mcp_hub
    a = mcp_hub.get(name) or {}
    i = _items().get(a.get("catalog") or name)
    return i["title"] if i else "@" + name


def _fill(v, subst: dict):
    if isinstance(v, str):
        return re.sub(r"\{(\w+)\}", lambda m: subst.get(m[1], m[0]), v)
    if isinstance(v, list):
        return [_fill(x, subst) for x in v]
    if isinstance(v, dict):
        return {k: _fill(x, subst) for k, x in v.items()}
    return v


def _values(it: dict, values: dict) -> dict:
    """检查用户填的内容，返回整理好的值（去掉首尾空格；nospace 的字段去掉所有空格）。"""
    vals = {}
    for f in it.get("fields", []):
        v = str(values.get(f["key"]) or f.get("default") or "").strip()
        if f.get("nospace"):
            v = re.sub(r"\s+", "", v)
        if not v and not f.get("optional"):
            raise ValueError(f"请填写{f['label']}")
        if v and f.get("pattern") and not re.search(f["pattern"], v):
            raise ValueError(f.get("invalid") or f"{f['label']}的格式不对")
        vals[f["key"]] = v
    return vals


async def check(item_id: str, values: dict):
    """添加前先试一下填的东西对不对，错了直接告诉用户，不用等应用连接失败。目前只有邮箱要试。"""
    it = _items().get(item_id) or {}
    host = (it.get("check") or {}).get("imap")
    if not host:
        return
    vals = _values(it, values)

    def login():
        try:
            m = imaplib.IMAP4_SSL(host, 993, timeout=20)
        except OSError as e:
            raise ValueError(f"连不上邮件服务器 {host}：{e}")
        try:
            m.login(vals["email"], vals["password"])
        except imaplib.IMAP4.error as e:
            detail = e.args[0].decode(errors="replace") if e.args and isinstance(e.args[0], bytes) else str(e)
            raise ValueError(f"邮箱登录失败：{detail[:120]}。注意填的是{_field(it, 'password')}，不是平时的登录密码")
        finally:
            try:
                m.logout()
            except Exception:
                pass
    await asyncio.to_thread(login)


def _field(it: dict, key: str) -> str:
    return next((f["label"] for f in it.get("fields", []) if f["key"] == key), key)


def build(item_id: str, values: dict) -> dict:
    """按市场里的模板生成应用配置。用户填的密钥存进保险库。"""
    it = _items().get(item_id)
    if not it:
        raise ValueError("应用市场里没有这个应用")
    vals = _values(it, values)
    subst, owned = {}, []
    for f in it.get("fields", []):
        v = vals[f["key"]]
        if f.get("secret") and v:
            ref = f"{item_id}_{f['key']}".upper().replace("-", "_")
            vault.put(ref, v)
            owned.append(ref)
            v = "{vault:" + ref + "}"
        subst[f["key"]] = v
    cfg = _fill(it["config"], subst)
    return cfg | {"catalog": item_id, "deny": it.get("deny", []), "confirm": it.get("confirm", "auto"), "owned": owned}


# ---------- 目录 ----------
def _mail(id_, title, host_imap, host_smtp, pw_label, steps, link, link_label, about_extra="", look=None):
    """邮箱都用 IMAP/SMTP（mcp-email-server），区别只在服务器地址和密码去哪拿。"""
    return {
        "id": id_, "title": title, "category": "mail", "by": "IMAP · 社区", "kind": "token", "how": "邮箱 + 一个密码",
        "desc": "读邮件、搜邮件、整理收件箱，替你起草和发送",
        "about": f"用 IMAP 连上你的{title}。Bot 能查找、阅读、归档邮件，帮你起草回复；发送和删除前会先问你。{about_extra}",
        "can": ["按发件人、主题、时间查邮件，读正文和附件", "归档、移动、标记已读", "起草、回复、转发、发送（先问你）"],
        "example": f"@{id_} 看看今天有哪些需要我回复的邮件，帮我起草回复",
        "steps": steps, "link": link, "link_label": link_label,
        "fields": [{"key": "email", "label": "邮箱地址", "placeholder": "you@example.com", "pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                    "invalid": "邮箱地址的格式不对"},
                   {"key": "password", "label": pw_label, "secret": True, "nospace": True}],
        "check": {"imap": host_imap},
        "deny": ["email_command"],
        "config": {"type": "stdio", "command": "uvx", "args": ["mcp-email-server@latest", "stdio"], "env": {
            "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "{email}", "MCP_EMAIL_SERVER_PASSWORD": "{password}",
            "MCP_EMAIL_SERVER_IMAP_HOST": host_imap, "MCP_EMAIL_SERVER_IMAP_PORT": "993",
            "MCP_EMAIL_SERVER_SMTP_HOST": host_smtp, "MCP_EMAIL_SERVER_SMTP_PORT": "465",
            # 收件人不限制：发送前 OSA 自己会弹确认卡片
            "MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS": "*"}},
    } | (look or {})


ITEMS += [
    # ----- 笔记与文档 -----
    {"id": "notion", "title": "Notion", "category": "notes", "by": "Notion 官方", "kind": "oauth",
     "desc": "搜索、阅读和编辑你的页面与数据库",
     "about": "用 Notion 官方的连接。登录后选择允许访问的页面，Bot 就能帮你查资料、写页面、往数据库里加条目。",
     "can": ["在整个工作区里搜索、阅读页面", "新建和修改页面，整理会议纪要、周报", "往数据库里加条目、改属性"],
     "example": "@notion 把刚才的讨论整理成一页会议纪要，放到「会议」数据库里",
     "deny": ["delete", "archive"],
     "config": {"type": "http", "url": "https://mcp.notion.com/mcp", "oauth": True}},
    {"id": "flomo", "title": "flomo 浮墨", "category": "notes", "by": "flomo 官方", "kind": "oauth", "glyph": "feather", "color": "#30CF79",
     "desc": "记灵感、搜笔记，让 Bot 了解你记过什么",
     "about": "flomo 官方的连接。Bot 能把想法记进 flomo，也能在你的笔记里搜索、回顾。",
     "can": ["把对话里的想法、摘录记成 memo", "按关键词、标签搜索旧笔记", "回顾某个主题下你写过的东西"],
     "example": "@flomo 把这段话记下来，打上 #读书 标签",
     "note": "需要 flomo MAX 会员。只有 PRO 的话，用下面的「flomo 快速记录」。",
     "config": {"type": "http", "url": "https://flomoapp.com/mcp", "oauth": True}},
    {"id": "flomo-api", "title": "flomo 快速记录", "category": "notes", "by": "社区", "kind": "token", "glyph": "feather", "color": "#1EA35F",
     "how": "粘贴 API 链接",
     "desc": "用 flomo 的 API 链接往 flomo 里记笔记（只能写）",
     "about": "适合 flomo PRO 会员：用 flomo 提供的 API 链接，让 Bot 把内容记进 flomo。这种方式只能新建笔记，不能搜索。",
     "can": ["把内容记成一条 memo，可以带 #标签"],
     "example": "@flomo-api 把今天的三点收获记到 flomo，标签 #日记",
     "steps": ["打开 flomo 的「API」设置页（需要 PRO 会员）", "复制页面上那条以 https://flomoapp.com/iwh/ 开头的链接", "粘贴到下面"],
     "link": "https://v.flomoapp.com/mine?source=incoming_webhook", "link_label": "打开 flomo API 设置",
     "fields": [{"key": "url", "label": "API 链接", "placeholder": "https://flomoapp.com/iwh/……", "secret": True,
                 "pattern": r"^https://flomoapp\.com/iwh/", "invalid": "链接应该以 https://flomoapp.com/iwh/ 开头"}],
     "config": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-server-flomo"], "env": {"FLOMO_API_URL": "{url}"}}},
    {"id": "tencent-docs", "title": "腾讯文档", "category": "notes", "by": "腾讯文档官方", "kind": "oauth", "glyph": "doc", "color": "#1E6FFF",
     "desc": "查找、阅读、新建和编辑你的腾讯文档",
     "about": "腾讯文档官方的连接。用 QQ 或微信登录授权后，Bot 能读写你的文档和表格。",
     "can": ["搜索和阅读文档、表格", "新建文档，把整理好的内容写进去", "修改已有文档"],
     "example": "@tencent-docs 新建一个文档，把这周的待办整理成表格",
     "deny": ["delete"],
     "config": {"type": "http", "url": "https://docs.qq.com/openapi/mcp", "oauth": True}},
    {"id": "yuque", "title": "语雀", "category": "notes", "by": "语雀官方", "kind": "token", "glyph": "book", "color": "#00B96B",
     "desc": "搜索和阅读知识库，新建和更新文档",
     "about": "语雀官方的 MCP。填一个个人 Token，Bot 就能在你的知识库里查找、阅读和写文档。",
     "can": ["列出知识库和文档，阅读正文", "搜索文档", "新建、更新文档和小记"],
     "example": "@yuque 在「产品」知识库里找一下上次的需求文档，总结要点",
     "steps": ["打开语雀的「Token」设置页，点「新建」", "权限按需勾选（只读就只勾读取），确定", "复制生成的 Token，粘贴到下面"],
     "link": "https://www.yuque.com/settings/tokens", "link_label": "打开语雀 Token 设置",
     "note": "语雀的个人 Token 可能需要超级会员。",
     "fields": [{"key": "token", "label": "个人 Token", "secret": True}],
     "deny": ["delete"],
     "config": {"type": "stdio", "command": "npx", "args": ["-y", "yuque-mcp"], "env": {"YUQUE_PERSONAL_TOKEN": "{token}"}}},
    {"id": "evernote", "title": "印象笔记 / Evernote", "category": "notes", "by": "Evernote 官方", "kind": "oauth", "icon": "evernote",
     "desc": "搜索、阅读和新建 Evernote 笔记",
     "about": "Evernote 官方的连接（国际版账号）。登录授权后，Bot 能查找和阅读你的笔记，也能新建笔记。",
     "can": ["搜索、阅读笔记", "新建笔记"],
     "example": "@evernote 找一下我去年记的装修清单",
     "note": "只支持 Evernote 国际版账号，国内的印象笔记账号登不上。",
     "config": {"type": "http", "url": "https://mcp.evernote.com/mcp", "oauth": True}},

    # ----- 邮件与日历 -----
    _mail("gmail", "Gmail", "imap.gmail.com", "smtp.gmail.com", "应用专用密码",
          ["先给 Google 账号开启两步验证（开过就跳过）", "打开「应用专用密码」页面，名字随便填（比如 Super Agent），点「创建」",
           "把弹出的 16 位密码复制到下面"],
          "https://myaccount.google.com/apppasswords", "打开 Google 应用专用密码",
          "不用在 Google Cloud 建应用，一个应用专用密码就够。"),
    _mail("qqmail", "QQ 邮箱", "imap.qq.com", "smtp.qq.com", "授权码",
          ["网页版 QQ 邮箱：设置 → 账号与安全 → 安全设置", "开启「IMAP/SMTP 服务」，按提示验证后会给你一个授权码", "把授权码复制到下面（不是 QQ 密码）"],
          "https://wx.mail.qq.com/", "打开 QQ 邮箱", look={"icon": "qq"}),
    _mail("mail163", "网易 163 邮箱", "imap.163.com", "smtp.163.com", "授权码",
          ["网页版 163 邮箱：设置 → POP3/SMTP/IMAP", "开启「IMAP/SMTP 服务」，按提示新增一个授权码", "把授权码复制到下面（不是登录密码）"],
          "https://mail.163.com/", "打开 163 邮箱", look={"glyph": "mail", "color": "#D43C33"}),

    # ----- 文件 -----
    {"id": "filesystem", "title": "文件夹", "category": "files", "by": "MCP 官方", "kind": "none", "glyph": "folder", "color": "#C98A2B",
     "desc": "读写服务器上一个指定文件夹里的文件",
     "about": "让 Bot 直接读写服务器上的一个文件夹，比如存放报告、整理资料。只能访问你指定的这个目录。",
     "can": ["列出、读取、搜索文件", "新建和修改文件、建文件夹"],
     "example": "@filesystem 把这份周报存成 weekly/本周.md",
     "fields": [{"key": "path", "label": "允许访问的目录", "default": str(WORKSPACE)}],
     "config": {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "{path}"]}},
    {"id": "dropbox", "title": "Dropbox", "category": "files", "by": "Dropbox 官方", "kind": "oauth",
     "desc": "查找、阅读和整理 Dropbox 里的文件",
     "about": "Dropbox 官方的连接。登录授权后，Bot 能搜索和阅读你的文件，帮你整理文件夹。",
     "can": ["搜索文件、读取内容", "移动、重命名、整理文件夹"],
     "example": "@dropbox 找一下去年的报税材料都在哪",
     "deny": ["delete"],
     "config": {"type": "http", "url": "https://mcp.dropbox.com/mcp", "oauth": True}},

    # ----- 任务与协作 -----
    {"id": "dida", "title": "滴答清单", "category": "tasks", "by": "滴答清单官方", "kind": "oauth", "icon": "ticktick",
     "desc": "查看和添加待办，帮你安排今天要做的事",
     "about": "滴答清单官方的连接。登录授权后，Bot 能查看你的清单和任务，帮你添加、修改、完成待办。",
     "can": ["查看今天、本周的任务", "添加任务，设置日期和提醒", "修改、完成任务"],
     "example": "@dida 把刚才说的三件事加到待办，周五之前做完",
     "config": {"type": "http", "url": "https://mcp.dida365.com/", "oauth": True}},
    {"id": "ticktick", "title": "TickTick", "category": "tasks", "by": "TickTick 官方", "kind": "oauth",
     "desc": "滴答清单国际版：查看和添加待办",
     "about": "TickTick（滴答清单国际版）官方的连接。登录授权后，Bot 能查看和管理你的任务。",
     "can": ["查看任务和清单", "添加、修改、完成任务"],
     "example": "@ticktick 明天上午有什么要做的？",
     "config": {"type": "http", "url": "https://mcp.ticktick.com/", "oauth": True}},
    {"id": "todoist", "title": "Todoist", "category": "tasks", "by": "Todoist 官方", "kind": "oauth",
     "desc": "管理任务和项目，安排日程",
     "about": "Todoist 官方的连接。登录授权后，Bot 能查看、添加和完成你的任务。",
     "can": ["查看今天和即将到期的任务", "添加任务、设置截止日期和优先级", "完成、修改任务"],
     "example": "@todoist 今天还有哪些没做完？挑最重要的三件",
     "config": {"type": "http", "url": "https://ai.todoist.net/mcp", "oauth": True}},
    {"id": "linear", "title": "Linear", "category": "tasks", "by": "Linear 官方", "kind": "oauth",
     "desc": "查 issue、建 issue、更新项目进度",
     "about": "Linear 官方的连接。登录授权后，Bot 能搜索 issue、新建和更新 issue、写评论。",
     "can": ["搜索和阅读 issue、项目", "新建 issue，指派、设优先级", "更新状态、写评论"],
     "example": "@linear 把刚才发现的三个 bug 建成 issue，指派给我",
     "config": {"type": "http", "url": "https://mcp.linear.app/mcp", "oauth": True}},

    # ----- 开发 -----
    {"id": "github", "title": "GitHub", "category": "dev", "by": "GitHub 官方", "kind": "token", "how": "粘贴一个 token",
     "desc": "仓库、issue、PR、代码搜索",
     "about": "GitHub 官方的远程 MCP。填一个 Personal access token，Bot 就能读代码、查 issue 和 PR、帮你提 issue。",
     "can": ["搜索代码、阅读文件", "查看和新建 issue、PR，写评论", "查看 Actions 运行结果"],
     "example": "@github 看看 kadaliao/open-super-agent 最近一周有哪些新 issue",
     "steps": ["打开 GitHub 新建 token 的页面", "选要授权的仓库和权限（只想让它看，就只给 Read 权限）", "生成后复制，粘贴到下面"],
     "link": "https://github.com/settings/personal-access-tokens/new", "link_label": "打开 GitHub token 设置",
     "fields": [{"key": "token", "label": "Personal access token", "placeholder": "github_pat_……", "secret": True}],
     "deny": ["delete"],
     "config": {"type": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer {token}"}}},
    {"id": "deepwiki", "title": "DeepWiki", "category": "dev", "by": "Cognition 官方", "kind": "none", "glyph": "book", "color": "#0F766E",
     "desc": "读懂任意公开 GitHub 仓库：架构、模块、用法",
     "about": "DeepWiki 为公开的 GitHub 仓库生成了文档。Bot 可以直接问某个仓库是怎么设计的、某个功能在哪实现。",
     "can": ["查看仓库的文档目录和内容", "就某个仓库提问，得到带出处的回答"],
     "example": "@deepwiki vercel/next.js 的路由缓存是怎么实现的？",
     "config": {"type": "http", "url": "https://mcp.deepwiki.com/mcp"}},
    {"id": "context7", "title": "Context7", "category": "dev", "by": "Upstash 官方", "kind": "none", "glyph": "doc", "color": "#16A34A",
     "desc": "查各种编程库的最新文档和示例",
     "about": "写代码时让 Bot 查最新版本的库文档，不用靠模型记忆里的旧 API。",
     "can": ["按库名查找文档", "取出某个主题的最新用法和示例代码"],
     "example": "@context7 查一下 FastAPI 最新版本里依赖注入怎么写",
     "config": {"type": "http", "url": "https://mcp.context7.com/mcp"}},
    {"id": "sentry", "title": "Sentry", "category": "dev", "by": "Sentry 官方", "kind": "oauth",
     "desc": "查线上报错，分析原因",
     "about": "Sentry 官方的连接。登录授权后，Bot 能查看项目里的报错和事件，帮你分析原因。",
     "can": ["查看最近的 issue 和报错详情", "按项目、时间、关键词搜索事件"],
     "example": "@sentry 昨天线上新增了哪些报错？最严重的是哪个？",
     "config": {"type": "http", "url": "https://mcp.sentry.dev/mcp", "oauth": True}},

    # ----- 搜索与网页 -----
    {"id": "exa", "title": "Exa 搜索", "category": "web", "by": "Exa 官方", "kind": "none", "glyph": "search", "color": "#1F40ED",
     "desc": "为 AI 设计的英文网页搜索，免配置",
     "about": "Exa 的免费远程搜索，适合查英文资料、找相似网页。免费额度有频率限制。",
     "can": ["按意思搜索网页", "读取网页正文"],
     "example": "@exa 找几篇 2026 年讲 MCP 授权的英文文章",
     "config": {"type": "http", "url": "https://mcp.exa.ai/mcp"}},
    {"id": "fetch", "title": "网页抓取", "category": "web", "by": "MCP 官方", "kind": "none", "glyph": "globe", "color": "#3B6FD8",
     "desc": "把任意网页转成干净的文字给 Bot 读",
     "about": "Bot 本来就能搜索和读网页；加上它，长网页、文档站会转成更干净的 Markdown，读得更准。",
     "can": ["读取网页正文，去掉广告和导航", "分段读很长的页面"],
     "example": "@fetch 读一下这篇文章，列出作者的三个主要观点：https://…",
     "config": {"type": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]}},

    # ----- 生活 -----
    {"id": "amap", "title": "高德地图", "category": "life", "by": "高德官方", "kind": "token", "glyph": "pin", "color": "#0091FF",
     "how": "填一个 Key",
     "desc": "查地点、规划路线、看天气",
     "about": "高德地图官方的远程 MCP。填一个高德 Web 服务 Key，Bot 就能查地点、算路线和时间、看天气。",
     "can": ["搜索地点、查地址和坐标", "规划驾车、公交、步行、骑行路线", "查城市天气"],
     "example": "@amap 周六从望京到颐和园，地铁和开车各要多久？",
     "steps": ["登录高德开放平台控制台，进入「应用管理 → 我的应用」，新建一个应用", "给应用「添加 Key」，服务平台选「Web 服务」", "把生成的 Key 复制到下面"],
     "link": "https://console.amap.com/dev/key/app", "link_label": "打开高德控制台",
     "fields": [{"key": "key", "label": "Web 服务 Key", "secret": True}],
     "config": {"type": "http", "url": "https://mcp.amap.com/mcp?key={key}"}},
    {"id": "train", "title": "12306 火车票", "category": "life", "by": "社区", "kind": "none", "glyph": "train", "color": "#1A63C4",
     "desc": "查火车票余票、车次和中转方案",
     "about": "查询 12306 的车次、余票和票价，也能找中转方案。只能查，不能买票。",
     "can": ["按日期、出发和到达城市查车次和余票", "查经停站、中转方案"],
     "example": "@train 下周五下午北京到上海还有二等座吗？",
     "config": {"type": "stdio", "command": "npx", "args": ["-y", "12306-mcp"]}},
]
