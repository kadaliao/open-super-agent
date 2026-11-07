"""对话里的图片和附件：存、取（要登录）、发消息时带上、能看图的模型直接看到、send_file、清理、聊天软件里的链接。"""
import time
import httpx
import pytest
from conftest import find, settle, until
from fakellm import Call
from osa import bots, channels, computer, media, memory, providers, router, server

PNG = providers._png(6, 4, (0, 128, 255))
GIF = b"GIF89a" + (12).to_bytes(2, "little") + (7).to_bytes(2, "little") + b"\x00" * 20
JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08\x00\x64\x00\xc8\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\x00" * 20)
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00" + (299).to_bytes(3, "little") + (149).to_bytes(3, "little")


def http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


def test_sniff_and_dimensions():
    for data, mime, size in ((PNG, "image/png", (6, 4)), (GIF, "image/gif", (12, 7)),
                             (JPEG, "image/jpeg", (200, 100)), (WEBP, "image/webp", (300, 150))):
        assert media.sniff(data) == mime
        assert media.dimensions(data, mime) == size
    assert media.sniff(b"<svg onload=alert(1)>") is None


async def test_upload_and_serve(no_password):
    async with http() as c:
        r = await c.put("/api/media?name=shot.png", content=PNG)
        assert r.status_code == 200
        m = r.json()
        assert media.ID.match(m["id"]) and (m["w"], m["h"], m["mime"], m["name"]) == (6, 4, "image/png", "shot.png")
        assert (await c.put("/api/media?name=again.png", content=PNG)).json()["id"] == m["id"]   # 同一张图只存一份
        got = await c.get(f"/api/media/{m['id']}")
        assert got.content == PNG and got.headers["content-type"] == "image/png"
        assert "immutable" in got.headers["cache-control"] and "private" in got.headers["cache-control"]
        assert got.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in (await c.get(f"/api/media/{m['id']}?download=1")).headers["content-disposition"]
        assert (await c.put("/api/media", content=b"<svg onload=alert(1)></svg>")).status_code == 400
        assert (await c.get("/api/media/" + "0" * 24)).status_code == 404


async def test_media_needs_login(password):
    mid = media.save(PNG, "x.png")["id"]
    async with http() as c:
        assert (await c.get(f"/api/media/{mid}")).status_code == 401
        r = await c.get(f"/media/{mid}")   # 聊天软件里的链接：先去登录，登录后回来
        assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]


def test_clean_attachments():
    mid = media.save(PNG, "a.png")["id"]
    ws = computer.get().workspace
    got = media.clean_attachments([
        {"media": mid, "name": "a.png", "path": "uploads/a.png", "size": 999},
        {"name": "report.pdf", "path": ws + "/uploads/report.pdf", "size": 1234},
        {"name": "evil", "path": "/etc/passwd"},            # 跑出工作文件夹：丢掉
        {"media": "f" * 24, "name": "gone.png"},             # 不存在的图片：丢掉
        "nonsense",
    ])
    assert got == [
        {"name": "a.png", "path": ws + "/uploads/a.png", "media": mid, "mime": "image/png", "w": 6, "h": 4, "size": len(PNG)},
        {"name": "report.pdf", "path": ws + "/uploads/report.pdf", "size": 1234},
    ]
    assert media.with_marks("看看", got) == f"看看\n[附件] {ws}/uploads/a.png\n[附件] {ws}/uploads/report.pdf"
    assert media.with_marks("", got[1:]) == f"[附件] {ws}/uploads/report.pdf"


async def test_vision_model_sees_attached_image(llm, events, make_bot, monkeypatch):
    a = make_bot("Seer")
    mid = media.save(PNG, "shot.png")["id"]
    atts = media.clean_attachments([{"media": mid, "name": "shot.png", "path": "uploads/shot.png"}])
    monkeypatch.setattr(media, "can_see", lambda *_: True)
    llm.script("router:Seer", "图里是一个蓝色方块")
    await server.safe_handle(bots.chat(a["chat_id"]), "图里是什么", attachments=atts)
    await until(lambda: find(a["chat_id"], role="assistant"), what="回复")
    user = find(a["chat_id"], role="user")[0]
    assert user["content"].startswith("图里是什么\n[附件] ") and user["meta"]["attachments"][0]["media"] == mid

    body = llm.bodies("router", "Seer")[-1]
    assert router.ATTACH_SEE in body["messages"][0]["content"]
    last = body["messages"][-1]["content"]
    assert isinstance(last, list) and last[0]["text"].startswith("图里是什么")
    assert last[1]["type"] == "image_url" and last[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "以下是系统附上的当前状态" in last[-1]["text"]   # 这一轮的状态接在图片后面，不另起一条
    assert not llm.bodies("worker")                        # 直接看图回答，没派任务


async def test_blind_model_gets_text_only(llm, events, make_bot, monkeypatch):
    a = make_bot("Blind")
    mid = media.save(PNG, "shot.png")["id"]
    monkeypatch.setattr(media, "can_see", lambda *_: False)
    await server.safe_handle(bots.chat(a["chat_id"]), "图里是什么",
                             attachments=media.clean_attachments([{"media": mid, "path": "uploads/shot.png"}]))
    await until(lambda: find(a["chat_id"], role="assistant"), what="回复")
    body = llm.bodies("router", "Blind")[-1]
    assert router.ATTACH_BLIND in body["messages"][0]["content"]
    assert isinstance(body["messages"][-1]["content"], str) and "[附件] " in body["messages"][-1]["content"]


def test_only_recent_images_go_to_model(make_bot):
    a = make_bot()
    cid = a["chat_id"]
    ids = []
    for i in range(6):
        m = media.save(providers._png(2, 2, (i, i, i)), f"{i}.png")
        ids.append(m["id"])
        memory.add("user", f"第 {i} 张", cid, a["id"], attachments=media.clean_attachments([{"media": m["id"]}]))
    msgs = memory.context(cid, images=media.MODEL_IMAGES)
    with_img = [m for m in msgs if isinstance(m["content"], list)]
    assert len(with_img) == media.MODEL_IMAGES and with_img[-1]["content"][0]["text"] == "第 5 张"
    assert msgs[0]["content"] == "第 0 张"   # 更早的只剩文字
    assert all(isinstance(m["content"], str) for m in memory.context(cid))


class FakePC:
    workspace = "/data/workspace"
    backend = "local"

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def status(self):
        return {"state": "running"}

    async def start(self):
        pass

    async def stat_paths(self, paths):
        return {p: {"name": p.rsplit("/", 1)[1], "path": p, "dir": False, "size": len(self.files[p]), "mtime": 0}
                for p in paths if p in self.files}

    async def read_bytes(self, path, limit=0):
        return self.files[path]


async def test_send_file_from_router_and_task(llm, events, make_bot, monkeypatch):
    pc = FakePC({"/data/workspace/shot.png": PNG, "/data/workspace/out/table.csv": b"a,b\n1,2\n"})
    monkeypatch.setattr(computer, "get", lambda: pc)
    a = make_bot("Sender")
    llm.script("router:Sender", [Call("send_file", {"path": "shot.png"}), Call("send_file", {"path": "nope.txt"})], "截图发给你了")
    await server.safe_handle(bots.chat(a["chat_id"]), "把截图发我")
    reply = (await until(lambda: find(a["chat_id"], role="assistant"), what="回复"))[0]
    att = reply["meta"]["attachments"]
    assert len(att) == 1 and att[0]["name"] == "shot.png" and media.get(att[0]["media"])[1] == PNG
    tools = [m for m in llm.bodies("router", "Sender")[-1]["messages"] if m["role"] == "tool"]
    assert "已发给用户" in tools[0]["content"] and "没有这个文件" in tools[1]["content"]

    llm.script("router:Sender", Call("dispatch", {"goal": "导出表格发给用户"}), "派出去了")
    llm.script("worker", Call("send_file", {"path": "out/table.csv"}), "表格做好了")
    await server.safe_handle(bots.chat(a["chat_id"]), "导出表格")
    res = (await until(lambda: find(a["chat_id"], kind="result"), what="任务结果"))[0]
    assert res["meta"]["attachments"] == [{"name": "table.csv", "path": "/data/workspace/out/table.csv", "size": 8}]
    await settle()


def test_gc_keeps_referenced_and_fresh(make_bot):
    a = make_bot()
    used_old, free_old, fresh = (media.save(providers._png(3, 3, c))["id"] for c in ((7, 7, 7), (8, 8, 8), (9, 9, 9)))
    memory.add("user", "x", a["chat_id"], a["id"], attachments=[{"media": used_old, "name": "u.png"}])
    for mid in (used_old, free_old):
        memory.conn.execute("update media set created=? where id=?", (time.time() - media.KEEP_S - 10, mid))
    media.gc()
    assert media.get(used_old) and media.get(fresh) and not media.get(free_old)
    bots.delete(a["id"])   # 删 Bot：消息没了，图片也清掉
    assert not media.get(used_old)


def test_channel_render_links_attachments(make_bot):
    a = make_bot()
    m = {"role": "assistant", "content": "给你", "bot_id": a["id"],
         "meta": {"attachments": [{"name": "shot.png", "media": "a" * 24}, {"name": "r.pdf", "path": "/data/workspace/r.pdf"}]}}
    out = channels.render(m, False)
    assert f"[shot.png](/media/{'a' * 24})" in out and "[r.pdf](/files/download?path=/data/workspace/r.pdf)" in out
    assert channels.render({**m, "content": ""}, False)   # 只有附件也要发


@pytest.fixture(autouse=True)
def _clear_vision_tests():
    yield
    memory.conn.execute("delete from settings where key='vision_tests'")
    memory.conn.commit()


def test_vision_test_result_is_remembered(monkeypatch):
    from osa import prices
    monkeypatch.setattr(prices, "lookup", lambda p, m: {"vision": False})
    assert not media.can_see("p", "m")
    media.remember_test("p", "m", "yes")
    assert media.can_see("p", "m")
    media.remember_test("p", "m", "unsure")   # 拿不准的不改
    assert media.can_see("p", "m")


async def test_model_rejecting_images_falls_back(llm, events, make_bot, monkeypatch):
    """以为能看图、服务却回 400：这一轮把图片换成文字重试，记下这个模型不能看图。"""
    import openai
    from osa.config import CFG
    a = make_bot("Picky")
    mid = media.save(providers._png(5, 5, (1, 2, 3)), "p.png")["id"]
    real = llm.client.chat.completions.create

    async def picky(**kw):
        if any(isinstance(m.get("content"), list) for m in kw["messages"]):
            raise openai.BadRequestError("image_url is not supported", body=None,
                                         response=httpx.Response(400, request=httpx.Request("POST", "http://fake.llm/v1")))
        return await real(**kw)
    monkeypatch.setattr(llm.client.chat.completions, "create", picky)
    monkeypatch.setattr(media, "can_see", lambda *_: True)
    llm.script("router:Picky", "看不了图，只看到文字")
    await server.safe_handle(bots.chat(a["chat_id"]), "这是什么",
                             attachments=media.clean_attachments([{"media": mid, "path": "uploads/p.png"}]))
    await until(lambda: find(a["chat_id"], role="assistant"), what="回复")
    r = CFG["roles"]["router"]
    assert media.tested(r["provider"], r["model"]) is False
    last = llm.bodies("router", "Picky")[-1]["messages"][-1]["content"]
    assert isinstance(last, str) and "这个模型不收图片" in last and "以下是系统附上的当前状态" in last
