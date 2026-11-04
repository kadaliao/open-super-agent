import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from . import approvals, bots, computer, mcp_hub, memory, router as rt, settings, triggers
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


@asynccontextmanager
async def lifespan(app):
    bots.ensure_default()
    settings.apply()
    await mcp_hub.start()
    bg = [asyncio.create_task(watchdog()), asyncio.create_task(triggers.scheduler()),
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
            "approvals": [approvals.public(a) for a in approvals.pending.values()]}


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
    rt.RUNTIMES.pop(bot_id, None)
    await broadcast({"type": "bots_changed"})
    return {"ok": True}


@app.get("/api/chats/{chat_id}/messages")
async def messages(chat_id: int, before: int | None = None, limit: int = 60):
    return memory.page(chat_id, before, min(limit, 200))


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    return {"result": rt.cancel(task_id, "用户在界面上取消")}


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
            "triggers": [{"name": t["name"], "type": t["type"], "enabled": t.get("enabled", True)}
                         for t in triggers.CFG.get("triggers", [])],
            "trigger_log": triggers.log}


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
    t = triggers.get(name)
    if not t or t.get("type") != "webhook":
        raise HTTPException(404, f"没有启用的 webhook 触发器：{name}")
    body = (await request.body()).decode(errors="replace")
    try:
        body = json.dumps(json.loads(body), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return await triggers.fire(t, body[:20000])


# ---------- 实时通道 ----------
@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    clients.add(sock)
    try:
        while True:
            data = await sock.receive_json()
            if data.get("type") == "approve":
                approvals.resolve(int(data["id"]), bool(data.get("ok")), data.get("note", ""))
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
