"""本地 Docker 后端：每个 Bot 一个容器，数据目录挂在 data/computers/bot-<id>（容器里的 /data）。"""
import asyncio
import json
from pathlib import Path
from ..config import CFG, ROOT
from .base import Computer, ComputerError, ExecResult

IMAGE = "osa-computer:1"
IMAGE_DIR = Path(__file__).parent / "image"


async def _run(*args, stdin: str | None = None, timeout: float = 120) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, "", "超时"
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
        self.data_dir = ROOT / "data" / "computers" / "shared"
        self._starting = asyncio.Lock()

    async def status(self) -> dict:
        code, out, _ = await _run("docker", "inspect", "-f", "{{json .State}}", self.name, timeout=15)
        if code:
            return {"state": "none", "detail": "还没有创建"}
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
                raise ComputerError("电脑镜像构建失败：" + (err or out)[-800:])

    async def start(self):
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
                self.data_dir.mkdir(parents=True, exist_ok=True)
                code, out, err = await _run(
                    "docker", "run", "-d", "--platform", await platform(), "--name", self.name, "--hostname", "osa",
                    "--shm-size=1g", "-p", "127.0.0.1::5900", "-v", f"{self.data_dir}:/data",
                    "-e", f"SCREEN_W={self.width}", "-e", f"SCREEN_H={self.height}",
                    "--label", "osa=computer", IMAGE, timeout=120)
                if code:
                    raise ComputerError("电脑启动失败：" + (err or out)[-500:])
            for _ in range(60):   # 等桌面就绪
                r = await self.exec("DISPLAY=:1 xdotool getdisplaygeometry", 10)
                if r.exit_code == 0:
                    return
                await asyncio.sleep(0.5)
            raise ComputerError("桌面没有在 30 秒内启动")

    async def pause(self):
        if (await self.status())["state"] == "running":
            await _run("docker", "pause", self.name)

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        args = ["docker", "exec", "-i", "-w", "/data/workspace", self.name, "bash", "-lc", cmd]
        code, out, err = await _run(*args, stdin=stdin if stdin is not None else "", timeout=timeout + 5)
        return ExecResult(code, out, err, timed_out=code == 124)

    async def _vnc_port(self) -> int:
        code, out, _ = await _run("docker", "port", self.name, "5900/tcp", timeout=10)
        if code or not out.strip():
            raise ComputerError("拿不到电脑的 VNC 端口")
        return int(out.strip().splitlines()[0].rsplit(":", 1)[1])

    async def open_vnc(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", await self._vnc_port())

        async def recv() -> bytes:
            data = await reader.read(65536)
            if not data:
                raise ConnectionError("VNC 连接已关闭")
            return data

        async def send(data: bytes):
            writer.write(data)
            await writer.drain()

        async def close():
            writer.close()

        return recv, send, close
