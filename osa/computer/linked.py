"""电脑类型 linked：服务器借用你的 Mac（连接见 link.py，Mac 上的小程序见 agent.py）。

和本机电脑（host.py）一样：Bot 用 Mac 上单独开的 Chrome，文件只在 Mac 的工作文件夹里，打开“操作整台电脑”后还能用屏幕和鼠标键盘。
排队、确认卡片、你一动鼠标就停这些都在服务器上照常做（继承 HostComputer），只是真正动手的那一步经连接交给 Mac：
浏览器控制脚本、整个屏幕的动作、截图、读写文件。不同的地方：
- 没有终端：服务器不能让 Mac 执行命令。
- 操作整个屏幕和本机一样看那台 Mac 的两个权限（借出时你在 Mac 上同意的就包括屏幕）。
- 不看服务器的服务器锁（security.local_exec）：动手的是你的 Mac，不是服务器。
"""
import base64
import time

from .. import i18n
from ..config import CFG
from . import link
from .base import Computer, ComputerError, ExecResult, check_enabled
from .host import HostComputer

OFFLINE = "你的 Mac 没连上（App 没开、没联网，或者电脑睡着了）"


class LinkedComputer(HostComputer):
    backend = "linked"

    def __init__(self, bot_id: int):
        Computer.__init__(self, bot_id)   # 不在服务器上建工作文件夹和 Chrome 配置目录
        self.workspace = ""
        self._init_desk()
        self._info: tuple[float, dict] = (0.0, {})

    @property
    def link(self) -> "link.Link":
        lk = link.connected()
        if lk is None:
            raise ComputerError(i18n.t(OFFLINE))
        if lk.workspace:
            self.workspace = lk.workspace
        return lk

    async def _call(self, op: str, **args):
        return await self.link.call(op, **args)

    # ---------- 浏览器 ----------
    async def _helper_run(self, payload: dict) -> dict:
        return await self._call("helper", payload=payload)

    async def status(self) -> dict:
        lk = link.connected()
        if lk is None:
            return {"state": "error", "detail": i18n.t(OFFLINE)}
        try:
            return await self._call("status")
        except ComputerError as e:
            return {"state": "error", "detail": str(e)}

    async def start(self):
        check_enabled()
        st = await self.status()
        if st["state"] == "error":
            raise ComputerError(st["detail"])
        if st["state"] != "running":
            await self._call("start", timeout=120)
            self.changed()

    async def pause(self):
        if link.connected() is not None:
            await self._call("pause")

    # ---------- 文件：在 Mac 的工作文件夹里 ----------
    async def read_bytes(self, path: str, limit: int = 32 << 20) -> bytes:
        return base64.b64decode(await self._call("read", path=path, limit=limit))

    async def write_bytes(self, path: str, data: bytes):
        await self._call("write", path=path, data=base64.b64encode(data).decode())

    async def ls(self, path: str) -> list[dict]:
        return await self._call("ls", path=path)

    async def stat_paths(self, paths: list[str]) -> dict[str, dict]:
        return await self._call("stat", paths=paths)

    async def rm(self, path: str):
        await self._call("rm", path=path)

    def upload_paths(self, paths: list[str]) -> list[str]:
        return paths   # Mac 收到上传时自己查在不在工作文件夹里（agent.py）

    # ---------- 终端：没有 ----------
    def shell_enabled(self) -> bool:
        return False

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        raise ComputerError(i18n.t("借用的 Mac 不能用终端"))

    async def shell(self, cmd: str, timeout: int = 120) -> ExecResult:
        return await self.exec(cmd)

    # ---------- 整个屏幕：经连接让 Mac 做 ----------
    def desk_supported(self) -> bool:
        lk = link.connected()
        return bool(lk and lk.desktop)

    def desktop_enabled(self) -> bool:
        """那台 Mac 给了两个权限（用最近一次问到的：Mac 连上时报一次，之后电脑面板每次刷新都会问）。"""
        lk = link.connected()
        if not (lk and lk.desktop and (CFG.get("computer") or {}).get("host_desktop", True)):
            return False
        info = self._info[1] or lk.perms
        return bool(info.get("accessibility") and info.get("screen"))

    async def _desk_info(self, fresh: bool = False) -> dict:
        """画面每一两秒取一次，用一秒内的结果；Bot 动手前（fresh）一定重新问。"""
        at, info = self._info
        if fresh or time.time() - at > 1:
            info = await self._call("desk_info")
            self._info = (time.time(), info)
        return info

    async def _desk_run(self, op: str, a: dict) -> dict:
        r = await self._call("desk", action=op, a=a)
        if "png" in r:
            r["png"] = base64.b64decode(r["png"])
        return r

    async def _capture(self, fmt: str = "png", quality: int = 70) -> bytes:
        return base64.b64decode(await self._call("capture", fmt=fmt, quality=quality))

    async def _front(self) -> dict:
        return await self._call("front")

    async def grant(self) -> None:
        await self._call("grant", timeout=30)
