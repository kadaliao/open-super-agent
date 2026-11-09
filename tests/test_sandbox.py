"""在沙箱里跑代码（sandbox.py）：本机的受限子进程在这台机器上真跑一次 python（能写工作文件夹、写不了外面、读不到数据目录、
quick 版连不上网）；电脑后端（Docker / Instabox）用假的 exec，但电脑里的那段脚本真用 bash 跑一遍。"""
import asyncio
import base64
import io
import os
import tarfile
from pathlib import Path

import pytest

from osa import builtin, computer, registry, sandbox
from osa.config import CFG, ROOT
from osa.computer.base import Computer, ExecResult

KINDS = [k for k in sandbox.kinds() if sandbox.works(k)]
host_kinds = pytest.mark.parametrize("kind", KINDS or [pytest.param("", marks=pytest.mark.skip(reason="这台机器没有沙箱"))])


@pytest.fixture
def data(tmp_path, monkeypatch):
    """假的数据目录：工作文件夹在里面，旁边放一份“机密”。"""
    d = (tmp_path / "data").resolve()
    (d / "workspace").mkdir(parents=True)
    (d / "secret.txt").write_text("机密-不该被读到")
    monkeypatch.setattr(sandbox, "DATA", d)
    monkeypatch.setitem(CFG, "security", {"local_exec": True})
    return d


def use(monkeypatch, kind: str):
    monkeypatch.setattr(sandbox, "_probed", {"kind": kind})


def call(name: str, **args) -> registry.Call:
    return registry.Call(registry.ROUTER if name == "quick_code" else registry.WORKER, {"id": 1}, 1, 1, args)


@host_kinds
async def test_host_sandbox_writes_only_workspace(kind, data, tmp_path, monkeypatch):
    use(monkeypatch, kind)
    ws = data / "workspace"
    outside, home = tmp_path / "outside.txt", Path.home() / f"osa-sandbox-test-{os.getpid()}.txt"
    code = f"""
import pathlib
pathlib.Path("result.txt").write_text("42")                  # 当前目录：工作文件夹里这次运行的文件夹
pathlib.Path({str(ws / "note.txt")!r}).write_text("hi")       # 工作文件夹里别的地方也能写
print("input:", open("sub/in.csv").read().strip())
for p in ({str(outside)!r}, {str(home)!r}, {str(data / "x.txt")!r}):
    try:
        open(p, "w").write("x")
    except OSError:
        print("写不了", p)
for p in ({str(data / "secret.txt")!r}, {str(ROOT / "pyproject.toml")!r}):
    try:
        print("读到了", open(p).read())
    except OSError:
        print("读不到", p)
print(sum(range(10)))
"""
    try:
        r = await sandbox.execute("python", code, {"sub/in.csv": "a,b"}, timeout=30, computer=False)
        assert r.text.startswith("退出码 0"), r.text
        assert "input: a,b" in r.text and "45" in r.text
        assert "读到了" not in r.text and "机密" not in r.text                  # 数据目录、代码目录（配置）读不到
        assert not outside.exists() and not home.exists() and not (data / "x.txt").exists()   # 工作文件夹外写不进去
        assert (ws / "note.txt").read_text() == "hi"
        assert len(r.files) == 1 and r.files[0].endswith("/result.txt")       # 带进去的文件、代码本身不算产出
        out = Path(r.files[0])
        assert out.read_text() == "42" and out.parent.parent == ws / ".run"
        assert not (out.parent / "main.py").exists()
        assert str(out) in r.text
    finally:
        for p in (outside, home):
            p.unlink(missing_ok=True)


@host_kinds
async def test_quick_code_is_offline_and_fast(kind, data, monkeypatch):
    use(monkeypatch, kind)
    tool = registry.available("quick_code", registry.ROUTER, {"scope": registry.ROUTER})
    assert tool and not tool.gated
    assert "42" in await tool.run(call("quick_code", code="print(6 * 7)"))
    net = """
import socket
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2).close()
    print("连上了")
except OSError as e:
    print("连不上", e)
"""
    out = await tool.run(call("quick_code", code=net))
    assert "连不上" in out and "连上了" not in out
    assert not list((data / "workspace" / ".run").iterdir())   # 没有产出的运行文件夹不留下

    monkeypatch.setattr(sandbox, "QUICK_TIMEOUT", 1)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await tool.run(call("quick_code", code="while True: pass")) == sandbox.QUICK_TIMED_OUT
    assert loop.time() - t0 < 5


@host_kinds
async def test_host_run_code_tool_and_timeout(kind, data, monkeypatch):
    use(monkeypatch, kind)
    tool = registry.available("run_code", registry.WORKER, {"scope": registry.WORKER, "use_pc": False})
    assert tool and not tool.gated
    out = await tool.run(call("run_code", lang="bash", code="echo $((2 ** 10)); echo oops >&2; exit 3"))
    assert out.startswith("退出码 3") and "1024" in out and "oops" in out
    r = await sandbox.execute("python", "import time\nprint('开始', flush=True)\ntime.sleep(30)", timeout=1, computer=False)
    assert r.timed_out and "超时了" in r.text and "开始" in r.text


@host_kinds
async def test_linked_mac_gets_the_outputs(kind, data, monkeypatch):
    """借来的 Mac：代码在服务器上跑，产出的文件传到 Mac 的工作文件夹，服务器上不留。"""
    use(monkeypatch, kind)

    class Mac:
        workspace = "/Users/me/OSA"
        got: dict = {}

        async def write_bytes(self, path, data):
            self.got[path] = data
    mac = Mac()
    monkeypatch.setattr(computer, "backend_name", lambda: "linked")
    monkeypatch.setattr(computer, "get", lambda bot_id=0: mac)
    r = await sandbox.execute("python", "open('chart.txt', 'w').write('图')", timeout=30)
    assert len(r.files) == 1 and r.files[0].startswith("/Users/me/OSA/.run/") and r.files[0].endswith("/chart.txt")
    assert list(mac.got.values()) == ["图".encode()] and "传到 Mac" in r.text
    assert not list((data / "workspace" / ".run").iterdir())


async def test_no_sandbox_explains(data, monkeypatch):
    use(monkeypatch, "")
    monkeypatch.setattr(sandbox, "_computer_backend", lambda: "")
    assert registry.available("run_code", registry.WORKER, {"scope": registry.WORKER}) is None
    assert registry.available("quick_code", registry.ROUTER, {"scope": registry.ROUTER}) is None
    monkeypatch.setitem(CFG, "computer", {**CFG["computer"], "backend": "host"})
    text, files = await sandbox.run("python", "print(1)")
    assert "没有沙箱" in text and "computer_shell" in text and files == []   # 本机电脑：可以改用终端（每条确认）
    monkeypatch.setitem(CFG, "security", {"local_exec": False})
    text, _ = await sandbox.run("python", "print(1)")
    assert "只允许在沙箱里" in text


def test_exec_mode(monkeypatch):
    for sec, mode in (({}, "host"), ({"local_exec": False}, "sandbox"), ({"demo": True}, "sandbox"),
                      ({"exec": "off"}, "off"), ({"exec": "sandbox"}, "sandbox"), ({"local_exec": False, "exec": "host"}, "host"),
                      ({"demo": True, "exec": "host"}, "sandbox")):
        monkeypatch.setitem(CFG, "security", sec)
        assert sandbox.exec_mode() == mode, sec


async def test_exec_off(data, monkeypatch):
    monkeypatch.setitem(CFG, "security", {"exec": "off"})
    monkeypatch.setattr(sandbox, "_computer_backend", lambda: "local")
    assert sandbox.place() == "" and sandbox.place(quick=True) == ""
    text, _ = await sandbox.run("python", "print(1)")
    assert "关掉了执行代码" in text


async def test_bad_input():
    assert "不支持" in (await sandbox.run("ruby", "puts 1"))[0]
    assert "没有要跑的代码" in (await sandbox.run("python", "  "))[0]
    assert "文件名不行" in (await sandbox.run("python", "print(1)", {"../x": "1"}))[0]
    assert "文件名不行" in (await sandbox.run("python", "print(1)", {"/etc/x": "1"}))[0]
    assert "太大" in (await sandbox.run("python", "x" * (sandbox.MAX_INPUT + 1)))[0]


# ---------- 电脑后端 ----------
class FakeBox(Computer):
    """冒充 Docker 电脑：exec 收到的命令在本机一个临时目录里用 bash 真跑（那就是电脑里会跑的脚本）。"""
    backend = "local"

    def __init__(self, workspace: Path, missing: str = ""):
        super().__init__(0)
        self.workspace = str(workspace)
        self.missing = missing
        self.calls: list[tuple[str, str | None]] = []

    async def status(self) -> dict:
        return {"state": "running"}

    async def start(self):
        pass

    async def exec(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> ExecResult:
        self.calls.append((cmd, stdin))
        if self.missing and f"command -v {self.missing} " in cmd:
            return ExecResult(97, "", "")
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, cwd=self.workspace, stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate((stdin or "").encode()), timeout)
        return ExecResult(p.returncode, out.decode(), err.decode())


@pytest.fixture
def box(tmp_path, monkeypatch):
    c = FakeBox((tmp_path / "box").resolve())
    Path(c.workspace).mkdir()
    monkeypatch.setattr(sandbox, "_computer_backend", lambda: "local")
    monkeypatch.setattr(computer, "get", lambda bot_id=0: c)
    monkeypatch.setitem(CFG, "security", {"local_exec": False})
    return c


async def test_runs_in_computer(box):
    assert sandbox.place() == "computer"
    code = "cat data.csv | tr a-z A-Z; mkdir -p out; echo done > out/r.txt; echo warn >&2"
    r = await sandbox.execute("bash", code, {"data.csv": "a,b\n"}, timeout=20)
    cmd, stdin = box.calls[-1]
    assert cmd.startswith("bash -c ")
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(stdin))) as t:   # 代码和文件经 stdin 打包进去，命令行不会太长
        assert sorted(t.getnames()) == ["data.csv", "main.sh"]
    assert r.text.startswith("退出码 0"), r.text
    assert "A,B" in r.text and "warn" in r.text
    assert len(r.files) == 1 and r.files[0].startswith(f"{box.workspace}/.run/") and r.files[0].endswith("/out/r.txt")
    run = Path(r.files[0]).parent.parent
    assert Path(r.files[0]).read_text() == "done\n"
    assert not (run / "main.sh").exists() and (run / "data.csv").exists()

    r = await sandbox.execute("bash", "echo 只是算一下; exit 2", timeout=20)
    assert r.text.startswith("退出码 2") and "只是算一下" in r.text and r.files == []
    assert [p.name for p in (Path(box.workspace) / ".run").iterdir()] == [run.name]   # 没产出的运行文件夹删掉了


async def test_computer_missing_language_and_timeout(box, monkeypatch):
    box.missing = "node"
    text, files = await sandbox.run("node", "console.log(1)")
    assert "电脑里没有 node" in text and files == []
    box.missing = ""

    async def slow(cmd, timeout=120, stdin=None):
        return ExecResult(0, "跑了一半\n\n" + sandbox.MARK + "124\n\n", "")
    monkeypatch.setattr(box, "exec", slow)
    r = await sandbox.execute("python", "while True: pass", timeout=3)
    assert r.timed_out and "超时了：3 秒" in r.text and "跑了一半" in r.text


async def test_router_quick_code_never_waits_for_computer(box, monkeypatch):
    use(monkeypatch, "")   # 只有电脑、没有本机沙箱：Router 不拿 quick_code（电脑可能要开机），Worker 照样有 run_code
    assert registry.available("quick_code", registry.ROUTER, {"scope": registry.ROUTER}) is None
    assert registry.available("run_code", registry.WORKER, {"scope": registry.WORKER})


def test_registered():
    builtin.load()
    rc, qc = registry.get("run_code"), registry.get("quick_code")
    assert rc.where == frozenset({registry.WORKER}) and qc.where == frozenset({registry.ROUTER})
    assert not rc.gated and not qc.gated
    for t in (rc, qc):
        assert "沙箱" in t.schema["function"]["description"] and "工作文件夹外改不了" in t.schema["function"]["description"]
    assert rc.progress({"code": "\n\nimport math\nprint(1)"}).endswith("import math")
