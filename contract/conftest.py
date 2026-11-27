"""接口测试：只通过 HTTP、WebSocket 和数据库文件和服务打交道，同一套测试跑 Python 版和 Rust 版。

    uv run pytest contract                 # Python 版
    OSA_IMPL=rust uv run pytest contract   # Rust 版（server/target/debug/osa-server，OSA_RUST_BIN 可以换）

每个测试文件起一个服务进程：临时数据目录、临时 config.yaml，模型指向假模型（fakellm_http.py），
应用里接一个假的 stdio 应用（fake_mcp.py）。测试各自建自己的 Bot，互不干扰。
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml
from websockets.sync.client import connect

from fakellm_http import FakeLLM

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
IMPL = os.environ.get("OSA_IMPL", "python")
RUST_BIN = os.environ.get("OSA_RUST_BIN", str(ROOT / "server" / "target" / "debug" / "osa-server"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_config(tmp: Path, llm_url: str, extra: dict | None = None) -> Path:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["providers"] = {"fake": {"base_url": llm_url, "api_key_env": "FAKE_LLM_KEY"}}
    cfg["roles"] = {r: {"provider": "fake", "model": "fake-model"} for r in ("router", "worker", "reviewer", "filter")}
    cfg["memory"]["db"] = "data/osa.db"
    cfg["computer"] = {**(cfg.get("computer") or {}), "enabled": False, "backend": "local"}
    cfg["triggers"] = []
    cfg["embedding"] = {"provider": "", "model": ""}
    cfg["pricing"]["source"] = "http://127.0.0.1:9/none.json"   # 不去 models.dev 取价格
    cfg["web"] = {"search": ["duckduckgo"], "fetch": ["direct"], "block": []}
    cfg["mcp_servers"] = {"demo": {"enabled": True, "command": sys.executable, "args": [str(HERE / "fake_mcp.py")],
                                   "confirm": "auto"}}
    for k, v in (extra or {}).items():
        cfg[k] = v
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    return p


class Server:
    """一个跑着的服务进程。"""

    def __init__(self, llm: FakeLLM, env: dict | None = None, config: dict | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix=f"osa-contract-{IMPL}-"))
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = make_config(self.tmp, llm.url, config)
        e = {k: v for k, v in os.environ.items()
             if k not in ("OSA_PASSWORD", "OSA_VAULT_KEY", "INSTABOX_API_KEY", "OSA_WEBHOOK_TOKEN", "DEEPSEEK_API_KEY",
                          "OSA_ALLOWED_HOSTS", "OSA_DESKTOP", "OSA_PARENT_PID", "OSA_DOCS_URL", "OSA_DOCS_SECRET", "OSA_DOCS_BIN")}
        e |= {"OSA_CONFIG": str(cfg), "OSA_DATA_DIR": str(self.data), "FAKE_LLM_KEY": "test", "NO_PROXY": "*",
              "no_proxy": "*", "TZ": "Asia/Shanghai",
              "OSA_DOCS": "off"}   # 文档（Open Agent Docs）只在 test_docs.py 里开：本机 PATH 上有 open-agent-docs 时也不自己起
        e |= env or {}
        if IMPL == "rust":
            cmd = [RUST_BIN, "--host", "127.0.0.1", "--port", str(self.port)]
            e["OSA_ROOT"] = str(ROOT)
        else:
            cmd = [sys.executable, "-m", "uvicorn", "osa.server:app", "--host", "127.0.0.1", "--port", str(self.port)]
        self._cmd, self._env = cmd, e   # restart() 用
        self.log = open(self.tmp / "server.log", "w")
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=e, stdout=self.log, stderr=subprocess.STDOUT)
        self.http = httpx.Client(base_url=self.url, timeout=20, trust_env=False)
        self._wait_up()

    def _wait_up(self):
        end = time.time() + 30
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务没起来：\n{(self.tmp / 'server.log').read_text()[-3000:]}")
            try:
                if self.http.get("/api/build").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > end:
                raise RuntimeError("30 秒内服务没起来")
            time.sleep(0.05)

    def restart(self, kill: bool = False, timeout: float = 60) -> float:
        """停掉再起来，数据目录、端口、配置都不变。kill=True 是强杀（SIGKILL），否则正常停（SIGTERM）。返回停下来用了几秒。"""
        t0 = time.time()
        if kill:
            self.proc.kill()
        else:
            self.proc.terminate()
        self.proc.wait(timeout)
        took = time.time() - t0
        self.log.close()
        self.log = open(self.tmp / "server.log", "a")
        self.proc = subprocess.Popen(self._cmd, cwd=ROOT, env=self._env, stdout=self.log, stderr=subprocess.STDOUT)
        self._wait_up()
        return took

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.http.close()
        self.log.close()

    def logtail(self, n: int = 3000) -> str:
        return (self.tmp / "server.log").read_text()[-n:]

    # ---- 方便的调用 ----
    def get(self, path: str, **kw):
        r = self.http.get(path, **kw)
        assert r.status_code < 400, f"GET {path} → {r.status_code} {r.text[:500]}"
        return r.json()

    def post(self, path: str, body=None, **kw):
        r = self.http.post(path, json=body if body is not None else {}, **kw)
        assert r.status_code < 400, f"POST {path} → {r.status_code} {r.text[:500]}"
        return r.json()

    def put(self, path: str, body=None, **kw):
        r = self.http.put(path, json=body if body is not None else {}, **kw)
        assert r.status_code < 400, f"PUT {path} → {r.status_code} {r.text[:500]}"
        return r.json()

    def patch(self, path: str, body=None, **kw):
        r = self.http.patch(path, json=body if body is not None else {}, **kw)
        assert r.status_code < 400, f"PATCH {path} → {r.status_code} {r.text[:500]}"
        return r.json()

    def delete(self, path: str, **kw):
        r = self.http.delete(path, **kw)
        assert r.status_code < 400, f"DELETE {path} → {r.status_code} {r.text[:500]}"
        return r.json()

    def db(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{self.data / 'osa.db'}?mode=ro", uri=True, timeout=20)   # 机器忙的时候服务写得慢，5 秒不够（CI 的容器里撞到过 database is locked）
        return c

    def messages(self, chat_id: int) -> list[dict]:
        with self.db() as c:
            rows = c.execute("select id, role, content, meta, bot_id from messages where chat_id=? order by id",
                             (chat_id,)).fetchall()
        return [{"id": i, "role": r, "content": t, "meta": json.loads(m or "{}"), "bot_id": b} for i, r, t, m, b in rows]

    def find(self, chat_id: int, **want) -> list[dict]:
        out = []
        for m in self.messages(chat_id):
            if "role" in want and m["role"] != want["role"]:
                continue
            if "kind" in want and m["meta"].get("kind") != want["kind"]:
                continue
            if "bot_id" in want and m["bot_id"] != want["bot_id"]:
                continue
            if "text" in want and want["text"] not in m["content"]:
                continue
            out.append(m)
        return out

    def make_bot(self, name: str, instructions: str = "") -> dict:
        return self.post("/api/bots", {"name": name, "instructions": instructions})


class Socket:
    """一个打开着的网页：收着服务端推来的事件，能发消息、点确认。"""

    def __init__(self, server: Server):
        self.ws = connect(server.url.replace("http", "ws") + "/ws", open_timeout=10, proxy=None)  # noqa
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"type": "visibility", "visible": True})   # 正被看着：不发手机推送

    def _read(self):
        try:
            for raw in self.ws:
                with self.lock:
                    self.events.append(json.loads(raw))
        except Exception:
            pass
        self.alive = False

    def send(self, data: dict):
        self.ws.send(json.dumps(data, ensure_ascii=False))

    def say(self, chat_id: int, text: str, **kw):
        self.send({"type": "send", "chat_id": chat_id, "text": text, **kw})

    def of(self, type_: str) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e.get("type") == type_]

    def close(self):
        self.ws.close()


def until(pred, timeout: float = 10, what: str = "条件"):
    end = time.time() + timeout
    while True:
        if v := pred():
            return v
        if time.time() > end:
            raise AssertionError(f"{timeout} 秒内没等到：{what}")
        time.sleep(0.03)


def idle(server: Server, timeout: float = 10):
    """等所有任务都结束（没有 running 的任务）。"""
    def done():
        with server.db() as c:
            return not c.execute("select 1 from messages where role='task' and json_extract(meta,'$.kind')='dispatch' "
                                 "and json_extract(meta,'$.status')='running' limit 1").fetchone()
    until(done, timeout, "任务都结束")


@pytest.fixture(scope="session")
def llm():
    return FakeLLM()


@pytest.fixture(scope="module")
def srv(llm):
    s = Server(llm)
    yield s
    s.stop()


@pytest.fixture
def fake(llm):
    llm.reset()
    yield llm
    llm.reset()


@pytest.fixture
def ws(srv):
    s = Socket(srv)
    yield s
    s.close()
