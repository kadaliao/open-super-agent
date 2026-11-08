"""演示一遍（Teach a task）：用户在共用电脑上做一遍，Bot 把录到的操作整理成技能草稿，用户审核后保存。

- 开始：没有任务在用电脑时，独占整台电脑（任务都排队），在电脑里启动录制进程（cdp_helper.py 的 record），再把控制权交给用户。
- 录制只记浏览器里的操作：打开的网址、点击的元素、输入的内容（密码类只记 ******）、选择、回车。
- 结束：停掉录制，交还控制权，把操作排成文字交给模型，生成技能草稿（名字、说明、做法）；不自动保存。
- 最长 MAX_S 秒，到时自动结束并生成草稿，通过 teach 事件推给前端。
"""
import asyncio
import json
import re
import time
from urllib.parse import urlparse
from .config import call_kwargs, client_for
from . import computer, i18n, memory, skills

MAX_S = 600
KEY = "teach"
state: dict | None = None      # {"bot_id", "started", "note", "file"}
notify = None                  # 由 server 设置
_timer: asyncio.Task | None = None


def public() -> dict | None:
    return {k: state[k] for k in ("bot_id", "started", "note")} | {"max_s": MAX_S} if state else None


async def _changed(extra: dict | None = None, pc: int | None = None):
    if notify:
        if pc is not None:   # 接手 / 交还要马上反映到电脑画面上
            await notify({"type": "computer", "computer": await computer.public_state(pc)})
        await notify({"type": "teach", "teach": public()} | (extra or {}))


async def start(bot_id: int, note: str = "") -> dict:
    global state, _timer
    if state:
        raise ValueError(i18n.t("已经在录制了，先结束上一段"))
    c = computer.get()
    if c.users or c.exclusive:
        raise ValueError(i18n.t("电脑正在被用着（{who}），等它做完或者先停掉再录", who=c.busy()))
    await c.start()
    try:
        await c.claim_exclusive(KEY, i18n.t("演示录制"))
    except computer.ComputerError as e:
        raise ValueError(i18n.t("{error}，等它做完或者先停掉再录", error=e)) from None
    path = f"/tmp/osa-rec-{int(time.time())}.jsonl"
    try:
        await c.browser("rec_start", as_human=True, file=path, max_s=MAX_S + 30)
    except Exception:
        await c.release(KEY)
        raise
    c.takeover()
    if c.backend in ("host", "linked"):   # 本机电脑：在 Chrome 窗口里直接演示，把它放到前面
        try:
            await c.show()
        except computer.ComputerError:
            pass
    state = {"bot_id": bot_id, "started": time.time(), "note": (note or "").strip()[:500], "file": path}
    _timer = asyncio.create_task(_auto_stop(state["started"]))
    await _changed(pc=bot_id)
    return public()


async def _auto_stop(started: float):
    await asyncio.sleep(MAX_S)
    if state and state["started"] == started:
        try:
            result = await stop()
            await _changed({"bot_id": result["bot_id"], "draft": result["draft"], "steps": result["steps"], "auto": True})
        except Exception as e:
            await _changed({"error": i18n.t("自动结束录制时出错：{error}", error=e)})


async def _finish() -> tuple[dict, list[dict]]:
    """停掉录制、交还电脑，返回 (录制信息, 录到的操作)。"""
    global state, _timer
    if not state:
        raise ValueError(i18n.t("现在没有在录制"))
    s, state = state, None
    if _timer and _timer is not asyncio.current_task():
        _timer.cancel()
    _timer = None
    c = computer.get()
    try:
        r = await c.browser("rec_stop", as_human=True, file=s["file"])
        events = r.get("events") or []
    finally:
        c.handback()
        await c.release(KEY)
        await _changed(pc=s["bot_id"])
    return s, events


async def cancel():
    await _finish()


async def stop() -> dict:
    s, events = await _finish()
    steps = describe(events, s["started"])
    if not steps:
        raise ValueError(i18n.t("没录到浏览器里的操作。录制只记网页上的点击、输入和打开的网址"))
    draft = await draft_skill(steps, s["note"])
    return {"bot_id": s["bot_id"], "draft": draft, "steps": steps}


# ---------- 整理 ----------
def describe(events: list[dict], started: float) -> list[str]:
    """把录到的事件排成一行一步的文字；合并重复的输入，去掉连续的同一网址。"""
    out, last_url, last = [], "", None
    for e in events:
        kind = e.get("type")
        sig = json.dumps({k: v for k, v in e.items() if k not in ("t", "title", "url")}, ensure_ascii=False, sort_keys=True)
        if kind == "input" and last and last[0] == "input" and last[1] == e.get("field"):
            out.pop()            # 同一个输入框连续记了几次（回车时一次、失去焦点时一次），只留最后一次
        elif sig == (last[2] if last else None) and kind != "navigate":
            continue
        when = time.strftime("%M:%S", time.gmtime(max(0, (e.get("t") or started) - started)))
        if kind == "navigate":
            url = e.get("url", "")
            if url == last_url:
                continue
            last_url = url
            line = i18n.t("开始时在 {url}", url=url) if e.get("start") else \
                i18n.t("新标签页打开 {url}", url=url) if e.get("new_tab") else i18n.t("页面到了 {url}", url=url)
            line += i18n.t("（{title}）", title=e["title"]) if e.get("title") else ""
        elif kind == "click":
            line = i18n.t("点击 {kind}「{name}」", kind=e.get("kind", i18n.t("元素")), name=e.get("name") or i18n.t("（无文字）")) \
                + (f" → {e['href']}" if e.get("href") else "")
        elif kind == "input":
            line = i18n.t("在「{field}」输入「{value}」", field=e.get("field") or i18n.t("输入框"), value=e.get("value", "")) \
                + (i18n.t("（密码类，没有记录内容）") if e.get("secret") else "") + (i18n.t("，然后按回车") if e.get("submit") else "")
        elif kind == "select":
            line = i18n.t("在「{field}」选择「{value}」", field=e.get("field") or i18n.t("下拉框"), value=e.get("value", ""))
        elif kind == "check":
            line = i18n.t("勾选「{field}」", field=e.get("field", "")) if e.get("checked") else \
                i18n.t("取消勾选「{field}」", field=e.get("field", ""))
        elif kind == "key":
            line = i18n.t("按 {key}", key=e.get("key"))
        else:
            continue
        if kind != "navigate" and e.get("url") and urlparse(e["url"]).netloc != urlparse(last_url).netloc:
            line += i18n.t("（在 {site}）", site=urlparse(e["url"]).netloc)
        out.append(f"{when} {line}")
        last = (kind, e.get("field"), sig)
    return out[-300:]


DRAFT_PROMPT = """用户刚在浏览器里演示了一遍怎么做一件事，下面是录下来的操作（时间、动作、页面、元素）。
请把它整理成一个可复用的技能，让 Bot 以后能用自己的浏览器工具照着做（打开网址、按文字找到按钮或输入框点击、输入、回车、读页面）。
- name：简短的名字，用中文、字母、数字、- 或 _，不超过 12 个字，不要空格。
- description：一句话说明什么时候用。
- body：完整的做法，分步骤写：从哪个网址开始，点哪个按钮（写按钮上的文字），在哪个输入框输入什么，最后怎么判断做完、要给用户什么结果。
  看起来每次会变的内容（搜索词、日期、收件人等）写成「{要搜索的内容}」这样的占位，并在开头说明调用时由用户补充。
  密码、验证码不要写进技能；遇到登录或验证码，写“请用户接手电脑处理”。演示里多余的、点错又退回的步骤去掉。
只输出 JSON：{"name": "...", "description": "...", "body": "..."}"""


def _parse(text: str) -> dict:
    """模型偶尔会在 JSON 外面包代码块或多说一句：先整段解析，不行就取最外层的 {...}。"""
    for s in (text, (m.group(0) if (m := re.search(r"\{[\s\S]*\}", text or "")) else "")):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    return {}


async def draft_skill(steps: list[str], note: str = "") -> dict:
    client, model = client_for("router")
    user = (f"用户说要演示的是：{note}\n\n" if note else "") + "录到的操作：\n" + "\n".join(steps)
    d = {}
    for _ in range(2):   # 解析不出来就再问一次
        resp = await client.chat.completions.create(model=model, messages=[
            {"role": "system", "content": DRAFT_PROMPT + i18n.model_hint()}, {"role": "user", "content": user}],
            response_format={"type": "json_object"}, **call_kwargs("router"))
        memory.record_usage("router", model, resp.usage)
        d = _parse(resp.choices[0].message.content or "")
        if d.get("body"):
            break
    name = re.sub(r"[^\w一-鿿-]", "", str(d.get("name") or ""))[:40]
    if not name or not skills.NAME_RE.match(name):
        name = i18n.t("演示{time}", time=time.strftime("%m%d%H%M"))
    body = str(d.get("body") or "").strip() or i18n.t("按下面录到的步骤做：") + "\n" + "\n".join(steps)
    return {"name": name, "description": str(d.get("description") or note or "")[:200], "body": body[:8000]}
