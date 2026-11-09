"""服务器锁和演示模式：按这台服务器给谁用，收紧网页上能做的事。只能在服务器上改 config.yaml 的 security，网页上改不了。

登录密码只管“能不能用这个应用”。有些功能等于把这台机器交出去，自己电脑上、桌面 App 里正常，放到公网服务器上，
密码一旦泄露就是服务器的 root：
- 电脑选“本机”、打开本机终端：Bot 的命令直接在这台机器上执行；
- 自定义应用的启动命令，或者改应用的 command / args / env（比如塞个 NODE_OPTIONS）：任意程序在这台机器上跑；
- “文件夹”应用指到工作文件夹以外：能改系统文件；
- 应用配置里引用服务的环境变量（env:名字）：服务自己的密钥（登录密码、模型的 API key）被带到别的地址；
- 插件：在服务进程里跑的 Python 代码。

策略表，三个键分开管（policy()）：
- security.exec: host | sandbox | off —— 命令在哪跑。host：电脑能选“本机”、能开本机终端；sandbox：只在沙箱里跑（Docker / Instabox
  的电脑，或者受限子进程）；off：都不行。
- security.plugins: any | market | off —— 插件。market：只加载和插件市场（osa/catalog/plugins）里随版本发布的文件一模一样的。
- security.apps: any | market —— 本地进程的应用。market：只认应用市场内置清单（osa/catalog/apps）里原样的命令，用户填的值只能进
  模板里留的位置，“文件夹”只能在工作文件夹里；不能新加 env: 引用。config.yaml 的 mcp_servers 里写的应用不受限（能改那个文件的人
  本来就能管这台机器）。检查放在连接应用之前（mcp_hub._transport），恢复备份、直接改数据库进来的配置也挡得住。

没写这三个键时按旧的两个开关推：local_exec: true（默认）→ host / any / any；local_exec: false（服务器锁）→ sandbox / market / market；
demo: true → sandbox / off / market。写了就以写的为准，写错的值按最严的算。

security.demo: true（演示服务器：密码公开给别人试用）另外再关掉：所有本地进程的应用（npx / uvx 会下载、运行第三方代码，fetch 这类
还能访问服务器上只对本机开放的服务）、自定义应用和应用的高级设置、本机或内网的模型服务地址、改用量上限、备份的导出和恢复。
演示服务器上三个键最多到 exec: sandbox、plugins: market、apps: market，写得更宽也不认。
"""
import logging
import os
import re
from pathlib import Path
from . import i18n
from .config import CFG, DATA

log = logging.getLogger(__name__)

WORKSPACE = DATA / "workspace"     # 和 mcp_hub.WORKSPACE、catalog.WORKSPACE 是同一个目录
SLOT = re.compile(r"(\{\w+\})")     # 清单模板里的 {字段名}
ENV_REF = re.compile(r"(^env:|\{env:)")
FS_PACKAGE = "@modelcontextprotocol/server-filesystem"


def _cfg() -> dict:
    return CFG.get("security") or {}


# 每个键能写的值，从严到宽
LEVELS = {"exec": ("off", "sandbox", "host"), "plugins": ("off", "market", "any"), "apps": ("market", "any")}
DEMO_MAX = {"exec": "sandbox", "plugins": "market", "apps": "market"}   # 演示服务器上最宽到这里
_warned: set = set()


def _level(key: str, raw) -> str:
    """config.yaml 里写的值。YAML 会把不带引号的 off 读成 false；写错的按最严的算（记一条警告）。"""
    levels = LEVELS[key]
    v = "off" if raw is False else str(raw).strip().lower()
    if v in levels:
        return v
    if (key, v) not in _warned:
        _warned.add((key, v))
        log.warning("config.yaml 的 security.%s 写的是 %r，只能是 %s；按 %s 处理", key, raw, " / ".join(levels), levels[0])
    return levels[0]


def policy() -> dict:
    """{"exec", "plugins", "apps"} 三个策略。没写的按 local_exec / demo 推，写了的以写的为准。"""
    c = _cfg()
    if c.get("demo"):
        out = {"exec": "sandbox", "plugins": "off", "apps": "market"}
    elif c.get("local_exec", True):
        out = {"exec": "host", "plugins": "any", "apps": "any"}
    else:
        out = {"exec": "sandbox", "plugins": "market", "apps": "market"}
    for k in LEVELS:
        if c.get(k) is not None:
            out[k] = _level(k, c[k])
    if c.get("demo"):
        out = {k: min(v, DEMO_MAX[k], key=LEVELS[k].index) for k, v in out.items()}
    return out


def exec_mode() -> str:
    """命令在哪跑：host（能在这台机器上直接跑）/ sandbox（只在沙箱里）/ off。"""
    return policy()["exec"]


def plugins_mode() -> str:
    """插件：any / market（只认随版本发布的原样插件）/ off。"""
    return policy()["plugins"]


def apps_mode() -> str:
    """本地进程的应用：any / market（只认应用市场里原样的命令）。"""
    return policy()["apps"]


def demo() -> bool:
    return bool(_cfg().get("demo"))


def local_exec() -> bool:
    """网页上能不能让这台机器执行任意命令（security.exec 是 host）。演示服务器一定不能。"""
    return exec_mode() == "host"


def public() -> dict:
    return {"local_exec": local_exec(), "demo": demo(), **policy()}


def no_local_exec() -> str:
    return i18n.t("这台服务器不允许从网页上在服务器本机执行命令（config.yaml 的 security.exec）")


def _market_only() -> str:
    return i18n.t("这台服务器上只能用应用市场里原样的本地应用，启动命令、参数和环境变量都不能改（config.yaml 的 security.apps）")


def _no_stdio_in_demo() -> str:
    return i18n.t("演示服务器上不能用在服务器上运行程序的应用，可以添加远程连接的应用")


# ---------- 应用 ----------
def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)


def env_refs(cfg: dict) -> bool:
    """配置里有没有引用服务的环境变量（env:名字 或 {env:名字}）。"""
    return any(ENV_REF.search(s) for k in ("url", "headers", "args", "env") for s in _strings(cfg.get(k)))


def _match(tpl, value, whole_arg: bool = False) -> bool:
    """value 是不是由模板 tpl 填出来的：{字段} 的位置可以是任何文字，其余必须原样。
    whole_arg：整个参数就是一个字段时，不能以 - 开头，免得填成命令行选项。"""
    if isinstance(tpl, str):
        if not isinstance(value, str):
            return False
        if whole_arg and SLOT.fullmatch(tpl) and value.startswith("-"):
            return False
        rx = "".join("(.*)" if SLOT.fullmatch(p) else re.escape(p) for p in SLOT.split(tpl))
        return bool(re.fullmatch(rx, value, re.S))
    if isinstance(tpl, list):
        return isinstance(value, list) and len(tpl) == len(value) and all(_match(t, v, True) for t, v in zip(tpl, value))
    if isinstance(tpl, dict):
        return isinstance(value, dict) and tpl.keys() == value.keys() and all(_match(tpl[k], value[k]) for k in tpl)
    return tpl == value


def from_market(cfg: dict) -> bool:
    """是不是照应用市场内置清单（不算 data/apps 里本机加的）原样生成的配置。"""
    from . import catalog
    it = catalog.builtin(cfg.get("catalog") or "")
    if not it:
        return False
    tpl = it["connect"]
    kind = tpl.get("type", "stdio")
    if (cfg.get("type") or "stdio") != kind:
        return False
    if kind == "stdio":
        return tpl["command"] == cfg.get("command") and _match(tpl.get("args") or [], cfg.get("args") or []) \
            and _match(tpl.get("env") or {}, cfg.get("env") or {})
    return _match(tpl["url"], cfg.get("url") or "") and _match(tpl.get("headers") or {}, cfg.get("headers") or {})


def _connect(c: dict) -> dict:
    return {"type": c.get("type") or "stdio", "command": c.get("command") or "", "args": c.get("args") or [],
            "env": c.get("env") or {}, "url": c.get("url") or "", "headers": c.get("headers") or {}}


def _configured(name: str, cfg: dict) -> bool:
    """config.yaml 的 mcp_servers 里原样写着的应用。"""
    from . import mcp_hub
    s = (CFG.get("mcp_servers") or {}).get(name)
    return bool(s) and _connect(mcp_hub.from_yaml(s)) == _connect(cfg)


def _inside_workspace(cfg: dict):
    """“文件夹”应用（官方的 server-filesystem）：参数里允许访问的目录都要在工作文件夹里。"""
    args = [str(a) for a in cfg.get("args") or []]
    if FS_PACKAGE not in args:
        return
    root = WORKSPACE.resolve()
    for a in args[args.index(FS_PACKAGE) + 1:]:
        p = Path(os.path.realpath(os.path.expanduser(a)))
        if p != root and root not in p.parents:
            raise ValueError(i18n.t("这台服务器上，文件夹应用只能访问工作文件夹（{dir}）", dir=str(root)))


def check_app(name: str, cfg: dict):
    """连接应用之前（以及启用、保存时）检查这份配置在这台服务器上能不能用，不能用就 raise ValueError。"""
    if apps_mode() == "any" or _configured(name, cfg):
        return
    stdio = (cfg.get("type") or "stdio") == "stdio"
    if demo():
        if stdio:
            raise ValueError(_no_stdio_in_demo())
        if not from_market(cfg):
            raise ValueError(i18n.t("演示服务器上只能添加应用市场里的应用"))
    if env_refs(cfg):
        raise ValueError(i18n.t("这台服务器上，应用配置不能引用服务的环境变量（env:名字）"))
    if stdio:
        if not from_market(cfg):
            raise ValueError(_market_only())
        _inside_workspace(cfg)


def check_market(item_id: str):
    """从应用市场添加之前先挡掉不能用的，免得填的密钥先存进了保险库。"""
    from . import catalog
    it = catalog.items().get(item_id)
    if not it or apps_mode() == "any" or it["connect"].get("type", "stdio") != "stdio":
        return
    if demo():
        raise ValueError(_no_stdio_in_demo())
    if catalog.builtin(item_id) is None:
        raise ValueError(_market_only())


# ---------- 模型服务 ----------
async def check_provider_url(url: str):
    """演示服务器上，模型服务的地址不能是本机或内网（不然能借“检查”读服务器上只对本机开放的服务）。"""
    if not demo():
        return
    from . import tools
    if await tools.private_host(url):
        raise ValueError(i18n.t("演示服务器上不能用本机或内网的模型服务地址"))
