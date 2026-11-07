"""对话里的图片和附件：用户发的、Bot 发的（send_file）、Bot 画的。

- 图片存在数据库的 media 表里，备份时跟着走。id 是内容的哈希，同一张图只存一份。只收 PNG / JPEG / WebP / GIF。
  浏览器先把大图缩到最长边 1600 像素左右再传；聊天软件发来的图片原样存（它们自己已经压过）。
- 用 /api/media/<id> 看，要登录（和头像不一样：对话里的图片是私人的）。内容不会变，可以长缓存。
- 附件记在消息的 meta.attachments 里：[{name, size, path?, media?, mime?, w?, h?}]。path 是电脑里的文件
  （content 里照旧写一行“[附件] 路径”，Router 和任务靠它找文件），media 是存在这里的图片（显示缩略图、给能看图的模型看）。
- 没有消息引用的图片放一天后清掉：刚传上来、还没发出去的不能删。删对话、删 Bot 时也清一次。
"""
import asyncio
import base64
import hashlib
import json
import posixpath
import re
import time
from . import i18n, library
from .config import CFG
from .memory import conn

conn.execute("""create table if not exists media(
  id text primary key, mime text, name text default '', size integer, w integer default 0, h integer default 0,
  data blob, created real)""")
conn.commit()

MAX_BYTES = 10 << 20
MAX_ATTACHMENTS = 10
KEEP_S = 86400
ID = re.compile(r"^[0-9a-f]{24}$")
# 给模型看的图片：最近几张，更早的只留“[附件] 路径”那一行。GIF 很多模型不收，不给
MODEL_IMAGES = 4
MODEL_MIMES = ("image/png", "image/jpeg", "image/webp")


def sniff(b: bytes) -> str | None:
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def dimensions(b: bytes, mime: str) -> tuple[int, int]:
    """从文件头里读宽高，读不出来是 (0, 0)。"""
    try:
        if mime == "image/png":
            return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
        if mime == "image/gif":
            return int.from_bytes(b[6:8], "little"), int.from_bytes(b[8:10], "little")
        if mime == "image/webp":
            kind = b[12:16]
            if kind == b"VP8 ":
                return int.from_bytes(b[26:28], "little") & 0x3FFF, int.from_bytes(b[28:30], "little") & 0x3FFF
            if kind == b"VP8L":
                w = 1 + (((b[22] & 0x3F) << 8) | b[21])
                h = 1 + (((b[24] & 0x0F) << 10) | (b[23] << 2) | ((b[22] & 0xC0) >> 6))
                return w, h
            if kind == b"VP8X":
                return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(b):
                if b[i] != 0xFF:
                    break
                marker = b[i + 1]
                if marker == 0xFF:   # 填充字节
                    i += 1
                    continue
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
                i += 2 + int.from_bytes(b[i + 2:i + 4], "big")
    except IndexError:
        pass
    return 0, 0


def _clean_name(name: str, mime: str) -> str:
    name = posixpath.basename((name or "").replace("\\", "/")).strip().replace("\0", "")[:200]
    return name or "image." + mime.split("/")[1].replace("jpeg", "jpg")


def save(data: bytes, name: str = "", limit: int = MAX_BYTES) -> dict:
    """存一张图片，返回它的信息（id、mime、name、size、w、h）。不是支持的图片格式就报错。"""
    if not data:
        raise ValueError(i18n.t("没收到图片"))
    if len(data) > limit:
        raise ValueError(i18n.t("图片太大了（最多 {n} MB）", n=limit >> 20))
    mime = sniff(data)
    if not mime:
        raise ValueError(i18n.t("只支持 PNG、JPG、WebP、GIF 图片"))
    mid = hashlib.sha256(data).hexdigest()[:24]
    w, h = dimensions(data, mime)
    name = _clean_name(name, mime)
    # 已经有的同一张图：刷新时间，免得刚发出去就被当成没人用的清掉
    conn.execute("""insert into media(id, mime, name, size, w, h, data, created) values(?,?,?,?,?,?,?,?)
        on conflict(id) do update set created=excluded.created""", (mid, mime, name, len(data), w, h, data, time.time()))
    conn.commit()
    return {"id": mid, "mime": mime, "name": name, "size": len(data), "w": w, "h": h}


def get(mid) -> tuple[str, bytes, str] | None:
    """(mime, 内容, 文件名)。"""
    if not isinstance(mid, str) or not ID.match(mid):
        return None
    r = conn.execute("select mime, data, name from media where id=?", (mid,)).fetchone()
    return (r[0], r[1], r[2]) if r else None


def info(mid) -> dict | None:
    if not isinstance(mid, str) or not ID.match(mid):
        return None
    r = conn.execute("select mime, name, size, w, h from media where id=?", (mid,)).fetchone()
    return {"id": mid, "mime": r[0], "name": r[1], "size": r[2], "w": r[3], "h": r[4]} if r else None


def url(mid: str) -> str:
    return f"/api/media/{mid}"


def used() -> set[str]:
    """消息里引用着的图片。"""
    return {r[0] for r in conn.execute("""select distinct json_extract(a.value, '$.media')
        from messages m, json_each(m.meta, '$.attachments') a
        where m.meta like '%"attachments"%' and json_extract(a.value, '$.media') is not null""")}


def gc():
    """清掉一天前存的、现在没有消息在用的图片。"""
    keep = used()
    old = [r[0] for r in conn.execute("select id from media where created < ?", (time.time() - KEEP_S,)) if r[0] not in keep]
    if old:
        conn.executemany("delete from media where id=?", [(x,) for x in old])
        conn.commit()


async def gc_loop():
    while True:
        try:
            gc()
        except Exception as e:
            print(f"清理对话图片出错：{e}", flush=True)
        await asyncio.sleep(86400)


# ---------- 消息里的附件 ----------
def clean_attachments(items) -> list[dict]:
    """网页、聊天软件发来的附件列表：只留认识的字段，图片按 media 表补上 mime 和宽高。
    path 要在电脑的工作文件夹里；两样都没有的丢掉。"""
    out = []
    for a in (items if isinstance(items, list) else [])[:MAX_ATTACHMENTS]:
        if not isinstance(a, dict):
            continue
        item: dict = {"name": str(a.get("name") or "")[:200]}
        if isinstance(a.get("path"), str) and a["path"].strip():
            try:
                item["path"] = library.norm(a["path"])
            except ValueError:
                pass
        if (m := info(a.get("media"))):
            item |= {"media": m["id"], "mime": m["mime"], "w": m["w"], "h": m["h"], "size": m["size"]}
            item["name"] = item["name"] or m["name"]
        if "path" not in item and "media" not in item:
            continue
        if "size" not in item and isinstance(a.get("size"), int) and a["size"] >= 0:
            item["size"] = a["size"]
        item["name"] = item["name"] or posixpath.basename(item.get("path") or "")
        out.append(item)
    return out


def with_marks(text: str, attachments: list[dict]) -> str:
    """消息正文后面接上“[附件] 路径”：存进数据库、不翻译，Router 和任务靠它找文件。"""
    lines = [f"[附件] {a['path']}" for a in attachments if a.get("path")]
    lines = [x for x in lines if x not in text]   # 已经写过的（旧客户端自己拼的）不重复
    return "\n".join(([text] if text else []) + lines)


def link(a: dict) -> str:
    """附件在聊天软件里的链接（站内地址，channels 会补成完整网址）。要登录才能看，没登录会先跳去登录页。"""
    from urllib.parse import quote
    if a.get("media"):
        return f"/media/{a['media']}"
    return f"/files/download?path={quote(a.get('path') or '')}"


# ---------- 给模型看图 ----------
def _vision_tests() -> dict:
    r = conn.execute("select value from settings where key='vision_tests'").fetchone()
    return json.loads(r[0]) if r else {}


def remember_test(provider: str, model: str, seen: str):
    """设置里点“测试”的结果：yes / no 记下来，以后按它判断这个模型能不能看图（unsure 不记）。"""
    if seen not in ("yes", "no"):
        return
    d = _vision_tests() | {f"{provider}/{model}": seen == "yes"}
    conn.execute("insert or replace into settings values('vision_tests', ?)", (json.dumps(d),))
    conn.commit()


def tested(provider: str, model: str) -> bool | None:
    return _vision_tests().get(f"{provider}/{model}")


def can_see(provider: str = "", model: str = "") -> bool:
    """这个模型（默认是对话用的）能不能看图：测过的按测试结果，没测过按 models.dev 的记录，不知道就当不能。"""
    from . import prices
    if not provider:
        r = CFG["roles"]["router"]
        provider, model = r["provider"], r["model"]
    t = tested(provider, model)
    if t is not None:
        return t
    return bool(prices.lookup(provider, model).get("vision"))


def strip_images(msgs: list[dict]) -> bool:
    """把消息里的图片换成一句文字（模型不收图片时用）。有图片被换掉返回 True。"""
    hit = False
    for m in msgs:
        if isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]):
            text = "\n\n".join(p.get("text") or "" for p in m["content"] if p.get("type") == "text")
            m["content"] = text + "\n（附带的图片没发给你：这个模型不收图片）"
            hit = True
    return hit


def image_part(mid: str) -> dict | None:
    got = get(mid)
    if not got or got[0] not in MODEL_MIMES:
        return None
    return {"type": "image_url", "image_url": {"url": f"data:{got[0]};base64," + base64.b64encode(got[1]).decode()}}


# ---------- send_file：把电脑里的文件发进对话 ----------
SEND_FILE = {"type": "function", "function": {
    "name": "send_file",
    "description": "把电脑里的文件发给用户：图片直接显示在对话里，其他文件显示成能点开、下载的卡片。"
                   "截图、画好的图、做好的表格和文档要交给用户时用。不要自己拼下载链接。",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "文件在电脑里的路径（可以写相对工作文件夹的）"}}, "required": ["path"]}}}
MAX_SEND = 5   # 一轮（一个任务）最多发几个


async def from_computer(path: str) -> dict:
    """电脑里的一个文件 → 附件。图片另存一份进 media（电脑关了也能看）。出错时报 ValueError，文字给模型看。"""
    from . import computer
    c = computer.get()
    p = library.norm(path, c.workspace)
    if (await c.status())["state"] != "running":
        await c.start()
    st = (await c.stat_paths([p])).get(p)
    if not st:
        raise ValueError(f"电脑里没有这个文件：{p}")
    if st["dir"]:
        raise ValueError(f"{p} 是文件夹，只能发文件")
    att = {"name": posixpath.basename(p), "path": p, "size": st["size"]}
    if st["size"] <= MAX_BYTES and re.search(r"\.(png|jpe?g|webp|gif)$", p, re.I):
        try:
            data = await c.read_bytes(p, MAX_BYTES)
        except computer.ComputerError:
            data = b""
        if sniff(data):
            m = save(data, att["name"])
            att |= {"media": m["id"], "mime": m["mime"], "w": m["w"], "h": m["h"]}
    return att


def sent_text(att: dict) -> str:
    """send_file 成功后给模型的话。"""
    what = "图片已经显示在对话里" if att.get("media") else "文件卡片已经显示在对话里，用户点开就能看或下载"
    return f"已发给用户：{att['name']}（{what}）。回复里不用再贴链接。"
