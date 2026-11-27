"""任务能对话、能接着做（见 docs/plans/tasks-board.md“接口约定 → 任务”）：只有 Rust 版有。

中途补一句、任务提问（ask_user）、做完后接着做、重试、Router 的 tell_task、讨论串里回复任务、
模型服务临时出错重试、卡住先重试上一步、服务重启后接着做（正常停：等那一步做完；强杀：说不准的那一步弹卡片）。"""
import sys
import threading
import time

import pytest
import yaml

import conftest
from conftest import HERE, ROOT, Server, Socket, idle, until
from fakellm_http import Call

pytestmark = pytest.mark.skipif(conftest.IMPL != "rust", reason="任务能对话、能接着做只有 Rust 版有")

BASE_LIMITS = yaml.safe_load((ROOT / "config.yaml").read_text())["limits"]
SLOW_APP = {"slow": {"enabled": True, "command": sys.executable, "args": [str(HERE / "fake_mcp_slow.py")], "confirm": "auto"}}


def limits(**kw) -> dict:
    return {**BASE_LIMITS, **kw}


@pytest.fixture(scope="module")
def tsrv(llm):
    # 重试等 0.2 秒、3 秒没动静算卡住，测试跑得快
    s = Server(llm, config={"limits": limits(task_retry_s=[0.2, 0.2, 0.2], task_stall_s=3, watchdog_interval_s=1)})
    yield s
    s.stop()


@pytest.fixture
def tws(tsrv):
    s = Socket(tsrv)
    yield s
    s.close()


def start_task(srv, ws, fake, name: str, goal: str = "查点东西", **kw):
    bot = srv.make_bot(name)
    fake.script(f"router:{name}", Call("dispatch", {"goal": goal, **kw}), "派了")
    ws.say(bot["chat_id"], "帮我" + goal)
    task = until(lambda: srv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]
    return bot, task["id"]


def results(srv, chat_id: int, tid: int) -> list[dict]:
    return [m for m in srv.find(chat_id, kind="result") if m["meta"].get("task_id") == tid]


def user_text(body: dict) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], str))


def tool_text(body: dict) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "tool")


def detail(srv, tid: int) -> dict:
    return srv.get(f"/api/tasks/{tid}")


def released(evt: threading.Event, reply):
    """剧本里的一条：等 evt 放行再回答（模拟一步做得慢）"""
    return lambda body: (evt.wait(15), reply)[1]


def router_done(srv, chat_id: int):
    until(lambda: srv.find(chat_id, role="assistant"), what="Bot 说完这一轮")


# ---------- 中途补一句 ----------
def test_message_while_running(tsrv, fake, tws):
    evt = threading.Event()
    fake.script("worker", released(evt, Call("search_memory", {"query": "欧洲"})), "做完了：含英国")
    bot, tid = start_task(tsrv, tws, fake, "Adder", "查欧洲的数据")
    until(lambda: fake.bodies("worker"), what="Worker 开始")
    r = tsrv.post(f"/api/tasks/{tid}/message", {"text": "顺便加上英国"})
    assert r["ok"] is True and r["task"]["status"] == "running"
    assert r["task"]["can_continue"] is True and r["task"]["round"] == 1 and r["task"]["waiting_kind"] is None
    evt.set()
    until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")
    second = fake.bodies("worker")[1]
    assert "顺便加上英国" in user_text(second)
    f = tsrv.find(bot["chat_id"], kind="followup")
    assert f and f[0]["content"] == f"[给任务 #{tid} 的话]\n顺便加上英国" and f[0]["meta"]["task_id"] == tid
    assert f[0]["role"] == "task"
    steps = detail(tsrv, tid)["steps"]
    assert [s["body"] for s in steps if s["kind"] == "user"] == ["顺便加上英国"]
    # 任务的公开字段（列表、详情）
    t = tsrv.get("/api/tasks", params={"bot_id": bot["id"]})[0]
    assert t["can_continue"] is True and t["round"] == 1 and t["waiting_kind"] is None
    ev = [e for e in tws.of("task") if e["task"]["id"] == tid]
    assert all("waiting_kind" in e["task"] and "can_continue" in e["task"] and "round" in e["task"] for e in ev)


# ---------- 任务提问 ----------
def test_ask_user_card_and_answer(tsrv, fake, tws):
    fake.script("worker", Call("ask_user", {"question": "要哪个城市？", "options": ["北京", "上海"]}), "按上海查好了")
    bot, tid = start_task(tsrv, tws, fake, "Asker", "查天气")
    card = until(lambda: [e for e in tws.of("approval") if e.get("status") == "pending" and e.get("kind") == "question"],
                 what="提问卡片")[0]
    assert card["question"] == "要哪个城市？" and card["options"] == ["北京", "上海"] and card["task_id"] == tid
    assert card["chat_id"] == bot["chat_id"]
    pend = [a for a in tsrv.get("/api/bootstrap")["approvals"] if a["id"] == card["id"]]
    assert pend and pend[0]["kind"] == "question" and pend[0]["options"] == ["北京", "上海"]
    assert detail(tsrv, tid)["task"]["waiting_kind"] == "question"
    assert tsrv.get("/api/tasks", params={"bot_id": bot["id"]})[0]["waiting_kind"] == "question"
    tws.send({"type": "approve", "id": card["id"], "ok": True, "note": "上海"})
    res = until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")
    assert "按上海查好了" in res[0]["content"]
    assert "用户回答：上海" in tool_text(fake.bodies("worker")[1])
    assert detail(tsrv, tid)["task"]["waiting_kind"] is None


def test_message_answers_question(tsrv, fake, tws):
    fake.script("worker", Call("ask_user", {"question": "要几天的？"}), "查了三天")
    bot, tid = start_task(tsrv, tws, fake, "Asker2", "查天气预报")
    card = until(lambda: [e for e in tws.of("approval") if e.get("status") == "pending" and e.get("task_id") == tid],
                 what="提问卡片")[0]
    assert card["kind"] == "question" and card["options"] == []
    r = tsrv.post(f"/api/tasks/{tid}/message", {"text": "三天"})
    assert r["task"]["status"] == "running"
    until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")
    assert "用户回答：三天" in tool_text(fake.bodies("worker")[1])
    assert tsrv.find(bot["chat_id"], kind="followup")


def test_question_refused(tsrv, fake, tws):
    fake.script("worker", Call("ask_user", {"question": "要不要发？"}), "没发")
    bot, tid = start_task(tsrv, tws, fake, "Asker3", "看情况发消息")
    card = until(lambda: [e for e in tws.of("approval") if e.get("status") == "pending" and e.get("task_id") == tid],
                 what="提问卡片")[0]
    tws.send({"type": "approve", "id": card["id"], "ok": False})
    until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")
    assert "用户没有回答" in tool_text(fake.bodies("worker")[1])


# ---------- 做完以后接着做 ----------
def test_continue_after_done(tsrv, fake, tws):
    fake.script("worker", "第一版结果", "Second version")
    bot, tid = start_task(tsrv, tws, fake, "Again", "写一段介绍")
    until(lambda: results(tsrv, bot["chat_id"], tid), what="第一条结果")
    idle(tsrv)
    t = detail(tsrv, tid)["task"]
    assert t["status"] == "done" and t["can_continue"] is True and t["round"] == 1
    r = tsrv.post(f"/api/tasks/{tid}/message", {"text": "改成英文"})
    assert r["ok"] is True and r["task"]["status"] == "running" and r["task"]["round"] == 2
    res = until(lambda: len(results(tsrv, bot["chat_id"], tid)) >= 2 and results(tsrv, bot["chat_id"], tid), what="第二条结果")
    assert res[0]["meta"]["round"] == 1 and res[1]["meta"]["round"] == 2 and "Second version" in res[1]["content"]
    idle(tsrv)
    t = detail(tsrv, tid)["task"]
    assert t["status"] == "done" and t["round"] == 2 and t["result"] == "Second version"
    body = fake.bodies("worker")[1]
    assert any(m["role"] == "assistant" and m.get("content") == "第一版结果" for m in body["messages"])
    assert "改成英文" in user_text(body)
    assert tsrv.find(bot["chat_id"], kind="followup")[0]["content"] == f"[给任务 #{tid} 的话]\n改成英文"


def test_message_errors(tsrv, fake, tws):
    assert tsrv.http.post("/api/tasks/99999999/message", json={"text": "在吗"}).status_code == 404
    assert tsrv.http.post("/api/tasks/99999999/retry").status_code == 404
    # Heavy 模式不存过程：不能接着做
    bot, tid = start_task(tsrv, tws, fake, "Heavy", "难题", mode="heavy")
    until(lambda: results(tsrv, bot["chat_id"], tid), timeout=20, what="Heavy 结果")
    idle(tsrv)
    assert detail(tsrv, tid)["task"]["can_continue"] is False
    r = tsrv.http.post(f"/api/tasks/{tid}/message", json={"text": "再想想"})
    assert r.status_code == 400 and "Heavy" in r.json()["detail"]
    assert tsrv.http.post(f"/api/tasks/{tid}/message", json={"text": "  "}).status_code == 400
    # 做完的任务不用重试
    r = tsrv.http.post(f"/api/tasks/{tid}/retry")
    assert r.status_code == 400


# ---------- 重试 ----------
def test_retry_failed(tsrv, fake, tws):
    evt = threading.Event()
    fake.script("worker", released(evt, Call("search_memory", {"query": "资料"})), "重试后做完了")
    bot, tid = start_task(tsrv, tws, fake, "Retrier", "整理资料")
    router_done(tsrv, bot["chat_id"])
    r = tsrv.http.post(f"/api/tasks/{tid}/retry")
    assert r.status_code == 400   # 还在做
    fake.fail(400)                # 第二步：模型服务报 400（不是临时的错），任务失败
    evt.set()
    res = until(lambda: results(tsrv, bot["chat_id"], tid), what="失败的结果")
    assert res[0]["meta"]["status"] == "failed"
    idle(tsrv)
    r = tsrv.post(f"/api/tasks/{tid}/retry")
    assert r["ok"] is True and r["task"]["status"] == "running" and r["task"]["round"] == 2
    res = until(lambda: len(results(tsrv, bot["chat_id"], tid)) >= 2 and results(tsrv, bot["chat_id"], tid), what="重试的结果")
    assert res[1]["meta"]["status"] == "done" and "重试后做完了" in res[1]["content"]
    body = fake.bodies("worker")[-1]
    assert "上次没做完" in user_text(body) and "search_memory" in str(body["messages"])   # 从停下的地方接着做
    assert detail(tsrv, tid)["task"]["status"] == "done"


def test_transient_errors_retried(tsrv, fake, tws):
    evt = threading.Event()
    fake.script("worker", released(evt, Call("search_memory", {"query": "x"})), "最后做完了")
    bot, tid = start_task(tsrv, tws, fake, "Flaky", "查点东西")
    router_done(tsrv, bot["chat_id"])
    fake.fail_now(503, 500, 429)
    evt.set()
    res = until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")
    assert res[0]["meta"]["status"] == "done" and "最后做完了" in res[0]["content"]
    notes = [s["title"] for s in detail(tsrv, tid)["steps"] if s["kind"] == "note"]
    assert sum("临时出错" in n for n in notes) == 3


def test_transient_errors_give_up(tsrv, fake, tws):
    evt = threading.Event()
    fake.script("worker", released(evt, Call("search_memory", {"query": "x"})), "不会走到这里")
    bot, tid = start_task(tsrv, tws, fake, "Flaky2", "查点东西")
    router_done(tsrv, bot["chat_id"])
    fake.fail_now(503, 503, 503, 503)   # 第四次还不行：失败
    evt.set()
    res = until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")
    assert res[0]["meta"]["status"] == "failed" and "503" in res[0]["content"]


# ---------- 卡住 ----------
def test_stall_retries_last_step_once(tsrv, fake, tws):
    fake.script("worker", lambda body: (time.sleep(6), "太慢了")[1], "重试后做完了")
    bot, tid = start_task(tsrv, tws, fake, "Staller", "慢慢查")
    res = until(lambda: results(tsrv, bot["chat_id"], tid), timeout=20, what="结果")
    assert res[0]["meta"]["status"] == "done" and "重试后做完了" in res[0]["content"]
    notes = [s["title"] for s in detail(tsrv, tid)["steps"] if s["kind"] == "note"]
    assert any("从上一步重试" in n for n in notes)


def test_stall_twice_cancels(tsrv, fake, tws):
    fake.script("worker", lambda body: (time.sleep(6), "慢")[1], lambda body: (time.sleep(6), "还是慢")[1])
    bot, tid = start_task(tsrv, tws, fake, "Staller2", "慢慢查")
    until(lambda: [m for m in tsrv.find(bot["chat_id"], kind="cancel") if m["meta"]["task_id"] == tid], timeout=20, what="取消")
    assert detail(tsrv, tid)["task"]["status"] == "cancelled"
    assert "Watchdog" in tsrv.find(bot["chat_id"], kind="cancel")[0]["content"]
    time.sleep(5)   # 等假模型那边睡完，别影响后面的测试


# ---------- Router 的 tell_task ----------
def test_tell_task(tsrv, fake, tws):
    holder = []
    bot = tsrv.make_bot("Teller")
    fake.script("router:Teller", Call("dispatch", {"goal": "写一段文案"}), "派了",
                lambda body: Call("tell_task", {"task_id": holder[0], "text": "结尾再加一句口号"}), "已经转给任务了")
    fake.script("worker", "文案初稿", "文案定稿（带口号）")
    tws.say(bot["chat_id"], "写一段文案")
    tid = until(lambda: tsrv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
    holder.append(tid)
    until(lambda: results(tsrv, bot["chat_id"], tid), what="第一条结果")
    idle(tsrv)
    n = len(tsrv.find(bot["chat_id"], role="assistant"))
    tws.say(bot["chat_id"], "结尾再加一句口号")
    res = until(lambda: len(results(tsrv, bot["chat_id"], tid)) >= 2 and results(tsrv, bot["chat_id"], tid), what="第二条结果")
    assert "文案定稿" in res[1]["content"] and res[1]["meta"]["round"] == 2
    bodies = fake.bodies("router", "Teller")
    third = bodies[2]
    # 状态里列着刚结束、还能接着说的任务；工具里有 tell_task
    assert f"#{tid} [done]" in user_text(third) and "tell_task" in user_text(third)
    assert "tell_task" in [t["function"]["name"] for t in third["tools"]]
    assert "已让它带着这句话接着做" in tool_text(bodies[3])
    until(lambda: len(tsrv.find(bot["chat_id"], role="assistant")) > n, what="Bot 回复")
    assert len(tsrv.find(bot["chat_id"], kind="dispatch")) == 1   # 没有另派
    assert "结尾再加一句口号" in user_text(fake.bodies("worker")[1])


def test_tell_task_running_and_foreign(tsrv, fake, tws):
    evt = threading.Event()
    holder = []
    bot = tsrv.make_bot("Teller2")
    other = tsrv.make_bot("Teller3")
    fake.script("router:Teller2", Call("dispatch", {"goal": "慢慢写"}), "派了",
                lambda body: [Call("tell_task", {"task_id": holder[0], "text": "用短句"}),
                              Call("tell_task", {"task_id": 99999999, "text": "x"})], "转了")
    fake.script("worker", released(evt, Call("search_memory", {"query": "x"})), "写好了")
    tws.say(bot["chat_id"], "慢慢写")
    tid = until(lambda: tsrv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
    holder.append(tid)
    router_done(tsrv, bot["chat_id"])
    tws.say(bot["chat_id"], "用短句")
    until(lambda: len(fake.bodies("router", "Teller2")) >= 4, what="Router 转话")
    out = tool_text(fake.bodies("router", "Teller2")[3])
    assert "它做下一步之前会看到" in out and "没有任务 #99999999" in out
    evt.set()
    until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")
    assert "用短句" in user_text(fake.bodies("worker")[1])
    # 别的 Bot 不能转给不是它派的任务
    fake.script("router:Teller3", Call("tell_task", {"task_id": tid, "text": "x"}), "好")
    tws.say(other["chat_id"], "x")
    until(lambda: len(fake.bodies("router", "Teller3")) >= 2, what="别的 Bot")
    assert "不是你派的" in tool_text(fake.bodies("router", "Teller3")[1])


# ---------- 讨论串里回复任务 ----------
def test_thread_reply_goes_to_task(tsrv, fake, tws):
    fake.script("worker", "第一版", "讨论串里的第二版")
    bot, tid = start_task(tsrv, tws, fake, "Threader", "写个提纲")
    res = until(lambda: results(tsrv, bot["chat_id"], tid), what="结果")[0]
    idle(tsrv)
    th = tsrv.post(f"/api/chats/{bot['chat_id']}/threads", {"root": res["id"]})["id"]
    n = len(fake.bodies("router", "Threader"))
    tws.say(th, "再补充一点细节")
    r2 = until(lambda: [m for m in tsrv.find(th, kind="result") if m["meta"]["task_id"] == tid], what="讨论串里的结果")
    assert "讨论串里的第二版" in r2[0]["content"] and r2[0]["meta"]["round"] == 2
    assert len(fake.bodies("router", "Threader")) == n   # Bot 没被叫醒
    f = tsrv.find(th, kind="followup")
    assert f and f[0]["content"].endswith("再补充一点细节")
    assert "再补充一点细节" in user_text(fake.bodies("worker")[1])
    # 派发消息下面的讨论串也一样
    fake.script("worker", "第三版")
    disp = tsrv.find(bot["chat_id"], kind="dispatch")[0]
    th2 = tsrv.post(f"/api/chats/{bot['chat_id']}/threads", {"root": disp["id"]})["id"]
    tws.say(th2, "再来一版")
    until(lambda: [m for m in tsrv.find(th2, kind="result") if m["meta"]["task_id"] == tid], what="第二个讨论串里的结果")
    assert len(fake.bodies("router", "Threader")) == n


def test_thread_on_plain_message_wakes_bot(tsrv, fake, tws):
    bot = tsrv.make_bot("Plain")
    fake.script("router:Plain", "你好", "讨论串里回答")
    tws.say(bot["chat_id"], "嗨")
    msg = until(lambda: tsrv.find(bot["chat_id"], role="assistant"), what="回复")[0]
    th = tsrv.post(f"/api/chats/{bot['chat_id']}/threads", {"root": msg["id"]})["id"]
    tws.say(th, "接着聊")
    until(lambda: tsrv.find(th, role="assistant"), what="讨论串里的回复")


# ---------- 服务重启 ----------
def wait_app(srv, name="slow"):
    until(lambda: any(a["name"] == name and a.get("state") == "connected" for a in srv.get("/api/apps")["apps"]),
          timeout=30, what="假应用连上")


def lines(path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_graceful_stop_finishes_step(llm):
    llm.reset()
    srv = Server(llm, config={"limits": limits(shutdown_wait_s=20), "mcp_servers": SLOW_APP})
    try:
        wait_app(srv)
        ws = Socket(srv)
        log = srv.tmp / "slow.log"
        bot = srv.make_bot("Stopper")
        llm.script("router:Stopper", Call("dispatch", {"goal": "慢慢查"}), "派了")
        llm.script("worker", Call("slow__slow_lookup", {"seconds": 2.5, "log": str(log)}), "重启后做完了")
        ws.say(bot["chat_id"], "慢慢查")
        tid = until(lambda: srv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
        until(lambda: "lookup-start" in lines(log), what="工具开始执行")
        ws.close()
        took = srv.restart()   # SIGTERM：等这一步做完、存好才退出
        assert took >= 1.5, took
        assert lines(log) == ["lookup-start", "lookup-done"]
        res = until(lambda: results(srv, bot["chat_id"], tid), timeout=20, what="重启后的结果")
        assert "重启后做完了" in res[0]["content"] and res[0]["meta"]["status"] == "done"
        assert lines(log).count("lookup-start") == 1   # 没有重做
        body = llm.bodies("worker")[-1]
        assert "查到了：一切正常" in tool_text(body) and "服务重启过" in user_text(body)
        assert "不知道有没有做成" not in tool_text(body)
        assert not srv.find(bot["chat_id"], kind="cancel")
        with srv.db() as c:
            assert c.execute("select json_extract(meta, '$.status') from messages where id=?", (tid,)).fetchone()[0] == "done"
    finally:
        srv.stop()
        llm.reset()


def test_killed_mid_write_asks_before_redo(llm):
    llm.reset()
    srv = Server(llm, config={"limits": limits(), "mcp_servers": SLOW_APP})
    ws = None
    try:
        wait_app(srv)
        srv.post("/api/rules", {"pattern": "slow__slow_append", "action": "allow"})   # 平常不用确认
        log = srv.tmp / "slow.log"
        for choice in ("redo", "done", "stop"):
            llm.reset()
            ws = Socket(srv)
            bot = srv.make_bot(f"Killer-{choice}")
            llm.script(f"router:Killer-{choice}", Call("dispatch", {"goal": "追加一行记录"}), "派了")
            llm.script("worker", Call("slow__slow_append", {"text": f"{choice}-1", "seconds": 10, "log": str(log)}),
                       Call("slow__slow_append", {"text": f"{choice}-2", "seconds": 0.1, "log": str(log)}), "收尾了")
            ws.say(bot["chat_id"], "追加一行记录")
            tid = until(lambda: srv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
            until(lambda: f"append-start {choice}-1" in lines(log), what="写操作开始执行")
            ws.close()
            srv.restart(kill=True)   # 强杀：那一步做了一半
            ws = Socket(srv)
            wait_app(srv)
            # 卡片可能在网页连上之前就弹了：看待确认列表
            card = until(lambda: [a for a in srv.get("/api/bootstrap")["approvals"] if a["task_id"] == tid],
                         timeout=20, what="说不准的那一步再做时弹卡片")[0]
            # 即使有“总是允许”的规则也问；卡片上写明是重启时正在做的那一步
            assert card["tool"] == "slow__slow_append" and card["scopes"] == ["once"]
            d = card["doubt"]
            assert d["tool"] == "slow__slow_append" and f"{choice}-1" in d["args"] and d["started"] > 0 and "checked" in d
            assert f"{choice}-2" in card["args"]
            body = llm.bodies("worker")[1]   # 重启后的第一次请求
            assert "不知道有没有做成" in tool_text(body) and "slow__slow_append" in tool_text(body)
            if choice == "redo":
                ws.send({"type": "approve", "id": card["id"], "ok": True})
                res = until(lambda: results(srv, bot["chat_id"], tid), timeout=20, what="结果")
                assert "收尾了" in res[0]["content"] and "又做了一次" in res[0]["content"]
                assert f"append-done {choice}-2" in lines(log)
                assert "已追加：redo-2" in tool_text(llm.bodies("worker")[-1])
            elif choice == "done":
                ws.send({"type": "approve", "id": card["id"], "ok": False, "note": "done"})
                res = until(lambda: results(srv, bot["chat_id"], tid), timeout=20, what="结果")
                assert "之前已经做成" in res[0]["content"]
                assert "用户确认这一步之前已经做成" in tool_text(llm.bodies("worker")[-1])
                assert f"append-start {choice}-2" not in lines(log)
            else:
                ws.send({"type": "approve", "id": card["id"], "ok": False, "note": "stop"})
                until(lambda: [m for m in srv.find(bot["chat_id"], kind="cancel") if m["meta"]["task_id"] == tid], what="任务停下")
                assert detail(srv, tid)["task"]["status"] == "cancelled"
                assert f"append-start {choice}-2" not in lines(log)
            idle(srv)
            ws.close()
            ws = None
    finally:
        if ws:
            ws.close()
        srv.stop()
        llm.reset()


@pytest.mark.parametrize("second", ["kill", "term"])
def test_restart_twice_still_asks(llm, second):
    """强杀在写操作当中 → 起来，模型收到“不知道有没有做成” → 它再调这个工具之前又重启一次（这时没有开着的步，清单只能从存下的来）
    → 起来后再调同一个工具，照样弹带 doubt 的卡片 → 选“已经做成了”，写操作总共只执行了一次。"""
    llm.reset()
    # 正常停最多等 2 秒：第二次重启时模型那一步卡着不回来
    srv = Server(llm, config={"limits": limits(shutdown_wait_s=2), "mcp_servers": SLOW_APP})
    evt = threading.Event()
    ws = None
    try:
        wait_app(srv)
        srv.post("/api/rules", {"pattern": "slow__slow_append", "action": "allow"})
        log = srv.tmp / "slow.log"
        bot = srv.make_bot("Twice")
        llm.script("router:Twice", Call("dispatch", {"goal": "追加一行记录"}), "派了")
        llm.script("worker",
                   Call("slow__slow_append", {"text": "w-1", "seconds": 10, "log": str(log)}),
                   released(evt, Call("slow__slow_append", {"text": "lost", "seconds": 0.1, "log": str(log)})),  # 第二次重启时丢掉
                   Call("slow__slow_append", {"text": "w-2", "seconds": 0.1, "log": str(log)}),
                   "收尾了")
        ws = Socket(srv)
        ws.say(bot["chat_id"], "追加一行记录")
        tid = until(lambda: srv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
        until(lambda: "append-start w-1" in lines(log), what="写操作开始执行")
        ws.close()
        ws = None
        srv.restart(kill=True)
        until(lambda: len(llm.bodies("worker")) >= 2, timeout=20, what="重启后模型收到说不准的结果")
        assert "不知道有没有做成" in tool_text(llm.bodies("worker")[1])
        with srv.db() as c:
            saved = c.execute("select doubts from task_runs where task_id=?", (tid,)).fetchone()[0]
        assert "slow__slow_append" in saved and '"settled": null' in saved
        took = srv.restart(kill=second == "kill")   # 模型还没回来（它下一步本来要再调这个工具）
        if second == "term":
            assert took < 10, took
        wait_app(srv)
        card = until(lambda: [a for a in srv.get("/api/bootstrap")["approvals"] if a["task_id"] == tid],
                     timeout=20, what="再调同一个工具时照样弹卡片")[0]
        assert card["tool"] == "slow__slow_append" and card["scopes"] == ["once"] and "w-2" in card["args"]
        assert "w-1" in card["doubt"]["args"] and card["doubt"]["tool"] == "slow__slow_append"
        body = llm.bodies("worker")[2]   # 第二次重启后：说不准的结果还在存下的过程里
        assert "不知道有没有做成" in tool_text(body)
        ws = Socket(srv)
        ws.send({"type": "approve", "id": card["id"], "ok": False, "note": "done"})
        res = until(lambda: results(srv, bot["chat_id"], tid), timeout=20, what="结果")
        assert "收尾了" in res[0]["content"] and "之前已经做成" in res[0]["content"]
        assert res[0]["content"].count("说不准") == 1
        assert "用户确认这一步之前已经做成" in tool_text(llm.bodies("worker")[-1])
        starts = [x for x in lines(log) if x.startswith("append-start")]
        assert starts == ["append-start w-1"], starts   # 写操作总共只执行了一次
        idle(srv)
        # 了结并注明过的不再带到下一轮
        llm.script("worker", "第二轮做完了")
        srv.post(f"/api/tasks/{tid}/message", {"text": "再看一眼"})
        res = until(lambda: len(results(srv, bot["chat_id"], tid)) >= 2 and results(srv, bot["chat_id"], tid), what="第二轮结果")
        assert "说不准" not in res[1]["content"]
    finally:
        evt.set()
        if ws:
            ws.close()
        srv.stop()
        llm.reset()


def test_unsettled_doubt_carried_to_next_round(llm):
    """说不准的那一步没了结就做完了这一轮：接着做的下一轮再调同一个工具，照样先问"""
    llm.reset()
    srv = Server(llm, config={"limits": limits(), "mcp_servers": SLOW_APP})
    ws = None
    try:
        wait_app(srv)
        srv.post("/api/rules", {"pattern": "slow__slow_append", "action": "allow"})
        log = srv.tmp / "slow.log"
        bot = srv.make_bot("Carry")
        llm.script("router:Carry", Call("dispatch", {"goal": "追加一行记录"}), "派了")
        llm.script("worker", Call("slow__slow_append", {"text": "c-1", "seconds": 10, "log": str(log)}),
                   "不确定有没有写进去，先停在这",
                   Call("slow__slow_append", {"text": "c-2", "seconds": 0.1, "log": str(log)}), "第二轮收尾")
        ws = Socket(srv)
        ws.say(bot["chat_id"], "追加一行记录")
        tid = until(lambda: srv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
        until(lambda: "append-start c-1" in lines(log), what="写操作开始执行")
        ws.close()
        ws = None
        srv.restart(kill=True)   # 起来后不等应用连上：恢复的任务自己等应用连好再接着做
        res = until(lambda: results(srv, bot["chat_id"], tid), timeout=20, what="第一轮结果")
        assert "之后没有再做这一步" in res[0]["content"]
        idle(srv)
        srv.post(f"/api/tasks/{tid}/message", {"text": "再写一次试试"})
        card = until(lambda: [a for a in srv.get("/api/bootstrap")["approvals"] if a["task_id"] == tid],
                     timeout=20, what="下一轮再调时照样先问")[0]
        assert "c-1" in card["doubt"]["args"] and card["scopes"] == ["once"]
        ws = Socket(srv)
        ws.send({"type": "approve", "id": card["id"], "ok": True})
        res = until(lambda: len(results(srv, bot["chat_id"], tid)) >= 2 and results(srv, bot["chat_id"], tid), timeout=20,
                    what="第二轮结果")
        assert "又做了一次" in res[1]["content"] and "append-done c-2" in lines(log)
    finally:
        if ws:
            ws.close()
        srv.stop()
        llm.reset()


# ---------- 卡住、用户停下时没做完的写操作（wire 补充说明 1） ----------
@pytest.fixture(scope="module")
def ssrv(llm):
    # 2 秒没动静算卡住；假应用的慢工具；写操作平常不用确认
    s = Server(llm, config={"limits": limits(task_stall_s=2, watchdog_interval_s=1), "mcp_servers": SLOW_APP})
    wait_app(s)
    s.post("/api/rules", {"pattern": "slow__slow_append", "action": "allow"})
    yield s
    s.stop()


def test_stalled_write_becomes_doubt(ssrv, fake):
    ws = Socket(ssrv)
    try:
        log = ssrv.tmp / "stall.log"
        bot = ssrv.make_bot("StallWrite")
        fake.script("router:StallWrite", Call("dispatch", {"goal": "追加一行"}), "派了")
        fake.script("worker", Call("slow__slow_append", {"text": "s-1", "seconds": 6, "log": str(log)}),
                    Call("slow__slow_append", {"text": "s-2", "seconds": 0.1, "log": str(log)}), "收尾了")
        ws.say(bot["chat_id"], "追加一行")
        tid = until(lambda: ssrv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
        card = until(lambda: [a for a in ssrv.get("/api/bootstrap")["approvals"] if a["task_id"] == tid], timeout=15,
                     what="卡住后再调同一个工具照样弹卡片")[0]
        assert card["doubt"]["why"] == "stall" and "s-1" in card["doubt"]["args"] and card["scopes"] == ["once"]
        body = fake.bodies("worker")[1]
        assert "卡住超时" in tool_text(body) and "不知道有没有做成" in tool_text(body)
        ws.send({"type": "approve", "id": card["id"], "ok": False, "note": "done"})
        res = until(lambda: results(ssrv, bot["chat_id"], tid), timeout=15, what="结果")
        assert res[0]["meta"]["status"] == "done" and "卡住超时被打断" in res[0]["content"] and "之前已经做成" in res[0]["content"]
        assert "用户确认这一步之前已经做成" in tool_text(fake.bodies("worker")[-1])
        assert [x for x in lines(log) if x.startswith("append-start")] == ["append-start s-1"]
        notes = [s["title"] for s in detail(ssrv, tid)["steps"] if s["kind"] == "note"]
        assert any("做完的留着" in n for n in notes)
    finally:
        ws.close()


def test_stopped_write_then_retry_asks(ssrv, fake):
    ws = Socket(ssrv)
    try:
        log = ssrv.tmp / "stop.log"
        bot = ssrv.make_bot("StopWrite")
        fake.script("router:StopWrite", Call("dispatch", {"goal": "追加一行"}), "派了")
        # 1.5 秒：比卡住的时限短，不会被当成卡住
        fake.script("worker", Call("slow__slow_append", {"text": "t-1", "seconds": 1.5, "log": str(log)}),
                    Call("slow__slow_append", {"text": "t-2", "seconds": 0.1, "log": str(log)}), "收尾了")
        ws.say(bot["chat_id"], "追加一行")
        tid = until(lambda: ssrv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
        until(lambda: "append-start t-1" in lines(log), what="写操作开始执行")
        ssrv.post(f"/api/tasks/{tid}/cancel")
        until(lambda: detail(ssrv, tid)["task"]["status"] == "cancelled", what="停下")
        notes = [s["title"] for s in detail(ssrv, tid)["steps"] if s["kind"] == "note"]
        assert any("任务停下时 slow__slow_append 正在执行" in n for n in notes)
        r = ssrv.post(f"/api/tasks/{tid}/retry")
        assert r["task"]["status"] == "running"
        card = until(lambda: [a for a in ssrv.get("/api/bootstrap")["approvals"] if a["task_id"] == tid], timeout=15,
                     what="重试后再调同一个工具照样弹卡片")[0]
        assert card["doubt"]["why"] == "stopped" and "t-1" in card["doubt"]["args"]
        assert "没有做完任务就被停下了" in tool_text(fake.bodies("worker")[1])
        ws.send({"type": "approve", "id": card["id"], "ok": False, "note": "done"})
        res = until(lambda: results(ssrv, bot["chat_id"], tid), timeout=15, what="结果")
        assert "任务被停下时 slow__slow_append" in res[-1]["content"]
        assert [x for x in lines(log) if x.startswith("append-start")] == ["append-start t-1"]
    finally:
        ws.close()


def test_stalled_read_only_retries_once(ssrv, fake):
    ws = Socket(ssrv)
    try:
        log = ssrv.tmp / "lookup.log"
        bot = ssrv.make_bot("StallRead")
        fake.script("router:StallRead", Call("dispatch", {"goal": "慢慢查"}), "派了")
        fake.script("worker", Call("slow__slow_lookup", {"seconds": 5, "log": str(log)}), "不会走到这里")
        ws.say(bot["chat_id"], "慢慢查")
        tid = until(lambda: ssrv.find(bot["chat_id"], kind="dispatch"), what="派发")[0]["id"]
        until(lambda: [m for m in ssrv.find(bot["chat_id"], kind="cancel") if m["meta"]["task_id"] == tid], timeout=20,
              what="重做一次还卡住才取消")
        assert lines(log).count("lookup-start") == 2   # 只读的：重做了一次
        assert len(fake.bodies("worker")) == 1   # 没有当成说不准、没有再问模型
    finally:
        ws.close()
    time.sleep(4)   # 等假应用那边睡完


def test_many_tasks_ending_together_do_not_hang(tsrv, tws, fake):
    """几个任务差不多同时结束时服务不能卡死：tasklog 里结束一步（先锁这一步、再锁 LIVE）和关掉一个任务（以前是拿着 LIVE 去锁每一步）
    锁的先后相反，两边撞上就互相等死，之后所有接口都不回。"""
    bot = tsrv.make_bot("Crowd")
    n, rounds = 8, 6
    for r in range(rounds):
        fake.script("router:Crowd", *[Call("dispatch", {"goal": f"第 {r} 轮第 {i} 件"}) for i in range(n)], "派了")
        fake.script("worker", *[Call("demo__search_notes", {"query": "x"}), "好"] * n)
        tws.say(bot["chat_id"], f"第 {r} 轮，一次派 {n} 个")
        until(lambda: len(tsrv.find(bot["chat_id"], kind="dispatch")) == (r + 1) * n, 20, "这一轮的任务都派出去")
        try:
            idle(tsrv, 20)
        except AssertionError:
            pass   # 卡死的话下面的请求会超时，报错更清楚
        assert tsrv.http.get("/api/bootstrap", timeout=5).status_code == 200, "服务不回了：任务同时结束时卡死"
        fake.reset()
    # 剧本是几个任务共用的一个队列，有的任务会连着拿到几次“调工具”，被 Watchdog 当成死循环取消：这也算结束了，这里只查没有卡着不动的
    ended = [m for m in tsrv.find(bot["chat_id"], kind="dispatch") if m["meta"]["status"] in ("done", "failed", "cancelled")]
    assert len(ended) == n * rounds, f"{len(ended)} / {n * rounds} 个任务结束了"


def test_card_numbers_continue_after_restart(llm, fake):
    """确认卡片的编号重启后接着往下数，不从 1 重来：重启前的旧卡片（聊天软件里还留着，钉钉是打字回编号）不能对上重启后另一张同号的卡片。"""
    srv = Server(llm, config={"limits": limits(shutdown_wait_s=1)})
    ws = Socket(srv)
    try:
        fake.script("worker", Call("ask_user", {"question": "要哪个城市？"}), "按回答做好了")
        bot, tid = start_task(srv, ws, fake, "Numbered", "问一下再做")
        pending = lambda: [a for a in srv.get("/api/bootstrap")["approvals"] if a["task_id"] == tid]
        old = until(pending, what="提问卡片")[0]["id"]
        ws.close()
        srv.restart(kill=True)
        ws = Socket(srv)
        new = until(pending, 30, "重启后任务接着做，重新问")[0]["id"]
        assert new > old, f"重启后的卡片 #{new} 和重启前的 #{old} 撞号了"
        ws.send({"type": "approve", "id": old, "ok": True, "note": "答在旧卡片上"})   # 旧编号：不该算数
        time.sleep(0.6)
        assert [a["id"] for a in pending()] == [new] and not results(srv, bot["chat_id"], tid)
        ws.send({"type": "approve", "id": new, "ok": True, "note": "上海"})
        until(lambda: results(srv, bot["chat_id"], tid), 30, "结果")
        assert "用户回答：上海" in tool_text(fake.bodies("worker")[-1]) and "答在旧卡片上" not in tool_text(fake.bodies("worker")[-1])
    finally:
        ws.close()
        srv.stop()
