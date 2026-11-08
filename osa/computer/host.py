"""本机电脑：Bot 用你这台电脑上装的 Chrome，不用 Docker。

- 浏览器：开一个单独的配置目录（数据目录下的 computers/host/chrome），和你日常用的 Chrome 分开；在里面登录一次，所有 Bot 共用。
  控制脚本 cdp_helper.py 就在这台电脑上跑，每个动作一个进程，和在 Docker 里一样；Chrome 由它按需打开，
  调试端口随机、只听 127.0.0.1。窗口不会被提到最前面，要看就点“显示窗口”。
- 文件：只放在工作文件夹（数据目录下的 workspace，和“文件系统”应用是同一个目录），读写、上传都限定在里面；
  浏览器下载的文件也存到这里的 downloads。
- 终端：默认关（computer.host_shell）。打开后在这台电脑上执行命令，每条都要你确认（tools.py），环境变量里的密钥不传过去。
- 画面：浏览器窗口就在你的屏幕上；网页上看截图（peek），手机上也能看。“接手”就是让 Bot 暂停，你直接在 Chrome 里操作。
- 整台电脑（只有 macOS）：你给 Open Super Agent 打开“辅助功能”和“屏幕录制”权限后，Bot 还能看整个屏幕、用真实的鼠标键盘操作别的 App
  （macdesk.py）。权限就是开关，没给就只用浏览器；config.yaml 的 computer.host_desktop: false 可以整个关掉。
  鼠标键盘只有一套，一次只给一个任务用（desk_claim，别的排队）；每个任务第一次用先问你（tools.py）。
  Bot 上一个动作之后你动了鼠标键盘（在 App 里点确认卡片、交还不算），它就自动停下，等你交还。屏幕锁着时什么都不做。
  不点、不往 Open Super Agent 自己的窗口里打字，免得它自己点“允许”。
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

from .. import i18n, security
from ..config import CFG, DATA
from . import macdesk
from .base import Computer, ComputerError, ExecResult, check_enabled

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
    return bool((CFG.get("computer") or {}).get("host_shell", False)) and security.local_exec()


def desktop_on() -> bool:
    """Bot 能看整个屏幕、用真实的鼠标键盘操作这台 Mac 上的 App：是 Mac、没关掉（computer.host_desktop）、两个权限都给了。"""
    if not (bool((CFG.get("computer") or {}).get("host_desktop", True)) and security.local_exec() and macdesk.supported()):
        return False
    return all(macdesk.permissions().values())


DESK_CHAT = 120   # 对话里直接操作屏幕：一轮说完就让出来（router），万一没让，这么多秒没再动也让给排队的任务
CHAT_OK = 600     # 对话里同意过操作整台电脑：这么多秒内再用不再问
QUIET = 0.5       # Bot 上一个动作之后又有键盘鼠标输入（晚了这么多秒以上）：是你在用电脑
DESK_STALE = "用户刚才在用电脑（或者接手过），屏幕可能已经变了，这一步没有执行。先 desktop_screenshot 看一下屏幕再继续"
OWN_TITLE = "Open Super Agent"   # 网页界面的标题：用户浏览器里开着的界面也不碰


def _own_pids() -> set[int]:
    """Open Super Agent 自己的进程：服务自己、桌面 App（服务的父进程）。"""
    out = {os.getpid()}
    if p := os.environ.get("OSA_PARENT_PID", ""):
        out.add(int(p) if p.isdigit() else 0)
    return out - {0}


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
        self._init_desk()

    def _init_desk(self):
        # 整台电脑：真实的鼠标键盘只有一套，一次只给一个用（任务的 key，或者对话里直接操作的 chat-<Bot id>）
        self.desk_owner, self.desk_label = "", ""
        self.desk_seen = 0.0           # 它最后一次用屏幕
        self.desk_at = 0.0             # Bot 最后一次操作屏幕：之后再有键盘鼠标输入，就是你在用电脑
        self.desk_ok: dict[str, float] = {}   # 同意过操作整台电脑的（task:<任务 id>，或 chat-<Bot id>）-> 什么时候；对话的过一会儿失效
        self.desk_denied: dict[str, float] = {}   # 不同意的 -> 什么时候：一会儿之内别再问
        self.paused_by = ""            # "input"：你动了鼠标键盘，Bot 自己停下的
        self._desk_free = asyncio.Condition()

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
        check_enabled()
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

    async def _show_screen(self, view: str) -> bool:
        """画面看整个屏幕还是 Bot 的 Chrome：你选的；没选就看最近一次操作的是哪个（Chrome 没开时看屏幕）。"""
        if view == "chrome" or not self.desktop_enabled() or self.desk_problem(await self._desk_info(), look=True):
            return False
        return view == "screen" or self.recent_desk or self.desk_busy() or (await self.status())["state"] != "running"

    async def peek(self, fmt: str = "jpeg", view: str = "") -> bytes:
        if await self._show_screen(view):
            return await self._capture(fmt, 60)
        return base64.b64decode((await self._peek(fmt))["data"])

    async def look(self) -> tuple[bytes, str]:
        if await self._show_screen(""):
            png = await self._capture()
            f = await self._front()
            return png, f"{f['app']} — {f['title']}" if f["title"] else f["app"]
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

    # ---------- 整台电脑（真实的屏幕、鼠标、键盘） ----------
    # 下面这几个是“在那台电脑上做”的部分：本机直接调 macdesk；服务器借用你的 Mac 时（linked.py）换成经连接让 Mac 去做
    def desktop_enabled(self) -> bool:
        """给不给 Bot desktop_* 工具。"""
        return desktop_on()

    async def _desk_info(self, fresh: bool = False) -> dict:
        """那台电脑现在的情况：两个权限、锁没锁屏、多久没有键盘鼠标输入。"""
        def get():
            return {"supported": macdesk.supported(), **macdesk.permissions(),
                    "locked": macdesk.locked(), "idle": macdesk.idle()}
        return await asyncio.to_thread(get)

    async def _desk_run(self, op: str, a: dict) -> dict:
        return await asyncio.to_thread(self._desk_sync, op, a)

    async def _capture(self, fmt: str = "png", quality: int = 70) -> bytes:
        return (await asyncio.to_thread(macdesk.capture, fmt, quality))[0]

    async def _front(self) -> dict:
        return await asyncio.to_thread(macdesk.front)

    async def grant(self) -> None:
        """弹系统的授权提示、打开系统设置里对应的页面。"""
        await asyncio.to_thread(macdesk.request_permissions)

    def desk_problem(self, info: dict, look: bool = False) -> str:
        """现在不能操作屏幕的原因；能操作返回空。info 是 _desk_info()；look：只是看（不用辅助功能权限）。"""
        if not self.desk_supported() or not (CFG.get("computer") or {}).get("host_desktop", True):
            return i18n.t("这台电脑不能让 Bot 操作整个屏幕（只有 Mac 可以）")
        missing = [i18n.t(n) for k, n in (("accessibility", i18n.N_("辅助功能")), ("screen", i18n.N_("屏幕录制")))
                   if not info.get(k) and not (look and k == "accessibility")]
        if missing:
            return i18n.t("还没给 Open Super Agent 这些权限：{what}。电脑面板里点“去打开权限”，或者到 系统设置 → 隐私与安全性 里打开",
                          what=i18n.t("、").join(missing))
        if not look and info.get("locked"):
            return i18n.t("屏幕锁着（或者显示器睡着了），现在不能操作")
        return ""

    def desk_busy(self) -> bool:
        """有谁正占着屏幕。"""
        return self._held("")

    def _held(self, key: str) -> bool:
        o = self.desk_owner
        return bool(o) and o != key and not (o.startswith("chat-") and time.time() - self.desk_seen > DESK_CHAT)

    async def desk_claim(self, key: str, label: str, on_wait=None):
        """占住屏幕（真实的鼠标键盘）。别的任务在用就排队；对话里直接操作的不等，直接告诉模型。"""
        async with self._desk_free:
            if self._held(key):
                if key.startswith("chat-"):
                    raise ComputerError(f"屏幕正被{self.desk_label}用着（鼠标键盘一次只能一个任务用）。等它做完，或者先 cancel_task 停掉它。")
                if on_wait:
                    await on_wait(i18n.t("排队等屏幕（{who} 正在用）", who=self.desk_label))
                while self._held(key):   # 对话占着的过一会儿自动让出来，所以隔几秒看一次
                    try:
                        await asyncio.wait_for(self._desk_free.wait(), 5)
                    except asyncio.TimeoutError:
                        pass
            if self.desk_owner != key:
                self.desk_owner, self.desk_label, self.desk_at = key, label, 0.0
                self.changed()
            self.desk_seen = time.time()

    async def desk_release(self, key: str):
        if self.desk_owner != key:
            return
        async with self._desk_free:
            self.desk_owner = self.desk_label = ""
            self._desk_free.notify_all()
        self.changed()

    async def release(self, key: str):
        await super().release(key)
        await self.desk_release(key)

    def handback(self):
        super().handback()
        self.paused_by = ""
        if self.desk_at:
            self.desk_at = time.time()   # 你点“交还”的这一下不算你在用电脑

    def desk_allowed(self, key: str) -> bool:
        """这个任务（或对话）已经同意过操作整台电脑。"""
        t = self.desk_ok.get(key)
        return t is not None and (not key.startswith("chat-") or time.time() - t < CHAT_OK)   # 任务 id 不重复，任务的一直算

    def user_active(self, info: dict) -> bool:
        """Bot 上一个屏幕动作之后，有没有别的键盘鼠标输入（你在用电脑）。在 App 里点确认卡片不算。
        只比时长不比时刻：借用的 Mac 和服务器的时钟可以不一样，网络慢只会让这里晚一点发现，不会误判。"""
        if not self.desk_at:
            return False
        from .. import approvals
        since = max(self.desk_at, approvals.last_done)
        return (time.time() - since) - info["idle"] > QUIET

    def _guard(self, op: str, a: dict):
        """不碰 Open Super Agent 自己的窗口（App 和浏览器里开着的界面）：Bot 不能自己点“允许”、改设置。"""
        own = _own_pids()
        if op in ("click", "drag", "scroll", "inspect"):
            wins = macdesk.windows()
            hit = False
            for x, y in [(a["x"], a["y"])] + ([(a["to_x"], a["to_y"])] if op == "drag" else []):   # 拖动的终点也算
                win = next((w for w in wins if w["x"] <= x < w["x"] + w["w"] and w["y"] <= y < w["y"] + w["h"]), {})
                hit = hit or macdesk.element_at(x, y)["pid"] in own or win.get("pid") in own or OWN_TITLE in (win.get("title") or "")
        elif op in ("type", "key"):
            f = macdesk.front()
            hit = f["pid"] in own or OWN_TITLE in f["title"]
        else:
            return
        if hit:
            raise ComputerError("那里是 Open Super Agent 自己的窗口，Bot 不能操作它（确认卡片、设置只能用户自己点）。"
                                "要操作别的 App，先用 desktop_open_app 把它切到前面。")

    def _desk_sync(self, op: str, a: dict) -> dict:
        m = macdesk
        if op in ("click", "drag", "scroll", "inspect"):
            a = {**a, **dict(zip(("x", "y"), m.to_screen(a["x"], a["y"])))}
        if op == "drag":
            a["to_x"], a["to_y"] = m.to_screen(a["to_x"], a["to_y"])
        self._guard(op, a)
        if op == "screenshot":
            png, w, h = m.capture()
            cx, cy = m.from_screen(*m.cursor())
            return {"png": png, "width": w, "height": h, "front": m.front(), "windows": m.windows(8), "cursor": [cx, cy]}
        if op == "inspect":
            el = m.element_at(a["x"], a["y"])
            return {**el, "app": next((w["app"] for w in m.windows() if w["pid"] == el["pid"]), "")}
        if op == "focused":
            return {**m.focused(), "app": m.front()["app"]}
        if op == "click":
            m.click(a["x"], a["y"], a.get("button") or "left", 2 if a.get("double") else 1)
        elif op == "drag":
            m.drag(a["x"], a["y"], a["to_x"], a["to_y"])
        elif op == "scroll":
            n = max(1, min(20, int(a.get("amount") or 3)))
            d = a.get("direction") or "down"
            m.scroll(a["x"], a["y"], dy={"up": n, "down": -n}.get(d, 0), dx={"left": n, "right": -n}.get(d, 0))
        elif op == "type":
            m.type_text(a["text"])
        elif op == "key":
            m.press_seq(a["keys"])
        elif op == "open_app":
            if err := m.open_app(a["name"]):
                raise ComputerError(err)
        else:
            raise ComputerError(f"未知操作 {op}")
        return {}

    async def desk(self, op: str, key: str, **a) -> dict:
        """Bot 在屏幕上做一个动作（先 desk_claim）。坐标按 desktop_screenshot 截图上的算。
        你接手期间等你交还；Bot 上一个动作之后你动过鼠标键盘，就自动接手、等你交还。交还后这一步不做（屏幕可能变了）。"""
        if self.desk_owner != key:
            raise ComputerError("没有占住屏幕")
        if await self.wait_for_control():
            raise ComputerError(DESK_STALE)
        info = await self._desk_info(fresh=True)
        if why := self.desk_problem(info):
            raise ComputerError(why)
        if self.user_active(info):
            self.paused_by = "input"
            self.takeover()
            self.changed()
            await self.wait_for_control()
            raise ComputerError(DESK_STALE)
        async with self.lock:
            try:
                return await self._desk_run(op, a)
            except (ValueError, RuntimeError, OSError) as e:
                raise ComputerError(str(e)) from None
            finally:
                self.desk_at = self.desk_seen = self.last_used = time.time()
                if key in self.desk_ok:   # 对话的同意从最后一次用算起
                    self.desk_ok[key] = self.desk_at
                if not self.recent_desk:
                    self.recent_desk = True
                    self.changed()

    def desk_supported(self) -> bool:
        return macdesk.supported() and security.local_exec()

    async def desk_state(self) -> dict:
        """给电脑面板：能不能用、开没开、权限、谁在用。"""
        if not self.desk_supported():
            return {"supported": False}
        out = {"supported": True, "paused_by": self.paused_by, "owner": self.desk_label if self.desk_busy() else ""}
        try:
            info = await self._desk_info()
        except ComputerError as e:
            return {**out, "problem": str(e)}
        out |= {k: bool(info.get(k)) for k in ("accessibility", "screen")}
        out["problem"] = self.desk_problem(info)
        return out

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
    def shell_enabled(self) -> bool:
        return shell_on()

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
