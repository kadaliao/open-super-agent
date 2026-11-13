"""画图、生成视频：按“配方”调各家的接口。代码里不写模型名和参数，都在配方里（osa/generation.yaml，config.yaml 的 generation 可以加、改）。

配方（一条管一家的一类模型）：
- 认模型：kind（image / video）、host（接口地址里的词，正则；也可以写服务的名字）、model（模型名正则，不分大小写）。按顺序找，第一条对上的算数。
- 能收什么：images（参考图最多几张）、frames（首帧、尾帧最多几张，视频用）、videos（参考视频最多几段）、total（加起来最多几个）、
  need（至少要几个，比如 {images: 1} 是只能改图、{frames: 1} 是只能图生视频，refs 是参考图和参考视频加起来）、seconds（能选的时长：列表或 "2-15"）、
  ratios / sizes（能选的画面比例；sizes 把比例换成接口要的尺寸）、image_url（参考图怎么给：data 是 base64，url 是临时公网地址）。
- 怎么调：request（method、path、headers、json 或 form + files），带参考图时换成 edit（画图）。path 可以是相对接口地址的，
  也可以写 ${origin}/... 用接口地址的协议和域名。异步的（视频）写 task（提交后从回包哪里取任务 id）和 poll（怎么查、什么状态算好、算失败、错误在哪）。
- 结果在哪：result，回包里的路径（a.b.0.c），可以写好几个，第一个有值的算数；是网址就下载，否则当 base64。
- 模板：值正好是 $名字 的换成变量（没有这个变量的整项去掉），字符串里的 ${名字} 换成文字；列表里写 {$each: media, as: {...}}
  按每个参考素材展开一项（素材里有 url、type（image / video）、role（按 roles 换成接口的叫法））。
  变量：model、prompt、seconds、ratio、size、id（任务 id）、image1…imageN、images（参考图列表）、media（全部素材）、origin。

参考视频大多只收公网网址：用 public_link() 给一个限时的签名地址（/pub/media/…），服务器的网页地址要能从外面打开。
"""
import base64
import copy
import hashlib
import hmac
import ipaddress
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import httpx
import yaml
from . import i18n, prices, providers
from .config import CFG

MAX_IMAGE = 20 << 20          # 生图服务给回来的原图
MAX_VIDEO = 200 << 20         # 生成的视频
_TRANSPORT = None             # 测试时换成假的
_DEFAULTS = yaml.safe_load((Path(__file__).parent / "generation.yaml").read_text(encoding="utf-8"))


def recipes() -> list[dict]:
    """config.yaml 的 generation.recipes 排在默认的前面；同名的默认配方不要了。"""
    mine = ((CFG.get("generation") or {}).get("recipes")) or []
    names = {r.get("name") for r in mine}
    return [*mine, *(r for r in _DEFAULTS["recipes"] if r.get("name") not in names)]


def prefer(kind: str) -> list[str]:
    """“自动”时按这个顺序挑：模型名里有这个词就算。"""
    return ((CFG.get("generation") or {}).get("prefer") or {}).get(kind) or _DEFAULTS["prefer"].get(kind) or []


def rank(kind: str, model: str) -> int:
    x = model.lower()
    order = prefer(kind)
    return next((i for i, k in enumerate(order) if k.lower() in x), len(order))


def _base(provider: str) -> str:
    return ((CFG["providers"].get(provider) or {}).get("base_url") or "").rstrip("/")


def recipe(kind: str, provider: str, model: str) -> dict | None:
    """这个服务的这个模型按哪条配方调；没有对上的返回 None（不是这一类模型）。"""
    base = _base(provider).lower()
    for r in recipes():
        if r.get("kind") != kind:
            continue
        host = r.get("host")
        if host and host != provider and not re.search(host, base, re.I):
            continue
        if re.search(r.get("model") or "", model, re.I):
            return None if r.get("skip") else r
    return None


def price(kind: str, provider: str, model: str) -> float | None:
    """每张图 / 每段视频多少钱（config.yaml 的 pricing.images / pricing.videos），不知道是 None。"""
    return prices.image_price(provider, model) if kind == "image" else prices.video_price(provider, model)


async def catalog(kind: str) -> list[dict]:
    """已经配好的模型服务里这一类的模型（服务列出来的，加上配方 offer 的），带价格和能收什么。"""
    out = []
    for name in CFG["providers"]:
        if not providers.usable(name):
            continue
        try:
            listed = (await providers.models(name)).get("models") or []
        except ValueError:
            continue
        title = providers.describe(name)["title"]
        for m in dict.fromkeys([*listed, *offers(kind, name)]):
            if not recipe(kind, name, m):
                continue
            c = caps(kind, name, m)
            out.append({"provider": name, "title": title, "model": m, "price": price(kind, name, m),
                        "refs": c.images, "needs_ref": c.needs_ref, "frames": c.frames, "videos": c.videos,
                        "seconds": list(c.seconds), "ratios": list(c.ratios)})
    return out


def offers(kind: str, provider: str) -> list[str]:
    """服务的模型列表里不一定有的（智谱、百炼的视频模型），配方里 offer 写了就也列出来。"""
    base = _base(provider).lower()
    out = []
    for r in recipes():
        if r.get("kind") == kind and r.get("offer") and r.get("host") and (r["host"] == provider or re.search(r["host"], base, re.I)):
            out += [m for m in r["offer"] if m not in out]
    return out


@dataclass(frozen=True)
class Caps:
    images: int = 0       # 参考图最多几张
    frames: int = 0       # 首帧、尾帧最多几张
    videos: int = 0       # 参考视频最多几段
    total: int = 0        # 加起来最多几个，0 是不另外限
    need: dict = field(default_factory=dict)
    seconds: tuple = ()
    ratios: tuple = ()

    @property
    def needs_ref(self) -> bool:
        return bool(self.need)


def _seconds(v) -> tuple:
    if isinstance(v, str) and re.fullmatch(r"\d+-\d+", v):
        a, b = map(int, v.split("-"))
        return tuple(range(a, b + 1))
    return tuple(int(x) for x in (v if isinstance(v, list) else [v] if v else []))


def caps(kind: str, provider: str, model: str) -> Caps:
    r = recipe(kind, provider, model) or {}
    return Caps(int(r.get("images") or 0), int(r.get("frames") or 0), int(r.get("videos") or 0), int(r.get("total") or 0),
                dict(r.get("need") or {}), _seconds(r.get("seconds")), tuple(r.get("ratios") or (r.get("sizes") or {}).keys()))


@dataclass(frozen=True)
class Ref:
    """一个参考素材：data 是内容（参考图），url 是能从外面打开的地址（参考视频、要网址的接口）。"""
    data: bytes = b""
    mime: str = ""
    name: str = ""
    url: str = ""
    role: str = ""   # frame（首帧、尾帧）、image（参考图）、video（参考视频）
    w: int = 0
    h: int = 0

    def data_url(self) -> str:
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode()

    @property
    def type(self) -> str:
        return "video" if self.role == "video" else "image"


def check(c: Caps, refs: list[Ref], what: str) -> str:
    """参考素材合不合这个模型：合适返回空，不合适返回原因。"""
    n = {k: sum(1 for x in refs if x.role == k[:-1]) for k in ("frames", "images", "videos")}
    labels = {"frames": i18n.t("首尾帧"), "images": i18n.t("参考图"), "videos": i18n.t("参考视频"), "refs": i18n.t("参考图或参考视频")}
    for k in ("frames", "images", "videos"):
        most = getattr(c, k)
        if n[k] > most:
            return i18n.t("{model} 不收{what}", model=what, what=labels[k]) if not most else \
                i18n.t("{model} 最多收 {n} 个{what}", model=what, what=labels[k], n=most)
    if c.total and sum(n.values()) > c.total:
        return i18n.t("{model} 的参考素材加起来最多 {n} 个", model=what, n=c.total)
    if n["frames"] and (n["images"] or n["videos"]):
        return i18n.t("首尾帧和参考素材不能一起用")
    for k, least in c.need.items():
        if (n["images"] + n["videos"] if k == "refs" else n.get(k, 0)) < int(least):
            return i18n.t("{model} 只能照着原图生成，要给它{what}", model=what, what=labels.get(k, k))
    return ""


# ---------- 模板 ----------
_VAR = re.compile(r"\$\{(\w+)\}")


def render(obj, v: dict):
    """把模板里的 $名字、${名字} 换成变量；值为 None 的键、列表项去掉。"""
    if isinstance(obj, str):
        if re.fullmatch(r"\$\w+", obj):
            return v.get(obj[1:])
        return _VAR.sub(lambda m: "" if v.get(m.group(1)) is None else str(v.get(m.group(1))), obj)
    if isinstance(obj, dict):
        out = {}
        for k, x in obj.items():
            val = render(x, v)
            if val is not None:
                out[render(k, v) if isinstance(k, str) else k] = val
        return out
    if isinstance(obj, list):
        out = []
        for x in obj:
            if isinstance(x, dict) and "$each" in x:
                out += [y for item in (v.get(x["$each"]) or []) if (y := render(x.get("as"), v | item)) is not None]
            elif (y := render(x, v)) is not None:
                out.append(y)
        return out
    return obj


def pick(d, path: str):
    """回包里 a.b.0.c 这样的路径，取不到返回 None。"""
    for k in path.split("."):
        if isinstance(d, list) and k.isdigit() and int(k) < len(d):
            d = d[int(k)]
        elif isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d


def _first(d, paths) -> object:
    for p in paths if isinstance(paths, list) else [paths]:
        if (x := pick(d, p)) not in (None, ""):
            return x
    return None


def _nearest(want, allowed: tuple, default=None):
    if not allowed:
        return None
    if want in allowed:
        return want
    if isinstance(want, int) and all(isinstance(a, int) for a in allowed):
        return min(allowed, key=lambda a: (abs(a - want), a))
    return default if default in allowed else allowed[0]


def _like(frame: Ref, allowed: tuple):
    """没说比例、给了首帧时：能自适应的用自适应，否则挑和首帧最接近的比例。"""
    if "adaptive" in allowed:
        return "adaptive"
    nums = [a for a in allowed if re.fullmatch(r"\d+:\d+", str(a))]
    if not (frame.w and frame.h and nums):
        return None
    want = frame.w / frame.h
    return min(nums, key=lambda a: abs(int(a.split(":")[0]) / int(a.split(":")[1]) - want))


def variables(r: dict, provider: str, model: str, prompt: str, refs: list[Ref], seconds=None, ratio=None) -> dict:
    p = urlsplit(_base(provider))
    sizes = r.get("sizes") or {}
    allowed = tuple(r.get("ratios") or sizes.keys())
    frames = [x for x in refs if x.role == "frame"]
    if not ratio and frames:
        ratio = _like(frames[0], allowed)
    ratio = _nearest(ratio, allowed, r.get("ratio"))
    secs = _seconds(r.get("seconds"))
    roles = r.get("roles") or {}
    as_url = r.get("image_url") == "url"
    imgs = [x for x in refs if x.type == "image"]
    v = {"model": model, "prompt": prompt, "origin": f"{p.scheme}://{p.netloc}", "ratio": ratio,
         "size": sizes.get(ratio) if ratio else r.get("size"),
         "seconds": _nearest(int(seconds) if seconds else int(r.get("default_seconds") or 5), secs) if secs else None,
         "images": [x.url if as_url else x.data_url() for x in imgs] or None}
    for i, x in enumerate(imgs):
        v[f"image{i + 1}"] = v["images"][i]
    media = []
    for x in refs:
        role = x.role
        if role == "frame":
            role = "first" if x is frames[0] else "last"
        url = x.url if (x.type == "video" or as_url) else x.data_url()
        media.append({"url": url, "type": x.type, "role": roles.get(role, role)})
    v["media"] = media or None
    return v


# ---------- 调接口 ----------
def _error(r: httpx.Response, where: str = "") -> str:
    try:
        d = r.json()
    except ValueError:
        d = None
    msg = None
    if isinstance(d, dict):
        e = d.get("error")
        msg = (e.get("message") if isinstance(e, dict) else e) or d.get("message") or d.get("detail") or \
            (pick(d, where) if where else None) or pick(d, "output.message")
    elif r.text:
        msg = r.text[:200]
    return f"HTTP {r.status_code}" + (f"：{msg}" if msg else "")


async def _call(c: httpx.AsyncClient, provider: str, req: dict, v: dict, refs: list[Ref]) -> dict:
    base = _base(provider)
    key = providers.api_key(provider)
    path = render(req.get("path") or "", v)
    url = path if path.startswith(("http://", "https://")) else base + path
    headers = ({"Authorization": f"Bearer {key}"} if key else {}) | render(req.get("headers") or {}, v)
    method = (req.get("method") or "POST").upper()
    kw: dict = {}
    if "form" in req:
        kw["data"] = {k: str(x) for k, x in render(req["form"], v).items()}
        kw["files"] = [(fname, (f"image{i + 1}.{x.mime.split('/')[1].replace('jpeg', 'jpg')}", x.data, x.mime))
                       for fname, which in (req.get("files") or {}).items() if which == "images"
                       for i, x in enumerate(y for y in refs if y.type == "image")]
    elif "json" in req:
        kw["json"] = render(req["json"], v)
    try:
        r = await c.request(method, url, headers=headers, **kw)
    except httpx.TimeoutException:
        raise ValueError(i18n.t("连接超时：{url}", url=base))
    except httpx.HTTPError:
        raise ValueError(i18n.t("连不上 {url}", url=base))
    if r.status_code >= 400:
        raise ValueError(_error(r))
    try:
        return r.json()
    except ValueError:
        raise ValueError(i18n.t("返回的不是 JSON：{text}", text=r.text[:120]))


async def _fetch(c: httpx.AsyncClient, found, limit: int) -> bytes:
    if isinstance(found, str) and found.startswith(("http://", "https://")):
        got = await c.get(found)   # 结果地址一般一小时到一天后失效，当场取回来
        if got.status_code >= 400:
            raise ValueError(f"HTTP {got.status_code}")
        b = got.content
    else:
        try:
            b = base64.b64decode(str(found or ""), validate=False)
        except ValueError:
            b = b""
    if not b or len(b) > limit:
        raise ValueError(i18n.t("服务没有返回结果"))
    return b


def _client(timeout: float = 180) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=_TRANSPORT)


def _usable(provider: str, model: str):
    if provider not in CFG["providers"] or not providers.usable(provider):
        raise ValueError(i18n.t("没有这个模型服务：{name}", name=provider))


async def draw(prompt: str, provider: str, model: str, refs: list[Ref] | None = None) -> tuple[bytes, str]:
    """画一张，返回 (原图, mime)。参考图不合这个模型（太多、只能改图却没给）时报错，不去调接口。"""
    prompt = (prompt or "").strip()[:1000]
    refs = [x if x.role else Ref(x.data, x.mime, x.name, x.url, "image") for x in refs or []]
    if not prompt:
        raise ValueError(i18n.t("写一句想要画什么"))
    _usable(provider, model)
    r = recipe("image", provider, model)
    if not r:
        raise ValueError(i18n.t("{model} 不是画图的模型", model=model))
    if why := check(caps("image", provider, model), refs, model):
        raise ValueError(why)
    req = (r.get("edit") if refs else None) or r["request"]
    async with _client() as c:
        try:
            d = await _call(c, provider, req, variables(r, provider, model, prompt, refs), refs)
            b = await _fetch(c, _first(d, req.get("result") or r.get("result") or []), MAX_IMAGE)
        except ValueError as e:
            raise ValueError(i18n.t("画图失败：{e}", e=e))
    from .media import sniff
    mime = sniff(b)
    if not mime or not mime.startswith("image/"):
        raise ValueError(i18n.t("画图服务没有返回图片"))
    record("image", provider, model, prices.image_price(provider, model))
    return b, mime


async def submit(provider: str, model: str, prompt: str, refs: list[Ref], seconds=None, ratio=None) -> dict:
    """提交一个视频任务，返回 job（之后 poll 用，存在消息里，服务重启后接着查）。"""
    _usable(provider, model)
    r = recipe("video", provider, model)
    if not r:
        raise ValueError(i18n.t("{model} 不是生成视频的模型", model=model))
    if why := check(caps("video", provider, model), refs, model):
        raise ValueError(why)
    v = variables(r, provider, model, prompt, refs, seconds, ratio)
    async with _client(60) as c:
        d = await _call(c, provider, r["request"], v, refs)
    tid = pick(d, r.get("task") or "id")
    if not tid:
        raise ValueError(i18n.t("服务没有返回任务编号"))
    return {"provider": provider, "model": model, "id": str(tid), "seconds": v.get("seconds"), "ratio": v.get("ratio")}


async def poll(job: dict) -> tuple[str, bytes | str | None]:
    """查一次视频任务：("running", None)、("done", 视频内容)、("failed", 原因)。"""
    r = recipe("video", job["provider"], job["model"])
    if not r:
        return "failed", i18n.t("找不到 {model} 的配方", model=job["model"])
    p = r["poll"]
    async with _client(60) as c:
        try:
            d = await _call(c, job["provider"], p["request"], {"id": job["id"], "model": job["model"],
                                                                  "origin": variables(r, job["provider"], job["model"], "", [])["origin"]}, [])
        except ValueError as e:
            return "running", str(e)   # 查的时候出错（网络抖动）：下次再查，连续出错由调用方决定什么时候放弃
        status = str(pick(d, p.get("status") or "status") or "")
        if status in [str(x) for x in p.get("failed") or []]:
            return "failed", str(_first(d, p.get("error") or []) or status)
        if status not in [str(x) for x in p.get("done") or []]:
            return "running", None
        try:
            return "done", await _fetch(c, _first(d, r.get("result") or []), MAX_VIDEO)
        except ValueError as e:
            return "failed", str(e)


def record(kind: str, provider: str, model: str, cost: float | None):
    """画了一张、生成了一段：费用统计里记一行（0 token，费用按价格，不知道的按 0）。"""
    from . import memory
    memory.add_usage(kind, model, provider=provider, cost=cost or 0.0)


# ---------- 临时公网地址 ----------
LINK_S = 6 * 3600


def _secret() -> bytes:
    """签临时地址用的密钥：第一次用时随机生成，存在 settings 表里（不放保险库，免得出现在用户的密钥列表里）。"""
    import secrets
    from .memory import conn
    r = conn.execute("select value from settings where key='media_link_secret'").fetchone()
    if not r:
        conn.execute("insert or ignore into settings values('media_link_secret', ?)", (secrets.token_hex(32),))
        conn.commit()
        r = conn.execute("select value from settings where key='media_link_secret'").fetchone()
    return bytes.fromhex(r[0])


def sign(mid: str, exp: int) -> str:
    return hmac.new(_secret(), f"{mid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(mid: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(sign(mid, exp), sig or "")


def public_base() -> str:
    """网页的公网地址；没填、或者是本机和内网地址（外面打不开）时返回空。"""
    from . import channels
    u = channels.public_url()
    host = urlsplit(u).hostname or ""
    if not host or host == "localhost" or host.endswith(".local"):
        return ""
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return ""
    except ValueError:
        pass
    return u.rstrip("/")


def public_link(mid: str) -> str:
    """给视频服务用的临时地址（6 小时内有效），不用登录。服务器的网页地址外面打不开时返回空。"""
    base = public_base()
    if not base:
        return ""
    exp = int(time.time()) + LINK_S
    return f"{base}/pub/media/{mid}?exp={exp}&sig={sign(mid, exp)}"


def defaults_copy() -> dict:
    return copy.deepcopy(_DEFAULTS)
