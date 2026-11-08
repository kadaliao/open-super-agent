"""GitHub 仓库动态（不用 webhook）：仓库名的写法、各种动态变成事件、只报新的、GitHub 报错换成看得懂的话。不联网。"""
import json

import httpx
import pytest

from osa import events as engine
from osa.sources import _util, github_repo as gh

REPO = "octo/demo"


@pytest.fixture
def api(monkeypatch):
    """假的 GitHub：路径 → JSON（或 httpx.Response，或函数）。收到的请求记在 api.seen 里。"""
    class Api(dict):
        def __init__(self):
            super().__init__()
            self.seen: list[httpx.Request] = []
    table = Api()

    def handle(req: httpx.Request) -> httpx.Response:
        table.seen.append(req)
        assert req.url.host == "api.github.com"
        v = table.get(req.url.path)
        if v is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(v):
            v = v(req)
        return v if isinstance(v, httpx.Response) else httpx.Response(200, json=v)

    async def not_blocked(url):
        return None
    monkeypatch.setattr(_util, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(_util, "blocked", not_blocked)
    return table


def params(**kw) -> dict:
    return engine.clean_params(gh.GITHUB_REPO, {"repo": REPO, **kw})


def release(i: int, tag: str, **kw) -> dict:
    return {"id": i, "tag_name": tag, "name": kw.pop("name", tag), "body": kw.pop("body", f"{tag} 的更新说明"),
            "html_url": f"https://github.com/{REPO}/releases/tag/{tag}", "draft": False, "prerelease": False,
            "published_at": "2026-11-08T01:00:00Z", "author": {"login": "alice"}, **kw}


# ---------- 参数 ----------
@pytest.mark.parametrize("raw, want", [
    ("octo/demo", ("octo/demo", "")),
    (" https://github.com/octo/demo ", ("octo/demo", "")),
    ("https://github.com/octo/demo.git", ("octo/demo", "")),
    ("github.com/octo/demo/issues/12", ("octo/demo", "")),
    ("git@github.com:octo/demo.git", ("octo/demo", "")),
    ("https://www.github.com/octo/my.repo_x-1/tree/feature/login?tab=readme", ("octo/my.repo_x-1", "feature/login")),
])
def test_parse_repo(raw, want):
    assert gh.parse_repo(raw) == want


@pytest.mark.parametrize("raw", ["", "octo", "https://github.com/octo", "octo/demo repo", "-octo/demo", "octo/..",
                                 "https://gitlab.com/octo/demo/x"])
def test_bad_repo(raw):
    with pytest.raises(ValueError, match="owner/repo"):
        gh.parse_repo(raw)


def test_params():
    p = params(repo="https://github.com/octo/demo/tree/dev", what="commits")
    assert p["repo"] == REPO and p["branch"] == "dev"
    assert params()["what"] == "releases"
    with pytest.raises(ValueError):
        params(what="stars")
    with pytest.raises(ValueError, match="分支"):
        params(what="commits", branch="a b")
    assert gh.GITHUB_REPO.summary(params()) == "octo/demo · releases"
    assert gh.GITHUB_REPO.summary(p) == "octo/demo@dev · commits"
    for sc in gh.GITHUB_REPO.scenarios:
        engine.clean_params(gh.GITHUB_REPO, {"repo": REPO, **sc.get("params", {})})


# ---------- 动态 ----------
async def test_releases_baseline_then_new(api):
    path = f"/repos/{REPO}/releases"
    api[path] = [release(2, "v1.1.0"), release(1, "v1.0.0")]
    p = params()
    evs, st = await gh.check(p, None)
    assert [e["title"] for e in evs] == ["[octo/demo] 发布了 v1.1.0"]      # 样例
    json.dumps(st)
    assert (await gh.check(p, st))[0] == []

    api[path] = [release(4, "v2.0.0-rc1", name="大版本", prerelease=True, body="**Breaking:** 去掉了旧接口"),
                 release(3, "v1.2.0", draft=True), release(2, "v1.1.0"), release(1, "v1.0.0")]
    evs, st = await gh.check(p, st)
    assert len(evs) == 1                                                     # 草稿不算
    ev = evs[0]
    assert ev["title"] == "[octo/demo] 发布了 v2.0.0-rc1：大版本（预发布）"
    assert ev["url"] == f"https://github.com/{REPO}/releases/tag/v2.0.0-rc1"
    assert "发布人：alice" in ev["text"] and "Breaking" in ev["text"]
    req = api.seen[-1]
    assert req.headers["accept"] == "application/vnd.github+json" and "authorization" not in req.headers


async def test_token_is_sent(api):
    api[f"/repos/{REPO}/releases"] = []
    await gh.check(params() | {"token": "ghp_secret"}, None)
    assert api.seen[-1].headers["authorization"] == "Bearer ghp_secret"


async def test_issues_skip_prs(api):
    path = f"/repos/{REPO}/issues"
    issue = lambda n, **kw: {"number": n, "title": f"问题 {n}", "user": {"login": "bob"}, "state": "open",
                             "html_url": f"https://github.com/{REPO}/issues/{n}", "body": "点保存没反应",
                             "labels": [{"name": "bug"}], "created_at": "2026-11-09T02:00:00Z", **kw}
    api[path] = [issue(1)]
    p = params(what="issues")
    _, st = await gh.check(p, None)
    api[path] = [issue(3, pull_request={"url": "x"}), issue(2, state="closed"), issue(1)]
    evs, st = await gh.check(p, st)
    assert [e["title"] for e in evs] == ["[octo/demo] 新 issue #2：问题 2（bob）（已关闭）"]
    assert "标签：bug" in evs[0]["text"] and "点保存没反应" in evs[0]["text"]
    q = api.seen[-1].url.params
    assert q["state"] == "all" and q["sort"] == "created" and q["direction"] == "desc"


async def test_pulls(api):
    path = f"/repos/{REPO}/pulls"
    pr = lambda n, **kw: {"number": n, "title": f"改动 {n}", "user": {"login": "carol"}, "state": "open",
                          "html_url": f"https://github.com/{REPO}/pull/{n}", "body": "修了登录",
                          "head": {"label": "carol:fix"}, "base": {"ref": "main"}, "created_at": "2026-11-09T02:00:00Z", **kw}
    api[path] = [pr(1)]
    p = params(what="pulls")
    _, st = await gh.check(p, None)
    api[path] = [pr(3, draft=True), pr(2, state="closed", merged_at="2026-11-09T03:00:00Z"), pr(1)]
    evs, _ = await gh.check(p, st)
    assert [e["title"] for e in evs] == ["[octo/demo] 新 PR #3：改动 3（carol）（草稿）",
                                         "[octo/demo] 新 PR #2：改动 2（carol）（已合并）"]
    assert "carol:fix → main" in evs[0]["text"]


async def test_commits_on_branch(api):
    path = f"/repos/{REPO}/commits"
    commit = lambda sha, msg, login="dave": {"sha": sha * 40, "html_url": f"https://github.com/{REPO}/commit/{sha * 40}",
                                             "author": {"login": login} if login else None,
                                             "commit": {"message": msg, "author": {"name": "Dave D", "date": "2026-11-09T02:00:00Z"}}}
    api[path] = [commit("a", "init")]
    p = params(what="commits", branch="dev")
    _, st = await gh.check(p, None)
    assert api.seen[-1].url.params["sha"] == "dev"
    api[path] = [commit("c", "feat: 加了导出\n\n可以导出成 CSV 了", login=None), commit("b", "fix: 修了崩溃"), commit("a", "init")]
    evs, _ = await gh.check(p, st)
    assert [e["title"] for e in evs] == ["[octo/demo] 新提交 ccccccc：feat: 加了导出（Dave D）",
                                         "[octo/demo] 新提交 bbbbbbb：fix: 修了崩溃（dave）"]
    assert "可以导出成 CSV 了" in evs[0]["text"] and "分支：dev" in evs[0]["text"]


async def test_default_branch_and_empty_repo(api):
    api[f"/repos/{REPO}/commits"] = httpx.Response(409, json={"message": "Git Repository is empty."})
    evs, st = await gh.check(params(what="commits"), None)
    assert evs == [] and st["seen"] == []
    assert "sha" not in api.seen[-1].url.params


async def test_tags(api):
    path = f"/repos/{REPO}/tags"
    tag = lambda name: {"name": name, "commit": {"sha": "f" * 40}}
    api[path] = [tag("v1.0")]
    p = params(what="tags")
    _, st = await gh.check(p, None)
    api[path] = [tag("v1.1"), tag("v1.0")]
    evs, _ = await gh.check(p, st)
    assert evs[0]["title"] == "[octo/demo] 打了新标签 v1.1"
    assert evs[0]["url"] == f"https://github.com/{REPO}/releases/tag/v1.1" and "fffffff" in evs[0]["text"]


async def test_switching_what_rebaselines_and_flood_capped(api):
    api[f"/repos/{REPO}/releases"] = [release(1, "v1")]
    _, st = await gh.check(params(), None)
    api[f"/repos/{REPO}/tags"] = [{"name": f"t{i}"} for i in range(30)]
    evs, st = await gh.check(params(what="tags"), st)
    assert evs == []                                       # 换成看标签：先记下现状，不把 30 个老标签当新的

    api[f"/repos/{REPO}/tags"] = [{"name": f"n{i}"} for i in range(25)] + [{"name": f"t{i}"} for i in range(30)]
    evs, st = await gh.check(params(what="tags"), st)
    assert len(evs) == gh.MAX_NEW and "一共有 25 条新的" in evs[0]["text"]


# ---------- 报错 ----------
async def test_errors_are_explained(api):
    with pytest.raises(ValueError, match="找不到仓库 octo/demo"):
        await gh.check(params(), None)
    with pytest.raises(ValueError, match="分支 nope"):
        await gh.check(params(what="commits", branch="nope"), None)

    api[f"/repos/{REPO}/releases"] = httpx.Response(
        403, json={"message": "API rate limit exceeded for 1.2.3.4."},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1794175391"})
    with pytest.raises(ValueError, match="调用次数用完了.*60 次.*token"):
        await gh.check(params(), None)

    api[f"/repos/{REPO}/releases"] = httpx.Response(401, json={"message": "Bad credentials"})
    with pytest.raises(ValueError, match="token"):
        await gh.check(params() | {"token": "bad"}, None)

    api[f"/repos/{REPO}/releases"] = httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
    with pytest.raises(ValueError, match="拒绝了.*Resource not accessible.*权限"):
        await gh.check(params() | {"token": "t"}, None)

    api[f"/repos/{REPO}/releases"] = httpx.Response(502)
    with pytest.raises(ValueError, match="HTTP 502"):
        await gh.check(params(), None)

    def broken(req):
        raise httpx.ConnectError("connection reset", request=req)
    api[f"/repos/{REPO}/releases"] = broken
    with pytest.raises(ValueError, match="连不上 GitHub"):
        await gh.check(params(), None)
