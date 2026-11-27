"""电脑：右栏的电脑面板、接手 / 交还、资料库里电脑里的文件、send_file、电脑里跑代码和终端、浏览器、VNC 画面，Instabox 的机器。

Docker 的电脑用真的 osa-computer:2 镜像（没装 Docker、没有这个镜像就跳过，测试里不构建镜像）：测试自己起一个容器，
再在本机起一个假的 computerd（unix socket，把请求转成 docker exec），两个版本都按 Docker Compose 部署的办法
（OSA_COMPUTER_SOCKET）连它，和 lsp 上开了电脑容器时走的是同一条路。容器名带随机后缀，不碰你自己的 osa-computer。
Instabox 用测试里的假 API（INSTABOX_API_URL），不碰真的账号。
"""
import base64
import json
import os
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from websockets.sync.client import connect

from conftest import Server, Socket, idle, until
from fakellm_http import Call

IMAGE = "osa-computer:2"


def _png(w: int = 8, h: int = 6) -> bytes:
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))

    def chunk(t, d):
        import struct
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    import struct
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ---------- 没开电脑 ----------
def test_panel_when_disabled(srv):
    bot = srv.get("/api/bootstrap")["bots"][0]
    st = srv.get(f"/api/bots/{bot['id']}/computer")
    for k in ("bot_id", "backend", "controller", "width", "height", "parallel", "users", "waiting", "watch", "exclusive",
              "workspace", "state", "detail"):
        assert k in st, k
    assert st["state"] == "none" and st["controller"] == "bot" and st["users"] == []
    assert srv.get("/api/computer/machines") == {"backend": "local", "machines": [], "fixed": False}
    assert srv.http.post("/api/computer/use", json={"machine_id": "m1"}).status_code == 400
    assert srv.http.post(f"/api/bots/{bot['id']}/computer/start").status_code == 400   # computer.enabled: false
    assert srv.http.post(f"/api/bots/{bot['id']}/computer/nope").status_code == 404
    assert srv.http.get("/api/bots/999999/computer").status_code == 404
    r = srv.get("/api/files")
    assert r["running"] is False and r["root"] == "/data/workspace"


# ---------- 真的 Docker 电脑 ----------
VNC_RELAY = r"""
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


def _docker_ok() -> str:
    if not shutil.which("docker"):
        return "没装 docker"
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return "" if r.returncode == 0 else f"没有 {IMAGE} 镜像（测试里不构建）"


class FakeComputerd:
    """本机上的假 computerd：协议和 osa/computer/image/computerd.py 一样，命令转给容器（docker exec）。"""

    def __init__(self, container: str):
        self.container = container
        self.dir = tempfile.mkdtemp(prefix="osapc-", dir="/tmp")
        self.path = os.path.join(self.dir, "c.sock")
        outer = self

        class H(socketserver.StreamRequestHandler):
            def reply(self, obj):
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()

            def handle(self):
                line = self.rfile.readline(64 << 20)
                if not line:
                    return
                req = json.loads(line)
                op = req.get("op")
                if op == "ready":
                    r = subprocess.run(["docker", "exec", outer.container, "sh", "-c", "DISPLAY=:1 xdotool getdisplaygeometry"],
                                       capture_output=True)
                    return self.reply({"ok": r.returncode == 0})
                if op == "exec":
                    t = max(1, min(3600, float(req.get("timeout") or 120)))
                    try:
                        r = subprocess.run(["docker", "exec", "-i", "-w", "/data/workspace", outer.container, "bash", "-lc",
                                            str(req.get("cmd") or "")], input=(req.get("stdin") or "").encode(),
                                           capture_output=True, timeout=t)
                        return self.reply({"exit_code": r.returncode, "stdout": r.stdout.decode(errors="replace"),
                                           "stderr": r.stderr.decode(errors="replace"), "timed_out": False})
                    except subprocess.TimeoutExpired:
                        return self.reply({"exit_code": 124, "stdout": "", "stderr": "超时", "timed_out": True})
                if op == "vnc":
                    p = subprocess.Popen(["docker", "exec", "-i", outer.container, "python3", "-c", VNC_RELAY],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                    self.reply({"ok": True})
                    conn = self.connection

                    def up():
                        try:
                            while d := conn.recv(65536):
                                p.stdin.write(d)
                                p.stdin.flush()
                        except OSError:
                            pass
                        try:
                            p.stdin.close()
                        except OSError:
                            pass
                    threading.Thread(target=up, daemon=True).start()
                    try:
                        while d := p.stdout.read1(65536):
                            conn.sendall(d)
                    except OSError:
                        pass
                    p.kill()
                    return
                self.reply({"error": f"不认识的操作：{op}"})

        class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.srv = S(self.path, H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture(scope="module")
def box():
    why = _docker_ok()
    if why:
        pytest.skip(why)
    name = f"osa-test-pc-{uuid.uuid4().hex[:8]}"
    arch = subprocess.run(["docker", "info", "--format", "{{.Architecture}}"], capture_output=True, text=True).stdout.strip()
    plat = "linux/" + {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}.get(arch, "amd64")
    subprocess.run(["docker", "run", "-d", "--platform", plat, "--name", name, "--shm-size=1g", "-e", "SCREEN_W=1280", "-e", "SCREEN_H=800",
                    "--label", "osa=test", IMAGE], check=True, capture_output=True)
    try:
        end = time.time() + 60
        while subprocess.run(["docker", "exec", name, "sh", "-c", "DISPLAY=:1 xdotool getdisplaygeometry"],
                             capture_output=True).returncode:
            if time.time() > end:
                pytest.skip("测试用的电脑容器 60 秒内没起来")
            time.sleep(0.5)
        d = FakeComputerd(name)
        yield d
        d.stop()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def pcsrv(llm, box):
    s = Server(llm, env={"OSA_COMPUTER_SOCKET": box.path},
               config={"computer": {"enabled": True, "backend": "local", "parallel": 2, "width": 1280, "height": 800,
                                    "idle_pause_s": 900}})
    yield s
    s.stop()


def _bot(s):
    return s.get("/api/bootstrap")["bots"][0]


def test_docker_panel_and_control(pcsrv):
    bot = _bot(pcsrv)
    st = pcsrv.get(f"/api/bots/{bot['id']}/computer")
    assert st["backend"] == "local" and st["state"] == "running" and st["parallel"] == 2 and st["workspace"] == "/data/workspace"
    ws = Socket(pcsrv)
    try:
        st = pcsrv.post(f"/api/bots/{bot['id']}/computer/takeover")
        assert st["controller"] == "human"
        assert until(lambda: [e for e in ws.of("computer") if e["computer"]["controller"] == "human"], what="接手的推送")
        assert pcsrv.post(f"/api/bots/{bot['id']}/computer/type", {"text": "hi", "key": "Escape"}) == {"ok": True}
        st = pcsrv.post(f"/api/bots/{bot['id']}/computer/handback")
        assert st["controller"] == "bot"
        r = pcsrv.http.post(f"/api/bots/{bot['id']}/computer/type", json={"text": "x"})
        assert r.status_code == 400
    finally:
        ws.close()
    assert pcsrv.http.post("/api/computer/watch", json={"key": "nobody"}).status_code == 404
    assert pcsrv.post("/api/computer/watch", {"key": ""})["watch"] is None


def test_docker_files(pcsrv):
    r = pcsrv.http.put("/api/files/content", params={"dir": "t", "name": "a.txt"}, content="你好，电脑".encode())
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "/data/workspace/t/a.txt"
    r2 = pcsrv.http.put("/api/files/content", params={"dir": "t", "name": "a.txt"}, content=b"again").json()
    assert r2["name"] == "a (2).txt"
    lst = pcsrv.get("/api/files", params={"path": "t"})
    assert lst["running"] and [e["name"] for e in lst["entries"]] == ["a (2).txt", "a.txt"]
    d = pcsrv.http.get("/api/files/download", params={"path": "/data/workspace/t/a.txt"})
    assert d.status_code == 200 and d.content == "你好，电脑".encode() and d.headers["accept-ranges"] == "bytes"
    assert d.headers["content-disposition"].startswith("inline")
    part = pcsrv.http.get("/api/files/download", params={"path": "t/a.txt"}, headers={"range": "bytes=0-2"})
    assert part.status_code == 206 and part.content == "你".encode() and part.headers["content-range"].endswith("/15")
    assert pcsrv.http.get("/api/files/download", params={"path": "t/a.txt"}, headers={"range": "bytes=99-"}).status_code == 416
    assert pcsrv.http.get("/api/files/download", params={"path": "t/nope.txt"}).status_code == 404
    assert pcsrv.http.get("/api/files", params={"path": "/etc"}).status_code == 400
    pcsrv.post("/api/files/pin", {"path": "t/a.txt", "on": True})
    assert any(p["path"] == "/data/workspace/t/a.txt" for p in pcsrv.get("/api/files")["pins"])
    assert pcsrv.delete("/api/files", params={"path": "t"}) == {"ok": True}
    assert pcsrv.http.delete("/api/files", params={"path": "/data/workspace"}).status_code == 400
    assert pcsrv.get("/api/files/pin" if False else "/api/files")["pins"] == []


def test_docker_worker_tools(pcsrv, fake):
    png = _png()
    pcsrv.http.put("/api/files/content", params={"dir": "img", "name": "p.png"}, content=png)
    a = pcsrv.make_bot("PcUser")
    ws = Socket(pcsrv)
    try:
        fake.script("router:PcUser", Call("dispatch", {"goal": "用电脑做几件事"}), "好")
        fake.script("worker", Call("computer_shell", {"cmd": "echo hi-from-box && pwd"}),
                    Call("run_code", {"lang": "python", "code": "print(6*7)"}),
                    Call("computer_write_file", {"path": "/data/workspace/w.txt", "content": "写进去"}),
                    Call("computer_read_file", {"path": "/data/workspace/w.txt"}),
                    Call("send_file", {"path": "img/p.png"}),
                    "都做完了")
        ws.say(a["chat_id"], "用电脑")
        res = until(lambda: pcsrv.find(a["chat_id"], kind="result"), 120, "任务结果")[0]
        outs = [m["content"] for b in fake.bodies("worker") for m in b["messages"] if m["role"] == "tool"]
        outs = list(dict.fromkeys(outs))
        assert outs[0].startswith("退出码 0\nhi-from-box\n/data/workspace"), outs[0]
        assert outs[1].startswith("退出码 0\n42"), outs[1]
        assert outs[2] == "已写入 /data/workspace/w.txt（3 字）"
        assert outs[3] == "写进去"
        assert outs[4] == "已发给用户：p.png（图片已经显示在任务结果下面）。回复里不用再贴链接。"
        att = res["meta"]["attachments"][0]
        assert att["name"] == "p.png" and att["path"] == "/data/workspace/img/p.png" and att["mime"] == "image/png" and att["w"] == 8
        sys_prompt = fake.bodies("worker")[0]["messages"][0]["content"]
        assert "你有一台电脑（Linux + Chromium 浏览器 + 终端" in sys_prompt
        names = [t["function"]["name"] for t in fake.bodies("worker")[0]["tools"]]
        assert "computer_shell" in names and "browser_open" in names and "run_code" in names
        idle(pcsrv)
    finally:
        ws.close()


def test_docker_browser(pcsrv, fake):
    a = pcsrv.make_bot("Browsy")
    pcsrv.http.put("/api/files/content", params={"dir": "site", "name": "index.html"},
                   content="<html><head><meta charset='utf-8'><title>测试页</title></head><body><h1>你好</h1><a href='/b.html'>下一页</a>"
                           "<input name='q'></body></html>".encode())
    ws = Socket(pcsrv)
    try:
        fake.script("router:Browsy", Call("dispatch", {"goal": "看网页"}), "好")
        fake.script("worker",
                    Call("computer_shell", {"cmd": "cd /data/workspace/site && (nohup python3 -m http.server 8799 >/dev/null 2>&1 &) ; sleep 1; echo ok"}),
                    Call("browser_open", {"url": "http://127.0.0.1:8799/index.html"}),
                    Call("computer_screenshot", {}),
                    "看完了")
        ws.say(a["chat_id"], "开网页")
        until(lambda: pcsrv.find(a["chat_id"], kind="result"), 180, "任务结果")
        bodies = fake.bodies("worker")
        outs = [m["content"] for b in bodies for m in b["messages"] if m["role"] == "tool"]
        page = next(o for o in outs if o.startswith("当前页面"))
        assert "测试页" in page and "下一页" in page, page
        shot = next(o for o in outs if o.startswith("这是浏览器里当前页面的截图"))
        assert shot
        last = bodies[-1]["messages"][-1]
        assert last["role"] == "user" and last["content"][0]["text"].startswith("（系统附图：当前页面截图，")
        st = pcsrv.get(f"/api/bots/{a['id']}/computer")
        assert st["users"] == []   # 任务结束就让出电脑
        idle(pcsrv)
    finally:
        ws.close()


def test_docker_look_and_vnc(pcsrv, fake):
    a = pcsrv.make_bot("Looker")
    fake.script("router:Looker", Call("look_at_screen", {}), "看到了")
    ws = Socket(pcsrv)
    try:
        ws.say(a["chat_id"], "屏幕上是什么")
        until(lambda: pcsrv.find(a["chat_id"], role="assistant"), 60, "回复")
        msgs = fake.bodies("router", "Looker")[-1]["messages"]
        tool = next(m for m in msgs if m["role"] == "tool")
        assert tool["content"].startswith("截图见下一条消息。当前窗口：") and "电脑空闲。" in tool["content"]
        pic = msgs[-1]
        assert pic["content"][0]["text"].startswith("（系统附图：电脑的整个屏幕，1280×800，只用来看，不能按这张图的坐标点击）")
    finally:
        ws.close()
    v = connect(pcsrv.url.replace("http", "ws") + f"/api/bots/{a['id']}/computer/vnc", subprotocols=["binary"], proxy=None)
    try:
        hello = v.recv(timeout=15)
        assert bytes(hello).startswith(b"RFB 003.")
    finally:
        v.close()


@pytest.mark.skip(reason="停用：新功能只做在 Rust 版，两个版本的提示词和工具不再一样，见 contract/test_parity.py 开头")
def test_docker_parity(box):
    """开着电脑时两个版本发给模型的请求也逐字一样（Worker 的电脑说明、Router 的能力清单、电脑工具的 schema）。"""
    import test_parity
    if not os.path.exists(test_parity.RUST_BIN):
        pytest.skip("没有构建 Rust 版")

    def scenario(srv, llm, ws):
        a = srv.make_bot("PcParity")
        llm.script("router:PcParity", Call("dispatch", {"goal": "看看电脑"}), "好")
        llm.script("worker", Call("computer_shell", {"cmd": "echo same"}), "完成")
        ws.say(a["chat_id"], "看看电脑")
        until(lambda: srv.find(a["chat_id"], kind="result"), 60, "任务结果")
    test_parity.compare(scenario, env={"OSA_COMPUTER_SOCKET": box.path},
                        config={"computer": {"enabled": True, "backend": "local", "parallel": 2}})


# ---------- Instabox（假的 API） ----------
class FakeInstabox:
    def __init__(self):
        self.machines = {"m_ours": {"id": "m_ours", "name": "osa-computer", "state": "suspended", "template": "agent-browser",
                                    "createdAt": "2026-11-01T00:00:00Z", "metadata": {"app": "open-super-agent"}},
                         "m_other": {"id": "m_other", "name": "other", "state": "running", "template": "x",
                                     "createdAt": "2026-11-02T00:00:00Z", "metadata": {}},
                         "m_gone": {"id": "m_gone", "name": "gone", "state": "deleted", "metadata": {}}}
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers.get("x-api-key")))
                if self.headers.get("x-api-key") not in ("good-key", "env-key"):
                    return self._send(401, {"error": {"code": "unauthorized", "message": "bad key"}})
                if self.path == "/v1/machines":
                    return self._send(200, {"machines": list(fake.machines.values())})
                mid = self.path.rsplit("/", 1)[-1]
                if self.path.startswith("/v1/machines/") and mid in fake.machines:
                    return self._send(200, fake.machines[mid])
                self._send(404, {"error": {"code": "not_found", "message": "no"}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def ibox(llm):
    f = FakeInstabox()
    s = Server(llm, env={"INSTABOX_API_URL": f.url, "INSTABOX_API_KEY": "env-key"},
               config={"computer": {"enabled": True, "backend": "instabox", "parallel": 3}})
    yield s, f
    s.stop()
    f.httpd.shutdown()


def test_instabox_machines(ibox):
    s, f = ibox
    bot = _bot(s)
    st = s.get(f"/api/bots/{bot['id']}/computer")
    assert st["backend"] == "instabox" and st["state"] == "none"
    m = s.get("/api/computer/machines")
    assert m["backend"] == "instabox" and m["fixed"] is False
    assert [x["id"] for x in m["machines"]] == ["m_ours", "m_other"]
    assert m["machines"][0] == {"id": "m_ours", "name": "osa-computer", "state": "paused", "template": "agent-browser",
                                "created": "2026-11-01T00:00:00Z", "ours": True, "current": False}
    assert s.http.post("/api/computer/use", json={"machine_id": "m_nope"}).status_code == 404
    st = s.post("/api/computer/use", {"machine_id": "m_other"})
    assert st["state"] == "running" and st["machine_id"] == "m_other"
    assert [x["current"] for x in s.get("/api/computer/machines")["machines"]] == [False, True]
    assert any(r[2] == "env-key" for r in f.requests)


def test_instabox_key(ibox):
    s, f = ibox
    r = s.http.put("/api/computer/instabox-key", json={"key": "wrong"})
    assert r.status_code == 400 and "401" in r.json()["detail"]
    out = s.put("/api/computer/instabox-key", {"key": "good-key"})
    assert next(x for x in out["computer_backends"] if x["id"] == "instabox")["key"] == "saved"
    # 换了 key：忘掉之前选的机器，那个账号里有以前给这里建的机器就接着用
    assert [x["current"] for x in s.get("/api/computer/machines")["machines"]] == [True, False]
    out = s.put("/api/computer/instabox-key", {"key": ""})
    assert next(x for x in out["computer_backends"] if x["id"] == "instabox")["key"] == "env"
