"""文档（Open Agent Docs，见 docs/plans/oad.md）：只有 Rust 版有。服务自己把 open-agent-docs 当子进程起，
浏览器经 /api/v1、/s/ 转过去；Bot 是 OAD 里的 Agent，save_page 的 Markdown 存成文档；评论 @ Bot 叫醒它，回答发回评论串。

要有 open-agent-docs 程序：OSA_DOCS_BIN，或者 PATH 上的（brew install kadaliao/tap/open-agent-docs）。"""
import os
import shutil

import pytest

import conftest
from conftest import Server, Socket, until
from fakellm_http import Call

BIN = os.environ.get("OSA_DOCS_BIN") or shutil.which("open-agent-docs")
pytestmark = [
    pytest.mark.skipif(conftest.IMPL != "rust", reason="文档只有 Rust 版有"),
    pytest.mark.skipif(not BIN, reason="没有 open-agent-docs 程序"),
]


@pytest.fixture(scope="module")
def dsrv(llm):
    s = Server(llm, env={"OSA_DOCS": "", "OSA_DOCS_BIN": BIN or ""})
    until(lambda: s.get("/api/settings")["docs"]["ready"], timeout=30, what="文档服务连上")
    yield s
    s.stop()


@pytest.fixture
def dws(dsrv):
    s = Socket(dsrv)
    yield s
    s.close()


def v1(srv, method, path, **kw):
    return srv.http.request(method, "/api/v1" + path, **kw)


def ws_id(srv):
    return v1(srv, "GET", "/me").json()["workspaces"][0]["id"]


def test_proxy_identity_and_origin(dsrv):
    me = v1(dsrv, "GET", "/me").json()
    assert me["workspaces"][0]["role"] == "owner" and me["user"]["name"] == "用户"
    # 浏览器自己带的 X-OAD-* 不认：还是同一个用户
    forged = v1(dsrv, "GET", "/me", headers={"X-OAD-User-Id": "evil", "X-OAD-Secret": "x"}).json()
    assert forged["user"]["id"] == me["user"]["id"]
    # 别的站点来的写请求在 OSA 这里就拦下了
    r = v1(dsrv, "POST", f"/workspaces/{ws_id(dsrv)}/pages", json={"title": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_agents_follow_bots(dsrv):
    b = dsrv.make_bot("文档 小助手")
    names = lambda: {a["name"]: a for a in v1(dsrv, "GET", f"/workspaces/{ws_id(dsrv)}/agents").json()["agents"]}
    until(lambda: "文档-小助手" in names(), what="建 Agent")   # 名字里的空格换成 -
    assert names()["OSA"]["scope"] == "admin" and names()["文档-小助手"]["scope"] == "write"
    dsrv.http.patch(f"/api/bots/{b['id']}", json={"name": "Scribe"})
    until(lambda: "Scribe" in names(), what="改名")
    dsrv.http.delete(f"/api/bots/{b['id']}")
    until(lambda: names()["Scribe"]["revoked_at"] is not None, what="撤掉")


def test_save_page_markdown_goes_to_docs_and_share(dsrv, fake, dws):
    b = dsrv.make_bot("Writer")
    fake.script("router:Writer", Call("save_page", {"title": "猫的报告", "content": "# 猫\n\n橘猫特别能吃。"}), "好了")
    dws.say(b["chat_id"], "写成文档")
    until(lambda: dsrv.find(b["chat_id"], role="assistant", text="好了"), what="回复")
    out = [m for m in fake.bodies("router", "Writer")[-1]["messages"] if m["role"] == "tool"][0]["content"]
    assert out.startswith("已保存文档「猫的报告」（page_id=\"") and "链接 /p/" in out
    doc = out.split('page_id="')[1].split('"')[0]
    page = v1(dsrv, "GET", f"/pages/{doc}").json()
    agents = {a["id"]: a["name"] for a in v1(dsrv, "GET", f"/workspaces/{ws_id(dsrv)}/agents").json()["agents"]}
    assert page["title"] == "猫的报告" and agents.get(page["created_by"].removeprefix("agent:")) in ("Writer", None)
    assert "橘猫特别能吃" in v1(dsrv, "GET", f"/pages/{doc}/content").json()["markdown"]

    # read_page 认文档的编号；html 照旧进资料库
    fake.script("router:Writer", Call("read_page", {"page_id": doc}), "读完")
    dws.say(b["chat_id"], "读一下")
    until(lambda: dsrv.find(b["chat_id"], role="assistant", text="读完"), what="回复")
    out = [m for m in fake.bodies("router", "Writer")[-1]["messages"] if m["role"] == "tool"][0]["content"]
    assert out.startswith(f"《猫的报告》/p/{doc}") and "橘猫特别能吃" in out

    # 正文开头的一级标题和文档标题是同一句：文档页最上面已经显示标题，这一行不再存进正文
    fake.script("router:Writer", Call("save_page", {"title": "狗的报告", "content": "# 狗的报告\n\n柴犬爱散步。"}), "存了")
    dws.say(b["chat_id"], "再写一篇")
    until(lambda: dsrv.find(b["chat_id"], role="assistant", text="存了"), what="回复")
    out = [m for m in fake.bodies("router", "Writer")[-1]["messages"] if m["role"] == "tool"][0]["content"]
    dog = v1(dsrv, "GET", f"/pages/{out.split('page_id=\"')[1].split('\"')[0]}/content").json()["markdown"]
    assert "柴犬爱散步" in dog and "狗的报告" not in dog
    assert "# 猫" in v1(dsrv, "GET", f"/pages/{doc}/content").json()["markdown"]   # 不一样的标题留着

    # 分享：不用登录的 /s/<token> 转给 OAD
    share = v1(dsrv, "POST", f"/pages/{doc}/share", json={}).json()
    r = dsrv.http.get(f"/s/{share['token']}")
    assert r.status_code == 200 and "橘猫特别能吃" in r.text


def test_comment_wakes_bot_and_reply_goes_back(dsrv, fake):
    b = dsrv.make_bot("Editor")
    w = ws_id(dsrv)
    page = v1(dsrv, "POST", f"/workspaces/{w}/pages", json={"title": "计划"}).json()
    v1(dsrv, "PUT", f"/pages/{page['id']}/content", json={"markdown": "第一段"})
    until(lambda: "Editor" in {a["name"] for a in v1(dsrv, "GET", f"/workspaces/{w}/agents").json()["agents"]}, what="Agent")
    fake.script("router:Editor", "已经看过了，没问题")
    th = v1(dsrv, "POST", f"/pages/{page['id']}/comments", json={"body": "@Editor 看看这段"}).json()
    # 私聊里有这条评论，Bot 被叫醒；它没自己在评论里回，回答由 OSA 发回那串评论
    until(lambda: dsrv.find(b["chat_id"], role="task", text="[文档评论]《计划》"), what="私聊里的评论")
    sent = str(until(lambda: fake.bodies("router", "Editor"), what="叫醒")[-1]["messages"])
    assert "在文档的评论里叫了你" in sent and "看看这段" in sent

    def replies():
        threads = v1(dsrv, "GET", f"/pages/{page['id']}/comments").json()["threads"]
        t = next(t for t in threads if t["id"] == th["id"])
        return [c for c in t["comments"] if c["author"]["kind"] == "agent"]
    until(lambda: replies(), what="评论里的回复")
    assert replies()[0]["author"]["name"] == "Editor" and replies()[0]["body"] == "已经看过了，没问题"


def test_legacy_markdown_pages_move_to_docs(llm):
    # 旧版资料库里的 Markdown 页面：第一次启动时搬进文档，旧编号指向新文档；网页留在资料库
    s = Server(llm, env={"OSA_DOCS": "off"})
    try:
        p = s.post("/api/pages", {"title": "旧周报", "content": "# 旧周报\n\n- 一", "format": "markdown"})
        h = s.post("/api/pages", {"title": "网页", "content": "<p>x</p>", "format": "html"})
    finally:
        s.stop()
    s2 = Server(llm, env={"OSA_DOCS": "", "OSA_DOCS_BIN": BIN or "", "OSA_DATA_DIR": str(s.data)})   # 同一个数据目录
    try:
        until(lambda: s2.get("/api/settings")["docs"]["ready"], timeout=30, what="文档服务连上")
        moved = until(lambda: s2.http.get(f"/api/pages/{p['id']}").json().get("moved"), what="搬过去")
        assert v1(s2, "GET", f"/pages/{moved}").json()["title"] == "旧周报"
        assert s2.http.get(f"/api/pages/{h['id']}").json()["format"] == "html"
    finally:
        s2.stop()


def test_backup_carries_docs(llm, fake):
    # 备份带上文档（数据库 + 上传的文件）；在另一台（另一个保险库主密钥）恢复后，文档还在，Bot 的 Agent 照样能用
    import base64
    b64 = lambda s: base64.b64encode(s.encode()).decode()
    s1 = Server(llm, env={"OSA_DOCS": "", "OSA_DOCS_BIN": BIN or ""})
    try:
        until(lambda: s1.get("/api/settings")["docs"]["ready"], timeout=30, what="文档服务连上")
        b = s1.make_bot("Keeper")
        w = ws_id(s1)
        page = v1(s1, "POST", f"/workspaces/{w}/pages", json={"title": "带走的文档"}).json()
        v1(s1, "PUT", f"/pages/{page['id']}/content", json={"markdown": "要带走的一段"})
        up = v1(s1, "POST", f"/workspaces/{w}/files", content=b"file-bytes-123",
                headers={"Content-Type": "application/octet-stream", "X-Filename": "a.txt"})
        assert up.status_code == 201, up.text
        until(lambda: "Keeper" in {a["name"] for a in v1(s1, "GET", f"/workspaces/{w}/agents").json()["agents"]}, what="Agent")
        assert s1.get("/api/backup")["counts"]["docs"] >= 1
        r = s1.http.post("/api/backup/export", json={"passphrase": "correct horse battery"})
        assert r.status_code == 200, r.text
        data = r.content
        # 命令行导出（Docker 里用）：服务开着也行，同样带着文档
        import subprocess, zipfile
        out = subprocess.run([conftest.RUST_BIN, "backup", "export", "-o", str(s1.tmp)], capture_output=True, text=True,
                             env={**os.environ, "OSA_DATA_DIR": str(s1.data), "OSA_CONFIG": str(s1.tmp / "config.yaml"),
                                  "OSA_BACKUP_PASSPHRASE": "correct horse battery"})
        assert out.returncode == 0 and "已导出到" in out.stdout, out.stdout + out.stderr
        assert '"docs"' in out.stdout and list(s1.tmp.glob("osa-backup-*.osabak"))
    finally:
        s1.stop()
    s2 = Server(llm, env={"OSA_DOCS": "", "OSA_DOCS_BIN": BIN or ""})   # 新的一台：数据目录、主密钥都不一样
    try:
        until(lambda: s2.get("/api/settings")["docs"]["ready"], timeout=30, what="文档服务连上")
        r = s2.http.put("/api/backup/restore", content=data, headers={"x-passphrase": b64("correct horse battery")})
        assert r.status_code == 200, r.text
        assert r.json()["counts"]["docs"] >= 1
        started = s2.get("/api/backup")["started"]
        assert s2.post("/api/backup/restore/confirm") == {"restarting": True}

        def back():
            try:
                i = s2.http.get("/api/backup").json()
                return i["started"] != started and s2.get("/api/settings")["docs"]["ready"]
            except Exception:
                return None
        until(back, timeout=60, what="重启后连上文档服务")
        assert v1(s2, "GET", f"/pages/{page['id']}").json()["title"] == "带走的文档"
        assert "要带走的一段" in v1(s2, "GET", f"/pages/{page['id']}/content").json()["markdown"]
        f = v1(s2, "GET", f"/files/{up.json()['id']}")
        assert f.status_code == 200 and f.content == b"file-bytes-123"
        # Bot 用恢复过来的 token 读文档（token 在新机器上重新加密过）
        bot = next(x for x in s2.get("/api/bootstrap")["bots"] if x["name"] == "Keeper")
        names = {a["name"] for a in v1(s2, "GET", f"/workspaces/{ws_id(s2)}/agents").json()["agents"] if a["revoked_at"] is None}
        assert "Keeper" in names and "OSA" in names and not any(n.startswith(("Keeper-", "OSA-")) for n in names)
        fake.script("router:Keeper", Call("read_page", {"page_id": page["id"]}), "读完")
        ws = Socket(s2)
        try:
            ws.say(bot["chat_id"], "读一下")
            until(lambda: s2.find(bot["chat_id"], role="assistant", text="读完"), what="回复")
        finally:
            ws.close()
        out = [m for m in fake.bodies("router", "Keeper")[-1]["messages"] if m["role"] == "tool"][0]["content"]
        assert out.startswith("《带走的文档》"), out
        assert list(s2.data.glob("docs.before-restore-*")), "原来的文档要留一份"
    finally:
        s2.stop()


def test_restore_backup_without_docs(llm):
    # 以前的版本（或者没开文档）做的备份里没有文档：上传后的摘要里没有 docs，网页据此提醒；恢复后文档从空的开始，原来的挪到一边
    import base64
    s0 = Server(llm, env={"OSA_DOCS": "off"})
    try:
        data = s0.http.post("/api/backup/export", json={"passphrase": "correct horse battery"}).content
    finally:
        s0.stop()
    s = Server(llm, env={"OSA_DOCS": "", "OSA_DOCS_BIN": BIN or ""})
    try:
        until(lambda: s.get("/api/settings")["docs"]["ready"], timeout=30, what="文档服务连上")
        page = v1(s, "POST", f"/workspaces/{ws_id(s)}/pages", json={"title": "这台的文档"}).json()
        assert s.get("/api/backup")["counts"]["docs"] >= 1
        r = s.http.put("/api/backup/restore", content=data, headers={"x-passphrase": base64.b64encode(b"correct horse battery").decode()})
        assert r.status_code == 200 and "docs" not in r.json()["counts"]
        started = s.get("/api/backup")["started"]
        s.post("/api/backup/restore/confirm")

        def back():
            try:
                return s.http.get("/api/backup").json()["started"] != started and s.get("/api/settings")["docs"]["ready"]
            except Exception:
                return None
        until(back, timeout=60, what="重启后连上文档服务")
        assert v1(s, "GET", f"/pages/{page['id']}").status_code == 404
        assert s.get("/api/backup")["counts"].get("docs", 0) == 0
        old = list(s.data.glob("docs.before-restore-*"))
        assert old and (old[0] / "oad.db").exists()
    finally:
        s.stop()
