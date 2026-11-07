"""应用市场的目录。

每个应用一份 YAML 清单，文件名就是应用名：
- osa/catalog/apps/<名字>.yaml：随项目发布。想让应用上架，加一份清单提 PR 就行
- data/apps/<名字>.yaml：只在这台机器上用，和内置的同名时覆盖内置的。适合先在本机试

格式见 docs/apps.md。清单改了不用重启，下次打开应用页就会重新读。`python -m osa.catalog` 检查所有清单。

connect 是连接配置的模板：字符串里的 {字段名} 换成用户填的值。secret 字段先存进保险库，
配置里只留 {vault:名字}，记在 owned 里，删除应用时一起删掉。

en 是英文界面用的文案，写了的覆盖中文的，没写的照旧显示中文。界面语言能在运行中切换，
所以读清单时原样留着，到 public()、title() 这些给界面用的地方再按当前语言换（_en）。
"""
import inspect
import json
import logging
import re
from pathlib import Path
import yaml
from .. import i18n
from ..config import DATA, ROOT
from . import checks

log = logging.getLogger(__name__)

BUILTIN = Path(__file__).parent / "apps"
LOCAL = DATA / "apps"
# 和 mcp_hub.WORKSPACE 是同一个目录。不从那边导入：导入 mcp_hub 会打开数据库，检查清单用不着
WORKSPACE = DATA / "workspace"

# 分类按这个顺序显示。要加新分类，改这里
CATEGORIES = {"notes": i18n.N_("笔记与文档"), "mail": i18n.N_("邮件与日历"), "files": i18n.N_("文件"),
              "tasks": i18n.N_("任务与协作"), "design": i18n.N_("设计"), "dev": i18n.N_("开发"),
              "web": i18n.N_("搜索与网页"), "life": i18n.N_("生活")}
# 没有品牌图标时可以用的线条图标，图形在 web/src/appIcons.ts
GLYPHS = {"globe", "folder", "mail", "search", "train", "pin", "book", "doc", "feather", "plug",
          "chat", "hash", "package", "cloud", "palette", "table", "calendar", "check", "notebook"}

ID = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")       # 文件名也是应用名，规则和 mcp_hub.NAME 一样
KEYS = {"title", "category", "order", "by", "icon", "desc", "about", "can", "example", "note", "how",
        "steps", "link", "link_label", "copy", "fields", "check", "deny", "confirm", "connect", "en"}
# en 里能写的键：和中文的同名键一样，写了就在英文界面里替换掉。
# fields 写成「字段的 key: {label, placeholder, invalid}」，check 写成「检查名: {hint}」，copy 只换 label
EN_TEXT = ("title", "by", "desc", "about", "example", "note", "how", "link_label")
EN_LIST = ("can", "steps")
EN_KEYS = {*EN_TEXT, *EN_LIST, "copy", "fields", "check"}
EN_FIELD_KEYS = ("label", "placeholder", "invalid")
MENTION = re.compile(r"(?<![\w@.])@([a-z][a-z0-9_-]{0,23})\b")     # 和 skills._APP_REF 一样
FIELD_KEYS = {"key", "label", "placeholder", "default", "secret", "optional", "nospace", "pattern", "invalid"}
CONNECT_KEYS = {"type", "command", "args", "env", "url", "headers", "oauth"}
OAUTH_KEYS = {"client_id", "client_secret", "scope", "params"}
SLOT = re.compile(r"\{(\w+)\}")
REF = re.compile(r"^(env|vault):|\{(env|vault):")   # mcp_hub._resolve 认的引用写法
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
SVG_PATH = re.compile(r"^[MmZzLlHhVvCcSsQqTtAa0-9eE.,+\-\s]+$")

_cache: dict = {"sig": None, "items": {}, "errors": []}


# ---------- 读清单 ----------
def _files() -> dict[str, Path]:
    files = {}
    for d in (BUILTIN, LOCAL):
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                files[p.stem] = p       # 本机的同名清单覆盖内置的
    return files


def _show(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def load() -> tuple[dict[str, dict], list[str]]:
    """读所有清单，返回（应用名 -> 清单，有错的清单和原因）。文件都没变就用上次读的。"""
    files = _files()
    try:
        sig = tuple((str(p), p.stat().st_mtime_ns) for p in files.values())
    except OSError:                     # 读的时候正好有文件被删掉，下次再读
        sig = None
    if sig is None or sig != _cache["sig"]:
        items, errors = {}, []
        for name, p in files.items():
            try:
                items[name] = parse(name, yaml.safe_load(p.read_text("utf-8")))
            except yaml.MarkedYAMLError as e:
                mark = e.problem_mark or e.context_mark
                where = f"第 {mark.line + 1} 行" if mark else ""
                errors.append(f"{_show(p)}：{where} YAML 格式不对（{e.problem or e.context}）")
            except (OSError, yaml.YAMLError, ValueError) as e:
                errors.append(f"{_show(p)}：{e}")
        rank = list(CATEGORIES)
        items = dict(sorted(items.items(), key=lambda kv: (rank.index(kv[1]["category"]), kv[1]["order"], kv[0])))
        for e in errors:
            log.warning("应用清单有错，已跳过：%s", e)
        _cache.update(sig=sig, items=items, errors=errors)
    return _cache["items"], _cache["errors"]


def items() -> dict[str, dict]:
    return load()[0]


_builtin: dict[str, tuple] = {}


def builtin(name: str) -> dict | None:
    """随项目发布的清单（不看 data/apps 里本机加的、覆盖的）。服务器锁只认这里的启动命令（见 security.py）。"""
    if not ID.match(name or ""):
        return None
    p = BUILTIN / f"{name}.yaml"
    try:
        sig = p.stat().st_mtime_ns
    except OSError:
        return None
    if (hit := _builtin.get(name)) and hit[0] == sig:
        return hit[1]
    try:
        it = parse(name, yaml.safe_load(p.read_text("utf-8")))
    except (OSError, yaml.YAMLError, ValueError):
        it = None
    _builtin[name] = (sig, it)
    return it


def parse(name: str, m) -> dict:
    """检查一份清单并补上默认值。有问题时 raise ValueError，一次列出所有问题。"""
    if not isinstance(m, dict):
        raise ValueError("内容应该是一组「键: 值」")
    bad: list[str] = []
    if not ID.match(name):
        bad.append("文件名就是应用名：小写字母开头，只能有小写字母、数字、- 和 _，最长 24 个字符")
    bad += [f"不认识的键 {k}" for k in m if k not in KEYS]

    def text(v, where: str, need=False):
        if v is None:
            if need:
                bad.append(f"缺少 {where}")
        elif not isinstance(v, str) or not v.strip():
            bad.append(f"{where} 应该是一段文字")

    def texts(v, where: str):
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v)):
            bad.append(f"{where} 应该是一组文字")

    for k in ("title", "by", "desc"):
        text(m.get(k), k, need=True)
    for k in ("about", "example", "note", "how", "link", "link_label"):
        text(m.get(k), k)
    for k in ("can", "steps", "deny"):
        texts(m.get(k), k)
    if m.get("category") not in CATEGORIES:
        bad.append(f"category 应该是 {'、'.join(CATEGORIES)} 之一")
    if "order" in m and (type(m["order"]) is not int):
        bad.append("order 应该是一个整数")
    cf = m.get("confirm", "auto")
    if cf not in ("auto", "all", "none") and not (isinstance(cf, list) and cf and all(isinstance(x, str) for x in cf)):
        bad.append("confirm 应该是 auto、all、none，或者一组词（工具名里含这些词就先问）")
    _check_icon(m.get("icon"), bad)
    cp = m.get("copy")
    if cp is not None and not (isinstance(cp, dict) and set(cp) == {"label", "text"}
                               and all(isinstance(v, str) and v.strip() for v in cp.values())):
        bad.append("copy 要写 label 和 text 两项文字")
    fields = _check_fields(m.get("fields"), bad)
    keys = {f["key"] for f in fields}
    used = _check_connect(m.get("connect"), keys, bad)
    used |= _check_checks(m.get("check"), keys, bad)
    bad += [f"字段 {k} 没有用在 connect 或 check 里" for k in sorted(keys - used)]
    _check_en(m.get("en"), m, fields, bad, text, texts)

    o = m["connect"].get("oauth") if isinstance(m.get("connect"), dict) else None
    oauth = o is True or isinstance(o, dict)
    if bad:
        raise ValueError("；".join(bad))

    builtin = {"workspace": str(WORKSPACE)}
    return m | {
        "id": name, "order": m.get("order", 100), "confirm": m.get("confirm", "auto"),
        "kind": "oauth" if oauth else "token" if any(f.get("secret") for f in fields) else "none",
        "client": isinstance(o, dict) and "client_id" in o,     # 用户自己建的 OAuth 客户端：要告诉他回调地址
        "can": m.get("can") or [], "steps": m.get("steps") or [], "deny": m.get("deny") or [],
        "check": m.get("check") or {},
        "fields": [f | {"default": _fill(f["default"], builtin)} if "default" in f else f for f in fields],
    }


def _check_en(en, m: dict, fields: list[dict], bad: list, text, texts):
    """en：英文界面用的文案。只能换中文里已经有的文案，写了英文的中文改了时容易忘掉。"""
    if en is None:
        return
    if not isinstance(en, dict):
        bad.append("en 应该是一组「键: 值」（英文界面用的文案）")
        return
    bad += [f"en 里不认识的键 {k}（能写的：{'、'.join(sorted(EN_KEYS))}）" for k in en if k not in EN_KEYS]
    bad += [f"en.{k} 没有对应的中文 {k}" for k in en if k in EN_KEYS and k not in m]
    for k in EN_TEXT:
        text(en.get(k), f"en.{k}")
    for k in EN_LIST:
        texts(en.get(k), f"en.{k}")
    if isinstance(en.get("example"), str) and isinstance(m.get("example"), str):
        want, got = set(MENTION.findall(m["example"])), set(MENTION.findall(en["example"]))
        if got != want:
            bad.append(f"en.example 要和中文的 example 一样 @ 这些应用：{'、'.join('@' + x for x in sorted(want)) or '（没有）'}")
    cp = en.get("copy")
    if cp is not None and not (isinstance(cp, dict) and set(cp) == {"label"} and isinstance(cp["label"], str)
                               and cp["label"].strip()):
        bad.append("en.copy 只写 label 一项文字（要复制的内容中英文都一样）")
    ef = en.get("fields")
    if ef is not None:
        if not isinstance(ef, dict):
            bad.append("en.fields 应该写成「字段的 key: {label: …}」")
        else:
            have = {f["key"]: f for f in fields}
            for k, v in ef.items():
                if k not in have:
                    bad.append(f"en.fields 里的 {k} 不是 fields 里的字段")
                elif not isinstance(v, dict):
                    bad.append(f"en.fields.{k} 应该写成 label、placeholder、invalid 这样的一组键")
                else:
                    bad += [f"en.fields.{k} 里不认识的键 {x}（能写的：{'、'.join(EN_FIELD_KEYS)}）"
                            for x in v if x not in EN_FIELD_KEYS]
                    bad += [f"en.fields.{k}.{x} 没有对应的中文 {x}" for x in v if x in EN_FIELD_KEYS and x not in have[k]]
                    for x in EN_FIELD_KEYS:
                        text(v.get(x), f"en.fields.{k}.{x}")
    ec = en.get("check")
    if ec is not None:
        ck = m.get("check") if isinstance(m.get("check"), dict) else {}
        if not isinstance(ec, dict):
            bad.append("en.check 应该写成「检查名: {hint: …}」")
        else:
            for k, v in ec.items():
                if k not in ck:
                    bad.append(f"en.check 里的 {k} 不是 check 里的检查")
                elif not isinstance(v, dict) or set(v) != {"hint"}:
                    bad.append(f"en.check.{k} 只写 hint 一项")
                elif not isinstance(ck[k], dict) or "hint" not in ck[k]:
                    bad.append(f"en.check.{k}.hint 没有对应的中文 hint")
                else:
                    text(v["hint"], f"en.check.{k}.hint")


def _check_icon(ic, bad: list):
    if ic is None:
        return
    if not isinstance(ic, dict):
        bad.append("icon 应该写 color 和 svg（或 glyph）")
        return
    bad += [f"icon 里不认识的键 {k}" for k in ic if k not in ("color", "svg", "glyph")]
    if not COLOR.match(str(ic.get("color") or "")):
        bad.append("icon.color 应该是 #RRGGBB 格式的颜色")
    if "svg" in ic and "glyph" in ic:
        bad.append("icon 里 svg 和 glyph 只写一个")
    if "svg" in ic and not (isinstance(ic["svg"], str) and SVG_PATH.match(ic["svg"])):
        bad.append("icon.svg 应该是 24×24 的 SVG path（<path d=\"…\"> 里 d 的值）")
    if "glyph" in ic and ic["glyph"] not in GLYPHS:
        bad.append(f"icon.glyph 应该是 {'、'.join(sorted(GLYPHS))} 之一")


def _check_fields(fields, bad: list) -> list[dict]:
    if fields is None:
        return []
    if not isinstance(fields, list):
        bad.append("fields 应该是一组字段")
        return []
    out, seen = [], set()
    for i, f in enumerate(fields, 1):
        where = f"fields 第 {i} 项"
        if not isinstance(f, dict):
            bad.append(f"{where} 应该是一组「键: 值」")
            continue
        bad += [f"{where}：不认识的键 {k}" for k in f if k not in FIELD_KEYS]
        k = f.get("key")
        if not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", k):
            bad.append(f"{where}：key 只能用小写字母、数字和 _，字母开头")
            continue
        if k in seen:
            bad.append(f"{where}：key {k} 重复了")
        seen.add(k)
        if not isinstance(f.get("label"), str) or not f["label"].strip():
            bad.append(f"{where}：缺少 label（给用户看的名字）")
        for b in ("secret", "optional", "nospace"):
            if b in f and not isinstance(f[b], bool):
                bad.append(f"{where}：{b} 应该是 true 或 false")
        for s in ("placeholder", "default", "pattern", "invalid"):
            if s in f and not isinstance(f[s], str):
                bad.append(f"{where}：{s} 应该是文字（数字请加引号）")
        if isinstance(f.get("pattern"), str):
            try:
                re.compile(f["pattern"])
            except re.error as e:
                bad.append(f"{where}：pattern 不是有效的正则（{e}）")
        out.append(f)
    return out


def _slots(v) -> set[str]:
    return set(SLOT.findall(json.dumps(v, ensure_ascii=False)))


def _check_connect(c, keys: set, bad: list) -> set[str]:
    if not isinstance(c, dict):
        bad.append("缺少 connect（怎么连这个应用）")
        return set()
    bad += [f"connect 里不认识的键 {k}" for k in c if k not in CONNECT_KEYS]

    def strmap(k):
        v = c.get(k)
        if v is not None and (not isinstance(v, dict) or not all(isinstance(x, str) for x in v.values())):
            bad.append(f"connect.{k} 的值都要是文字（数字请加引号，比如 '993'）")

    t = c.get("type")
    if t == "stdio":
        if not isinstance(c.get("command"), str) or not c["command"].strip():
            bad.append("本机运行的应用（type: stdio）要写 command")
        if "args" in c and (not isinstance(c["args"], list) or not all(isinstance(x, str) for x in c["args"])):
            bad.append("connect.args 应该是一组文字（数字请加引号）")
        strmap("env")
        bad += [f"本机运行的应用不能写 {k}" for k in ("url", "headers", "oauth") if k in c]
    elif t in ("http", "sse"):
        # 整个地址也可以让用户填（飞书、企业微信的地址里带着授权）：写成 '{字段}'，字段里用 pattern 检查
        if not isinstance(c.get("url"), str) or not re.match(r"^(https?://|\{\w+\}$)", c["url"]):
            bad.append(f"远程应用（type: {t}）要写 url，以 https:// 开头")
        strmap("headers")
        _check_oauth(c.get("oauth"), bad)
        bad += [f"远程应用不能写 {k}" for k in ("command", "args", "env") if k in c]
    else:
        bad.append("connect.type 应该是 stdio（在本机运行）、http（远程地址）或 sse（只有旧版 SSE 地址的远程应用）")
    used = _slots(c)
    bad += [f"connect 里的 {{{s}}} 不是 fields 里的字段" for s in sorted(used - keys)]
    return used


def _check_oauth(o, bad: list):
    """oauth: true 用动态注册；写成一组键时可以带自己建的客户端（client_id、client_secret）、
    要申请的权限（scope，不写就按对方公布的全要）和授权页的额外参数（params）。"""
    if o is None or isinstance(o, bool):
        return
    if not isinstance(o, dict):
        bad.append("connect.oauth 应该是 true，或者写成 client_id、scope 这样的一组键")
        return
    bad += [f"connect.oauth 里不认识的键 {k}" for k in o if k not in OAUTH_KEYS]
    for k in ("client_id", "client_secret", "scope"):
        if k in o and (not isinstance(o[k], str) or not o[k].strip()):
            bad.append(f"connect.oauth.{k} 应该是一段文字")
    if "client_secret" in o and "client_id" not in o:
        bad.append("connect.oauth 写了 client_secret 就要写 client_id")
    p = o.get("params")
    if p is not None and (not isinstance(p, dict) or not all(isinstance(x, str) for x in p.values())):
        bad.append("connect.oauth.params 的值都要是文字")


def _check_checks(ck, keys: set, bad: list) -> set[str]:
    if ck is None:
        return set()
    if not isinstance(ck, dict):
        bad.append("check 应该写成「检查名: 参数」")
        return set()
    used = set()
    for name, params in ck.items():
        fn = checks.CHECKS.get(name)
        if not fn:
            bad.append(f"没有叫 {name} 的检查，现有的：{'、'.join(checks.CHECKS)}")
            continue
        if not isinstance(params, dict):
            bad.append(f"check.{name} 应该写成一组参数")
            continue
        ps = inspect.signature(fn).parameters
        bad += [f"check.{name} 缺少 {p}" for p, v in ps.items() if v.default is v.empty and p not in params]
        bad += [f"check.{name} 没有参数 {p}" for p in params if p not in ps]
        used |= _slots(params)
    bad += [f"check 里的 {{{s}}} 不是 fields 里的字段" for s in sorted(used - keys)]
    return used


# ---------- 给前端和其他模块用 ----------
def _en(it: dict) -> dict:
    """英文界面时，用清单 en 里写了的英文换掉中文；没写的、中文界面都原样返回。"""
    en = it.get("en")
    if not en or i18n.lang() != "en":
        return it
    o = it | {k: en[k] for k in (*EN_TEXT, *EN_LIST) if k in en}
    if "copy" in en:
        o["copy"] = it["copy"] | en["copy"]
    if "fields" in en:
        o["fields"] = [f | en["fields"].get(f["key"], {}) for f in it["fields"]]
    if "check" in en:
        o["check"] = {n: p | en["check"].get(n, {}) for n, p in it["check"].items()}
    return o


def public() -> list[dict]:
    out = []
    for it in map(_en, items().values()):
        o = {k: v for k, v in it.items() if k not in ("connect", "check", "order", "en")}
        o["fields"] = [{k: v for k, v in f.items() if k not in ("pattern", "nospace")} for f in it["fields"]]
        o.setdefault("example", "")
        o.setdefault("about", "")
        o["remote"] = it["connect"]["type"] != "stdio"
        out.append(o)
    return out


def categories() -> list[list[str]]:
    """有应用的分类，按显示顺序：[[键, 名字], …]。"""
    used = {i["category"] for i in items().values()}
    return [[k, i18n.t(v)] for k, v in CATEGORIES.items() if k in used]


def title(name: str) -> str:
    from .. import mcp_hub
    a = mcp_hub.get(name) or {}
    i = items().get(a.get("catalog") or name)
    return _en(i)["title"] if i else "@" + name


def _fill(v, subst: dict):
    if isinstance(v, str):
        return SLOT.sub(lambda m: subst.get(m[1], m[0]), v)
    if isinstance(v, list):
        return [_fill(x, subst) for x in v]
    if isinstance(v, dict):
        return {k: _fill(x, subst) for k, x in v.items()}
    return v


def _values(it: dict, values: dict) -> dict:
    """检查用户填的内容，返回整理好的值（去掉首尾空格；nospace 的字段去掉所有空格）。"""
    vals = {}
    for f in it.get("fields", []):
        v = str(values.get(f["key"]) or f.get("default") or "").strip()
        if f.get("nospace"):
            v = re.sub(r"\s+", "", v)
        if not v and not f.get("optional"):
            raise ValueError(i18n.t("请填写{label}", label=f["label"]))
        if v and f.get("pattern") and not re.search(f["pattern"], v):
            raise ValueError(f.get("invalid") or i18n.t("{label}的格式不对", label=f["label"]))
        if REF.search(v):   # 值原样填进模板，连接时才把 {env:名字} 这类引用换成明文，不能让用户借此取服务的密钥
            raise ValueError(i18n.t("{label}里不能写 env: 或 vault: 引用", label=f["label"]))
        vals[f["key"]] = v
    return vals


async def check(item_id: str, values: dict):
    """添加前先按清单里的 check 试一下填的东西对不对，错了直接告诉用户，不用等应用连接失败。"""
    it = _en(items().get(item_id) or {})
    if not it.get("check"):
        return
    vals = _values(it, values)
    for name, params in it["check"].items():
        await checks.CHECKS[name](**_fill(params, vals))


def build(item_id: str, values: dict) -> dict:
    """按清单里的模板生成应用配置。用户填的密钥存进保险库。"""
    from .. import vault
    it = items().get(item_id)
    if not it:
        raise ValueError(i18n.t("应用市场里没有这个应用"))
    vals = _values(_en(it), values)
    subst, owned = {}, []
    for f in it["fields"]:
        v = vals[f["key"]]
        if f.get("secret") and v:
            ref = f"{item_id}_{f['key']}".upper().replace("-", "_")
            vault.put(ref, v)
            owned.append(ref)
            v = "{vault:" + ref + "}"
        subst[f["key"]] = v
    cfg = _fill(it["connect"], subst)
    return cfg | {"catalog": item_id, "deny": it["deny"], "confirm": it["confirm"], "owned": owned}
