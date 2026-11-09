"""在沙箱里跑一段代码：run_code（后台任务）和 quick_code（对话里算数、换单位、算日期）。

在哪跑：
- 电脑是 Docker / Instabox：电脑本身就是隔离的。代码和带的文件写进电脑工作文件夹的 .run/<编号>/，在那里执行，
  产出的文件留在那个文件夹里，send_file 能直接发。
- 电脑是本机（host）、借来的 Mac（linked），或者电脑关着：在服务所在的这台机器上开一个受限的子进程。
  - macOS：sandbox-exec。只能写工作文件夹和一个临时目录；/Users、/Volumes、/tmp 和服务自己的代码、数据目录（配置、数据库、保险库）
    读不到，只露出工作文件夹和解释器自己的目录；不能连本机的服务（localhost、unix socket）、不能叫起别的 App；quick_code 完全不能联网。
  - Linux：优先 bwrap，没有就 unshare（用户 + 挂载 + pid + 网络命名空间，挂载自己改成只读、丢掉全部权限，见 _PRELUDE）。
    只露出系统目录（/usr、/bin、/lib……，/etc 只留几个公开的文件）、工作文件夹和解释器；根目录下别的（/root、/home、/opt、/var、/run、/tmp……）都换成空的；
    不能联网：共用网络就能连本机上只对 127.0.0.1 开放的服务。服务用 root 跑时这些尤其要紧。
  - 环境变量照 host._env() 去掉密钥。都没有就返回“这台机器没有沙箱”，Bot 自己会说明。
  借来的 Mac：代码在服务器上跑（服务器不能让 Mac 执行命令），产出的文件再传到 Mac 的工作文件夹。
- quick_code 只用本机的受限子进程：Router 要快，不等电脑开机。没有沙箱时 Router 就没有这个工具。

security.exec（exec_mode()）：off 不能执行代码；sandbox 只在沙箱里；host 另外还允许本机终端（computer_shell，每条确认）。
"""
import asyncio
import base64
import io
import json
import os
import posixpath
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import i18n, security
from .config import CFG, DATA, ROOT

LANGS = ("python", "bash", "node")
EXT = {"python": "py", "bash": "sh", "node": "js"}
LIMIT = 20000          # 给模型的输出最多这么多字
CAP = 256 << 10        # 程序的输出最多收这么多字节，再多的丢掉
TIMEOUT, QUICK_TIMEOUT, MAX_TIMEOUT = 60, 5, 600
MAX_INPUT = 1 << 20    # 代码加带的文件最多 1 MB
MAX_FILES = 20
RUN = ".run"           # 工作文件夹里每次运行一个子文件夹
KEEP_DAYS = 7          # 超过这么多天的运行文件夹下次跑的时候清掉
MARK = "__OSA_RUN_EXIT__"
QUICK_TIMED_OUT = "超时了，改派后台任务"


def exec_mode() -> str:
    """security.exec：off / sandbox / host，策略表在 security.py（没写时按 local_exec / demo 推）。"""
    return security.exec_mode()


# ---------- 在哪跑 ----------
def _computer_backend() -> str:
    """能在里面跑代码的电脑（Docker / Instabox），没有返回空。"""
    from . import computer
    if not (CFG.get("computer") or {}).get("enabled", True):
        return ""
    b = computer.backend_name()
    return b if b == "instabox" or (b == "local" and shutil.which("docker")) else ""


def place(quick: bool = False) -> str:
    """这次在哪跑：computer（Docker / Instabox 电脑里）、host（本机的受限子进程），都不行返回空。"""
    if exec_mode() == "off":
        return ""
    if not quick and _computer_backend():
        return "computer"
    return "host" if host_sandbox() else ""


_probed: dict[str, str] = {}


def host_sandbox() -> str:
    """这台机器上能用的沙箱：sandbox-exec / bwrap / unshare，都没有返回空。第一次用时真跑一次试试，结果记住。"""
    if "kind" not in _probed:
        _probed["kind"] = _probe()
    return _probed["kind"]


def kinds() -> list[str]:
    """这个平台上按顺序试的沙箱。"""
    return ["sandbox-exec"] if sys.platform == "darwin" else ["bwrap", "unshare"] if sys.platform.startswith("linux") else []


def works(kind: str) -> bool:
    """装了、而且真能起来（容器里常常不让建命名空间）。"""
    if not shutil.which(kind):
        return False
    d = Path(tempfile.mkdtemp(prefix="osa-probe-")).resolve()
    try:
        argv = _wrap(kind, [sys.executable, "-c", "pass"], ws=d, run=d, tmp=d, net=False, reads=_reads("python"))
        return subprocess.run(argv, cwd=d, env=_env(d), stdin=subprocess.DEVNULL, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _probe() -> str:
    return next((k for k in kinds() if works(k)), "")


def _no_sandbox() -> str:
    s = "这台机器没有沙箱（macOS 的 sandbox-exec、Linux 的 bwrap 或 unshare 都用不了），跑不了代码。"
    from . import computer
    if exec_mode() != "host":
        return s + "这台服务器只允许在沙箱里执行代码。"
    if computer.backend_name() == "host":
        from .computer import host
        if host.shell_on():
            return s + "可以改用 computer_shell 在终端里跑（每条命令要用户确认）。"
        return s + "用户在电脑面板打开“允许 Bot 用终端”后，可以用 computer_shell 跑（每条命令要用户确认）。"
    return s


# ---------- 对外 ----------
@dataclass
class Result:
    text: str
    files: list = field(default_factory=list)
    timed_out: bool = False


async def run(lang: str, code: str, files: dict | None = None, timeout: int = TIMEOUT, net: bool = True,
              computer: bool = True) -> tuple[str, list[str]]:
    """跑一段代码，返回 (给模型的文字：退出码 + 输出 + 产出的文件, 产出的文件路径)。出错也只返回一句说明，不抛异常。
    net=False 不能联网（Linux 的本机沙箱总是不能）；computer=False 不用电脑，只用本机的受限子进程（quick_code）。"""
    r = await execute(lang, code, files, timeout, net, computer)
    return r.text, r.files


async def execute(lang: str, code: str, files: dict | None = None, timeout: int = TIMEOUT, net: bool = True,
                  computer: bool = True) -> Result:
    lang = (lang or "python").strip().lower()
    if lang not in LANGS:
        return Result(f"不支持 {lang}，只能用 {' / '.join(LANGS)}")
    if not isinstance(code, str) or not code.strip():
        return Result("没有要跑的代码")
    inputs = _inputs(files, f"main.{EXT[lang]}")
    if isinstance(inputs, str):
        return Result(inputs)
    if len(code.encode()) + sum(len(v.encode()) for v in inputs.values()) > MAX_INPUT:
        return Result(f"代码和文件加起来太大了（最多 {MAX_INPUT >> 20} MB），大文件先存进工作文件夹再在代码里读")
    try:
        timeout = max(1, min(MAX_TIMEOUT, int(timeout or TIMEOUT)))
    except (TypeError, ValueError):
        timeout = TIMEOUT
    if exec_mode() == "off":
        return Result("这台服务器关掉了执行代码（config.yaml 的 security.exec: off）")
    where = place(quick=not computer)
    if not where:
        return Result(_no_sandbox())
    try:
        if where == "computer":
            from . import computer as pc
            return await _in_computer(pc.get(), lang, code, inputs, timeout)
        return await _on_host(lang, code, inputs, timeout, net)
    except Exception as e:   # 给模型一句话，别让工具调用整个失败
        return Result(f"没跑成：{e}")


def _inputs(files, main: str) -> dict[str, str] | str:
    """带进去的文件 {相对路径: 文字内容}。不对时返回一句说明。"""
    if not files:
        return {}
    if not isinstance(files, dict):
        return "files 要写成 {文件名: 内容}"
    if len(files) > MAX_FILES:
        return f"一次最多带 {MAX_FILES} 个文件"
    out = {}
    for name, content in files.items():
        p = PurePosixPath(str(name).replace("\\", "/"))
        if not p.parts or p.is_absolute() or ".." in p.parts or str(p) in (main, ".") or str(p).startswith("-"):
            return f"文件名不行：{name}（写相对路径，不能有 ..）"
        if not isinstance(content, str):
            return f"{name} 的内容要是文字"
        out[str(p)] = content
    return out


def _rid() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _text(rc: int, out: str, timed_out: bool, timeout: int, files: list[str], note: str = "") -> str:
    out = out.rstrip()
    if len(out) > LIMIT:
        out = out[:LIMIT] + f"\n…（输出太长，后面 {len(out) - LIMIT} 字没给）"
    head = f"超时了：{timeout} 秒内没跑完，已经停掉" if timed_out else f"退出码 {rc}"
    text = head + "\n" + (out or "（没有输出）")
    if files:
        text += "\n产出的文件：\n" + "\n".join(f"- {f}" for f in files)
    return text + (f"\n{note}" if note else "")


# ---------- 电脑里（Docker / Instabox） ----------
def _tarball(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


async def _in_computer(c, lang: str, code: str, inputs: dict[str, str], timeout: int) -> Result:
    exe = {"python": "python3", "bash": "bash", "node": "node"}[lang]
    cmd = {"python": "python3 -B", "bash": "bash", "node": "node"}[lang]
    main = f"main.{EXT[lang]}"
    root = f"{c.workspace}/{RUN}"
    d = f"{root}/{_rid()}"
    keep = " ".join(f"! -path {shlex.quote('./' + n)}" for n in [main, *inputs])
    script = f"""command -v {exe} >/dev/null 2>&1 || exit 97
find {shlex.quote(root)} -mindepth 1 -maxdepth 1 -type d -mtime +{KEEP_DAYS} -exec rm -rf {{}} + 2>/dev/null
mkdir -p {shlex.quote(d)} && cd {shlex.quote(d)} && base64 -d | tar -xzf - --no-same-owner -m || exit 98
T=""; command -v timeout >/dev/null 2>&1 && T="timeout -k 2 {timeout}"
PYTHONUNBUFFERED=1 MPLBACKEND=Agg $T {cmd} {main} </dev/null 2>&1 | head -c {CAP}
rc=${{PIPESTATUS[0]}}
rm -f {main}
outs=$(find . -type f {keep} | sed 's|^\\./||' | head -n 50)
[ -n "$outs" ] || {{ cd / && rm -rf {shlex.quote(d)}; }}
printf '\\n{MARK}%s\\n%s\\n' "$rc" "$outs"
"""
    await c.start()
    c.last_used = time.time()
    r = await c.exec("bash -c " + shlex.quote(script), timeout + 30, stdin=_tarball({**inputs, main: code}))
    if r.exit_code == 97:
        return Result(f"电脑里没有 {exe}，换一种语言写吧")
    at = r.stdout.rfind("\n" + MARK)
    if at < 0:
        if r.timed_out:
            return Result(_text(124, r.stdout, True, timeout, []), timed_out=True)
        return Result(f"没跑成：{(r.stderr or r.stdout).strip()[-500:] or f'退出码 {r.exit_code}'}")
    out, tail = r.stdout[:at], r.stdout[at + len(MARK) + 1:].splitlines()
    rc = int(tail[0]) if tail and tail[0].strip().lstrip("-").isdigit() else r.exit_code
    files = [f"{d}/{x}" for x in tail[1:] if x.strip()]
    timed_out = rc in (124, 137) or r.timed_out
    return Result(_text(rc, out, timed_out, timeout, files), files, timed_out)


# ---------- 本机的受限子进程 ----------
def _real(p) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _interpreter(lang: str) -> str:
    if lang == "python":
        return sys.executable
    return shutil.which(lang) or ("/bin/bash" if lang == "bash" and os.path.exists("/bin/bash") else "")


def _reads(lang: str, exe: str = "") -> list[str]:
    """解释器自己的目录：藏起来的地方（用户目录、外接硬盘、/opt……）里装的解释器也要能读。"""
    if lang == "python":
        dirs = {sys.prefix, sys.base_prefix, sys.exec_prefix, str(Path(_real(sys.executable)).parent.parent)}
    else:
        dirs = {str(Path(_real(exe)).parent.parent)} if exe else set()
    return sorted({_real(d) for d in dirs if d} - {"/"})


def _config_file() -> str:
    return _real(ROOT / os.environ.get("OSA_CONFIG", "config.yaml"))


# Linux：根目录下只露出这些系统目录（/etc 另外只留 ETC_KEEP），别的（/root、/home、/opt、/var、/run、/srv、/data……）都换成空的
SYSTEM = {"usr", "bin", "sbin", "lib", "lib32", "lib64", "libx32", "dev", "proc", "sys"}


def _hidden() -> list[str]:
    """沙箱里读不到的地方：用户自己的文件、服务的代码和数据目录（配置、数据库、保险库都在里面）。"""
    mine = [_real("~"), _real(ROOT), _real(DATA), _config_file()]
    if sys.platform == "darwin":
        return ["/Users", "/Volumes", "/private/tmp", "/private/var/folders", *mine]
    try:
        top = [f"/{n}" for n in os.listdir("/") if n not in SYSTEM and n != "etc"]
    except OSError:
        top = ["/root", "/home", "/opt", "/srv", "/mnt", "/media", "/var", "/run", "/boot"]
    return sorted({"/tmp", *top}) + mine


# Linux：/etc 里只留这些（没有密钥的系统文件）
ETC_KEEP = ("passwd", "group", "nsswitch.conf", "hosts", "resolv.conf", "localtime", "timezone", "ld.so.cache", "ld.so.conf",
            "ld.so.conf.d", "ssl", "ca-certificates", "pki", "alternatives", "mime.types", "fonts", "os-release", "lsb-release",
            "debian_version", "python3", "matplotlibrc")


def _env(tmp) -> dict:
    from .computer.host import _env as clean
    tmp = str(tmp)
    return {**clean(), "HOME": tmp, "TMPDIR": tmp, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg", "LANG": os.environ.get("LANG") or "C.UTF-8"}


def _under(p: str, roots) -> bool:
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def _sbpl(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _seatbelt(ws: str, tmp: str, reads: list[str], net: bool) -> str:
    """macOS 的沙箱规则（后面的规则盖过前面的）：先全部拒绝，再放开跑程序要的；能读系统，藏起来的地方读不到，只能写工作文件夹和临时目录。"""
    def where(paths):
        return " ".join(f"(literal {_sbpl(p)})" if os.path.isfile(p) else f"(subpath {_sbpl(p)})" for p in paths)
    hide = [h for h in _hidden() if os.path.exists(h)]
    rules = [
        "(version 1)", "(deny default)",
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow ipc-posix-sem)",
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
        "(allow file-read*)",
        f"(deny file-read* {where(hide)})",
        f"(allow file-read* {where([*reads, ws, tmp])})",
        "(allow file-read-metadata)",
        f"(allow file-write* {where([ws, tmp])})",
        '(allow file-write-data file-ioctl (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") '
        '(literal "/dev/dtracehelper") (subpath "/dev/fd"))',
    ]
    if net:   # 能连外面，连不了本机（localhost 上的服务、unix socket；查域名要的 mDNSResponder 除外）
        rules += ["(allow network-outbound (remote ip))", '(deny network-outbound (remote ip "localhost:*"))',
                  '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
                  "(allow system-socket)",
                  '(allow mach-lookup (global-name "com.apple.dnssd.service") (global-name "com.apple.SystemConfiguration.configd")'
                  ' (global-name "com.apple.SystemConfiguration.DNSConfiguration") (global-name "com.apple.trustd.agent"))']
    return "\n".join(rules)


def _view(ws: str, reads: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Linux 沙箱里看到的文件系统：(换成空目录的, 从外面带进来只读的, 从外面带进来能写的)。"""
    hide = []
    for h in sorted({_real(x) for x in _hidden()}, key=len):
        if h != "/" and os.path.isdir(h) and not os.path.islink(h) and not _under(h, hide):
            hide.append(h)
    hide.append("/etc")
    ro = [f"/etc/{f}" for f in ETC_KEEP if os.path.exists(f"/etc/{f}")]
    ro += [r for r in reads if os.path.exists(r) and _under(r, hide) and not _under(r, [ws])]
    cfg = _config_file()
    if os.path.isfile(cfg) and not _under(cfg, hide):
        ro.append(cfg)   # 配置文件在别处：换成空的（见 _bwrap / _PRELUDE）
    return hide, sorted(ro, key=len), [ws]


def _bwrap(argv: list[str], ws: str, run: str, reads: list[str]) -> list[str]:
    hide, ro, rw = _view(ws, reads)
    cfg = _config_file()
    # --cap-drop ALL：服务用 root 跑时 bwrap 默认留着全部权限，沙箱里就能把盖上的空目录卸掉、看到下面原来的文件
    a = ["bwrap", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--unshare-user-try", "--unshare-ipc", "--unshare-pid",
         "--unshare-uts", "--unshare-cgroup-try", "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for h in hide:
        a += ["--tmpfs", h]
    for p in ro:
        a += ["--ro-bind", "/dev/null", p] if p == cfg else ["--ro-bind-try", p, p]
    for p in rw:
        a += ["--bind", p, p]
    for h in hide:   # 换上的空目录也只读（/tmp 除外），写了也只是写进内存，但别让人以为写成了
        if h != "/tmp":
            a += ["--remount-ro", h]
    return a + ["--chdir", run, "--", *argv]


# unshare 退路：在新的用户 + 挂载命名空间里自己做 bwrap 做的事，再丢掉全部权限，然后换成要跑的程序。
# 先拿住要带进来的目录（O_PATH），再把要藏的地方盖上空的 tmpfs，然后经 /proc/self/fd 把它们挂回原处；
# 最后除了工作文件夹、/tmp、/dev/shm，所有挂载都改成只读（带上原来锁住的 nosuid / nodev 这些，不然改不了）。
_PRELUDE = r'''
import ctypes, json, os, sys
spec = json.loads(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
RO, NOSUID, NODEV, NOEXEC, REMOUNT, BIND, REC, PRIVATE = 1, 2, 4, 8, 32, 4096, 16384, 1 << 18
LOCKED = {"nosuid": NOSUID, "nodev": NODEV, "noexec": NOEXEC, "noatime": 1024, "nodiratime": 2048,
          "relatime": 1 << 21, "strictatime": 1 << 24}
def mount(src, dst, fs, flags, data=None):
    enc = lambda s: s.encode() if s is not None else None
    if libc.mount(enc(src), enc(dst), enc(fs), flags, enc(data)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, "mount %s: %s" % (dst, os.strerror(e)))
def under(p, roots):
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)
mount(None, "/", None, REC | PRIVATE)
devs = ["/dev/" + d for d in ("null", "zero", "full", "random", "urandom", "tty")]
held = {p: os.open(p, os.O_PATH) for p in spec["ro"] + spec["rw"] + devs if os.path.exists(p)}
for h in spec["hide"] + ["/dev"]:
    mount("tmpfs", h, "tmpfs", NOSUID | NODEV, "mode=755")
for p in sorted(held, key=len):
    src = "/proc/self/fd/%d" % held[p]
    if p == spec["cfg"]:
        src = "/proc/self/fd/%d" % held["/dev/null"]
    if os.path.isdir(src):
        os.makedirs(p, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "a").close()
    mount(src, p, None, BIND | REC)
os.makedirs("/dev/shm", exist_ok=True)
mount("tmpfs", "/dev/shm", "tmpfs", NOSUID | NODEV, "mode=1777")
for name, target in (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"), ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2")):
    os.symlink(target, "/dev/" + name)
rw = spec["rw"] + ["/tmp", "/dev/shm"]
for line in open("/proc/self/mountinfo"):
    f = line.split()
    m = f[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
    if under(m, rw):
        continue
    flags = REMOUNT | BIND | RO
    for o in f[5].split(","):
        flags |= LOCKED.get(o, 0)
    try:
        mount(None, m, None, flags)
    except OSError as e:
        if e.errno != 2 and not under(m, ["/proc", "/sys"]):   # 2：被盖住了，路径已经不在
            raise
mount("tmpfs", "/sys", "tmpfs", NOSUID | NODEV | NOEXEC | RO, "mode=555")   # sysfs 的文件按权限位能写，整个藏起来
try:
    mount("proc", "/proc", "proc", NOSUID | NODEV | NOEXEC | RO)   # 只看得到沙箱里的进程；只读，/proc/sys 写不了
except OSError:
    mount("tmpfs", "/proc", "tmpfs", NOSUID | NODEV | NOEXEC | RO, "mode=555")   # 挂不了新的就整个藏起来
PR_SET_NO_NEW_PRIVS, PR_CAPBSET_DROP, PR_SET_SECUREBITS = 38, 24, 28
libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
libc.prctl(PR_SET_SECUREBITS, 0x2f, 0, 0, 0)   # NOROOT | NO_SETUID_FIXUP 并锁住：root 执行程序也拿不回权限
for cap in range(64):
    libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "no_new_privs")
class Hdr(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]
class Data(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]
if libc.capset(ctypes.byref(Hdr(0x20080522, 0)), (Data * 2)()) != 0:
    raise OSError(ctypes.get_errno(), "capset")
os.chdir(spec["cwd"])
os.execv(spec["argv"][0], spec["argv"])
'''


def _unshare(argv: list[str], ws: str, run: str, reads: list[str]) -> list[str]:
    hide, ro, rw = _view(ws, reads)
    spec ={"hide": hide, "ro": ro, "rw": rw, "cfg": _config_file(), "cwd": run, "argv": argv}
    return ["unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork", "--net", "--ipc", "--uts", "--",
            sys.executable, "-I", "-S", "-c", _PRELUDE, json.dumps(spec)]


def _wrap(kind: str, argv: list[str], *, ws, run, tmp, net: bool, reads: list[str]) -> list[str]:
    ws, run, tmp = str(ws), str(run), str(tmp)
    if kind == "sandbox-exec":
        return ["sandbox-exec", "-p", _seatbelt(ws, tmp, reads, net), *argv]
    if kind == "bwrap":
        return _bwrap(argv, ws, run, reads)
    if kind == "unshare":
        return _unshare(argv, ws, run, reads)
    raise ValueError(kind)


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            p.kill()
        except ProcessLookupError:
            pass


async def _spawn(argv: list[str], cwd: str, env: dict, timeout: int) -> tuple[int, str, bool]:
    """跑起来，stdout 和 stderr 合在一起收（最多 CAP 字节），超时连它开的子进程一起停掉。返回 (退出码, 输出, 是否超时)。"""
    p = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                             start_new_session=True)
    buf = bytearray()

    async def pump():
        while chunk := await p.stdout.read(65536):
            if len(buf) < CAP:
                buf.extend(chunk[:CAP - len(buf)])
        return await p.wait()

    try:
        rc = await asyncio.wait_for(pump(), timeout)
        return rc, buf.decode(errors="replace"), False
    except asyncio.TimeoutError:
        _kill(p)
        await p.wait()
        return -9, buf.decode(errors="replace"), True
    except BaseException:
        _kill(p)
        raise


def _prune(root: Path):
    try:
        old = time.time() - KEEP_DAYS * 86400
        for e in os.scandir(root):
            if e.is_dir(follow_symlinks=False) and e.stat(follow_symlinks=False).st_mtime < old:
                shutil.rmtree(e.path, ignore_errors=True)
    except OSError:
        pass


def _outputs(run: Path, skip: set[str]) -> list[str]:
    out = []
    for dirpath, dirs, names in os.walk(run):
        for n in names:
            rel = os.path.relpath(os.path.join(dirpath, n), run).replace(os.sep, "/")
            if rel not in skip:
                out.append(rel)
    return sorted(out)[:50]


async def _on_host(lang: str, code: str, inputs: dict[str, str], timeout: int, net: bool) -> Result:
    from . import computer
    kind = host_sandbox()
    exe = _interpreter(lang)
    if not exe:
        return Result(f"这台机器上没有 {lang}，换一种语言写吧")
    ws = DATA / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    ws = Path(_real(ws))
    _prune(ws / RUN)
    rid, main = _rid(), f"main.{EXT[lang]}"
    run = ws / RUN / rid
    for name, content in {**inputs, main: code}.items():
        p = run / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="osa-run-")).resolve() if kind == "sandbox-exec" else Path("/tmp")
    argv = [exe, "-B", main] if lang == "python" else [exe, main]
    try:
        rc, out, timed_out = await _spawn(_wrap(kind, argv, ws=ws, run=run, tmp=tmp, net=net, reads=_reads(lang, exe)),
                                          str(run), _env(tmp), timeout)
    finally:
        if kind == "sandbox-exec":
            shutil.rmtree(tmp, ignore_errors=True)
    (run / main).unlink(missing_ok=True)
    made = _outputs(run, set(inputs))
    if not made:
        shutil.rmtree(run, ignore_errors=True)
    files, note = [str(run / x) for x in made], ""
    if made and computer.backend_name() == "linked":   # 借来的 Mac：Bot 的文件工具看的是 Mac 的工作文件夹，传过去
        files, note = await _to_linked(run, rid, made)
    return Result(_text(rc, out, timed_out, timeout, files, note), files, timed_out)


async def _to_linked(run: Path, rid: str, made: list[str]) -> tuple[list[str], str]:
    from . import computer
    c = computer.get()
    out = []
    try:
        for x in made:
            p = f"{RUN}/{rid}/{x}"
            await c.write_bytes(p, (run / x).read_bytes())
            out.append(posixpath.join(c.workspace, p) if c.workspace else p)
    except Exception as e:
        return [str(run / x) for x in made], f"（代码在服务器上跑的，产出的文件没能传到你的 Mac：{e}；文件还在服务器的工作文件夹里）"
    shutil.rmtree(run, ignore_errors=True)
    return out, "（代码在服务器上跑的，读不到 Mac 上的文件；产出的文件已经传到 Mac 的工作文件夹）"


# ---------- 工具 ----------
RUN_CODE = {"type": "function", "function": {
    "name": "run_code",
    "description": "在沙箱里跑一段代码（python / bash / node），返回输出和产出的文件。算数、算日期、换单位、统计和处理数据、"
                   "画图表、转换文件格式都用它算，不要心算。在沙箱里跑：当前目录在工作文件夹里，用相对路径写的文件会留下来"
                   "（结果里列出路径，可以 send_file），工作文件夹外改不了；电脑是用户自己的电脑时读不到用户的其他文件，"
                   "也可能不能联网（要下载的先用 fetch_url 或浏览器存进工作文件夹）。",
    "parameters": {"type": "object", "properties": {
        "lang": {"type": "string", "enum": list(LANGS), "description": "语言，默认 python"},
        "code": {"type": "string", "description": "要跑的代码，结果用 print / echo 输出"},
        "files": {"type": "object", "additionalProperties": {"type": "string"},
                  "description": "可选：一起放进去的文字文件 {相对路径: 内容}，比如数据、要调用的脚本"},
        "timeout": {"type": "integer", "description": f"最多跑多少秒，默认 {TIMEOUT}，最多 {MAX_TIMEOUT}"}},
        "required": ["code"]}}}

QUICK_CODE = {"type": "function", "function": {
    "name": "quick_code",
    "description": "在沙箱里马上跑一小段 Python，返回输出。算数、换单位、算日期和星期、算时差、算百分比这类要算准的，用它算，不要心算。"
                   f"在沙箱里跑：{QUICK_TIMEOUT} 秒内要跑完，不能联网，工作文件夹外改不了。要处理文件、联网或跑得久的派任务。",
    "parameters": {"type": "object", "properties": {
        "code": {"type": "string", "description": "Python 代码，结果用 print 输出"}}, "required": ["code"]}}}


async def tool_run(call) -> str:
    a = call.args
    text, _ = await run(str(a.get("lang") or "python"), a.get("code"), a.get("files"), a.get("timeout") or TIMEOUT)
    return text


async def tool_quick(call) -> str:
    r = await execute("python", call.args.get("code"), timeout=QUICK_TIMEOUT, net=False, computer=False)
    return QUICK_TIMED_OUT if r.timed_out else r.text


def progress(args: dict) -> str:
    first = next((ln.strip() for ln in str(args.get("code") or "").splitlines() if ln.strip()), "")
    return i18n.t("跑代码：{what}", what=first[:60])
