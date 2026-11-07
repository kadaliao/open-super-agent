"""头像图片：上传、给 Bot 和群聊用上、没人用的清掉、AI 生成（硅基流动和标准 OpenAI 两种返回格式）。"""
import base64
import json
import time
import httpx
import pytest
from fastapi import HTTPException
from osa import avatars, bots, memory, providers, server
from osa.config import CFG

PNG = providers._png(4, 4, (255, 0, 0))


def data_url(b: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(b).decode()


async def test_upload_and_serve():
    url = (await server.upload_avatar({"data": data_url(PNG)}))["image"]
    assert avatars.URL.match(url)
    r = await server.avatar_file(url.rsplit("/", 1)[1])
    assert r.body == PNG and r.media_type == "image/png" and "immutable" in r.headers["cache-control"]
    assert (await server.upload_avatar({"data": data_url(PNG)}))["image"] == url   # 同一张图只存一份

    for bad in ("", data_url(b"<svg onload=alert(1)></svg>", "image/svg+xml"), data_url(b"\x89PNG\r\n\x1a\n" + b"0" * avatars.MAX_BYTES)):
        with pytest.raises(HTTPException):
            await server.upload_avatar({"data": bad})
    with pytest.raises(HTTPException):
        await server.avatar_file("0" * 24)


async def test_bot_and_group_use_image(events, make_bot):
    url = avatars.save(data_url(PNG))
    a, b = make_bot(), make_bot()
    got = await server.update_bot(a["id"], {"avatar": {"shape": "star", "color": "#ff0000", "image": url}})
    assert got["avatar"] == {"shape": "star", "color": "#ff0000", "image": url}
    with pytest.raises(HTTPException):   # 不是传上来的图片：不收
        await server.update_bot(a["id"], {"avatar": {"shape": "star", "color": "#ff0000", "image": "https://evil.example/x.png"}})
    # 不认识的形状、颜色换成默认的
    weird = bots.update(b["id"], avatar={"shape": "nope", "color": "red; x"})["avatar"]
    assert weird["shape"] in bots.SHAPES and weird["color"] in bots.COLORS and "image" not in weird

    g = bots.create_group([a["id"], b["id"]])
    assert g["image"] is None
    assert (await server.update_group(g["id"], {"image": url}))["image"] == url
    assert (await server.update_group(g["id"], {"title": "改个名"}))["image"] == url   # 不带 image：不动
    assert (await server.update_group(g["id"], {"image": ""}))["image"] is None
    with pytest.raises(HTTPException):
        await server.update_group(g["id"], {"image": "/avatars/" + "f" * 24})


async def test_gc_keeps_used_and_fresh(make_bot):
    old_used, old_free, fresh = (avatars.save(data_url(providers._png(2, 2, c))) for c in ((1, 1, 1), (2, 2, 2), (3, 3, 3)))
    bots.update(make_bot()["id"], avatar={"shape": "star", "color": "#ff0000", "image": old_used})
    for u in (old_used, old_free):
        memory.conn.execute("update avatars set created=? where id=?", (time.time() - avatars.KEEP_S - 10, u.rsplit("/", 1)[1]))
    avatars.gc()
    assert avatars.valid(old_used) and avatars.valid(fresh) and not avatars.valid(old_free)


@pytest.fixture
def image_providers(monkeypatch):
    monkeypatch.setitem(CFG["providers"], "sf", {"base_url": "https://api.siliconflow.cn/v1", "api_key_env": "OSA_TEST_SF"})
    monkeypatch.setitem(CFG["providers"], "oai", {"base_url": "https://img.example.com/v1", "api_key_env": "OSA_TEST_OAI"})
    monkeypatch.setenv("OSA_TEST_SF", "k1")
    monkeypatch.setenv("OSA_TEST_OAI", "k2")
    seen = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "api.siliconflow.cn":
            return httpx.Response(200, json={"images": [{"url": "https://files.example.com/a.png"}], "seed": 1})
        if req.url.host == "files.example.com":
            return httpx.Response(200, content=PNG)
        if req.url.host == "img.example.com":
            if json.loads(req.content)["model"].startswith("broken"):
                return httpx.Response(400, json={"error": {"message": "quota exceeded"}})
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        return httpx.Response(404)

    monkeypatch.setattr(avatars, "_TRANSPORT", httpx.MockTransport(handle))
    return seen


async def test_generate(image_providers):
    out = await server.generate_avatar({"prompt": "一只猫", "provider": "sf", "model": "Qwen/Qwen-Image"})
    assert out["data"] == data_url(PNG)
    body = json.loads(image_providers[0].content)
    assert body["image_size"] == "1328x1328" and "size" not in body
    assert image_providers[0].headers["authorization"] == "Bearer k1"

    out = await server.generate_avatar({"prompt": "一只猫", "provider": "oai", "model": "gpt-image-1"})
    assert out["data"] == data_url(PNG) and json.loads(image_providers[-1].content)["size"] == "1024x1024"

    with pytest.raises(HTTPException) as e:
        await server.generate_avatar({"prompt": "一只猫", "provider": "oai", "model": "broken-gpt-image"})
    assert "quota exceeded" in e.value.detail   # 服务的报错原样告诉用户
    with pytest.raises(HTTPException):   # 对话模型不拿来画图
        await server.generate_avatar({"prompt": "一只猫", "provider": "oai", "model": "gpt-5"})
    with pytest.raises(HTTPException):
        await server.generate_avatar({"prompt": " ", "provider": "sf", "model": "Kwai-Kolors/Kolors"})


async def test_models_list(image_providers, monkeypatch):
    lists = {"sf": ["Qwen/Qwen-Image", "Qwen/Qwen-Image-Edit", "Kwai-Kolors/Kolors", "deepseek-ai/DeepSeek-V3"], "oai": ["gpt-image-1", "gpt-5"]}

    async def fake_models(name, refresh=False):
        return {"ok": True, "models": lists.get(name, [])}
    monkeypatch.setattr(providers, "models", fake_models)
    memory.conn.execute("delete from settings where key in ('avatar_model', 'image_model')")

    r = await server.avatar_models()
    assert [(m["provider"], m["model"]) for m in r["models"]] == [("sf", "Kwai-Kolors/Kolors"), ("oai", "gpt-image-1"), ("sf", "Qwen/Qwen-Image")]
    assert [m["price"] for m in r["models"]] == [0, None, 0.3]   # 按张的价格：硅基流动写在 config.yaml 里，别家不知道
    assert r["default"]["model"] == "Kwai-Kolors/Kolors"

    await server.generate_avatar({"prompt": "一只猫", "provider": "oai", "model": "gpt-image-1"})   # 记住上次用的
    assert (await server.avatar_models())["default"]["model"] == "gpt-image-1"
