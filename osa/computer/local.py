"""本地 Docker 后端：所有 Bot 共用一个容器 osa-computer，容器里的 /data 默认挂 data/computers/shared。

用 Docker Compose 部署时服务自己也在容器里，它看到的路径和宿主机对不上，这时用环境变量 OSA_COMPUTER_DATA
指定一个命名卷（也可以是宿主机上的绝对路径）。
VNC 没有密码，只监听电脑容器里的 127.0.0.1，由服务经 docker exec 在电脑里转发：服务在不在容器里都能连上。
"""
import asyncio
import json
import os
from pathlib import Path
from .. import i18n
from ..config import CFG, DATA
from .base import Computer, ComputerError, ExecResult, check_enabled

IMAGE = "osa-computer:1"
IMAGE_DIR = Path(__file__).parent / "image"

# 在电脑里把 stdin / stdout 接到 x11vnc。网页关掉画面时 stdin 读到结尾，半关连接，x11vnc 随即断开，进程退出。
# 电脑刚创建时桌面先就绪、x11vnc 稍后才监听，所以连不上时等一会儿再试
_VNC_RELAY = """
import os, socket, sys, threading, time
for _ in range(50):
    try:
        s = socket.create_connection(("127.0.0.1", 5900))
        break
    except OSError:
        time.sleep(0.2)
else:
    sys.exit("VNC 没有启动")
def up():
    while True:
        d = os.read(0, 65536)
        if not d:
            break
        s.sendall(d)
    s.shutdown(socket.SHUT_WR)
threading.Thread(target=up, daemon=True).start()
out = sys.stdout.buffer
while True:
    d = s.recv(65536)
    if not d:
        break
    out.write(d)
    out.flush()
"""


async def _run(*args, stdin: str | None = None, timeout: float = 120) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, "", i18n.t("超时")
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


_platform: str | None = None


async def platform() -> str:
    """用 Docker 引擎的原生架构，避免环境变量 DOCKER_DEFAULT_PLATFORM 让镜像走模拟运行。"""
    global _platform
    if _platform is None:
        forced = (CFG.get("computer") or {}).get("local_platform")
        if forced:
            _platform = forced
        else:
            _, out, _ = await _run("docker", "info", "--format", "{{.Architecture}}", timeout=20)
            arch = out.strip()
            _platform = "linux/" + {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}.get(arch, "amd64")
    return _platform


class LocalComputer(Computer):
    backend = "local"

    def __init__(self, bot_id: int):
        super().__init__(bot_id)
        self.name = "osa-computer"
        self.data = os.environ.get("OSA_COMPUTER_DATA") or str(DATA / "computers" / "shared")
        self._starting = asyncio.Lock()

    async def status(self) -> dict:
        code, out, _ = await _run("docker", "inspect", "-f", "{{json .State}}", self.name, timeout=15)
        if code:
            return {"state": "none", "detail": i18n.t("还没有创建")}
        st = json.loads(out)
        state = {"running": "running", "paused": "paused", "exited": "stopped", "created": "stopped",
                 "restarting": "starting"}.get(st.get("Status"), "error")
        return {"state": state, "detail": st.get("Status", "")}

    async def _ensure_image(self):
        code, _, _ = await _run("docker", "image", "inspect", IMAGE, timeout=15)
        if code:
            code, out, err = await _run("docker", "build", "--platform", await platform(), "-t", IMAGE,
                                        str(IMAGE_DIR), timeout=1800)
            if code:
                raise ComputerError(i18n.t("电脑镜像构建失败：{error}", error=(err or out)[-800:]))

    async def start(self):
        check_enabled()
        async with self._starting:
            st = (await self.status())["state"]
            if st == "running":
                return
            if st == "paused":
                await _run("docker", "unpause", self.name)
            elif st == "stopped":
                await _run("docker", "start", self.name)
            else:
                await self._ensure_image()
                if os.path.isabs(self.data):   # 不是绝对路径就是命名卷，docker run 时自动创建
                    Path(self.data).mkdir(parents=True, exist_ok=True)
                code, out, err = await _run(
                    "docker", "run", "-d", "--platform", await platform(), "--name", self.name, "--hostname", "osa",
                    "--shm-size=1g", "-v", f"{self.data}:/data",
                    "-e", f"SCREEN_W={self.width}", "-e", f"SCREEN_H={self.height}",
                    "--label", "osa=computer", IMAGE, timeout=120)
                if code:
                    raise ComputerError(i18n.t("电脑启动失败：{error}", error=(err or out)[-500:]))
            for _ in range(60):   # 等桌面就绪
                r = await self.exec("DISPLAY=:1 xdotool getdisplaygeometry", 10)
                if r.exit_code == 0:
                    return
                await asyncio.sleep(0.5)
            raise ComputerError(i18n.t("桌面没有在 30 秒内启动"))

    async def pause(self):
        if (await self.status())["state"] == "running":
            await _run("docker", "pause", self.name)

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        args = ["docker", "exec", "-i", "-w", "/data/workspace", self.name, "bash", "-lc", cmd]
        code, out, err = await _run(*args, stdin=stdin if stdin is not None else "", timeout=timeout + 5)
        return ExecResult(code, out, err, timed_out=code == 124)

    async def open_vnc(self):
        p = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self.name, "python3", "-c", _VNC_RELAY,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        async def recv() -> bytes:
            data = await p.stdout.read(65536)
            if not data:
                raise ConnectionError("VNC 连接已关闭")
            return data

        async def send(data: bytes):
            p.stdin.write(data)
            await p.stdin.drain()

        async def close():
            if p.returncode is None:
                p.stdin.close()   # 电脑里的转发进程读到结尾后断开 VNC、自己退出
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except asyncio.TimeoutError:
                    p.kill()
                    await p.wait()

        return recv, send, close
