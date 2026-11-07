"""本机电脑：Bot 用你这台电脑上装的 Chrome，不用 Docker。

- 浏览器：开一个单独的配置目录（数据目录下的 computers/host/chrome），和你日常用的 Chrome 分开；在里面登录一次，所有 Bot 共用。
  控制脚本 cdp_helper.py 就在这台电脑上跑，每个动作一个进程，和在 Docker 里一样；Chrome 由它按需打开，
  调试端口随机、只听 127.0.0.1。窗口不会被提到最前面，要看就点“显示窗口”。
- 文件：只放在工作文件夹（数据目录下的 workspace，和“文件系统”应用是同一个目录），读写、上传都限定在里面；
  浏览器下载的文件也存到这里的 downloads。
- 终端：默认关（computer.host_shell）。打开后在这台电脑上执行命令，每条都要你确认（tools.py），环境变量里的密钥不传过去。
- 画面：浏览器窗口就在你的屏幕上；网页上看截图（peek），手机上也能看。“接手”就是让 Bot 暂停，你直接在 Chrome 里操作。
"""
import asyncio
import base64
import itertools
import json
import os
import posixpath
import re
import shutil
import signal
import stat
import sys
import time
from pathlib import Path

import httpx

from .. import i18n
from ..config import CFG, DATA
from .base import Computer, ComputerError, ExecResult

HELPER = Path(__file__).parent / "cdp_helper.py"
LEGACY = "/data/workspace"   # Docker / Instabox 电脑里的工作文件夹，技能、记忆里可能还写着
CHROMES = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    "win32": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
              r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
              r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
              r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"],
}
SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|^OSA_", re.I)
XDO = {"return": "enter", "backspace": "backspace"}   # 网页上的输入条发的是 xdotool 的键名


def _probe(path: str) -> bool | None:
    """是个文件：True；不存在或不是文件：False；有但没权限看：None。
    macOS 上 /Applications 里的 Chrome 可能只是个链接，真身在外接硬盘上，App 没拿到“可移除的宗卷”权限时就看不了。"""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except PermissionError:
        return None
    except OSError:
        return False


def chrome_path() -> str | None:
    """Bot 用的浏览器：config.yaml 的 computer.chrome_path，否则按常见位置找 Chrome（Chromium、Edge、Brave 也行）。
    没权限看的也算找到了，status() 会说明要去哪里给权限。"""
    forced = (CFG.get("computer") or {}).get("chrome_path")
    if forced:
        forced = os.path.expanduser(forced)
        return forced if _probe(forced) is not False else None
    for c in CHROMES.get(sys.platform, []):
        c = os.path.expandvars(os.path.expanduser(c))
        if _probe(c) is not False:
            return c
    for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        if w := shutil.which(n):
            return w
    return None


def shell_on() -> bool:
    return bool((CFG.get("computer") or {}).get("host_shell", False))


def _env() -> dict:
    """给控制脚本、Chrome 和终端命令的环境变量：去掉密钥和服务自己的设置（登录密码、保险库主密钥、模型的 API key）。"""
    return {k: v for k, v in os.environ.items() if not SECRET.search(k)}


class HostComputer(Computer):
    backend = "host"
    auto_pause = False

    def __init__(self, bot_id: int):
        super().__init__(bot_id)
        self.profile = DATA / "computers" / "host" / "chrome"
        root = DATA / "workspace"
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()          # 判断路径在不在里面要用真实路径（macOS 的 /tmp 是个链接）
        self.workspace = str(self.root)
        self._starting = asyncio.Lock()

    # ---------- 浏览器 ----------
    def _helper_env(self) -> dict:
        return {**_env(), "OSA_CDP_PROFILE": str(self.profile), "OSA_CDP_CHROME": chrome_path() or "",
                "OSA_CDP_SIZE": f"{self.width},{self.height + 90}",   # 窗口加上标签栏、地址栏，页面差不多是 width × height
                "OSA_CDP_DOWNLOADS": str(self.root / "downloads")}

    async def _helper_run(self, payload: dict) -> dict:
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(HELPER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._helper_env())
        try:
            out, err = await asyncio.wait_for(p.communicate(json.dumps(payload, ensure_ascii=False).encode()), 90)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            raise ComputerError(i18n.t("超时")) from None
        if p.returncode == 0:
            return json.loads(out.decode().strip().splitlines()[-1])
        e = (err or out).decode(errors="replace").strip() or i18n.t("退出码 {code}", code=p.returncode)
        raise ComputerError(e.splitlines()[-1].removeprefix("RuntimeError: ")[:500])

    def _port(self) -> int:
        try:
            return int((self.profile / "DevToolsActivePort").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return 0

    async def status(self) -> dict:
        path = chrome_path()
        if not path:
            return {"state": "error", "detail": i18n.t("没找到 Chrome：先安装 Google Chrome，或者在 config.yaml 的 computer.chrome_path 写上它的路径")}
        if _probe(path) is None:
            return {"state": "error", "detail": i18n.t("没权限打开 Chrome（{path}），它可能在外接硬盘上。到 系统设置 → 隐私与安全性 → 文件与文件夹，"
                                                       "给 Open Super Agent 打开“可移除的宗卷”，再重新打开 App", path=path)}
        if port := self._port():
            try:
                async with httpx.AsyncClient(timeout=2) as h:
                    if (await h.get(f"http://127.0.0.1:{port}/json/version")).status_code == 200:
                        return {"state": "running", "detail": ""}
            except httpx.HTTPError:
                pass
        return {"state": "stopped", "detail": ""}

    async def start(self):
        st = await self.status()
        if st["state"] == "error":
            raise ComputerError(st["detail"])
        async with self._starting:
            if (await self.status())["state"] != "running":
                self.profile.mkdir(parents=True, exist_ok=True)
                await self._helper_run({"op": "ensure"})
                self.changed()

    async def pause(self):
        """关掉 Bot 用的 Chrome（登录状态在配置目录里，下次打开还在）。"""
        if (await self.status())["state"] == "running":
            await self._helper_run({"op": "quit"})
            for _ in range(20):   # Chrome 收到后还要一会儿才退出：等它真的关了，界面上的状态才对
                if (await self.status())["state"] != "running":
                    break
                await asyncio.sleep(0.25)

    def _view(self) -> tuple:
        """画面看哪个窗口：你点了“看这个”的任务，否则最近一次操作的窗口。"""
        u = self.users.get(self.watch) if self.watch else None
        return (u["window"], u.get("browser")) if u and u.get("window") else self.recent

    async def _peek(self, fmt: str) -> dict:
        if (await self.status())["state"] != "running":
            raise ComputerError(i18n.t("Bot 用的 Chrome 没开"))
        win, browser = self._view()
        return await self._helper_run({"op": "peek", "window": win, "browser": browser, "format": fmt})

    async def peek(self, fmt: str = "jpeg") -> bytes:
        return base64.b64decode((await self._peek(fmt))["data"])

    async def look(self) -> tuple[bytes, str]:
        r = await self._peek("png")
        return base64.b64decode(r["data"]), f"{r.get('title') or ''} | {r.get('url') or ''}"

    async def screenshot(self) -> bytes:
        return (await self.look())[0]

    async def show(self):
        """把 Bot 的 Chrome 窗口放到你屏幕的最前面（你点了“显示窗口”或“接手”）。"""
        await self.start()
        if self.watch and (self.users.get(self.watch) or {}).get("window"):
            await self.front(self.watch)
        else:
            await self.browser("front", as_human=True)   # 最近用过的标签页

    async def type(self, text: str, as_human: bool = False):
        if text:
            await self.browser("type", as_human=as_human, text=text)

    async def press(self, keys: str | list[str], as_human: bool = False):
        ks = keys if isinstance(keys, list) else [k for k in str(keys).replace(" ", "").split("+") if k]
        if not ks:
            raise ComputerError(i18n.t("没有按键"))
        await self.browser("press", as_human=as_human, keys="+".join(XDO.get(k.lower(), k) for k in ks))

    async def open_vnc(self):
        raise ComputerError(i18n.t("本机电脑没有远程画面，浏览器窗口就在这台电脑的屏幕上"))

    # ---------- 文件：只在工作文件夹里 ----------
    def _path(self, path: str, follow: bool = True) -> Path:
        """模型或界面给的路径 → 工作文件夹里的真实路径。相对路径从工作文件夹算起；经链接跑出去的也不行。
        follow=False：最后一段是链接时指链接本身（删除用）。"""
        path = (path or "").strip() or self.workspace
        if path == LEGACY or path.startswith(LEGACY + "/"):
            path = self.workspace + path[len(LEGACY):]
        q = Path(os.path.expanduser(path))
        q = Path(os.path.normpath(q if q.is_absolute() else self.root / q))
        r = q.resolve() if follow else q.parent.resolve() / q.name
        if r != self.root and self.root not in r.parents:
            raise ComputerError(i18n.t("只能访问工作文件夹里的文件（{dir}）", dir=self.workspace))
        return r

    async def read_bytes(self, path: str, limit: int = 32 << 20) -> bytes:
        p = self._path(path)
        if not p.is_file():
            raise ComputerError(i18n.t("文件不存在：{path}", path=path))
        if p.stat().st_size > limit:
            raise ComputerError(i18n.t("文件太大"))
        return await asyncio.to_thread(p.read_bytes)

    async def write_bytes(self, path: str, data: bytes):
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def ls(self, path: str) -> list[dict]:
        try:
            p = self._path(path)
        except ComputerError as e:
            raise ValueError(str(e)) from None
        if not p.is_dir():
            raise ValueError(i18n.t("目录不存在"))
        out = []
        with os.scandir(p) as it:
            for e in itertools.islice(it, 1000):
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append({"name": e.name, "path": posixpath.join(path, e.name), "dir": e.is_dir(follow_symlinks=False),
                            "size": st.st_size, "mtime": st.st_mtime})
        return out

    async def stat_paths(self, paths: list[str]) -> dict[str, dict]:
        out = {}
        for x in paths:
            try:
                st = os.lstat(self._path(x, follow=False))
            except (ComputerError, OSError):
                continue
            out[x] = {"name": posixpath.basename(x), "path": x, "dir": stat.S_ISDIR(st.st_mode),
                      "size": st.st_size, "mtime": st.st_mtime}
        return out

    async def rm(self, path: str):
        try:
            p = self._path(path, follow=False)
        except ComputerError as e:
            raise ValueError(str(e)) from None
        if p == self.root:
            raise ValueError(i18n.t("不能删除整个工作目录"))
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            await asyncio.to_thread(shutil.rmtree, p)

    def upload_paths(self, paths: list[str]) -> list[str]:
        out = []
        for x in paths:
            p = self._path(x)
            if not p.is_file():
                raise ComputerError(i18n.t("文件不存在：{path}", path=x))
            out.append(str(p))
        return out

    # ---------- 终端 ----------
    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        if not shell_on():
            raise ComputerError(i18n.t("本机电脑的终端没有打开（电脑面板里可以打开，打开后每条命令都要你确认）"))
        sh = shutil.which("bash") or "/bin/sh"
        p = await asyncio.create_subprocess_exec(
            sh, "-c", cmd, cwd=self.root, env=_env(), start_new_session=True,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None), timeout)
        except asyncio.TimeoutError:
            try:   # 连它开的子进程一起停掉
                os.killpg(p.pid, signal.SIGKILL) if hasattr(os, "killpg") else p.kill()
            except OSError:
                pass
            await p.wait()
            return ExecResult(124, "", i18n.t("超时"), timed_out=True)
        return ExecResult(p.returncode, out.decode(errors="replace"), err.decode(errors="replace"))

    async def shell(self, cmd: str, timeout: int = 120) -> ExecResult:
        self.last_used = time.time()   # 终端不用浏览器，不用先开 Chrome
        return await self.exec(cmd, max(1, min(600, timeout)))
