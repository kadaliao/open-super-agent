"""换电脑：Instabox 的 API key 能在界面里填（加密存在保险库），Docker 没装、没开时说清楚，换电脑时旧的那台暂停。
不连真的 Instabox，也不调真的 Docker。"""
import asyncio

import httpx
import pytest

from osa import computer, settings, vault
from osa.computer import instabox, local
from osa.computer.base import ComputerError
from osa.config import CFG
from osa.memory import conn


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.delenv("INSTABOX_API_KEY", raising=False)
    monkeypatch.delenv("OSA_DESKTOP", raising=False)
    monkeypatch.setitem(CFG.setdefault("computer", {}), "backend", "auto")
    monkeypatch.setattr(computer, "_computers", {})
    yield
    vault.delete(instabox.KEY)
    conn.execute("delete from computers")
    conn.commit()


def machine() -> str | None:
    r = conn.execute("select ref from computers where backend='instabox'").fetchone()
    return r[0] if r else None


def fake_instabox(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(instabox.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_saved_key_is_used_first(clean, monkeypatch):
    assert instabox.key_source() == "" and computer.backend_name() == "local"
    vault.put(instabox.KEY, "from-ui")
    assert instabox.key_source() == "saved"
    assert computer.backend_name() == "instabox"     # auto：有 key 就用云电脑
    monkeypatch.setenv("INSTABOX_API_KEY", "from-env")
    assert instabox._headers()["X-API-Key"] == "from-ui"
    assert next(b for b in computer.available() if b["id"] == "instabox") == {"id": "instabox", "ok": True, "key": "saved"}
    vault.delete(instabox.KEY)
    assert instabox.key_source() == "env" and instabox._headers()["X-API-Key"] == "from-env"


async def test_save_key_checks_it_and_reuses_our_machine(clean, monkeypatch):
    def handler(req: httpx.Request):
        if req.headers["X-API-Key"] == "bad":
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if req.headers["X-API-Key"] == "down":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"machines": [
            {"id": "m_other", "state": "running", "metadata": {}},
            {"id": "m_ours", "state": "suspended", "metadata": {"app": "open-super-agent"}}]})
    fake_instabox(monkeypatch, handler)
    conn.execute("insert or replace into computers values(0, 'instabox', 'm_old')")
    conn.commit()
    with pytest.raises(ComputerError, match="不认这个 API Key"):
        await instabox.save_key("bad")
    with pytest.raises(ComputerError, match="连不上 Instabox"):
        await instabox.save_key("down")
    assert vault.get(instabox.KEY) is None and machine() == "m_old"   # 不能用的 key 不存，也不动原来的机器
    await instabox.save_key("  good  ")
    assert vault.get(instabox.KEY) == "good"
    assert machine() == "m_ours"           # 换了账号：接着用那个账号里以前给这里建的机器
    await instabox.save_key("")            # 删掉界面里填的
    assert vault.get(instabox.KEY) is None and machine() is None


def test_choice_needs_key_and_docker(clean, monkeypatch):
    monkeypatch.setattr(computer.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="API Key"):
        settings.save({"computer_backend": "instabox"})
    with pytest.raises(ValueError, match="没装 Docker"):
        settings.save({"computer_backend": "local"})


async def test_docker_missing_or_stopped(monkeypatch):
    monkeypatch.setitem(CFG["computer"], "enabled", True)   # 测试环境默认关着电脑
    assert (await local._run("osa-no-such-command-xyz"))[0] == 127
    c = local.LocalComputer(0)

    def answer(code: int, err: str):
        async def run(*args, **kw):
            return code, "", err
        monkeypatch.setattr(local, "_run", run)
    answer(127, "这台电脑没装 Docker")
    assert await c.status() == {"state": "error", "detail": "这台电脑没装 Docker"}
    answer(1, "Cannot connect to the Docker daemon at unix:///Users/x/.docker/run/docker.sock. Is the docker daemon running?")
    st = await c.status()
    assert st["state"] == "error" and "Docker 没在运行" in st["detail"]
    with pytest.raises(ComputerError, match="Docker 没在运行"):
        await c._ensure_image()
    answer(1, "error: no such object: osa-computer")
    assert (await c.status())["state"] == "none"


async def test_first_build_shows_progress(monkeypatch):
    monkeypatch.setitem(CFG["computer"], "enabled", True)
    c = local.LocalComputer(0)
    go = asyncio.Event()
    calls = []

    async def run(*args, **kw):
        calls.append(args[1])
        if args[1:3] == ("image", "inspect"):
            return 1, "", "Error response from daemon: No such image: osa-computer:2"
        if args[1] == "build":
            await go.wait()
        return 0, "", ""
    monkeypatch.setattr(local, "_run", run)
    monkeypatch.setattr(local, "_platform", "linux/arm64")
    job = asyncio.create_task(c._ensure_image())
    await asyncio.sleep(0.01)
    st = await c.status()
    assert st["state"] == "starting" and "几分钟" in st["detail"]
    go.set()
    await job
    assert not c.building and "build" in calls


async def test_old_image_container_is_rebuilt(monkeypatch):
    """电脑镜像出了新版：停着（或暂停着）的旧容器删掉，按新镜像重建（/data 是挂进去的，文件和登录状态都在）；开着的不动。"""
    monkeypatch.setitem(CFG["computer"], "enabled", True)
    c = local.LocalComputer(0)
    calls, state = [], {"s": "exited"}

    async def run(*args, **kw):
        calls.append(args[1:3])
        if args[1:3] == ("inspect", "-f") and args[3] == "{{json .State}}":
            if state["s"] == "gone":
                return 1, "", "Error: No such object: osa-computer"
            return 0, '{"Status": "%s"}' % state["s"], ""
        if args[1:3] == ("inspect", "-f"):
            return 0, "osa-computer:1\n", ""
        if args[1] == "rm":
            state["s"] = "gone"
        if args[1] == "run":
            state["s"] = "running"
        return 0, "", ""
    monkeypatch.setattr(local, "_run", run)
    monkeypatch.setattr(local, "_platform", "linux/arm64")

    async def ready(cmd, timeout=120, stdin=None):
        return local.ExecResult(0, "1280 800", "")
    monkeypatch.setattr(c, "exec", ready)
    await c.start()
    assert ("rm", "-f") in calls and ("run", "-d") in calls and ("start", "osa-computer") not in calls
    calls.clear()
    await c.start()                                           # 已经开着：不查镜像、不重建
    assert calls == [("inspect", "-f")]


async def test_switching_pauses_the_old_computer(clean, monkeypatch):
    old = computer.get()
    assert old.backend == "local"
    paused = []

    async def pause():
        paused.append(old.backend)
    old.pause = pause
    vault.put(instabox.KEY, "k")              # auto 换成云电脑
    assert computer.get().backend == "instabox"
    await asyncio.sleep(0.01)
    assert paused == ["local"]


def test_choices_limit_the_picker(clean, monkeypatch):
    monkeypatch.setattr(computer, "host_allowed", lambda: True)   # 你自己的 Mac 上；服务器上见 test_computer_mac
    assert computer.choices() == ["host", "local", "instabox"]
    monkeypatch.setitem(CFG["computer"], "choices", ["instabox"])   # 服务器上不想跑 Docker
    assert computer.choices() == ["instabox"]
    assert settings.public()["computer_choices"] == ["instabox"]
    monkeypatch.setattr(computer.shutil, "which", lambda name: "/usr/bin/docker")
    with pytest.raises(ValueError, match="computer.choices"):
        settings.save({"computer_backend": "local"})
    settings.save({"computer_backend": "auto"})                     # 回到自动照常可以
