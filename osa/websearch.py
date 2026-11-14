"""搜索网页（web_search）、读网页（fetch_url）：按“配方”调各家的接口，代码里不写死用哪家。

默认配方在 osa/websearch.yaml，config.yaml 的 web.recipes 可以加、改（kind 和 name 都一样的替掉默认的）。
设置 → 搜索和读网页 里选用哪几家、排先后、填密钥和地址，存在 settings 表的 web 一行（密钥进保险库）。
没选过是“自动”：配好了的都用（模型服务里有对应的、该填的填了），按配方的顺序；auto: false 的要自己勾上才用。
按顺序试，前一家出错、被限流、没结果就换下一家；换过的写在结果末尾。

配方（一条管一家的一类）：
- name、kind（search / fetch）、title、desc、link（去哪申请密钥）、en（英文界面的 title / desc）。
  搜索和读网页可以同名，同名的共用填的密钥和地址。
- 密钥从哪来：key: {provider: 正则} 用模型服务里接口地址对上这个正则、配好了密钥的那家（变量 origin 是它的协议和域名）；
  fields 是要用户填的：key、label、secret（是密钥：存进保险库，不再显示）、optional（可以不填）、placeholder、
  env（没填时看这个环境变量）、config（没填时看 config.yaml 里这个路径，比如以前的 search.searxng_url）。
- 怎么调：request 里写 method（默认 POST）、url、headers、params（网址后面的参数）、json 或 form。
  带了 days（只要最近几天的）时，recent 里写的几项替掉 request 里的同名项。
  模板同画图的配方（generation.render）：值正好是 $名字 的换成变量，没有这个变量的整项去掉；字符串里的 ${名字} 换成文字。
  变量：query、count、url（要读的网址）、days、since（days 天前，YYYY-MM-DD）、today、range（day / week / month / year，
  按 ranges 换成接口的叫法）、origin、fields 里的每一项、bearer（"Bearer 密钥"，没有密钥时没有这个变量）。
- 结果在哪：搜索写 results（结果列表在回包里的路径，可以写几个）和 item（每条的 title、url、text、date 在哪，可以写几个）；
  读网页写 page: {title, text}。parse 是代码里写好的解析：duckduckgo（DuckDuckGo 网页版）、direct（自己下载网页）。
- price_usd：每次成功调用多少美元，记进费用统计（观测页）。

不用的网站（web.block，设置里能改）：搜索结果里去掉，fetch_url 和浏览器都不打开。
"""
import datetime as dt
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
import httpx
import yaml
from . import i18n, providers, vault
from .config import CFG
from .generation import _error as http_error, _first, pick, render
from .memory import conn

KINDS = ("search", "fetch", "posts")
COUNT = 8              # 每次搜几条
POSTS = 10             # 搜帖子默认几条（X 一次最少 10 条，按条收钱）
TEXT = 300             # 每条摘要最多几个字（8 条就是两千多字，再长太占上下文）
MAX_CHARS = 8000       # fetch_url 返回的正文最多几个字
RANGES = ((1, "day"), (7, "week"), (31, "month"), (366, "year"))
UA = {"User-Agent": "open-super-agent/0.1"}
_TRANSPORT = None      # 测试时换成假的
_DEFAULTS = yaml.safe_load((Path(__file__).parent / "websearch.yaml").read_text(encoding="utf-8"))
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z0-9-]{2,63}$")


class Failed(Exception):
    """这一家这次不行（出错、限流、回包不对），换下一家。"""


# ---------- 配方和设置 ----------
def _cfg() -> dict:
    return CFG.get("web") or {}


def recipes(kind: str | None = None) -> list[dict]:
    """config.yaml 的 web.recipes 排在默认的前面；kind 和 name 都一样的默认配方不要了。"""
    mine = [r for r in (_cfg().get("recipes") or []) if isinstance(r, dict) and r.get("name") and r.get("kind") in KINDS]
    have = {(r["kind"], r["name"]) for r in mine}
    out = [*mine, *(r for r in _DEFAULTS["recipes"] if (r["kind"], r["name"]) not in have)]
    return [r for r in out if kind is None or r["kind"] == kind]


def recipe(kind: str, name: str) -> dict | None:
    return next((r for r in recipes(kind) if r["name"] == name), None)


def _saved() -> dict:
    r = conn.execute("select value from settings where key='web'").fetchone()
    try:
        d = json.loads(r[0]) if r else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _store(d: dict):
    conn.execute("insert or replace into settings values('web', ?)", (json.dumps(d, ensure_ascii=False),))
    conn.commit()


def _tr(r: dict, k: str) -> str:
    """配方里给人看的字（title、desc、label）：英文界面用 en 里写的。"""
    if i18n.lang() == "en" and (v := (r.get("en") or {}).get(k)):
        return v
    return r.get(k) or ""


def _vault_name(name: str, field: str) -> str:
    return "WEB_" + re.sub(r"\W", "_", f"{name}_{field}".upper())


def field_value(r: dict, f: dict) -> str:
    """用户填的值；没填看环境变量（env）、config.yaml（config）、默认值。"""
    if f.get("secret"):
        v = vault.get(_vault_name(r["name"], f["key"]))
    else:
        v = ((_saved().get("fields") or {}).get(r["name"]) or {}).get(f["key"])
    if not v and f.get("env"):
        v = os.environ.get(f["env"], "")
    if not v and f.get("config"):
        v = pick(CFG, f["config"])
    return str(v or f.get("default") or "").strip()


def _provider(r: dict) -> str | None:
    """key: {provider: 正则} 的配方用哪个模型服务的密钥；配方不用模型服务是 None，要用但没有配好的是空字符串。"""
    want = (r.get("key") or {}).get("provider")
    if not want:
        return None
    for name, p in CFG["providers"].items():
        if re.search(want, p.get("base_url") or "", re.I) and providers.usable(name):
            return name
    return ""


def missing(r: dict) -> str:
    """这一家现在用不了的原因；能用返回空。"""
    if _provider(r) == "":
        return i18n.t("要先在 设置 → 模型 里添加 {name} 的模型服务", name=_tr(r, "title"))
    for f in r.get("fields") or []:
        if not f.get("optional") and not field_value(r, f):
            return i18n.t("还没填：{label}", label=_tr(f, "label") or f["key"])
    return ""


def chosen(kind: str) -> list[str]:
    """设置里选的顺序（没选过看 config.yaml 的 web.search / web.fetch）；空的是“自动”。"""
    v = _saved().get(kind) or _cfg().get(kind) or []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _seen(kind: str) -> set[str] | None:
    """保存顺序那时已经有的服务；之后才加进配方的，在选过的列表里按自己的默认（auto）算开不开。
    以前保存的没记这个：当时只有不带 hosts 的（只管某些网站的读法是后来加的）。"""
    s = _saved()
    if kind not in s:
        return None
    seen = s.get(f"{kind}_seen")
    return set(seen) if isinstance(seen, list) else {r["name"] for r in recipes(kind) if not r.get("hosts")}


def _on(kind: str, r: dict, names: list[str]) -> bool:
    if not names:
        return bool(r.get("auto", True))
    if r["name"] in names:
        return True
    seen = _seen(kind)
    return seen is not None and r["name"] not in seen and bool(r.get("auto", True))


def order(kind: str) -> list[dict]:
    """这次按顺序试哪几家：选了的按选的顺序（后来新加的、默认开的排在后面），没选是配好了的全部（auto: false 的除外）。"""
    names = chosen(kind)
    rs = [r for n in names if (r := recipe(kind, n))] + [r for r in recipes(kind) if r["name"] not in names and _on(kind, r, names)]
    return [r for r in rs if not missing(r)]


def block_list() -> list[str]:
    s = _saved()
    v = s["block"] if "block" in s else _cfg().get("block")
    return [d for d in (str(x).strip().lower().strip(".") for x in v or []) if d]


def blocked_domain(url: str) -> str | None:
    """网址是不是在不用的网站里（域名本身和它的子域名），是的话返回那一项。"""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return next((d for d in block_list() if host == d or host.endswith("." + d)), None)


def refuse_text(url: str, fetching: bool = False) -> str:
    """不用的网站：给模型的话。fetching：fetch_url（和内网检查的说法一致，以“没抓取”开头）。"""
    host = urlsplit(url).hostname or url
    if fetching:
        return f"没抓取：{host} 在不用的网站里（设置 → 搜索和读网页），不要从这个网站找信息"
    return f"没打开：{host} 在不用的网站里（设置 → 搜索和读网页），不要从这个网站找信息"


# ---------- 调接口 ----------
def _vars(r: dict, **kw) -> dict:
    v = {k: x for k, x in kw.items() if x is not None}
    key = ""
    if name := _provider(r):
        p = urlsplit(CFG["providers"][name].get("base_url") or "")
        v["origin"] = f"{p.scheme}://{p.netloc}"
        key = providers.api_key(name)
    for f in r.get("fields") or []:
        if val := field_value(r, f):
            v[f["key"]] = val.rstrip("/") if f["key"] == "url" else val
            if f.get("secret") and not key:
                key = val
    if key:
        v["bearer"] = f"Bearer {key}"
    if days := kw.get("days"):
        today = dt.date.today()
        v["since"] = (today - dt.timedelta(days=days)).isoformat()
        v["today"] = today.isoformat()
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days) + dt.timedelta(minutes=1)   # 留一分钟，免得刚好超出“最近 7 天”
        v["since_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        bucket = next((b for n, b in RANGES if days <= n), "year")
        v["range"] = (r.get("ranges") or {}).get(bucket, bucket)
    return v


async def _send(c: httpx.AsyncClient, r: dict, v: dict) -> httpx.Response:
    req = dict(r.get("request") or {})
    days = v.get("days")
    if days and r.get("long") and days > int(r.get("long_after") or 0):
        req |= r["long"]          # 比 long_after 天还早的（X 的全部存档）
    elif days and r.get("recent"):
        req |= r["recent"]
    kw: dict = {}
    if "params" in req:
        kw["params"] = {k: str(x) for k, x in render(req["params"], v).items()}
    if "json" in req:
        kw["json"] = render(req["json"], v)
    elif "form" in req:
        kw["data"] = {k: str(x) for k, x in render(req["form"], v).items()}
    headers = UA | {k: str(x) for k, x in render(req.get("headers") or {}, v).items()}
    url = render(req.get("url") or "", v)
    try:
        resp = await c.request((req.get("method") or "POST").upper(), url, headers=headers, **kw)
    except httpx.TimeoutException:
        raise Failed(i18n.t("连接超时"))
    except httpx.HTTPError as e:
        raise Failed(i18n.t("连不上：{error}", error=type(e).__name__))
    if resp.status_code == 429:
        raise Failed(i18n.t("被限流了（HTTP 429）"))
    if resp.status_code >= 400:
        raise Failed(http_error(resp))
    return resp


def _json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        raise Failed(i18n.t("返回的不是 JSON：{text}", text=resp.text[:120]))


def _clean(s) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(s or "")))).strip()


def _ddg(text: str) -> list[dict]:
    """DuckDuckGo 网页版的结果。链接是 duckduckgo.com/l/?uddg=真实地址，取出真实地址；广告（y.js）不要。"""
    links = re.findall(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', text, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text, re.S)
    if not links and "anomaly" in text:
        raise Failed(i18n.t("DuckDuckGo 要人机验证（请求太多被拦了）"))
    out = []
    for i, (u, t) in enumerate(links):
        u = html.unescape(u)
        if u.startswith("//"):
            u = "https:" + u
        if "uddg=" in u:
            u = parse_qs(urlsplit(u).query).get("uddg", [u])[0]
        if (urlsplit(u).hostname or "").endswith("duckduckgo.com"):
            continue
        out.append({"title": _clean(t), "url": u, "text": _clean(snippets[i]) if i < len(snippets) else "", "date": None})
    return out


POST_FIELDS = ("author", "likes", "comments", "shares", "views")


def _get(x: dict, spec):
    """item 里一项的取法：路径（a.b.0），或带 ${路径} 的模板（https://x.com/i/web/status/${id}）；写几个时第一个有值的算数。"""
    for s in spec if isinstance(spec, list) else [spec]:
        if not isinstance(s, str):
            continue
        if "${" in s:
            parts = {m: pick(x, m) for m in re.findall(r"\$\{([\w.]+)\}", s)}
            if all(v not in (None, "") for v in parts.values()):
                return re.sub(r"\$\{([\w.]+)\}", lambda m: str(parts[m.group(1)]), s)
        elif (v := pick(x, s)) not in (None, ""):
            return v
    return None


def _items(r: dict, d) -> list[dict]:
    """按 results、item 从回包里取出一条条结果。回包里没有结果列表算没结果（X 没搜到时就不给 data）；带 errors 的算出错。"""
    paths = r.get("results") or "results"
    found = next((x for p in (paths if isinstance(paths, list) else [paths]) if isinstance(x := pick(d, p), list)), None)
    if found is None:
        if isinstance(d, dict) and (e := d.get("errors") or d.get("error")):
            e = e[0] if isinstance(e, list) and e else e
            raise Failed(str((e.get("message") or e.get("detail") or e) if isinstance(e, dict) else e)[:200])
        return []
    item = r.get("item") or {}
    out = []
    for x in found:
        if not isinstance(x, dict) or not isinstance(u := _get(x, item.get("url") or "url"), str) or not u.startswith(("http://", "https://")):
            continue
        e = {"title": _clean(_get(x, item.get("title") or "title")), "url": u,
             "text": _clean(_get(x, item.get("text") or "text")), "date": _get(x, item.get("date") or "date")}
        for k in POST_FIELDS:
            if k in item:
                e[k] = _get(x, item[k])
        out.append(e)
    return out


async def _search_one(c: httpx.AsyncClient, r: dict, query: str, days: int | None, count: int = COUNT) -> list[dict]:
    resp = await _send(c, r, _vars(r, query=query, count=count, days=days))
    if r.get("parse") == "duckduckgo":
        return _ddg(resp.text)
    return _items(r, _json(resp))


def _comment_lines(items, depth: int, out: list[str], budget: list[int]):
    """评论按楼层缩进；最多两层、一共四十条左右，每条三百字以内。"""
    for x in items or []:
        if budget[0] <= 0 or not isinstance(x, dict):
            return
        body = _clean(x.get("body"))
        if body and body not in ("[deleted]", "[removed]"):
            budget[0] -= 1
            score = x.get("score", x.get("ups"))
            out.append("  " * depth + f"- u/{x.get('author') or '?'}" + (f"（{_num(score)}）" if score not in (None, "") else "")
                       + f"：{body[:TEXT]}")
        if depth < 1:
            _comment_lines((x.get("replies") or {}).get("items"), depth + 1, out, budget)


def _reddit_comments(d) -> tuple[str, str]:
    post = d.get("post") or {} if isinstance(d, dict) else {}
    if not post and not (d or {}).get("comments"):
        raise Failed(i18n.t("回包里没有帖子"))
    meta = [f"r/{post['subreddit']}" if post.get("subreddit") else "", f"u/{post['author']}" if post.get("author") else "",
            _date(post.get("created_utc")), f"{i18n.t('赞')} {_num(post.get('score'))}" if post.get("score") else "",
            f"{i18n.t('评论')} {_num(post.get('num_comments'))}" if post.get("num_comments") else ""]
    lines = [" · ".join(x for x in meta if x)]
    if post.get("selftext"):
        lines += ["", str(post["selftext"]).strip()]
    comments: list[str] = []
    _comment_lines(d.get("comments"), 0, comments, [40])
    if comments:
        lines += ["", "## " + i18n.t("评论"), *comments]
    return str(post.get("title") or "").strip(), "\n".join(lines)


def _x_token(tid: str) -> str:
    """X 嵌入帖子的地址要带的 token：和 X 自己的嵌入代码一样，(编号 / 1e15 × π) 写成 36 进制、去掉 0 和小数点。现在不校验，照样算一个。"""
    x = int(tid) / 1e15 * 3.141592653589793
    whole, frac = int(x), x - int(x)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while whole:
        whole, d = divmod(whole, 36)
        out = digits[d] + out
    out = (out or "0") + "."
    for _ in range(11):
        frac *= 36
        out += digits[int(frac)]
        frac -= int(frac)
    return re.sub(r"0+|\.", "", out) or "a"


def _x_post(d) -> tuple[str, str]:
    """X 嵌入帖子的回包：作者、日期、点赞回复数、正文（t.co 短链换成原地址），引用的帖子跟在后面。"""
    if not isinstance(d, dict) or not (d.get("text") or d.get("note_tweet")):
        raise Failed(i18n.t("没有这条帖子（可能删了，或者要登录才能看）"))
    def body(t: dict) -> str:
        text = str(((t.get("note_tweet") or {}).get("text")) or t.get("text") or "")
        for u in (t.get("entities") or {}).get("urls") or []:
            if u.get("url") and u.get("expanded_url"):
                text = text.replace(u["url"], u["expanded_url"])
        return re.sub(r"\s*https://t\.co/\w+$", "", text).strip()   # 最后那个是图片、视频的短链
    user = d.get("user") or {}
    names = (("favorite_count", i18n.t("赞")), ("conversation_count", i18n.t("回复")))
    meta = [_date(d.get("created_at"))] + [f"{label} {n}" for k, label in names if (n := _num(d.get(k))) not in ("", "0")]
    lines = [" · ".join(x for x in meta if x), "", body(d)]
    if q := d.get("quoted_tweet"):
        lines += ["", f"> {i18n.t('引用')} @{(q.get('user') or {}).get('screen_name', '?')}：{body(q)}"]
    return f"@{user.get('screen_name', '?')}（{user.get('name', '')}）", "\n".join(lines)


def _getdaytrends(page: str) -> tuple[str, str]:
    """getdaytrends.com 的第一张表就是现在的热门话题：排名、话题名；每个话题附上 X 的搜索链接。"""
    m = re.search(r"<table[\s\S]*?</table>", page)
    rows = re.findall(r'class="pos">(\d+)</th>\s*<td class="main"><a[^>]*>([^<]+)</a>', m.group(0)) if m else []
    if not rows:
        raise Failed(i18n.t("页面里没找到热门话题表"))
    title = _clean((re.search(r"<h1[^>]*>([\s\S]*?)</h1>", page) or re.search(r"<title>([^<]+)", page) or [None, ""])[1])
    lines = [f"{n}. {html.unescape(t)} — https://x.com/search?q={quote(html.unescape(t))}" for n, t in rows[:30]]
    return title, i18n.t("（只有话题名和排名，看不到在说什么：要知道的话用 web_search 查话题名）") + "\n\n" + "\n".join(lines)


async def _fetch_one(c: httpx.AsyncClient, r: dict, url: str) -> tuple[str, str]:
    if r.get("parse") == "direct":
        from . import tools
        return "", await tools.direct_fetch(url)
    extra = {}
    for k, rx in (r.get("url_vars") or {}).items():   # 从网址里取的变量（X 帖子的编号）
        if not (m := re.search(rx, url)):
            raise Failed(i18n.t("这个网址读不了：{url}", url=url[:80]))
        extra[k] = m.group(1)
    if r.get("parse") == "x_post":
        extra["x_token"] = _x_token(extra["tweet_id"])
    resp = await _send(c, r, _vars(r, url=url, **extra))
    if r.get("parse") == "getdaytrends":
        return _getdaytrends(resp.text)
    d = _json(resp)
    if r.get("parse") == "x_post":
        return _x_post(d)
    if r.get("parse") == "reddit_comments":
        return _reddit_comments(d)
    page = r.get("page") or {}
    title, text = _first(d, page.get("title") or "title"), _first(d, page.get("text") or "text")
    return str(title or "").strip(), str(text or "")


def _date(v) -> str:
    if isinstance(v, (int, float)) and v > 1e8:   # Unix 时间（秒；Reddit 的 created_utc）
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc).date().isoformat()
    s = str(v or "").strip()
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else s[:24]


def _format(items: list[dict]) -> str:
    lines = []
    for e in items[:COUNT]:
        head = f"- {e['title'] or e['url']} | {e['url']}" + (f" | {d}" if (d := _date(e.get("date"))) else "")
        lines.append(head + (f"\n  {e['text'][:TEXT]}" if e.get("text") else ""))
    return "\n".join(lines)


def _num(v) -> str:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return ""
    return f"{n / 10000:.1f}万".replace(".0万", "万") if n >= 10000 else str(n)


def _format_posts(items: list[dict]) -> str:
    """帖子：作者、日期、互动数、链接，下面是正文开头。"""
    names = (("likes", i18n.t("赞")), ("comments", i18n.t("评论")), ("shares", i18n.t("转发")), ("views", i18n.t("浏览")))
    lines = []
    for e in items:
        meta = [str(e["author"]) if e.get("author") else "", _date(e.get("date"))]
        meta += [f"{label} {n}" for k, label in names if (n := _num(e.get(k))) not in ("", "0")]   # 0 的不写
        head = "- " + " · ".join(x for x in meta if x) + f" | {e['url']}"
        body = " ".join(x for x in (e.get("title"), e.get("text")) if x)
        lines.append(head + (f"\n  {body[:TEXT]}" if body else ""))
    return "\n".join(lines)


def _record(r: dict, what: str, ms: float, n: int = 1):
    """收钱的服务：费用统计里记一行（0 token，费用按配方的单价：每次 price_usd，或者每条结果 price_usd_item）。"""
    usd = float(r.get("price_usd") or 0) + float(r.get("price_usd_item") or 0) * n
    if not usd:
        return
    from . import memory, prices
    memory.add_usage("web", what, provider=_provider(r) or r["name"], cost=usd * prices._rate(), ms=int(ms))


def _client(timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=_TRANSPORT)


def _tail(notes: list[str], used: str = "") -> str:
    if not notes:
        return ""
    s = "；".join(notes)
    return "\n" + (i18n.t("（{notes}；这次用的是 {name}）", notes=s, name=used) if used else i18n.t("（{notes}）", notes=s))


# ---------- 两个工具 ----------
async def search(query: str, days: int | None = None, only: dict | None = None) -> str:
    """按顺序试各家，第一家有结果的算数。only：只试这一家（设置里的测试）。"""
    query = (query or "").strip()
    if not query:
        return i18n.t("没有给搜索词")
    rs = [only] if only else order("search")
    if not rs:
        return i18n.t("搜索不了：没有能用的搜索服务（设置 → 搜索和读网页）")
    notes, empty = [], 0
    async with _client() as c:
        for r in rs:
            t0 = time.monotonic()
            try:
                items = await _search_one(c, r, query, days)
            except Failed as e:
                notes.append(f"{_tr(r, 'title')}：{e}")
                continue
            if items:
                _record(r, "search", (time.monotonic() - t0) * 1000)
            items = [e for e in items if not blocked_domain(e["url"])]
            if items:
                return _format(items) + _tail(notes, _tr(r, "title"))
            empty += 1
            notes.append(i18n.t("{name}：没有结果", name=_tr(r, "title")))
    head = i18n.t("无结果") if empty else i18n.t("搜索失败")
    return head + _tail(notes)


async def fetch(url: str, private: bool = False, only: dict | None = None) -> str:
    """读一个网页：按顺序试各家。private（本机、内网地址，config 里允许了才走到这里）只能自己抓，不发给别家。"""
    if only:
        rs = [only]
    elif private:
        rs = [r for r in recipes("fetch") if r.get("parse") == "direct"][:1]
    else:
        host = (urlsplit(url).hostname or "").lower()
        mine = lambda r: any(host == h or host.endswith("." + h) for h in r.get("hosts") or [])   # noqa: E731
        rs = order("fetch")
        rs = [r for r in rs if mine(r)] + [r for r in rs if not r.get("hosts")]   # 只管某些网站的排前面，别的网站不用它
        rs = rs or [r for r in recipes("fetch") if r.get("parse") == "direct"][:1]
    notes = []
    async with _client(30) as c:
        for r in rs:
            t0 = time.monotonic()
            try:
                title, text = await _fetch_one(c, r, url)
            except Failed as e:
                notes.append(f"{_tr(r, 'title')}：{e}")
                continue
            except httpx.HTTPError as e:
                notes.append(f"{_tr(r, 'title')}：{type(e).__name__}")
                continue
            if r.get("parse") == "direct" and text.startswith("没抓取"):
                return text   # 直接抓时跳到了内网或不用的网站：原因原样给模型
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text:
                _record(r, "fetch", (time.monotonic() - t0) * 1000)
                head = f"# {title}\n\n" if title and title not in text.split("\n", 1)[0] else ""   # 正文第一行就是标题的不重复
                return head + text[:MAX_CHARS] + _tail(notes, _tr(r, "title"))
            notes.append(i18n.t("{name}：没读到内容", name=_tr(r, "title")))
    return i18n.t("没读到：{why}", why="；".join(notes) or url)


PLATFORMS = {"x": "X", "reddit": "Reddit", "xiaohongshu": "小红书"}
BUDGET = 0.05          # 每个任务搜按条收钱的帖子（X）最多花多少美元：X 一次 10 条 = 0.05
_spent: dict = {}      # 任务 → 这个任务在按条收钱的帖子上已经花的美元（只记在内存里，重启清零）


def budget() -> float:
    """每个任务搜按条收钱的帖子最多花多少美元（设置里改过的优先，其次 config.yaml 的 web.posts_budget_usd）。"""
    s = _saved()
    v = s["posts_budget_usd"] if "posts_budget_usd" in s else _cfg().get("posts_budget_usd", BUDGET)
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return BUDGET


def _task_key():
    """花费算在谁头上：后台任务按任务，小应用按对话。"""
    from .memory import WHO
    who = WHO.get() or {}
    return who.get("task_id") or (f"chat:{who['chat_id']}" if who.get("chat_id") else "other")


def platforms() -> list[str]:
    """配方里有的平台（给 search_posts 的说明用）。"""
    return list(dict.fromkeys(r.get("platform") for r in recipes("posts") if r.get("platform")))


def _platform(p: str) -> str:
    p = (p or "").strip().lower()
    alias = {"twitter": "x", "推特": "x", "小红书": "xiaohongshu", "xhs": "xiaohongshu", "红书": "xiaohongshu"}
    return alias.get(p, p)


async def search_posts(platform: str, query: str, days: int | None = None, count: int | None = None, only: dict | None = None,
                       browser: bool = True) -> str:
    """搜某个平台上的帖子：按顺序试这个平台配好了的几家。via: browser 的（小红书）没有接口，告诉模型怎么用电脑上的浏览器去搜。"""
    query = (query or "").strip()
    if not query:
        return i18n.t("没有给搜索词")
    plat = _platform(platform)
    allr = [r for r in recipes("posts") if r.get("platform") == plat]
    if not allr:
        return i18n.t("不认识这个平台：{name}。能搜的：{list}", name=platform, list="、".join(platforms()))
    rs = [only] if only else [r for r in order("posts") if r.get("platform") == plat]
    if not rs:
        why = "；".join(f"{_tr(r, 'title')}：{missing(r) or i18n.t('没打开')}" for r in allr)
        return i18n.t("搜不了 {name} 的帖子：{why}（设置 → 搜索和读网页）", name=PLATFORMS.get(plat, plat), why=why)
    count = max(1, min(int(count or POSTS), 50))
    notes, empty = [], 0
    async with _client() as c:
        for r in rs:
            if days and r.get("max_days") and days > int(r["max_days"]):
                notes.append(i18n.t("{name}：只能搜最近 {n} 天", name=_tr(r, "title"), n=r["max_days"]))
                continue
            if r.get("via") == "browser":
                if not browser:   # 这一轮没有电脑（设置里关了电脑，或者是小应用调的）
                    notes.append(i18n.t("{name}：要用电脑上的浏览器搜，这里没有电脑可用", name=_tr(r, "title")))
                    continue
                url = render(r.get("url") or "", {"query": quote(query)})
                return i18n.t("{name} 没有接口，用电脑上的浏览器搜：browser_open 打开 {url}，用 browser_read 读结果，点开笔记看正文和评论。"
                              "页面要登录或出现验证码时调用 ask_human，请用户在右栏的电脑画面里登录（只要登一次）。"
                              "别连续快速翻很多页：请求太多会被风控。", name=_tr(r, "title"), url=url)
            want = max(count, int(r.get("min_count") or 1))
            per, key = float(r.get("price_usd_item") or 0), _task_key()
            if per:   # 按条收钱的（X）：每个任务有预算，要的条数也不超过剩下的钱
                left = budget() - _spent.get(key, 0.0)
                afford = int(left / per + 1e-9)
                if afford < int(r.get("min_count") or 1):
                    notes.append(i18n.t("{name}：这个任务搜帖子的预算用完了（每个任务最多 {usd} 美元，设置 → 搜索和读网页 里能改）。"
                                        "用已经搜到的；只想知道在热议什么，用 fetch_url 读 getdaytrends.com 的热门话题榜，再用 web_search 查",
                                        name=_tr(r, "title"), usd=f"{budget():g}"))
                    continue
                want = min(want, afford)
            t0 = time.monotonic()
            try:
                items = await _search_one(c, r, query, days, want)
            except Failed as e:
                notes.append(f"{_tr(r, 'title')}：{e}")
                continue
            if items:
                _record(r, "posts", (time.monotonic() - t0) * 1000, len(items))
                if per:
                    if len(_spent) > 500:
                        _spent.clear()
                    _spent[key] = _spent.get(key, 0.0) + per * len(items)
            items = [e for e in items if not blocked_domain(e["url"])][:count]
            if items:
                return _format_posts(items) + _tail(notes, _tr(r, "title"))
            empty += 1
            notes.append(i18n.t("{name}：没有结果", name=_tr(r, "title")))
    return (i18n.t("无结果") if empty else i18n.t("搜索失败")) + _tail(notes)


# ---------- 设置 ----------
def _price(r: dict) -> float | None:
    """每千次（按条收钱的是每千条）多少钱（按 pricing.usd_rate 换算）；不要钱的是 0；要自己的密钥、配方没写单价的不知道（None）。"""
    from . import prices
    if usd := r.get("price_usd") or r.get("price_usd_item"):
        return round(float(usd) * 1000 * prices._rate(), 2)
    return None if any(f.get("secret") and not f.get("optional") for f in r.get("fields") or []) else 0


def public() -> dict:
    from . import prices
    out: dict = {"block": block_list(), "default_block": [str(x) for x in _cfg().get("block") or []],
                 "posts_budget_usd": budget(), "usd_rate": prices._rate()}
    for kind in KINDS:
        names = chosen(kind)
        rows = []
        for r in recipes(kind):
            why = missing(r)
            on = _on(kind, r, names) and (bool(names) or not why)
            rows.append({
                "name": r["name"], "title": _tr(r, "title"), "desc": _tr(r, "desc"), "link": r.get("link") or "",
                "platform": PLATFORMS.get(r.get("platform") or "", r.get("platform") or ""), "per_item": bool(r.get("price_usd_item")),
                "via": r.get("via") or "",
                "missing": why, "on": on, "auto": r.get("auto", True), "price": _price(r),
                "provider": _provider(r) or "",
                "fields": [{"key": f["key"], "label": _tr(f, "label") or f["key"], "secret": bool(f.get("secret")),
                            "optional": bool(f.get("optional")), "placeholder": f.get("placeholder") or "",
                            "set": bool(field_value(r, f)),
                            "value": "" if f.get("secret") else field_value(r, f)} for f in r.get("fields") or []]})
        if names:
            rank = {n: i for i, n in enumerate(names)}
            rows.sort(key=lambda x: rank.get(x["name"], len(rank)))
        out[kind] = {"auto": not names, "services": rows}
    return out


def save(data: dict) -> dict:
    """改顺序（search / fetch：名字列表，null 或 [] 是回到自动）、不用的网站（block）、填的值（fields：{名字: {项: 值}}）。"""
    from . import security
    s = _saved()
    for kind in KINDS:
        if kind not in data:
            continue
        v = data[kind]
        if not v:
            s.pop(kind, None)
            s.pop(f"{kind}_seen", None)
            continue
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(i18n.t("顺序要是名字的列表"))
        if bad := [x for x in v if not recipe(kind, x)]:
            raise ValueError(i18n.t("没有这个服务：{name}", name=bad[0]))
        s[kind] = list(dict.fromkeys(v))
        s[f"{kind}_seen"] = [r["name"] for r in recipes(kind)]
    if "posts_budget_usd" in data:
        v = data["posts_budget_usd"]
        if v is None:
            s.pop("posts_budget_usd", None)
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(i18n.t("预算要是数字"))
            if not 0 <= v <= 10:
                raise ValueError(i18n.t("预算在 0 到 10 美元之间"))
            s["posts_budget_usd"] = v
    if "block" in data:
        v = data["block"]
        if v is None:
            s.pop("block", None)
        else:
            if not isinstance(v, list):
                raise ValueError(i18n.t("不用的网站要是域名的列表"))
            ds = []
            for x in v:
                d = re.sub(r"^[a-z]+://", "", str(x).strip().lower()).split("/")[0].strip(".")
                if d.startswith("www."):
                    d = d[4:]
                if not _DOMAIN.match(d):
                    raise ValueError(i18n.t("不是域名：{text}", text=str(x)[:60]))
                ds.append(d)
            s["block"] = list(dict.fromkeys(ds))
    for name, vals in (data.get("fields") or {}).items():
        if security.demo():
            raise ValueError(i18n.t("演示服务器上不能改搜索服务的密钥和地址"))
        r = next((x for x in recipes() if x["name"] == name), None)
        fs = {f["key"]: f for x in recipes() if x["name"] == name for f in x.get("fields") or []}
        if not r or not isinstance(vals, dict):
            raise ValueError(i18n.t("没有这个服务：{name}", name=name))
        for k, val in vals.items():
            f = fs.get(k)
            if not f:
                raise ValueError(i18n.t("没有这一项：{name}", name=k))
            val = str(val or "").strip()
            if k == "url" and val and not re.match(r"^https?://[^\s/]+", val):
                raise ValueError(i18n.t("地址要以 http:// 或 https:// 开头"))
            if f.get("secret") and val:
                vault.put(_vault_name(name, k), val)
            elif f.get("secret"):
                vault.delete(_vault_name(name, k))
            else:
                box = s.setdefault("fields", {}).setdefault(name, {})
                if val:
                    box[k] = val
                else:
                    box.pop(k, None)
    _store(s)
    return public()


async def test(kind: str, name: str) -> dict:
    """真的调一次这一家（只这一家），给设置页的“测试”。"""
    r = recipe(kind, name)
    if not r:
        raise ValueError(i18n.t("没有这个服务：{name}", name=name))
    if why := missing(r):
        return {"ok": False, "error": why, "ms": 0}
    t0 = time.monotonic()
    if kind == "search":
        out = await search("DeepSeek", only=r)
        ok = out.startswith("- ")
    elif kind == "posts":
        out = await search_posts(r.get("platform") or "", "DeepSeek", days=3, only=r)
        ok = out.startswith("- ") or r.get("via") == "browser"
    elif r.get("hosts"):
        out = await fetch("https://www.reddit.com/r/LocalLLaMA/comments/1xburkx/", only=r)
        ok = not out.startswith(i18n.t("没读到"))
    else:
        out = await fetch("https://example.com/", only=r)
        ok = "Example Domain" in out
    ms = int((time.monotonic() - t0) * 1000)
    return {"ok": ok, "ms": ms, "error": "" if ok else out.strip()[:300], "sample": out[:300] if ok else ""}
