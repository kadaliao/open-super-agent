"""技能（osa/skills.py）：带文件的技能包、导入导出、use_skill、apps 声明、技能市场。不联网（GitHub 换成 MockTransport），不调真模型。"""
import base64
import io
import json
import zipfile
import httpx
import pytest
from conftest import until
from fakellm import Call
from osa import bots, i18n, memory, registry, routines, server, skills, templates


def http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


@pytest.fixture
def bot(make_bot):
    """用完就删：测试库里 Bot 有上限，大家共用。"""
    made = []

    def make(name: str = "") -> dict:
        made.append(make_bot(name))
        return made[-1]
    yield make
    for b in made:
        memory.conn.execute("delete from skills where bot_id=?", (b["id"],))
        memory.conn.commit()
        bots.delete(b["id"])


def make_zip(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p, v in entries.items():
            z.writestr(p, v)
    return buf.getvalue()


SKILL_MD = "---\nname: fill-form\ndescription: 填表\napps: [notion]\n---\n\n1. 跑 scripts/fill.py\n2. 结果记到 @github\n"


# ---------- 文件上限 ----------
def test_file_limits():
    assert skills.check_files({"./scripts\\a.py": "print(1)", "references/b.md": "x"}) == \
        {"references/b.md": "x", "scripts/a.py": "print(1)"}            # 统一成 / 分隔、按路径排好
    for bad in ("../x.py", "/etc/passwd", ".env", "scripts/.git/config", "a//b", "a/../b", ""):
        with pytest.raises(ValueError, match="路径不对"):
            skills.check_files({bad: "x"})
    with pytest.raises(ValueError, match="不是文字文件"):
        skills.check_files({"a.bin": "ab\x00cd"})
    with pytest.raises(ValueError, match="不是文字文件"):
        skills.check_files({"a.py": b"bytes"})
    with pytest.raises(ValueError, match="SKILL.md"):
        skills.check_files({"SKILL.md": "x"})
    with pytest.raises(ValueError, match="太多"):
        skills.check_files({f"f{i}.txt": "x" for i in range(skills.MAX_FILES + 1)})
    with pytest.raises(ValueError, match="太大"):
        skills.check_files({"a.txt": "x" * 150_000, "b.txt": "y" * 60_000})
    assert len(skills.check_files({f"f{i}.txt": "x" for i in range(skills.MAX_FILES)})) == skills.MAX_FILES


def test_save_keeps_files_unless_given(bot):
    b = bot()
    s = skills.save(b["id"], "带文件", "做法", "", "import", files={"scripts/a.py": "print(1)"}, apps=["notion"])
    assert s["files"] == {"scripts/a.py": "print(1)"} and s["apps"] == ["notion"]
    # 对话里、botadmin 改做法时不传 files、apps：原样留着
    s = skills.save(b["id"], "带文件", "新做法", "", "chat")
    assert s["body"] == "新做法" and s["files"] == {"scripts/a.py": "print(1)"} and s["apps"] == ["notion"]
    s = skills.save(b["id"], "带文件", "新做法", "", sid=s["id"], files={}, apps=None)   # 明确清空
    assert s["files"] == {} and s["apps"] is None
    with pytest.raises(ValueError, match="太大"):
        skills.save(b["id"], "太大", "做法", files={"a.txt": "x" * (skills.MAX_FILES_TOTAL + 1)})
    assert not skills.find(b["id"], "太大")


# ---------- 导入导出 ----------
def test_md_round_trip(bot):
    b = bot()
    s = skills.save(b["id"], "周报", "1. 收集\n2. 写 @github 的进展", "写周报", apps=["notion"])
    data, name, kind = skills.export(s)
    assert name == "周报.md" and kind.startswith("text/markdown")
    d = skills.from_file(data.decode(), name)
    assert (d["name"], d["description"], d["body"], d["apps"]) == ("周报", "写周报", s["body"], ["notion"])
    s2 = skills.save(b["id"], "周报2", "1. @github 看一下", "")   # 没声明：导出时按做法里的 @ 写上
    assert skills.from_file(skills.to_file(s2))["apps"] == ["github"]


def test_zip_round_trip(bot):
    b = bot()
    files = {"scripts/fill.py": "print('填表')\n", "references/fields.md": "# 字段\n- 名字"}
    s = skills.save(b["id"], "fill-form", "1. 跑 scripts/fill.py", "填表", files=files, apps=["notion"])
    data, name, kind = skills.export(s)
    assert name == "fill-form.zip" and kind == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == ["SKILL.md", "references/fields.md", "scripts/fill.py"]   # SKILL.md 在根上
    d = skills.from_zip(data, name)
    assert d["files"] == files and d["apps"] == ["notion"] and d["body"] == s["body"] and d["skipped"] == []


def test_zip_from_elsewhere():
    """别处打的包：一层文件夹包着、带 __MACOSX 和二进制文件；名字没写就用文件夹名。"""
    data = make_zip({"pdf-tools/SKILL.md": "---\ndescription: 处理 PDF\n---\n做法", "pdf-tools/scripts/split.py": "x = 1",
                     "pdf-tools/assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00", "pdf-tools/fonts/a.dat": b"\xff\xfe\x00bin",
                     "__MACOSX/pdf-tools/._SKILL.md": b"\x00\x05", "pdf-tools/.DS_Store": b"\x00"})
    d = skills.from_zip(data, "download.zip")
    assert d["name"] == "pdf-tools" and d["files"] == {"scripts/split.py": "x = 1"}
    assert sorted(d["skipped"]) == ["assets/logo.png", "fonts/a.dat"]
    with pytest.raises(ValueError, match="SKILL.md"):
        skills.from_zip(make_zip({"README.md": "x"}))
    with pytest.raises(ValueError, match="zip"):
        skills.from_zip(b"PK\x03\x04 not really")
    with pytest.raises(ValueError, match="太大"):
        skills.from_zip(make_zip({"SKILL.md": "---\nname: big\n---\n" + "x" * skills.MAX_FILE}))


async def test_http_import_export(bot):
    b = bot()
    async with http() as c:
        r = await c.post(f"/api/bots/{b['id']}/skills/parse", json={"zip": base64.b64encode(make_zip(
            {"SKILL.md": SKILL_MD, "scripts/fill.py": "print(1)"})).decode(), "filename": "x.zip"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["name"] == "fill-form" and d["files"] == {"scripts/fill.py": "print(1)"}
        assert d["declared_apps"] == ["notion"]                                    # 以声明为准：@github 不算
        assert [(a["name"], a["connected"], a["in_market"]) for a in d["apps"]] == [("notion", False, True)]
        r = await c.post(f"/api/bots/{b['id']}/skills", json={"name": d["name"], "description": d["description"], "body": d["body"],
                                                              "source": "import", "files": d["files"], "apps": d["declared_apps"]})
        s = r.json()
        assert s["files"] == d["files"] and s["apps"] == ["notion"] and s["source"] == "import"
        r = await c.get(f"/api/skills/{s['id']}/export")
        assert r.headers["content-type"] == "application/zip" and "fill-form.zip" in r.headers["content-disposition"]
        # 编辑器保存时不传 apps：保持原样；去掉一个文件
        r = await c.put(f"/api/skills/{s['id']}", json={"body": "新做法", "files": {}})
        assert r.json()["files"] == {} and r.json()["apps"] == ["notion"]
        r = await c.get(f"/api/skills/{s['id']}/export")
        assert r.headers["content-type"].startswith("text/markdown")
        r = await c.post(f"/api/bots/{b['id']}/skills/parse", json={"text": "---\nname: x\n---\n用 @github", "filename": "x.md"})
        assert r.json()["declared_apps"] is None and [a["name"] for a in r.json()["apps"]] == ["github"]   # 没声明才回退正则


async def test_share_link_carries_files(bot, monkeypatch):
    """分享出去的技能带文件时，原文件是 zip；别人粘贴链接导入能拿到文件。"""
    b = bot()
    s = skills.share(skills.save(b["id"], "share-me", "做法", files={"scripts/a.sh": "echo hi"})["id"], True)
    async with http() as c:
        assert (await c.get(f"/api/shared/skills/{s['share_token']}")).json()["files"] == ["scripts/a.sh"]
        raw = await c.get(f"/api/shared/skills/{s['share_token']}/raw")
    assert raw.headers["content-type"] == "application/zip"
    monkeypatch.setattr(skills, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=raw.content, headers={"content-type": "application/zip"}))))
    d = await skills.fetch(f"https://friend.example/skill/{s['share_token']}")
    assert d["name"] == "share-me" and d["files"] == {"scripts/a.sh": "echo hi"}


def github(handler_files: dict[str, str], limited: bool = False):
    """假的 GitHub：contents 接口列目录，raw.githubusercontent.com 给文件。"""
    seen = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "api.github.com":
            if limited:
                return httpx.Response(403, json={"message": "API rate limit exceeded"})
            assert req.url.params["ref"] == "main"
            path = req.url.path.split("/contents/", 1)[1]
            items, dirs = [], set()
            for p, v in handler_files.items():
                if not p.startswith(path + "/"):
                    continue
                rest = p[len(path) + 1:]
                if "/" in rest:
                    d = rest.split("/")[0]
                    if d not in dirs:
                        dirs.add(d)
                        items.append({"name": d, "path": f"{path}/{d}", "type": "dir"})
                else:
                    items.append({"name": rest, "path": p, "type": "file", "size": len(v.encode()),
                                  "download_url": f"https://raw.githubusercontent.com/o/r/main/{p}"})
            return httpx.Response(200, json=items)
        if req.url.host == "raw.githubusercontent.com":
            p = req.url.path.split("/main/", 1)[1]
            return httpx.Response(200, text=handler_files[p]) if p in handler_files else httpx.Response(404)
        return httpx.Response(404)
    return handle, seen


async def test_github_folder(monkeypatch):
    repo = {"skills/fill/SKILL.md": SKILL_MD, "skills/fill/scripts/fill.py": "print('hi')",
            "skills/fill/references/deep/more.md": "more", "skills/fill/logo.png": "png", "skills/other/SKILL.md": "x"}
    handle, seen = github(repo)
    monkeypatch.setattr(skills, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    for url in ("https://github.com/o/r/tree/main/skills/fill", "https://github.com/o/r/blob/main/skills/fill/SKILL.md"):
        d = await skills.fetch(url)
        assert d["name"] == "fill-form" and d["files"] == {"references/deep/more.md": "more", "scripts/fill.py": "print('hi')"}
        assert d["skipped"] == ["logo.png"] and "warning" not in d
    assert not any("logo.png" in u for u in seen)              # 一看就不是文字的不去取
    assert skills.github_dir("https://github.com/o/r/blob/main/notes.md") is None   # 单个 Markdown 不是技能包


async def test_github_rate_limited_still_gets_skill_md(monkeypatch):
    handle, _ = github({"skills/fill/SKILL.md": SKILL_MD}, limited=True)
    monkeypatch.setattr(skills, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    d = await skills.fetch("https://github.com/o/r/tree/main/skills/fill")
    assert d["name"] == "fill-form" and d["files"] == {} and "SKILL.md" in d["warning"]


# ---------- use_skill ----------
def test_use_skill_tool(bot):
    b = bot()
    t = registry.get("use_skill")
    assert t and t.where == {registry.ROUTER, registry.WORKER} and not t.gated
    long = "".join(f"第{i}行\n" for i in range(3000))
    skills.save(b["id"], "填表", "1. 打开表单\n2. 跑脚本", "填表单", files={"scripts/fill.py": "A" * 500, "references/long.md": long})
    out = skills.tool_use(b["id"], registry.WORKER, {"name": "/填表"})
    assert "1. 打开表单" in out and "用户调用了" not in out            # 是 Bot 自己取的，不是用户调用
    assert "scripts/fill.py（500 字）" in out and "A" * 400 + "……" in out and "A" * 401 not in out   # 每个文件开头 400 字
    assert "computer_write_file" in out and "run_code" in out and "computer_shell" in out
    assert "dispatch" in skills.tool_use(b["id"], registry.ROUTER, {"name": "填表"})   # Router 跑不了脚本：派任务
    assert skills.tool_use(b["id"], registry.WORKER, {"name": "填表", "file": "scripts/fill.py"}).count("A") == 500
    part = skills.tool_use(b["id"], registry.WORKER, {"name": "填表", "file": "references/long.md"})
    assert f"共 {len(long)} 字" in part and "offset=7000" in part and len(part) < 7500    # 太长的分段给
    rest = skills.tool_use(b["id"], registry.WORKER, {"name": "填表", "file": "references/long.md", "offset": 7000})
    assert rest.split("---\n", 1)[1].startswith(long[7000:7010])
    assert "没有 nope.py" in skills.tool_use(b["id"], registry.WORKER, {"name": "填表", "file": "nope.py"})
    assert "没有 /不存在" in skills.tool_use(b["id"], registry.WORKER, {"name": "不存在"}) and "/填表" in \
        skills.tool_use(b["id"], registry.WORKER, {"name": "不存在"})


async def test_router_uses_skill(llm, events, bot):
    """Router 觉得某个技能管这件事时自己 use_skill 取做法；系统提示词里技能列表还是只有名字和描述。"""
    b = bot("Skilly")
    skills.save(b["id"], "日报", "1. 看今天的任务\n2. 写三条要点", "写日报", files={"scripts/stat.py": "print('统计')"})
    llm.script("router:Skilly", Call("use_skill", {"name": "日报"}), "写好了")
    await server.safe_handle(bots.chat(b["chat_id"]), "帮我写个日报")
    await until(lambda: len(llm.bodies("router", "Skilly")) == 2, what="第二次请求")
    first, second = llm.bodies("router", "Skilly")
    system = first["messages"][0]["content"]
    assert "use_skill" in system and "- /日报：写日报" in system and "看今天的任务" not in system
    assert "use_skill" in {t["function"]["name"] for t in first["tools"]}
    tool_msg = next(m for m in second["messages"] if m["role"] == "tool")
    assert "2. 写三条要点" in tool_msg["content"] and "print('统计')" in tool_msg["content"]


async def test_slash_call_mentions_files(llm, events, bot):
    b = bot("Slashy")
    skills.save(b["id"], "日报", "做法", "", files={"scripts/stat.py": "x"})
    await server.safe_handle(bots.chat(b["chat_id"]), "/日报 今天的")
    await until(lambda: llm.bodies("router", "Slashy"), what="请求")
    last = llm.bodies("router", "Slashy")[0]["messages"][-1]["content"]
    assert "用户调用了技能 /日报" in last and "scripts/stat.py" in last and "use_skill" in last


def test_routine_goal_mentions_files(bot):
    b = bot()
    skills.save(b["id"], "日报", "做法", "", files={"scripts/stat.py": "x"})
    goal = routines.goal_for({"name": "每天", "trigger": {"kind": "every", "minutes": 60}, "skill": "日报", "prompt": "", "bot_id": b["id"]})
    assert "scripts/stat.py" in goal and "use_skill" in goal


# ---------- apps 声明 ----------
def test_declared_apps_win():
    d = skills.from_file("---\nname: a\napps: [Notion, '@feishu', 'bad name']\n---\n记到 @github")
    assert d["apps"] == ["notion", "feishu"] and skills.apps_of(d) == ["notion", "feishu"]
    assert skills.from_file("---\nname: a\napps: notion, gmail\n---\nx")["apps"] == ["notion", "gmail"]
    assert skills.apps_of(skills.from_file("---\nname: a\napps: []\n---\n记到 @github")) == []      # 写了空的就是不用
    d = skills.from_file("---\nname: a\n---\n记到 @github，@nosuchapp 不算")
    assert d["apps"] is None and skills.apps_of(d) == ["github"]                                  # 没写才按 @ 找


# ---------- 技能市场 ----------
def test_builtin_market_both_languages(monkeypatch):
    zh = {m["id"]: m for m in skills.market()}
    assert set(zh) >= {"weekly-report", "save-webpage", "meeting-notes"}
    assert zh["meeting-notes"]["name"] == "会议纪要" and zh["meeting-notes"]["description"]
    monkeypatch.setattr(i18n, "lang", lambda: "en")
    en = {m["id"]: m for m in skills.market()}
    assert en["meeting-notes"]["name"] == "meeting-notes" and not i18n.CJK.search(en["meeting-notes"]["description"])
    for m in [*zh.values(), *en.values()]:          # 只用现有工具：不依赖应用，也不带脚本
        assert m["apps"] == [] and m["files"] == [] and m["description"]
    for name in ("SKILL.md", "SKILL.en.md"):
        for d in skills.MARKET.iterdir():
            if d.is_dir():
                assert (d / name).is_file(), f"{d.name} 缺 {name}"


async def test_market_install(bot, monkeypatch, tmp_path):
    pkg = tmp_path / "fill-form"
    (pkg / "scripts").mkdir(parents=True)
    (pkg / "SKILL.md").write_text(SKILL_MD)
    (pkg / "scripts" / "fill.py").write_text("print(1)")
    monkeypatch.setattr(skills, "MARKET", tmp_path)
    b = bot()
    async with http() as c:
        m = (await c.get(f"/api/bots/{b['id']}/skills/market")).json()
        assert [(x["id"], x["installed"], x["files"]) for x in m] == [("fill-form", False, ["scripts/fill.py"])]
        s = (await c.post(f"/api/bots/{b['id']}/skills/market/fill-form")).json()
        assert s["source"] == "market" and s["bot_id"] == b["id"] and s["files"] == {"scripts/fill.py": "print(1)"}
        assert s["apps"] == ["notion"]
        assert (await c.get(f"/api/bots/{b['id']}/skills/market")).json()[0]["installed"]
        assert (await c.post(f"/api/bots/{b['id']}/skills/market/fill-form")).json()["id"] == s["id"]   # 重装是替换，不是多一个
        assert (await c.post(f"/api/bots/{b['id']}/skills/market/nope")).status_code == 400
    for bad in ("../fill-form", "fill-form/scripts", "Fill-Form", ""):
        with pytest.raises(ValueError, match="技能市场里没有"):
            skills.market_skill(bad)


def test_template_carries_files(bot):
    b = bot()
    skills.save(b["id"], "带文件", "做法", "", files={"scripts/a.py": "print(1)"}, apps=["notion"])
    t = templates.parse(templates.to_text(templates.from_bot(b["id"])))
    assert "notion" in t["apps"]
    s = next(x for x in t["skills"] if x["name"] == "带文件")
    assert s["files"] == {"scripts/a.py": "print(1)"} and s["apps"] == ["notion"]
    nb = templates.install(t, name="模板装的")
    try:
        got = skills.find(nb["id"], "带文件")
        assert got["files"] == {"scripts/a.py": "print(1)"} and got["apps"] == ["notion"]
    finally:
        memory.conn.execute("delete from skills where bot_id=?", (nb["id"],))
        memory.conn.commit()
        bots.delete(nb["id"])


def test_schema_migration_adds_columns():
    cols = {r[1] for r in memory.conn.execute("pragma table_info(skills)")}
    assert {"files", "apps", "share_token"} <= cols
    assert json.loads(json.dumps(skills.USE_SKILL))["function"]["parameters"]["required"] == ["name"]
