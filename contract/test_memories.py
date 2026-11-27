"""记忆的截止日、置顶、整理（立即整理、撤销）、每日摘要。只有 Rust 版有（Python 版不再加功能）。"""
import json
import time

import pytest

from conftest import IMPL, idle, until
from fakellm_http import Call

pytestmark = pytest.mark.skipif(IMPL != "rust", reason="记忆整理只在 Rust 版")


def joined(body: dict) -> str:
    return "\n".join(str(m.get("content") or "") for m in body["messages"])


def mems(srv, bot_id: int) -> dict:
    return srv.get(f"/api/bots/{bot_id}/memories")


def texts(srv, bot_id: int, group: str = "own") -> set[str]:
    return {m["text"] for m in mems(srv, bot_id)[group]}


def test_remember_with_until(srv, fake, ws):
    a = srv.make_bot("Dated")
    fake.script("router:Dated", Call("remember", {"text": "用户 12 月 5 日去东京出差", "until": "2099-12-05"}), "记住了")
    ws.say(a["chat_id"], "记住我 2099 年 12 月 5 日去东京出差")
    until(lambda: srv.find(a["chat_id"], role="assistant"), what="回复")
    body = fake.bodies("router", "Dated")[0]
    remember = next(t for t in body["tools"] if t["function"]["name"] == "remember")
    assert "until" in remember["function"]["parameters"]["properties"]
    assert "带上 until" in body["messages"][0]["content"]
    tool = [x for x in fake.bodies("router", "Dated")[-1]["messages"] if x["role"] == "tool"][0]["content"]
    assert tool.startswith("已记住 #") and "到 2099-12-05 为止" in tool
    m = next(x for x in mems(srv, a["id"])["own"] if x["text"] == "用户 12 月 5 日去东京出差")
    assert m["pinned"] is False and m["until"] > time.time()
    # 那一天结束才过期：截止时刻在 12-05 和 12-06 之间
    assert time.strftime("%Y-%m-%d", time.localtime(m["until"] - 1)) == "2099-12-05"
    # 截止日写错了：没记
    fake.script("router:Dated", Call("remember", {"text": "随便一条", "until": "下周五"}), "好")
    ws.say(a["chat_id"], "再记一条")
    until(lambda: len(srv.find(a["chat_id"], role="assistant")) >= 2, what="第二次回复")
    tool = [x for x in fake.bodies("router", "Dated")[-1]["messages"] if x["role"] == "tool"][-1]["content"]
    assert tool.startswith("没记住") and "YYYY-MM-DD" in tool
    srv.delete(f"/api/memories/{m['id']}")


def test_secret_not_remembered(srv):
    a = srv.make_bot("Secret")
    r = srv.http.post(f"/api/bots/{a['id']}/memories", json={"text": "用户的 OpenAI key 是 sk-abcdefghijklmnopqrstuvwxyz123456"})
    assert r.status_code == 400 and "密钥" in r.json()["detail"]
    r = srv.http.post(f"/api/bots/{a['id']}/memories", json={"text": "用户的 WiFi 密码是 hunter2hunter2"})
    assert r.status_code == 400


def test_expired_left_out_and_pinned_kept(srv, fake, ws):
    a = srv.make_bot("Expiry")
    old = srv.post(f"/api/bots/{a['id']}/memories", {"text": "用户上周三有个面试"})
    pin = srv.post(f"/api/bots/{a['id']}/memories", {"text": "用户每年都去京都看红叶"})
    live = srv.post(f"/api/bots/{a['id']}/memories", {"text": "用户的报价单有效到 2099 年底"})
    assert old["until"] is None and old["pinned"] is False
    u = srv.patch(f"/api/memories/{old['id']}", {"until": "2000-01-01"})
    assert u["until"] < time.time() and u["text"] == "用户上周三有个面试"
    p = srv.patch(f"/api/memories/{pin['id']}", {"pinned": True, "until": "2000-01-01"})
    assert p["pinned"] is True
    srv.patch(f"/api/memories/{live['id']}", {"until": "2099-12-31"})
    r = srv.http.patch(f"/api/memories/{live['id']}", json={"until": "明年"})
    assert r.status_code == 400
    # 面板里都在（过期的也在，界面上灰掉）
    own = {m["id"]: m for m in mems(srv, a["id"])["own"]}
    assert {old["id"], pin["id"], live["id"]} <= set(own)
    assert next(iter(mems(srv, a["id"])["own"]))["id"] == pin["id"]   # 置顶的排前面

    fake.script("router:Expiry", Call("dispatch", {"goal": "看看日程"}), "派了")
    fake.script("worker", "没事")
    ws.say(a["chat_id"], "我最近有什么事")
    until(lambda: srv.find(a["chat_id"], kind="result"), what="任务结果")
    idle(srv)
    for body in fake.bodies("router", "Expiry"):
        text = joined(body)
        assert "用户上周三有个面试" not in text
        assert "用户每年都去京都看红叶" in text and "用户的报价单有效到 2099 年底" in text
        assert "（有效到 2099-12-31）" in text
    worker = joined(fake.bodies("worker")[-1])
    assert "用户上周三有个面试" not in worker and "用户每年都去京都看红叶" in worker
    # 清掉截止日：又回来了
    srv.patch(f"/api/memories/{old['id']}", {"until": None})
    ws.say(a["chat_id"], "再看看")
    n = len(fake.bodies("router", "Expiry"))
    until(lambda: len(fake.bodies("router", "Expiry")) > n, what="下一次请求")
    assert "用户上周三有个面试" in fake.bodies("router", "Expiry")[-1]["messages"][0]["content"]
    idle(srv)
    for m in (old, pin, live):
        srv.delete(f"/api/memories/{m['id']}")


NOTES = ["用户喜欢简短的回答", "用户希望先给结论", "用户要求回答先说结论再给细节", "用户正在写第三章", "用户用 Mac 办公"]


def _cons_bot(srv, name):
    b = srv.make_bot(name)
    ids = [srv.post(f"/api/bots/{b['id']}/memories", {"text": t})["id"] for t in NOTES]
    pin = srv.post(f"/api/bots/{b['id']}/memories", {"text": "用户叫小林"})
    srv.patch(f"/api/memories/{pin['id']}", {"pinned": True})
    expired = srv.post(f"/api/bots/{b['id']}/memories", {"text": "用户昨天要交周报"})
    srv.patch(f"/api/memories/{expired['id']}", {"until": "2000-01-01"})
    return b, ids, pin["id"], expired["id"]


def test_consolidate_and_undo(srv, fake, ws):
    b, ids, pin, expired = _cons_bot(srv, "Tidy")
    seen = []

    def reply(body):
        user = body["messages"][-1]["content"]
        seen.append(body)
        if "Bot「Tidy」" not in user:
            return json.dumps({"ops": []})
        return json.dumps({"ops": [
            {"op": "rewrite", "id": ids[0], "text": "用户喜欢简短、直接的回答"},
            {"op": "merge", "ids": [ids[1], ids[2]], "text": "用户希望先给结论，再给细节"},
            {"op": "delete", "id": ids[3]},
            {"op": "keep", "id": ids[4]},
            {"op": "rewrite", "id": pin, "text": "模型不该改置顶的"},
            {"op": "delete", "id": 99999999},
        ]}, ensure_ascii=False)

    fake.script("other", *[reply] * 10)
    before = {m["id"]: m for m in mems(srv, b["id"])["own"]}
    r = srv.post("/api/memories/consolidate")
    assert r["ok"] is True and r["run"] and r["changed"] >= 5   # 过期 1 + 改写 1 + 合并 2 + 删 1
    asked = [x for x in seen if "Bot「Tidy」" in x["messages"][-1]["content"]]
    assert len(asked) == 1
    prompt = asked[0]["messages"][0]["content"]
    assert "用用户写这些记忆的语言写" in prompt and asked[0].get("response_format") == {"type": "json_object"}
    listed = asked[0]["messages"][-1]["content"]
    assert "用户叫小林" in listed.split("置顶的")[1] and "用户昨天要交周报" not in listed
    now = {m["id"]: m for m in mems(srv, b["id"])["own"]}
    assert now[ids[0]]["text"] == "用户喜欢简短、直接的回答"
    assert now[ids[1]]["text"] == "用户希望先给结论，再给细节" and ids[2] not in now
    assert ids[3] not in now and expired not in now
    assert now[ids[4]]["text"] == "用户用 Mac 办公"
    assert now[pin] == before[pin]
    until(lambda: ws.of("memories_changed"), what="memories_changed")
    st = srv.get("/api/memories/consolidate")
    assert st["running"] is False and st["last"]["run"] == r["run"] and st["last"]["changed"] == r["changed"] and st["last"]["undone"] is False

    u = srv.post("/api/memories/consolidate/undo")
    assert u["ok"] is True and u["run"] == r["run"]
    back = {m["id"]: m for m in mems(srv, b["id"])["own"]}
    assert back == before
    last = srv.get("/api/memories/consolidate")["last"]
    assert last["run"] == r["run"] and last["undone"] is True
    r2 = srv.http.post("/api/memories/consolidate/undo")
    assert r2.status_code == 400   # 没有别的可撤销了
    for i in [*ids, pin, expired]:
        srv.delete(f"/api/memories/{i}")


def test_consolidate_garbage_keeps_everything(srv, fake):
    b, ids, pin, expired = _cons_bot(srv, "Messy")
    srv.delete(f"/api/memories/{expired}")

    def reply(body):
        if "Bot「Messy」" in body["messages"][-1]["content"]:
            return "好的，我整理了一下：{ops: 乱七八糟"
        return json.dumps({"ops": []})

    fake.script("other", *[reply] * 10)
    before = mems(srv, b["id"])["own"]
    r = srv.post("/api/memories/consolidate")
    assert r["changed"] == 0
    assert mems(srv, b["id"])["own"] == before

    # 删得太多（超过一半）也当作答坏了
    def wipe(body):
        if "Bot「Messy」" in body["messages"][-1]["content"]:
            return json.dumps({"ops": [{"op": "delete", "id": i} for i in ids]})
        return json.dumps({"ops": []})

    fake.reset()
    fake.script("other", *[wipe] * 10)
    assert srv.post("/api/memories/consolidate")["changed"] == 0
    assert mems(srv, b["id"])["own"] == before

    # 少于几条不调模型
    small = srv.make_bot("Small")
    srv.post(f"/api/bots/{small['id']}/memories", {"text": "用户喜欢猫"})
    fake.reset()
    srv.post("/api/memories/consolidate")
    assert not [x for x in fake.bodies("other") if "Bot「Small」" in x["messages"][-1]["content"]]
    for i in [*ids, pin]:
        srv.delete(f"/api/memories/{i}")


def test_daily_digest_in_state(srv, fake, ws):
    a = srv.make_bot("Digest")
    fake.script("router:Digest", Call("dispatch", {"goal": "查一下东京下周的天气"}), "派了")
    fake.script("worker", "东京下周晴")
    ws.say(a["chat_id"], "东京下周天气怎么样")
    until(lambda: srv.find(a["chat_id"], kind="result"), what="任务结果")
    idle(srv)
    fake.script("other", *[json.dumps({"ops": []})] * 10)
    r = srv.post("/api/memories/consolidate")
    assert r["digest"] is True
    digest = mems(srv, a["id"])["digest"]
    assert digest and "查一下东京下周的天气" in digest[0]["text"] and "Digest" in digest[0]["text"]
    assert digest[0]["shared"] is True and digest[0]["kind"] == "digest" and "（完成）" in digest[0]["text"]
    # 再整理一次：上一条摘要以来没做事，不写新的
    assert srv.post("/api/memories/consolidate")["digest"] is False
    assert len(mems(srv, a["id"])["digest"]) == len(digest)

    n = len(fake.bodies("router", "Digest"))
    ws.say(a["chat_id"], "今天做了什么")
    until(lambda: len(fake.bodies("router", "Digest")) > n, what="下一次请求")
    body = fake.bodies("router", "Digest")[-1]
    assert "查一下东京下周的天气" not in body["messages"][0]["content"]   # 不进系统提示词
    state = body["messages"][-1]["content"]
    assert "每日摘要" in str(state) and "查一下东京下周的天气" in str(state)
    idle(srv)
