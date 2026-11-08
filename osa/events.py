"""事件：某件事发生时（收到 webhook、订阅源有新文章、网页变了、收到邮件、价格到了线……），Bot 在后台处理一次。

和定时任务的区别只在“什么时候跑”：定时任务按时间，事件按外面发生的事。所以两者存在同一张表（routines），
运行、运行记录、“无新内容不打扰”都共用 routines.py；这里管事件特有的部分：
- trigger 写成 {"kind": "event", "source": 来源, "params": {...}, "every": 分钟, "include", "exclude", "llm_filter", "key"}。
- 来源是 registry.Source：内置的在 osa/sources/，插件也能用 ctx.source 加。三种：
  push（POST /hooks/<key>，key 是每个事件自己的随机密钥，地址本身就是凭据）、poll（osa 隔一段时间去看）、
  internal（osa 自己的事件，比如后台任务失败了）。
- 来的事件先逐条过关键词（include / exclude），剩下的合成一次运行，再过 LLM 过滤（llm_filter），最后派给 Bot。
  上一次还没跑完时新事件先排队（最多 MAX_PENDING 条），跑完接着处理，不丢。
- poll 来源的状态（看到哪了）存在 event_state 表，第一次只记下现状、不报。
- 参数里 secret 类型的值（邮箱授权码之类）存进保险库，参数里只留 "vault:名字"。

旧版的 webhook 定时任务（kind=webhook，地址 /hooks/<名字>）在启动时迁成 source=webhook 的事件，旧地址照旧能用（同样要 OSA_WEBHOOK_TOKEN）。
"""
import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from . import i18n, registry
from .memory import conn
from .registry import POLL, PUSH, Source

log = logging.getLogger(__name__)

conn.execute("""create table if not exists event_state(
  routine_id integer primary key, state text, checked real, error text default '', fails integer default 0,
  sample text default '')""")
conn.commit()

MAX_EVENTS = 30          # 一次运行最多带几条事件
MAX_TEXT = 20_000        # 一次运行给 Bot 的事件原文最多多少字
MAX_PENDING = 50
MAX_BODY = 1_000_000
POLL_TIMEOUT = 60
POLL_PARALLEL = 4
CATEGORIES = (i18n.N_("开发"), i18n.N_("资讯"), i18n.N_("生活"), i18n.N_("金融"), i18n.N_("系统"), i18n.N_("通用"))
PENDING: dict[int, list[dict]] = {}     # routine_id -> 排队的事件
TASK_ROUTINE: dict[int, int] = {}       # 任务 id -> 派它的事件（internal 来源不让一个事件被自己派的任务再触发）
_polling: set[int] = set()
_sem: asyncio.Semaphore | None = None


# ---------- 来源 ----------
def get_source(sid: str) -> Source | None:
    return registry.SOURCES.get(sid)


def unavailable(src: Source) -> str:
    try:
        return src.available() if src.available else ""
    except Exception as e:
        return str(e)


def _field_public(f: dict) -> dict:
    out = {k: v for k, v in f.items() if k not in ("label", "help", "placeholder", "options")}
    out["label"] = i18n.t(f.get("label") or f["key"])
    for k in ("help", "placeholder"):
        if f.get(k):
            out[k] = i18n.t(f[k])
    if f.get("options"):
        out["options"] = [{"value": o["value"], "label": i18n.t(o.get("label") or o["value"])} for o in f["options"]]
    return out


def _scenario_public(src: Source, sc: dict) -> dict:
    out = {"source": src.id, "title": i18n.t(sc["title"]), "description": i18n.t(sc.get("description") or ""),
           "name": i18n.t(sc.get("name") or sc["title"]), "params": sc.get("params") or {},
           "prompt": i18n.t(sc.get("prompt") or "")}
    for k in ("include", "exclude"):
        out[k] = sc.get(k) or []
    out["llm_filter"] = i18n.t(sc["llm_filter"]) if sc.get("llm_filter") else ""
    if sc.get("every"):
        out["every"] = sc["every"]
    return out


def catalog() -> list[dict]:
    """网页上“新建事件”用：所有来源（按分类）和它们的常见用法。"""
    out = []
    for src in registry.SOURCES.values():
        out.append({"id": src.id, "title": i18n.t(src.title), "description": i18n.t(src.description), "mode": src.mode,
                    "category": i18n.t(src.category), "icon": src.icon, "fields": [_field_public(f) for f in src.fields],
                    "every": src.every, "min_every": src.min_every, "setup": i18n.t(src.setup) if src.setup else "",
                    "plugin": src.owner.removeprefix("plugin:") if src.owner.startswith("plugin:") else "",
                    "unavailable": unavailable(src), "scenarios": [_scenario_public(src, sc) for sc in src.scenarios]})
    order = {c: i for i, c in enumerate(CATEGORIES)}
    out.sort(key=lambda s: (order.get(next((c for c in CATEGORIES if i18n.t(c) == s["category"]), ""), 99), bool(s["plugin"])))
    return out


def brief_sources() -> str:
    """给模型看的来源清单（create_event 填错 source 时返回）。"""
    lines = []
    for src in registry.SOURCES.values():
        if unavailable(src):
            continue
        fs = "、".join(f"{f['key']}（{f.get('label', '')}{'，必填' if f.get('required') else ''}"
                      f"{'，可选：' + '/'.join(o['value'] for o in f['options']) if f.get('options') else ''}）" for f in src.fields)
        lines.append(f"- {src.id}：{src.title}。{src.description}" + (f" 参数：{fs}" if fs else ""))
    return "\n".join(lines)


# ---------- 参数 ----------
def _split(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[\n,，]+", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def _secret(v, old):
    """secret 参数：留空就用原来的；"vault:名字" 原样；别的存进保险库换成引用。"""
    from . import vault
    v = str(v or "").strip()
    if not v:
        return old or ""
    if v.startswith("vault:"):
        return v
    name = f"event_{secrets.token_hex(6)}"
    vault.put(name, v)
    return f"vault:{name}"


def clean_params(src: Source, raw: dict, old: dict | None = None) -> dict:
    old = old or {}
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for f in src.fields:
        k, typ, label = f["key"], f.get("type", "text"), i18n.t(f.get("label") or f["key"])
        v = raw[k] if k in raw else old.get(k, f.get("default"))
        if typ == "secret":
            v = _secret(v, old.get(k))
        elif typ in ("text", "textarea", "url"):
            v = str(v if v is not None else "").strip()[:4000 if typ == "textarea" else 1000]
            if typ == "url" and v and not re.match(r"https?://[^\s/]+", v):
                raise ValueError(i18n.t("{label}要以 http:// 或 https:// 开头", label=label))
        elif typ == "number":
            if v in (None, ""):
                v = None
            else:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise ValueError(i18n.t("{label}要填数字", label=label)) from None
                v = int(v) if v.is_integer() else v
                if f.get("min") is not None and v < f["min"]:
                    raise ValueError(i18n.t("{label}至少是 {n}", label=label, n=f["min"]))
                if f.get("max") is not None and v > f["max"]:
                    raise ValueError(i18n.t("{label}最多是 {n}", label=label, n=f["max"]))
        elif typ == "bool":
            v = v not in (False, 0, "0", "false", "False", "", None)
        elif typ == "select":
            v = str(v or "")
            if v and v not in [o["value"] for o in f.get("options") or []]:
                raise ValueError(i18n.t("{label}只能选：{options}", label=label,
                                        options=" / ".join(o["value"] for o in f.get("options") or [])))
        elif typ == "multi":
            allowed = [o["value"] for o in f.get("options") or []]
            v = [x for x in _split(v) if x in allowed]
        elif typ == "list":
            v = _split(v)[:100]
        if f.get("required") and v in (None, "", []):
            raise ValueError(i18n.t("要填{label}", label=label))
        out[k] = v
    if src.validate:
        out = src.validate(out) or out
    return out


def resolve(src: Source, params: dict) -> dict:
    """给来源用的参数：secret 换成保险库里的值。"""
    from . import vault
    out = dict(params)
    for f in src.fields:
        if f.get("type") == "secret" and isinstance(v := out.get(f["key"]), str) and v.startswith("vault:"):
            out[f["key"]] = vault.get(v[6:]) or ""
    return out


def clean(t: dict, old: dict | None = None) -> dict:
    """检查事件的 trigger。old 是原来的 trigger（改的时候：没填的参数、secret、密钥沿用）。"""
    old = old if old and old.get("kind") == "event" else {}
    sid = str(t.get("source") or old.get("source") or "")
    src = get_source(sid)
    if not src:
        raise ValueError(i18n.t("没有这种事件来源：{source}", source=sid or "—"))
    if why := unavailable(src):
        raise ValueError(why)
    same = old.get("source") == sid
    params = clean_params(src, t.get("params") if isinstance(t.get("params"), dict) else {},
                          old.get("params") if same else None)
    words = lambda k: _split(t[k] if k in t else old.get(k))[:50]
    out = {"kind": "event", "source": sid, "params": params, "include": words("include"), "exclude": words("exclude"),
           "llm_filter": str(t["llm_filter"] if "llm_filter" in t else old.get("llm_filter") or "").strip()[:1000]}
    if src.mode == POLL:
        try:
            every = int(t.get("every") or (old.get("every") if same else 0) or src.every)
        except (TypeError, ValueError):
            every = src.every
        if every < src.min_every:
            raise ValueError(i18n.t("这种事件最快 {n} 分钟看一次", n=src.min_every))
        out["every"] = min(every, 7 * 24 * 60)
    if src.mode == PUSH:
        out["key"] = old.get("key") or new_key()
        if old.get("path"):
            out["path"] = old["path"]
    return out


def new_key() -> str:
    return secrets.token_urlsafe(18)


def portable(t: dict) -> dict:
    """导出成模板时的 trigger：去掉密钥、旧地址和 secret 参数。"""
    if t.get("kind") != "event":
        return t
    src = get_source(t.get("source", ""))
    secret = {f["key"] for f in (src.fields if src else []) if f.get("type") == "secret"}
    return {k: v for k, v in t.items() if k not in ("key", "path")} | \
        {"params": {k: v for k, v in (t.get("params") or {}).items() if k not in secret}}


def migrate_legacy(t: dict, name: str) -> dict:
    """旧版 kind=webhook → source=webhook 的事件，旧地址 /hooks/<名字> 记在 path 里。"""
    return {"kind": "event", "source": "webhook", "params": {}, "include": t.get("include") or [],
            "exclude": t.get("exclude") or [], "llm_filter": t.get("llm_filter") or "", "key": new_key(),
            **({"path": name} if re.fullmatch(r"[\w-]{1,40}", name or "") else {})}


def reset(rid: int):
    conn.execute("delete from event_state where routine_id=?", (rid,))
    conn.commit()


def forget(r: dict):
    """删事件时：状态、排队的事件、自动存进保险库的 secret 一起删。"""
    from . import vault
    conn.execute("delete from event_state where routine_id=?", (r["id"],))
    conn.commit()
    PENDING.pop(r["id"], None)
    for v in ((r.get("trigger") or {}).get("params") or {}).values():
        if isinstance(v, str) and v.startswith("vault:event_"):
            vault.delete(v[6:])


# ---------- 说明 ----------
def describe(t: dict, lang: str | None = None) -> str:
    src = get_source(t.get("source", ""))
    if not src:
        return i18n.t("事件来源 {source} 现在没有（插件停用了？）", lang, source=t.get("source", ""))
    title = i18n.t(src.title, lang)
    what = ""
    if src.summary:
        try:
            what = src.summary(t.get("params") or {}) or ""
        except Exception:
            what = ""
    s = f"{title}：{what}" if what else title
    if src.mode == POLL and t.get("every"):
        m = t["every"]
        s += i18n.t("（每 {n} 小时看一次）", lang, n=m // 60) if m % 60 == 0 else i18n.t("（每 {n} 分钟看一次）", lang, n=m)
    return s


def hook_path(t: dict) -> str:
    return f"/hooks/{t['key']}" if t.get("kind") == "event" and t.get("key") else ""


def hook_url(t: dict) -> str:
    from . import channels
    p = hook_path(t)
    return (channels.public_url() + p) if p else ""


def state_of(rid: int) -> dict:
    r = conn.execute("select checked, error, fails, sample from event_state where routine_id=?", (rid,)).fetchone()
    out = {"checked": r[0], "error": r[1] or "", "fails": r[2] or 0, "has_sample": bool(r[3])} if r else \
        {"checked": None, "error": "", "fails": 0, "has_sample": False}
    return out | {"pending": len(PENDING.get(rid) or [])}


def _load_state(rid: int):
    r = conn.execute("select state from event_state where routine_id=?", (rid,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def _save_state(rid: int, state=None, error: str = "", sample: dict | None = None, keep_state: bool = False):
    now = time.time()
    old = conn.execute("select state, fails, sample from event_state where routine_id=?", (rid,)).fetchone()
    st = old[0] if keep_state and old else json.dumps(state, ensure_ascii=False) if state is not None else None
    fails = ((old[1] or 0) + 1) if error and old else (1 if error else 0)
    smp = json.dumps(sample, ensure_ascii=False) if sample else (old[2] if old else "")
    conn.execute("insert or replace into event_state(routine_id, state, checked, error, fails, sample) values(?,?,?,?,?,?)",
                 (rid, st, now, error[:300], fails, smp))
    conn.commit()


# ---------- 把事件交给 Bot ----------
def _ev(e) -> dict:
    if isinstance(e, str):
        return {"title": e[:120], "text": e}
    e = dict(e)
    e["title"] = str(e.get("title") or "")[:300]
    e["text"] = str(e.get("text") or "")
    return e


def _plain(e: dict) -> str:
    return "\n".join(str(e.get(k) or "") for k in ("title", "text", "url"))


def render(src: Source | None, params: dict, events: list[dict]) -> str:
    head = f"事件来源：{src.title if src else '?'}"
    if src and src.summary:
        try:
            if what := src.summary(params):
                head += f"（{what}）"
        except Exception:
            pass
    parts, used = [head + (f"，共 {len(events)} 条" if len(events) > 1 else "")], 0
    for i, e in enumerate(events[:MAX_EVENTS], 1):
        block = (f"{i}. " if len(events) > 1 else "") + (e["title"] or "").strip()
        if e.get("url"):
            block += f"\n链接：{e['url']}"
        if e["text"].strip() and e["text"].strip() != e["title"].strip():
            block += "\n" + e["text"].strip()
        if used + len(block) > MAX_TEXT:
            parts.append(f"……还有 {len(events) - i + 1} 条，太长没放进来")
            break
        used += len(block)
        parts.append(block)
    if len(events) > MAX_EVENTS:
        parts.append(f"……还有 {len(events) - MAX_EVENTS} 条，太多没放进来")
    return "\n\n".join(parts)


async def deliver(r: dict, events: list, source: str = "event") -> dict:
    """事件来了：关键词逐条过滤 → 正在跑就排队 → LLM 过滤 → 派给 Bot。source：webhook / event / test。"""
    from . import routines
    rid, trig = r["id"], r["trigger"]
    events = [_ev(e) for e in events if e]
    if not events:
        return {"accepted": False, "reason": i18n.t("没有事件")}
    if source != "test":
        kept = [e for e in events if routines._keyword_filter(trig, _plain(e)) is None]
        if not kept:
            reason = routines._keyword_filter(trig, _plain(events[0])) or ""
            routines._record(rid, source, "filtered", reason + (f"（{len(events)}）" if len(events) > 1 else ""))
            await routines._changed(r)
            return {"accepted": False, "stage": "rule", "reason": reason}
        events = kept
    if rid in routines.RUNNING:
        q = PENDING.setdefault(rid, [])
        q.extend(events)
        del q[:-MAX_PENDING]
        await routines._changed(r)
        return {"accepted": True, "queued": len(q)}
    src = get_source(trig.get("source", ""))
    text = render(src, trig.get("params") or {}, events)
    if source != "test" and (rule := trig.get("llm_filter")):
        try:
            ok, reason = await routines._llm_filter(rule, text, r["bot_id"])
        except Exception as e:
            ok, reason = True, i18n.t("过滤出错，默认放行：{error}", error=str(e)[:100])
        if not ok:
            routines._record(rid, source, "filtered", i18n.t("LLM：{reason}", reason=reason))
            await routines._changed(r)
            return {"accepted": False, "stage": "llm", "reason": reason}
    return await routines.fire(r, source, text)


async def after_run(r: dict):
    """一次运行结束：排队的事件接着处理。"""
    if q := PENDING.pop(r["id"], None):
        from . import routines
        fresh = routines.get(r["id"])
        if fresh and fresh["enabled"]:
            await deliver(fresh, q, "event")


# ---------- push ----------
def by_key(key: str) -> dict | None:
    from . import routines
    for r in routines.list_for():
        k = r["trigger"].get("key")
        if r["trigger"].get("kind") == "event" and k and hmac.compare_digest(str(k), key):
            return r
    return None


def by_path(name: str) -> dict | None:
    """旧地址 /hooks/<名字>。"""
    from . import routines
    return next((r for r in routines.list_for() if r["trigger"].get("kind") == "event" and r["trigger"].get("path") == name), None)


async def on_push(r: dict, headers: dict, body: bytes, query: dict) -> dict:
    from . import routines
    try:
        data = json.loads(body) if body else None
    except Exception:
        data = None
    # 飞书、Slack 第一次配地址时发一个 challenge，要原样回过去
    if isinstance(data, dict) and data.get("type") == "url_verification" and "challenge" in data:
        return {"challenge": data["challenge"]}
    if not r["enabled"]:
        return {"accepted": False, "reason": i18n.t("这个事件暂停了")}
    src = get_source(r["trigger"].get("source", ""))
    if not src or src.mode != PUSH:
        return {"accepted": False, "reason": i18n.t("事件来源 {source} 现在没有（插件停用了？）", source=r["trigger"].get("source", ""))}
    req = {"headers": {k.lower(): v for k, v in headers.items()}, "body": body, "json": data, "query": dict(query)}
    try:
        out = src.parse(req, resolve(src, r["trigger"].get("params") or {}) | {"_key": r["trigger"].get("key", "")})
        if asyncio.iscoroutine(out):
            out = await out
    except PermissionError as e:
        routines._record(r["id"], "webhook", "failed", str(e))
        await routines._changed(r)
        raise
    except Exception as e:
        routines._record(r["id"], "webhook", "failed", i18n.t("解析出错：{error}", error=str(e)[:200]))
        await routines._changed(r)
        return {"accepted": False, "reason": str(e)}
    if isinstance(out, str):   # 这次不处理（ping、不关心的事件类型）
        routines._record(r["id"], "webhook", "ignored", out)
        await routines._changed(r)
        return {"accepted": False, "reason": out}
    if out:
        _save_state(r["id"], None, sample=_ev(out[0]), keep_state=True)
    return await deliver(r, out or [], "webhook")


# ---------- poll ----------
async def poll(r: dict, now: bool = False) -> dict:
    """看一次这个事件的来源，有新的就交给 Bot。now：用户点了“立即检查”。"""
    from . import routines
    rid = r["id"]
    src = get_source(r["trigger"].get("source", ""))
    if not src or src.mode != POLL or rid in _polling:
        return {"accepted": False, "reason": i18n.t("正在检查")}
    global _sem
    _sem = _sem or asyncio.Semaphore(POLL_PARALLEL)
    _polling.add(rid)
    try:
        async with _sem:
            state = _load_state(rid)
            first = state is None
            try:
                events, new_state = await asyncio.wait_for(
                    src.check(resolve(src, r["trigger"].get("params") or {}), state), POLL_TIMEOUT)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}" if not str(e) else str(e)
                if isinstance(e, asyncio.TimeoutError):
                    msg = i18n.t("{n} 秒内没看完", n=POLL_TIMEOUT)
                _save_state(rid, None, error=msg[:300], keep_state=True)
                await routines._changed(r)
                return {"accepted": False, "reason": msg}
            events = [_ev(e) for e in events or []]
            _save_state(rid, new_state if new_state is not None else (state or {}), sample=events[0] if events else None)
        if not events:
            await routines._changed(r)
            return {"accepted": False, "reason": i18n.t("已记下现状，之后有新的才通知") if first else i18n.t("没有新的")}
        if first and not src.fire_first:
            await routines._changed(r)
            return {"accepted": False, "reason": i18n.t("已记下现状，之后有新的才通知")}
        return await deliver(r, events, "event")
    finally:
        _polling.discard(rid)


def due_poll(r: dict) -> bool:
    return r["trigger"].get("kind") == "event" and (src := get_source(r["trigger"].get("source", ""))) is not None \
        and src.mode == POLL and r["id"] not in _polling


# ---------- internal ----------
def _internal(source_id: str, kw: dict):
    """osa 自己的事件到了：交给所有用这个来源、开着的事件。不等它们跑完。"""
    from . import routines
    src = get_source(source_id)
    if not src:
        return
    task = kw.get("task")
    for r in routines.list_for():
        t = r["trigger"]
        if not r["enabled"] or t.get("kind") != "event" or t.get("source") != source_id:
            continue
        if isinstance(task, dict) and TASK_ROUTINE.get(task.get("id")) == r["id"]:
            continue   # 自己派的任务不再触发自己
        try:
            evs = src.match(resolve(src, t.get("params") or {}), routine=r, **kw)
        except Exception:
            log.exception("事件来源 %s 判断出错", source_id)
            continue
        if evs:
            asyncio.get_running_loop().create_task(deliver(r, list(evs), "event"))


registry.internal_hook = _internal


# ---------- 测试 ----------
async def test(r: dict) -> dict:
    """点“测试”：poll 的先真看一次；没有新的就拿最近看到的一条（或来源自带的示例）跑一遍，结果照常发出来。"""
    src = get_source(r["trigger"].get("source", ""))
    if not src:
        return {"accepted": False, "reason": i18n.t("事件来源 {source} 现在没有（插件停用了？）", source=r["trigger"].get("source", ""))}
    if src.mode == POLL:
        out = await poll(r, now=True)
        if out.get("accepted"):
            return out
    row = conn.execute("select sample from event_state where routine_id=?", (r["id"],)).fetchone()
    sample = json.loads(row[0]) if row and row[0] else None
    sample = sample or src.sample or {"title": "（这是一次手动测试，没有真实事件）", "text": ""}
    return await deliver(r, [sample], "test")
