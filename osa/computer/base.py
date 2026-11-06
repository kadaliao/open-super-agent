"""电脑接口。所有 Bot 共用一台电脑；后端只需实现执行命令、读写文件、启动/暂停和 VNC 连接。

几个任务可以同时用：每个任务在自己的浏览器窗口里做（登录状态、文件共用），操作都经 CDP 发给自己窗口里的页面（cdp_helper.py），
不动真实的鼠标键盘。同时用的任务最多 computer.parallel 个，多了排队。
真实的屏幕只给你看和接手：截全屏、你接手时从网页打字用 xdotool / scrot。
"""
import asyncio
import base64
import hashlib
import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from .. import i18n
from ..config import CFG

_HELPER = (Path(__file__).parent / "cdp_helper.py").read_bytes()
HELPER_PATH = f"/tmp/osa-cdp-{hashlib.sha1(_HELPER).hexdigest()[:8]}.py"

DISPLAY = ":1"
KEYS = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "space",
        "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "home": "Home", "end": "End",
        "pageup": "Prior", "pagedown": "Next", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "meta": "super", "cmd": "super",
        "super": "super", "win": "super", "+": "plus"}


class ComputerError(Exception):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def text(self, limit: int = 8000) -> str:
        out = self.stdout + (("\n[stderr]\n" + self.stderr) if self.stderr.strip() else "")
        if len(out) > limit:
            out = out[:limit] + f"\n…（输出过长，截断了 {len(out) - limit} 字）"
        tail = "（超时）" if self.timed_out else ""
        return f"退出码 {self.exit_code}{tail}\n{out}".strip()


def keysym(key: str) -> str:
    if not key or any(c.isspace() for c in key):
        raise ComputerError(i18n.t("按键名不合法：{key}", key=repr(key)))
    low = key.lower()
    if low in KEYS:
        return KEYS[low]
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return low.upper()
    return key


CHAT_KEEP = 600   # 对话里直接操作过的窗口，这么多秒内不分给任务
# 依赖“刚才看到的页面”的动作：用户接手过就不照原样做（见 browser）
STALE_OPS = {"click", "type", "key", "upload", "scroll", "back", "click_xy", "press", "wheel"}
STALE = "用户刚接手过电脑，页面可能已经变了，这一步没有执行。先看一下页面（browser_snapshot 或 computer_screenshot）再继续"


class Computer:
    backend = "base"
    on_change = None   # 谁在用电脑变了：() -> None，由 computer/__init__.py 设置，用来通知前端
    background = staticmethod(asyncio.ensure_future)   # 在后台跑一个协程（computer/__init__.py 换成会留引用的）

    def __init__(self, bot_id: int):
        self.bot_id = bot_id
        self.controller = "bot"          # bot | human：你接手期间，Bot 的操作都会等待
        self._handback = asyncio.Event()
        self._handback.set()
        self.last_used = time.time()
        self.lock = asyncio.Lock()        # 真实的鼠标键盘同一时间只做一件事
        self.width, self.height = 1280, 800
        # 谁在用：任务从第一次用浏览器起占一个窗口，到结束为止。key 是任务的标识（Heavy 的几个研究员各不相同）
        self.users: dict[str, dict] = {}         # key -> {"label", "task_id", "bot_id", "window", "browser", "since"}
        self.waiting: dict[str, str] = {}        # 排队的 key -> label
        self.exclusive, self.exclusive_label = "", ""   # 演示录制独占整台电脑
        self.watch: str | None = None            # 你点了“看这个”的任务：画面固定在它的窗口上
        self.chat_windows: dict[str, dict] = {}  # 对话里直接操作用的窗口：chat-<Bot id> -> {"window", "browser", "at"}
        self.asking: dict = {}                   # 正在“请你帮忙”的任务 id -> key（Heavy 的几个研究员共用一个任务 id）
        self._free = asyncio.Condition()
        self._assign = asyncio.Lock()            # 一次只给一个任务挑窗口，免得两个挑中同一个空窗口
        self._helper = asyncio.Lock()
        self._helper_ok = False

    @staticmethod
    def slots() -> int:
        return max(1, int((CFG.get("computer") or {}).get("parallel", 3)))

    def busy(self) -> str:
        """谁在用电脑，给排队提示和 Bot 看。"""
        return self.exclusive_label or i18n.t("、").join(u["label"] for u in self.users.values())

    def changed(self):
        if self.on_change:
            self.on_change()

    async def claim(self, key: str, label: str, on_wait=None, info: dict | None = None):
        """占一个位置；满了（或者在演示录制）就排队。同一个 key 重复占没关系。"""
        ok = lambda: key in self.users or (not self.exclusive and len(self.users) < self.slots())   # noqa: E731
        async with self._free:
            if key in self.users:
                return
            if not ok():
                self.waiting[key] = label
                self.changed()
                try:
                    if on_wait:
                        await on_wait(i18n.t("排队等电脑（{who} 正在用）", who=self.busy()))
                    await self._free.wait_for(ok)
                finally:   # 排队时被取消也要从队里拿掉
                    self.waiting.pop(key, None)
                    self.changed()
            self.users[key] = {"label": label, "since": time.time(), "window": None, **(info or {})}
        self.changed()

    async def claim_exclusive(self, key: str, label: str):
        """演示录制：没有任务在用时才能开始，录制期间任务都排队。"""
        async with self._free:
            if self.exclusive or self.users:
                raise ComputerError(i18n.t("电脑正在被用着（{who}）", who=self.busy()))
            self.exclusive, self.exclusive_label = key, label
        self.changed()

    async def release(self, key: str):
        async with self._free:
            u = self.users.pop(key, None)
            if self.exclusive == key:
                self.exclusive, self.exclusive_label = "", ""
            elif u is None:
                return
            if self.watch == key:
                self.watch = None
            self._free.notify_all()
        if u and u.get("window"):
            self.background(self._close(u["window"], u.get("browser")))
        self.changed()

    async def recheck(self):
        """并行数改大了：叫醒排队的。"""
        async with self._free:
            self._free.notify_all()

    async def _close(self, window: int, browser: str | None):
        """任务做完了：窗口比并行数多就关掉它的窗口，不多就留着（画面上还能看到它最后的样子）。"""
        try:
            async with self._assign:
                taken = [u.get("window") for u in self.users.values()] + [c.get("window") for c in self.chat_windows.values()]
                if window in taken:   # 排在前面的已经把它分给别人了
                    return
                await self._helper_run({"op": "close_window", "window": window, "browser": browser, "keep": self.slots()})
        except Exception:
            pass   # 电脑暂停了之类，下次再说

    # ---------- 后端需要实现 ----------
    async def status(self) -> dict:
        """{"state": "none|starting|running|paused|stopped|error", "detail": str}"""
        raise NotImplementedError

    async def start(self):
        """确保电脑在运行、桌面已启动。可以重复调用。"""
        raise NotImplementedError

    async def pause(self):
        raise NotImplementedError

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        raise NotImplementedError

    async def read_bytes(self, path: str, limit: int = 32 << 20) -> bytes:
        r = await self.exec(f"[ -f {shlex.quote(path)} ] || exit 3; base64 -w0 {shlex.quote(path)}", 120)
        if r.exit_code == 3:
            raise ComputerError(i18n.t("文件不存在：{path}", path=path))
        if r.exit_code:
            raise ComputerError(r.stderr or i18n.t("读取失败"))
        data = base64.b64decode(r.stdout)
        if len(data) > limit:
            raise ComputerError(i18n.t("文件太大"))
        return data

    async def write_bytes(self, path: str, data: bytes):
        q = shlex.quote(path)
        r = await self.exec(f"mkdir -p \"$(dirname {q})\" && base64 -d > {q}", 120, stdin=base64.b64encode(data).decode())
        if r.exit_code:
            raise ComputerError(r.stderr or i18n.t("写入失败"))

    async def open_vnc(self):
        """返回一个双向通道 (recv, send, close)，传的是 VNC（RFB）字节流，给网页上的实时画面用。"""
        raise NotImplementedError

    # ---------- 控制权 ----------
    def takeover(self):
        self.controller = "human"
        self._handback.clear()

    def handback(self):
        self.controller = "bot"
        self._handback.set()

    async def wait_for_control(self, timeout: float = 1800) -> bool:
        """用户接手期间等用户交还。返回这次是不是真的等过。"""
        if self._handback.is_set():
            return False
        await asyncio.wait_for(self._handback.wait(), timeout)
        return True

    # ---------- 真实的屏幕（截全屏给对话里看、你接手时从网页打字） ----------
    async def _desktop(self, cmd: str, stdin: str | None = None, timeout: int = 60, as_human: bool = False) -> ExecResult:
        if not as_human and await self.wait_for_control():   # 用户接手期间，Bot 的动作等用户交还
            raise ComputerError(STALE)
        await self.start()
        async with self.lock:
            self.last_used = time.time()
            r = await self.exec(f"export DISPLAY={DISPLAY}; {cmd}", timeout, stdin)
        if r.exit_code:
            raise ComputerError((r.stderr or r.stdout or i18n.t("退出码 {code}", code=r.exit_code)).strip()[:500])
        return r

    async def screenshot(self) -> bytes:
        """截整个屏幕（你在画面上看到的）。只是看，你接手时也不用等。"""
        path = f"/tmp/osa-shot-{time.time_ns()}.png"
        await self._desktop(f"scrot -o {path} 2>/dev/null || import -window root png:{path}", as_human=True)
        try:
            return await self.read_bytes(path)
        finally:
            await self.exec(f"rm -f {path}", 10)

    async def type(self, text: str, as_human: bool = False):
        if not text:
            return
        if text.isascii():
            await self._desktop("xdotool type --delay 12 --file -", stdin=text, as_human=as_human)
        else:
            # xdotool 打中文会丢字，改成写进剪贴板再粘贴；没有 xclip 时退回逐字输入
            await self._desktop("if command -v xclip >/dev/null; then xclip -selection clipboard -i && sleep 0.1 "
                                "&& xdotool key ctrl+v; else xdotool type --delay 30 --file -; fi", stdin=text, as_human=as_human)

    async def press(self, keys: str | list[str], as_human: bool = False):
        ks = keys if isinstance(keys, list) else [k for k in str(keys).replace(" ", "").split("+") if k]
        if not ks:
            raise ComputerError(i18n.t("没有按键"))
        await self._desktop(f"xdotool key -- {shlex.quote('+'.join(keysym(k) for k in ks))}", as_human=as_human)

    # ---------- 浏览器（CDP） ----------
    async def _helper_run(self, payload: dict) -> dict:
        """在电脑里跑一次浏览器控制脚本。脚本第一次用时写进电脑；电脑重建过就重新写。"""
        stdin = json.dumps(payload, ensure_ascii=False)
        for attempt in (0, 1):
            if not self._helper_ok:
                async with self._helper:
                    if not self._helper_ok:
                        if (await self.exec(f"test -f {HELPER_PATH}", 10)).exit_code:
                            await self.write_bytes(HELPER_PATH, _HELPER)
                        self._helper_ok = True
            r = await self.exec(f"export DISPLAY={DISPLAY}; python3 {HELPER_PATH}", 90, stdin)
            if r.exit_code == 0:
                return json.loads(r.stdout.strip().splitlines()[-1])
            err = (r.stderr or r.stdout or i18n.t("退出码 {code}", code=r.exit_code)).strip()
            if attempt == 0 and ("No such file" in err or "can't open file" in err):
                self._helper_ok = False
                continue
            raise ComputerError(err.splitlines()[-1].removeprefix("RuntimeError: ")[:500])
        raise ComputerError(i18n.t("浏览器控制脚本运行失败"))

    def _win(self, key: str, op: str = "") -> dict:
        """告诉脚本这次在哪个窗口做（见 cdp_helper.Ctx）。op 是要做的动作：对话里打开网页时可以换窗口。"""
        now = time.time()
        tasks = {k: u["window"] for k, u in self.users.items() if u.get("window")}
        chats = {k: c["window"] for k, c in self.chat_windows.items() if c.get("window") and now - c["at"] < CHAT_KEEP}
        avoid = [w for k, w in {**tasks, **chats}.items() if k != key]
        # 老浏览器没带防降速参数时可以借这次重开一次：电脑上只有这一个、你没在操作、不是要在已经打开的页面上做事
        alone = self.controller == "bot" and not chats and op != "front"
        if key in self.users:
            u = self.users[key]
            first = not u.get("window")
            watched = (self.users.get(self.watch) or {}).get("window") if self.watch != key else None
            return {"id": u.get("window"), "browser": u.get("browser"), "avoid": avoid, "keep_front": watched,
                    "front": first and not watched and self.controller == "bot",
                    "restart_ok": first and alone and len(self.users) == 1}
        c = self.chat_windows.get(key) or {}
        mine = c.get("window") if c.get("window") not in tasks.values() else None
        return {"id": mine, "browser": c.get("browser") if mine else None, "avoid": avoid, "front": True, "router": True,
                "op": op, "restart_ok": alone and op == "open" and not self.users}

    def _set_window(self, key: str, window, browser: str | None = None):
        if key in self.users:
            self.users[key].update(window=window, browser=browser)
        elif key.startswith("chat-"):
            self.chat_windows[key] = {"window": window, "browser": browser, "at": time.time()}

    async def browser(self, op: str, *, key: str | None = None, as_human: bool = False, **args) -> dict:
        """用 CDP 操作浏览器（见 cdp_helper.py）。key 是谁在操作：任务（claim 过的）在自己的窗口里做；
        对话里直接操作的（chat-<Bot id>）在屏幕上看到的那个窗口里做。不给 key 是老办法：最前面的标签页（演示录制）。
        as_human=True 是用户自己发起的操作（开始 / 结束演示录制、点“看这个”），用户接手时也照常执行。"""
        # 用户接手期间，Bot 的动作等用户交还。交还后不照原样做：用户可能换了页面、动了页面，
        # 按接手前看到的元素编号或坐标点下去会点错地方（打开网址、读页面、截图不看当前页面，照常做）
        if not as_human and await self.wait_for_control() and op in STALE_OPS:
            raise ComputerError(STALE)
        await self.start()
        self.last_used = time.time()
        if key is None:
            return await self._helper_run({"op": op, **args})
        if key not in self.users and not key.startswith("chat-"):
            raise ComputerError("这个任务已经不占用电脑了")
        if key.startswith("chat-") and self.watch:
            self.watch = None   # 你在对话里让 Bot 操作：画面跟着它，不再固定在某个任务上
            self.changed()
        for attempt in (0, 1):
            try:
                if self._win(key, op)["id"] is None:   # 要挑窗口：一次只挑一个（免得两个挑中同一个空窗口），挑好了再做动作
                    async with self._assign:
                        if (win := self._win(key, op))["id"] is None:
                            got = await self._helper_run({"op": "window", "win": win})
                            self._set_window(key, got["window"], got.get("browser"))
                            self.changed()
                out = await self._helper_run({"op": op, **args, "win": self._win(key, op)})
            except ComputerError as e:
                if str(e).startswith("WINDOW_GONE") and attempt == 0:   # 动作之前发现窗口没了（页面自己关的、浏览器重开过）：重新挑
                    self._set_window(key, None)
                    continue
                if str(e).startswith("WINDOW_CLOSED"):   # 动作做完窗口才没的：不能重做，下次再挑
                    self._set_window(key, None)
                    raise ComputerError(str(e).removeprefix("WINDOW_CLOSED ")) from None
                if str(e).startswith("FRONT_BUSY"):
                    w = str(e).split()[-1]
                    who = next((f"{u['label']} " for u in self.users.values() if str(u.get("window")) == w), "别的 Bot 在对话里用")
                    raise ComputerError(f"屏幕上现在是{who}的窗口，它还在用。要在那个页面上操作就先 cancel_task 停掉它；"
                                        "只是打开网页的话直接 browser_open，会换一个窗口。") from None
                raise
            self._set_window(key, out.get("window"), out.get("browser"))   # 点链接弹出的新窗口，脚本会跟过去
            return out
        raise ComputerError(i18n.t("找不到浏览器窗口"))

    async def page_shot(self, key: str) -> bytes:
        """截取 key 自己窗口里的页面（不含浏览器边框），点击坐标以它为准。"""
        r = await self.browser("screenshot", key=key)
        try:
            return await self.read_bytes(r["path"])
        finally:
            await self.exec(f"rm -f {shlex.quote(r['path'])}", 10)

    async def front(self, key: str):
        """把任务的窗口放到屏幕最前面，并固定看它（别的任务点击时会把它放回前面）。"""
        await self.browser("front", key=key, as_human=True)
        self.watch = key
        self.changed()

    async def shell(self, cmd: str, timeout: int = 120) -> ExecResult:
        """Bot 在电脑里执行终端命令（不需要桌面）。"""
        await self.start()
        self.last_used = time.time()
        return await self.exec(cmd, max(1, min(600, timeout)))
