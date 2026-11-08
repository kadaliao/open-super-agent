"""小应用：Bot 做出来的可交互界面（表单、看板、工具面板……）。

小应用就是资料库里一个 html 页面（library.py 的 pages 表），多了一份清单 app：
- tools：界面里能调用的工具（内置的 web_search / fetch_url，连接了的应用的 MCP 工具）
- chat：界面能不能替用户给 Bot 发消息
另外每个小应用有一份自己的数据（page_data 表，键值对）：界面里填的、Bot 用 page_data 工具写的都在这里，两边都能读。

安全边界和普通 html 页面一样：页面在带 CSP 的沙箱 iframe 里跑，没有同源权限、connect-src 'none'，
只能通过 postMessage 和外面的网页说话。网页（web/src/applet.ts）只认来自那个 iframe 的消息、只替它做清单里允许的事，
再调服务器的接口（/api/pages/<id>/call、/data、/say）。工具调用照旧走确认规则：要确认的会在 Bot 的私聊里弹卡片。
分享出去的页面（/s/<token>）没有宿主，SDK 的调用都会失败，数据接口要登录才能用。

页面里的 SDK（window.osa）由服务器在返回原文时内联进去（inject），Bot 写页面时只管调用：
  osa.call(tool, args)           调工具，返回文字
  osa.send(text)                 替用户给 Bot 发一条消息（清单里 chat: true 才行）
  osa.store.get(key) / .all()    读这个小应用的数据
  osa.store.set(key, value) / .remove(key)
  osa.on('data', fn)             数据被 Bot 或别的窗口改了
  osa.resize(height)             一般不用：高度会自动跟着内容变
"""
import asyncio
import json
import re
from . import i18n, registry

MAX_TOOLS = 20
MAX_KEYS = 100
MAX_VALUE = 32_000       # 一个值（JSON 文本）最长
MAX_DATA = 256_000       # 全部数据最长
MAX_SAY = 4000
MAX_CALLS = 4            # 一个小应用同时最多几个工具调用在跑
_running: dict[int, int] = {}

handle = None   # 由 server 设置：async (chat, text, applet) -> None，替用户发消息


def manifest(app) -> dict | None:
    """检查 Bot 给的清单，不认识的键丢掉。None 或空就是普通页面。"""
    if not app:
        return None
    if not isinstance(app, dict):
        raise ValueError(i18n.t("app 要是一个对象"))
    tools = app.get("tools") or []
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ValueError(i18n.t("app.tools 要是工具名的列表"))
    tools = list(dict.fromkeys(t.strip() for t in tools if t.strip()))[:MAX_TOOLS]
    return {"tools": tools, "chat": bool(app.get("chat"))}


def known(tool: str) -> bool:
    """小应用能列进清单的工具：注册表里标了 applet 场合的（网页工具、连接了的应用的工具）。"""
    return registry.available(tool, registry.APPLET) is not None


def check_data(updates: dict, current: dict) -> dict:
    """合并一次改动：值为 None 的键删掉。返回合并后的全部数据，超限就报错。"""
    if not isinstance(updates, dict):
        raise ValueError(i18n.t("数据要是一个对象"))
    out = dict(current)
    for k, v in updates.items():
        if not isinstance(k, str) or not k or len(k) > 100:
            raise ValueError(i18n.t("键要是不超过 100 字的文字"))
        if v is None:
            out.pop(k, None)
            continue
        if len(json.dumps(v, ensure_ascii=False)) > MAX_VALUE:
            raise ValueError(i18n.t("「{key}」太大（最多 {n}K 字）", key=k, n=MAX_VALUE // 1000))
        out[k] = v
    if len(out) > MAX_KEYS:
        raise ValueError(i18n.t("最多 {n} 个键", n=MAX_KEYS))
    if len(json.dumps(out, ensure_ascii=False)) > MAX_DATA:
        raise ValueError(i18n.t("数据太大（最多 {n}K 字）", n=MAX_DATA // 1000))
    return out


async def call(page: dict, tool: str, args) -> str:
    """界面里调一个工具。只认清单里列的；要确认的先弹卡片（出现在 Bot 的私聊里）。"""
    from . import bots
    app = page.get("app") or {}
    if tool not in app.get("tools", ()):
        raise PermissionError(i18n.t("这个小应用不能用工具 {tool}", tool=tool))
    t = registry.available(tool, registry.APPLET)
    if not t:
        raise LookupError(i18n.t("现在没有工具 {tool}（应用可能没连接）", tool=tool))
    if not isinstance(args, dict):
        raise ValueError(i18n.t("参数要是一个对象"))
    if _running.get(page["id"], 0) >= MAX_CALLS:
        raise RuntimeError(i18n.t("这个小应用同时最多 {n} 个调用", n=MAX_CALLS))
    bot = bots.get(page["bot_id"]) if page.get("bot_id") else None
    chat_id = (bot or {}).get("chat_id") or 0
    _running[page["id"]] = _running.get(page["id"], 0) + 1
    try:
        if t.gated:
            from . import mcpapps
            await mcpapps.confirm(bot, tool, args, f"小应用「{page['title']}」调用 {tool}")
        out = await t.run(registry.Call(registry.APPLET, bot or {"id": page["bot_id"]}, chat_id, chat_id, args))
        return str(out[0] if isinstance(out, tuple) else out)
    finally:
        _running[page["id"]] -= 1
        if not _running[page["id"]]:
            del _running[page["id"]]


async def say(page: dict, text: str):
    """界面替用户给 Bot 发一条消息，发到 Bot 的私聊里，消息上标明来自哪个小应用。"""
    from . import bots
    app = page.get("app") or {}
    if not app.get("chat"):
        raise PermissionError(i18n.t("这个小应用不能发消息"))
    await send(page.get("bot_id"), text, {"id": page["id"], "title": page["title"]})


async def send(bot_id: int | None, text: str, origin: dict):
    """界面替用户给 Bot 发一条消息，发到 Bot 的私聊里。origin 记在消息上（小应用 {id, title}；应用的界面 {app, title}）。"""
    from . import bots
    text = (text or "").strip()
    if not text:
        raise ValueError(i18n.t("消息不能为空"))
    if len(text) > MAX_SAY:
        raise ValueError(i18n.t("消息太长（最多 {n} 字）", n=MAX_SAY))
    bot = bots.get(bot_id) if bot_id else None
    chat = bots.chat(bot["chat_id"]) if bot and bot.get("chat_id") else None
    if not chat:
        raise LookupError(i18n.t("发起它的 Bot 已删除"))
    assert handle
    asyncio.create_task(handle(chat, text, origin))


# ---------- 页面里的 SDK ----------
SDK_JS = r"""
(function () {
  if (window.osa) return;
  var n = 0, waits = {}, handlers = {};
  function rpc(method, params) {
    return new Promise(function (res, rej) {
      var id = ++n;
      waits[id] = { res: res, rej: rej };
      parent.postMessage({ osa: 1, id: id, method: method, params: params || {} }, '*');
      setTimeout(function () { if (waits[id]) { delete waits[id]; rej(new Error('timeout')); } }, 35 * 60 * 1000);
    });
  }
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.osa !== 1 || e.source !== parent) return;
    if (d.id && waits[d.id]) {
      var w = waits[d.id]; delete waits[d.id];
      d.error ? w.rej(new Error(d.error)) : w.res(d.result);
    } else if (d.event) {
      (handlers[d.event] || []).forEach(function (f) { try { f(d.data); } catch (err) { console.error(err); } });
    }
  });
  function height() { var b = document.body, h = document.documentElement; return Math.max(b ? b.scrollHeight : 0, h.scrollHeight, b ? b.offsetHeight : 0); }
  window.osa = {
    call: function (tool, args) { return rpc('call', { tool: tool, args: args || {} }); },
    send: function (text) { return rpc('say', { text: String(text) }); },
    store: {
      get: function (key) { return rpc('data.get', { key: key }); },
      all: function () { return rpc('data.get', {}); },
      set: function (key, value) { var o = {}; o[key] = value; return rpc('data.set', { data: o }); },
      remove: function (key) { var o = {}; o[key] = null; return rpc('data.set', { data: o }); }
    },
    on: function (event, fn) { (handlers[event] = handlers[event] || []).push(fn); },
    resize: function (h) { parent.postMessage({ osa: 1, event: 'resize', height: h == null ? height() : h }, '*'); }
  };
  function start() {
    parent.postMessage({ osa: 1, event: 'ready' }, '*');
    window.osa.resize();
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(function () { window.osa.resize(); });
      ro.observe(document.documentElement);
      if (document.body) ro.observe(document.body);
    }
    setInterval(function () { window.osa.resize(); }, 1500);
  }
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
})();
"""
_TAG = f"<script>{SDK_JS}</script>"
_HEAD = re.compile(r"<head\b[^>]*>", re.I)
_HTML = re.compile(r"<html\b[^>]*>", re.I)


def inject(html: str) -> str:
    """把 SDK 内联到页面开头（<head> 里，没有就放最前面），保证页面自己的脚本运行时 window.osa 已经在了。"""
    if m := _HEAD.search(html):
        return html[:m.end()] + _TAG + html[m.end():]
    if m := _HTML.search(html):
        return html[:m.end()] + _TAG + html[m.end():]
    return _TAG + html


GUIDE = """做小应用（可交互的界面）时 format=html 并填 app，页面里直接用 window.osa（服务器会自动注入，不用引脚本）：
  osa.call(工具名, 参数对象) → 文字结果（只能用 app.tools 里列的工具；要确认的会弹卡片）；
  osa.send(文字) 替用户给你发一条消息（要 app.chat=true），你会像收到用户消息一样接着处理；
  osa.store.get(键) / set(键, 值) / all() / remove(键) 读写这个小应用的数据，你用 page_data 工具也能读写同一份数据，
  osa.on('data', fn) 在数据被改时收到通知。页面高度会自动适应内容，不要写死 100vh；不要用 fetch，外网和接口都连不上。
用户在对话里会直接看到小应用并能操作，所以回复里附上链接就行，不用复述界面内容。"""
