import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from . import approvals, bots, computer, mcp_hub, memory, router as rt, routines, settings, skills, vault
from .watchdog import watchdog

DIST = Path(__file__).parent / "static" / "dist"
clients: set[WebSocket] = set()


async def broadcast(event: dict):
    for ws in list(clients):
        try:
            await ws.send_json(event)
        except Exception:
            clients.discard(ws)

rt.notify = broadcast
approvals.notify = broadcast
mcp_hub.notify = broadcast
routines.notify = broadcast


@asynccontextmanager
async def lifespan(app):
    bots.ensure_default()
    settings.apply()
    routines.import_legacy(bots.primary()["id"])
    await mcp_hub.start()
    bg = [asyncio.create_task(watchdog()), asyncio.create_task(routines.scheduler()),
          asyncio.create_task(computer.idle_pauser())]
    yield
    for t in bg:
        t.cancel()
    await mcp_hub.stop()

app = FastAPI(lifespan=lifespan)


def _err(e: Exception):
    raise HTTPException(400, str(e))


# ---------- Bot 与对话 ----------
@app.get("/api/bootstrap")
async def bootstrap():
    return {"bots": bots.list_bots(), "chats": bots.list_chats(), "settings": settings.public(),
            "shapes": bots.SHAPES, "colors": bots.COLORS,
            "tasks": [t.public() for t in rt.TASKS.values() if t.status == "running"],
            "approvals": [approvals.public(a) for a in approvals.pending.values()],
            "apps": mcp_hub.public()}


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
    try:
        bots.delete(bot_id)
    except ValueError as e:
        _err(e)
    routines.delete_for_bot(bot_id)
    memory.conn.execute("delete from skills where bot_id=?", (bot_id,))
    memory.conn.commit()
    rt.RUNTIMES.pop(bot_id, None)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.get("/api/chats/{chat_id}/messages")
async def messages(chat_id: int, before: int | None = None, limit: int = 60):
    return memory.page(chat_id, before, min(limit, 200))


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
                        data.get("description", ""))
    except ValueError as e:
        _err(e)
    await broadcast({"type": "skills_changed"})
    return s


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
    c = _pc(bot_id)
    st = await c.status()
    return {"bot_id": bot_id, "backend": c.backend, "controller": c.controller,
            "width": c.width, "height": c.height, **st}


@app.get("/api/bots/{bot_id}/computer")
async def pc_status(bot_id: int):
    return await _pc_state(bot_id)


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
            c.handback()
            # 如果 Bot 正在等你帮忙，交还就表示处理完了
            for a in list(approvals.pending.values()):
                if a["bot_id"] == bot_id and a.get("kind") == "help":
                    approvals.resolve(a["id"], True, "")
        else:
            raise HTTPException(404, "未知操作")
    except computer.ComputerError as e:
        _err(e)
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
@app.get("/api/apps")
async def list_apps():
    return {"apps": mcp_hub.public(), "presets": mcp_hub.PRESETS}


@app.put("/api/apps/{name}")
async def save_app(name: str, data: dict = Body(...)):
    """新建或修改应用。secrets 里的明文先存进保险库，配置里只留 vault:名字。"""
    try:
        env = dict(data.get("env") or (mcp_hub.get(name) or {}).get("env") or {})
        for k, v in (data.pop("secrets", None) or {}).items():
            if v:
                ref = f"{name}_{k}".upper().replace("-", "_")
                vault.put(ref, v)
                env[k] = f"vault:{ref}"
        a = mcp_hub.save(name, data | {"env": env})
    except ValueError as e:
        _err(e)
    if a["enabled"]:
        await mcp_hub.connect(name)
    else:
        await mcp_hub.disconnect(name)
        mcp_hub.status[name] = "未启用"
    await broadcast({"type": "apps_changed"})
    return next(x for x in mcp_hub.public() if x["name"] == name)


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
    else:
        raise HTTPException(404, "未知操作")
    await broadcast({"type": "apps_changed"})
    return next(x for x in mcp_hub.public() if x["name"] == name)


@app.delete("/api/apps/{name}")
async def delete_app(name: str):
    await mcp_hub.disconnect(name)
    mcp_hub.remove(name)
    await broadcast({"type": "apps_changed"})
    return {"ok": True}


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
    await sock.accept()
    clients.add(sock)
    try:
        while True:
            data = await sock.receive_json()
            if data.get("type") == "approve":
                approvals.resolve(int(data["id"]), bool(data.get("ok")), data.get("note", ""), bool(data.get("always")))
            elif data.get("type") == "send":
                text = (data.get("text") or "").strip()
                chat = bots.chat(int(data.get("chat_id", 0)))
                if text and chat:
                    asyncio.create_task(safe_handle(chat, text))
    except WebSocketDisconnect:
        clients.discard(sock)


async def safe_handle(chat: dict, text: str):
    bot_id = chat["bot_ids"][0]   # 私聊；群聊在 P6 实现
    try:
        await rt.runtime(bot_id).handle_user(text)
    except Exception as e:
        await broadcast({"type": "error", "chat_id": chat["id"], "message": f"出错了：{e}"})


# ---------- 前端 ----------
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}")
async def spa(path: str):
    f = DIST / path
    if path and f.is_file():
        return FileResponse(f)
    index = DIST / "index.html"
    if not index.exists():
        return FileResponse(Path(__file__).parent / "static" / "index.html")  # 还没构建前端时的旧页面
    return FileResponse(index)
