"""假模型：按剧本流式返回文字或工具调用，走真的 AsyncOpenAI 客户端（请求经 httpx.MockTransport 送到这里）。

剧本按角色排队：router（还能细到某个 Bot：router:名字）、worker、reviewer，其他的归 other。
每次请求取队首的一条；队空了 router 回“好的”，别的回“完成”。一条可以是：
- 字符串：直接回答；
- Call / [Call, ...]：调用工具；
- 函数 (body) -> 上面两种之一：按收到的请求临时决定。
"""
import json
import re
from dataclasses import dataclass, field

import httpx
from openai import AsyncOpenAI


@dataclass
class Call:
    name: str
    args: dict = field(default_factory=dict)


def _chunk(delta: dict, finish=None) -> dict:
    return {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": "fake",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(reply) -> bytes:
    if isinstance(reply, Call):
        reply = [reply]
    if isinstance(reply, str):
        events, finish = [_chunk({"role": "assistant", "content": reply})], "stop"
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
    """(角色, Bot 名字)。按系统提示词的开头认。"""
    sys = system_of(body)
    if m := re.match(r"你是「(.+?)」", sys):
        return "router", m.group(1)
    if sys.startswith("你是执行任务的 Worker"):
        return "worker", ""
    if sys.startswith("你是审稿人"):
        return "reviewer", ""
    return "other", ""


class FakeLLM:
    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []   # (角色, Bot 名字, 请求体)
        self.scripts: dict[str, list] = {}
        self.failures: list[int] = []   # 接下来的对话请求依次直接返回这些状态码（模拟服务那边出故障）
        self.failed = 0                 # 已经按 failures 报错了几次
        self.transport = httpx.MockTransport(self._handle)
        self.client = AsyncOpenAI(base_url="http://fake.llm/v1", api_key="test",
                                  http_client=httpx.AsyncClient(transport=self.transport))

    def script(self, who: str, *replies):
        """who：router、router:名字、worker、reviewer、other。"""
        self.scripts.setdefault(who, []).extend(replies)

    def fail(self, *codes: int):
        """接下来的几次对话请求依次返回这些状态码，不消耗剧本。"""
        self.failures.extend(codes)

    def bodies(self, role: str, name: str = "") -> list[dict]:
        return [b for r, n, b in self.requests if r == role and (not name or n == name)]

    def _next(self, role: str, name: str, body: dict):
        for key in (f"{role}:{name}", role):
            if self.scripts.get(key):
                reply = self.scripts[key].pop(0)
                return reply(body) if callable(reply) else reply
        return "好的" if role == "router" else "完成"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            if self.failures:
                self.failed += 1
                return httpx.Response(self.failures.pop(0), json={"error": {
                    "message": "Internal Server Error (request_id: fake-0000)", "type": "server_error"}})
            body = json.loads(request.content)
            role, name = role_of(body)
            self.requests.append((role, name, body))
            reply = self._next(role, name, body)
            if not body.get("tools") and not isinstance(reply, str):
                reply = "（没给工具，只能回答）"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(reply))
        if path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "fake", "object": "model"}]})
        return httpx.Response(404, json={"error": {"message": f"假模型没有 {path}"}})
