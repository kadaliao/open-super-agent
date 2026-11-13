"""下载电脑里的文件（/api/files/download）：按 Range 分段发（视频能拖进度）、大文件分块读、download=1 一律当附件、
音视频在浏览器里直接放；派到 Mac 上的任务发的文件去那台 Mac 上取，旧版 App 的小程序不会按段读时整个读一次再切。"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from test_computer_mac import mac  # noqa: F401  fixture：一台连着的 Mac

from osa import library, server
from osa.computer import base, host, link, mac as macs
from osa.computer.base import ExecResult

DATA = bytes(range(256)) * 40   # 10240 字节


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


@pytest.fixture
def pc(tmp_path, monkeypatch, no_password):
    """共用电脑换成本机电脑（工作文件夹在临时目录），里面放一个视频和一个压缩包。"""
    monkeypatch.setattr(host, "DATA", tmp_path / "data")
    monkeypatch.setattr(host, "chrome_path", lambda: "/fake/chrome")
    c = host.HostComputer(0)
    (c.root / "out").mkdir(parents=True)
    (c.root / "out" / "片子.mp4").write_bytes(DATA)
    (c.root / "out" / "a.zip").write_bytes(b"PK" + b"x" * 98)
    (c.root / "out" / "empty.txt").write_bytes(b"")

    async def files_pc(start):
        return c
    monkeypatch.setattr(server, "_files_pc", files_pc)
    monkeypatch.setattr(library, "root", lambda: c.workspace)
    monkeypatch.setattr(server, "CHUNK", 4096)   # 分块小一点，测得到一块一块地读
    return c


def test_byte_range():
    r = server._byte_range
    assert r(None, 100) is None and r("", 100) is None and r("items=0-1", 100) is None
    assert r("bytes=0-", 100) == (0, 99) and r("bytes=0-1", 100) == (0, 1) and r("bytes=10-500", 100) == (10, 99)
    assert r("bytes=-10", 100) == (90, 99) and r("bytes=-500", 100) == (0, 99)
    assert r("bytes=0-1,5-6", 100) is None and r("bytes=5-3", 100) is None    # 好几段、倒过来的：整个发
    assert r("bytes=100-", 100) is False and r("bytes=-0", 100) is False      # 超出文件：416


async def test_whole_file_in_chunks(pc, monkeypatch):
    reads = []
    real = pc.read_range

    async def counting(path, offset, length):
        reads.append((offset, length))
        return await real(path, offset, length)
    monkeypatch.setattr(pc, "read_range", counting)
    async with client() as c:
        r = await c.get("/api/files/download", params={"path": "out/片子.mp4"})
    assert r.status_code == 200 and r.content == DATA
    assert reads == [(0, 4096), (4096, 4096), (8192, 2048)]                   # 不整个读进内存
    assert r.headers["content-type"] == "video/mp4" and r.headers["content-disposition"].startswith("inline")
    assert r.headers["accept-ranges"] == "bytes" and r.headers["content-length"] == str(len(DATA))


async def test_ranges_for_video(pc):
    url = "/api/files/download?path=out/%E7%89%87%E5%AD%90.mp4"
    async with client() as c:
        r = await c.get(url, headers={"Range": "bytes=0-1"})                 # Safari 先探一下
        assert r.status_code == 206 and r.content == DATA[:2] and r.headers["content-range"] == f"bytes 0-1/{len(DATA)}"
        r = await c.get(url, headers={"Range": "bytes=5000-"})               # 拖进度
        assert r.status_code == 206 and r.content == DATA[5000:] and r.headers["content-length"] == str(len(DATA) - 5000)
        r = await c.get(url, headers={"Range": "bytes=-100"})
        assert r.status_code == 206 and r.content == DATA[-100:]
        r = await c.get(url, headers={"Range": f"bytes={len(DATA)}-"})
        assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(DATA)}"
        etag = (await c.get(url, headers={"Range": "bytes=0-0"})).headers["etag"]
        r = await c.get(url, headers={"Range": "bytes=0-9", "If-Range": etag})
        assert r.status_code == 206 and r.content == DATA[:10]
        r = await c.get(url, headers={"Range": "bytes=0-9", "If-Range": '"changed"'})   # 文件变了：整个重发
        assert r.status_code == 200 and r.content == DATA
        r = await c.get(url, headers={"If-None-Match": etag})
        assert r.status_code == 304 and not r.content


async def test_download_param_and_kinds(pc):
    async with client() as c:
        r = await c.get("/api/files/download", params={"path": "out/片子.mp4", "download": 1})
        assert r.headers["content-disposition"].startswith("attachment") and r.headers["content-type"] == "application/octet-stream"
        assert r.content == DATA
        r = await c.get("/api/files/download", params={"path": "out/a.zip"})
        assert r.headers["content-disposition"].startswith("attachment") and r.content.startswith(b"PK")
        r = await c.get("/api/files/download", params={"path": "out/empty.txt"})
        assert r.status_code == 200 and r.content == b""
        r = await c.get("/api/files/download", params={"path": "out/nope.mp4"})
        assert r.status_code == 404
        r = await c.get("/api/files/download", params={"path": "out"})                    # 文件夹
        assert r.status_code == 404
        r = await c.get("/api/files/download", params={"path": "../../etc/passwd"})
        assert r.status_code == 400


async def test_read_range_over_exec(tmp_path):
    """Docker / 云电脑经终端命令按段读。"""
    f = tmp_path / "v.bin"
    f.write_bytes(DATA)

    async def run(cmd, timeout=120, stdin=None):
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        return ExecResult(p.returncode, out.decode(), err.decode())
    fake = SimpleNamespace(exec=run)
    assert await base.Computer.read_range(fake, str(f), 0, 10) == DATA[:10]
    assert await base.Computer.read_range(fake, str(f), 10000, 4096) == DATA[10000:]   # 到结尾就短一些
    assert await base.Computer.read_range(fake, str(f), 20000, 10) == b""
    with pytest.raises(base.ComputerError, match="文件不存在"):
        await base.Computer.read_range(fake, str(tmp_path / "nope"), 0, 10)


async def test_files_on_a_mac(mac, monkeypatch, no_password):  # noqa: F811
    """派到 Mac 上的任务发的文件：带 mac= 去那台 Mac 上取。新版小程序按段读，旧版的整个读一次、两分钟里切着用。"""
    (mac.mac.root / "EP.mp4").write_bytes(DATA)
    monkeypatch.setitem(macs._macs, mac.lid, mac)
    monkeypatch.setattr(server, "CHUNK", 4096)
    params = {"path": mac.mac.workspace + "/EP.mp4", "mac": mac.lid}
    async with client() as c:
        mac.lk.can_range = True
        mac.pipe.sent.clear()
        r = await c.get("/api/files/download", params=params, headers={"Range": "bytes=100-199"})
        assert r.status_code == 206 and r.content == DATA[100:200] and mac.pipe.sent == ["stat", "read"]

        mac.lk.can_range = False   # 旧版 App
        mac.pipe.sent.clear()
        r = await c.get("/api/files/download", params=params)
        assert r.status_code == 200 and r.content == DATA
        r = await c.get("/api/files/download", params=params, headers={"Range": "bytes=-10"})
        assert r.content == DATA[-10:]
        assert mac.pipe.sent.count("read") == 1                              # 只整个读了一次

        r = await c.get("/api/files/download", params={**params, "mac": 999999})
        assert r.status_code == 404                                           # 这台 Mac 已经移除
        monkeypatch.delitem(link.online, mac.lid)
        r = await c.get("/api/files/download", params=params)
        assert r.status_code == 400 and "detail" in r.json()                  # 没连上：网页上显示原因


async def test_agent_reads_ranges(mac):  # noqa: F811
    (mac.mac.root / "a.bin").write_bytes(DATA)
    mac.lk.can_range = True
    assert await mac.read_range(mac.mac.workspace + "/a.bin", 250, 20) == DATA[250:270]
    assert await mac.read_range(mac.mac.workspace + "/a.bin", 10230, 100) == DATA[10230:]
