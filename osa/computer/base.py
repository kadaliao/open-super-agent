"""电脑接口。所有 Bot 共用一台电脑；后端只需实现执行命令、读写文件、启动/暂停和 VNC 连接。

桌面操作（截图、点击、输入……）统一在电脑里用 xdotool / scrot 完成，和 Instabox SDK 的做法一致。
"""
import asyncio
import base64
import hashlib
import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

_HELPER = (Path(__file__).parent / "cdp_helper.py").read_bytes()
HELPER_PATH = f"/tmp/osa-cdp-{hashlib.sha1(_HELPER).hexdigest()[:8]}.py"

DISPLAY = ":1"
BUTTONS = {"left": 1, "middle": 2, "right": 3}
WHEEL = {"up": 4, "down": 5, "left": 6, "right": 7}
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
        raise ComputerError(f"按键名不合法：{key!r}")
    low = key.lower()
    if low in KEYS:
        return KEYS[low]
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return low.upper()
    return key


def _coord(v) -> int:
    v = int(v)
    if not 0 <= v <= 16384:
        raise ComputerError(f"坐标超出范围：{v}")
    return v


class Computer:
    backend = "base"

    def __init__(self, bot_id: int):
        self.bot_id = bot_id
        self.controller = "bot"          # bot | human：你接手期间，Bot 的桌面操作会等待
        self._handback = asyncio.Event()
        self._handback.set()
        self.last_used = time.time()
        self.lock = asyncio.Lock()        # 一台电脑同一时间只执行一个桌面动作
        self.width, self.height = 1280, 800
        # 所有 Bot 共用这台电脑：一个任务从第一次用电脑起独占到结束，其他任务排队
        self.owner: str | None = None
        self.owner_label = ""
        self._free = asyncio.Condition()

    async def claim(self, key: str, label: str, on_wait=None):
        async with self._free:
            if self.owner not in (None, key) and on_wait:
                await on_wait(f"排队等电脑（{self.owner_label} 正在用）")
            await self._free.wait_for(lambda: self.owner in (None, key))
            self.owner, self.owner_label = key, label

    async def release(self, key: str):
        async with self._free:
            if self.owner == key:
                self.owner, self.owner_label = None, ""
                self._free.notify_all()

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
            raise ComputerError(f"文件不存在：{path}")
        if r.exit_code:
            raise ComputerError(r.stderr or "读取失败")
        data = base64.b64decode(r.stdout)
        if len(data) > limit:
            raise ComputerError("文件太大")
        return data

    async def write_bytes(self, path: str, data: bytes):
        q = shlex.quote(path)
        r = await self.exec(f"mkdir -p \"$(dirname {q})\" && base64 -d > {q}", 120, stdin=base64.b64encode(data).decode())
        if r.exit_code:
            raise ComputerError(r.stderr or "写入失败")

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

    async def wait_for_control(self, timeout: float = 1800):
        if not self._handback.is_set():
            await asyncio.wait_for(self._handback.wait(), timeout)

    # ---------- 桌面操作（公共实现） ----------
    async def _desktop(self, cmd: str, stdin: str | None = None, timeout: int = 60) -> ExecResult:
        await self.wait_for_control()
        await self.start()
        async with self.lock:
            self.last_used = time.time()
            r = await self.exec(f"export DISPLAY={DISPLAY}; {cmd}", timeout, stdin)
        if r.exit_code:
            raise ComputerError((r.stderr or r.stdout or f"退出码 {r.exit_code}").strip()[:500])
        return r

    async def screenshot(self) -> bytes:
        path = f"/tmp/osa-shot-{time.time_ns()}.png"
        await self._desktop(f"scrot -o {path} 2>/dev/null || import -window root png:{path}")
        try:
            return await self.read_bytes(path)
        finally:
            await self.exec(f"rm -f {path}", 10)

    async def click(self, x, y, button: str = "left", double: bool = False):
        b = BUTTONS.get(button)
        if not b:
            raise ComputerError("button 只能是 left / right / middle")
        rep = " --repeat 2 --delay 80" if double else ""
        await self._desktop(f"xdotool mousemove {_coord(x)} {_coord(y)} click{rep} {b}")

    async def move(self, x, y):
        await self._desktop(f"xdotool mousemove {_coord(x)} {_coord(y)}")

    async def drag(self, x1, y1, x2, y2):
        await self._desktop(f"xdotool mousemove {_coord(x1)} {_coord(y1)} mousedown 1 sleep 0.1 "
                            f"mousemove {_coord(x2)} {_coord(y2)} sleep 0.1 mouseup 1")

    async def scroll(self, direction: str = "down", amount: int = 3):
        w = WHEEL.get(direction)
        if not w:
            raise ComputerError("direction 只能是 up / down / left / right")
        await self._desktop(f"xdotool click --repeat {max(1, min(100, int(amount)))} --delay 30 {w}")

    async def type(self, text: str):
        if not text:
            return
        if text.isascii():
            await self._desktop("xdotool type --delay 12 --file -", stdin=text)
        else:
            # xdotool 打中文会丢字，改成写进剪贴板再粘贴；没有 xclip 时退回逐字输入
            await self._desktop("if command -v xclip >/dev/null; then xclip -selection clipboard -i && sleep 0.1 "
                                "&& xdotool key ctrl+v; else xdotool type --delay 30 --file -; fi", stdin=text)

    async def press(self, keys: str | list[str]):
        ks = keys if isinstance(keys, list) else [k for k in str(keys).replace(" ", "").split("+") if k]
        if not ks:
            raise ComputerError("没有按键")
        await self._desktop(f"xdotool key -- {shlex.quote('+'.join(keysym(k) for k in ks))}")

    async def browser(self, op: str, **args) -> dict:
        """用 CDP 操作浏览器（见 cdp_helper.py）。脚本第一次用时写进电脑；电脑重建过就重新写。"""
        stdin = json.dumps({"op": op, **args}, ensure_ascii=False)
        for attempt in (0, 1):
            r = await self.exec(f"test -f {HELPER_PATH}", 10) if attempt == 0 else None
            if r is None or r.exit_code:
                await self.write_bytes(HELPER_PATH, _HELPER)
            try:
                out = await self._desktop(f"python3 {HELPER_PATH}", stdin=stdin, timeout=90)
                return json.loads(out.stdout.strip().splitlines()[-1])
            except ComputerError as e:
                if attempt == 0 and ("No such file" in str(e) or "can't open file" in str(e)):
                    continue
                raise
        raise ComputerError("浏览器控制脚本运行失败")

    async def open_url(self, url: str):
        if not url.startswith(("http://", "https://")):
            raise ComputerError("只能打开 http / https 网址")
        try:
            await self.browser("open", url=url)
            return
        except ComputerError:
            pass   # CDP 不可用时退回老办法：命令行打开新标签页
        await self._desktop(
            "b=$(command -v chromium || command -v chromium-browser || command -v google-chrome) || "
            "{ echo '电脑里没有浏览器' >&2; exit 127; }; "
            f"setsid nohup \"$b\" --no-first-run --no-default-browser-check --password-store=basic "
            f"{shlex.quote(url)} >/dev/null 2>&1 </dev/null &")
        await asyncio.sleep(2.5)   # 给页面一点加载时间

    async def shell(self, cmd: str, timeout: int = 120) -> ExecResult:
        """Bot 在电脑里执行终端命令（不需要桌面）。"""
        await self.start()
        self.last_used = time.time()
        return await self.exec(cmd, max(1, min(600, timeout)))
