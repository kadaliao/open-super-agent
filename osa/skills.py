"""技能：可复用的做事方法。用户在对话里用 /名字 调用，Bot 按技能正文去做。

bot_id 为空的技能所有 Bot 共用；同名时 Bot 自己的优先。

导出、导入、分享：一个技能一个 Markdown 文件，开头是 YAML（name、description，可选 apps），后面是做法。
和 Claude 等用的 SKILL.md 写法一样，别处写的技能也能直接导入（不认识的键忽略）。
分享就是生成一个不可猜的链接 /skill/<token>，别人在自己的 open-super-agent 里粘贴这个链接导入；取消分享后链接失效。
导入的技能先打开编辑器给用户看一遍再保存：技能是交给 Bot 照着做的说明，来路不明的可能让 Bot 做不该做的事。
"""
import re
import secrets
import time
from urllib.parse import urlparse
import httpx
import yaml
from . import i18n
from .memory import conn

conn.execute("""create table if not exists skills(
  id integer primary key, bot_id integer, name text, description text default '', body text,
  source text default 'manual', created real, updated real)""")
if "share_token" not in {r[1] for r in conn.execute("pragma table_info(skills)")}:
    conn.execute("alter table skills add column share_token text")
conn.execute("create unique index if not exists skills_token on skills(share_token)")
conn.commit()

NAME_RE = re.compile(r"^[\w一-鿿-]{1,40}$")
MAX_PER_BOT = 100
MAX_BODY = 20000
MAX_FILE = 200_000
SOURCES = ("manual", "chat", "teach", "import")


def _row(r) -> dict:
    i, bot_id, name, desc, body, source, created, updated, token = r
    return {"id": i, "bot_id": bot_id, "name": name, "description": desc or "", "body": body or "",
            "source": source, "created": created, "updated": updated, "share_token": token}

_COLS = "id, bot_id, name, description, body, source, created, updated, share_token"


def list_for(bot_id: int) -> list[dict]:
    """这个 Bot 能用的技能（自己的 + 共用的），同名时自己的覆盖共用的。"""
    rows = [_row(r) for r in conn.execute(
        f"select {_COLS} from skills where bot_id=? or bot_id is null order by bot_id is null, name", (bot_id,))]
    seen, out = set(), []
    for s in rows:
        if s["name"] not in seen:
            seen.add(s["name"])
            out.append(s)
    return out


def get(sid: int) -> dict | None:
    r = conn.execute(f"select {_COLS} from skills where id=?", (sid,)).fetchone()
    return _row(r) if r else None


def find(bot_id: int, name: str) -> dict | None:
    return next((s for s in list_for(bot_id) if s["name"] == name), None)


def _check(name: str, body: str):
    name = name.strip().lstrip("/")
    if not NAME_RE.match(name):
        raise ValueError(i18n.t("技能名只能用中文、字母、数字、- 和 _，最长 40 个字"))
    if not body.strip():
        raise ValueError(i18n.t("技能内容不能为空"))
    if len(body) > MAX_BODY:
        raise ValueError(i18n.t("技能做法太长了（{count} 个字），最多 {max} 个字", count=len(body), max=MAX_BODY))
    return name


def save(bot_id: int | None, name: str, body: str, description: str = "", source: str = "manual",
         sid: int | None = None) -> dict:
    """新建或修改。同一个 Bot 下同名的技能会被覆盖。"""
    name = _check(name, body)
    now = time.time()
    if sid is None:
        r = conn.execute("select id from skills where name=? and bot_id is ?", (name, bot_id)).fetchone()
        sid = r[0] if r else None
    if sid is None:
        n = conn.execute("select count(*) from skills where bot_id is ?", (bot_id,)).fetchone()[0]
        if n >= MAX_PER_BOT:
            raise ValueError(i18n.t("每个 Bot 最多 {n} 个技能", n=MAX_PER_BOT))
        sid = conn.execute("insert into skills(bot_id, name, description, body, source, created, updated) "
                           "values(?,?,?,?,?,?,?)", (bot_id, name, description[:200], body, source, now, now)).lastrowid
    else:
        dup = conn.execute("select id from skills where name=? and bot_id is ? and id != ?", (name, bot_id, sid)).fetchone()
        if dup:
            raise ValueError(i18n.t("已经有叫 /{name} 的技能了", name=name))
        conn.execute("update skills set bot_id=?, name=?, description=?, body=?, updated=? where id=?",
                     (bot_id, name, description[:200], body, now, sid))
    conn.commit()
    return get(sid)


def delete(sid: int):
    conn.execute("delete from skills where id=?", (sid,))
    conn.commit()


def parse(bot_id: int, text: str) -> tuple[dict, str] | None:
    """消息以 /技能名 开头时返回 (技能, 其余文字)。"""
    m = re.match(r"^/([\w一-鿿-]+)(?:\s+([\s\S]*))?$", text.strip())
    if not m:
        return None
    s = find(bot_id, m[1])
    return (s, (m[2] or "").strip()) if s else None


def expand(skill: dict, extra: str) -> str:
    """把技能写成交给模型的说明。"""
    return (f"（用户调用了技能 /{skill['name']}，请按下面的做法执行；用户补充的要求优先。）\n"
            f"--- 技能做法 ---\n{skill['body']}\n--- 结束 ---"
            + (f"\n用户补充：{extra}" if extra else ""))


def summary(bot_id: int) -> str:
    return "\n".join(f"- /{s['name']}：{s['description'] or s['body'][:40]}" for s in list_for(bot_id)) or "暂无技能"


# ---------- 导出、导入、分享 ----------
_APP_REF = re.compile(r"(?<![\w@.])@([a-z][a-z0-9_-]{0,23})\b")


def apps_in(body: str) -> list[str]:
    """做法里 @ 到的应用（应用市场里有的，或者本机装了的）。"""
    from . import catalog, mcp_hub
    known = set(catalog.items()) | set(mcp_hub.all_apps())
    return sorted({m for m in _APP_REF.findall(body or "") if m in known})


def to_file(skill: dict) -> str:
    head = {"name": skill["name"], "description": skill["description"] or ""}
    if apps := apps_in(skill["body"]):
        head["apps"] = apps
    meta = yaml.safe_dump(head, allow_unicode=True, sort_keys=False, width=1000).strip()
    return f"---\n{meta}\n---\n\n{skill['body'].strip()}\n"


def _clean_name(name: str) -> str:
    """别处的技能名可能有空格、点：换成 -，去掉不能用的字。"""
    name = re.sub(r"[\s.]+", "-", str(name or "").strip().lstrip("/"))
    return re.sub(r"[^\w一-鿿-]", "", name).strip("-_")[:40]


def from_file(text: str, filename: str = "") -> dict:
    """解析一个技能文件，返回 {name, description, body}；格式不对就抛 ValueError。"""
    text = (text or "").lstrip("\ufeff").replace("\r\n", "\n")
    if len(text) > MAX_FILE:
        raise ValueError(i18n.t("文件太大了，不像是一个技能"))
    head, body = {}, text
    if m := re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", text, re.S):
        try:
            head = yaml.safe_load(m[1]) or {}
        except yaml.YAMLError as e:
            raise ValueError(i18n.t("开头的 YAML 写错了：{e}", e=str(e).splitlines()[0]))
        if not isinstance(head, dict):
            raise ValueError(i18n.t("开头的 YAML 应该是一组「键: 值」"))
        body = text[m.end():]
    body = body.strip()
    name = head.get("name")
    if not name and filename:   # 没写名字：用文件名（SKILL.md 用它所在的文件夹名，调用方传进来）
        name = re.sub(r"(\.skill)?\.(md|markdown|txt)$", "", filename.rsplit("/", 1)[-1], flags=re.I)
        name = "" if name.upper() == "SKILL" else name
    if not name and (h := re.match(r"^#\s+(.+)", body)):
        name = h[1]
    name = _clean_name(name or "")
    if not name:
        raise ValueError(i18n.t("没找到技能的名字：在文件开头写上 name"))
    desc = head.get("description") or ""
    if not isinstance(desc, str):
        desc = str(desc)
    _check(name, body)
    return {"name": name, "description": " ".join(desc.split())[:200], "body": body}


def share(sid: int, on: bool) -> dict:
    s = get(sid)
    if not s:
        raise ValueError(i18n.t("没有这个技能"))
    token = (s["share_token"] or secrets.token_urlsafe(16)) if on else None
    conn.execute("update skills set share_token=? where id=?", (token, sid))
    conn.commit()
    return get(sid)


def by_token(token: str) -> dict | None:
    if not token:
        return None
    r = conn.execute(f"select {_COLS} from skills where share_token=?", (token,)).fetchone()
    return _row(r) if r else None


def raw_url(url: str) -> tuple[str, str]:
    """别人给的链接 → 能直接取到文件内容的地址，和用来猜名字的文件名。"""
    url = url.strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(i18n.t("链接要以 http:// 或 https:// 开头"))
    parts = [x for x in u.path.split("/") if x]
    if len(parts) == 2 and parts[0] == "skill":                       # 别人的 open-super-agent 分享出来的
        return f"{u.scheme}://{u.netloc}/api/shared/skills/{parts[1]}/raw", ""
    if u.netloc == "github.com" and len(parts) >= 5 and parts[2] in ("blob", "tree"):
        path = "/".join(parts[4:])
        if parts[2] == "tree" or not re.search(r"\.(md|markdown|txt)$", path, re.I):   # 一个文件夹：取里面的 SKILL.md
            folder, path = parts[-1], path + "/SKILL.md"
        else:
            folder = parts[-2] if parts[-1].upper() == "SKILL.MD" and len(parts) > 5 else ""
        name = folder or parts[-1]
        return f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{parts[3]}/{path}", name
    name = parts[-2] if parts and parts[-1].upper() == "SKILL.MD" and len(parts) > 1 else (parts[-1] if parts else "")
    return url, name


async def fetch(url: str) -> dict:
    """从链接取一个技能文件并解析。"""
    src, name = raw_url(url)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            async with c.stream("GET", src, headers={"accept": "text/markdown, text/plain;q=0.9, */*;q=0.5"}) as r:
                if r.status_code == 404:
                    raise ValueError(i18n.t("链接打不开（404）：分享可能已经取消了，或者地址不对"))
                if r.status_code >= 400:
                    raise ValueError(i18n.t("链接打不开（{code}）", code=r.status_code))
                if "text/html" in r.headers.get("content-type", ""):
                    raise ValueError(i18n.t("这个链接打开是一个网页，不是技能文件。GitHub 上的请用文件或文件夹的链接"))
                data = b""
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_FILE:
                        raise ValueError(i18n.t("文件太大了，不像是一个技能"))
    except httpx.HTTPError as e:
        raise ValueError(i18n.t("取不到这个链接（{e}）", e=type(e).__name__))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(i18n.t("不是文字文件"))
    return from_file(text, name)
