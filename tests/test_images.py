"""对话里画图：没模型时照实说、先弹确认卡片、允许后出图、“总是允许”后不再问、拒绝就不画、费用记一行。"""
import base64
import httpx
import pytest
from conftest import find, settle, until
from fakellm import Call
from osa import approvals, avatars, bots, images, media, memory, prices, providers, server
from osa.config import CFG

PNG = providers._png(8, 8, (10, 200, 30))


@pytest.fixture
def sf(monkeypatch):
    """一个假的硅基流动：列出 Kolors 和 Qwen-Image，画出来是一张绿色小图。"""
    monkeypatch.setitem(CFG["providers"], "sf", {"base_url": "https://api.siliconflow.cn/v1", "api_key_env": "OSA_TEST_SF"})
    monkeypatch.setenv("OSA_TEST_SF", "k1")
    real_models = providers.models

    async def fake_models(name, refresh=False):
        if name == "sf":
            return {"ok": True, "models": ["Kwai-Kolors/Kolors", "Qwen/Qwen-Image"]}
        return await real_models(name, refresh)
    monkeypatch.setattr(providers, "models", fake_models)
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.siliconflow.cn":
            calls.append(req)
            return httpx.Response(200, json={"images": [{"url": "https://files.example.com/a.png"}]})
        if req.url.host == "files.example.com":
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)
    monkeypatch.setattr(avatars, "_TRANSPORT", httpx.MockTransport(handle))
    memory.conn.execute("delete from settings where key in ('avatar_model', 'image_model')")
    memory.conn.execute("delete from rules where pattern='generate_image'")
    memory.conn.commit()
    yield calls
    memory.conn.execute("delete from rules where pattern='generate_image'")
    memory.conn.commit()


def test_image_price():
    assert prices.image_price("sf", "Kwai-Kolors/Kolors") is None   # 没有 sf 这个服务
    CFG["providers"]["sf2"] = {"base_url": "https://api.siliconflow.cn/v1"}
    try:
        assert prices.image_price("sf2", "Kwai-Kolors/Kolors") == 0
        assert prices.image_price("sf2", "Tongyi-MAI/Z-Image-Turbo") == 0.1
        assert prices.image_price("sf2", "Tongyi-MAI/Z-Image") == 0.3   # 不会被 Z-Image-Turbo 那一行认走
        assert prices.image_price("sf2", "black-forest-labs/FLUX.1-dev") is None
    finally:
        del CFG["providers"]["sf2"]


async def test_no_image_model(llm, events, make_bot, monkeypatch):
    async def none():
        return {"models": [], "default": None}
    monkeypatch.setattr(avatars, "models", none)
    a = make_bot("Painter0")
    llm.script("router:Painter0", Call("generate_image", {"prompt": "一只猫"}), "我现在画不了")
    await server.safe_handle(bots.chat(a["chat_id"]), "画一只猫")
    await until(lambda: find(a["chat_id"], role="assistant"), what="回复")
    tool = [m for m in llm.bodies("router", "Painter0")[-1]["messages"] if m["role"] == "tool"][0]
    assert tool["content"] == images.NO_MODEL
    assert not [x for x in approvals.pending.values() if x["tool"] == "generate_image"]


async def test_ask_then_draw_then_always(llm, events, make_bot, sf):
    a = make_bot("Painter")
    cid = a["chat_id"]
    llm.script("router:Painter", Call("generate_image", {"prompt": "一只戴墨镜的猫"}), "请在卡片上确认")
    await server.safe_handle(bots.chat(cid), "画一只戴墨镜的猫")
    card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "generate_image"), None), what="确认卡片")
    assert card["chat_id"] == cid and "一只戴墨镜的猫" in card["args"] and "Kwai-Kolors/Kolors" in card["args"] and "免费" in card["args"]
    assert find(cid, role="assistant", text="请在卡片上确认")   # Router 不干等，这一轮照常说完
    assert not sf                                              # 没确认之前不画

    before = memory.conn.execute("select count(*) from llm_calls where role='image'").fetchone()[0]
    approvals.resolve(card["id"], True, always=True)
    img = (await until(lambda: find(cid, kind="image"), what="画好的图"))[0]
    assert img["meta"]["prompt"] == "一只戴墨镜的猫" and img["meta"]["model"] == "Kwai-Kolors/Kolors" and img["meta"]["price"] == 0
    att = img["meta"]["attachments"][0]
    assert media.get(att["media"])[1] == PNG and (att["w"], att["h"]) == (8, 8)
    assert memory.conn.execute("select count(*) from llm_calls where role='image'").fetchone()[0] == before + 1
    await settle()

    # 点过“总是允许”：下一次直接画，工具说已画好
    llm.script("router:Painter", Call("generate_image", {"prompt": "再来一只"}), "画好了")
    await server.safe_handle(bots.chat(cid), "再画一只")
    await until(lambda: len(find(cid, kind="image")) == 2, what="第二张")
    tool = [m for m in llm.bodies("router", "Painter")[-1]["messages"] if m["role"] == "tool"][0]
    assert tool["content"].startswith("已画好")
    assert not [x for x in approvals.pending.values() if x["tool"] == "generate_image"]

    # 下一轮的上下文里知道自己画过什么
    llm.script("router:Painter", "好的")
    await server.safe_handle(bots.chat(cid), "刚才画了什么")
    await until(lambda: len(find(cid, role="assistant")) == 3, what="第三轮")
    ctx = [m["content"] for m in llm.bodies("router", "Painter")[-1]["messages"] if isinstance(m["content"], str)]
    assert any("画好了一张图" in c and "再来一只" in c for c in ctx)


async def test_reject_draws_nothing(llm, events, make_bot, sf):
    a = make_bot("Painter2")
    llm.script("router:Painter2", Call("generate_image", {"prompt": "一座山"}), "等你确认")
    await server.safe_handle(bots.chat(a["chat_id"]), "画一座山")
    card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "generate_image"), None), what="确认卡片")
    approvals.resolve(card["id"], False)
    await settle()
    assert not find(a["chat_id"], kind="image") and not sf


async def test_group_card_goes_to_private_chat(llm, events, make_bot, sf):
    a, b = make_bot("GPainter"), make_bot()
    g = bots.create_group([a["id"], b["id"]], lead=a["id"])
    llm.script("router:GPainter", Call("generate_image", {"prompt": "群头像"}), "确认一下")
    await server.safe_handle(bots.chat(g["id"]), "@GPainter 画个群头像")
    card = await until(lambda: next((x for x in approvals.pending.values() if x["tool"] == "generate_image"), None), what="确认卡片")
    assert card["chat_id"] == a["chat_id"] and card["origin_chat_id"] == g["id"]
    approvals.resolve(card["id"], True)
    await until(lambda: find(g["id"], kind="image"), what="图发到群里")
    await settle()


def test_usage_counts_images():
    u = memory.usage_stats()
    assert all("calls" in r for r in u["by_role"])


def test_png_fixture_is_png():
    assert base64.b64encode(PNG) and media.sniff(PNG) == "image/png"
