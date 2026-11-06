"""Router 每轮发给模型的内容：前面的部分要逐字不变，才能命中模型服务的缓存。"""
from conftest import until
from osa import bots, memories, memory, router, server
from osa.config import CFG


async def test_system_prompt_and_history_stay_identical(llm, events, make_bot):
    a = make_bot("Cachy")
    chat = bots.chat(a["chat_id"])
    for i, text in enumerate(["你好", "今天有什么安排", "帮我记一下"], 1):
        llm.script("router:Cachy", f"第 {i} 次回复")
        await server.safe_handle(chat, text)
        await until(lambda: len(llm.bodies("router", "Cachy")) == i, what=f"第 {i} 轮")
        memories.log_work(a["id"], 1000 + i, f"做过的第 {i} 件事")   # 状态在变：下一轮“最近做过的事”不一样

    b1, b2, b3 = [b["messages"] for b in llm.bodies("router", "Cachy")]
    assert b1[0] == b2[0] == b3[0]                       # 系统提示词逐字不变
    assert "做过的第" not in b3[0]["content"]
    assert "做过的第 2 件事" in b3[-1]["content"]          # 变的部分附在最新一条消息后面
    assert b3[-1]["content"].startswith("帮我记一下")
    assert "以下是系统附上的当前状态" in b3[-1]["content"]
    assert b3[:len(b2) - 1] == b2[:-1]                   # 上一轮发过的历史（除了它最新那条）原样是这一轮的前缀


def test_state_goes_after_last_user_message():
    state = "\n\n（以下是系统附上的当前状态）"
    hist = [{"role": "user", "content": "你好"}]
    assert router.with_state(hist, state) == [{"role": "user", "content": "你好" + state}]
    assert hist == [{"role": "user", "content": "你好"}]   # 不改原来的列表
    hist = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "嗨"}]
    assert router.with_state(hist, state)[-1] == {"role": "user", "content": state.lstrip()}


def test_history_window_moves_in_batches(make_bot, monkeypatch):
    monkeypatch.setitem(CFG["memory"], "inline_messages", 4)
    a = make_bot("Windowy")
    cid = a["chat_id"]
    prev = None
    for i in range(1, 13):
        memory.add("user", f"消息 {i}", cid, a["id"])
        ctx = memory.context(cid)
        kept = [m for m in ctx if m["role"] == "user"]
        assert 4 <= len(kept) <= 7 or i < 4
        if i in (4, 8, 12):       # 每满 4 条才挪一次
            assert kept[0]["content"] == f"消息 {i - 3}"
        elif prev is not None:    # 没挪：上一次的内容原样是这一次的前缀
            assert ctx[:len(prev)] == prev
        prev = ctx
    assert ctx[0]["role"] == "system" and "#" in ctx[0]["content"]   # 移出的只留编号和开头
