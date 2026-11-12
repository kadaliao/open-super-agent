"""用 Docker Compose 部署时的 Docker 沙盒：服务不碰 Docker，经共用卷里的 unix socket 找电脑容器里的 computerd。
这里在本机起一个真的 computerd（image/computerd.py），服务这边走 local.py 的 SOCKET 模式：执行命令、读写文件、超时、
VNC 字节流、闲置时关浏览器再恢复。桌面是否就绪（xdotool）换成假的。"""
import asyncio
import importlib.util
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from osa import computer
from osa.computer import local

spec = importlib.util.spec_from_file_location("computerd", Path(local.IMAGE_DIR) / "computerd.py")
computerd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(computerd)


@pytest.fixture
def pc(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="osa-sock-", dir="/tmp")).resolve()   # unix socket 路径不能太长（macOS 上 104 字节）
    path = str(d / "computer.sock")
    monkeypatch.setattr(computerd, "WORKSPACE", str(d / "workspace"))
    monkeypatch.setattr(computerd, "ready", lambda: True)
    srv = computerd.serve(path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(local, "SOCKET", path)
    monkeypatch.setitem(computer.CFG.setdefault("computer", {}), "enabled", True)
    c = local.LocalComputer(0)
    c.workspace = str(d / "workspace")
    yield c
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(d, ignore_errors=True)


async def test_exec_files_and_timeout(pc):
    assert (await pc.status())["state"] == "running"
    await pc.start()
    r = await pc.exec("pwd; echo hi >&2; exit 3")
    assert r.exit_code == 3 and r.stdout.strip() == pc.workspace and r.stderr.strip() == "hi"
    assert (await pc.exec("cat", stdin="从 stdin 来")).stdout == "从 stdin 来"
    await pc.write_bytes(pc.workspace + "/a/b.bin", bytes(range(256)) * 100)   # 走 base64，和 docker exec 一样
    if sys.platform.startswith("linux"):   # 读文件用 GNU 的 base64 -w0（电脑容器里是 Debian）；macOS 自带的不认
        assert await pc.read_bytes(pc.workspace + "/a/b.bin") == bytes(range(256)) * 100
        assert [x["name"] for x in await pc.ls(pc.workspace + "/a")] == ["b.bin"]
    else:
        assert (Path(pc.workspace) / "a" / "b.bin").read_bytes() == bytes(range(256)) * 100
    r = await pc.exec("sleep 5", timeout=1)
    assert r.timed_out and r.exit_code == 124


async def test_idle_closes_the_browser_and_comes_back(pc):
    await pc.pause()                                     # 不能暂停容器：关掉浏览器，状态显示已暂停
    assert pc.idle and (await pc.status())["state"] == "paused"
    await pc.start()                                     # 恢复：桌面一直开着，浏览器用的时候再开
    assert not pc.idle and (await pc.status())["state"] == "running"


async def test_vnc_goes_through_the_socket(pc, monkeypatch):
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)
    monkeypatch.setattr(computerd, "VNC", echo.getsockname())

    def serve():
        conn, _ = echo.accept()
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data.upper())
    threading.Thread(target=serve, daemon=True).start()
    recv, send, close = await pc.open_vnc()
    await send(b"rfb 003.008\n")
    assert await asyncio.wait_for(recv(), 5) == b"RFB 003.008\n"
    await close()
    echo.close()


async def test_container_not_running(monkeypatch):
    monkeypatch.setattr(local, "SOCKET", "/tmp/osa-no-such.sock")
    monkeypatch.setitem(computer.CFG.setdefault("computer", {}), "enabled", True)
    c = local.LocalComputer(0)
    st = await c.status()
    assert st["state"] == "error" and "docker compose up -d computer" in st["detail"]
    assert computer.docker_ok()                          # 服务的容器里没有 docker 命令也算能用 Docker 沙盒


async def test_computer_turned_off(monkeypatch):
    """演示服务器（computer.enabled: false）的容器里没有 Docker 也没有电脑容器：面板上不报错，只是没开。"""
    monkeypatch.setattr(local, "SOCKET", "")
    monkeypatch.setitem(computer.CFG.setdefault("computer", {}), "enabled", False)
    assert await local.LocalComputer(0).status() == {"state": "none", "detail": ""}
