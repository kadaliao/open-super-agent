import asyncio
import json
import mimetypes
import os
import posixpath
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from . import approvals, auth, bots, catalog, channels, computer, groups, library, mcp_hub, memories, memory, push, router as rt, routines
from . import settings, skills, teach, vault
from . import worker as worker_mod
from .watchdog import watchdog

DIST = Path(__file__).parent / "static" / "dist"
clients: set[WebSocket] = set()
watching: dict[WebSocket, bool] = {}   # 每个页面现在是不是正被看着（前端在切换前后台时上报）


async def broadcast(event: dict):
    channels.on_event(event)   # 聊天软件（Telegram、飞书）里也发一份
    for ws in list(clients):
        try:
            await ws.send_json(event)
        except Exception:
            clients.discard(ws)
            watching.pop(ws, None)
    # 没有任何页面正被看着时，需要你知道的事推送到手机
    if not any(watching.get(ws, True) for ws in clients) and (msg := push.message_for(event)) and push.count():
        asyncio.create_task(push.send(*msg, urgency="high" if event.get("type") == "approval" else "normal"))

rt.notify = broadcast
approvals.notify = broadcast
mcp_hub.notify = broadcast
routines.notify = broadcast
groups.notify = broadcast
teach.notify = broadcast
channels.notify = broadcast


async def _pages_changed():
    await broadcast({"type": "pages_changed"})

worker_mod.PAGES_CHANGED = _pages_changed


@asynccontextmanager
async def lifespan(app):
    if not auth.enabled():
        print("未设置 OSA_PASSWORD：网页不用登录就能访问，只能在本机或内网使用", flush=True)
    bots.ensure_default()
    settings.apply()
    routines.import_legacy(bots.primary()["id"])
    await mcp_hub.start()
    await channels.start()
    bg = [asyncio.create_task(watchdog()), asyncio.create_task(routines.scheduler()),
          asyncio.create_task(computer.idle_pauser())]
    yield
    for t in bg:
        t.cancel()
    await channels.stop()
    await mcp_hub.stop()

app = FastAPI(lifespan=lifespan)
app.add_middleware(auth.AuthMiddleware)
app.include_router(auth.router)


def _err(e: Exception):
    raise HTTPException(400, str(e))


# ---------- Bot 与对话 ----------
@app.get("/api/bootstrap")
async def bootstrap(request: Request):
    return {"bots": bots.list_bots(), "chats": bots.list_chats(), "settings": settings.public(),
            "shapes": bots.SHAPES, "colors": bots.COLORS,
            "tasks": [t.public() for t in rt.TASKS.values() if t.status == "running"],
            "approvals": [approvals.public(a) for a in approvals.pending.values()],
            **_apps_out(request), "teach": teach.public(), "auth": auth.enabled()}


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
        raise HTTPException(404, "没有这个 Bot")
    b = bots.update(bot_id, **data)
    if data.get("name"):
        memory.conn.execute("update chats set title=? where id=?", (b["name"], b["chat_id"]))
        memory.conn.commit()
    await broadcast({"type": "bots_changed"})
    return b


@app.post("/api/bots/{bot_id}/primary")
async def make_primary(bot_id: int):
    bots.set_primary(bot_id)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: int):
    for t in list(rt.TASKS.values()):
        if t.bot_id == bot_id and t.status == "running":
            rt.cancel(t.id, "Bot 已删除")
    chat_id = (bots.get(bot_id) or {}).get("chat_id")
    try:
        bots.delete(bot_id)
    except ValueError as e:
        _err(e)
    routines.delete_for_bot(bot_id)
    memories.delete_for_bot(bot_id)   # 共享的用户事实保留；页面留在资料库里
    memory.conn.execute("delete from skills where bot_id=?", (bot_id,))
    memory.conn.commit()
    rt.RUNTIMES.pop(bot_id, None)
    channels.chat_removed(chat_id)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.get("/api/chats/{chat_id}/messages")
async def messages(chat_id: int, before: int | None = None, limit: int = 60):
    return memory.page(chat_id, before, min(limit, 200))


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
        g = bots.update_group(chat_id, data.get("title"), data.get("bot_ids"), data.get("lead"))
    except (ValueError, TypeError) as e:
        _err(e)
    await broadcast({"type": "bots_changed"})
    return g


@app.delete("/api/chats/{chat_id}")
async def delete_group(chat_id: int):
    chat = bots.chat(chat_id)
    if not chat or chat["kind"] != "group":
        raise HTTPException(400, "只能删除群聊；Bot 的私聊随 Bot 一起删除")
    for t in list(rt.TASKS.values()):
        if t.chat_id == chat_id and t.status == "running":
            rt.cancel(t.id, "群聊已删除")
    bots.delete_group(chat_id)
    groups.forget(chat_id)
    channels.chat_removed(chat_id)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.post("/api/chats/{chat_id}/reset")
async def new_topic(chat_id: int):
    """新话题：之前的消息不再带进 Bot 的上下文，长期记忆还在。"""
    chat = bots.chat(chat_id)
    if not chat:
        raise HTTPException(404, "没有这个对话")
    return {"id": await reset_chat(chat)}


async def reset_chat(chat: dict | int) -> int:
    chat = bots.chat(chat) if isinstance(chat, int) else chat
    chat_id = chat["id"]
    bot_id = chat["bot_ids"][0] if chat["kind"] == "direct" else None
    mid = memory.add("task", "[新话题]", chat_id, bot_id, kind="reset")
    await broadcast({"type": "message", "chat_id": chat_id, "bot_id": bot_id, "message": {
        "id": mid, "ts": time.time(), "role": "task", "content": "[新话题]", "meta": {"kind": "reset"}, "bot_id": bot_id}})
    return mid


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    return {"result": rt.cancel(task_id, "用户在界面上取消")}


# ---------- 技能 ----------
def _bot_or_404(bot_id: int) -> dict:
    b = bots.get(bot_id)
    if not b:
        raise HTTPException(404, "没有这个 Bot")
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
                        data.get("description", ""), "teach" if data.get("source") == "teach" else "manual")
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
        raise HTTPException(404, "未知操作")
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
        raise HTTPException(400, "还没有设备开启推送")
    return {"results": await push.send("推送测试", "能收到这条，说明这台设备的推送是通的。", "/", "test")}


@app.put("/api/skills/{sid}")
async def update_skill(sid: int, data: dict = Body(...)):
    old = skills.get(sid)
    if not old:
        raise HTTPException(404, "没有这个技能")
    owner = old["bot_id"] if "shared" not in data else (None if data["shared"] else data.get("bot_id", old["bot_id"]))
    try:
        s = skills.save(owner, data.get("name", old["name"]), data.get("body", old["body"]),
                        data.get("description", old["description"]), old["source"], sid=sid)
    except ValueError as e:
        _err(e)
    await broadcast({"type": "skills_changed"})
    return s


@app.delete("/api/skills/{sid}")
async def delete_skill(sid: int):
    skills.delete(sid)
    await broadcast({"type": "skills_changed"})
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
        raise HTTPException(404, "没有这条记忆")
    owner = m["bot_id"]
    if "shared" in data:
        owner = None if data["shared"] else (m["bot_id"] or data.get("bot_id"))
        if not data["shared"] and not (isinstance(owner, int) and bots.get(owner)):
            raise HTTPException(400, "改成只给某个 Bot 时要说明是哪个 Bot")
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
        raise HTTPException(404, "没有这个页面")
    return p


def _raw(page: dict) -> Response:
    """html 页面的原文，带 CSP：在沙箱里运行，碰不到这个站点的接口和数据。"""
    if page["format"] != "html":
        raise HTTPException(404, "只有 html 页面有原文地址")
    return HTMLResponse(page["content"], headers={"Content-Security-Policy": library.RAW_CSP,
                                                  "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


@app.get("/api/pages")
async def list_pages():
    return library.list_pages()


@app.post("/api/pages")
async def create_page(data: dict = Body(...)):
    try:
        p = library.save_page(data.get("bot_id"), data.get("title", ""), data.get("content", ""),
                              data.get("format") or "markdown", task_id=data.get("task_id"))
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


@app.get("/api/shared/{token}")
async def shared_page(token: str):
    p = library.by_token(token)
    if not p:
        raise HTTPException(404, "链接已失效")
    b = bots.get(p["bot_id"]) if p["bot_id"] else None
    return {k: p[k] for k in ("title", "format", "content", "updated")} | (
        {"bot": {"name": b["name"], "avatar": b["avatar"]}} if b else {})


@app.get("/api/shared/{token}/raw")
async def shared_raw(token: str):
    p = library.by_token(token)
    if not p:
        raise HTTPException(404, "链接已失效")
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
        return {"running": False, "state": (await computer.get().status())["state"], "path": p, "root": library.WORKSPACE}
    try:
        entries = await library.list_dir(c, p)
        pinned = library.pins()
        stats = await library.stat_many(c, pinned)
    except (ValueError, computer.ComputerError) as e:
        _err(e)
    return {"running": True, "path": p, "root": library.WORKSPACE, "entries": entries,
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
        raise HTTPException(413, f"文件太大（最多 {library.MAX_UPLOAD >> 20} MB）")
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


# ---------- 定时任务 ----------
def _routine_or_404(rid: int) -> dict:
    r = routines.get(rid)
    if not r:
        raise HTTPException(404, "没有这个定时任务")
    return r


@app.get("/api/bots/{bot_id}/routines")
async def list_routines(bot_id: int):
    _bot_or_404(bot_id)
    return routines.list_for(bot_id)


@app.post("/api/bots/{bot_id}/routines")
async def create_routine(bot_id: int, data: dict = Body(...)):
    _bot_or_404(bot_id)
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
    if action in ("pause", "resume"):
        r = routines.set_enabled(rid, action == "resume")
    elif action == "test":
        res = await routines.fire(r, "test", "（这是一次手动测试运行）" if r["trigger"]["kind"] == "webhook" else "")
        if not res["accepted"]:
            _err(ValueError(res["reason"]))
        r = routines.get(rid)
    else:
        raise HTTPException(404, "未知操作")
    await broadcast({"type": "routines_changed", "bot_id": r["bot_id"]})
    return r


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
        raise HTTPException(404, "没有这个 Bot")
    return computer.get(bot_id)


async def _pc_state(bot_id: int) -> dict:
    _pc(bot_id)
    return await computer.public_state(bot_id)


@app.get("/api/bots/{bot_id}/computer")
async def pc_status(bot_id: int):
    return await _pc_state(bot_id)


@app.post("/api/bots/{bot_id}/computer/type")   # 要放在 /computer/{action} 前面，否则会被它先匹配
async def pc_type(bot_id: int, data: dict = Body(...)):
    """你接手电脑时从网页输入文字或按键（手机上点画面弹不出键盘，用这个）。"""
    c = _pc(bot_id)
    if c.controller != "human":
        raise HTTPException(400, "先接手电脑再输入")
    try:
        if data.get("text"):
            await c.type(str(data["text"])[:2000], as_human=True)
        if data.get("key"):
            await c.press(str(data["key"]), as_human=True)
    except computer.ComputerError as e:
        _err(e)
    return {"ok": True}


@app.post("/api/bots/{bot_id}/computer/{action}")
async def pc_action(bot_id: int, action: str):
    c = _pc(bot_id)
    try:
        if action == "start":
            await c.start()
        elif action == "pause":
            await c.pause()
        elif action == "takeover":
            await c.start()
            c.takeover()
        elif action == "handback":
            return await handback(bot_id)
        else:
            raise HTTPException(404, "未知操作")
    except computer.ComputerError as e:
        _err(e)
    state = await _pc_state(bot_id)
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
        raise HTTPException(400, "只有 Instabox 后端能选已有的机器")
    if c.owner:
        raise HTTPException(400, f"电脑正被{c.owner_label}使用，等它用完再换")
    try:
        if mid not in {m["id"] for m in await c.list_machines()}:
            raise HTTPException(404, "账号下没有这台机器")
        c.use_machine(mid)
    except computer.ComputerError as e:
        _err(e)
    state = await _pc_state(int(data.get("bot_id") or bots.primary()["id"]))
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
        _err(ValueError("已经添加过了"))
    values = data.get("values") or {}
    try:
        await catalog.check(item_id, values)
        mcp_hub.save(item_id, catalog.build(item_id, values) | {"enabled": True})
    except ValueError as e:
        _err(e)
    if catalog.items()[item_id]["kind"] == "oauth":     # 先填字段再登录的（Slack）：前端接着打开授权弹窗
        return await _app_out(item_id)
    await mcp_hub.connect(item_id)
    if item_id not in mcp_hub._connected:   # 没连上就撤掉，用户改一下再点连接就行
        msg = mcp_hub.status.get(item_id) or "没连上"
        await mcp_hub.disconnect(item_id)
        mcp_hub.remove(item_id)
        await broadcast({"type": "apps_changed"})
        if any(w in msg for w in ("401", "403", "400 Bad Request", "Unauthorized", "parse JSON", "密钥无效", "鉴权失败", "invalid or expired")):
            msg = "对方没有接受这个密钥，检查一下有没有复制完整。（" + msg[:120] + "）"
        _err(ValueError(msg))
    return await _app_out(item_id)


@app.put("/api/apps/{name}")
async def save_app(name: str, data: dict = Body(...)):
    """自定义或高级设置。secrets 里的明文先存进保险库，配置里只留 vault:名字；token 放进 Authorization 请求头。"""
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
        mcp_hub.status[name] = "未启用"
    return await _app_out(name)


@app.post("/api/apps/{name}/{action}")
async def app_action(name: str, action: str):
    if not mcp_hub.get(name):
        raise HTTPException(404, "没有这个应用")
    if action in ("enable", "reconnect"):
        mcp_hub.save(name, {"enabled": True})
        await mcp_hub.connect(name)
    elif action == "disable":
        mcp_hub.save(name, {"enabled": False})
        await mcp_hub.disconnect(name)
        mcp_hub.status[name] = "未启用"
    elif action == "cancel":
        await mcp_hub.cancel_login(name)
    else:
        raise HTTPException(404, "未知操作")
    return await _app_out(name)


@app.patch("/api/apps/{name}")
async def patch_app(name: str, data: dict = Body(...)):
    """只改确认策略、屏蔽词这类设置，改完重连让它生效。"""
    if not mcp_hub.get(name):
        raise HTTPException(404, "没有这个应用")
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
    return HTMLResponse(f"""<!doctype html><html lang="zh"><meta charset="utf-8">
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
        raise HTTPException(403, "请从应用市场里点连接")
    a = mcp_hub.get(name)
    if not a:
        try:
            mcp_hub.save(name, catalog.build(name, {}) | {"enabled": True})
        except ValueError as e:
            return _oauth_page(False, "没法连接", str(e))
        a = mcp_hub.get(name)
    if not a.get("oauth"):
        return _oauth_page(False, "这个应用不用登录授权", "回到应用市场里直接连接就行")
    kind, val = await mcp_hub.login(name, str(request.url_for("oauth_callback")))
    if kind == "url":
        return RedirectResponse(val, 302)
    ok = name in mcp_hub._connected
    return _oauth_page(ok, f"已连接 {catalog.title(name)}" if ok else "没有连上", "" if ok else val)


@app.get("/oauth/callback", name="oauth_callback")
async def oauth_callback(state: str = "", code: str = "", iss: str = "", error: str = "", error_description: str = ""):
    name = await mcp_hub.finish_login(state, code, iss, error_description or error)
    if not name:
        return _oauth_page(False, "这次授权已经失效", "回到应用市场重新点一次连接")
    if name in mcp_hub._connected:
        return _oauth_page(True, f"已连接 {catalog.title(name)}", "可以关掉这个窗口了")
    return _oauth_page(False, "没有连上", mcp_hub.status.get(name, ""))


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
        raise HTTPException(404, "没有这个连接")
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
        return settings.save(data)
    except ValueError as e:
        _err(e)


@app.get("/api/usage")
async def usage():
    return memory.usage_stats()


@app.get("/api/status")
async def status():
    return {"mcp": mcp_hub.status, "mcp_tools": sorted(mcp_hub.TOOLS), "mcp_confirm": sorted(mcp_hub.CONFIRM),
            "routines": [{"id": r["id"], "bot_id": r["bot_id"], "name": r["name"], "enabled": r["enabled"],
                          "trigger": routines.describe(r["trigger"])} for r in routines.list_for()]}


# ---------- Webhook ----------
@app.post("/hooks/{name}")
async def hook(name: str, request: Request):
    """外部事件入口。设置了 OSA_WEBHOOK_TOKEN 时，请求头 X-OSA-Token 必须一致；没设置时只接受本机请求。"""
    token = os.environ.get("OSA_WEBHOOK_TOKEN")
    if token:
        if request.headers.get("x-osa-token") != token:
            raise HTTPException(401, "token 不对")
    elif request.client.host not in ("127.0.0.1", "::1") or any(
            h in request.headers for h in ("x-forwarded-for", "forwarded", "tailscale-user-login")):
        # 经反向代理（如 tailscale serve）转发的请求来源也显示为本机，按外部请求处理
        raise HTTPException(403, "未设置 OSA_WEBHOOK_TOKEN 时只接受本机请求")
    r = routines.by_hook(name)
    if not r or not r["enabled"]:
        raise HTTPException(404, f"没有启用的 webhook 定时任务：{name}")
    body = (await request.body()).decode(errors="replace")
    try:
        body = json.dumps(json.loads(body), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return await routines.fire(r, "webhook", body[:20000])


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
                approvals.resolve(int(data["id"]), bool(data.get("ok")), data.get("note", ""), bool(data.get("always")))
            elif data.get("type") == "visibility":
                watching[sock] = bool(data.get("visible"))
            elif data.get("type") == "send":
                text = (data.get("text") or "").strip()
                chat = bots.chat(int(data.get("chat_id", 0)))
                if text and chat:
                    asyncio.create_task(safe_handle(chat, text))
    except WebSocketDisconnect:
        clients.discard(sock)
        watching.pop(sock, None)


async def safe_handle(chat: dict, text: str, via: str = ""):
    """用户发来一条消息（网页，或者 via 指明的聊天软件）。"""
    try:
        if chat["kind"] == "group":
            await groups.on_user(chat, text, via)
        else:
            await rt.runtime(chat["bot_ids"][0]).handle_user(text, via)
    except Exception as e:
        await broadcast({"type": "error", "chat_id": chat["id"], "message": f"出错了：{e}"})


channels.handle = safe_handle
channels.reset = reset_chat
channels.handback = handback


# ---------- 前端 ----------
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}")
async def spa(path: str):
    f = (DIST / path).resolve()
    if path and f.is_relative_to(DIST.resolve()) and f.is_file():   # 不让 ../ 读到 dist 外面的文件
        return FileResponse(f)
    index = DIST / "index.html"
    if not index.exists():
        return FileResponse(Path(__file__).parent / "static" / "index.html")  # 还没构建前端时的旧页面
    return FileResponse(index)
