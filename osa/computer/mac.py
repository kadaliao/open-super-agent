"""派到你的 Mac 上做的任务用的电脑（连接见 link.py，Mac 上的小程序见 agent.py）。

服务器上的 Bot 平时用服务器自己的电脑（Docker 沙盒 / 云电脑）；有些事只有你的 Mac 上才做得了（Mac 上登录着的 App、本地文件、
只装在 Mac 上的软件），Bot 就把那个任务派到某一台 Mac 上（dispatch 的 mac，每次先问你，见 router.py）。
这个任务里的电脑工具用的就是那台 Mac：每台 Mac 一个 MacComputer。

和本机电脑（host.py）一样：Bot 用 Mac 上单独开的 Chrome，文件只在 Mac 的工作文件夹里，有权限的话还能用屏幕和鼠标键盘。
排队、确认卡片、你一动鼠标就停这些都在服务器上照常做（继承 HostComputer），只是真正动手的那一步经连接交给 Mac：
浏览器控制脚本、整个屏幕的动作、截图、读写文件、终端命令。不同的地方：
- 终端每台单独开关（电脑面板里，哪台设备上都能开关，默认关），不看服务器锁；那台 Mac 上的 App 太旧时没有。
  每条命令照样先问你（approvals.py 把你的 Mac 和本机一样对待）。
- 操作整个屏幕看那台 Mac 的两个权限。
- 不看服务器的服务器锁（security）：动手的是你的 Mac，不是服务器。
"""
import base64
import time

from .. import i18n
from ..config import CFG
from . import link
from .base import Computer, ComputerError, ExecResult, check_enabled
from .host import HostComputer

_macs: dict[int, "MacComputer"] = {}
on_change = None   # 某台 Mac 的状态变了（谁在用、接手）：() -> None，computer/__init__.py 设置
background = None  # 在后台跑一个协程，computer/__init__.py 设置


def get(lid: int) -> "MacComputer":
    """那台 Mac 的电脑（不管连没连着；用的时候没连上会报错）。"""
    c = _macs.get(lid)
    if c is None:
        c = _macs[lid] = MacComputer(lid)
        cfg = CFG.get("computer") or {}
        c.width, c.height = cfg.get("width", 1280), cfg.get("height", 800)
        if on_change:
            c.on_change = on_change
        if background:
            c.background = background
    return c


def drop(lid: int):
    _macs.pop(lid, None)


def computers() -> list["MacComputer"]:
    return list(_macs.values())


class MacComputer(HostComputer):
    backend = "mac"

    def __init__(self, lid: int):
        Computer.__init__(self, 0)   # 不在服务器上建工作文件夹和 Chrome 配置目录
        self.lid = lid
        self.workspace = ""
        self._init_desk()
        self._info: tuple[float, dict] = (0.0, {})

    @property
    def name(self) -> str:
        r = link.row(self.lid)
        return r["name"] if r else "Mac"

    @property
    def link(self) -> "link.Link":
        lk = link.connected(self.lid)
        if lk is None:
            raise ComputerError(i18n.t(link.OFFLINE, name=self.name))
        if lk.workspace:
            self.workspace = lk.workspace
        return lk

    async def _call(self, op: str, **args):
        return await self.link.call(op, **args)

    # ---------- 浏览器 ----------
    async def _helper_run(self, payload: dict) -> dict:
        return await self._call("helper", payload=payload)

    async def status(self) -> dict:
        if link.connected(self.lid) is None:
            return {"state": "error", "detail": i18n.t(link.OFFLINE, name=self.name)}
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
        if link.connected(self.lid) is not None:
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

    # ---------- 终端：那台 Mac 在电脑面板里打开了才有 ----------
    def shell_enabled(self) -> bool:
        lk, r = link.connected(self.lid), link.row(self.lid)
        return bool(lk and lk.can_shell and r and r["shell"])

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        if not self.shell_enabled():
            lk = self.link
            if not lk.can_shell:
                raise ComputerError(i18n.t("「{name}」上的 App 太旧，不能执行终端命令，更新到新版再试", name=self.name))
            raise ComputerError(i18n.t("「{name}」的终端没有打开（电脑面板里可以打开，手机上也行，打开后每条命令都要你确认）", name=self.name))
        r = await self._call("exec", cmd=cmd, secs=timeout, stdin=stdin, timeout=timeout + 15)
        return ExecResult(int(r["exit_code"]), str(r["stdout"]), str(r["stderr"]), bool(r.get("timed_out")))

    # ---------- 整个屏幕：经连接让 Mac 做 ----------
    def desk_supported(self) -> bool:
        lk = link.connected(self.lid)
        return bool(lk and lk.desktop)

    def desktop_enabled(self) -> bool:
        """那台 Mac 给了两个权限（用最近一次问到的：Mac 连上时报一次，之后电脑面板每次刷新都会问）。"""
        lk = link.connected(self.lid)
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
