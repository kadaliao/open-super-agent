import asyncio
import base64
import json
import logging
import mimetypes
import os
import posixpath
import shutil
import signal
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from . import backup
backup.apply_pending()   # 确认过的恢复：在打开数据库之前换上
from . import applets, approvals, auth, avatars, botadmin, bots, catalog, channels, computer, dm, events, groups, i18n, library, mcp_hub, media, memories, memory, push, router as rt, routines
from . import mcpapps, observe, outputs, plugins, prices, providers, search, security, settings, skills, tasklog, teach, templates, unfurl, vault
from .computer import link
from .computer.base import HOSTS
from . import builtin, registry  # noqa: F401  builtin：导入即注册内置工具
from .watchdog import watchdog

DIST = Path(__file__).parent / "static" / "dist"
clients: set[WebSocket] = set()
watching: dict[WebSocket, bool] = {}   # 每个页面现在是不是正被看着（前端在切换前后台时上报）


async def broadcast(event: dict):
    # 讨论串里的新消息：带上上级对话和“N 条回复”，前端据此更新被回复的那条消息、未读数和通知
    if event.get("type") == "message" and event.get("chat_id") and (th := bots.thread_event(event["chat_id"])):
        event = {**event, "thread": th}
    channels.on_event(event)   # 聊天软件（Telegram、飞书、钉钉……）里也发一份
    for ws in list(clients):
        try:
            await ws.send_json(event)
        except Exception:
            clients.discard(ws)
            watching.pop(ws, None)
    # 没有任何页面正被看着时，需要你知道的事推送到手机
    if not any(watching.get(ws, True) for ws in clients) and (msg := push.message_for(event)) and push.count():
        asyncio.create_task(push.send(*msg, urgency="high" if event.get("type") == "approval" else "normal",
                                      badge=len(approvals.pending)))

rt.notify = broadcast
approvals.notify = broadcast
mcp_hub.notify = broadcast
routines.notify = broadcast
groups.notify = broadcast
dm.notify = broadcast
teach.notify = broadcast
computer.notify = broadcast
channels.notify = broadcast
tasklog.notify = broadcast


# 工具的实现只发事件（registry.emit），由这里广播给网页
_events = registry.Context("server")
_events.on("pages_changed", lambda: broadcast({"type": "pages_changed"}))
_events.on("page_data", lambda page_id, data: broadcast({"type": "page_data", "page_id": page_id, "data": data}))
_events.on("plugins_changed", lambda: broadcast({"type": "plugins_changed"}))


async def parent_watch(pid: int, every: float = 2):
    """桌面版：启动服务的 App 异常退出、没来得及停服务时，服务自己退出，免得留下一个没人管的进程占着端口。"""
    if os.name == "nt":   # Windows 上 os.kill(pid, 0) 会直接结束那个进程，做 Windows 版时换别的办法
        return
    while True:
        await asyncio.sleep(every)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print("启动服务的 App 已经退出，服务跟着停止", flush=True)
            os.kill(os.getpid(), signal.SIGTERM)   # 和 Ctrl-C 一样正常退出：停掉聊天软件和应用
            return
        except PermissionError:   # 进程还在，只是不归这个用户
            pass


@asynccontextmanager
async def lifespan(app):
    if not auth.enabled():
        print("未设置 OSA_PASSWORD：网页不用登录就能访问，只能在本机或内网使用", flush=True)
    bots.ensure_default()
    tasklog.startup()   # 上次没做完的任务标成“已中断”，清掉太旧的任务过程
    settings.apply()
    routines.import_legacy(bots.primary()["id"])
    await mcp_hub.start()
    await plugins.start()   # 启用了的插件（服务器锁下不加载）
    await channels.start()
    bg = [asyncio.create_task(watchdog()), asyncio.create_task(routines.scheduler()),
          asyncio.create_task(computer.idle_pauser()), asyncio.create_task(search.embed_loop()),
          asyncio.create_task(prices.loop()), asyncio.create_task(tasklog.prune_loop()), asyncio.create_task(media.gc_loop())]
    if os.environ.get("OSA_PARENT_PID", "").isdigit():
        bg.append(asyncio.create_task(parent_watch(int(os.environ["OSA_PARENT_PID"]))))
    search.sync()   # 补上旧数据的索引；配了 embedding 的话后台开始算向量
    yield
    for t in bg:
        t.cancel()
    await channels.stop()
    await plugins.stop()
    await mcp_hub.stop()
    await computer.shutdown()
    if backup.RESTART:   # 网页上确认了恢复：用同样的命令行重新启动，启动时换上备份
        backup.restart()

app = FastAPI(lifespan=lifespan)
app.add_middleware(auth.AuthMiddleware)
app.include_router(auth.router)


def _err(e: Exception):
    raise HTTPException(400, str(e))


# ---------- Bot 与对话 ----------
@app.get("/api/bootstrap")
async def bootstrap(request: Request):
    if not i18n.chosen():   # 第一次打开：新装的按浏览器的语言，升级上来的老用户保持中文
        i18n.set_lang(i18n.from_header(request.headers.get("accept-language")) if settings.onboarding() else "zh")
    return {"bots": bots.list_bots(), "chats": bots.list_chats(), "settings": settings.public(),
            "shapes": bots.SHAPES, "colors": bots.COLORS,
            "tasks": [t.public() for t in rt.TASKS.values() if t.status == "running"],
            "approvals": [approvals.public(a) for a in approvals.pending.values()],
            "threads": bots.thread_list(unread=True),   # 有新回复的讨论串：对话里提醒你去看
            **_apps_out(request), "teach": teach.public(), "auth": auth.enabled(), "onboarding": settings.onboarding()}


@app.post("/api/bots")
async def create_bot(data: dict = Body(...)):
    try:
        b = bots.create(data.get("name", ""), data.get("avatar"), data.get("instructions", ""))
    except ValueError as e:
        _err(e)
    await broadcast({"type": "bots_changed"})
    return b


@app.patch("/api/bots/{bot_id}")
async def update_bot(bot_id: int, data: dict = Body(...)):
    if not bots.get(bot_id):
        raise HTTPException(404, i18n.t("没有这个 Bot"))
    try:
        b = bots.update(bot_id, **data)
    except ValueError as e:
        _err(e)
    await broadcast({"type": "bots_changed"})
    return b


@app.post("/api/bots/{bot_id}/primary")
async def make_primary(bot_id: int):
    bots.set_primary(bot_id)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: int):
    try:
        await botadmin.remove(bot_id)   # 共享的用户事实保留；页面留在资料库里
    except ValueError as e:
        _err(e)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.get("/api/chats/{chat_id}/messages")
async def messages(chat_id: int, before: int | None = None, limit: int = 60):
    return memory.page(chat_id, before, min(limit, 200))


# ---------- 讨论串 ----------
def _root(chat_id: int, root: int) -> dict:
    r = memory.conn.execute("select id, ts, role, content, meta, bot_id from messages where id=? and chat_id=?",
                            (root, chat_id)).fetchone()
    if not r:
        raise HTTPException(404, i18n.t("这个对话里没有这条消息"))
    return {"id": r[0], "ts": r[1], "role": r[2], "content": r[3], "meta": json.loads(r[4] or "{}"), "bot_id": r[5]}


@app.get("/api/chats/{chat_id}/threads")
async def list_threads(chat_id: int):
    """这个对话下面的讨论串，最近有回复的在前（对话顶上的“讨论串”列表）。"""
    return bots.thread_list(chat_id)


@app.get("/api/threads/{thread_id}")
async def get_thread_by_id(thread_id: int):
    """一个讨论串在哪（parent、root）和它的摘要：点了讨论串的系统通知、页面还不知道这个讨论串时用。"""
    th = bots.thread_event(thread_id)
    if not th:
        raise HTTPException(404, i18n.t("没有这个对话"))
    return th


@app.post("/api/threads/{thread_id}/seen")
async def thread_seen(thread_id: int, data: dict = Body(default={})):
    """看过了讨论串里到 id 为止的消息（不给 id 就是全部）。其他打开着的页面、手机跟着去掉“新回复”。"""
    try:
        th = bots.mark_seen(thread_id, data.get("id"))
    except (TypeError, ValueError) as e:
        _err(e)
    if not th:
        raise HTTPException(404, i18n.t("没有这个对话"))
    await broadcast({"type": "thread_seen", "chat_id": th["parent"], "thread": th})
    return th


@app.get("/api/chats/{chat_id}/threads/{root}")
async def get_thread(chat_id: int, root: int):
    """某条消息下的讨论串：被回复的那条消息，和讨论串的对话 id（还没人回复过是 null，不新建）。"""
    return {"root": _root(chat_id, root), "id": bots.find_thread(chat_id, root)}


@app.post("/api/chats/{chat_id}/threads")
async def open_thread(chat_id: int, data: dict = Body(...)):
    """针对某条消息回复：返回它下面的讨论串，没有就建一个。之后像普通对话一样往讨论串的 id 发消息。"""
    try:
        th = bots.thread(chat_id, int(data.get("root") or 0))
    except (ValueError, TypeError) as e:
        _err(e)
    return {"root": _root(chat_id, th["root"]), "id": th["id"]}


# ---------- 头像 ----------
@app.post("/api/avatars")
async def upload_avatar(data: dict = Body(...)):
    """传一张头像（浏览器裁好的 256 像素图片，data URL），返回地址；Bot 或群聊保存时再用上。"""
    try:
        return {"image": avatars.save(data.get("data") or "")}
    except ValueError as e:
        _err(e)


@app.get("/api/avatars/models")
async def avatar_models():
    return await avatars.models()


@app.post("/api/avatars/generate")
async def generate_avatar(data: dict = Body(...)):
    """让生图模型画一张头像，返回原图（data URL），浏览器裁好后再传上来。"""
    try:
        return {"data": await avatars.generate(data.get("prompt", ""), data.get("provider", ""), data.get("model", ""))}
    except ValueError as e:
        _err(e)


@app.get("/avatars/{aid}")
async def avatar_file(aid: str):
    """头像图片。不用登录（分享出去的页面也要显示）；地址是内容的哈希，内容不会变。"""
    got = avatars.get(aid) if len(aid) == 24 else None
    if not got:
        raise HTTPException(404)
    return Response(got[1], media_type=got[0], headers={
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'"})


# ---------- 对话里的图片 ----------
@app.put("/api/media")
async def upload_media(request: Request, name: str = ""):
    """传一张对话里用的图片（请求体就是图片，浏览器已经缩过），返回 id、宽高。发消息时放进 attachments。"""
    if int(request.headers.get("content-length") or 0) > media.MAX_BYTES:
        raise HTTPException(413, i18n.t("图片太大了（最多 {n} MB）", n=media.MAX_BYTES >> 20))
    data = await request.body()
    try:
        return media.save(data, name)
    except ValueError as e:
        _err(e)


def _media_response(mid: str, download: bool = False) -> Response:
    got = media.get(mid)
    if not got:
        raise HTTPException(404)
    mime, data, name = got
    return Response(data, media_type=mime, headers={
        "Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
        "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(name)}"})


@app.get("/api/media/{mid}")
async def media_file(mid: str, download: bool = False):
    """对话里的图片。要登录；地址是内容的哈希，内容不会变。"""
    return _media_response(mid, download)


@app.get("/media/{mid}")
async def media_page(mid: str):
    """同一张图片，给聊天软件里的链接用：不是 /api/ 开头，没登录时会先跳去登录页，登录后回到这里。"""
    return _media_response(mid)


@app.get("/api/unfurl")
async def unfurl_link(url: str = ""):
    """链接卡片：网页的标题、摘要、配图、站点名和图标。抓不到、不让抓（本机和内网地址）的返回 ok=false，前端显示普通链接。"""
    data = await unfurl.get(url)
    return {"ok": True, **data} if data else {"ok": False}


# ---------- 群聊 ----------
@app.post("/api/groups")
async def create_group(data: dict = Body(...)):
    try:
        g = bots.create_group(data.get("bot_ids") or [], data.get("title", ""), data.get("lead"))
    except (ValueError, TypeError) as e:
        _err(e)
    await broadcast({"type": "bots_changed"})
    return g


@app.patch("/api/chats/{chat_id}")
async def update_group(chat_id: int, data: dict = Body(...)):
    try:
        g = bots.update_group(chat_id, data.get("title"), data.get("bot_ids"), data.get("lead"), data.get("image"))
    except (ValueError, TypeError) as e:
        _err(e)
    await broadcast({"type": "bots_changed"})
    return g


@app.post("/api/chats/{chat_id}/pin")
async def pin_chat(chat_id: int, data: dict = Body(...)):
    """置顶 / 取消置顶：{"pinned": true|false}。置顶的私聊、群聊显示在侧栏上面那排。"""
    try:
        c = bots.set_pinned(chat_id, bool(data.get("pinned")))
    except ValueError as e:
        _err(e)
    await broadcast({"type": "bots_changed"})
    return c


@app.delete("/api/chats/{chat_id}")
async def delete_group(chat_id: int):
    chat = bots.chat(chat_id)
    if not chat or chat["kind"] != "group" or chat.get("parent"):
        raise HTTPException(400, i18n.t("只能删除群聊；Bot 的私聊随 Bot 一起删除"))
    inside = {chat_id, *bots.thread_ids(chat_id)}   # 群里的讨论串一起删
    for t in list(rt.TASKS.values()):
        if t.chat_id in inside and t.status == "running":
            rt.cancel(t.id, i18n.t("群聊已删除"))
    bots.delete_group(chat_id)
    for i in inside:
        groups.forget(i)
    channels.chat_removed(chat_id)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.post("/api/chats/{chat_id}/reset")
async def new_topic(chat_id: int):
    """新话题：之前的消息不再带进 Bot 的上下文，长期记忆还在。"""
    chat = bots.chat(chat_id)
    if not chat:
        raise HTTPException(404, i18n.t("没有这个对话"))
    if chat.get("parent"):
        raise HTTPException(400, i18n.t("讨论串里不能开新话题"))
    return {"id": await reset_chat(chat)}


async def reset_chat(chat: dict | int) -> int:
    chat = bots.chat(chat) if isinstance(chat, int) else chat
    chat_id = chat["id"]
    bot_id = chat["bot_ids"][0] if chat["kind"] == "direct" else None
    mid = memory.add("task", "[新话题]", chat_id, bot_id, kind="reset")
    await broadcast({"type": "message", "chat_id": chat_id, "bot_id": bot_id, "message": {
        "id": mid, "ts": time.time(), "role": "task", "content": "[新话题]", "meta": {"kind": "reset"}, "bot_id": bot_id}})
    return mid


# 正在跑的任务：数据库里的记录盖上内存里的实时状态
LIVE_KEYS = ("status", "steps", "started", "elapsed", "waiting_approval", "last_action")


def _live(t: dict) -> dict:
    if (live := rt.TASKS.get(t["id"])) and live.status == "running":
        p = live.public()
        t |= {k: p[k] for k in LIVE_KEYS}
    return t


@app.get("/api/tasks")
async def list_tasks(bot_id: int | None = None, chat_id: int | None = None, before: int | None = None, limit: int = 30):
    """任务列表：某个 Bot 做的（含在群里做的），或者某个对话里派的。新的在前。"""
    return [_live(t) for t in tasklog.list_tasks(bot_id, chat_id, before, max(1, min(limit, 100)))]


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: int):
    """一个任务的详情和过程（每一步想了什么、调了什么工具、拿到了什么）。"""
    t = tasklog.detail(task_id)
    if not t:
        raise HTTPException(404, i18n.t("没有这个任务"))
    return {"task": _live(t), "steps": tasklog.steps(task_id)}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    return {"result": rt.cancel(task_id, i18n.t("用户在界面上取消"))}


# ---------- 技能 ----------
def _bot_or_404(bot_id: int) -> dict:
    b = bots.get(bot_id)
    if not b:
        raise HTTPException(404, i18n.t("没有这个 Bot"))
    return b


@app.get("/api/bots/{bot_id}/skills")
async def list_skills(bot_id: int):
    _bot_or_404(bot_id)
    return skills.list_for(bot_id)


@app.post("/api/bots/{bot_id}/skills")
async def create_skill(bot_id: int, data: dict = Body(...)):
    _bot_or_404(bot_id)
    try:
        s = skills.save(None if data.get("shared") else bot_id, data.get("name", ""), data.get("body", ""),
                        data.get("description", ""), data["source"] if data.get("source") in ("teach", "import") else "manual",
                        files=data.get("files") or {}, apps=data.get("apps"))
    except ValueError as e:
        _err(e)
    await broadcast({"type": "skills_changed"})
    return s


# 技能市场：osa/catalog/skills 里随项目发布的技能，装就是复制一份给这个 Bot
@app.get("/api/bots/{bot_id}/skills/market")
async def skill_market(bot_id: int):
    _bot_or_404(bot_id)
    return skills.market(bot_id)


@app.post("/api/bots/{bot_id}/skills/market/{mid}")
async def install_market_skill(bot_id: int, mid: str):
    _bot_or_404(bot_id)
    try:
        s = skills.install(bot_id, mid)
    except ValueError as e:
        _err(e)
    await broadcast({"type": "skills_changed"})
    return s


# ---------- 演示一遍 ----------
@app.post("/api/bots/{bot_id}/teach/start")
async def teach_start(bot_id: int, data: dict = Body(default={})):
    _bot_or_404(bot_id)
    try:
        return await teach.start(bot_id, data.get("note", ""))
    except (ValueError, computer.ComputerError) as e:
        _err(e)


@app.post("/api/teach/{action}")
async def teach_action(action: str):
    """stop：结束并生成技能草稿（不保存，交给用户审核）；cancel：放弃这段录制。"""
    if action not in ("stop", "cancel"):
        raise HTTPException(404, i18n.t("未知操作"))
    try:
        if action == "stop":
            return await teach.stop()
        await teach.cancel()
        return {"ok": True}
    except (ValueError, computer.ComputerError) as e:
        _err(e)


# ---------- 推送 ----------
@app.get("/api/push")
async def push_info():
    return {"key": push.public_key(), "count": push.count()}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, data: dict = Body(...)):
    try:
        n = push.subscribe(data.get("subscription") or {}, request.headers.get("user-agent", ""))
    except ValueError as e:
        _err(e)
    return {"count": n}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(data: dict = Body(...)):
    push.unsubscribe(data.get("endpoint", ""))
    return {"count": push.count()}


@app.post("/api/push/test")
async def push_test():
    if not push.count():
        raise HTTPException(400, i18n.t("还没有设备开启推送"))
    return {"results": await push.send(i18n.t("推送测试"), i18n.t("能收到这条，说明这台设备的推送是通的。"), "/", "test")}


@app.put("/api/skills/{sid}")
async def update_skill(sid: int, data: dict = Body(...)):
    old = skills.get(sid)
    if not old:
        raise HTTPException(404, i18n.t("没有这个技能"))
    owner = old["bot_id"] if "shared" not in data else (None if data["shared"] else data.get("bot_id", old["bot_id"]))
    try:   # files、apps 没传就保持原样
        s = skills.save(owner, data.get("name", old["name"]), data.get("body", old["body"]),
                        data.get("description", old["description"]), old["source"], sid=sid,
                        files=(data["files"] or {}) if "files" in data else None,
                        **({"apps": data["apps"]} if "apps" in data else {}))
    except ValueError as e:
        _err(e)
    await broadcast({"type": "skills_changed"})
    return s


@app.delete("/api/skills/{sid}")
async def delete_skill(sid: int):
    skills.delete(sid)
    await broadcast({"type": "skills_changed"})
    return {"ok": True}


def _skill_file(s: dict) -> Response:
    """导出的文件：不带文件的是 .md，带了的是 zip（SKILL.md 加上文件）。"""
    data, name, kind = skills.export(s)
    return Response(data, media_type=kind, headers={"content-disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/skills/{sid}/export")
async def export_skill(sid: int):
    s = skills.get(sid)
    if not s:
        raise HTTPException(404, i18n.t("没有这个技能"))
    return _skill_file(s)


@app.post("/api/skills/{sid}/share")
async def share_skill(sid: int, data: dict = Body(...)):
    try:
        s = skills.share(sid, bool(data.get("on")))
    except ValueError as e:
        raise HTTPException(404, str(e))
    await broadcast({"type": "skills_changed"})
    return s


@app.post("/api/bots/{bot_id}/skills/parse")
async def parse_skill(bot_id: int, data: dict = Body(...)):
    """导入前先解析：一个链接、粘贴的文件内容（text）或者上传的 zip（zip：base64）→ 技能草稿，交给编辑器给用户审核，不保存。
    apps 是给界面看的（连没连），declared_apps 是 frontmatter 里写的（没写是 null），保存时原样传回来。"""
    _bot_or_404(bot_id)
    try:
        if (data.get("url") or "").strip():
            d = await skills.fetch(data["url"])
        elif data.get("zip"):
            try:
                raw = base64.b64decode(data["zip"], validate=True)
            except ValueError:
                raise ValueError(i18n.t("不是能打开的 zip 文件（{e}）", e="base64"))
            d = skills.from_zip(raw, data.get("filename") or "")
        else:
            d = skills.from_file(data.get("text") or "", data.get("filename") or "") | {"skipped": []}
    except ValueError as e:
        _err(e)
    d["declared_apps"] = d.pop("apps")
    d["apps"] = _app_status(skills.apps_of(d | {"apps": d["declared_apps"]}))
    d["exists"] = bool(skills.find(bot_id, d["name"]))
    return d


def _app_status(names: list[str]) -> list[dict]:
    """技能要用的应用连没连上；应用市场里没有、本机也没装的标出来（没处连）。"""
    have, known = mcp_hub.all_apps(), catalog.items()
    return [{"name": a, "title": catalog.title(a) if a in known or a in have else "@" + a,
             "connected": a in have, "in_market": a in known} for a in names]


@app.get("/api/shared/skills/{token}")
async def shared_skill(token: str):
    s = skills.by_token(token)
    if not s:
        raise HTTPException(404, i18n.t("链接已失效"))
    b = bots.get(s["bot_id"]) if s["bot_id"] else None
    return {k: s[k] for k in ("name", "description", "body", "updated")} | {"apps": skills.apps_of(s), "files": list(s["files"])} | (
        {"bot": {"name": b["name"], "avatar": b["avatar"]}} if b else {})


@app.get("/api/shared/skills/{token}/raw")
async def shared_skill_raw(token: str):
    s = skills.by_token(token)
    if not s:
        raise HTTPException(404, i18n.t("链接已失效"))
    return _skill_file(s)


# ---------- 检索 ----------
@app.get("/api/search")
async def search_all(q: str = "", kind: str = "all", bot_id: int | None = None):
    kinds = {"page": ("page",), "memory": ("memory",)}.get(kind, ("memory", "page"))
    return {"hits": await search.search(q, bot_id, kinds, 20), "info": search.info()}


@app.get("/api/search/info")
async def search_info():
    search.sync()
    return search.info()


# ---------- Bot 模板 ----------
@app.get("/api/templates")
async def list_templates():
    return templates.builtin()


@app.post("/api/templates/preview")
async def preview_template(data: dict = Body(...)):
    """内置的（id）、粘贴的文件内容（text）或链接（url）→ 预览。返回的 text 是检查过的模板，安装时原样交回来。"""
    try:
        if data.get("id"):
            t = templates.parse(templates.builtin_text(data["id"]))
        elif (data.get("url") or "").strip():
            t = await templates.fetch(data["url"])
        else:
            t = templates.parse(data.get("text") or "")
    except ValueError as e:
        _err(e)
    return templates.preview(t) | {"text": templates.to_text({"osa_template": templates.FORMAT, **t})}


@app.post("/api/templates/install")
async def install_template(data: dict = Body(...)):
    try:
        t = templates.parse(data.get("text") or "")
        b = templates.install(t, bool(data.get("enable_routines", True)), data.get("name") or "")
    except ValueError as e:
        _err(e)
    for ev in ("bots_changed", "skills_changed", "routines_changed"):
        await broadcast({"type": ev})
    return b


def _template_file(bot_id: int) -> Response:
    t = templates.from_bot(bot_id)
    return Response(templates.to_text(t), media_type="application/yaml; charset=utf-8",
                    headers={"content-disposition": f"attachment; filename*=UTF-8''{quote(t['name'] + '.osabot.yaml')}"})


@app.get("/api/bots/{bot_id}/template")
async def export_template(bot_id: int):
    _bot_or_404(bot_id)
    return _template_file(bot_id)


@app.get("/api/bots/{bot_id}/template/share")
async def template_share_state(bot_id: int):
    _bot_or_404(bot_id)
    return {"token": templates.token_of(bot_id)}


@app.post("/api/bots/{bot_id}/template/share")
async def share_template(bot_id: int, data: dict = Body(...)):
    _bot_or_404(bot_id)
    return {"token": templates.share(bot_id, bool(data.get("on")))}


@app.get("/api/shared/templates/{token}")
async def shared_template(token: str):
    bid = templates.by_token(token)
    if not bid:
        raise HTTPException(404, i18n.t("链接已失效"))
    t = templates.preview(templates.from_bot(bid))
    for a in t["apps"]:
        a.pop("connected", None)   # 那台连没连应用和看的人无关，不说
    return t


@app.get("/api/shared/templates/{token}/raw")
async def shared_template_raw(token: str):
    bid = templates.by_token(token)
    if not bid:
        raise HTTPException(404, i18n.t("链接已失效"))
    return _template_file(bid)


# ---------- 备份与迁移 ----------
STARTED = time.time()
MAX_BACKUP = 4 << 30


@app.get("/api/backup")
async def backup_info():
    return {"counts": backup.counts(memory.conn), "secrets": len(vault.names()), "staged": backup.staged(),
            "started": STARTED, "version": backup.version()}


def _no_backup_in_demo():
    """演示服务器的数据大家共用：不让导出（里面有别人存的密钥），也不让恢复（会换掉整个数据库）。"""
    if security.demo():
        raise HTTPException(403, i18n.t("演示服务器上不能导出或恢复备份"))


@app.post("/api/backup/export")
async def backup_export(data: dict = Body(...)):
    _no_backup_in_demo()
    try:
        backup.check_passphrase(data.get("passphrase") or "")
        snap = backup.snapshot()   # 快照在这里做（用的是服务的数据库连接），压缩、加密放到线程里
        path, meta = await asyncio.to_thread(backup.export, data["passphrase"], snap)
    except backup.BackupError as e:
        _err(e)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name,
                        headers={"x-backup-secrets": str(meta["secrets"]), "x-backup-unreadable": str(len(meta["unreadable"]))},
                        background=BackgroundTask(shutil.rmtree, path.parent, True))


@app.put("/api/backup/restore")
async def backup_upload(request: Request):
    """上传备份文件（请求体就是文件，口令放在 x-passphrase 头里，base64 编码）：解密、检查，返回摘要等确认。"""
    _no_backup_in_demo()
    if backup.RESTART:
        raise HTTPException(409, i18n.t("正在重启，稍后再试"))
    try:
        passphrase = base64.b64decode(request.headers.get("x-passphrase", "")).decode()
    except ValueError:
        passphrase = ""
    if not passphrase:
        raise HTTPException(400, i18n.t("请填备份时设的口令"))
    fd, tmp = tempfile.mkstemp(suffix=".osabak")
    try:
        size = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BACKUP:
                    raise HTTPException(413, i18n.t("文件太大了"))
                f.write(chunk)
        if not size:
            raise HTTPException(400, i18n.t("没有收到文件"))
        return await asyncio.to_thread(backup.stage, Path(tmp), passphrase)
    except backup.BackupError as e:
        _err(e)
    finally:
        os.unlink(tmp)


@app.post("/api/backup/restore/confirm")
async def backup_confirm():
    _no_backup_in_demo()
    try:
        backup.confirm()
    except backup.BackupError as e:
        _err(e)
    await broadcast({"type": "restarting"})
    backup.request_restart()
    return {"restarting": True}


@app.delete("/api/backup/restore")
async def backup_cancel():
    backup.cancel()
    return {"ok": True}


# ---------- 长期记忆 ----------
@app.get("/api/bots/{bot_id}/memories")
async def list_memories(bot_id: int):
    _bot_or_404(bot_id)
    return memories.list_for(bot_id)


@app.post("/api/bots/{bot_id}/memories")
async def add_memory(bot_id: int, data: dict = Body(...)):
    _bot_or_404(bot_id)
    try:
        m = memories.add(None if data.get("shared") else bot_id, data.get("text", ""), "manual")
    except ValueError as e:
        _err(e)
    await broadcast({"type": "memories_changed"})
    return m


@app.post("/api/bots/{bot_id}/memories/import")
async def import_memories(bot_id: int, data: dict = Body(...)):
    _bot_or_404(bot_id)
    try:
        saved = await memories.import_text(bot_id, data.get("text", ""))
    except ValueError as e:
        _err(e)
    await broadcast({"type": "memories_changed"})
    return saved


@app.patch("/api/memories/{mid}")
async def update_memory(mid: int, data: dict = Body(...)):
    m = memories.get(mid)
    if not m:
        raise HTTPException(404, i18n.t("没有这条记忆"))
    owner = m["bot_id"]
    if "shared" in data:
        owner = None if data["shared"] else (m["bot_id"] or data.get("bot_id"))
        if not data["shared"] and not (isinstance(owner, int) and bots.get(owner)):
            raise HTTPException(400, i18n.t("改成只给某个 Bot 时要说明是哪个 Bot"))
    try:
        m = memories.update(mid, data.get("text"), owner)
    except ValueError as e:
        _err(e)
    await broadcast({"type": "memories_changed"})
    return m


@app.delete("/api/memories/{mid}")
async def delete_memory(mid: int):
    memories.delete(mid)
    await broadcast({"type": "memories_changed"})
    return {"ok": True}


# ---------- 资料库：页面 ----------
def _page_or_404(pid: int, content: bool = False) -> dict:
    p = library.get_page(pid, content)
    if not p:
        raise HTTPException(404, i18n.t("没有这个页面"))
    return p


def _raw(page: dict) -> Response:
    """html 页面的原文，带 CSP：在沙箱里运行，碰不到这个站点的接口和数据。"""
    if page["format"] != "html":
        raise HTTPException(404, i18n.t("只有 html 页面有原文地址"))
    html = applets.inject(page["content"]) if page.get("app") else page["content"]
    return HTMLResponse(html, headers={"Content-Security-Policy": library.RAW_CSP,
                                                  "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


@app.get("/api/pages")
async def list_pages():
    return library.list_pages()


@app.post("/api/pages")
async def create_page(data: dict = Body(...)):
    try:
        p = library.save_page(data.get("bot_id"), data.get("title", ""), data.get("content", ""),
                              data.get("format") or "markdown", task_id=data.get("task_id"), app=data.get("app"))
    except (ValueError, TypeError) as e:
        _err(e)
    await broadcast({"type": "pages_changed"})
    return p


@app.get("/api/pages/{pid}")
async def get_page(pid: int):
    return _page_or_404(pid, True)


@app.get("/api/pages/{pid}/raw")
async def page_raw(pid: int):
    return _raw(_page_or_404(pid, True))


@app.patch("/api/pages/{pid}")
async def update_page(pid: int, data: dict = Body(...)):
    _page_or_404(pid)
    p = library.update_page(pid, data.get("title"), data.get("pinned"))
    await broadcast({"type": "pages_changed"})
    return p


@app.post("/api/pages/{pid}/share")
async def share_page(pid: int, data: dict = Body(...)):
    _page_or_404(pid)
    p = library.share(pid, bool(data.get("on")))
    await broadcast({"type": "pages_changed"})
    return p


@app.delete("/api/pages/{pid}")
async def delete_page(pid: int):
    library.delete_page(pid)
    await broadcast({"type": "pages_changed"})
    return {"ok": True}


# ---------- 应用自带的界面（MCP Apps）：网页宿主 web/src/mcpapp.ts 调过来 ----------
def _mcp_errors(e: Exception):
    if isinstance(e, PermissionError):
        raise HTTPException(403, str(e))
    if isinstance(e, LookupError):
        raise HTTPException(404, str(e))
    if isinstance(e, (ValueError, TypeError)):
        raise HTTPException(400, str(e))
    raise HTTPException(502, i18n.t("应用出错：{e}", e=providers.brief(e)))


@app.get("/api/mcp-apps/{app_name}/view")
async def mcp_app_view(app_name: str, uri: str):
    """应用的界面：CSP 按它声明的域名拼，开头带 sandbox（不透明源，碰不到这个站点）。"""
    try:
        html, csp = await mcpapps.view(app_name, uri)
    except Exception as e:
        _mcp_errors(e)
    return HTMLResponse(html, headers={"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
                                       "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


@app.post("/api/mcp-apps/{app_name}/call")
async def mcp_app_call(app_name: str, data: dict = Body(...)):
    """界面调同一个应用里给界面用的工具；命中确认规则的在 Bot 的私聊里弹卡片，一直等到用户决定。"""
    try:
        return await mcpapps.call(app_name, str(data.get("name") or ""), data.get("arguments") or {},
                                  bots.get(data["bot_id"]) if isinstance(data.get("bot_id"), int) else None)
    except Exception as e:
        _mcp_errors(e)


@app.post("/api/mcp-apps/{app_name}/read")
async def mcp_app_read(app_name: str, data: dict = Body(...)):
    try:
        return await mcpapps.read(app_name, data.get("uri"))
    except Exception as e:
        _mcp_errors(e)


@app.post("/api/mcp-apps/{app_name}/say")
async def mcp_app_say(app_name: str, data: dict = Body(...)):
    """界面替用户给 Bot 发消息（ui/message）：发到结果所属 Bot 的私聊。"""
    if not mcp_hub.session_of(app_name):
        raise HTTPException(404, i18n.t("应用 @{name} 没连接", name=app_name))
    try:
        await applets.send(data.get("bot_id") if isinstance(data.get("bot_id"), int) else None, str(data.get("text") or ""),
                           {"app": app_name, "title": "@" + app_name})
    except Exception as e:
        _mcp_errors(e)
    return {"ok": True}


# ---------- 插件（osa/plugins.py）：只有用户在设置里能启用，Bot 没有对应的工具 ----------
def _plugin_errors(e: Exception):
    if isinstance(e, PermissionError):
        raise HTTPException(403, str(e))
    if isinstance(e, LookupError):
        raise HTTPException(404, str(e))
    if isinstance(e, (ValueError, TypeError)):
        raise HTTPException(400, str(e))
    raise HTTPException(500, str(e))


@app.get("/api/plugins")
async def list_plugins():
    return plugins.listing()


@app.get("/api/plugins/{name}/source")
async def plugin_source(name: str, catalog: bool = False):
    try:
        return {"source": plugins.read_source(name, catalog)}
    except Exception as e:
        _plugin_errors(e)


@app.post("/api/plugins/{name}/{action}")
async def plugin_action(name: str, action: str):
    fn = {"enable": plugins.enable, "disable": plugins.disable, "reload": plugins.reload, "install": plugins.install}.get(action)
    if not fn:
        raise HTTPException(404, i18n.t("没有这个操作"))
    try:
        await fn(name)
    except Exception as e:
        _plugin_errors(e)
    return plugins.info(name) if action != "install" else {"ok": True}


@app.put("/api/plugins/{name}/config")
async def plugin_config(name: str, data: dict = Body(...)):
    try:
        await plugins.set_config(name, data.get("config"))
    except Exception as e:
        _plugin_errors(e)
    return plugins.info(name)


@app.delete("/api/plugins/{name}")
async def delete_plugin(name: str):
    try:
        await plugins.remove(name)
    except Exception as e:
        _plugin_errors(e)
    return {"ok": True}


@app.get("/api/bots/{bot_id}/outputs")
async def bot_outputs(bot_id: int, request: Request):
    """右栏“资料库”：这个 Bot 交出来的页面、文件、图片、链接，新的在前（见 outputs.py）。"""
    _bot_or_404(bot_id)
    return outputs.for_bot(bot_id, own_hosts={request.headers.get("host", ""), request.url.netloc})
# ---------- 小应用：界面经网页宿主调过来（web/src/applet.ts） ----------
def _applet_or_404(pid: int) -> dict:
    p = _page_or_404(pid)
    if not p.get("app"):
        raise HTTPException(400, i18n.t("页面 #{id} 不是小应用", id=pid))
    return p


@app.get("/api/pages/{pid}/data")
async def page_data(pid: int):
    _applet_or_404(pid)
    return library.get_data(pid)


@app.put("/api/pages/{pid}/data")
async def put_page_data(pid: int, data: dict = Body(...)):
    _applet_or_404(pid)
    try:
        out = library.set_data(pid, data)
    except (ValueError, TypeError) as e:
        _err(e)
    await broadcast({"type": "page_data", "page_id": pid, "data": out})
    return out


@app.post("/api/pages/{pid}/call")
async def page_call(pid: int, data: dict = Body(...)):
    """小应用调一个清单里允许的工具。要确认的在 Bot 的私聊里弹卡片，这个请求一直等到用户决定。"""
    p = _applet_or_404(pid)
    try:
        return {"result": await applets.call(p, str(data.get("tool") or ""), data.get("args") or {})}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, TypeError, RuntimeError) as e:
        _err(e)
    except Exception as e:
        raise HTTPException(500, i18n.t("工具出错：{e}", e=providers.brief(e)))


@app.post("/api/pages/{pid}/say")
async def page_say(pid: int, data: dict = Body(...)):
    p = _applet_or_404(pid)
    try:
        await applets.say(p, str(data.get("text") or ""))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, TypeError) as e:
        _err(e)
    return {"ok": True}


@app.get("/api/shared/{token}")
async def shared_page(token: str):
    p = library.by_token(token)
    if not p:
        raise HTTPException(404, i18n.t("链接已失效"))
    b = bots.get(p["bot_id"]) if p["bot_id"] else None
    return {k: p[k] for k in ("title", "format", "content", "updated")} | (
        {"bot": {"name": b["name"], "avatar": b["avatar"]}} if b else {})


@app.get("/api/shared/{token}/raw")
async def shared_raw(token: str):
    p = library.by_token(token)
    if not p:
        raise HTTPException(404, i18n.t("链接已失效"))
    return _raw(p)


# ---------- 资料库：电脑里的文件 ----------
async def _files_pc(start: bool):
    """文件都在共用电脑里。start=False 时电脑没开就返回 None，不替用户开机。"""
    c = computer.get()
    if (await c.status())["state"] != "running":
        if not start:
            return None
        try:
            await c.start()
        except computer.ComputerError as e:
            _err(e)
    return c


@app.get("/api/files")
async def list_files(path: str = "", start: bool = False):
    try:
        p = library.norm(path)
    except ValueError as e:
        _err(e)
    c = await _files_pc(start)
    if not c:
        return {"running": False, "state": (await computer.get().status())["state"], "path": p, "root": library.root()}
    try:
        entries = await library.list_dir(c, p)
        pinned = library.pins()
        stats = await library.stat_many(c, pinned)
    except (ValueError, computer.ComputerError) as e:
        _err(e)
    return {"running": True, "path": p, "root": library.root(), "entries": entries,
            "pins": [stats.get(x) or {"name": posixpath.basename(x), "path": x, "missing": True} for x in pinned]}


@app.get("/api/files/download")
async def download_file(path: str):
    try:
        p = library.norm(path)
        c = await _files_pc(True)
        data = await c.read_bytes(p, library.MAX_UPLOAD * 2)
    except (ValueError, computer.ComputerError) as e:
        _err(e)
    name = posixpath.basename(p)
    kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
    # 图片、PDF、纯文本在浏览器里直接看，其余下载；html 一律当附件，免得在这个站点下运行
    inline = kind.startswith("image/") and kind != "image/svg+xml" or kind in ("application/pdf", "text/plain")
    return Response(data, media_type=kind if inline else "application/octet-stream", headers={
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff"})


@app.get("/files/download")
async def download_file_page(path: str):
    """聊天软件里的文件链接：和 /api/files/download 一样，没登录时先跳去登录页。"""
    return await download_file(path)


@app.put("/api/files/content")
async def upload_file(request: Request, dir: str = "", name: str = ""):
    """上传一个文件到电脑里的目录（请求体就是文件内容）。同名时自动加序号，不覆盖。"""
    try:   # 先检查路径和文件名，不对就不用读请求体
        library.norm(dir)
        library.check_name(name)
    except ValueError as e:
        _err(e)
    data = await request.body()
    if len(data) > library.MAX_UPLOAD:
        raise HTTPException(413, i18n.t("文件太大（最多 {n} MB）", n=library.MAX_UPLOAD >> 20))
    c = await _files_pc(True)
    try:
        return await library.put_file(c, dir, name, data)
    except (ValueError, computer.ComputerError) as e:
        _err(e)


@app.delete("/api/files")
async def delete_file(path: str):
    c = await _files_pc(True)
    try:
        await library.remove(c, path)
    except (ValueError, computer.ComputerError) as e:
        _err(e)
    return {"ok": True}


@app.post("/api/files/pin")
async def pin_file(data: dict = Body(...)):
    try:
        library.pin(data.get("path", "")) if data.get("on") else library.unpin(data.get("path", ""))
    except ValueError as e:
        _err(e)
    return {"ok": True, "pins": library.pins()}


# ---------- 定时任务和事件 ----------
def _routine_or_404(rid: int) -> dict:
    r = routines.get(rid)
    if not r:
        raise HTTPException(404, i18n.t("没有这个定时任务或事件"))
    return r


@app.get("/api/routines")
async def all_routines():
    """所有 Bot 的定时任务和事件（设置里的一览，能在那里开关）。"""
    return routines.list_for()


@app.get("/api/event-sources")
async def event_sources():
    """“新建事件”里能选的来源（内置的和插件提供的）、它们的参数和常见用法。"""
    return events.catalog()


def _remember_origin(request: Request):
    """第一次建 webhook 类的事件时记下网页地址：Bot 在对话里告诉用户地址时要用完整的。"""
    if not channels.public_url():
        origin = request.headers.get("origin") or ""
        if origin.startswith(("http://", "https://")) and "localhost" not in origin and "127.0.0.1" not in origin:
            try:
                channels.set_public_url(origin)
            except ValueError:
                pass


@app.get("/api/bots/{bot_id}/routines")
async def list_routines(bot_id: int):
    _bot_or_404(bot_id)
    return routines.list_for(bot_id)


@app.post("/api/bots/{bot_id}/routines")
async def create_routine(bot_id: int, request: Request, data: dict = Body(...)):
    _bot_or_404(bot_id)
    _remember_origin(request)
    try:
        r = routines.save(bot_id, data)
    except (ValueError, TypeError) as e:
        _err(e)
    await broadcast({"type": "routines_changed", "bot_id": bot_id})
    return r


@app.put("/api/routines/{rid}")
async def update_routine(rid: int, data: dict = Body(...)):
    old = _routine_or_404(rid)
    try:
        r = routines.save(old["bot_id"], data, rid)
    except (ValueError, TypeError) as e:
        _err(e)
    await broadcast({"type": "routines_changed", "bot_id": old["bot_id"]})
    return r


@app.post("/api/routines/{rid}/{action}")
async def routine_action(rid: int, action: str):
    r = _routine_or_404(rid)
    note = ""
    if action in ("pause", "resume"):
        r = routines.set_enabled(rid, action == "resume")
    elif action == "test":
        if routines.is_event(r):
            # 要轮询的先真去看一次；没有新的就拿最近看到的一条（或示例）跑一遍
            res = await events.test(r)
            note = "" if res.get("accepted") else res.get("reason", "")
            if res.get("queued"):
                note = i18n.t("上一次还没跑完，排队了")
        else:
            res = await routines.fire(r, "test")
            if not res["accepted"]:
                _err(ValueError(res["reason"]))
        r = routines.get(rid)
    elif action == "check" and routines.is_event(r):
        res = await events.poll(r, now=True)
        note = i18n.t("有新的，已经交给 Bot") if res.get("accepted") else res.get("reason", "")
        r = routines.get(rid)
    elif action == "rekey" and r["trigger"].get("key"):
        r = routines.rekey(rid)
    else:
        raise HTTPException(404, i18n.t("未知操作"))
    await broadcast({"type": "routines_changed", "bot_id": r["bot_id"]})
    return r | ({"note": note} if note else {})


@app.delete("/api/routines/{rid}")
async def delete_routine(rid: int):
    r = _routine_or_404(rid)
    routines.delete(rid)
    await broadcast({"type": "routines_changed", "bot_id": r["bot_id"]})
    return {"ok": True}


@app.get("/api/routines/{rid}/runs")
async def routine_runs(rid: int):
    _routine_or_404(rid)
    return routines.runs(rid)


# ---------- 电脑 ----------
def _pc(bot_id: int):
    if not bots.get(bot_id):
        raise HTTPException(404, i18n.t("没有这个 Bot"))
    return computer.get(bot_id)


async def _pc_state(bot_id: int) -> dict:
    _pc(bot_id)
    return await computer.public_state(bot_id)


@app.get("/api/bots/{bot_id}/computer")
async def pc_status(bot_id: int):
    return await _pc_state(bot_id)


@app.get("/api/bots/{bot_id}/computer/shot")
async def pc_shot(bot_id: int, view: str = ""):
    """本机电脑的画面：Bot 正在用的那个窗口的截图，网页上每隔一两秒取一次（没有 VNC）。
    打开了“操作整台电脑”时 view 选 screen（整个屏幕）/ chrome，不给就跟着 Bot 最近一次操作的走。"""
    c = _pc(bot_id)
    try:
        data = await c.peek("jpeg", view)
    except computer.ComputerError as e:
        raise HTTPException(409, str(e))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/bots/{bot_id}/computer/type")   # 要放在 /computer/{action} 前面，否则会被它先匹配
async def pc_type(bot_id: int, data: dict = Body(...)):
    """你接手电脑时从网页输入文字或按键（手机上点画面弹不出键盘，用这个）。"""
    c = _pc(bot_id)
    if c.controller != "human":
        raise HTTPException(400, i18n.t("先接手电脑再输入"))
    try:
        if data.get("text"):
            await c.type(str(data["text"])[:2000], as_human=True)
        if data.get("key"):
            await c.press(str(data["key"]), as_human=True)
    except computer.ComputerError as e:
        _err(e)
    return {"ok": True}


@app.post("/api/bots/{bot_id}/computer/{action}")
async def pc_action(bot_id: int, action: str, data: dict | None = Body(None)):
    c = _pc(bot_id)
    try:
        if action == "start":
            await c.start()
        elif action == "pause":
            await c.pause()
        elif action == "takeover":
            desk = c.backend in HOSTS and c.desk_busy()   # Bot 在用整个屏幕：接手就是让它停下，不用开 Chrome
            if not desk:
                await c.start()
            # 从“请你帮忙”卡片接手：先把请你帮忙的那个任务（Heavy 里是那个研究员）的窗口放到最前面
            tid = (data or {}).get("task_id")
            key = c.asking.get(tid) or next((k for k, u in c.users.items() if tid and u.get("task_id") == tid), None)
            if key in c.users and c.users[key].get("window"):
                await c.front(key)
            elif c.backend in HOSTS and not desk:   # 浏览器窗口就在你屏幕上：放到最前面，接手就是直接在里面操作
                await c.show()
            c.takeover()
        elif action == "show" and c.backend in HOSTS:
            await c.show()
        elif action == "handback":
            return await handback(bot_id)
        else:
            raise HTTPException(404, i18n.t("未知操作"))
    except computer.ComputerError as e:
        _err(e)
    state = await _pc_state(bot_id)
    await broadcast({"type": "computer", "computer": state})
    return state


@app.post("/api/computer/desktop/grant")
async def pc_desktop_grant():
    """“操作整台电脑”还差 macOS 权限：在那台 Mac 上弹系统的授权提示，打开系统设置里对应的页面。"""
    c = computer.get()
    if c.backend not in HOSTS or not c.desk_supported():
        raise HTTPException(400, i18n.t("只有在 Mac 上用本机电脑时才能操作整台电脑"))
    try:
        await c.grant()
    except computer.ComputerError as e:
        _err(e)
    state = await computer.public_state()
    await broadcast({"type": "computer", "computer": state})
    return state


async def handback(bot_id: int) -> dict:
    """交还电脑。如果 Bot 正在等你帮忙，交还就表示处理完了。"""
    _pc(bot_id).handback()
    for a in list(approvals.pending.values()):
        if a["bot_id"] == bot_id and a.get("kind") == "help":
            approvals.resolve(a["id"], True, "")
    state = await _pc_state(bot_id)
    await broadcast({"type": "computer", "computer": state})
    return state


@app.get("/api/computer/machines")
async def pc_machines():
    """Instabox 账号下已有的机器，可以选一台当共用电脑。"""
    c = computer.get()
    if not hasattr(c, "list_machines"):
        return {"backend": c.backend, "machines": [], "fixed": False}
    try:
        return {"backend": c.backend, "machines": await c.list_machines(), "fixed": bool(c.fixed_machine)}
    except computer.ComputerError as e:
        _err(e)


@app.post("/api/computer/use")
async def pc_use(data: dict = Body(...)):
    c = computer.get()
    mid = (data.get("machine_id") or "").strip()
    if not hasattr(c, "use_machine") or not mid:
        raise HTTPException(400, i18n.t("只有 Instabox 后端能选已有的机器"))
    if c.users or c.exclusive:
        raise HTTPException(400, i18n.t("电脑正在被用着（{who}），等用完再换", who=c.busy()))
    try:
        if mid not in {m["id"] for m in await c.list_machines()}:
            raise HTTPException(404, i18n.t("账号下没有这台机器"))
        c.use_machine(mid)
    except computer.ComputerError as e:
        _err(e)
    state = await _pc_state(int(data.get("bot_id") or bots.primary()["id"]))
    await broadcast({"type": "computer", "computer": state})
    return state


# ---------- 服务器借用你的 Mac（computer/link.py） ----------
@app.get("/api/computer/links")
async def pc_links():
    return link.links()


@app.post("/api/computer/links")
async def pc_link_create(data: dict | None = Body(None)):
    """发一个借用令牌，网页交给桌面 App，App 在 Mac 上问过你再存起来。"""
    if security.demo():
        raise HTTPException(403, i18n.t("演示服务器上不能借用你的电脑"))
    lid, token = link.create(str((data or {}).get("name") or "Mac"))
    return {"id": lid, "token": token}


@app.delete("/api/computer/links/{lid}")
async def pc_link_delete(lid: int):
    await link.revoke(lid)
    await broadcast({"type": "computer", "computer": await computer.public_state()})
    return {"ok": True}


@app.websocket("/api/computer/link")
async def pc_link_ws(sock: WebSocket):
    await link.serve(sock)


@app.post("/api/computer/watch")
async def pc_watch(data: dict = Body(...)):
    """画面固定在某个任务的窗口上（key 为空就取消固定，画面跟着最近的操作走）。"""
    c = computer.get()
    key = data.get("key") or None
    try:
        if key:
            if not (c.users.get(key) or {}).get("window"):
                raise HTTPException(404, i18n.t("这个任务已经不用电脑了，或者还没打开浏览器"))
            await c.front(key)
        else:
            c.watch = None
    except computer.ComputerError as e:
        _err(e)
    state = await computer.public_state()
    await broadcast({"type": "computer", "computer": state})
    return state


@app.websocket("/api/bots/{bot_id}/computer/vnc")
async def pc_vnc(sock: WebSocket, bot_id: int):
    """网页上的 noVNC ⇄ 电脑的 VNC。只做字节转发；能不能操作由前端的“接手”状态决定。"""
    proto = "binary" if "binary" in (sock.headers.get("sec-websocket-protocol") or "") else None
    await sock.accept(subprotocol=proto)
    c = computer.get(bot_id)
    try:
        await c.start()
        recv, send, close = await c.open_vnc()
    except Exception as e:
        await sock.close(code=1011, reason=str(e)[:100])
        return

    async def up():
        while True:
            m = await sock.receive()
            if m["type"] == "websocket.disconnect":
                return
            data = m.get("bytes") or (m.get("text") or "").encode()
            if data:
                c.last_used = __import__("time").time()
                await send(data)

    async def down():
        while True:
            await sock.send_bytes(await recv())

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    try:
        await close()
        await sock.close()
    except Exception:
        pass


# ---------- 应用、确认规则、保险库 ----------
def _apps_out(request: Request) -> dict:
    # oauth_callback：自己建 OAuth 客户端（比如 Slack）时要填到对方那里的回调地址，和授权时实际用的一样
    return {"apps": mcp_hub.public(), "catalog": catalog.public(), "categories": catalog.categories(),
            "oauth_callback": str(request.url_for("oauth_callback"))}


@app.get("/api/apps")
async def list_apps(request: Request):
    return _apps_out(request)


async def _app_out(name: str) -> dict:
    await broadcast({"type": "apps_changed"})
    return next(x for x in mcp_hub.public() if x["name"] == name)


@app.post("/api/market/{item_id}")
async def install_app(item_id: str, data: dict = Body(default={})):
    """从应用市场添加：values 是用户填的内容（token 之类），密钥存进保险库，配置里只留引用。"""
    if mcp_hub.get(item_id):
        _err(ValueError(i18n.t("已经添加过了")))
    values = data.get("values") or {}
    try:
        security.check_market(item_id)
        await catalog.check(item_id, values)
        mcp_hub.save(item_id, catalog.build(item_id, values) | {"enabled": True})
    except ValueError as e:
        _err(e)
    if catalog.items()[item_id]["kind"] == "oauth":     # 先填字段再登录的（Slack）：前端接着打开授权弹窗
        return await _app_out(item_id)
    await mcp_hub.connect(item_id)
    if item_id not in mcp_hub._connected:   # 没连上就撤掉，用户改一下再点连接就行
        msg = str(mcp_hub.status.get(item_id) or i18n.t("没连上"))
        await mcp_hub.disconnect(item_id)
        mcp_hub.remove(item_id)
        await broadcast({"type": "apps_changed"})
        if any(w in msg for w in ("401", "403", "400 Bad Request", "Unauthorized", "parse JSON", "密钥无效", "鉴权失败", "invalid or expired")):
            msg = i18n.t("对方没有接受这个密钥，检查一下有没有复制完整。（{msg}）", msg=msg[:120])
        _err(ValueError(msg))
    return await _app_out(item_id)


@app.put("/api/apps/{name}")
async def save_app(name: str, data: dict = Body(...)):
    """自定义或高级设置。secrets 里的明文先存进保险库，配置里只留 vault:名字；token 放进 Authorization 请求头。"""
    if security.demo():   # 应用市场填的值有格式检查，这里没有（比如能把地址改到服务器的内网）
        raise HTTPException(403, i18n.t("演示服务器上只能添加应用市场里的应用，不能自定义或改高级设置"))
    try:
        old = mcp_hub.get(name) or {}
        owned = list(old.get("owned") or [])
        env = dict(data.get("env") or old.get("env") or {})
        for k, v in (data.pop("secrets", None) or {}).items():
            if v:
                ref = f"{name}_{k}".upper().replace("-", "_")
                vault.put(ref, v)
                env[k] = f"vault:{ref}"
                owned.append(ref)
        if token := (data.pop("token", None) or "").strip():
            ref = f"{name}_TOKEN".upper().replace("-", "_")
            vault.put(ref, token)
            data["headers"] = {**(data.get("headers") or old.get("headers") or {}), "Authorization": f"Bearer {{vault:{ref}}}"}
            owned.append(ref)
        a = mcp_hub.save(name, data | {"env": env, "owned": sorted(set(owned))})
    except ValueError as e:
        _err(e)
    if a["enabled"]:
        await mcp_hub.connect(name)
    else:
        await mcp_hub.disconnect(name)
        mcp_hub.status[name] = i18n.later("未启用")
    return await _app_out(name)


@app.post("/api/apps/{name}/{action}")
async def app_action(name: str, action: str):
    if not mcp_hub.get(name):
        raise HTTPException(404, i18n.t("没有这个应用"))
    if action in ("enable", "reconnect"):
        mcp_hub.save(name, {"enabled": True})
        await mcp_hub.connect(name)
    elif action == "disable":
        mcp_hub.save(name, {"enabled": False})
        await mcp_hub.disconnect(name)
        mcp_hub.status[name] = i18n.later("未启用")
    elif action == "cancel":
        await mcp_hub.cancel_login(name)
    else:
        raise HTTPException(404, i18n.t("未知操作"))
    return await _app_out(name)


@app.patch("/api/apps/{name}")
async def patch_app(name: str, data: dict = Body(...)):
    """只改确认策略、屏蔽词这类设置，改完重连让它生效。"""
    if not mcp_hub.get(name):
        raise HTTPException(404, i18n.t("没有这个应用"))
    try:
        a = mcp_hub.save(name, {k: v for k, v in data.items() if k in ("deny", "confirm")})
    except ValueError as e:
        _err(e)
    if a["enabled"] and name in mcp_hub._connected:
        await mcp_hub.connect(name)
    return await _app_out(name)


@app.delete("/api/apps/{name}")
async def delete_app(name: str):
    await mcp_hub.disconnect(name)
    mcp_hub.remove(name)
    await broadcast({"type": "apps_changed"})
    return {"ok": True}


def _oauth_page(ok: bool, title: str, detail: str = "") -> HTMLResponse:
    """授权弹窗的最后一页：告诉用户结果，通知原页面，然后自己关掉。"""
    from html import escape
    mark = "✓" if ok else "!"
    js = json.dumps({"type": "osa-app", "ok": ok})
    return HTMLResponse(f"""<!doctype html><html lang="{"en" if i18n.lang() == "en" else "zh-CN"}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<style>body{{margin:0;height:100vh;display:grid;place-items:center;font-family:-apple-system,"PingFang SC",sans-serif;
background:#0d0d0e;color:#ececec;text-align:center}}@media(prefers-color-scheme:light){{body{{background:#fbfbfa;color:#18181a}}}}
.m{{font-size:44px;color:{'#5fcf98' if ok else '#ef6b6b'}}}p{{margin:6px 24px}}.d{{opacity:.6;font-size:14px}}</style>
<div><div class="m">{mark}</div><p>{escape(title)}</p><p class="d">{escape(detail)}</p></div>
<script>try{{opener&&opener.postMessage({js},location.origin)}}catch(e){{}}
setTimeout(function(){{if(opener)close();else location.replace('/apps')}},{1200 if ok else 4000})</script></html>""")


@app.get("/oauth/start/{name}")
async def oauth_start(name: str, request: Request):
    """点“连接”时在弹窗里打开：需要的话先从市场添加，然后跳到对方的登录授权页。"""
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(403, i18n.t("请从应用市场里点连接"))
    a = mcp_hub.get(name)
    if not a:
        try:
            mcp_hub.save(name, catalog.build(name, {}) | {"enabled": True})
        except ValueError as e:
            return _oauth_page(False, i18n.t("没法连接"), str(e))
        a = mcp_hub.get(name)
    if not a.get("oauth"):
        return _oauth_page(False, i18n.t("这个应用不用登录授权"), i18n.t("回到应用市场里直接连接就行"))
    kind, val = await mcp_hub.login(name, str(request.url_for("oauth_callback")))
    if kind == "url":
        return RedirectResponse(val, 302)
    ok = name in mcp_hub._connected
    return _oauth_page(ok, i18n.t("已连接 {title}", title=catalog.title(name)) if ok else i18n.t("没有连上"), "" if ok else val)


@app.get("/oauth/callback", name="oauth_callback")
async def oauth_callback(state: str = "", code: str = "", iss: str = "", error: str = "", error_description: str = ""):
    name = await mcp_hub.finish_login(state, code, iss, error_description or error)
    if not name:
        return _oauth_page(False, i18n.t("这次授权已经失效"), i18n.t("回到应用市场重新点一次连接"))
    if name in mcp_hub._connected:
        return _oauth_page(True, i18n.t("已连接 {title}", title=catalog.title(name)), i18n.t("可以关掉这个窗口了"))
    return _oauth_page(False, i18n.t("没有连上"), str(mcp_hub.status.get(name, "")))


@app.get("/api/rules")
async def list_rules():
    return approvals.rules()


@app.post("/api/rules")
async def add_rule(data: dict = Body(...)):
    try:
        return approvals.add_rule(data.get("pattern", ""), data.get("action", ""), data.get("note", ""))
    except ValueError as e:
        _err(e)


@app.delete("/api/rules/{rid}")
async def delete_rule(rid: int):
    approvals.delete_rule(rid)
    return {"ok": True}


@app.get("/api/vault")
async def list_vault():
    return vault.names()


@app.put("/api/vault/{name}")
async def put_vault(name: str, data: dict = Body(...)):
    try:
        vault.put(name, data.get("value", ""))
    except ValueError as e:
        _err(e)
    return {"ok": True}


@app.delete("/api/vault/{name}")
async def delete_vault(name: str):
    vault.delete(name)
    return {"ok": True}


# ---------- 聊天软件 ----------
@app.get("/api/channels")
async def list_channels():
    return {"channels": channels.list_public(), "public_url": channels.public_url()}


@app.post("/api/channels")
async def add_channel(data: dict = Body(...)):
    try:
        if data.get("public_url") and not channels.public_url():
            channels.set_public_url(data["public_url"])   # 第一次添加时记下网页地址，消息里的链接要用
        return await channels.create(data.get("kind", ""), data.get("fields") or {}, int(data.get("chat_id") or 0))
    except (ValueError, TypeError) as e:
        _err(e)


@app.put("/api/channels/settings")
async def channel_settings(data: dict = Body(...)):
    try:
        channels.set_public_url(data.get("public_url", ""))
    except ValueError as e:
        _err(e)
    return {"public_url": channels.public_url()}


@app.patch("/api/channels/{cid}")
async def update_channel(cid: int, data: dict = Body(...)):
    try:
        return await channels.update(cid, data)
    except (ValueError, TypeError) as e:
        _err(e)


@app.post("/api/channels/{cid}/pair")
async def repair_channel(cid: int):
    if not channels.row(cid):
        raise HTTPException(404, i18n.t("没有这个连接"))
    channels.new_pair_code(cid)
    channels.changed()
    return channels.public(channels.row(cid))


@app.delete("/api/channels/{cid}")
async def delete_channel(cid: int):
    channels.delete(cid)
    return {"ok": True}


# ---------- 设置、费用、状态 ----------
@app.get("/api/settings")
async def get_settings():
    return settings.public()


@app.put("/api/settings")
async def put_settings(data: dict = Body(...)):
    try:
        out = settings.save(data)
    except ValueError as e:
        _err(e)
    if "computer_parallel" in data:   # 并行数改大了，排队的任务马上能开始
        await computer.get().recheck()
    if {"computer_parallel", "computer_backend", "computer_host_shell"} & data.keys():
        await broadcast({"type": "computer", "computer": await computer.public_state()})
    return out


@app.post("/api/onboarding/done")
async def onboarding_done():
    """第一次打开时的引导走完或跳过了，以后不再自动弹出。"""
    settings.finish_onboarding()
    return {"ok": True}


# ---------- 模型服务 ----------
@app.get("/api/providers/{name}/models")
async def provider_models(name: str, refresh: bool = False):
    try:
        return await providers.models(name, refresh)
    except ValueError as e:
        _err(e)


@app.post("/api/providers/check")
async def check_provider(data: dict = Body(...)):
    """保存之前试一下：地址对不对、密钥能不能用。密钥留空就用已经存的。"""
    try:
        url = providers._clean_url(data.get("base_url", ""))
    except ValueError as e:
        _err(e)
    try:
        await security.check_provider_url(url)
    except ValueError as e:
        _err(e)
    key = (data.get("api_key") or "").strip()
    name = data.get("name") or ""
    # 留空用已经存的密钥，但只发给原来那个服务：不然改个地址点“检查”，密钥就发到别人那里了
    if not key and providers.exists(name) and providers.same_origin(url, providers.describe(name)["base_url"]):
        key = providers.api_key(name)
    return await providers.check(url, key)


@app.post("/api/providers")
async def add_provider(data: dict = Body(...)):
    try:
        await security.check_provider_url(data.get("base_url") or "")
        name = providers.save(None, data)
    except ValueError as e:
        _err(e)
    return {"name": name, "settings": settings.public()}


@app.put("/api/providers/{name}")
async def update_provider(name: str, data: dict = Body(...)):
    try:
        await security.check_provider_url(data.get("base_url") or "")
        providers.save(name, data)
    except ValueError as e:
        _err(e)
    return {"name": name, "settings": settings.public()}


@app.delete("/api/providers/{name}")
async def delete_provider(name: str):
    try:
        providers.delete(name, settings.ROLE_NAMES)
    except ValueError as e:
        _err(e)
    return {"settings": settings.public()}


@app.post("/api/models/test")
async def test_model(data: dict = Body(...)):
    try:
        return await providers.test(data.get("provider", ""), data.get("model", ""), data.get("kind", "chat"), data.get("role", ""))
    except ValueError as e:
        _err(e)


@app.get("/api/prices")
async def price_info():
    return prices.info()


@app.post("/api/prices/refresh")
async def refresh_prices():
    r = await prices.refresh()
    return {**r, "settings": settings.public()}


@app.get("/api/usage")
async def usage():
    return memory.usage_stats()


# ---------- 观测 ----------
def _observe_args(range: str, tz: int, bot: int | None) -> tuple[str, int, int | None]:
    return (range if range in observe.RANGES else "7d"), max(-840, min(840, tz)), bot


@app.get("/api/observe")
async def observe_stats(range: str = "7d", tz: int = 0, bot: int | None = None):
    """观测页：一段时间里花了多少、做了多少、快不快、哪里出了问题。bot=-1 是没记录是哪个 Bot 的调用。"""
    return observe.stats(*_observe_args(range, tz, bot))


@app.get("/api/observe/calls")
async def observe_calls(range: str = "7d", tz: int = 0, bot: int | None = None, before: int | None = None,
                        limit: int = 50, failed: bool = False):
    return observe.calls(*_observe_args(range, tz, bot), before, max(1, min(limit, 200)), failed)


@app.get("/api/observe/calls.csv")
async def observe_calls_csv(range: str = "7d", tz: int = 0, bot: int | None = None):
    names = {b["id"]: b["name"] for b in bots.list_bots()}
    body = observe.calls_csv(*_observe_args(range, tz, bot), names)
    return Response(body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"osa-calls-{time.strftime('%Y%m%d')}.csv\""})


@app.get("/api/status")
async def status():
    return {"mcp": {k: str(v) for k, v in mcp_hub.status.items()}, "mcp_tools": mcp_hub.all_tool_names(), "mcp_confirm": sorted(mcp_hub.CONFIRM),
            "routines": [{"id": r["id"], "bot_id": r["bot_id"], "name": r["name"], "enabled": r["enabled"],
                          "trigger": routines.describe(r["trigger"])} for r in routines.list_for()]}


# ---------- Webhook ----------
async def _hook_body(request: Request) -> bytes:
    if int(request.headers.get("content-length") or 0) > events.MAX_BODY:
        raise HTTPException(413, i18n.t("内容太大"))
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > events.MAX_BODY:
            raise HTTPException(413, i18n.t("内容太大"))
    return body


@app.post("/hooks/{name}")
async def hook(name: str, request: Request):
    """外部事件入口（不用登录）。
    新地址 /hooks/<密钥>：每个事件自己的随机密钥，地址本身就是凭据。
    旧地址 /hooks/<名字>（以前的 webhook 定时任务）：设置了 OSA_WEBHOOK_TOKEN 时请求头 X-OSA-Token 必须一致，没设置时只接受本机请求。"""
    r = events.by_key(name) if len(name) >= 16 else None
    if not r:
        token = os.environ.get("OSA_WEBHOOK_TOKEN")
        if token:
            if request.headers.get("x-osa-token") != token:
                raise HTTPException(401, i18n.t("token 不对"))
        elif request.client.host not in ("127.0.0.1", "::1") or any(
                h in request.headers for h in ("x-forwarded-for", "forwarded", "tailscale-user-login")):
            # 经反向代理（如 tailscale serve）转发的请求来源也显示为本机，按外部请求处理
            raise HTTPException(403, i18n.t("未设置 OSA_WEBHOOK_TOKEN 时只接受本机请求"))
        r = events.by_path(name)
    if not r:
        raise HTTPException(404, i18n.t("没有这个事件地址"))
    body = await _hook_body(request)
    try:
        out = await events.on_push(r, dict(request.headers), body, dict(request.query_params))
    except PermissionError as e:
        raise HTTPException(401, str(e))
    if "challenge" in out:
        return out
    await broadcast({"type": "routines_changed", "bot_id": r["bot_id"]})
    return {"ok": True, **out}


@app.get("/hooks/{name}")
async def hook_check(name: str):
    """有的服务填地址时先用 GET 试一下通不通。"""
    if len(name) < 16 or not events.by_key(name):
        raise HTTPException(404, i18n.t("没有这个事件地址"))
    return {"ok": True}


# ---------- 实时通道 ----------
@app.websocket("/ws")
async def ws(sock: WebSocket):
    # 沙箱里的 html 页面来源是 null：不让它连上来替你发消息、点确认
    if sock.headers.get("origin") == "null":
        await sock.close(code=1008)
        return
    await sock.accept()
    clients.add(sock)
    try:
        while True:
            data = await sock.receive_json()
            if data.get("type") == "approve":
                approvals.resolve(int(data["id"]), bool(data.get("ok")), data.get("note", ""), bool(data.get("always")),
                                  data.get("edits"), data.get("scope"), data.get("prefix"))
            elif data.get("type") == "visibility":
                watching[sock] = bool(data.get("visible"))
            elif data.get("type") == "send":
                text = (data.get("text") or "").strip()
                chat = bots.chat(int(data.get("chat_id", 0)))
                atts = media.clean_attachments(data.get("attachments"))
                if (text or atts) and chat:
                    asyncio.create_task(safe_handle(chat, text, attachments=atts))
    except WebSocketDisconnect:
        clients.discard(sock)
        watching.pop(sock, None)


async def safe_handle(chat: dict, text: str, via: str = "", attachments: list | None = None):
    """用户发来一条消息（网页，或者 via 指明的聊天软件）。attachments：附件（见 media.py），
    正文后面照旧接上“[附件] 路径”，附件本身记在消息的 meta.attachments 里。"""
    atts = attachments or []
    text = media.with_marks(text, atts)
    registry.fire("user_message", chat=chat, text=text, via=via)   # 插件能订阅；不等它们
    try:
        if chat["kind"] == "group":
            await groups.on_user(chat, text, via, atts)
        else:
            await rt.runtime(chat["bot_ids"][0]).handle_user(text, via, chat["id"], atts)
    except Exception as e:
        logging.getLogger(__name__).exception("对话 %s 出错", chat["id"])
        await broadcast({"type": "error", "chat_id": chat["id"], "message": i18n.t("出错了：{e}", e=providers.brief(e))})


channels.handle = safe_handle


async def _applet_say(chat: dict, text: str, applet: dict):
    try:
        await rt.runtime(chat["bot_ids"][0]).handle_user(text, "", chat["id"], applet=applet)
    except Exception as e:
        logging.getLogger(__name__).exception("小应用 %s 发消息出错", applet.get("id"))
        await broadcast({"type": "error", "chat_id": chat["id"], "message": i18n.t("出错了：{e}", e=providers.brief(e))})

applets.handle = _applet_say
channels.reset = reset_chat
channels.handback = handback


# ---------- 前端 ----------
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


# 首页和 Service Worker 每次都向服务器确认有没有新版（没变时只回 304）：不然浏览器和 App 里的 WebKit 会按经验缓存，
# 服务器升级后还显示旧界面。/assets 里的文件名带哈希，可以照常缓存
NO_CACHE = {"cache-control": "no-cache"}


@app.get("/{path:path}")
async def spa(path: str):
    f = (DIST / path).resolve()
    if path and f.is_relative_to(DIST.resolve()) and f.is_file():   # 不让 ../ 读到 dist 外面的文件
        return FileResponse(f, headers=NO_CACHE if f.name in ("index.html", "sw.js") else None)
    index = DIST / "index.html"
    if not index.exists():
        return FileResponse(Path(__file__).parent / "static" / "index.html", headers=NO_CACHE)  # 还没构建前端时的旧页面
    return FileResponse(index, headers=NO_CACHE)
