"""资料库：Bot 生成的页面（Pages），以及共用电脑工作文件夹里的文件（Docker / Instabox 是 /data/workspace，本机电脑见 host.py）。

页面存在数据库里，格式是 markdown 或 html。分享就是生成一个不可猜的链接 /s/<token>，取消分享后链接失效。
html 页面只通过带 CSP 的 /raw 地址在沙箱 iframe 里显示：脚本可以跑，但拿不到这个站点的任何数据，也连不上接口。
带清单 app 的 html 页面是小应用（见 applets.py）：页面里能通过 postMessage 调清单里允许的工具、读写自己的数据（page_data 表）。
文件直接读写电脑里的目录（Computer.ls / stat_paths / rm / read_bytes / write_bytes），Docker 和 Instabox 要先开机。
"""
import json
import posixpath
import re
import secrets
import time
from . import i18n
from .memory import conn

conn.execute("""create table if not exists pages(
  id integer primary key, bot_id integer, title text, format text default 'markdown', content text,
  share_token text, pinned integer default 0, task_id integer, created real, updated real, app text)""")
conn.execute("create unique index if not exists pages_token on pages(share_token)")
if "app" not in {r[1] for r in conn.execute("pragma table_info(pages)")}:
    conn.execute("alter table pages add column app text")
conn.execute("""create table if not exists page_data(
  page_id integer, key text, value text, updated real, primary key(page_id, key))""")
conn.execute("create table if not exists file_pins(path text primary key, ts real)")
conn.commit()

FORMATS = ("markdown", "html")
MAX_PAGE = 500_000
MAX_PAGES = 1000
LEGACY = "/data/workspace"   # Docker / Instabox 电脑里的工作文件夹
MAX_UPLOAD = 20 << 20
RAW_CSP = ("sandbox allow-scripts allow-popups; default-src 'none'; script-src 'unsafe-inline' https:; "
           "style-src 'unsafe-inline' https:; img-src https: data:; font-src https: data:; media-src https: data:; "
           "connect-src 'none'; frame-src 'none'; form-action 'none'")

_COLS = "id, bot_id, title, format, share_token, pinned, task_id, created, updated, length(content), app"


# ---------- 页面 ----------
def _page(r, content: str | None = None) -> dict:
    i, bot_id, title, fmt, token, pinned, task_id, created, updated, size, app = r
    p = {"id": i, "bot_id": bot_id, "title": title, "format": fmt, "share_token": token, "pinned": bool(pinned),
         "task_id": task_id, "created": created, "updated": updated, "size": size or 0,
         "app": json.loads(app) if app else None}
    if content is not None:
        p["content"] = content
    return p


def list_pages() -> list[dict]:
    return [_page(r) for r in conn.execute(f"select {_COLS} from pages order by pinned desc, updated desc")]


def get_page(pid: int, with_content: bool = True) -> dict | None:
    r = conn.execute(f"select {_COLS}, content from pages where id=?", (pid,)).fetchone()
    return _page(r[:-1], r[-1] if with_content else None) if r else None


def by_token(token: str) -> dict | None:
    if not token:
        return None
    r = conn.execute("select id from pages where share_token=?", (token,)).fetchone()
    return get_page(r[0]) if r else None


def save_page(bot_id: int | None, title: str, content: str, fmt: str = "markdown",
              pid: int | None = None, task_id: int | None = None, app=None) -> dict:
    """新建，或者给 pid 时更新内容。app 是小应用的清单（见 applets.manifest），只对 html 有效；更新时不给就保留原来的。"""
    from . import applets
    title = " ".join((title or "").split())[:120] or i18n.t("未命名页面")
    fmt = fmt if fmt in FORMATS else "markdown"
    app = applets.manifest(app) if fmt == "html" else None
    if not (content or "").strip():
        raise ValueError(i18n.t("页面内容不能为空"))
    if len(content) > MAX_PAGE:
        raise ValueError(i18n.t("页面太大（最多 {n}K 字）", n=MAX_PAGE // 1000))
    now = time.time()
    if pid is not None:
        if not (old := get_page(pid, False)):
            raise ValueError(i18n.t("没有页面 #{id}", id=pid))
        if app is None and fmt == "html":
            app = old["app"]
        conn.execute("update pages set title=?, format=?, content=?, updated=?, app=? where id=?",
                     (title, fmt, content, now, json.dumps(app, ensure_ascii=False) if app else None, pid))
    else:
        if conn.execute("select count(*) from pages").fetchone()[0] >= MAX_PAGES:
            raise ValueError(i18n.t("资料库最多 {n} 个页面，先删掉一些", n=MAX_PAGES))
        pid = conn.execute("insert into pages(bot_id, title, format, content, task_id, created, updated, app) "
                           "values(?,?,?,?,?,?,?,?)", (bot_id, title, fmt, content, task_id, now, now,
                                                       json.dumps(app, ensure_ascii=False) if app else None)).lastrowid
    conn.commit()
    return get_page(pid, False)


def update_page(pid: int, title: str | None = None, pinned: bool | None = None) -> dict:
    if not get_page(pid, False):
        raise ValueError(i18n.t("没有页面 #{id}", id=pid))
    if title is not None:
        conn.execute("update pages set title=?, updated=? where id=?",
                     (" ".join(title.split())[:120] or i18n.t("未命名页面"), time.time(), pid))
    if pinned is not None:
        conn.execute("update pages set pinned=? where id=?", (int(pinned), pid))
    conn.commit()
    return get_page(pid, False)


def share(pid: int, on: bool) -> dict:
    """打开分享时生成新链接；关掉后旧链接立即失效。"""
    conn.execute("update pages set share_token=? where id=?", (secrets.token_urlsafe(16) if on else None, pid))
    conn.commit()
    return get_page(pid, False)


def delete_page(pid: int):
    conn.execute("delete from pages where id=?", (pid,))
    conn.execute("delete from page_data where page_id=?", (pid,))
    conn.commit()


# ---------- 小应用的数据 ----------
def get_data(pid: int) -> dict:
    return {k: json.loads(v) for k, v in conn.execute("select key, value from page_data where page_id=? order by key", (pid,))}


def set_data(pid: int, updates: dict) -> dict:
    """合并写入：值为 None 的键删掉。返回全部数据。"""
    from . import applets
    cur = get_data(pid)
    new = applets.check_data(updates, cur)
    now = time.time()
    for k in set(cur) - set(new):
        conn.execute("delete from page_data where page_id=? and key=?", (pid, k))
    for k, v in new.items():
        if k not in cur or cur[k] != v:
            conn.execute("insert or replace into page_data(page_id, key, value, updated) values(?,?,?,?)",
                         (pid, k, json.dumps(v, ensure_ascii=False), now))
    conn.commit()
    return new


def link(pid: int) -> str:
    """对话里引用页面用的地址，前端会在资料库里打开它。"""
    return f"/library/pages/{pid}"


SAVE_PAGE = {"type": "function", "function": {
    "name": "save_page",
    "description": "把成品（报告、清单、对比表、网页等）保存成资料库里的页面，返回页面链接。"
                   "用户要“做成页面/报告/可以分享的页面”，或结果很长、以后还要看时用。给 page_id 就是更新那个页面。"
                   "要做成可交互的小应用（表单、看板、计算器、能点按钮查东西的面板）时 format=html 并填 app，"
                   "页面里用 window.osa 调工具、读写数据、给你发消息（见系统提示词里的说明）。",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string"},
        "content": {"type": "string", "description": "页面全文。markdown 用 Markdown；html 是完整的 HTML（可以带内联 CSS 和脚本）"},
        "format": {"type": "string", "enum": list(FORMATS)},
        "page_id": {"type": "integer", "description": "可选：要更新的页面编号"},
        "app": {"type": "object", "description": "可选，只对 html 有效：填了就是小应用。tools 是页面里允许调用的工具名（只能是你现在有的工具）；"
                                                 "chat 为 true 时页面能替用户给你发消息",
                "properties": {"tools": {"type": "array", "items": {"type": "string"}}, "chat": {"type": "boolean"}}}},
        "required": ["title", "content"]}}}


PAGE_DATA = {"type": "function", "function": {
    "name": "page_data",
    "description": "读或写一个小应用的数据（键值对）。用户在小应用里填的内容在这里；你写进去的数据小应用里马上能看到（osa.store / osa.on('data')）。"
                   "不给 set 就只读。",
    "parameters": {"type": "object", "properties": {
        "page_id": {"type": "integer"},
        "set": {"type": "object", "description": "要写入的键值（值可以是任何 JSON；设为 null 删除这个键）"}},
        "required": ["page_id"]}}}


async def tool_page_data(bot_id: int, args: dict) -> str:
    pid = args.get("page_id")
    p = get_page(pid, False) if isinstance(pid, int) else None
    if not p:
        return f"没有页面 #{pid}"
    if not p.get("app"):
        return f"页面 #{pid} 不是小应用，没有数据。要做成小应用，用 save_page 更新它并填 app"
    if (upd := args.get("set")) is not None:
        if p["bot_id"] != bot_id:
            return f"没写：小应用 #{pid} 不是你建的，只能读"
        try:
            data = set_data(pid, upd)
        except (ValueError, TypeError) as e:
            return f"没写：{e}"
        from . import registry
        await registry.emit("page_data", page_id=pid, data=data)
        head = f"已写入 {len(upd)} 个键。"
    else:
        data, head = get_data(pid), ""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    return f"{head}小应用「{p['title']}」的数据（{len(data)} 个键）：\n{text[:12000]}" + ("\n…（太长，截断）" if len(text) > 12000 else "")



READ_PAGE = {"type": "function", "function": {
    "name": "read_page",
    "description": "读资料库里一个页面的全文（html 页面只给文字）。页面编号可以用 search_memory 查到",
    "parameters": {"type": "object", "properties": {"page_id": {"type": "integer"}}, "required": ["page_id"]}}}


def tool_read_page(args: dict, limit: int = 12000) -> str:
    from . import search
    p = get_page(args.get("page_id")) if isinstance(args.get("page_id"), int) else None
    if not p:
        return f"没有页面 #{args.get('page_id')}"
    text = search._plain(p["content"] or "", p["format"])
    if p["format"] == "html":
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
    more = f"\n…（还有 {len(text) - limit} 字没显示）" if len(text) > limit else ""
    return f"《{p['title']}》{link(p['id'])}，{time.strftime('%Y-%m-%d', time.localtime(p['updated']))} 更新\n\n{text[:limit]}{more}"


def tool_save_page(bot_id: int, args: dict, task_id: int | None = None) -> str:
    pid = args.get("page_id")
    if pid is not None and (not (old := get_page(pid, False)) or old["bot_id"] != bot_id):
        return f"没保存：页面 #{pid} 不存在或不是你建的，只能更新自己的页面"
    app = args.get("app")
    if app and (unknown := [t for t in (app.get("tools") or []) if isinstance(t, str) and not applets_known(t)]):
        return f"没保存：app.tools 里有现在没有的工具 {', '.join(unknown)}，只能列你这一轮能用的工具"
    try:
        p = save_page(bot_id, args.get("title", ""), args.get("content", ""), args.get("format") or "markdown",
                      args.get("page_id"), task_id, app)
    except (ValueError, TypeError) as e:
        return f"没保存：{e}"
    if p["app"]:
        return (f"已保存小应用 #{p['id']}「{p['title']}」，链接 {link(p['id'])}。回复里用 Markdown 链接附上它，对话里会直接显示这个小应用，"
                f"用户能在里面操作。用 page_data 能读写它的数据（page_id={p['id']}）。")
    return f"已保存页面 #{p['id']}「{p['title']}」，链接 {link(p['id'])}（用户在资料库里也能看到）。回复里用 Markdown 链接附上它。"


def applets_known(tool: str) -> bool:
    from . import applets
    return applets.known(tool)


# ---------- 文件 ----------
def root() -> str:
    """现在这台电脑的工作文件夹。"""
    from . import computer
    return computer.get().workspace


def norm(path: str | None, base: str | None = None) -> str:
    """把路径规范成工作文件夹下的绝对路径（相对路径从工作文件夹算起）；跑出去就报错。"""
    base = base or root()
    p = (path or "").strip() or base
    if base != LEGACY and (p == LEGACY or p.startswith(LEGACY + "/")):   # 技能、记忆里可能还写着 Docker 电脑里的路径
        p = base + p[len(LEGACY):]
    if not p.startswith("/"):
        p = posixpath.join(base, p)
    p = posixpath.normpath(p)
    if p != base and not p.startswith(base.rstrip("/") + "/"):
        raise ValueError(i18n.t("只能访问 {dir} 里的文件", dir=base))
    return p


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name or len(name) > 200:
        raise ValueError(i18n.t("文件名不合法"))
    return name


async def list_dir(c, path: str) -> list[dict]:
    out = await c.ls(norm(path, c.workspace))
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return out


async def put_file(c, directory: str, name: str, data: bytes) -> dict:
    """把文件写进电脑里的目录。同名时自动加序号，不覆盖。"""
    d, name = norm(directory, c.workspace), check_name(name)
    if len(data) > MAX_UPLOAD:
        raise ValueError(i18n.t("文件太大（最多 {n} MB）", n=MAX_UPLOAD >> 20))
    try:
        existing = {e["name"] for e in await list_dir(c, d)}
    except ValueError:
        existing = set()   # 目录还不存在，写文件时会自动建
    stem, ext = posixpath.splitext(name)
    final, n = name, 1
    while final in existing:
        n += 1
        final = f"{stem} ({n}){ext}"
    path = posixpath.join(d, final)
    await c.write_bytes(path, data)
    return {"path": path, "name": final, "size": len(data)}


async def stat_many(c, paths: list[str]) -> dict[str, dict]:
    """置顶文件的现状（可能已经被删了）。"""
    return await c.stat_paths(paths) if paths else {}


async def remove(c, path: str):
    p = norm(path, c.workspace)
    if p == c.workspace:
        raise ValueError(i18n.t("不能删除整个工作目录"))
    await c.rm(p)
    unpin(p)


def pins() -> list[str]:
    """置顶的文件。换了电脑后，别的电脑里的置顶先不显示，换回去还在。"""
    base = root().rstrip("/") + "/"
    return [r[0] for r in conn.execute("select path from file_pins order by ts") if (r[0] + "/").startswith(base)]


def pin(path: str):
    conn.execute("insert or replace into file_pins values(?, ?)", (norm(path), time.time()))
    conn.commit()


def unpin(path: str):
    p = norm(path)
    conn.execute("delete from file_pins where path=? or path like ?", (p, p + "/%"))
    conn.commit()
