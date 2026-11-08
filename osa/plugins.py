"""插件：放在 data/plugins 里的 Python 代码，在服务进程里运行，用 registry.Context 注册工具、订阅事件。

一个插件是 data/plugins/<名字>.py，或者一个文件夹 data/plugins/<名字>/__init__.py。里面要有：
  META = {"title": "…", "description": "…", "version": "1.0", "config": {默认设置}}   # 可选，只能写字面量
  def apply(ctx): …            # 必需，也可以是 async def；用 ctx.tool / ctx.simple / ctx.on / ctx.task / ctx.effect 注册
写法见 docs/plugins.md。

插件和服务是同一个进程，能做服务能做的一切，所以：
- **列出插件不执行代码**：名字、说明、版本用 ast 从源码里读（META 只认字面量），apply 有没有也是看语法树。
- **启用时记下文件的 sha256，文件改了就不加载**，要用户看过改动再启用一次。Bot 或别的程序往 data/plugins 里写东西，
  不经用户在设置里点“启用”就跑不起来；Bot 也没有启用插件的工具。
- 服务器锁（security.local_exec: false）和演示服务器上整个关掉：不加载、不能启用。
- 卸载就是 ctx.dispose()：注册过的工具、监听器、后台任务按相反顺序撤销，然后从 sys.modules 里拿掉。
  插件自己开的线程、改过的全局状态管不到，要用 ctx.effect 登记清理。
- 备份不带插件文件（只带启用状态和设置）：换机器后把文件拷过去，再启用一次。

应用市场一样的“插件市场”是 osa/catalog/plugins 里随项目发布、经过审查的插件：点“安装”复制到 data/plugins，仍要用户启用。
不支持从任意网址安装。
"""
import ast
import asyncio
import hashlib
import importlib.util
import inspect
import json
import logging
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from . import i18n, registry, security
from .config import DATA, ROOT
from .memory import conn

DIR = DATA / "plugins"
CATALOG = ROOT / "osa" / "catalog" / "plugins"
NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_SOURCE = 300_000
MAX_CONFIG = 20_000
APPLY_TIMEOUT = 15
log = logging.getLogger(__name__)

conn.execute("""create table if not exists plugins(
  name text primary key, enabled integer default 0, sha256 text, config text, updated real)""")
conn.commit()

_loaded: dict[str, "PluginContext"] = {}
_errors: dict[str, str] = {}


class PluginContext(registry.Context):
    """插件拿到的 ctx：registry.Context 的全部，加上自己的设置、数据目录、日志和读保险库。"""

    def __init__(self, name: str, config: dict):
        super().__init__(f"plugin:{name}")
        self.plugin = name
        self.config = config
        self.data_dir = DIR / f".{name}.data"     # 插件自己放文件的地方（删插件时一起删）
        self.log = logging.getLogger(f"osa.plugin.{name}")

    def secret(self, value):
        """设置里写 "vault:名字" 的值从保险库取（webhook 地址、token 不用明文写在设置里）。"""
        if isinstance(value, str) and value.startswith("vault:"):
            from . import vault
            return vault.get(value[6:])
        return value


# ---------- 文件 ----------
def locked() -> str:
    """这台服务器能不能用插件；不能就返回原因。"""
    if security.demo() or not security.local_exec():
        return i18n.t("这台服务器不允许运行插件（config.yaml 的 security.local_exec）")
    return ""


def _entry(base: Path, name: str) -> Path | None:
    f, pkg = base / f"{name}.py", base / name / "__init__.py"
    return f if f.is_file() else pkg if pkg.is_file() else None


def _names(base: Path) -> list[str]:
    if not base.is_dir():
        return []
    out = {p.stem for p in base.glob("*.py")} | {p.parent.name for p in base.glob("*/__init__.py")}
    return sorted(n for n in out if NAME.match(n))


def _files(entry: Path) -> list[Path]:
    """插件的全部源码文件：单文件就是它，文件夹是里面所有的 .py。"""
    if entry.name != "__init__.py":
        return [entry]
    return sorted(p for p in entry.parent.rglob("*.py") if "__pycache__" not in p.parts)


def digest(entry: Path) -> str:
    h = hashlib.sha256()
    for p in _files(entry):
        h.update(str(p.relative_to(entry.parent)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def source(entry: Path) -> str:
    """给用户看的源码：文件夹插件把每个文件拼起来，前面标上文件名。"""
    files = _files(entry)
    if len(files) == 1:
        return files[0].read_text(errors="replace")
    return "\n\n".join(f"# ===== {p.relative_to(entry.parent)} =====\n{p.read_text(errors='replace')}" for p in files)


def describe(entry: Path) -> dict:
    """不执行代码，从语法树里读 META 和 apply。"""
    out = {"title": "", "description": "", "version": "", "author": "", "config": {}, "has_apply": False, "syntax_error": ""}
    try:
        text = entry.read_text(errors="replace")
        if len(text) > MAX_SOURCE:
            raise ValueError(i18n.t("文件太大"))
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as e:
        out["syntax_error"] = str(e)
        return out
    doc = (ast.get_docstring(tree) or "").strip()
    out["description"] = doc.split("\n\n")[0].replace("\n", " ")[:300]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "apply":
            out["has_apply"] = True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "META" for t in node.targets):
            try:
                meta = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(meta, dict):
                for k in ("title", "description", "version", "author"):
                    if isinstance(meta.get(k), str):
                        out[k] = meta[k][:300]
                if isinstance(meta.get("config"), dict):
                    out["config"] = meta["config"]
    t = out["title"]
    if t and out["description"].startswith((t + "：", t + ":")):   # 说明开头重复了标题（“日期计算：……”）
        out["description"] = out["description"][len(t) + 1:].strip()
    return out


# ---------- 状态 ----------
def _row(name: str) -> dict:
    r = conn.execute("select enabled, sha256, config from plugins where name=?", (name,)).fetchone()
    return {"enabled": bool(r[0]), "sha256": r[1], "config": json.loads(r[2]) if r and r[2] else None} if r else \
        {"enabled": False, "sha256": None, "config": None}


def _save(name: str, **fields):
    row = _row(name) | fields
    conn.execute("insert or replace into plugins(name, enabled, sha256, config, updated) values(?,?,?,?,?)",
                 (name, int(row["enabled"]), row["sha256"],
                  json.dumps(row["config"], ensure_ascii=False) if row["config"] is not None else None, time.time()))
    conn.commit()


def config_of(name: str, entry: Path | None = None) -> dict:
    row = _row(name)
    if row["config"] is not None:
        return row["config"]
    entry = entry or _entry(DIR, name)
    return dict(describe(entry)["config"]) if entry else {}


def info(name: str) -> dict:
    entry = _entry(DIR, name)
    row = _row(name)
    meta = describe(entry) if entry else {}
    sha = digest(entry) if entry else None
    ctx = _loaded.get(name)
    tools = sorted(t.name for t in registry.TOOLS.values() if t.owner == f"plugin:{name}")
    cat = _entry(CATALOG, name)
    return {"name": name, **{k: meta.get(k) for k in ("title", "description", "version", "author")},
            "kind": "folder" if entry and entry.name == "__init__.py" else "file",
            "enabled": row["enabled"], "loaded": ctx is not None,
            "changed": bool(row["enabled"] and row["sha256"] and sha != row["sha256"]),
            "error": _errors.get(name) or meta.get("syntax_error") or ("" if not meta or meta.get("has_apply") else i18n.t("没有 apply(ctx) 函数")),
            "tools": tools, "config": config_of(name, entry), "default_config": meta.get("config") or {},
            "from_catalog": bool(cat), "update": bool(cat and entry and digest(cat) != sha)}


def listing() -> dict:
    installed = _names(DIR)
    catalog = []
    for n in _names(CATALOG):
        meta = describe(_entry(CATALOG, n))
        catalog.append({"name": n, **{k: meta[k] for k in ("title", "description", "version", "author")}, "installed": n in installed})
    return {"locked": locked(), "dir": str(DIR), "plugins": [info(n) for n in installed], "catalog": catalog}


# ---------- 加载和卸载 ----------
def _modname(name: str) -> str:
    return f"osa_plugin_{name}"


async def load(name: str):
    """按启用时记下的 sha256 加载；文件改过、出错都不加载，原因记在 _errors 里。"""
    if why := locked():
        raise PermissionError(why)
    entry = _entry(DIR, name)
    if not entry:
        raise LookupError(i18n.t("没有插件 {name}", name=name))
    row = _row(name)
    if not row["enabled"] or not row["sha256"]:
        raise PermissionError(i18n.t("插件 {name} 还没启用", name=name))
    if digest(entry) != row["sha256"]:
        raise PermissionError(i18n.t("插件 {name} 的文件在启用之后改过了：先看一下改了什么，再重新启用", name=name))
    unload(name)
    _errors.pop(name, None)
    ctx = PluginContext(name, config_of(name, entry))
    mod = _modname(name)
    try:
        spec = importlib.util.spec_from_file_location(
            mod, entry, submodule_search_locations=[str(entry.parent)] if entry.name == "__init__.py" else None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod] = module
        spec.loader.exec_module(module)
        apply = getattr(module, "apply", None)
        if not callable(apply):
            raise ValueError(i18n.t("没有 apply(ctx) 函数"))
        out = apply(ctx)
        if inspect.isawaitable(out):
            await asyncio.wait_for(out, APPLY_TIMEOUT)
    except BaseException as e:
        ctx.dispose()
        _forget_module(name)
        if isinstance(e, asyncio.CancelledError):
            raise
        tb = traceback.format_exception(e)
        _errors[name] = "".join(tb[-3:]).strip()[-1500:]
        log.warning("插件 %s 加载失败：%s", name, e)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise RuntimeError(_errors[name]) from None
        raise RuntimeError(i18n.t("插件 {name} 加载失败：{error}", name=name, error=f"{type(e).__name__}: {e}")) from None
    _loaded[name] = ctx


def _forget_module(name: str):
    mod = _modname(name)
    for k in [k for k in sys.modules if k == mod or k.startswith(mod + ".")]:
        del sys.modules[k]


def unload(name: str):
    if ctx := _loaded.pop(name, None):
        ctx.dispose()
    _forget_module(name)


async def _changed():
    await registry.emit("plugins_changed")


async def enable(name: str):
    """用户在设置里点了启用：记下现在的文件指纹，加载。"""
    if why := locked():
        raise PermissionError(why)
    entry = _entry(DIR, name)
    if not entry:
        raise LookupError(i18n.t("没有插件 {name}", name=name))
    _save(name, enabled=True, sha256=digest(entry))
    try:
        await load(name)
    finally:
        await _changed()


async def disable(name: str):
    unload(name)
    _errors.pop(name, None)
    _save(name, enabled=False)
    await _changed()


async def reload(name: str):
    """改了设置、或者想让插件重新跑一遍 apply。文件改过的不行，要重新启用。"""
    try:
        await load(name)
    finally:
        await _changed()


async def set_config(name: str, config):
    if not isinstance(config, dict):
        raise ValueError(i18n.t("设置要是一个 JSON 对象"))
    if len(json.dumps(config, ensure_ascii=False)) > MAX_CONFIG:
        raise ValueError(i18n.t("设置太大（最多 {n}K 字）", n=MAX_CONFIG // 1000))
    if not _entry(DIR, name):
        raise LookupError(i18n.t("没有插件 {name}", name=name))
    _save(name, config=config)
    if _row(name)["enabled"]:   # 启用着的按新设置重新加载（之前因为没填设置加载失败的，这时也会再试一次）
        try:
            await load(name)
        except Exception:
            pass   # 原因记在 _errors 里，设置页上看得到
    await _changed()


async def install(name: str):
    """从插件市场（osa/catalog/plugins）复制到 data/plugins。已经启用的会先停掉：新文件要用户看过再启用。"""
    if why := locked():
        raise PermissionError(why)
    src = _entry(CATALOG, name) if NAME.match(name or "") else None
    if not src:
        raise LookupError(i18n.t("插件市场里没有 {name}", name=name))
    unload(name)
    _remove_files(name)
    DIR.mkdir(parents=True, exist_ok=True)
    if src.name == "__init__.py":
        shutil.copytree(src.parent, DIR / name, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        shutil.copy2(src, DIR / f"{name}.py")
    if _row(name)["enabled"]:
        _save(name, enabled=False)
    await _changed()


def _remove_files(name: str):
    for p in (DIR / f"{name}.py", DIR / name, DIR / f".{name}.data"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


async def remove(name: str):
    if not NAME.match(name or ""):
        raise LookupError(i18n.t("没有插件 {name}", name=name))
    unload(name)
    _errors.pop(name, None)
    _remove_files(name)
    conn.execute("delete from plugins where name=?", (name,))
    conn.commit()
    await _changed()


def read_source(name: str, catalog: bool = False) -> str:
    entry = _entry(CATALOG if catalog else DIR, name) if NAME.match(name or "") else None
    if not entry:
        raise LookupError(i18n.t("没有插件 {name}", name=name))
    return source(entry)


async def start():
    """服务启动：加载启用了的插件。一个出错不影响别的。"""
    if locked():
        return
    for (name,) in conn.execute("select name from plugins where enabled=1").fetchall():
        try:
            await load(name)
        except Exception as e:
            _errors.setdefault(name, str(e))
            log.warning("插件 %s 没加载：%s", name, e)


async def stop():
    for name in list(_loaded):
        unload(name)
