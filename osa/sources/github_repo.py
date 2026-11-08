"""盯一个 GitHub 仓库的动态：新版本、新标签、新提交、新 issue、新 PR。不用配 webhook。

webhook.py 里的 github 要有仓库的管理权限才能配；这个隔一段时间调一次 GitHub 的接口去看，别人的项目也能盯。
不填 token 时 GitHub 每个 IP 每小时只让调 60 次（同一台机器上别的程序也算在里面），所以默认一小时看一次、最快 15 分钟；
填了 token 是每小时 5000 次，私有仓库也能看。
state 记看到过的 id（release 的 id、标签名、提交的 sha、issue / PR 的编号），盯的东西换了就重新记下现状。
"""
import re
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from ..i18n import N_
from ..registry import POLL, Source
from . import _util
from ._util import oneline, short

API = "https://api.github.com"
MAX_SEEN = 500
MAX_NEW = 20
WHAT = [("releases", N_("新版本（Release）")), ("tags", N_("新标签（Tag）")), ("commits", N_("新提交")),
        ("issues", N_("新 issue")), ("pulls", N_("新 PR"))]
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_BRANCH = re.compile(r"[^\s~^:?*\[\\]{1,200}")


# ---------- 参数 ----------
def parse_repo(s: str) -> tuple[str, str]:
    """“owner/repo”、仓库网址、git@ 地址 → (owner/repo, 网址里带的分支)。不对就 raise ValueError。"""
    s = str(s or "").strip().split("?")[0].split("#")[0]
    s = re.sub(r"^(?:git@github\.com:|(?:https?://)?(?:www\.)?github\.com/)", "", s, flags=re.I)
    parts = [x for x in s.split("/") if x]
    repo = "/".join(parts[:2]).removesuffix(".git")
    if len(parts) < 2 or not _REPO.fullmatch(repo) or repo.split("/")[1] in (".", ".."):
        raise ValueError("仓库要写成 owner/repo（比如 ollama/ollama），或者直接贴仓库的网址")
    branch = "/".join(parts[3:]) if len(parts) > 3 and parts[2] in ("tree", "commits") else ""
    return repo, branch


def validate(p: dict) -> dict:
    p["repo"], branch = parse_repo(p.get("repo"))
    p["what"] = p.get("what") or "releases"
    if p["what"] == "commits" and not p.get("branch") and branch:
        p["branch"] = branch          # 贴的是 github.com/o/r/tree/dev 这种网址，分支就用网址里的
    if p.get("branch") and not _BRANCH.fullmatch(p["branch"]):
        raise ValueError("分支名里不能有空格和 ~ ^ : ? * [ \\ 这些字符")
    return p


# ---------- 调接口 ----------
def _error(r: httpx.Response, p: dict) -> str:
    """GitHub 的报错 → 一句用户看得懂的话。"""
    code, repo = r.status_code, p.get("repo", "")
    try:
        msg = str((r.json() or {}).get("message") or "")
    except Exception:
        msg = ""
    if code in (403, 429) and (r.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in msg.lower()):
        reset = r.headers.get("x-ratelimit-reset", "")
        when = time.strftime("%H:%M", time.localtime(int(reset))) + " 之后" if reset.isdigit() else "过一会儿"
        if p.get("token"):
            return f"GitHub 的调用次数用完了，{when}恢复"
        return (f"GitHub 的调用次数用完了：不填 token 每个 IP 每小时只能调 60 次（这台机器上别的程序也算），{when}恢复。"
                "填一个 token 能到每小时 5000 次")
    if code == 401:
        return "GitHub 不认这个 token：填错了或者过期了"
    if code in (404, 422) and p.get("what") == "commits" and p.get("branch"):
        return f"找不到 {repo} 的分支 {p['branch']}：仓库名或分支名写错了，或者是私有仓库（要填有权限的 token）"
    if code == 404:
        return f"找不到仓库 {repo}：名字写错了，或者是私有仓库（要填有权限的 token）"
    if code == 403:
        return f"GitHub 拒绝了：{msg or 'HTTP 403'}" + ("（token 可能没有这个仓库的权限）" if p.get("token") else "")
    if code >= 500:
        return f"GitHub 那边出错了（HTTP {code}），下次再看"
    return f"GitHub 返回了错误：HTTP {code}" + (f" {msg}" if msg else "")


async def _api(path: str, query: dict, p: dict) -> list:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if p.get("token"):
        h["Authorization"] = f"Bearer {p['token']}"
    try:
        r = await _util.get(API + path, params=query, headers=h, timeout=20, max_bytes=5_000_000)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:      # 空仓库（还没有提交）
            return []
        raise ValueError(_error(e.response, p)) from None
    except httpx.TimeoutException:
        raise ValueError("连 GitHub 超时了，下次再看") from None
    except httpx.RequestError as e:
        raise ValueError(f"连不上 GitHub：{oneline(str(e), 150) or type(e).__name__}") from None
    data = r.json()
    return data if isinstance(data, list) else []


def _t(iso) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def _login(d) -> str:
    return (d.get("login") or "") if isinstance(d, dict) else ""


def _meta(*pairs) -> str:
    return "　".join(f"{k}：{v}" for k, v in pairs if v)


# ---------- 各种动态 → [(id, 事件)]，新的在前 ----------
async def _releases(p: dict) -> list:
    repo, out = p["repo"], []
    for rl in await _api(f"/repos/{repo}/releases", {"per_page": 30}, p):
        if not isinstance(rl, dict) or rl.get("draft"):
            continue
        tag, name = rl.get("tag_name") or "", (rl.get("name") or "").strip()
        head = tag + (f"：{oneline(name, 100)}" if name and name != tag else "")
        body = short(rl.get("body") or "", 4000) or "（没写发布说明）"
        out.append((f"r{rl.get('id')}", {
            "title": f"[{repo}] 发布了 {head}" + ("（预发布）" if rl.get("prerelease") else ""),
            "url": rl.get("html_url") or f"https://github.com/{repo}/releases",
            "text": _meta(("发布时间", _t(rl.get("published_at") or rl.get("created_at"))), ("发布人", _login(rl.get("author"))))
                    + "\n" + body}))
    return out


async def _tags(p: dict) -> list:
    repo, out = p["repo"], []
    for tg in await _api(f"/repos/{repo}/tags", {"per_page": 100}, p):
        name = tg.get("name") if isinstance(tg, dict) else ""
        if not name:
            continue
        sha = ((tg.get("commit") or {}).get("sha") or "")[:7]
        out.append((f"t{name}", {
            "title": f"[{repo}] 打了新标签 {name}", "url": f"https://github.com/{repo}/releases/tag/{quote(name)}",
            "text": f"指向的提交：{sha}\n这个标签之前的提交：https://github.com/{repo}/commits/{quote(name)}"}))
    return out


async def _commits(p: dict) -> list:
    repo, out = p["repo"], []
    q = {"per_page": 30} | ({"sha": p["branch"]} if p.get("branch") else {})
    for c in await _api(f"/repos/{repo}/commits", q, p):
        if not isinstance(c, dict) or not c.get("sha"):
            continue
        cm = c.get("commit") or {}
        lines = (cm.get("message") or "").strip().splitlines() or [""]
        who = _login(c.get("author")) or (cm.get("author") or {}).get("name") or ""
        out.append((f"c{c['sha'][:12]}", {
            "title": f"[{repo}] 新提交 {c['sha'][:7]}：{oneline(lines[0], 120)}" + (f"（{who}）" if who else ""),
            "url": c.get("html_url") or f"https://github.com/{repo}/commit/{c['sha']}",
            "text": _meta(("时间", _t((cm.get("author") or {}).get("date"))), ("分支", p.get("branch")))
                    + ("\n" + short("\n".join(lines[1:]).strip(), 1500) if len(lines) > 1 else "")}))
    return out


async def _issues(p: dict) -> list:
    repo, out = p["repo"], []
    # state=all：开了又马上关掉的也算新 issue；接口把 PR 也当 issue 返回，去掉
    for i in await _api(f"/repos/{repo}/issues", {"state": "all", "sort": "created", "direction": "desc", "per_page": 30}, p):
        if not isinstance(i, dict) or i.get("pull_request"):
            continue
        labels = "、".join(x.get("name", "") for x in i.get("labels") or [] if isinstance(x, dict))
        out.append((f"i{i.get('number')}", {
            "title": f"[{repo}] 新 issue #{i.get('number')}：{oneline(i.get('title') or '', 150)}（{_login(i.get('user'))}）"
                     + ("（已关闭）" if i.get("state") == "closed" else ""),
            "url": i.get("html_url") or "",
            "text": _meta(("时间", _t(i.get("created_at"))), ("标签", labels)) + "\n" + short(i.get("body") or "", 2000)}))
    return out


async def _pulls(p: dict) -> list:
    repo, out = p["repo"], []
    for pr in await _api(f"/repos/{repo}/pulls", {"state": "all", "sort": "created", "direction": "desc", "per_page": 30}, p):
        if not isinstance(pr, dict):
            continue
        state = "（已合并）" if pr.get("merged_at") else "（已关闭）" if pr.get("state") == "closed" else \
            "（草稿）" if pr.get("draft") else ""
        branches = f"{(pr.get('head') or {}).get('label', '')} → {(pr.get('base') or {}).get('ref', '')}"
        out.append((f"p{pr.get('number')}", {
            "title": f"[{repo}] 新 PR #{pr.get('number')}：{oneline(pr.get('title') or '', 150)}（{_login(pr.get('user'))}）{state}",
            "url": pr.get("html_url") or "",
            "text": _meta(("时间", _t(pr.get("created_at"))), ("分支", branches)) + "\n" + short(pr.get("body") or "", 2000)}))
    return out


FETCH = {"releases": _releases, "tags": _tags, "commits": _commits, "issues": _issues, "pulls": _pulls}


async def check(params: dict, state):
    what = params.get("what") or "releases"
    if what not in FETCH:
        raise ValueError(f"不认识要看的东西：{what}")
    items = await FETCH[what](params)
    key = f"{params['repo']}|{what}|{params.get('branch') or ''}"
    same = isinstance(state, dict) and state.get("key") == key
    seen = (state.get("seen") or []) if same else []
    new_state = {"key": key, "seen": list(dict.fromkeys([k for k, _ in items] + seen))[:MAX_SEEN]}
    if state is None:     # 第一次：只记下现状；最新的一条留作“测试”的样例
        return [items[0][1]] if items else [], new_state
    if not same:          # 换了仓库、分支或者要看的东西：重新记下现状
        return [], new_state
    old, fresh = set(seen), []
    for k, ev in items:
        if k not in old:
            old.add(k)
            fresh.append(ev | {"id": k})
    events = fresh[:MAX_NEW]
    if len(fresh) > MAX_NEW:
        events[0]["text"] += f"\n（这次一共有 {len(fresh)} 条新的，只列了最新的 {MAX_NEW} 条）"
    return events, new_state


def _summary(p: dict) -> str:
    repo = p.get("repo") or ""
    if (p.get("what") or "releases") == "commits":
        return f"{repo}@{p['branch']} · commits" if p.get("branch") else f"{repo} · commits"
    return f"{repo} · {p.get('what') or 'releases'}"


GITHUB_REPO = Source(
    id="github_repo", mode=POLL, category=N_("开发"), icon="📦",
    title=N_("GitHub 仓库有新动态"),
    description=N_("盯任何一个 GitHub 仓库的新版本、新标签、新提交、新 issue 或新 PR，不用配 webhook，别人的项目也能盯。"),
    fields=[
        {"key": "repo", "label": N_("仓库"), "type": "text", "required": True, "placeholder": "owner/repo",
         "help": N_("写成 owner/repo，比如 ollama/ollama；直接贴仓库的网址也行")},
        {"key": "what", "label": N_("看什么"), "type": "select", "default": "releases", "required": True,
         "options": [{"value": v, "label": lb} for v, lb in WHAT]},
        {"key": "branch", "label": N_("分支（可选）"), "type": "text", "placeholder": "main",
         "help": N_("只在看“新提交”时有用，不填就是仓库的默认分支")},
        {"key": "token", "label": N_("GitHub token（可选）"), "type": "secret",
         "help": N_("不填也能用，但 GitHub 每小时只让调 60 次；填了能到 5000 次，私有仓库也能看。"
                    "在 GitHub 的 Settings → Developer settings → Personal access tokens 里建一个只读的")},
    ],
    check=check, validate=validate, every=60, min_every=15,
    summary=_summary,
    sample={"title": "[ollama/ollama] 发布了 v0.12.0", "url": "https://github.com/ollama/ollama/releases/tag/v0.12.0",
            "text": "发布时间：2026-11-08 09:12　发布人：jmorganca\n"
                    "## What's Changed\n- New models: Qwen4, Gemma 4\n- Faster model loading on Apple Silicon\n"
                    "- **Breaking:** the `/api/generate` `context` field has been removed, use `/api/chat` instead\n"
                    "- Fixed a crash when the GPU runs out of memory"},
    scenarios=[
        {"title": N_("关注的项目发新版本"), "description": N_("用的开源项目发版时，总结更新内容，把不兼容的改动挑出来"),
         "name": N_("新版本"), "params": {"repo": "ollama/ollama", "what": "releases"},
         "prompt": N_("这个项目发布了新版本。用三五条要点总结更新了什么；不兼容的改动（breaking changes）、要手动迁移的放在最前面提醒我；"
                      "只修了些小问题的一句话带过。")},
        {"title": N_("有人给项目提 issue（不用配 webhook）"), "description": N_("没有管理权限也能用：新 issue 来了先分析一遍"),
         "name": N_("新 issue"), "params": {"what": "issues"}, "every": 30,
         "prompt": N_("项目有新 issue。判断是 bug、功能请求还是提问，急不急；是 bug 的推测可能原因；最后写一段可以直接回复的草稿。"
                      "明显是广告、重复的写 [无新内容]。")},
        {"title": N_("仓库有新提交"), "description": N_("某个分支有新提交时，总结改了什么（不填分支就是默认分支）"),
         "name": N_("新提交"), "params": {"what": "commits"}, "exclude": ["dependabot", "chore(deps)", "renovate"],
         "prompt": N_("仓库有新提交。按新功能、修复、重构、其他归类，总结改了什么，重要的改动说清楚影响；只改了文档、格式的一句话带过。")},
        {"title": N_("有新 PR"), "description": N_("有人提了新 PR，先看一眼值不值得优先审"),
         "name": N_("新 PR"), "params": {"what": "pulls"}, "every": 30,
         "prompt": N_("项目有新 PR。说清楚想改什么、改动大不大、有没有关联的 issue，值得我优先看的标出来；机器人提的依赖升级只写 [无新内容]。")},
        {"title": N_("项目打了新标签"), "description": N_("不发 Release、只打标签的项目，用这个盯新版本"),
         "name": N_("新标签"), "params": {"what": "tags"},
         "prompt": N_("项目打了新标签，可能是发了新版本。打开链接看看这个版本附近改了什么，简单总结；看着不是版本号的标签写 [无新内容]。")},
    ])

SOURCES = [GITHUB_REPO]
