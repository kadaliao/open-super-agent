"""假模型，真的 HTTP 服务：服务进程（Python 版或 Rust 版）照常按 config.yaml 里的地址连过来。

剧本和 tests/fakellm.py 一样按角色排队（router、router:名字、worker、reviewer、other），按系统提示词的开头认角色。
一条可以是字符串（直接回答）、Call / [Call, ...]（调用工具），或者函数 (body) -> 上面两种之一。
"""
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Call:
    name: str
    args: dict = field(default_factory=dict)


def _chunk(delta: dict, finish=None) -> dict:
    return {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": "fake",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def sse(reply) -> bytes:
    if isinstance(reply, Call):
        reply = [reply]
    if isinstance(reply, str):
        # 分两块发：测流式拼接
        half = len(reply) // 2
        events = [_chunk({"role": "assistant", "content": ""})]
        events += [_chunk({"content": p}) for p in (reply[:half], reply[half:]) if p]
        finish = "stop"
    else:
        calls = [{"index": i, "id": f"call_{i}", "type": "function",
                  "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                 for i, c in enumerate(reply)]
        events, finish = [_chunk({"role": "assistant", "content": None, "tool_calls": calls})], "tool_calls"
    events.append(_chunk({}, finish))
    events.append({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": "fake", "choices": [],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
    return ("".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events) + "data: [DONE]\n\n").encode()


def system_of(body: dict) -> str:
    return next((m.get("content") or "" for m in body["messages"] if m["role"] == "system"), "")


def role_of(body: dict) -> tuple[str, str]:
    s = system_of(body)
    if m := re.match(r"你是「(.+?)」", s):
        return "router", m.group(1)
    if s.startswith("你是执行任务的 Worker"):
        return "worker", ""
    if s.startswith("你是审稿人"):
        return "reviewer", ""
    return "other", ""


class FakeLLM:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str, dict]] = []
        self.scripts: dict[str, list] = {}
        self.failures: list[int] = []
        self.failures_now: list[int] = []   # fail_now：带 x-should-retry: false，客户端不自己重试
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.scripts.clear()
            self.failures.clear()
            self.failures_now.clear()

    def script(self, who: str, *replies):
        with self.lock:
            self.scripts.setdefault(who, []).extend(replies)

    def fail(self, *codes: int):
        with self.lock:
            self.failures.extend(codes)

    def fail_now(self, *codes: int):
        """接下来几次请求按顺序返回这些状态码，并告诉客户端别自己重试（测任务自己的重试）。"""
        with self.lock:
            self.failures_now.extend(codes)

    def bodies(self, role: str, name: str = "") -> list[dict]:
        with self.lock:
            return [b for r, n, b in self.requests if r == role and (not name or n == name)]

    def _next(self, role: str, name: str, body: dict):
        """取剧本里的下一条（拿着锁调）。函数原样返回，由调用方放开锁以后再调：它可能要等别的请求。"""
        for key in (f"{role}:{name}", role):
            if self.scripts.get(key):
                return self.scripts[key].pop(0)
        return "好的" if role == "router" else "完成"

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._send(200, json.dumps({"object": "list", "data": [{"id": "fake", "object": "model"}]}).encode(),
                               "application/json")
                else:
                    self._send(404, b'{"error":{"message":"not found"}}', "application/json")

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n)
                if not self.path.endswith("/chat/completions"):
                    self._send(404, b'{"error":{"message":"not found"}}', "application/json")
                    return
                with fake.lock:
                    if fake.failures_now:
                        code = fake.failures_now.pop(0)
                        self._send(code, json.dumps({"error": {"message": "Service Unavailable (fake)", "type": "server_error"}}).encode(),
                                   "application/json", {"x-should-retry": "false"})
                        return
                    if fake.failures:
                        code = fake.failures.pop(0)
                        self._send(code, json.dumps({"error": {"message": "Internal Server Error (request_id: fake-0000)",
                                                               "type": "server_error"}}).encode(), "application/json")
                        return
                    body = json.loads(raw)
                    role, name = role_of(body)
                    fake.requests.append((role, name, body))
                    reply = fake._next(role, name, body)
                if callable(reply):
                    reply = reply(body)
                if not body.get("tools") and not isinstance(reply, str):
                    reply = "（没给工具，只能回答）"
                if not body.get("stream"):
                    text = reply if isinstance(reply, str) else ""
                    out = {"id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": "fake",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
                    self._send(200, json.dumps(out, ensure_ascii=False).encode(), "application/json")
                    return
                self._send(200, sse(reply), "text/event-stream")

        return H
