"""Instabox 后端：所有 Bot 共用一台 Firecracker 微虚拟机（agent-browser 模板，自带 Xfce 桌面和 Chromium）。

接口依据 https://api.instabox.sh/v1/openapi.json 和 Instabox 文档编写。
需要环境变量 INSTABOX_API_KEY（scope 至少 machines:write）。
"""
import asyncio
import os
import uuid
import httpx
import websockets
from .. import i18n
from ..memory import conn
from .base import Computer, ComputerError, ExecResult, check_enabled

API = os.environ.get("INSTABOX_API_URL", "https://api.instabox.sh")
STATE = {"running": "running", "suspended": "paused", "stopped": "stopped", "creating": "starting",
         "starting": "starting", "resuming": "starting", "suspending": "paused", "stopping": "stopped",
         "error": "error", "deleting": "error", "deleted": "none"}


def _headers(extra: dict | None = None) -> dict:
    key = os.environ.get("INSTABOX_API_KEY")
    if not key:
        raise ComputerError(i18n.t("没有设置 INSTABOX_API_KEY"))
    return {"X-API-Key": key, **(extra or {})}


class InstaboxComputer(Computer):
    backend = "instabox"

    def __init__(self, bot_id: int, template: str = "agent-browser", idle_seconds: int = 900,
                 fixed_machine: str | None = None):
        super().__init__(bot_id)
        self.fixed_machine = fixed_machine   # config 里指定已有机器（免费版只能有一台）
        self.template = template
        self.idle_seconds = idle_seconds
        self._starting = asyncio.Lock()
        self.http = httpx.AsyncClient(base_url=API, timeout=httpx.Timeout(60, read=660))

    # 机器 id 存在数据库里，以后一直用同一台机器
    @property
    def machine_id(self) -> str | None:
        if self.fixed_machine:
            return self.fixed_machine
        r = conn.execute("select ref from computers where bot_id=? and backend='instabox'", (self.bot_id,)).fetchone()
        return r[0] if r else None

    def _save_id(self, mid: str):
        conn.execute("insert or replace into computers(bot_id, backend, ref) values(?, 'instabox', ?)", (self.bot_id, mid))
        conn.commit()

    async def list_machines(self) -> list[dict]:
        """账号下所有机器，给界面选“用已有的电脑”。"""
        out, cursor = [], None
        for _ in range(10):
            d = await self._req("GET", "/v1/machines" + (f"?cursor={cursor}" if cursor else ""))
            out += d.get("machines", [])
            if not (cursor := d.get("nextCursor")):
                break
        cur = self.machine_id
        return [{"id": m["id"], "name": m.get("name") or "", "state": STATE.get(m.get("state"), "error"),
                 "template": m.get("template") or "", "created": m.get("createdAt"),
                 "ours": (m.get("metadata") or {}).get("app") == "open-super-agent", "current": m["id"] == cur}
                for m in out if m.get("state") not in ("deleting", "deleted")]

    def use_machine(self, mid: str):
        if self.fixed_machine:
            raise ComputerError(i18n.t("config.yaml 的 computer.instabox_machine 已经指定了机器，要换请先清空那一项"))
        self._save_id(mid)

    async def _req(self, method: str, path: str, json: dict | None = None, idem: bool = False, timeout: float | None = None):
        h = _headers({"Idempotency-Key": str(uuid.uuid4())} if idem else None)
        r = await self.http.request(method, path, json=json, headers=h, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
        if r.status_code >= 400:
            try:
                e = r.json().get("error", {})
                msg = f"{e.get('code')}: {e.get('message')}"
            except Exception:
                msg = r.text[:300]
            raise ComputerError(f"Instabox {r.status_code} {msg}")
        return r.json() if r.content else {}

    async def _wait_op(self, op: dict) -> dict:
        while op.get("state") not in ("succeeded", "failed"):
            op = await self._req("GET", f"/v1/operations/{op['id']}?wait=25s", timeout=40)
        if op["state"] == "failed":
            raise ComputerError(i18n.t("Instabox 操作失败：{error}", error=op.get("error")))
        return op

    async def status(self) -> dict:
        mid = self.machine_id
        if not mid:
            return {"state": "none", "detail": i18n.t("还没有创建")}
        try:
            m = await self._req("GET", f"/v1/machines/{mid}")
        except ComputerError as e:
            return {"state": "error", "detail": str(e)}
        return {"state": STATE.get(m.get("state"), "error"), "detail": m.get("state", ""), "machine_id": mid}

    async def start(self):
        check_enabled()
        async with self._starting:
            st = await self.status()
            mid = self.machine_id
            if st["state"] == "none" or not mid:
                try:
                    op = await self._req("POST", "/v1/machines", idem=True, json={
                    "name": "osa-computer", "templateId": self.template,
                    "autoSuspendAfterSeconds": self.idle_seconds, "metadata": {"app": "open-super-agent", "role": "shared"}})
                except ComputerError as e:
                    if " 429 " in str(e):
                        raise ComputerError(i18n.t("Instabox 账号的机器数量到上限了（免费版只能有一台）。"
                                                   "可以在电脑面板里选“用已有的电脑”，或者去 Instabox 删掉一台。")) from e
                    raise
                op = await self._wait_op(op)
                self._save_id(op["machineId"])
                mid = op["machineId"]
            elif st["detail"] == "suspended":
                await self._wait_op(await self._req("POST", f"/v1/machines/{mid}/resume", idem=True))
            elif st["detail"] == "stopped":
                await self._wait_op(await self._req("POST", f"/v1/machines/{mid}/start", idem=True))
            elif st["state"] == "running":
                await self._req("POST", f"/v1/machines/{mid}/wake", json={})   # 记一次活动，防止被闲置暂停
            d = await self._req("POST", f"/v1/machines/{mid}/desktop", timeout=100,
                                json={"action": "start", "width": self.width, "height": self.height})
            if not d.get("installed"):   # 首次使用要先装桌面，约半分钟
                if not d.get("installing"):
                    await self._req("POST", f"/v1/machines/{mid}/desktop", json={"action": "install"})
                for _ in range(120):
                    await asyncio.sleep(3)
                    d = await self._req("POST", f"/v1/machines/{mid}/desktop", json={"action": "status"})
                    if d.get("failed"):
                        raise ComputerError(i18n.t("桌面安装失败：{error}", error=str(d.get("log", ""))[:300]))
                    if d.get("installed"):
                        break
                await self._req("POST", f"/v1/machines/{mid}/desktop", timeout=100,
                                json={"action": "start", "width": self.width, "height": self.height})
            # 和本地后端保持一致的工作目录；桌面已在运行时分辨率可能不是配置值，以实际为准
            r = await self.exec("mkdir -p /data/workspace; DISPLAY=:1 xdpyinfo 2>/dev/null | awk '/dimensions:/{print $2}'", timeout=20)
            try:
                self.width, self.height = map(int, r.stdout.strip().split("x"))
            except ValueError:
                pass

    async def pause(self):
        mid = self.machine_id
        if mid and (await self.status())["state"] == "running":
            await self._wait_op(await self._req("POST", f"/v1/machines/{mid}/suspend", idem=True))

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        mid = self.machine_id
        if not mid:
            raise ComputerError(i18n.t("电脑还没创建"))
        body = {"cmd": cmd, "timeoutSeconds": max(1, min(600, timeout)), "cwd": "/data/workspace"}
        if stdin is not None:
            body["stdin"] = stdin
        r = await self._req("POST", f"/v1/machines/{mid}/exec", json=body, timeout=timeout + 30)
        return ExecResult(r.get("exitCode", -1), r.get("stdout", ""), r.get("stderr", ""), bool(r.get("timedOut")))

    async def read_bytes(self, path: str, limit: int = 32 << 20) -> bytes:
        mid = self.machine_id
        r = await self.http.get(f"/v1/machines/{mid}/files/content", params={"path": path}, headers=_headers())
        if r.status_code == 404:
            raise ComputerError(i18n.t("文件不存在：{path}", path=path))
        if r.status_code >= 400:
            return await super().read_bytes(path, limit)
        if len(r.content) > limit:
            raise ComputerError(i18n.t("文件太大"))
        return r.content

    def takeover(self):
        super().takeover()
        if self.machine_id:   # 同时告诉 Instabox，它自带的电脑操作工具也会停手
            asyncio.create_task(self._req("POST", f"/v1/machines/{self.machine_id}/desktop", json={"action": "takeover"}))

    def handback(self):
        super().handback()
        if self.machine_id:
            asyncio.create_task(self._req("POST", f"/v1/machines/{self.machine_id}/desktop", json={"action": "handback"}))

    async def open_vnc(self):
        url = API.replace("https://", "wss://").replace("http://", "ws://") + f"/v1/machines/{self.machine_id}/vnc"
        ws = await websockets.connect(url, additional_headers=_headers(), max_size=None, ping_interval=None)

        async def recv() -> bytes:
            m = await ws.recv()
            return m if isinstance(m, bytes) else m.encode()

        return recv, ws.send, ws.close
