"""文件夹里有新文件：下载、截图、发票、共享文件夹……

只在能从网页上让这台机器执行命令的地方能用（security.local_exec）：开了服务器锁的服务器不能让网页上的人
去翻服务器上的文件夹。
每次把文件夹看一遍，记下每个文件的大小和修改时间（state 里最多 MAX_FILES 个，多了只记最新的）。
新文件要连着两次看到的大小、修改时间都一样才报：下载、拷贝到一半的文件不报。隐藏文件和浏览器没下完的
（.crdownload、.part……）不看。
"""
import asyncio
import fnmatch
import mimetypes
import os
import re
import time
from .. import i18n, security
from ..i18n import N_
from ..registry import POLL, Source

MAX_FILES = 5000         # state 里最多记多少个文件
MAX_SCAN = 50_000        # 一次最多看多少个条目，免得指到整个硬盘
MAX_REPORT = 30          # 一次最多报多少个文件，剩下的合成一句
PARTIAL = (".crdownload", ".part", ".partial", ".download", ".downloading", ".tmp", ".temp", ".opdownload",
           ".td", ".td.cfg", ".aria2", ".!qb", ".xltd", ".bc!", ".filepart")


def unavailable() -> str:
    if not security.local_exec():
        return i18n.t("这台服务器开了服务器锁，不能看服务器上的文件夹（config.yaml 的 security.local_exec）")
    return ""


def _patterns(p: dict) -> list[str]:
    return [x.strip() for x in re.split(r"[,，;；\n]+", p.get("pattern") or "*") if x.strip()] or ["*"]


def _skip(name: str) -> bool:
    low = name.lower()
    return name.startswith((".", "~$")) or low.endswith(PARTIAL) or low in ("thumbs.db", "desktop.ini")


def scan(root: str, patterns: list[str], recursive: bool) -> dict[str, list]:
    """{相对路径: [大小, 修改时间]}，只要符合 pattern 的文件；超过 MAX_FILES 个只留最新的。"""
    found, seen, stack = {}, 0, [""]
    pats = [x.lower() for x in patterns]
    while stack and seen < MAX_SCAN:
        rel = stack.pop()
        try:
            it = os.scandir(os.path.join(root, rel) if rel else root)
        except PermissionError:
            if not rel:
                raise ValueError(f"没有权限看这个文件夹：{root}。macOS 上要在“系统设置 → 隐私与安全性 → 文件与文件夹”里允许 osa") from None
            continue
        except OSError:
            continue
        with it:
            for e in it:
                seen += 1
                if seen > MAX_SCAN:
                    break
                if _skip(e.name):
                    continue
                sub = f"{rel}/{e.name}" if rel else e.name
                try:
                    if e.is_dir(follow_symlinks=False):
                        if recursive:
                            stack.append(sub)
                        continue
                    if not e.is_file():
                        continue
                    if not any(fnmatch.fnmatchcase(e.name.lower(), p) for p in pats):
                        continue
                    s = e.stat()
                except OSError:
                    continue
                found[sub] = [s.st_size, round(s.st_mtime, 3)]
    if len(found) > MAX_FILES:
        found = dict(sorted(found.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_FILES])
    return found


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _event(root: str, rel: str, info: list, changed: bool) -> dict:
    path = os.path.join(root, rel)
    kind = mimetypes.guess_type(rel)[0] or ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info[1]))
    return {"id": f"{rel}-{info[1]}",
            "title": f"{'文件改了' if changed else '新文件'}：{rel}",
            "text": "\n".join(x for x in (f"位置：{path}", f"大小：{_size(info[0])}", f"修改时间：{when}",
                                          f"类型：{kind}" if kind else "") if x)}


def compare(root: str, cur: dict, state: dict | None, modified: bool) -> tuple[list, dict]:
    """这次看到的 cur 和上次的 state 比：新文件（和改了的）先放进 pending，下一次还是一样大才报。"""
    if state is None:
        # 第一次只记下现状；最新的一个给“测试”当示例（引擎第一次不报）
        newest = max(cur.items(), key=lambda kv: kv[1][1], default=None)
        return ([_event(root, *newest, False)] if newest else []), {"files": cur, "pending": {}}
    files, pending = dict(state.get("files") or {}), dict(state.get("pending") or {})
    report, nxt = [], {}
    for rel, info in cur.items():
        old = files.get(rel)
        if old == info:
            continue
        if old is not None and not modified:
            files[rel] = info            # 改了但不用报：悄悄记下
            continue
        if pending.get(rel) == info:     # 连着两次一样：写完了
            report.append((rel, info, old is not None))
            files[rel] = info
        else:
            nxt[rel] = info
    for rel in list(files):
        if rel not in cur:
            del files[rel]
    if len(files) > MAX_FILES:
        files = dict(sorted(files.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_FILES])
    report.sort(key=lambda x: x[1][1])
    events = [_event(root, rel, info, changed) for rel, info, changed in report[:MAX_REPORT]]
    if len(report) > MAX_REPORT:
        rest = report[MAX_REPORT:]
        events.append({"id": f"more-{rest[-1][1][1]}", "title": f"另外还有 {len(rest)} 个文件",
                       "text": "\n".join(rel for rel, _, _ in rest[:200]) + ("\n……" if len(rest) > 200 else "")})
    return events, {"files": files, "pending": nxt}


def _root(p: dict) -> str:
    return os.path.abspath(os.path.expanduser((p.get("path") or "").strip()))


async def check(params: dict, state):
    if why := unavailable():
        raise ValueError(why)
    root = _root(params)
    if not os.path.isdir(root):
        raise ValueError(f"文件夹不存在了：{root}")
    cur = await asyncio.to_thread(scan, root, _patterns(params), bool(params.get("recursive")))
    return compare(root, cur, state, bool(params.get("modified")))


def validate(p: dict) -> dict:
    p["pattern"] = p.get("pattern") or "*"
    root = _root(p)
    if not os.path.isdir(root):
        raise ValueError(f"文件夹不存在：{root}")
    return p


def summary(p: dict) -> str:
    pat = p.get("pattern") or "*"
    return (p.get("path") or "") + (f"（{pat}）" if pat != "*" else "")


FOLDER = Source(
    id="folder", mode=POLL, category=N_("系统"), icon="📁",
    title=N_("文件夹有新文件"),
    description=N_("这台电脑上的某个文件夹里出现新文件时（下载完、截了图、有人放进共享文件夹），Bot 看一眼、整理或归档。没下载完的文件不会报。"),
    fields=[
        {"key": "path", "label": N_("文件夹"), "type": "text", "required": True, "placeholder": N_("~/Downloads"),
         "help": N_("这台电脑（运行 osa 的那台）上的文件夹，~ 是你的主目录")},
        {"key": "pattern", "label": N_("文件名"), "type": "text", "default": "*", "placeholder": N_("*.pdf, *.png"),
         "help": N_("只看名字符合的文件，* 是任意文字；多个用逗号分开。不填就是所有文件")},
        {"key": "recursive", "label": N_("也看子文件夹"), "type": "bool", "default": False},
        {"key": "modified", "label": N_("文件改了也提醒"), "type": "bool", "default": False,
         "help": N_("不勾就只报新出现的文件")},
    ],
    check=check, every=5, min_every=1, available=unavailable, validate=validate, summary=summary,
    sample={"title": "新文件：发票-2026-11.pdf",
            "text": "位置：/Users/me/Downloads/发票-2026-11.pdf\n大小：182.4 KB\n修改时间：2026-11-09 10:21\n类型：application/pdf"},
    scenarios=[
        {"title": N_("下载文件夹有新文件时整理"), "description": N_("下载完的文件按类型挪到合适的地方"),
         "name": N_("整理下载"), "params": {"path": "~/Downloads"},
         "prompt": N_("下载文件夹里有新文件。看看是什么，按类型挪到合适的文件夹（文档、图片、安装包、压缩包……），文件名乱的顺手改清楚；拿不准的别动，告诉我一声。")},
        {"title": N_("新发票 / PDF 自动归档"), "description": N_("新的 PDF 是发票的话，记下金额、改好名字放进发票文件夹"),
         "name": N_("发票归档"), "params": {"path": "~/Downloads", "pattern": "*.pdf"},
         "prompt": N_("有新的 PDF。打开看看是不是发票：是的话记下开票方、金额、日期，改名成“日期-开票方-金额.pdf”，挪到 ~/Documents/发票/年-月/ 里，告诉我一句；不是发票的写 [无新内容]。")},
        {"title": N_("截图文件夹有新截图时识别内容"), "description": N_("截了图，Bot 看看图里是什么"),
         "name": N_("截图识别"), "params": {"path": "~/Desktop", "pattern": "截屏*.png, Screenshot*.png"},
         "prompt": N_("我刚截了一张图。看看图里是什么，用一两句话说清楚；是报错的告诉我怎么解决，是文字多的把要点摘出来。")},
        {"title": N_("共享文件夹有人放了新文件"), "description": N_("团队共享、网盘同步的文件夹里来了新文件"),
         "name": N_("共享文件夹"), "params": {"path": "~/Dropbox", "recursive": True},
         "prompt": N_("共享文件夹里有新文件。告诉我是什么文件、放在哪；能打开的用两三句话说说内容，需要我处理的放在最前面。")},
    ])

SOURCES = [FOLDER]
