"""右栏“资料库”：Bot 交出来的页面、文件、图片、链接（osa/outputs.py）。"""
import httpx
import pytest
from osa import bots, library, memory, outputs, server


@pytest.fixture
def bot(make_bot):
    """用完就删：测试库里 Bot 有 50 个的上限，大家共用。"""
    made = []

    def make() -> dict:
        made.append(make_bot())
        return made[-1]
    yield make
    for b in made:
        if not (bots.get(b["id"]) or {}).get("is_primary"):
            bots.delete(b["id"])


def test_links_markdown_bare_and_punctuation():
    text = ("看这里：[**官方文档**](https://docs.example.com/a_(b)) 和 https://example.com/x。"
            "还有（https://foo.org/p?q=1）, 以及 https://bar.io/path).\n"
            "见https://zh.example.com/wiki/北京，然后 <https://angle.dev/x>")
    got = dict(outputs.links(text))
    assert got["https://docs.example.com/a_(b)"] == "官方文档"
    assert got["https://example.com/x"] == ""
    assert "https://foo.org/p?q=1" in got
    assert "https://bar.io/path" in got
    assert "https://zh.example.com/wiki/北京" in got
    assert "https://angle.dev/x" in got
    assert len(got) == 6


def test_links_skip_code_blocks_and_junk():
    text = "```\ncurl https://api.example.com/v1\n```\n裸的 http://localhost/x 和 https://nodot/ 还有 [链接](https://a.com/x \"t\")"
    got = dict(outputs.links(text))
    assert "https://api.example.com/v1" not in got
    assert "https://nodot/" not in got
    assert got["https://a.com/x"] == "链接"
    assert "http://localhost/x" in got


def test_internal_links():
    own = {"osa.example.com"}
    assert outputs.internal("https://osa.example.com/library/pages/3", own)
    assert outputs.internal("http://127.0.0.1:8765/api/files/download?path=/x", set())
    assert not outputs.internal("https://osa.example.com/blog", own)
    assert not outputs.internal("https://other.com/library/pages/3", own)


def test_for_bot_collects_and_dedupes(bot):
    b, other = bot(), bot()
    cid = b["chat_id"]
    memory.add("user", "帮我看看 https://user-said.com", cid)
    memory.add("task", "[任务已派发] 研究 https://goal.com", cid, b["id"], kind="dispatch", status="running")
    memory.add("assistant", "先看 https://dup.com/a", cid, b["id"])
    tid = memory.add("task", "[任务已派发] 做表", cid, b["id"], kind="dispatch")
    memory.add("task", f"[任务 #{tid} 结果]\n[对比](https://dup.com/a) 和 https://osa.local/library/pages/1",
               cid, b["id"], kind="result", task_id=tid,
               attachments=[{"name": "表.xlsx", "path": "/data/workspace/表.xlsx", "size": 2048},
                            {"name": "shot.png", "path": "/data/workspace/shot.png", "media": "a" * 24, "mime": "image/png", "w": 4, "h": 3}])
    memory.add("assistant", "再发一次", cid, b["id"], attachments=[{"name": "表.xlsx", "path": "/data/workspace/表.xlsx", "size": 4096}])
    memory.add("task", "[画图] 一只猫", cid, b["id"], kind="image", attachments=[{"name": "cat.png", "media": "b" * 24, "mime": "image/png"}])
    memory.add("task", "私信 https://dm.com", cid, b["id"], kind="dm", to=other["id"])
    memory.add("assistant", "别人的 https://other-bot.com", other["chat_id"], other["id"])
    page = library.save_page(b["id"], "周报", "# hi")

    items = outputs.for_bot(b["id"], own_hosts={"osa.local"})
    keys = [x["key"] for x in items]
    assert "page:%d" % page["id"] in keys
    urls = {x["url"]: x for x in items if x["kind"] == "link"}
    assert set(urls) == {"https://dup.com/a"}            # 用户、派任务、私信、别的 Bot、站内链接都不算
    assert urls["https://dup.com/a"]["count"] == 2 and urls["https://dup.com/a"]["title"] == "对比"
    assert urls["https://dup.com/a"]["task_id"] == tid    # 最新一次在任务结果里
    files = [x for x in items if x["kind"] == "file"]
    assert len(files) == 1 and files[0]["size"] == 4096 and files[0]["count"] == 2 and files[0]["task_id"] is None
    imgs = {x["media"]: x for x in items if x["kind"] == "image"}
    assert set(imgs) == {"a" * 24, "b" * 24} and imgs["a" * 24]["task_id"] == tid
    assert [x["ts"] for x in items] == sorted((x["ts"] for x in items), reverse=True)
    assert outputs.for_bot(other["id"])[0]["url"] == "https://other-bot.com"


async def test_route(bot, no_password):
    b = bot()
    memory.add("assistant", "https://route.example.com/x", b["chat_id"], b["id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1") as c:
        r = await c.get(f"/api/bots/{b['id']}/outputs")
        assert r.status_code == 200 and r.json()[0]["url"] == "https://route.example.com/x"
        assert (await c.get("/api/bots/999999/outputs")).status_code == 404
