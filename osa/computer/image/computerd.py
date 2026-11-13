"""电脑容器里的小服务：用 Docker Compose 部署时，服务（osa 容器）不碰 Docker（不挂 docker.sock，那等于宿主机 root），
电脑是 compose 管的常驻容器，服务经两个容器共用的卷里的 unix socket 找它做事（osa/computer/local.py 的 SOCKET 模式）。
两个容器不在同一个网络里：电脑里跑的浏览器、命令连不到服务的端口。

一次连接一件事，先发一行 JSON：
- {"op": "ready"}：桌面起来没有 → {"ok": true/false}
- {"op": "exec", "cmd", "stdin", "timeout"}：在 /data/workspace 里用 bash -lc 执行 → {"exit_code", "stdout", "stderr", "timed_out"}
- {"op": "vnc"}：回 {"ok": true} 之后这条连接就是到桌面 VNC（127.0.0.1:5900）的字节流
只用标准库（镜像里只有系统自带的 python3）。
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading

PATH = os.environ.get("OSA_COMPUTER_SOCKET", "/run/osa-computer/computer.sock")
WORKSPACE = "/data/workspace"
VNC = ("127.0.0.1", 5900)
MAX_LINE = 64 << 20   # 一行请求最多这么大（写文件时内容在 stdin 里，base64 过）


def ready() -> bool:
    return subprocess.run(["xdotool", "getdisplaygeometry"], env={**os.environ, "DISPLAY": ":1"},
                          capture_output=True, timeout=5).returncode == 0


def run(cmd: str, stdin: str | None, timeout: float) -> dict:
    os.makedirs(WORKSPACE, exist_ok=True)
    p = subprocess.Popen(["bash", "-lc", cmd], cwd=WORKSPACE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate((stdin or "").encode(), timeout=timeout)
        return {"exit_code": p.returncode, "stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace"),
                "timed_out": False}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)   # 连它开的子进程一起停掉
        except OSError:
            pass
        p.wait()
        return {"exit_code": 124, "stdout": "", "stderr": "超时", "timed_out": True}


def splice(a: socket.socket, b: socket.socket):
    """两边互相转发，一边读到结尾就半关另一边。"""
    def pump(src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join()


class Handler(socketserver.StreamRequestHandler):
    def reply(self, obj: dict):
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        self.wfile.flush()

    def handle(self):
        line = self.rfile.readline(MAX_LINE)
        if not line:   # 服务只是来看看这里有没有在听（computer.docker_problem），连上就断开了
            return
        try:
            req = json.loads(line)
        except ValueError:
            return self.reply({"error": "bad request"})
        op = req.get("op")
        if op == "ready":
            return self.reply({"ok": ready()})
        if op == "exec":
            return self.reply(run(str(req.get("cmd") or ""), req.get("stdin"), max(1, min(3600, float(req.get("timeout") or 120)))))
        if op == "vnc":
            try:
                vnc = socket.create_connection(VNC, timeout=10)
            except OSError as e:
                return self.reply({"error": f"VNC 没有启动：{e}"})
            vnc.settimeout(None)
            self.reply({"ok": True})
            with vnc:
                splice(self.connection, vnc)
            return
        self.reply({"error": f"不认识的操作：{op}"})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(path: str = PATH) -> "Server":
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)   # 上次留下的
    except FileNotFoundError:
        pass
    srv = Server(path, Handler)
    os.chmod(path, 0o660)
    return srv


def main():
    with serve() as srv:
        print(f"computerd listening on {PATH}", file=sys.stderr, flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
