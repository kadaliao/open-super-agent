"""把看板接进对话（第二波，只有 Rust 版有，见 docs/plans/tasks-board.md“第二波：把看板接进对话”）：
派任务时归卡（need / need_id / project）、从看板开始的卡、状态里的看板和 update_need、任务的公开字段 need、
消息以卡片编号开头交给任务、任务状态变了卡片跟着变（等别的卡的卡自动开始）、项目的简介和目录、项目记忆、counts。"""
import json
import threading
import time

import pytest

from conftest import IMPL, idle, until
from fakellm_http import Call

pytestmark = pytest.mark.skipif(IMPL != "rust", reason="看板只做在 Rust 版")


def card(srv, nid: int) -> dict:
    return srv.get(f"/api/needs/{nid}")


def column(srv, nid: int) -> str:
    return card(srv, nid)["column"]


def project(srv, key: str, name: str = "", **kw) -> dict:
    return srv.post("/api/projects", {"key": key, "name": name or f"{key} 项目", **kw})


def all_cards(srv) -> list[dict]:
    b = srv.get("/api/board")
    return [c for cs in b["columns"].values() for c in cs]


def primary(srv) -> dict:
    return next(b for b in srv.get("/api/bootstrap")["bots"] if b["is_primary"])


def dispatched(srv, chat_id: int, n: int = 1, timeout: float = 10) -> list[dict]:
    return until(lambda: len(srv.find(chat_id, kind="dispatch")) >= n and srv.find(chat_id, kind="dispatch"), timeout, "派发")


def user_text(body: dict) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], str))


def tool_text(body: dict) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "tool")


def released(evt: threading.Event, reply):
    return lambda body: (evt.wait(15), reply)[1]


# ---------- 1. 派任务时归卡 ----------
def test_dispatch_with_need_makes_card(srv, fake, ws):
    a = srv.make_bot("Carder")
    fake.script("router:Carder", Call("dispatch", {"goal": "查三家竞品的价格", "need": "一份三家竞品的价格对比"}), "派了")
    fake.script("worker", "价格对比做好了")
    ws.say(a["chat_id"], "帮我比一下三家竞品的价格")
    d = dispatched(srv, a["chat_id"])[0]
    nid = d["meta"]["need_id"]
    c = until(lambda: column(srv, nid) == "done" and card(srv, nid), what="卡片做完")
    assert c["title"] == "一份三家竞品的价格对比" and c["auto_title"] is False
    assert c["source_text"] == "帮我比一下三家竞品的价格" and c["assignee_bot_id"] == a["id"]
    assert [t["id"] for t in c["tasks"]] == [d["id"]]
    assert c["origin"]["chat_id"] == a["chat_id"]
    # 4. 任务的公开字段多 need（WS 事件、列表、详情）
    want = {"id": nid, "key": None, "title": "一份三家竞品的价格对比"}
    ev = [e["task"] for e in ws.of("task") if e["task"]["id"] == d["id"]]
    assert ev and all(t["need"] == want for t in ev)
    assert srv.get("/api/tasks", params={"bot_id": a["id"]})[0]["need"] == want
    assert srv.get(f"/api/tasks/{d['id']}")["task"]["need"] == want
    out = tool_text(fake.bodies("router", "Carder")[1])
    assert f"已派发任务 #{d['id']}（卡片 #{nid}）" in out


def test_dispatch_without_need_uses_user_words(srv, fake, ws):
    a = srv.make_bot("Plain")
    # 同一轮派两个任务：归同一张卡
    fake.script("router:Plain", [Call("dispatch", {"goal": "查北京天气"}), Call("dispatch", {"goal": "查上海天气"})], "派了")
    fake.script("worker", "北京晴", "上海雨")
    ws.say(a["chat_id"], "  查一下北京和上海的天气\n明天的 ")
    ds = dispatched(srv, a["chat_id"], 2)
    assert ds[0]["meta"]["need_id"] == ds[1]["meta"]["need_id"]
    c = card(srv, ds[0]["meta"]["need_id"])
    assert c["title"] == "查一下北京和上海的天气" and c["auto_title"] is True
    assert sorted(t["id"] for t in c["tasks"]) == sorted(d["id"] for d in ds)
    idle(srv)


def test_dispatch_need_id_and_project(srv, fake, ws):
    p = project(srv, "WEB", "官网", brief="公司官网改版", dir="/srv/web")
    a = srv.make_bot("Proj")
    fake.script("router:Proj", Call("dispatch", {"goal": "改首页文案", "need": "新的首页文案", "project": "web"}), "派了")
    fake.script("worker", "文案改好了")
    ws.say(a["chat_id"], "首页文案改一下")
    d = dispatched(srv, a["chat_id"])[0]
    c = card(srv, d["meta"]["need_id"])
    assert c["key"] == "WEB-1" and c["project_id"] == p["id"]
    # 7. 项目的简介和目录接在任务交代后面
    goal = user_text(until(lambda: fake.bodies("worker"), what="Worker 开始")[0])
    assert "这个任务属于项目「官网」（WEB）" in goal and "项目目录：/srv/web" in goal and "公司官网改版" in goal
    idle(srv)
    # 用户接着追问已有的卡：need_id 写编号，归到这张卡
    fake.script("router:Proj", Call("dispatch", {"goal": "再加一句标语", "need_id": "WEB-1"}), "加上")
    fake.script("worker", "标语加好了")
    ws.say(a["chat_id"], "再加一句标语")
    d2 = dispatched(srv, a["chat_id"], 2)[1]
    assert d2["meta"]["need_id"] == c["id"]
    until(lambda: len(card(srv, c["id"])["tasks"]) == 2, what="归到同一张卡")
    # 不存在的卡：不派
    fake.script("router:Proj", Call("dispatch", {"goal": "x", "need_id": "WEB-999"}), "好")
    ws.say(a["chat_id"], "x")
    until(lambda: len(fake.bodies("router", "Proj")) >= 6, what="Router 第三轮")
    assert "没有卡片 WEB-999" in tool_text(fake.bodies("router", "Proj")[5])
    assert len(srv.find(a["chat_id"], kind="dispatch")) == 2
    idle(srv)


def test_dm_dispatch_not_on_board(srv, fake, ws):
    a = srv.make_bot("Asker")
    b = srv.make_bot("Doer")
    fake.script("router:Asker", Call("message_bot", {"to": "Doer", "text": "帮我查一下汇率"}), "问了 Doer", "Doer 回了")
    fake.script("router:Doer", Call("dispatch", {"goal": "查汇率", "need": "汇率"}), "这就查")
    fake.script("worker", "1 美元 = 7.1 元")
    ws.say(a["chat_id"], "让 Doer 查汇率")
    d = dispatched(srv, b["chat_id"])[0]
    assert "need_id" not in d["meta"]
    assert srv.get(f"/api/tasks/{d['id']}")["task"]["need"] is None
    assert not [c for c in all_cards(srv) if c["title"] == "汇率"]
    idle(srv)


# ---------- 2. 从看板开始一张交给主 Bot 的卡 ----------
def test_board_start_primary_attaches_turn(srv, fake, ws):
    p = primary(srv)
    before = len(all_cards(srv))
    fake.script(f"router:{p['name']}", [Call("dispatch", {"goal": "查原因"}), Call("dispatch", {"goal": "写修复方案", "need": "别的标题"})],
                "派了两个")
    fake.script("worker", "原因找到了", "方案写好了")
    n = srv.post("/api/needs", {"title": "登录老是掉线", "start": True})
    msg = until(lambda: srv.find(p["chat_id"], role="user", text="登录老是掉线"), what="替你发的消息")[0]
    assert msg["meta"]["need_id"] == n["id"]
    ds = dispatched(srv, p["chat_id"], 2)
    assert all(d["meta"]["need_id"] == n["id"] for d in ds[-2:])
    until(lambda: column(srv, n["id"]) == "done", what="卡片做完")
    assert len(card(srv, n["id"])["tasks"]) == 2
    assert len(all_cards(srv)) == before + 1   # 没有另开卡
    idle(srv)


# ---------- 3. 状态里的看板、update_need ----------
def test_state_block_and_update_need(srv, fake, ws):
    pj = project(srv, "OPS", "运维")
    a = srv.make_bot("Keeper")
    fake.script("router:Keeper", Call("dispatch", {"goal": "看日志", "need": "日志里的报错", "project": "OPS"}), "派了")
    evt = threading.Event()
    fake.script("worker", released(evt, "日志看完了"))
    ws.say(a["chat_id"], "看看日志")
    d = dispatched(srv, a["chat_id"])[0]
    nid = d["meta"]["need_id"]
    key = card(srv, nid)["key"]
    other = srv.post("/api/needs", {"title": "另一张", "start": False})
    holder = {"key": key, "other": other["id"]}
    fake.script("router:Keeper",
                lambda body: [Call("update_need", {"id": holder["key"], "action": "rename", "title": "日志里的报错和原因"}),
                              Call("update_need", {"id": str(holder["other"]), "action": "done"}),
                              Call("update_need", {"id": holder["key"], "action": "fly"})],
                "改好了")
    ws.say(a["chat_id"], "卡片改个名")
    until(lambda: len(fake.bodies("router", "Keeper")) >= 4, what="update_need")
    third = fake.bodies("router", "Keeper")[2]
    st = user_text(third)
    assert "看板（" in st and f"{key} [进行中] 日志里的报错（任务 #{d['id']}）" in st
    assert "update_need" in [t["function"]["name"] for t in third["tools"]]
    out = tool_text(fake.bodies("router", "Keeper")[3])
    assert "改名为「日志里的报错和原因」" in out
    assert "不是交给你的" in out   # 另一张交给主 Bot（没写交给谁）：别的 Bot 不能改
    assert "不认识 fly" in out
    assert card(srv, nid)["title"] == "日志里的报错和原因" and card(srv, nid)["auto_title"] is False
    assert card(srv, other["id"])["column"] == "todo"
    evt.set()
    until(lambda: column(srv, nid) == "done", what="做完")
    # 主 Bot 能改所有卡：merge、move、cancel
    p = primary(srv)
    x = srv.post("/api/needs", {"title": "合并来源", "body": "细节", "start": False})
    y = srv.post("/api/needs", {"title": "合并目标", "start": False})
    z = srv.post("/api/needs", {"title": "等合并来源", "after": [x["id"]], "start": False})
    fake.script(f"router:{p['name']}",
                lambda body: [Call("update_need", {"id": x["id"], "action": "merge", "into": y["id"]}),
                              Call("update_need", {"id": y["id"], "action": "move", "project": "OPS"}),
                              Call("update_need", {"id": str(holder["other"]), "action": "cancel"})],
                "处理好了")
    n0 = len(fake.bodies("router", p["name"]))
    ws.say(p["chat_id"], "整理一下卡片")
    until(lambda: len(fake.bodies("router", p["name"])) >= n0 + 2, what="主 Bot 改卡")
    assert srv.http.get(f"/api/needs/{x['id']}").status_code == 404
    yy = card(srv, y["id"])
    assert "合并来源" in yy["body"] and yy["key"].startswith("OPS-") and yy["project_id"] == pj["id"]
    assert card(srv, z["id"])["after"] == [y["id"]]
    assert card(srv, other["id"])["column"] == "cancelled"
    idle(srv)


# ---------- 5. 消息以卡片编号开头 ----------
def test_key_prefix_goes_to_task(srv, fake, ws):
    project(srv, "UK", "英国")
    a = srv.make_bot("Prefix")
    evt = threading.Event()
    fake.script("router:Prefix", Call("dispatch", {"goal": "查欧洲市场", "need": "欧洲市场报告", "project": "UK"}), "派了")
    fake.script("worker", released(evt, Call("search_memory", {"query": "欧洲"})), "报告含英国")
    ws.say(a["chat_id"], "查一下欧洲市场")
    d = dispatched(srv, a["chat_id"])[0]
    key = card(srv, d["meta"]["need_id"])["key"]
    until(lambda: fake.bodies("worker"), what="Worker 开始")
    n = len(fake.bodies("router", "Prefix"))
    ws.say(a["chat_id"], f"{key} 加上英国")
    f = until(lambda: srv.find(a["chat_id"], kind="followup"), what="给任务的话")[0]
    assert f["content"] == f"[给任务 #{d['id']} 的话]\n{key} 加上英国" and f["meta"]["task_id"] == d["id"]
    evt.set()
    until(lambda: srv.find(a["chat_id"], kind="result"), what="结果")
    assert f"{key} 加上英国" in user_text(fake.bodies("worker")[1])
    assert len(fake.bodies("router", "Prefix")) == n   # Bot 没被叫醒
    # 10. 给任务的话不算卡片的新任务
    assert len(card(srv, d["meta"]["need_id"])["tasks"]) == 1
    idle(srv)
    # 没有任务在做：照常交给 Bot
    fake.script("router:Prefix", "好的")
    ws.say(a["chat_id"], f"{key} 怎么样了")
    until(lambda: len(fake.bodies("router", "Prefix")) > n, what="Bot 被叫醒")
    idle(srv)


# ---------- 6. 任务状态变了：列跟着变，等别的卡的卡自动开始 ----------
def test_after_card_starts_with_results(srv, fake, ws):
    k = srv.make_bot("Chain")
    first = srv.post("/api/needs", {"title": "先调研", "assignee_bot_id": k["id"], "start": False})
    second = srv.post("/api/needs", {"title": "再写报告", "assignee_bot_id": k["id"], "after": [first["id"]], "start": False})
    fake.script("worker", "调研结论：市场在涨", "报告写好了")
    srv.post(f"/api/needs/{first['id']}/start")
    until(lambda: column(srv, second["id"]) == "done", timeout=15, what="后面的卡自动开始并做完")
    goal = user_text(fake.bodies("worker")[1])
    assert "再写报告" in goal and "前面几张卡的结果" in goal and "调研结论：市场在涨" in goal
    # 前面的失败：后面的不开始
    third = srv.post("/api/needs", {"title": "会失败", "assignee_bot_id": k["id"], "start": False})
    fourth = srv.post("/api/needs", {"title": "等失败的", "assignee_bot_id": k["id"], "after": [third["id"]], "start": False})
    fake.fail(400)
    srv.post(f"/api/needs/{third['id']}/start")
    until(lambda: column(srv, third["id"]) == "waiting", what="失败的卡等你处理")
    time.sleep(0.5)
    assert column(srv, fourth["id"]) == "todo" and card(srv, fourth["id"])["blocked_by"][0]["id"] == third["id"]


def test_board_events_follow_task(srv, fake, ws):
    until(lambda: any(a["name"] == "demo" and a.get("state") == "connected" for a in srv.get("/api/apps")["apps"]),
          timeout=30, what="假应用连上")
    k = srv.make_bot("Evt")
    fake.script("worker", Call("demo__create_note", {"text": "周五开会"}), "记好了")
    n = srv.post("/api/needs", {"title": "记笔记", "assignee_bot_id": k["id"], "start": True})
    approval = until(lambda: [e for e in ws.of("approval") if e.get("status") == "pending" and e["tool"] == "demo__create_note"],
                     what="确认卡片")[0]
    # 开始等你：不靠每 5 秒的兜底，马上到 waiting
    t0 = time.time()
    until(lambda: column(srv, n["id"]) == "waiting", timeout=3, what="卡片在等确认")
    assert time.time() - t0 < 3
    seen = len([e for e in ws.of("board") if e.get("need_id") == n["id"]])
    ws.send({"type": "approve", "id": approval["id"], "ok": True})
    until(lambda: column(srv, n["id"]) == "done", timeout=3, what="确认以后做完")
    until(lambda: len([e for e in ws.of("board") if e.get("need_id") == n["id"]]) > seen, timeout=3, what="board 事件")


# ---------- 10. 接着做：卡片回到进行中 ----------
def test_continue_moves_card_back_to_doing(srv, fake, ws):
    a = srv.make_bot("Again")
    fake.script("router:Again", Call("dispatch", {"goal": "写文案", "need": "一段文案"}), "派了")
    evt = threading.Event()
    fake.script("worker", "第一版", released(evt, "第二版"))
    ws.say(a["chat_id"], "写段文案")
    d = dispatched(srv, a["chat_id"])[0]
    nid = d["meta"]["need_id"]
    until(lambda: column(srv, nid) == "done", what="第一轮做完")
    srv.post(f"/api/tasks/{d['id']}/message", {"text": "改短一点"})
    until(lambda: column(srv, nid) == "doing", timeout=3, what="卡片回到进行中")
    assert len(card(srv, nid)["tasks"]) == 1
    evt.set()
    until(lambda: column(srv, nid) == "done", what="第二轮做完")
    idle(srv)


# ---------- 8. 项目记忆 ----------
def test_project_memories(srv, fake, ws):
    pj = project(srv, "MEM", "记忆项目")
    a = srv.make_bot("Rememberer")
    assert srv.get(f"/api/projects/{pj['id']}/memories") == []
    m = srv.post(f"/api/projects/{pj['id']}/memories", {"text": "这个项目的代码用 Rust 写"})
    assert m["project_id"] == pj["id"] and m["shared"] is False and m["bot_id"] is None
    old = srv.post(f"/api/projects/{pj['id']}/memories", {"text": "旧的约定已经过期", "until": "2020-01-01"})
    # until 和 PATCH /api/memories/{id} 一样：YYYY-MM-DD、秒数、null、空串
    sec = srv.post(f"/api/projects/{pj['id']}/memories", {"text": "用秒数的截止日", "until": 4102444800})
    assert sec["until"] == 4102444800 and sec["project_id"] == pj["id"]
    for v in (None, ""):
        z = srv.post(f"/api/projects/{pj['id']}/memories", {"text": f"不过期的 {v!r}", "until": v})
        assert z["until"] is None
        srv.delete(f"/api/memories/{z['id']}")
    srv.delete(f"/api/memories/{sec['id']}")
    assert all(x["project_id"] == pj["id"] for x in srv.get(f"/api/projects/{pj['id']}/memories"))
    assert srv.http.post(f"/api/projects/{pj['id']}/memories", json={"text": "坏日期", "until": "明天"}).status_code == 400
    assert srv.http.get("/api/projects/99999/memories").status_code == 404
    assert srv.http.post(f"/api/projects/{pj['id']}/memories", json={"text": " "}).status_code == 400
    # remember 带 project
    fake.script("router:Rememberer", Call("remember", {"text": "发布前要跑 cargo test", "project": "mem"}),
                Call("remember", {"text": "x", "project": "NOPE"}), "记住了")
    ws.say(a["chat_id"], "记住：这个项目发布前要跑 cargo test")
    until(lambda: len(srv.get(f"/api/projects/{pj['id']}/memories")) == 3, what="项目记忆")
    out = tool_text(until(lambda: len(fake.bodies("router", "Rememberer")) >= 3 and fake.bodies("router", "Rememberer"), what="r")[2])
    assert "项目 MEM「记忆项目」" in out and "没有项目 NOPE" in out
    # 不算共享的，也不是 Bot 自己的
    lst = srv.get(f"/api/bots/{a['id']}/memories")
    texts = [x["text"] for x in lst["shared"] + lst["own"]]
    assert "这个项目的代码用 Rust 写" not in texts and "发布前要跑 cargo test" not in texts
    # 改、删用现成的接口
    srv.patch(f"/api/memories/{m['id']}", {"text": "这个项目的代码用 Rust 2024 写", "pinned": True})
    got = {x["id"]: x for x in srv.get(f"/api/projects/{pj['id']}/memories")}
    assert got[m["id"]]["text"] == "这个项目的代码用 Rust 2024 写" and got[m["id"]]["pinned"] is True
    assert got[m["id"]]["project_id"] == pj["id"]
    idle(srv)
    # 项目的任务：Worker 读到项目记忆（过期的不读）；Router 状态里有进行中的卡所属项目的记忆
    evt = threading.Event()
    fake.script("router:Rememberer", Call("dispatch", {"goal": "发个版", "need": "新版本", "project": "MEM"}), "派了",
                "知道了")
    fake.script("worker", released(evt, "发好了"))
    ws.say(a["chat_id"], "发个版")
    d = dispatched(srv, a["chat_id"])[0]
    sysmsg = until(lambda: fake.bodies("worker"), what="Worker")[0]["messages"][0]["content"]
    assert "这个任务属于项目 MEM「记忆项目」" in sysmsg and "Rust 2024" in sysmsg and "cargo test" in sysmsg
    assert "旧的约定已经过期" not in sysmsg
    n = len(fake.bodies("router", "Rememberer"))
    ws.say(a["chat_id"], "进度怎么样")
    st = user_text(until(lambda: len(fake.bodies("router", "Rememberer")) > n and fake.bodies("router", "Rememberer"), what="r")[-1])
    assert "项目 MEM「记忆项目」的记忆" in st and "Rust 2024" in st and "旧的约定已经过期" not in st
    evt.set()
    until(lambda: srv.find(a["chat_id"], kind="result"), what="结果")
    idle(srv)
    # 不属于项目的任务读不到
    fake.script("router:Rememberer", Call("dispatch", {"goal": "查天气"}), "派了")
    fake.script("worker", "晴")
    nb = len(fake.bodies("worker"))
    ws.say(a["chat_id"], "查天气")
    sysmsg = until(lambda: len(fake.bodies("worker")) > nb and fake.bodies("worker"), what="Worker")[-1]["messages"][0]["content"]
    assert "Rust 2024" not in sysmsg
    idle(srv)
    # 整理时每个项目单独一组
    for t in ("项目约定一", "项目约定二", "项目约定三"):
        srv.post(f"/api/projects/{pj['id']}/memories", {"text": t})
    asked = []

    def reply(body):
        asked.append(body["messages"][-1]["content"])
        return json.dumps({"ops": []})

    fake.script("other", *[reply] * 30)
    srv.post("/api/memories/consolidate")
    mine = [x for x in asked if "项目 MEM「记忆项目」的记忆" in x]
    assert len(mine) == 1 and "项目约定一" in mine[0] and "Rust 2024" in mine[0].split("置顶的")[1]
    assert not any("项目约定一" in x for x in asked if x not in mine)
    assert old["id"] not in [x["id"] for x in srv.get(f"/api/projects/{pj['id']}/memories")]   # 过期的整理时删掉
    # 删项目时它的记忆一起删
    srv.delete(f"/api/projects/{pj['id']}")
    with srv.db() as c:
        assert c.execute("select count(*) from memories where project_id=?", (pj["id"],)).fetchone()[0] == 0
    assert card(srv, d["meta"]["need_id"])["project_id"] is None


# ---------- 9. counts ----------
def test_board_counts(srv, fake, ws):
    b = srv.get("/api/board")
    counts = b["counts"]
    assert set(counts) == {"todo", "doing", "waiting", "done", "cancelled"}
    for col, cards in b["columns"].items():
        assert counts[col] >= len(cards)
    srv.post("/api/needs", {"title": "数一数", "start": False})
    c2 = srv.get("/api/board", params={"limit": 1})
    assert c2["counts"]["todo"] == counts["todo"] + 1   # 不受分页影响
    assert sum(len(v) for v in c2["columns"].values()) == 1
    # 跟着筛选条件走（project、q、archived），只是不受 limit / before 影响
    pj = project(srv, "CNT", "数数")
    for t in ("甲", "乙", "丙"):
        srv.post("/api/needs", {"title": f"计数{t}", "project_id": pj["id"], "start": False})
    f = srv.get("/api/board", params={"project": pj["id"], "limit": 1})
    assert f["counts"] == {"todo": 3, "doing": 0, "waiting": 0, "done": 0, "cancelled": 0}
    assert srv.get("/api/board", params={"q": "计数乙"})["counts"]["todo"] == 1
    last = max(c["id"] for c in all_cards(srv))
    assert srv.get("/api/board", params={"project": pj["id"], "before": last})["counts"]["todo"] == 3
    one = next(c for c in all_cards(srv) if c["title"] == "计数甲")
    srv.post(f"/api/needs/{one['id']}/archive")
    assert srv.get("/api/board", params={"project": pj["id"], "archived": 1})["counts"]["todo"] == 1
    assert srv.get("/api/board", params={"project": pj["id"]})["counts"]["todo"] == 2
