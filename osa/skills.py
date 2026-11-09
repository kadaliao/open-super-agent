"""技能：可复用的做事方法。用户在对话里用 /名字 调用；Bot 觉得某个技能管这件事时，自己用 use_skill 取做法照着做。

bot_id 为空的技能所有 Bot 共用；同名时 Bot 自己的优先。

技能包和 Claude 等用的 SKILL.md 一样：SKILL.md 开头是 YAML（name、description，可选 apps），后面是做法；
旁边可以带文件（scripts/ 里的脚本、references/ 里的参考资料……），存在 files 列（相对路径 → 内容，只收 UTF-8 文字，合计 ≤ 20 万字）。
别处写的技能也能直接导入，不认识的键忽略。apps 写了就以它为准，没写才按做法里的 @名字 找。

- 导出：没带文件的是一个 .md；带了的是 zip（SKILL.md 在根上，文件按相对路径）。
- 导入：.md、zip、链接（别人分享的 /skill/<token>、GitHub 上的文件或文件夹，文件夹用 GitHub 的接口列出里面的文件逐个取）。
  导入的技能先打开编辑器给用户看一遍再保存：技能是交给 Bot 照着做的说明，来路不明的可能让 Bot 做不该做的事。
- 分享：生成一个不可猜的链接 /skill/<token>，别人在自己的 open-super-agent 里粘贴导入；取消分享后链接失效。
- 技能市场：osa/catalog/skills/<名字>/SKILL.md（英文界面装 SKILL.en.md，有的话），随项目发布；装就是复制进 skills 表（source=market）。
"""
import asyncio
import io
import json
import logging
import re
import secrets
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse
import httpx
import yaml
from . import i18n
from .memory import conn

log = logging.getLogger(__name__)

conn.execute("""create table if not exists skills(
  id integer primary key, bot_id integer, name text, description text default '', body text,
  source text default 'manual', created real, updated real)""")
_have = {r[1] for r in conn.execute("pragma table_info(skills)")}
for _col in ("share_token", "files", "apps"):   # files：JSON（相对路径 → 内容）；apps：JSON 列表，空表示没声明
    if _col not in _have:
        conn.execute(f"alter table skills add column {_col} text")
conn.execute("create unique index if not exists skills_token on skills(share_token)")
conn.commit()

NAME_RE = re.compile(r"^[\w一-鿿-]{1,40}$")
MAX_PER_BOT = 100
MAX_BODY = 20000
MAX_FILE = 200_000          # 一个 SKILL.md 最大多少字
MAX_FILES = 50              # 一个技能最多带几个文件
MAX_FILES_TOTAL = 200_000   # 带的文件合计最多多少字
MAX_ZIP = 2_000_000         # 上传、下载的 zip 最大多少字节
PREVIEW = 400               # use_skill 给每个文件附上开头多少字
CHUNK = 7000                # use_skill 一次最多给多少字（Worker 的工具结果截到 8000 字）
SOURCES = ("manual", "chat", "teach", "import", "market")
MARKET = Path(__file__).parent / "catalog" / "skills"
MARKET_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
APP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_PART = re.compile(r'^(?!\.)[^/\\\x00-\x1f:*?"<>|]{1,80}$')     # 路径的每一段：不以 . 开头（也就没有 ..），没有奇怪的字
_SKILL_MD = re.compile(r"^skill(\.[a-z]{2})?\.md$", re.I)        # SKILL.md、SKILL.en.md：做法本身，不算带的文件
_BINARY = re.compile(r"\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|tgz|bz2|xz|7z|rar|ttf|otf|woff2?|eot|mp[34]|mov|wav|"
                     r"exe|dll|so|dylib|bin|class|jar|pyc|docx?|xlsx?|pptx?|sqlite|db)$", re.I)
_KEEP = object()


def _row(r) -> dict:
    i, bot_id, name, desc, body, source, created, updated, token, files, apps = r
    return {"id": i, "bot_id": bot_id, "name": name, "description": desc or "", "body": body or "",
            "source": source, "created": created, "updated": updated, "share_token": token,
            "files": json.loads(files) if files else {}, "apps": json.loads(apps) if apps else None}

_COLS = "id, bot_id, name, description, body, source, created, updated, share_token, files, apps"


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


def clean_path(p) -> str:
    """技能包里文件的相对路径：统一成 a/b.py；绝对路径、..、以 . 开头的（.git、.env）都不要，返回空。"""
    p = str(p or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    if not p or len(p) > 200 or p.startswith("/"):
        return ""
    parts = p.split("/")
    return p if all(_PART.match(x) and x == x.strip() for x in parts) else ""


def check_files(files) -> dict[str, str]:
    """带的文件：{相对路径: 文字}，按路径排好。不对就抛 ValueError。"""
    if not files:
        return {}
    if not isinstance(files, dict):
        raise ValueError(i18n.t("技能带的文件应该是「相对路径: 文字内容」"))
    out, total = {}, 0
    for raw, text in files.items():
        path = clean_path(raw)
        if not path:
            raise ValueError(i18n.t("文件路径不对：{path}（要用相对路径，不能有 ..，也不能以 . 开头）", path=str(raw)[:80]))
        if _SKILL_MD.match(path):
            raise ValueError(i18n.t("SKILL.md 就是做法本身，不用再作为文件带上"))
        if not isinstance(text, str) or "\x00" in text:
            raise ValueError(i18n.t("{path} 不是文字文件：技能只能带文字文件（脚本、Markdown、JSON……）", path=path))
        total += len(text)
        out[path] = text
    if len(out) > MAX_FILES:
        raise ValueError(i18n.t("带的文件太多了（{count} 个），最多 {max} 个", count=len(out), max=MAX_FILES))
    if total > MAX_FILES_TOTAL:
        raise ValueError(i18n.t("带的文件太大了（合计 {count} 个字），最多 {max} 个字", count=total, max=MAX_FILES_TOTAL))
    return dict(sorted(out.items()))


def clean_apps(apps) -> list[str]:
    """frontmatter 里的 apps：列表，或者用逗号、空格分开的一串；@notion、Notion 都认成 notion，不像应用名的去掉。"""
    if isinstance(apps, str):
        apps = re.split(r"[\s,，、]+", apps)
    if not isinstance(apps, list):
        return []
    out = [str(a).strip().lstrip("@").lower() for a in apps if isinstance(a, (str, int))]
    return list(dict.fromkeys(a for a in out if APP_ID.match(a)))[:20]


def save(bot_id: int | None, name: str, body: str, description: str = "", source: str = "manual",
         sid: int | None = None, files=None, apps=_KEEP) -> dict:
    """新建或修改。同一个 Bot 下同名的技能会被覆盖。
    files（{相对路径: 文字}）不给、apps 不给时：修改的保持原样，新建的没有。apps=None 是没声明（用的时候按做法里的 @名字 找）。"""
    name = _check(name, body)
    cols = {}
    if files is not None:
        cols["files"] = json.dumps(f, ensure_ascii=False) if (f := check_files(files)) else None
    if apps is not _KEEP:
        cols["apps"] = None if apps is None else json.dumps(clean_apps(apps))
    now = time.time()
    if sid is None:
        r = conn.execute("select id from skills where name=? and bot_id is ?", (name, bot_id)).fetchone()
        sid = r[0] if r else None
    if sid is None:
        n = conn.execute("select count(*) from skills where bot_id is ?", (bot_id,)).fetchone()[0]
        if n >= MAX_PER_BOT:
            raise ValueError(i18n.t("每个 Bot 最多 {n} 个技能", n=MAX_PER_BOT))
        sid = conn.execute("insert into skills(bot_id, name, description, body, source, created, updated, files, apps) "
                           "values(?,?,?,?,?,?,?,?,?)", (bot_id, name, description[:200], body, source, now, now,
                                                         cols.get("files"), cols.get("apps"))).lastrowid
    else:
        dup = conn.execute("select id from skills where name=? and bot_id is ? and id != ?", (name, bot_id, sid)).fetchone()
        if dup:
            raise ValueError(i18n.t("已经有叫 /{name} 的技能了", name=name))
        extra = "".join(f", {k}=?" for k in cols)
        conn.execute(f"update skills set bot_id=?, name=?, description=?, body=?, updated=?{extra} where id=?",
                     (bot_id, name, description[:200], body, now, *cols.values(), sid))
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


def expand(skill: dict, extra: str = "", by_user: bool = True) -> str:
    """把技能写成交给模型的说明。by_user：用户用 /名字 调用的（False 是 Bot 自己用 use_skill 取的）。"""
    head = f"（用户调用了技能 /{skill['name']}，请按下面的做法执行；用户补充的要求优先。）" if by_user else \
        f"（技能 /{skill['name']} 的做法，按它执行；和用户这次的要求冲突时以用户的为准。）"
    files = skill.get("files") or {}
    return (f"{head}\n--- 技能做法 ---\n{skill['body']}\n--- 结束 ---"
            + (f"\n这个技能带了文件：{'、'.join(files)}。要看全文或运行，用 use_skill(name=\"{skill['name']}\", file=\"路径\") 取。"
               if by_user and files else "")
            + (f"\n用户补充：{extra}" if extra else ""))


def summary(bot_id: int) -> str:
    return "\n".join(f"- /{s['name']}：{s['description'] or s['body'][:40]}" for s in list_for(bot_id)) or "暂无技能"


# ---------- use_skill 工具 ----------
USE_SKILL = {"type": "function", "function": {
    "name": "use_skill",
    "description": "取一个技能的做法和它带的文件（脚本、参考资料）。要按某个技能做事时先调用它，再照着做",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "技能名，不带 /"},
        "file": {"type": "string", "description": "可选：要看全文的文件，写技能带的文件的相对路径（比如 scripts/fill.py）"},
        "offset": {"type": "integer", "description": "可选：内容太长分段取时，从第几个字开始"}},
        "required": ["name"]}}}


def _piece(text: str, offset, label: str, again: str) -> tuple[str, bool]:
    """一段内容：太长就只给 CHUNK 个字，告诉模型怎么取下一段。返回 (文字, 后面还有没有)。"""
    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0
    part = text[off:off + CHUNK]
    more = len(text) - off - len(part)
    where = f"，这是第 {off + 1}–{off + len(part)} 字" if off or more else ""
    return (f"--- {label}（共 {len(text)} 字{where}）---\n{part}\n--- 结束 ---"
            + (f"\n还有 {more} 字没给：用 {again.format(offset=off + len(part))} 取下一段。" if more > 0 else "")), more > 0


def _files_block(s: dict, scope: str) -> str:
    files, name = s["files"], s["name"]
    if not files:
        return ""
    lines, budget = [f"\n\n这个技能带了 {len(files)} 个文件（附上每个文件开头 {PREVIEW} 字）："], 5000
    for p, text in files.items():
        if budget > 0:
            head = text[:PREVIEW]
            budget -= len(head)
            lines.append(f"--- {p}（{len(text)} 字）---\n{head}{'……' if len(text) > PREVIEW else ''}")
        else:
            lines.append(f"--- {p}（{len(text)} 字，开头没附上）---")
    lines.append(f"要看某个文件的全文：use_skill(name=\"{name}\", file=\"路径\")。")
    if scope == "router":
        lines.append(f"要运行里面的脚本就派任务（dispatch），goal 里写明按技能 /{name} 做、跑哪个脚本，Worker 会自己取文件。")
    else:
        lines.append("要运行脚本，用 computer_write_file 把文件内容写进工作文件夹再用 run_code 或 computer_shell 跑；"
                     "参考资料看全文就行，不用写进电脑。")
    return "\n".join(lines)


def tool_use(bot_id: int, scope: str, args: dict) -> str:
    """use_skill：没给 file 返回做法（带文件的附上清单和每个文件的开头），给了 file 返回那个文件的全文。太长的分段给。"""
    name = str(args.get("name") or "").strip().lstrip("/")
    s = find(bot_id, name)
    if not s:
        have = " ".join("/" + x["name"] for x in list_for(bot_id))
        return f"没有 /{name} 这个技能。能用的：{have or '无'}"
    if raw := str(args.get("file") or "").strip():
        path = clean_path(raw)
        if path not in s["files"]:
            return f"/{name} 没有 {raw} 这个文件。它带的文件：{'、'.join(s['files']) or '无'}"
        return _piece(s["files"][path], args.get("offset"), path, f'use_skill(name="{name}", file="{path}", offset={{offset}})')[0]
    out = expand(s, by_user=False)
    if len(out) > CHUNK or args.get("offset"):   # 做法很长：分段给，文件清单跟在最后一段后面
        out, more = _piece(s["body"], args.get("offset"), f"技能 /{name} 的做法", f'use_skill(name="{name}", offset={{offset}})')
        return out if more else out + _files_block(s, scope)
    return out + _files_block(s, scope)


def files_hint(skill: dict) -> str:
    """定时任务、事件按技能跑时附在做法后面：技能带了哪些文件，怎么取。"""
    if not skill.get("files"):
        return ""
    return (f"\n这个技能带了文件：{'、'.join(skill['files'])}。要看全文用 use_skill(name=\"{skill['name']}\", file=\"路径\")；"
            "要运行脚本，用 computer_write_file 把文件内容写进工作文件夹再用 run_code 或 computer_shell 跑。")


# ---------- 导出、导入、分享 ----------
_APP_REF = re.compile(r"(?<![\w@.])@([a-z][a-z0-9_-]{0,23})\b")


def apps_in(body: str) -> list[str]:
    """做法里 @ 到的应用（应用市场里有的，或者本机装了的）。"""
    from . import catalog, mcp_hub
    known = set(catalog.items()) | set(mcp_hub.all_apps())
    return sorted({m for m in _APP_REF.findall(body or "") if m in known})


def apps_of(skill: dict) -> list[str]:
    """技能要用的应用：frontmatter 里写了 apps 就以它为准（写空列表就是不用应用），没写才按做法里的 @名字 找。"""
    return list(skill["apps"]) if skill.get("apps") is not None else apps_in(skill.get("body", ""))


def to_file(skill: dict) -> str:
    head = {"name": skill["name"], "description": skill["description"] or ""}
    if (apps := apps_of(skill)) or skill.get("apps") is not None:
        head["apps"] = apps
    meta = yaml.safe_dump(head, allow_unicode=True, sort_keys=False, width=1000).strip()
    return f"---\n{meta}\n---\n\n{skill['body'].strip()}\n"


def to_zip(skill: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("SKILL.md", to_file(skill))
        for path, text in (skill.get("files") or {}).items():
            z.writestr(path, text)
    return buf.getvalue()


def export(skill: dict) -> tuple[bytes, str, str]:
    """导出成文件：(内容, 文件名, 类型)。带文件的是 zip，不带的是 .md。"""
    if skill.get("files"):
        return to_zip(skill), skill["name"] + ".zip", "application/zip"
    return to_file(skill).encode(), skill["name"] + ".md", "text/markdown; charset=utf-8"


def _clean_name(name: str) -> str:
    """别处的技能名可能有空格、点：换成 -，去掉不能用的字。"""
    name = re.sub(r"[\s.]+", "-", str(name or "").strip().lstrip("/"))
    return re.sub(r"[^\w一-鿿-]", "", name).strip("-_")[:40]


def from_file(text: str, filename: str = "") -> dict:
    """解析一个技能文件，返回 {name, description, body, apps, files}；格式不对就抛 ValueError。
    apps 是 frontmatter 里声明的应用，没写是 None。"""
    text = (text or "").lstrip("﻿").replace("\r\n", "\n")
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
        name = re.sub(r"(\.skill)?\.(md|markdown|txt|zip)$", "", filename.rsplit("/", 1)[-1], flags=re.I)
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
    return {"name": name, "description": " ".join(desc.split())[:200], "body": body,
            "apps": clean_apps(head["apps"]) if head.get("apps") is not None else None, "files": {}}


def _junk(path: str) -> bool:
    """打包时顺带进去的：macOS 的 __MACOSX、.DS_Store，.git 这类以 . 开头的。"""
    return any(x.startswith(".") or x in ("__MACOSX", "Thumbs.db", "__pycache__") for x in path.split("/"))


def _package(entries: dict[str, bytes], hint: str = "") -> dict:
    """一个技能包（相对路径 → 内容）→ from_file 的结果，加上 files（带的文字文件）和 skipped（没收的：不是文字、路径不对）。
    SKILL.md 在根上，或者在唯一的一层文件夹里（别处打的 zip 常这样，名字用这个文件夹名）。英文界面有 SKILL.en.md 就用它。"""
    entries = {p.replace("\\", "/").lstrip("/"): v for p, v in entries.items()}
    entries = {p: v for p, v in entries.items() if p and not p.endswith("/") and not _junk(p)}
    root = ""
    if not any(p.lower() == "skill.md" for p in entries):
        tops = {p.split("/", 1)[0] for p in entries}
        top = next(iter(tops)) if len(tops) == 1 else ""
        if not top or not any(p.lower() == f"{top}/skill.md".lower() for p in entries):
            raise ValueError(i18n.t("包里没有 SKILL.md：技能包的根上要有一个 SKILL.md"))
        root, hint = top + "/", top
    rel = {p[len(root):]: v for p, v in entries.items()}
    mds = {p.lower(): p for p in rel if _SKILL_MD.match(p)}
    md = mds["skill.en.md"] if i18n.lang() == "en" and "skill.en.md" in mds else mds["skill.md"]
    try:
        text = rel[md].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(i18n.t("SKILL.md 不是 UTF-8 文字"))
    d = from_file(text, hint)
    files, skipped = {}, []
    for p, data in sorted(rel.items()):
        if p in mds.values():
            continue
        path = clean_path(p)
        try:
            content = data.decode("utf-8").lstrip("﻿")
        except UnicodeDecodeError:
            content = None
        if not path or content is None or "\x00" in content:
            skipped.append(p)
            continue
        files[path] = content
    d["files"] = check_files(files)
    d["skipped"] = skipped
    return d


def from_zip(data: bytes, filename: str = "") -> dict:
    """解析一个 zip 技能包。"""
    if len(data) > MAX_ZIP:
        raise ValueError(i18n.t("压缩包太大了，最多 {n} MB", n=MAX_ZIP // 1_000_000))
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = [i for i in z.infolist() if not i.is_dir() and not _junk(i.filename)]
        if len(infos) > MAX_FILES * 2:
            raise ValueError(i18n.t("压缩包里的文件太多了，不像是一个技能"))
        entries, total = {}, 0
        for i in infos:
            total += i.file_size
            if total > MAX_ZIP * 4:   # 解开大得离谱：压缩炸弹
                raise ValueError(i18n.t("压缩包解开太大了，不像是一个技能"))
            # 一看就不是文字的、太大的不解开，内容记成 b"\x00"（_package 会把它列进 skipped）；SKILL.md 总要读
            skip = not _SKILL_MD.match(i.filename.rsplit("/", 1)[-1]) and (_BINARY.search(i.filename) or i.file_size > MAX_FILE)
            entries[i.filename] = b"\x00" if skip else z.read(i)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as e:
        raise ValueError(i18n.t("不是能打开的 zip 文件（{e}）", e=type(e).__name__))
    return _package(entries, re.sub(r"\.zip$", "", filename.rsplit("/", 1)[-1], flags=re.I))


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


def github_dir(url: str) -> tuple[str, str, str, str] | None:
    """GitHub 上的一个技能文件夹（文件夹的链接，或者文件夹里 SKILL.md 的链接）→ (owner, repo, ref, 文件夹路径)；别的链接返回 None。"""
    u = urlparse(url.strip())
    parts = [x for x in u.path.split("/") if x]
    if u.netloc != "github.com" or len(parts) < 5 or parts[2] not in ("blob", "tree"):
        return None
    path = parts[4:]
    if re.search(r"\.(md|markdown|txt)$", path[-1], re.I):
        if path[-1].upper() != "SKILL.MD":
            return None   # 单个 Markdown 文件，不是技能包
        path = path[:-1]
    return parts[0], parts[1], parts[3], "/".join(path)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, follow_redirects=True)


async def _get(c: httpx.AsyncClient, url: str, limit: int, accept: str = "*/*") -> bytes:
    async with c.stream("GET", url, headers={"accept": accept}) as r:
        if r.status_code == 404:
            raise ValueError(i18n.t("链接打不开（404）：分享可能已经取消了，或者地址不对"))
        if r.status_code >= 400:
            raise ValueError(i18n.t("链接打不开（{code}）", code=r.status_code))
        if "text/html" in r.headers.get("content-type", ""):
            raise ValueError(i18n.t("这个链接打开是一个网页，不是技能文件。GitHub 上的请用文件或文件夹的链接"))
        data = b""
        async for chunk in r.aiter_bytes():
            data += chunk
            if len(data) > limit:
                raise ValueError(i18n.t("文件太大了，不像是一个技能"))
    return data


async def _github_files(c: httpx.AsyncClient, owner: str, repo: str, ref: str, folder: str) -> dict[str, bytes]:
    """用 GitHub 的 contents 接口列出文件夹里的文件（往下最多 3 层），逐个取 raw。
    一看就不是文字的、太大的不取，内容记成 b"\\x00"（_package 会列进 skipped）。"""
    found: list[tuple[str, dict]] = []

    async def walk(path: str, rel: str, depth: int):
        r = await c.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path)}", params={"ref": ref},
                        headers={"accept": "application/vnd.github+json"})
        if r.status_code in (403, 429):
            raise ValueError(i18n.t("GitHub 限制了没登录时的访问次数，过一会儿再试"))
        if r.status_code >= 400:
            raise ValueError(i18n.t("列不出 GitHub 上这个文件夹里的文件（{code}）", code=r.status_code))
        items = r.json()
        for it in items if isinstance(items, list) else []:
            sub = rel + str(it.get("name") or "")
            if _junk(sub):
                continue
            if it.get("type") == "dir" and depth < 3:
                await walk(it.get("path") or f"{path}/{it.get('name')}", sub + "/", depth + 1)
            elif it.get("type") == "file":
                found.append((sub, it))
            if len(found) > MAX_FILES * 2:
                raise ValueError(i18n.t("这个文件夹里的文件太多了，不像是一个技能"))
    await walk(folder, "", 0)

    async def one(sub: str, it: dict) -> bytes:
        if not it.get("download_url") or not _SKILL_MD.match(sub) and (_BINARY.search(sub) or (it.get("size") or 0) > MAX_FILE):
            return b"\x00"
        return await _get(c, it["download_url"], MAX_FILE)
    data = await asyncio.gather(*[one(sub, it) for sub, it in found])
    return {sub: d for (sub, _), d in zip(found, data)}


async def fetch(url: str) -> dict:
    """从链接取一个技能（.md 或 zip；GitHub 上的文件夹连里面的文件一起取）并解析。"""
    src, name = raw_url(url)
    gh = github_dir(url)
    warning = ""
    try:
        async with _client() as c:
            if gh:
                try:
                    entries = await _github_files(c, *gh)
                except ValueError as e:   # 列不出文件夹（访问次数用完之类）：至少把 SKILL.md 取回来，说一声
                    warning = i18n.t("只取到了 SKILL.md，文件夹里的其他文件没取到（{e}）", e=e)
                else:
                    return _package(entries, name)
            data = await _get(c, src, MAX_ZIP, "text/markdown, text/plain;q=0.9, */*;q=0.5")
    except httpx.HTTPError as e:
        raise ValueError(i18n.t("取不到这个链接（{e}）", e=type(e).__name__))
    if data[:4] == b"PK\x03\x04":
        return from_zip(data, name or src.rsplit("/", 1)[-1])
    if len(data) > MAX_FILE:
        raise ValueError(i18n.t("文件太大了，不像是一个技能"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(i18n.t("不是文字文件"))
    return from_file(text, name) | {"skipped": []} | ({"warning": warning} if warning else {})


# ---------- 技能市场 ----------
def _read_dir(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def market_skill(mid: str) -> dict:
    """技能市场里的一个（按当前界面语言），格式和导入的一样。"""
    if not MARKET_ID.match(mid or "") or not (MARKET / mid / "SKILL.md").is_file():
        raise ValueError(i18n.t("技能市场里没有这个技能"))
    return _package(_read_dir(MARKET / mid), mid)


def market(bot_id: int | None = None) -> list[dict]:
    """技能市场的清单。给了 bot_id 就标上这个 Bot 是不是已经有同名的技能。"""
    out = []
    for d in sorted(MARKET.iterdir()) if MARKET.is_dir() else []:
        if not (d / "SKILL.md").is_file():
            continue
        try:
            s = market_skill(d.name)
        except ValueError as e:
            log.warning("技能市场的 %s 有问题，跳过：%s", d.name, e)
            continue
        have = find(bot_id, s["name"]) if bot_id else None
        out.append({"id": d.name, "name": s["name"], "description": s["description"], "body": s["body"],
                    "files": list(s["files"]), "apps": apps_of(s), "installed": bool(have)})
    return out


def install(bot_id: int | None, mid: str) -> dict:
    """从技能市场装：复制进 skills 表（source=market）。这个 Bot 已经有同名的就替换掉。"""
    s = market_skill(mid)
    return save(bot_id, s["name"], s["body"], s["description"], "market", files=s["files"], apps=s["apps"])
