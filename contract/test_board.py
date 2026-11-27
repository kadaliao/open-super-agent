"""需求看板和项目（只有 Rust 版有）：项目、卡片、列由任务算出来、开始/完成/取消、移项目重新编号、搜索筛选归档、board 事件。"""
import time

import pytest

from conftest import IMPL, idle, until
from fakellm_http import Call

pytestmark = pytest.mark.skipif(IMPL != "rust", reason="看板只做在 Rust 版")


def card(srv, nid: int) -> dict:
    return srv.get(f"/api/needs/{nid}")


def column(srv, nid: int) -> str:
    return card(srv, nid)["column"]


def in_board(srv, nid: int, **params) -> str | None:
    b = srv.get("/api/board", params=params)
    for col, cards in b["columns"].items():
        if any(c["id"] == nid for c in cards):
            return col
    return None


def need(srv, title: str, **kw) -> dict:
    return srv.post("/api/needs", {"title": title, "start": False, **kw})


def project(srv, key: str, name: str = "", **kw) -> dict:
    return srv.post("/api/projects", {"key": key, "name": name or f"{key} 项目", **kw})


def test_projects_crud_and_key(srv):
    p = project(srv, "opc", "一人公司", brief="官网和产品", dir="/srv/opc", repo="git@example.com:opc.git")
    assert p["key"] == "OPC" and p["name"] == "一人公司" and p["brief"] == "官网和产品" and p["count"] == 0
    assert p["dir"] == "/srv/opc" and p["repo"] == "git@example.com:opc.git" and p["created"]
    for bad in ("A", "TOOLONGKEY", "A-B", "中文", ""):
        r = srv.http.post("/api/projects", json={"key": bad, "name": "x"})
        assert r.status_code == 400 and r.json()["detail"], bad
    assert srv.http.post("/api/projects", json={"key": "OPC", "name": "重复"}).status_code == 400
    assert srv.http.post("/api/projects", json={"key": "NONAME", "name": " "}).status_code == 400
    q = project(srv, "Q2")
    assert srv.http.patch(f"/api/projects/{q['id']}", json={"key": "opc"}).status_code == 400   # 编号重复
    u = srv.patch(f"/api/projects/{q['id']}", {"name": "第二季度", "brief": "季度目标", "key": "Q2X"})
    assert u["name"] == "第二季度" and u["brief"] == "季度目标" and u["key"] == "Q2X"
    assert srv.http.patch("/api/projects/999999", json={"name": "x"}).status_code == 404
    n = need(srv, "季度复盘", project_id=q["id"])
    assert n["key"] == "Q2X-1" and n["no"] == 1
    lst = srv.get("/api/projects")
    assert next(x for x in lst if x["id"] == q["id"])["count"] == 1
    assert {"OPC", "Q2X"} <= {x["key"] for x in lst}
    # 删项目：卡片留下，变成不属于项目
    assert srv.delete(f"/api/projects/{q['id']}") == {"ok": True}
    c = card(srv, n["id"])
    assert c["project_id"] is None and c["key"] is None and c["no"] is None
    assert q["id"] not in [x["id"] for x in srv.get("/api/projects")]
    assert srv.http.delete(f"/api/projects/{q['id']}").status_code == 404


def test_card_fields_and_validation(srv):
    assert srv.http.post("/api/needs", json={"title": "  "}).status_code == 400
    assert srv.http.post("/api/needs", json={"title": "x", "project_id": 999999}).status_code == 400
    assert srv.http.post("/api/needs", json={"title": "x", "assignee_bot_id": 999999}).status_code == 400
    assert srv.http.post("/api/needs", json={"title": "x", "after": [999999]}).status_code == 400
    assert srv.http.get("/api/needs/999999").status_code == 404
    assert srv.http.post("/api/needs/999999/start").status_code == 404
    a = need(srv, "  写一份   竞品分析 ", body="看三家")
    assert a["title"] == "写一份 竞品分析" and a["body"] == "看三家" and a["column"] == "todo"
    for k in ("id", "key", "no", "project_id", "title", "auto_title", "body", "source_text", "column", "assignee_bot_id",
              "after", "blocked_by", "tasks", "progress", "latest", "created", "updated", "archived", "origin"):
        assert k in a, k
    assert a["key"] is None and a["auto_title"] is False and a["tasks"] == [] and a["progress"] == {"done": 0, "total": 0}
    assert a["origin"] is None and a["archived"] is False and a["after"] == [] and a["assignee_bot_id"] is None
    b = need(srv, "根据竞品分析定价", after=[a["id"]])
    assert b["after"] == [a["id"]]
    assert b["blocked_by"] == [{"id": a["id"], "key": None, "title": "写一份 竞品分析", "column": "todo"}]
    assert srv.http.patch(f"/api/needs/{a['id']}", json={"after": [b["id"]]}).status_code == 400   # 互相等
    assert srv.http.patch(f"/api/needs/{a['id']}", json={"after": [a["id"]]}).status_code == 400   # 等自己
    assert srv.http.patch(f"/api/needs/{a['id']}", json={"title": ""}).status_code == 400
    u = srv.patch(f"/api/needs/{a['id']}", {"title": "竞品分析", "body": "看四家"})
    assert u["title"] == "竞品分析" and u["body"] == "看四家"
    d = card(srv, a["id"])
    assert d["results"] == [] and d["questions"] == []
    # 手动标完成以后，等它的卡不再被挡
    assert srv.post(f"/api/needs/{a['id']}/done")["column"] == "done"
    assert card(srv, b["id"])["blocked_by"] == []


def test_start_with_other_bot_done(srv, fake, ws):
    h = srv.make_bot("Helper")
    fake.script("worker", "调研做完了：三家都涨价\n细节见附件")
    n = need(srv, "调研竞品价格", body="只看国内", assignee_bot_id=h["id"])
    assert in_board(srv, n["id"]) == "todo"
    c = srv.post(f"/api/needs/{n['id']}/start")
    assert c["column"] == "doing"
    disp = until(lambda: srv.find(h["chat_id"], kind="dispatch"), what="派给 Helper 的任务")[0]
    assert "调研竞品价格" in disp["content"] and "只看国内" in disp["content"]
    until(lambda: column(srv, n["id"]) == "done", what="卡片到 done")
    c = card(srv, n["id"])
    assert [t["id"] for t in c["tasks"]] == [disp["id"]]
    t = c["tasks"][0]
    assert t["bot_id"] == h["id"] and t["status"] == "done" and t["waiting_kind"] is None and "调研竞品价格" in t["goal"]
    assert c["progress"] == {"done": 1, "total": 1}
    assert c["latest"] == "调研做完了：三家都涨价"
    assert c["results"] == [{"task_id": disp["id"], "text": "调研做完了：三家都涨价\n细节见附件"}]
    assert in_board(srv, n["id"]) == "done"
    assert until(lambda: [e for e in ws.of("board") if e.get("need_id") == n["id"]], what="board 事件")
    # 任务做完以后也发了一次（不只是开始时）
    until(lambda: len([e for e in ws.of("board") if e.get("need_id") == n["id"]]) >= 3, what="任务结束的 board 事件")


def test_start_with_primary_bot(srv, fake, ws):
    p = next(b for b in srv.get("/api/bootstrap")["bots"] if b["is_primary"])
    proj = project(srv, "PRI")
    fake.script(f"router:{p['name']}", Call("dispatch", {"goal": "查一下首页为什么慢"}), "派出去了")
    fake.script("worker", "首页慢是因为图片太大")
    n = srv.post("/api/needs", {"title": "首页太慢", "body": "手机上打开要 5 秒", "project_id": proj["id"], "start": True})
    assert n["key"] == "PRI-1" and n["column"] == "doing"
    msg = until(lambda: srv.find(p["chat_id"], role="user", text="PRI-1 首页太慢"), what="替你发给主 Bot 的消息")[0]
    assert msg["content"].startswith("PRI-1 首页太慢") and "手机上打开要 5 秒" in msg["content"]
    disp = until(lambda: srv.find(p["chat_id"], kind="dispatch"), what="主 Bot 派的任务")[0]
    until(lambda: column(srv, n["id"]) == "done", what="卡片到 done")
    c = card(srv, n["id"])
    assert [t["id"] for t in c["tasks"]] == [disp["id"]] and c["latest"] == "首页慢是因为图片太大"
    assert srv.find(p["chat_id"], role="assistant", text="派出去了")
    # 主 Bot 没派任务、直接回了话：等你看过以后标完成
    fake.script(f"router:{p['name']}", "这个不用派任务，直接告诉你：是缓存没开")
    m = srv.post("/api/needs", {"title": "缓存开了没有", "start": True})
    until(lambda: srv.find(p["chat_id"], role="assistant", text="是缓存没开"), what="主 Bot 直接回答")
    until(lambda: column(srv, m["id"]) == "waiting", what="没有任务的卡等你处理")
    c = card(srv, m["id"])
    assert c["tasks"] == [] and c["latest"] == "这个不用派任务，直接告诉你：是缓存没开"
    assert srv.post(f"/api/needs/{m['id']}/done")["column"] == "done"


def test_waiting_on_approval(srv, fake, ws):
    until(lambda: any(a["name"] == "demo" and a.get("state") == "connected" for a in srv.get("/api/apps")["apps"]),
          timeout=30, what="假应用连上")
    k = srv.make_bot("Noter")
    fake.script("worker", Call("demo__create_note", {"text": "周五交报告"}), "记好了")
    n = srv.post("/api/needs", {"title": "记一条笔记：周五交报告", "assignee_bot_id": k["id"], "start": True})
    approval = until(lambda: [e for e in ws.of("approval") if e.get("status") == "pending" and e["tool"] == "demo__create_note"],
                     what="确认卡片")[0]
    until(lambda: column(srv, n["id"]) == "waiting", what="卡片在等确认")
    c = card(srv, n["id"])
    assert c["tasks"][0]["status"] == "running" and c["tasks"][0]["waiting_kind"] == "approval"
    assert [q["id"] for q in c["questions"]] == [approval["id"]] and c["questions"][0]["tool"] == "demo__create_note"
    assert in_board(srv, n["id"]) == "waiting"
    ws.send({"type": "approve", "id": approval["id"], "ok": True})
    until(lambda: column(srv, n["id"]) == "done", what="确认以后做完")
    assert card(srv, n["id"])["questions"] == []


def test_failed_task_waits_then_restart(srv, fake, ws):
    f = srv.make_bot("Flaky")
    fake.fail(400)   # 不重试的错误：任务直接失败
    n = srv.post("/api/needs", {"title": "会失败的事", "assignee_bot_id": f["id"], "start": True})
    until(lambda: card(srv, n["id"])["tasks"] and card(srv, n["id"])["tasks"][0]["status"] == "failed", what="任务失败")
    assert column(srv, n["id"]) == "waiting"
    # 重新开始：新派一个任务，做完到 done
    fake.script("worker", "这次成了")
    c = srv.post(f"/api/needs/{n['id']}/start")
    assert c["column"] == "doing"
    until(lambda: column(srv, n["id"]) == "done", what="重新开始后做完")
    c = card(srv, n["id"])
    assert [t["status"] for t in c["tasks"]] == ["failed", "done"] and c["progress"] == {"done": 1, "total": 2}


def test_done_and_cancel_stop_tasks(srv, fake, ws):
    s = srv.make_bot("Slow")
    fake.script("worker", lambda body: (time.sleep(2), "慢慢做完了")[1], lambda body: (time.sleep(2), "慢慢做完了")[1])
    a = srv.post("/api/needs", {"title": "慢活一", "assignee_bot_id": s["id"], "start": True})
    b = srv.post("/api/needs", {"title": "慢活二", "assignee_bot_id": s["id"], "start": True})
    until(lambda: card(srv, a["id"])["tasks"] and card(srv, b["id"])["tasks"], what="两个任务都派了")
    assert srv.http.post(f"/api/needs/{a['id']}/start").status_code == 400   # 已经在做了
    ta = card(srv, a["id"])["tasks"][0]["id"]
    tb = card(srv, b["id"])["tasks"][0]["id"]
    assert srv.post(f"/api/needs/{a['id']}/done")["column"] == "done"
    assert srv.post(f"/api/needs/{b['id']}/cancel")["column"] == "cancelled"
    assert srv.get(f"/api/tasks/{ta}")["task"]["status"] == "cancelled"
    assert srv.get(f"/api/tasks/{tb}")["task"]["status"] == "cancelled"
    assert in_board(srv, a["id"]) == "done" and in_board(srv, b["id"]) == "cancelled"
    time.sleep(2.3)
    assert not srv.find(s["chat_id"], kind="result")   # 停掉以后不再出结果
    idle(srv)


def test_move_project_renumbers(srv):
    a = project(srv, "MVA")
    b = project(srv, "MVB")
    n1 = need(srv, "一", project_id=a["id"])
    n2 = need(srv, "二", project_id=a["id"])
    assert (n1["key"], n2["key"]) == ("MVA-1", "MVA-2")
    m = srv.patch(f"/api/needs/{n1['id']}", {"project_id": b["id"]})
    assert m["key"] == "MVB-1" and m["no"] == 1 and m["project_id"] == b["id"]
    n3 = need(srv, "三", project_id=a["id"])
    assert n3["key"] == "MVA-3"   # 旧编号不回收
    back = srv.patch(f"/api/needs/{n1['id']}", {"project_id": a["id"]})
    assert back["key"] == "MVA-4"
    same = srv.patch(f"/api/needs/{n1['id']}", {"project_id": a["id"], "title": "一（改）"})
    assert same["key"] == "MVA-4"   # 没换项目不重新编号
    none = srv.patch(f"/api/needs/{n1['id']}", {"project_id": None})
    assert none["key"] is None and none["project_id"] is None
    ids = {c["id"] for c in srv.get("/api/board", params={"project": "none"})["columns"]["todo"]}
    assert n1["id"] in ids and n2["id"] not in ids
    ids = {c["id"] for c in srv.get("/api/board", params={"project": a["id"]})["columns"]["todo"]}
    assert ids == {n2["id"], n3["id"]}


def test_search_time_archive_paging(srv, ws):
    p = project(srv, "SRCH")
    x = need(srv, "整理发票", body="去年十二月的", project_id=p["id"])
    y = need(srv, "订机票", body="去上海")
    t_mid = time.time()
    time.sleep(1.1)
    z = need(srv, "订酒店", body="上海外滩附近")

    def found(**params):
        b = srv.get("/api/board", params=params)
        return {c["id"] for cards in b["columns"].values() for c in cards}

    assert found(q="发票") == {x["id"]}
    assert found(q="srch-1") == {x["id"]}            # 编号，不分大小写
    assert found(q="上海") >= {y["id"], z["id"]} and x["id"] not in found(q="上海")
    assert z["id"] in found(**{"from": t_mid}) and y["id"] not in found(**{"from": t_mid})
    assert y["id"] in found(to=t_mid) and z["id"] not in found(to=t_mid)
    # 归档和恢复
    events = len([e for e in ws.of("board") if e.get("need_id") == y["id"]])
    a = srv.post(f"/api/needs/{y['id']}/archive")
    assert a["archived"] is True
    assert y["id"] not in found() and y["id"] in found(archived=1) and x["id"] not in found(archived=1)
    until(lambda: len([e for e in ws.of("board") if e.get("need_id") == y["id"]]) > events, what="归档的 board 事件")
    assert srv.http.post(f"/api/needs/{y['id']}/start").status_code == 400   # 归档的不能开始
    r = srv.post(f"/api/needs/{y['id']}/restore")
    assert r["archived"] is False and y["id"] in found()
    # 分页：按 id 从新到旧
    b = srv.get("/api/board", params={"limit": 1})
    assert b["more"] is True and sum(len(v) for v in b["columns"].values()) == 1
    first = next(c for v in b["columns"].values() for c in v)
    b2 = srv.get("/api/board", params={"limit": 1, "before": first["id"]})
    second = next(c for v in b2["columns"].values() for c in v)
    assert second["id"] < first["id"]
    assert set(srv.get("/api/board")["columns"]) == {"todo", "doing", "waiting", "done", "cancelled"}
    assert any(pp["key"] == "SRCH" for pp in srv.get("/api/board")["projects"])
