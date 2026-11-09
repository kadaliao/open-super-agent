"""MCP Apps 宿主：应用（MCP 服务器）自带的界面。

规范是 MCP Apps 扩展（io.modelcontextprotocol/ui，2026-01-26 版）：
- 工具在 _meta.ui.resourceUri 指向一个 ui:// 资源（text/html;profile=mcp-app），这个工具的结果就用那个界面显示。
- _meta.ui.visibility：["model", "app"]（默认两个都有）。没有 model 的不给模型；没有 app 的界面不能调。
- 资源的 _meta.ui.csp 声明界面要连的域名（connectDomains）、要加载脚本样式图片的域名（resourceDomains）等，宿主按它拼 CSP。
- 界面和宿主之间是 postMessage 上的 JSON-RPC：ui/initialize、tool-input、tool-result、tools/call、ui/message……
  网页里的宿主在 web/src/mcpapp.ts，这里是它调的服务端：取界面（view）、替界面调工具（call）、读资源（read）、发消息（say）。

和规范不一样的一处：规范要网页宿主用“沙箱代理”（另一个源上的中间 iframe）。自己部署的服务往往只有一个地址，
没有第二个源可用，所以界面直接放在一个不透明源的 iframe 里（iframe 不给 allow-same-origin，响应头的 CSP 里也有 sandbox），
碰不到这个站点的 cookie 和接口。协议上看不出差别（代理只是转发），官方的 App SDK 照常能用；
代价是界面里没有 localStorage，界面发出的请求 Origin 是 null。
"""
import base64
import re
from types import SimpleNamespace
from . import approvals, i18n, registry

EXT = "io.modelcontextprotocol/ui"
MIME = "text/html;profile=mcp-app"
CAPABILITY = {EXT: {"mimeTypes": [MIME]}}   # 连应用时告诉它我们能显示界面
MAX_VIEWS = 3              # 一个任务的结果下面最多挂几个界面
MAX_RESULT = 200_000       # 存进消息的工具结果最多多大（JSON 文字），太大就不显示界面，只给文字
MAX_HTML = 2_000_000
MAX_DOMAINS = 20
MODEL_HINT = ("\n\n【给你的说明，不要写进答案】用户会在任务结果下面直接看到这个应用的界面，上面就是这些数据。"
              "最终答案只写一两句结论，不要贴原始数据，也不要提到界面或这条说明。")

_views: dict[tuple[str, str], tuple[str, str]] = {}   # (应用, uri) -> (html, csp)，应用断开时清掉


# ---------- 工具上的声明 ----------
def tool_ui(meta) -> tuple[str | None, list[str]]:
    """工具的 _meta → (界面的 ui:// 地址或 None, visibility)。也认已经废弃的 _meta["ui/resourceUri"]。"""
    meta = meta if isinstance(meta, dict) else {}
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    uri = ui.get("resourceUri") or meta.get("ui/resourceUri")
    vis = ui.get("visibility")
    vis = [v for v in vis if v in ("model", "app")] if isinstance(vis, list) else ["model", "app"]
    return (uri if isinstance(uri, str) and uri.startswith("ui://") else None), vis


def dump(result) -> dict:
    """CallToolResult → 发给界面的 JSON，只留规范里的字段。"""
    d = result.model_dump(by_alias=True, exclude_none=True, mode="json")
    return {k: d[k] for k in ("content", "structuredContent", "isError", "_meta") if k in d}


def view_record(app: str, tool: str, uri: str, args: dict, result) -> dict | None:
    """挂在任务结果上的一个界面：前端据此打开 iframe、发 tool-input 和 tool-result。太大就不挂。"""
    rec = {"app": app, "tool": tool, "uri": uri, "input": args, "result": dump(result)}
    import json
    return rec if len(json.dumps(rec, ensure_ascii=False)) <= MAX_RESULT else None


# ---------- CSP ----------
_SRC = re.compile(r"^(https://|wss://)(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:\d{1,5})?(/[A-Za-z0-9._~/%-]*)?$")
_LOCAL = re.compile(r"^(http|ws)://(localhost|127\.0\.0\.1)(:\d{1,5})?$")


def _domains(v) -> list[str]:
    """声明的域名只认 https:// / wss://（和本机的 http）这种干净的写法，其余丢掉：防止往 CSP 里塞别的指令。"""
    if not isinstance(v, list):
        return []
    out = [d.strip() for d in v if isinstance(d, str) and (_SRC.match(d.strip()) or _LOCAL.match(d.strip()))]
    return list(dict.fromkeys(out))[:MAX_DOMAINS]


def csp(declared) -> str:
    """按资源的 _meta.ui.csp 拼 CSP。没声明的都是 'none'；'self' 不给（这里的 self 是本站，带着登录的接口）。
    开头的 sandbox 让文档是不透明源，直接在新标签页打开也一样。"""
    d = declared if isinstance(declared, dict) else {}
    res = " ".join(_domains(d.get("resourceDomains")))
    conn = " ".join(_domains(d.get("connectDomains"))) or "'none'"
    frame = " ".join(_domains(d.get("frameDomains"))) or "'none'"
    base = " ".join(_domains(d.get("baseUriDomains"))) or "'none'"
    return ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms; default-src 'none'; "
            f"script-src 'unsafe-inline' {res}; style-src 'unsafe-inline' {res}; img-src data: blob: {res}; "
            f"font-src data: {res}; media-src data: blob: {res}; connect-src {conn}; frame-src {frame}; "
            f"base-uri {base}; form-action 'none'; object-src 'none'").replace("  ", " ")


# ---------- 给网页宿主用的 ----------
def _session(app: str):
    from . import mcp_hub
    s = mcp_hub.session_of(app)
    if not s:
        raise LookupError(i18n.t("应用 @{name} 没连接", name=app))
    return s


def _declared(app: str) -> set[str]:
    return {t.meta["mcp"]["ui"] for t in registry.by_group().get(app, []) if t.meta.get("mcp", {}).get("ui")}


async def view(app: str, uri: str) -> tuple[str, str]:
    """取界面的 HTML 和 CSP。只认这个应用的工具声明过的 ui:// 地址；同一次连接里缓存。"""
    if uri not in _declared(app):
        raise LookupError(i18n.t("应用 @{name} 没有界面 {uri}", name=app, uri=uri))
    if hit := _views.get((app, uri)):
        return hit
    rr = await _session(app).read_resource(uri)
    c = next((c for c in rr.contents if str(c.uri) == uri), rr.contents[0] if rr.contents else None)
    if c is None:
        raise LookupError(i18n.t("应用 @{name} 没有界面 {uri}", name=app, uri=uri))
    mime = (c.mime_type or "").lower().replace(" ", "")
    if mime and not mime.startswith("text/html"):
        raise LookupError(i18n.t("界面 {uri} 不是网页（{mime}）", uri=uri, mime=c.mime_type))
    html = getattr(c, "text", None)
    if html is None and getattr(c, "blob", None):
        html = base64.b64decode(c.blob).decode("utf-8", "replace")
    if not html or len(html) > MAX_HTML:
        raise LookupError(i18n.t("界面 {uri} 是空的或太大", uri=uri))
    meta = c.meta if isinstance(c.meta, dict) else {}
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    out = (html, csp(ui.get("csp")))
    _views[(app, uri)] = out
    return out


def forget(app: str):
    """应用断开或重连：缓存的界面作废。"""
    for k in [k for k in _views if k[0] == app]:
        del _views[k]


def _tool(app: str, name: str) -> registry.Tool:
    t = next((t for t in registry.by_group().get(app, []) if t.meta.get("mcp", {}).get("tool") == name), None)
    if not t:
        raise LookupError(i18n.t("应用 @{app} 没有工具 {tool}", app=app, tool=name))
    if "app" not in t.meta["mcp"].get("visibility", ()):
        raise PermissionError(i18n.t("工具 {tool} 不给界面调用", tool=name))
    return t


async def confirm(bot: dict | None, tool: str, args: dict, label: str):
    """命中确认规则的工具先弹卡片（在 Bot 的私聊里），用户拒绝就报 PermissionError。小应用和应用的界面共用。"""
    if not approvals.decide(tool, args=args)[0]:
        return
    chat_id = (bot or {}).get("chat_id")
    if not chat_id:
        raise PermissionError(i18n.t("这个操作要确认，但发起它的 Bot 已删除"))
    fake = SimpleNamespace(id=label, bot_id=bot["id"], chat_id=chat_id, approval_chat_id=chat_id, goal=label, background=False)
    ok, note = await approvals.request(fake, tool, args)
    if not ok:
        raise PermissionError(i18n.t("用户拒绝了这个操作。{note}", note=note).strip())


async def call(app: str, name: str, args, bot: dict | None) -> dict:
    """界面调同一个应用里的工具（visibility 含 app 的）。返回完整的 CallToolResult。"""
    if not isinstance(args, dict):
        raise ValueError(i18n.t("参数要是一个对象"))
    t = _tool(app, name)
    await confirm(bot, t.name, args, f"应用 @{app} 的界面调用 {name}")
    return dump(await _session(app).call_tool(name, args))


async def read(app: str, uri: str) -> dict:
    """界面读同一个应用的资源。"""
    if not isinstance(uri, str) or not uri:
        raise ValueError(i18n.t("缺少 uri"))
    rr = await _session(app).read_resource(uri)
    return rr.model_dump(by_alias=True, exclude_none=True, mode="json")
