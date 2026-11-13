"""生成视频：各家的请求按配方拼（全是假服务，不调真的接口）、对话里先问再生成、后台查进度、重启后接着查、参考视频的临时地址。"""
import json
import time
import httpx
import pytest
from conftest import find, settle, until
from fakellm import Call
from osa import approvals, bots, channels, generation, media, memory, providers, server, videos
from osa.config import CFG

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200
PNG = providers._png(16, 9, (10, 120, 200))


@pytest.fixture
def sfv(monkeypatch):
    """假的硅基流动：有 Wan 的文生视频和图生视频；提交后查一次“还在做”，第二次做好。"""
    monkeypatch.setitem(CFG["providers"], "sfv", {"base_url": "https://api.siliconflow.cn/v1", "api_key_env": "OSA_TEST_SFV"})
    monkeypatch.setenv("OSA_TEST_SFV", "k1")
    real = providers.models

    async def fake_models(name, refresh=False):
        if name == "sfv":
            return {"ok": True, "models": ["Wan-AI/Wan2.2-T2V-A14B", "Wan-AI/Wan2.2-I2V-A14B", "deepseek-ai/DeepSeek-V3"]}
        return await real(name, refresh)
    monkeypatch.setattr(providers, "models", fake_models)
    monkeypatch.setattr(videos, "POLL_S", (0, 0, 0))
    monkeypatch.setattr(bots, "MAX_BOTS", 10 ** 6)   # 整套测试前面建的 Bot 已经到上限了（同 test_unread）
    seen = {"submit": [], "status": 0}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/video/submit"):
            seen["submit"].append(json.loads(req.content))
            return httpx.Response(200, json={"requestId": f"r{len(seen['submit'])}"})
        if req.url.path.endswith("/video/status"):
            seen["status"] += 1
            if seen["status"] % 2:
                return httpx.Response(200, json={"status": "InProgress"})
            return httpx.Response(200, json={"status": "Succeed", "results": {"videos": [{"url": "https://files.example.com/v.mp4"}]}})
        if req.url.host == "files.example.com":
            return httpx.Response(200, content=MP4)
        return httpx.Response(404)
    monkeypatch.setattr(generation, "_TRANSPORT", httpx.MockTransport(handle))
    memory.conn.execute("delete from settings where key='video_model'")
    memory.conn.execute("delete from rules where pattern='generate_video'")
    memory.conn.commit()
    yield seen
    memory.conn.execute("delete from rules where pattern='generate_video'")
    memory.conn.commit()


def test_sniff_video():
    assert media.sniff(MP4) == "video/mp4" and media.sniff(b"\x1aE\xdf\xa3" + b"\x00" * 20) == "video/webm"
    with pytest.raises(ValueError):
        media.save(MP4, "a.mp4")   # 不说 video=True 就不收视频（上传图片的接口）
    assert media.save(MP4, "a.mp4", video=True)["mime"] == "video/mp4"


async def test_models_and_pick(sfv):
    d = await server.video_models()
    assert [m["model"] for m in d["models"]] == ["Wan-AI/Wan2.2-T2V-A14B", "Wan-AI/Wan2.2-I2V-A14B"]
    assert d["default"]["model"] == "Wan-AI/Wan2.2-T2V-A14B"   # 只能图生视频的不当默认
    frame = [generation.Ref(name="a.png", role="frame")]
    assert (await videos.pick(frame))["model"] == "Wan-AI/Wan2.2-I2V-A14B"   # 带首帧：换成收首帧的
    assert await videos.pick([generation.Ref(name="v.mp4", role="video")]) is None


async def test_text_to_video_in_chat(llm, events, make_bot, sfv):
    a = make_bot("Director")
    cid = a["chat_id"]
    llm.script("router:Director", Call("generate_video", {"prompt": "一只猫在海边奔跑", "seconds": 8, "ratio": "9:16"}), "确认一下")
    await server.safe_handle(bots.chat(cid), "生成一段猫在海边跑的视频")
    card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "generate_video"), None), what="确认卡片")
    assert "Wan-AI/Wan2.2-T2V-A14B" in card["args"] and "5 秒" in card["args"]   # 这个模型只能 5 秒
    assert not sfv["submit"]
    approvals.resolve(card["id"], True)
    vid = (await until(lambda: find(cid, kind="video"), what="生成好的视频", timeout=10))[0]
    att = vid["meta"]["attachments"][0]
    assert media.get(att["media"])[1] == MP4 and att["mime"] == "video/mp4"
    assert sfv["submit"][0] == {"model": "Wan-AI/Wan2.2-T2V-A14B", "prompt": "一只猫在海边奔跑", "image_size": "720x1280"}
    job = (await until(lambda: [x for x in find(cid, kind="video_job") if x["meta"]["status"] == "done"], what="标成做好"))[0]
    assert job["meta"]["job"]["id"] == "r1"
    assert any(e["meta"].get("status") == "done" for e in events.of("message_meta"))
    assert memory.conn.execute("select count(*) from llm_calls where role='video'").fetchone()[0] >= 1
    await settle()

    # 下一轮的上下文里有视频编号
    llm.script("router:Director", "好的")
    await server.safe_handle(bots.chat(cid), "刚才那段呢")
    await until(lambda: len(find(cid, role="assistant")) == 2, what="第二轮")
    ctx = [m["content"] for m in llm.bodies("router", "Director")[-1]["messages"] if isinstance(m["content"], str)]
    assert any("生成好了一段视频" in c and f"[视频] {att['media']}" in c for c in ctx)
    await settle()
    bots.delete(a["id"])   # 整套测试的 Bot 离上限（50 个）不远，用完删掉


async def test_image_to_video_uses_frame(llm, events, make_bot, sfv):
    a = make_bot("Director2")
    cid = a["chat_id"]
    pic = media.save(PNG, "poster.png")
    memory.conn.execute("insert into rules(pattern, action, created) values('generate_video', 'allow', ?)", (time.time(),))   # 总是允许
    memory.conn.commit()
    llm.script("router:Director2", Call("generate_video", {"prompt": "让海报动起来", "images": [pic["id"]]}), "开始了")
    await server.safe_handle(bots.chat(cid), "让这张海报动起来")
    await until(lambda: find(cid, kind="video"), what="视频", timeout=10)
    body = sfv["submit"][-1]
    assert body["model"] == "Wan-AI/Wan2.2-I2V-A14B" and body["image"].startswith("data:image/png;base64,")
    assert body["image_size"] == "1280x720"   # 没说比例：跟着首帧（16:9）
    tool = [m for m in llm.bodies("router", "Director2")[-1]["messages"] if m["role"] == "tool"][0]
    assert tool["content"].startswith("开始生成了")
    await settle()
    bots.delete(a["id"])


async def test_failure_and_no_model(llm, events, make_bot, sfv, monkeypatch):
    a = make_bot("Director3")
    cid = a["chat_id"]
    v = media.save(MP4, "ref.mp4", video=True)
    llm.script("router:Director3", Call("generate_video", {"prompt": "照这个动作拍", "videos": [v["id"]]}), "好")
    await server.safe_handle(bots.chat(cid), "照这个视频拍一段")
    await settle()
    tool = [m for m in llm.bodies("router", "Director3")[-1]["messages"] if m["role"] == "tool"][0]
    assert "都收不了这些参考素材" in tool["content"]   # 硅基流动的 Wan 不收参考视频

    async def none():
        return {"models": [], "default": None}
    monkeypatch.setattr(videos, "models", none)
    llm.script("router:Director3", Call("generate_video", {"prompt": "x"}), "好")
    await server.safe_handle(bots.chat(cid), "生成视频")
    await settle()
    tool = [m for m in llm.bodies("router", "Director3")[-1]["messages"] if m["role"] == "tool"][0]
    assert tool["content"] == videos.NO_MODEL
    bots.delete(a["id"])


async def test_resume_after_restart(make_bot, sfv):
    a = make_bot()
    cid = a["chat_id"]
    meta = {"kind": "video_job", "prompt": "重启前提交的", "provider": "sfv", "model": "Wan-AI/Wan2.2-T2V-A14B", "seconds": 5,
            "status": "running", "started": time.time(), "job": {"provider": "sfv", "model": "Wan-AI/Wan2.2-T2V-A14B", "id": "r9"}}
    running = memory.add("task", "[生成视频] 重启前提交的", cid, a["id"], **meta)
    lost = memory.add("task", "[生成视频] 没提交上", cid, a["id"], **(meta | {"job": None, "prompt": "没提交上"}))
    videos.resume()
    status = lambda mid: json.loads(memory.conn.execute("select meta from messages where id=?", (mid,)).fetchone()[0])["status"]   # noqa: E731
    await until(lambda: status(running) == "done", what="接着查到的视频", timeout=10)
    assert find(cid, kind="video") and status(lost) == "failed"
    assert not sfv["submit"]   # 不重新提交
    await settle()
    bots.delete(a["id"])


async def test_vendor_requests(monkeypatch):
    """火山方舟、百炼、智谱的请求按配方拼对（假服务，不调真的接口）。"""
    monkeypatch.setitem(CFG["providers"], "ark", {"base_url": "https://ark.cn-beijing.volces.com/api/v3"})
    monkeypatch.setitem(CFG["providers"], "ds", {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    monkeypatch.setitem(CFG["providers"], "zp", {"base_url": "https://open.bigmodel.cn/api/paas/v4"})
    monkeypatch.setattr(providers, "usable", lambda name: True)
    monkeypatch.setattr(providers, "api_key", lambda name: "k")
    seen = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        p = req.url.path
        if p.endswith("/contents/generations/tasks"):
            return httpx.Response(200, json={"id": "cgt-1"})
        if "/contents/generations/tasks/" in p:
            return httpx.Response(200, json={"status": "succeeded", "content": {"video_url": "https://files.example.com/a.mp4"}})
        if p.endswith("/video-synthesis"):
            return httpx.Response(200, json={"output": {"task_id": "t-1", "task_status": "PENDING"}})
        if p.startswith("/api/v1/tasks/"):
            return httpx.Response(200, json={"output": {"task_status": "FAILED", "message": "The size is not match"}})
        if p.endswith("/videos/generations"):
            return httpx.Response(200, json={"id": "z-1"})
        if "/async-result/" in p:
            return httpx.Response(200, json={"task_status": "PROCESSING"})
        if req.url.host == "files.example.com":
            return httpx.Response(200, content=MP4)
        return httpx.Response(404)
    monkeypatch.setattr(generation, "_TRANSPORT", httpx.MockTransport(handle))
    frame = generation.Ref(PNG, "image/png", "f.png", role="frame", w=16, h=9)
    ref_img = generation.Ref(PNG, "image/png", "r.png", role="image")
    ref_vid = generation.Ref(url="https://osa.example.com/pub/media/x?exp=1&sig=s", name="v.mp4", role="video")

    job = await generation.submit("ark", "doubao-seedance-2-0-260128", "跳舞", [ref_img, ref_vid], seconds=8, ratio="9:16")
    body = json.loads(seen[-1].content)
    assert body["content"][0] == {"type": "text", "text": "跳舞"}
    assert body["content"][1] == {"type": "image_url", "image_url": {"url": ref_img.data_url()}, "role": "reference_image"}
    assert body["content"][2] == {"type": "video_url", "video_url": {"url": ref_vid.url}, "role": "reference_video"}
    assert (body["duration"], body["ratio"], body["watermark"]) == (8, "9:16", False)
    await generation.submit("ark", "doubao-seedance-2-0-260128", "动起来", [frame])
    body = json.loads(seen[-1].content)
    assert body["content"][1]["role"] == "first_frame" and body["ratio"] == "adaptive"   # 首帧：比例自适应
    status, got = await generation.poll(job)
    assert status == "done" and got == MP4 and seen[-2].url.path == "/api/v3/contents/generations/tasks/cgt-1"

    job = await generation.submit("ds", "wan2.7-r2v", "照着拍", [ref_img, ref_vid], seconds=12)
    req = seen[-1]
    body = json.loads(req.content)
    assert str(req.url) == "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
    assert req.headers["x-dashscope-async"] == "enable" and job["id"] == "t-1"
    assert body["input"]["media"] == [{"type": "reference_image", "url": ref_img.data_url()}, {"type": "reference_video", "url": ref_vid.url}]
    assert body["parameters"]["duration"] == 10   # 有参考视频时最长 10 秒
    assert await generation.poll(job) == ("failed", "The size is not match")

    job = await generation.submit("zp", "cogvideox-3", "日落", [], seconds=7, ratio="1:1")
    body = json.loads(seen[-1].content)
    assert body == {"model": "cogvideox-3", "prompt": "日落", "size": "1024x1024", "duration": 5}
    assert await generation.poll(job) == ("running", None) and seen[-1].url.path == "/api/paas/v4/async-result/z-1"

    with pytest.raises(ValueError, match="不收参考视频"):
        await generation.submit("zp", "cogvideox-3", "x", [ref_vid])
    with pytest.raises(ValueError, match="首尾帧和参考素材不能一起用"):
        await generation.submit("ark", "doubao-seedance-2-0-260128", "x", [frame, ref_img])


async def test_public_link_and_range():
    v = media.save(MP4, "clip.mp4", video=True)
    old = channels.public_url()
    try:
        channels.set_public_url("http://192.168.1.5:8765")
        assert generation.public_link(v["id"]) == ""   # 内网地址：外面打不开，不给
        channels.set_public_url("https://osa.example.com")
        link = generation.public_link(v["id"])
        assert link.startswith("https://osa.example.com/pub/media/" + v["id"])
    finally:
        channels.set_public_url(old)
    q = dict(x.split("=") for x in link.split("?")[1].split("&"))
    from starlette.requests import Request
    req = Request({"type": "http", "headers": [(b"range", b"bytes=4-11")], "method": "GET", "path": "/"})
    r = await server.media_public(req, v["id"], int(q["exp"]), q["sig"])
    assert r.status_code == 206 and r.body == MP4[4:12] and r.headers["content-range"] == f"bytes 4-11/{len(MP4)}"
    with pytest.raises(server.HTTPException):
        await server.media_public(req, v["id"], int(q["exp"]), "0" * 32)
    with pytest.raises(server.HTTPException):
        await server.media_public(req, v["id"], int(time.time()) - 1, generation.sign(v["id"], int(time.time()) - 1))


async def test_video_models_from_typed_list(monkeypatch):
    """硅基流动的视频模型不在默认的 /models 里：配方的 list 写了 /models?type=video，从那里列出来。"""
    monkeypatch.setitem(CFG["providers"], "sfl", {"base_url": "https://api.siliconflow.cn/v1", "api_key_env": "OSA_TEST_SFL"})
    monkeypatch.setenv("OSA_TEST_SFL", "k1")
    monkeypatch.setattr(generation, "_lists", {})
    real = providers.models

    async def fake_models(name, refresh=False):
        if name == "sfl":
            return {"ok": True, "models": ["MiniMaxAI/MiniMax-M2", "Qwen/Qwen3-8B"]}   # 默认列表：只有对话模型
        return await real(name, refresh)
    monkeypatch.setattr(providers, "models", fake_models)
    asked = []

    def handle(req: httpx.Request) -> httpx.Response:
        asked.append(str(req.url))
        if req.url.path == "/v1/models" and req.url.params.get("type") == "video":
            return httpx.Response(200, json={"data": [{"id": "MiniMaxAI/MiniMax-H3"}]})
        return httpx.Response(404)
    monkeypatch.setattr(generation, "_TRANSPORT", httpx.MockTransport(handle))
    got = [(m["provider"], m["model"]) for m in await generation.catalog("video") if m["provider"] == "sfl"]
    assert got == [("sfl", "MiniMaxAI/MiniMax-H3")]   # MiniMax-M2 是对话模型，不算
    await generation.catalog("video")
    assert len(asked) == 1   # 缓存住了
