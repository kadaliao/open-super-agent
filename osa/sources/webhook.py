"""外面推过来的事件：通用 Webhook、GitHub。

地址是 /hooks/<密钥>，密钥每个事件一个、随机生成，地址本身就是凭据（GitHub、表单、监控服务大多不能加自定义请求头）。
"""
import hashlib
import hmac
import json
from ..i18n import N_
from ..registry import PUSH, Source
from ._util import oneline, short

MAX_JSON = 12_000


# ---------- 通用 ----------
def _title_of(data, text: str) -> str:
    """从常见字段里挑一行标题：title、subject、summary、message……"""
    if isinstance(data, dict):
        for k in ("title", "subject", "summary", "message", "text", "event", "type", "action", "status", "name"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return oneline(v, 120)
            if isinstance(v, dict):
                for kk in ("title", "name", "message"):
                    if isinstance(v.get(kk), str) and v[kk].strip():
                        return oneline(v[kk], 120)
    return oneline(text, 120) or "收到一个 webhook"


def parse_generic(req: dict, params: dict):
    data = req.get("json")
    if data is not None:
        text = json.dumps(data, ensure_ascii=False, indent=1)
    else:
        text = (req.get("body") or b"").decode(errors="replace")
    if not text.strip():
        return "请求是空的"
    return [{"title": _title_of(data, text), "text": short(text, MAX_JSON)}]


WEBHOOK = Source(
    id="webhook", mode=PUSH, category=N_("通用"), icon="🪝",
    title=N_("收到 Webhook"),
    description=N_("任何能发 webhook 的服务（表单、监控报警、支付通知、快捷指令……）往这个地址 POST，Bot 就处理一次。"),
    parse=parse_generic,
    setup=N_("把地址填进对方的 webhook 设置，用 POST 发 JSON 或文字都行。地址本身就是密钥，别公开；泄露了在编辑里点“换一个地址”。"),
    sample={"title": "示例事件", "text": json.dumps({"event": "test", "message": "这是一条示例事件"}, ensure_ascii=False, indent=1)},
    scenarios=[
        {"title": N_("收到 Webhook 时处理"), "description": N_("任何服务推过来的消息，按你的要求处理"),
         "name": N_("Webhook"), "prompt": N_("看看收到的内容是什么，用两三句话告诉我要点；需要我做什么的，列出来。")},
        {"title": N_("监控报警时分析"), "description": N_("Grafana、Uptime Kuma、Prometheus 的报警推过来，先分析再告诉你"),
         "name": N_("监控报警"), "prompt": N_("这是一条监控报警。说清楚是哪个服务、什么问题、从什么时候开始，推测可能的原因，给出排查的第一步。报警已恢复的只用一句话告诉我。")},
        {"title": N_("Sentry 报错时排查"), "description": N_("新的报错推过来，Bot 看堆栈、猜原因"),
         "name": N_("Sentry 报错"), "prompt": N_("这是 Sentry 推来的报错。看错误信息和堆栈，判断可能的原因和受影响的功能，给出修复建议；明显是噪音（爬虫、已知问题）的写 [无新内容]。")},
        {"title": N_("表单有人提交时整理"), "description": N_("问卷、报名表（Tally、金数据、腾讯问卷……）有新提交"),
         "name": N_("表单提交"), "prompt": N_("有人提交了表单。把内容整理成一段清楚的摘要；是投诉或需要尽快回复的，放在最前面提醒我。")},
        {"title": N_("付款、订单通知"), "description": N_("Stripe、面包多、爱发电这类付款或订单通知"),
         "name": N_("订单通知"), "prompt": N_("这是一条付款或订单通知。告诉我谁买了什么、多少钱；退款、拒付这类要我处理的放在最前面。")},
        {"title": N_("从快捷指令触发"), "description": N_("iPhone 快捷指令、自动化工具用 POST 发一段文字过来，Bot 照着做"),
         "name": N_("快捷指令"), "prompt": N_("收到的文字是我从快捷指令发来的要求，照着做，做完简短告诉我结果。")},
    ])


# ---------- GitHub ----------
GH_EVENTS = [
    ("issues", N_("issue（新开、关闭、重开）")), ("issue_comment", N_("issue 和 PR 的评论")),
    ("pull_request", N_("PR（新开、更新、合并）")), ("pull_request_review", N_("PR 的审查")),
    ("push", N_("推送代码")), ("release", N_("发布版本")), ("workflow_run", N_("Actions 跑完")),
    ("discussion", N_("讨论区")), ("star", N_("点星")), ("fork", N_("fork")),
]


def _gh_sig_ok(secret: str, body: bytes, sig: str) -> bool:
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig or "")


def _who(d: dict) -> str:
    return (d.get("sender") or {}).get("login") or ""


def _is_bot(d: dict) -> bool:
    s = d.get("sender") or {}
    return s.get("type") == "Bot" or str(s.get("login") or "").endswith("[bot]")


def gh_event(kind: str, d: dict) -> dict | str:
    """一个 GitHub 事件 → {title, text, url}；不值得处理的返回原因。"""
    repo = (d.get("repository") or {}).get("full_name") or ""
    who = _who(d)
    action = d.get("action") or ""
    pre = f"[{repo}] " if repo else ""
    if kind == "issues":
        i = d.get("issue") or {}
        if action not in ("opened", "reopened", "closed", "labeled", "assigned"):
            return f"issue 的 {action}，不处理"
        labels = "、".join(x.get("name", "") for x in i.get("labels") or [])
        verb = {"opened": "新 issue", "reopened": "重开 issue", "closed": "关闭 issue", "labeled": "issue 加了标签",
                "assigned": "issue 指派了人"}[action]
        return {"title": f"{pre}{verb} #{i.get('number')}：{i.get('title', '')}（{who}）", "url": i.get("html_url", ""),
                "text": (f"标签：{labels}\n" if labels else "") + short(i.get("body") or "", 3000)}
    if kind == "issue_comment":
        i, c = d.get("issue") or {}, d.get("comment") or {}
        if action != "created":
            return f"评论的 {action}，不处理"
        where = "PR" if i.get("pull_request") else "issue"
        return {"title": f"{pre}{who} 评论了 {where} #{i.get('number')}：{i.get('title', '')}", "url": c.get("html_url", ""),
                "text": short(c.get("body") or "", 3000)}
    if kind == "pull_request":
        p = d.get("pull_request") or {}
        if action == "closed":
            verb = "合并了 PR" if p.get("merged") else "关闭了 PR"
        elif action in ("opened", "reopened", "ready_for_review", "synchronize"):
            verb = {"opened": "新 PR", "reopened": "重开 PR", "ready_for_review": "PR 可以审了", "synchronize": "PR 有新提交"}[action]
        else:
            return f"PR 的 {action}，不处理"
        stat = f"改了 {p.get('changed_files', '?')} 个文件，+{p.get('additions', '?')} -{p.get('deletions', '?')}"
        return {"title": f"{pre}{verb} #{p.get('number')}：{p.get('title', '')}（{who}）", "url": p.get("html_url", ""),
                "text": f"{p.get('head', {}).get('ref', '')} → {p.get('base', {}).get('ref', '')}，{stat}\n" + short(p.get("body") or "", 3000)}
    if kind == "pull_request_review":
        rv, p = d.get("review") or {}, d.get("pull_request") or {}
        if action != "submitted":
            return f"审查的 {action}，不处理"
        state = {"approved": "批准了", "changes_requested": "要求修改", "commented": "评论了"}.get(rv.get("state", ""), rv.get("state", ""))
        return {"title": f"{pre}{who} {state} PR #{p.get('number')}：{p.get('title', '')}", "url": rv.get("html_url", ""),
                "text": short(rv.get("body") or "", 3000)}
    if kind == "push":
        commits = d.get("commits") or []
        if not commits:
            return "没有提交的推送（建分支、删分支），不处理"
        branch = str(d.get("ref") or "").removeprefix("refs/heads/")
        lines = [f"- {c.get('id', '')[:7]} {oneline(c.get('message') or '', 120)}（{(c.get('author') or {}).get('name', '')}）"
                 for c in commits[:20]]
        return {"title": f"{pre}{who} 推送了 {len(commits)} 个提交到 {branch}", "url": d.get("compare", ""),
                "text": "\n".join(lines) + (f"\n……共 {len(commits)} 个" if len(commits) > 20 else "")}
    if kind == "release":
        rl = d.get("release") or {}
        if action not in ("published", "released", "prereleased"):
            return f"发布的 {action}，不处理"
        return {"title": f"{pre}发布了 {rl.get('tag_name', '')}：{rl.get('name') or ''}{'（预发布）' if rl.get('prerelease') else ''}",
                "url": rl.get("html_url", ""), "text": short(rl.get("body") or "", 5000)}
    if kind == "workflow_run":
        w = d.get("workflow_run") or {}
        if action != "completed":
            return f"Actions 的 {action}，还没跑完，不处理"
        concl = w.get("conclusion") or ""
        return {"title": f"{pre}Actions「{w.get('name', '')}」{'失败' if concl in ('failure', 'timed_out', 'startup_failure') else concl}："
                         f"{w.get('head_branch', '')} {oneline(w.get('display_title') or '', 80)}",
                "url": w.get("html_url", ""), "text": f"结论：{concl}\n触发：{w.get('event', '')}，提交 {str(w.get('head_sha', ''))[:7]}",
                "conclusion": concl}
    if kind == "discussion":
        ds = d.get("discussion") or {}
        if action != "created":
            return f"讨论的 {action}，不处理"
        return {"title": f"{pre}新讨论 #{ds.get('number')}：{ds.get('title', '')}（{who}）", "url": ds.get("html_url", ""),
                "text": short(ds.get("body") or "", 3000)}
    if kind == "star":
        if action != "created":
            return "取消点星，不处理"
        n = (d.get("repository") or {}).get("stargazers_count")
        return {"title": f"{pre}{who} 点了星" + (f"（现在 {n} 颗）" if n else ""), "url": (d.get("sender") or {}).get("html_url", ""), "text": ""}
    if kind == "fork":
        f = d.get("forkee") or {}
        return {"title": f"{pre}{who} fork 了仓库", "url": f.get("html_url", ""), "text": ""}
    return {"title": f"{pre}{kind} {action}".strip(), "text": short(json.dumps(d, ensure_ascii=False, indent=1), MAX_JSON)}


def parse_github(req: dict, params: dict):
    h = req["headers"]
    kind = h.get("x-github-event") or ""
    if params.get("secret"):
        if not _gh_sig_ok(params["secret"], req["body"], h.get("x-hub-signature-256", "")):
            raise PermissionError("签名不对：GitHub 里填的 Secret 和这里的不一样")
    if not kind:
        return "不像是 GitHub 发来的（没有 X-GitHub-Event）"
    d = req.get("json")
    if not isinstance(d, dict):
        return "GitHub 发来的不是 JSON：在 GitHub 的 webhook 设置里把 Content type 选成 application/json"
    if kind == "ping":
        return f"收到 GitHub 的 ping，地址配好了（{(d.get('repository') or {}).get('full_name') or d.get('hook_id', '')}）"
    if params.get("events") and kind not in params["events"]:
        return f"没选这类事件：{kind}"
    if params.get("skip_bots", True) and _is_bot(d):
        return f"机器人（{_who(d)}）的，不处理"
    ev = gh_event(kind, d)
    if isinstance(ev, str):
        return ev
    if kind == "workflow_run" and params.get("only_failed", True) and ev.get("conclusion") in ("success", "skipped", "neutral"):
        return f"Actions 成功了（{ev['conclusion']}），不处理"
    ev.pop("conclusion", None)
    return [ev]


GITHUB = Source(
    id="github", mode=PUSH, category=N_("开发"), icon="🐙",
    title=N_("GitHub 推送"),
    description=N_("仓库里有新 issue、PR、评论、发布、Actions 失败时，GitHub 推过来，Bot 处理。要有仓库的管理权限才能配。"),
    fields=[
        {"key": "events", "label": N_("要哪些事件"), "type": "multi", "options": [{"value": v, "label": lb} for v, lb in GH_EVENTS],
         "help": N_("不选就是 GitHub 发来的都要；GitHub 那边也要勾上对应的事件")},
        {"key": "skip_bots", "label": N_("忽略机器人（dependabot 之类）"), "type": "bool", "default": True},
        {"key": "only_failed", "label": N_("Actions 只管失败的"), "type": "bool", "default": True},
        {"key": "secret", "label": N_("Secret（可选）"), "type": "secret",
         "help": N_("GitHub 的 webhook 设置里填了 Secret 的话，这里填一样的，会校验签名；留空也行，地址本身就是密钥")},
    ],
    parse=parse_github,
    setup=N_("在 GitHub 仓库的 Settings → Webhooks → Add webhook：Payload URL 填 {url}，Content type 选 application/json，"
             "再选要发哪些事件。保存后 GitHub 会先发一个 ping，运行记录里显示“地址配好了”就通了。"),
    summary=lambda p: "、".join(p.get("events") or []) or "",
    sample={"title": "[octo/demo] 新 issue #42：点保存没反应（alice）", "url": "https://github.com/octo/demo/issues/42",
            "text": "标签：bug\n在设置页改了名字点保存，按钮转一下就没反应了，刷新后名字没变。Chrome 130。"},
    scenarios=[
        {"title": N_("新 issue 自动分析"), "description": N_("判断是 bug、需求还是提问，给出可能原因和回复草稿"),
         "name": N_("GitHub issue"), "params": {"events": ["issues"]},
         "llm_filter": N_("只有新开的、报告 bug 或故障、或者需要回复的问题才处理；纯通知、关闭、机器人的忽略"),
         "prompt": N_("看这个 issue：判断是 bug、功能请求还是提问；是 bug 的推测可能原因和要看的代码位置；最后写一段可以直接贴上去的回复草稿。")},
        {"title": N_("Actions 失败时排查"), "description": N_("CI 挂了，Bot 看是哪一步、可能为什么"),
         "name": N_("CI 失败"), "params": {"events": ["workflow_run"], "only_failed": True},
         "prompt": N_("GitHub Actions 失败了。打开链接看是哪个任务、哪一步失败，找出最可能的原因（代码问题、依赖、偶发），告诉我怎么修；偶发的建议重跑。")},
        {"title": N_("新 PR 先审一遍"), "description": N_("有人提 PR，Bot 先看改动、指出明显问题"),
         "name": N_("PR 初审"), "params": {"events": ["pull_request"]},
         "llm_filter": N_("只处理新开的 PR 和标记为可以审的 PR"),
         "prompt": N_("有新 PR。打开链接看改动，用三五句话说清楚改了什么，指出明显的问题（缺测试、破坏兼容、安全隐患），给出审查意见草稿。")},
        {"title": N_("发版后写更新说明"), "description": N_("发布新版本时整理成给用户看的更新说明"),
         "name": N_("发版说明"), "params": {"events": ["release"]},
         "prompt": N_("仓库发布了新版本。把发布说明改写成给普通用户看的更新说明：新功能、修复、需要注意的变化，各几条，去掉技术细节。")},
        {"title": N_("评论里有人问问题"), "description": N_("issue / PR 下面有新评论，需要回复的提醒你"),
         "name": N_("GitHub 评论"), "params": {"events": ["issue_comment"]},
         "llm_filter": N_("只有在提问、求助或者在等维护者回复的评论才处理"),
         "prompt": N_("有人在 GitHub 上评论。说清楚他在问什么、急不急，写一段回复草稿。")},
    ])

SOURCES = [WEBHOOK, GITHUB]
