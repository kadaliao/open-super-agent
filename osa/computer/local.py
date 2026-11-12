"""Docker 沙盒：所有 Bot 共用一个电脑容器。两种用法：

- 服务直接跑在机器上（桌面 App 独立运行、开发时）：服务用 docker 命令自己创建、启停容器 osa-computer，
  容器里的 /data 默认挂 data/computers/shared（OSA_COMPUTER_DATA 可以换成命名卷或别的路径），执行命令、VNC 都经 docker exec。
- 用 Docker Compose 部署（设了 OSA_COMPUTER_SOCKET）：电脑是 compose 管的常驻容器，服务不碰 Docker（不挂 docker.sock，
  那等于宿主机的 root），经两个容器共用的卷里的 unix socket 找电脑里的 computerd（image/computerd.py）执行命令、转发 VNC。
  两个容器不在一个网络里，电脑里跑的东西连不到服务。这时“暂停”是关掉电脑里的浏览器省内存，桌面一直开着。
VNC 没有密码，只监听电脑容器里的 127.0.0.1。
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
SOCKET = os.environ.get("OSA_COMPUTER_SOCKET", "")   # Docker Compose 部署：电脑里的 computerd 在这个 unix socket 上

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
    try:
        p = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return 127, "", i18n.t("这台电脑没装 Docker")
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, "", i18n.t("超时")
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


_platform: str | None = None


def _problem(err: str) -> str:
    """docker 命令失败的原因，说成人话：没装、没开，否则原样。"""
    e = err.lower()
    if "cannot connect to the docker daemon" in e or "docker daemon is not running" in e or "docker.sock" in e:
        return i18n.t("Docker 没在运行：先打开 Docker Desktop（或 OrbStack），再点启动")
    return err.strip()[-500:]


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


async def _ask(req: dict, timeout: float) -> dict:
    """经 unix socket 让电脑里的 computerd 做一件事（一次连接一件），返回它回的那行 JSON。"""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(SOCKET, limit=64 << 20), 10)
    except (OSError, asyncio.TimeoutError):
        raise ComputerError(i18n.t("电脑容器没在运行（docker compose up -d computer）")) from None
    try:
        w.write((json.dumps(req, ensure_ascii=False) + "\n").encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout)
    except asyncio.TimeoutError:
        raise ComputerError(i18n.t("超时")) from None
    finally:
        w.close()
    if not line:
        raise ComputerError(i18n.t("电脑容器没有回应"))
    out = json.loads(line)
    if "error" in out:
        raise ComputerError(out["error"])
    return out


class LocalComputer(Computer):
    backend = "local"

    def __init__(self, bot_id: int):
        super().__init__(bot_id)
        self.name = "osa-computer"
        self.data = os.environ.get("OSA_COMPUTER_DATA") or str(DATA / "computers" / "shared")
        self._starting = asyncio.Lock()
        self.building = False   # 第一次用：正在构建电脑镜像（几分钟）
        self.idle = False       # compose 部署：闲置时关掉了浏览器（桌面还开着）

    async def status(self) -> dict:
        if not (CFG.get("computer") or {}).get("enabled", True):   # 演示服务器这类：电脑功能关着，不用去找 Docker
            return {"state": "none", "detail": ""}
        if SOCKET:
            try:
                ok = (await _ask({"op": "ready"}, 15))["ok"]
            except ComputerError as e:
                return {"state": "error", "detail": str(e)}
            return {"state": "starting" if not ok else "paused" if self.idle else "running", "detail": ""}
        if self.building:
            return {"state": "starting", "detail": i18n.t("第一次用要先准备电脑（下载、安装浏览器和桌面），要几分钟")}
        code, out, err = await _run("docker", "inspect", "-f", "{{json .State}}", self.name, timeout=15)
        if code:
            if code == 127 or "no such" not in err.lower():   # 没装 Docker、Docker 没开
                return {"state": "error", "detail": _problem(err)}
            return {"state": "none", "detail": i18n.t("还没有创建")}
        st = json.loads(out)
        state = {"running": "running", "paused": "paused", "exited": "stopped", "created": "stopped",
                 "restarting": "starting"}.get(st.get("Status"), "error")
        return {"state": state, "detail": st.get("Status", "")}

    async def _ensure_image(self):
        code, _, err = await _run("docker", "image", "inspect", IMAGE, timeout=15)
        if code and (code == 127 or "no such" not in err.lower()):
            raise ComputerError(_problem(err))
        if code:
            self.building = True
            self.changed()
            try:
                code, out, err = await _run("docker", "build", "--platform", await platform(), "-t", IMAGE,
                                            str(IMAGE_DIR), timeout=1800)
            finally:
                self.building = False
                self.changed()
            if code:
                raise ComputerError(i18n.t("电脑镜像构建失败：{error}", error=(err or out)[-800:]))

    async def start(self):
        check_enabled()
        if SOCKET:   # 容器由 compose 管：等桌面起来就行，浏览器用的时候自己开
            self.idle = False
            for _ in range(60):
                if (await self.status())["state"] == "running":
                    return
                await asyncio.sleep(0.5)
            raise ComputerError(i18n.t("桌面没有在 30 秒内启动"))
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
        if SOCKET:   # 不能暂停容器：关掉浏览器省内存（登录状态在 /data 里，下次用时浏览器控制脚本再打开）
            if (await self.status())["state"] == "running":
                await _ask({"op": "exec", "cmd": "pkill -TERM -f /usr/lib/chromium || true", "timeout": 20}, 30)
                self.idle = True
                self.changed()
            return
        if (await self.status())["state"] == "running":
            await _run("docker", "pause", self.name)

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        if SOCKET:
            self.idle = False
            try:
                r = await _ask({"op": "exec", "cmd": cmd, "stdin": stdin, "timeout": timeout}, timeout + 15)
            except ComputerError as e:
                return ExecResult(1, "", str(e))
            return ExecResult(int(r["exit_code"]), r["stdout"], r["stderr"], bool(r.get("timed_out")))
        args = ["docker", "exec", "-i", "-w", "/data/workspace", self.name, "bash", "-lc", cmd]
        code, out, err = await _run(*args, stdin=stdin if stdin is not None else "", timeout=timeout + 5)
        return ExecResult(code, out, err, timed_out=code == 124)

    async def open_vnc(self):
        if SOCKET:
            return await self._socket_vnc()
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

    async def _socket_vnc(self):
        """compose 部署：经 unix socket 让 computerd 接到桌面的 VNC，这条连接之后就是 VNC 字节流。"""
        try:
            r, w = await asyncio.wait_for(asyncio.open_unix_connection(SOCKET), 10)
        except (OSError, asyncio.TimeoutError):
            raise ComputerError(i18n.t("电脑容器没在运行（docker compose up -d computer）")) from None
        w.write(b'{"op": "vnc"}\n')
        await w.drain()
        ack = json.loads(await asyncio.wait_for(r.readline(), 15) or b"{}")
        if not ack.get("ok"):
            w.close()
            raise ComputerError(ack.get("error") or i18n.t("电脑容器没有回应"))

        async def recv() -> bytes:
            data = await r.read(65536)
            if not data:
                raise ConnectionError("VNC 连接已关闭")
            return data

        async def send(data: bytes):
            w.write(data)
            await w.drain()

        async def close():
            w.close()

        return recv, send, close
