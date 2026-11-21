"""两套服务共用的 Magpie 协议测试：目录、设置选择、生成图片、查询和下载视频。"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import Server, until
from fakellm_http import Call
from test_wiring import PNG

DRAW = "upstream/brush"
EDIT = "another/brush"
VIDEO = "studio/reel"
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(100)


@pytest.fixture(scope="module")
def gateway():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body, status=200):
            self.send_response(status)
            if isinstance(body, bytes):
                self.send_header("Content-Type", "video/mp4")
            else:
                body = json.dumps(body).encode()
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer test":
                return self.respond({}, 401)
            if self.path == "/v1/models":
                rows = [{"id": "chat/flux", "magpie_label": "Chat"}]
                if self.headers.get("X-Magpie-Drawers") == "1":
                    for model, edits in [(DRAW, []), (EDIT, ["image-edit"])]:
                        rows.append({"id": model, "kind": "image", "magpie_label": model,
                                     "supported_endpoint_types": ["image-generation", *edits]})
                if self.headers.get("X-Magpie-Videomakers") == "1":
                    rows.append({"id": VIDEO, "kind": "video", "magpie_label": VIDEO, "supported_endpoint_types": ["openai-video"]})
                return self.respond({"data": rows})
            if self.path == "/v1/videos/video_123":
                return self.respond({"status": "completed"})
            if self.path == "/v1/videos/video_123/content":
                return self.respond(MP4)
            self.respond({}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers.get("Authorization") != "Bearer test":
                return self.respond({}, 401)
            if self.path == "/v1/images/generations" and body["model"] == DRAW and body["prompt"] == "画猫":
                return self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
            if self.path == "/v1/videos" and body == {"model": VIDEO, "prompt": "海浪"}:
                return self.respond({"id": "video_123", "status": "queued"})
            self.respond({}, 400)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{httpd.server_port}/v1"
        finally:
            httpd.shutdown()


@pytest.fixture(scope="module")
def srv(llm, gateway):
    # 服务名、地址和模型名都没有 Magpie 字样。原有 embedding 独立配置。
    s = Server(llm, config={"providers": {"fake": {"base_url": llm.url}, "gateway": {"base_url": gateway, "api_key_env": "FAKE_LLM_KEY"}},
                            "embedding": {"provider": "fake", "model": "emb-1"}})
    yield s
    s.stop()


def test_catalog_selection_and_image(srv):
    pick = {"provider": "gateway", "model": DRAW}
    settings = srv.put("/api/settings", {"image_model": pick})   # 冷启动，尚未访问模型列表
    assert settings["image_model"] == pick and settings["embedding"] == {"provider": "fake", "model": "emb-1"}
    assert srv.get("/api/providers/gateway/models")["models"] == ["chat/flux"]
    images = srv.get("/api/avatars/models")
    assert {m["model"] for m in images["models"]} == {DRAW, EDIT}
    assert [m["model"] for m in images["edit_models"]] == [EDIT]
    assert images["default"]["model"] == DRAW and images["default"]["price"] is None
    assert srv.http.put("/api/settings", json={"image_edit_model": pick}).status_code == 400
    assert srv.post("/api/avatars/generate", {**pick, "prompt": "画猫"})["data"] == "data:image/png;base64," + base64.b64encode(PNG).decode()


def test_video_in_chat(srv, fake, ws):
    assert [m["model"] for m in srv.get("/api/videos/models")["models"]] == [VIDEO]
    video = {"provider": "gateway", "model": VIDEO}
    assert srv.put("/api/settings", {"video_model": video})["video_model"] == video
    a = srv.make_bot("Filmer")
    fake.script("router:Filmer", Call("generate_video", {"prompt": "海浪"}), "好")
    ws.say(a["chat_id"], "生成一段海浪的视频")
    card = until(lambda: [e for e in ws.of("approval") if e.get("tool") == "generate_video" and e.get("status") == "pending"])
    ws.send({"type": "approve", "id": card[0]["id"], "ok": True})
    result = until(lambda: srv.find(a["chat_id"], kind="video"), timeout=20)[0]["meta"]
    assert result["provider"] == "gateway" and result["model"] == VIDEO
    mid = result["attachments"][0]["media"]
    assert srv.http.get(f"/api/media/{mid}").content == MP4
