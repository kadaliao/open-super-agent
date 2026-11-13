"""头像图片：上传、给 Bot 和群聊用上、没人用的清掉、AI 生成（硅基流动和标准 OpenAI 两种返回格式）。"""
import base64
import json
import time
import httpx
import pytest
from fastapi import HTTPException
from osa import avatars, bots, generation, memory, providers, server
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

    monkeypatch.setattr(generation, "_TRANSPORT", httpx.MockTransport(handle))
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
    edit = await server.avatar_models()
    assert [(m["provider"], m["model"], m["refs"]) for m in edit["edit_models"]] == \
        [("oai", "gpt-image-1", 16), ("sf", "Qwen/Qwen-Image-Edit", 1), ("sf", "Kwai-Kolors/Kolors", 1)]
    assert edit["edit_default"]["model"] == "gpt-image-1"
    assert "Qwen/Qwen-Image-Edit" not in [m["model"] for m in edit["models"]]   # 只能改图的不拿来直接画
    avatars.choose("sf", "Qwen/Qwen-Image-Edit", edit=True)
    assert (await avatars.current(refs=1))["model"] == "Qwen/Qwen-Image-Edit"
    assert (await avatars.current(refs=2))["model"] == "gpt-image-1"   # 选的那个收不了两张，换能收的
    assert (await avatars.current(refs=17)) is None
    assert (await avatars.current())["model"] == "gpt-image-1"   # 直接画的不受影响
    memory.conn.execute("delete from settings where key='image_edit_model'")


def test_recipes(monkeypatch):
    """配方认模型：能收几张参考图、是不是只能改图；视频的首尾帧、参考视频、时长。"""
    monkeypatch.setitem(CFG["providers"], "sf", {"base_url": "https://api.siliconflow.cn/v1"})
    monkeypatch.setitem(CFG["providers"], "ark", {"base_url": "https://ark.cn-beijing.volces.com/api/v3"})
    monkeypatch.setitem(CFG["providers"], "oai", {"base_url": "https://api.openai.com/v1"})
    monkeypatch.setitem(CFG["providers"], "ds", {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    img = lambda p, m: (generation.caps("image", p, m).images, generation.caps("image", p, m).needs_ref)   # noqa: E731
    assert img("sf", "Kwai-Kolors/Kolors") == (1, False)
    assert img("sf", "Qwen/Qwen-Image") == (0, False)
    assert img("sf", "Qwen/Qwen-Image-Edit") == (1, True)
    assert img("sf", "Qwen/Qwen-Image-Edit-2509") == (3, True)
    assert img("sf", "black-forest-labs/FLUX.1-Kontext-pro") == (1, True)
    assert img("ark", "doubao-seedream-4-0-250828") == (10, False)
    assert img("ark", "doubao-seedream-3-0-t2i-250415") == (0, False)
    assert img("oai", "gpt-image-1.5") == (16, False)
    assert generation.recipe("image", "oai", "flux-kontext-pro") is None   # 别家的改图模型不知道怎么传，不算
    assert generation.recipe("image", "sf", "Wan-AI/Wan2.2-T2V-A14B") is None   # 视频模型不算画图的
    assert generation.recipe("image", "oai", "gpt-5") is None

    v = generation.caps("video", "ark", "doubao-seedance-2-0-260128")
    assert (v.frames, v.images, v.videos, v.seconds[0], v.seconds[-1]) == (2, 9, 3, 4, 15)
    v = generation.caps("video", "sf", "Wan-AI/Wan2.2-I2V-A14B")
    assert v.need == {"frames": 1} and v.seconds == (5,) and v.ratios == ("16:9", "9:16", "1:1")
    assert generation.recipe("video", "sf", "Wan-AI/Wan2.2-T2V-A14B")["name"] == "siliconflow-wan-t2v"
    assert generation.recipe("video", "ds", "wan2.7-r2v")["name"] == "dashscope-wan-r2v"
    assert generation.recipe("video", "ds", "wan2.2-i2v-plus")["name"] == "dashscope-wan-i2v"
    assert "wan2.7-t2v" in generation.offers("video", "ds") and not generation.offers("video", "oai")


def test_config_recipes_come_first(monkeypatch):
    """config.yaml 里写的配方排在默认的前面，同名的替掉默认的；prefer 也能改。"""
    monkeypatch.setitem(CFG["providers"], "sf", {"base_url": "https://api.siliconflow.cn/v1"})
    mine = {"name": "siliconflow-kolors", "kind": "image", "host": "siliconflow", "model": "kolors", "images": 2,
            "request": {"path": "/x", "json": {"model": "$model"}}, "result": "data.0.url"}
    monkeypatch.setitem(CFG, "generation", {"recipes": [mine], "prefer": {"image": ["z-image"]}})
    assert generation.caps("image", "sf", "Kwai-Kolors/Kolors").images == 2
    assert [r["name"] for r in generation.recipes()].count("siliconflow-kolors") == 1
    assert generation.rank("image", "Tongyi-MAI/Z-Image-Turbo") == 0 and generation.rank("image", "Kwai-Kolors/Kolors") == 1


def test_render_template():
    v = {"model": "m", "prompt": "p", "media": [{"url": "u1", "type": "image", "role": "first_frame"},
                                                 {"url": "u2", "type": "video", "role": "reference_video"}], "seconds": None}
    t = {"model": "$model", "duration": "$seconds", "content": [{"type": "text", "text": "$prompt"},
         {"$each": "media", "as": {"type": "${type}_url", "${type}_url": {"url": "$url"}, "role": "$role"}}], "path": "/t/${model}"}
    assert generation.render(t, v) == {"model": "m", "content": [
        {"type": "text", "text": "p"}, {"type": "image_url", "image_url": {"url": "u1"}, "role": "first_frame"},
        {"type": "video_url", "video_url": {"url": "u2"}, "role": "reference_video"}], "path": "/t/m"}
    assert generation.pick({"a": [{"b": 1}]}, "a.0.b") == 1 and generation.pick({"a": []}, "a.0.b") is None


async def test_draw_with_refs(monkeypatch):
    """各家的参考图怎么传：硅基流动 image / image2 / input_image，OpenAI 走 /images/edits 的 image[]，火山方舟 image 数组。"""
    monkeypatch.setitem(CFG["providers"], "sf", {"base_url": "https://api.siliconflow.cn/v1"})
    monkeypatch.setitem(CFG["providers"], "ark", {"base_url": "https://ark.cn-beijing.volces.com/api/v3"})
    monkeypatch.setitem(CFG["providers"], "oai", {"base_url": "https://api.openai.com/v1"})
    monkeypatch.setattr(providers, "usable", lambda name: True)
    monkeypatch.setattr(providers, "api_key", lambda name: "k")
    seen = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    monkeypatch.setattr(generation, "_TRANSPORT", httpx.MockTransport(handle))
    a, b = generation.Ref(PNG, "image/png", "a.png"), generation.Ref(b"\xff\xd8\xff\xe0jpeg", "image/jpeg", "b.jpg")

    assert (await generation.draw("换成夜景", "sf", "Qwen/Qwen-Image-Edit-2509", [a, b]))[0] == PNG
    body = json.loads(seen[-1].content)
    assert body["image"] == a.data_url() and body["image2"] == b.data_url() and "image3" not in body and "image_size" not in body
    await generation.draw("换成夜景", "sf", "black-forest-labs/FLUX.1-Kontext-pro", [a])
    body = json.loads(seen[-1].content)
    assert body["input_image"] == a.data_url() and "image" not in body
    await generation.draw("水彩风格", "sf", "Kwai-Kolors/Kolors", [a])
    body = json.loads(seen[-1].content)
    assert body["image"] == a.data_url() and body["image_size"] == "1024x1024"

    await generation.draw("放在一起", "oai", "gpt-image-1.5", [a, b])
    req = seen[-1]
    assert req.url.path == "/v1/images/edits" and req.headers["content-type"].startswith("multipart/form-data")
    assert req.content.count(b'name="image[]"') == 2 and b'filename="image2.jpg"' in req.content and PNG in req.content

    await generation.draw("放在一起", "ark", "doubao-seedream-4-0-250828", [a, b])
    assert json.loads(seen[-1].content)["image"] == [a.data_url(), b.data_url()]
    await generation.draw("改一下", "ark", "doubao-seedream-4-0-250828", [a])
    assert json.loads(seen[-1].content)["image"] == [a.data_url()]

    n = len(seen)
    with pytest.raises(ValueError, match="最多收 1 个参考图"):
        await generation.draw("x", "sf", "Kwai-Kolors/Kolors", [a, b])
    with pytest.raises(ValueError, match="不收参考图"):
        await generation.draw("x", "sf", "Qwen/Qwen-Image", [a])
    with pytest.raises(ValueError, match="只能照着原图生成"):
        await generation.draw("x", "sf", "Qwen/Qwen-Image-Edit", [])
    assert len(seen) == n   # 参数不对的不去调接口
