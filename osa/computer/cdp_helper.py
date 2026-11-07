"""在电脑里运行的浏览器控制脚本：通过 Chrome DevTools Protocol（CDP）操作 Chromium。

服务端把它写进电脑，然后每个动作执行一次：stdin 传 JSON {"op": ..., ...}，stdout 返回 JSON。
只用 Python 标准库（电脑里不一定能装包），自带一个最小的 WebSocket 客户端。
DevTools 端口只监听电脑内部的 127.0.0.1，不对外暴露。

几个任务同时用电脑时，每个任务在自己的浏览器窗口里做（stdin 里的 "win"，见 Ctx），登录状态是同一份。
截图、点击、按键都经 CDP 发给自己窗口里的页面，不动真实的鼠标键盘，所以互不干扰。

本机模式（host.py，环境变量 OSA_CDP_PROFILE 给了配置目录）：用你装的 Chrome，开一个单独的配置目录，和你日常用的分开。
- 调试端口由 Chrome 随机选（--remote-debugging-port=0），写在配置目录的 DevToolsActivePort 里，只听 127.0.0.1；
  不加 --remote-allow-origins，网页连不上它（这个脚本的 WebSocket 不带 Origin，不受影响）。
- 不扫 /proc、不杀别的 Chrome，不把窗口提到最前面（你可能正在用别的程序），截图直接放在返回的 JSON 里。
"""
from __future__ import annotations   # 电脑里的 Python 可能比较旧

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:   # Windows
    fcntl = None

HOST = os.environ.get("OSA_CDP_PROFILE", "")   # 本机模式：Chrome 的配置目录


def _read_port() -> int:
    """本机模式下 Chrome 自己选的调试端口。Chrome 正常退出后这个文件还在，所以拿到端口还要连一下才算数。"""
    try:
        with open(os.path.join(HOST, "DevToolsActivePort")) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


PORT = _read_port() if HOST else 9222
# 窗口被别的窗口挡住时，浏览器默认把它当成隐藏的：动画帧停掉、定时器降到每秒一次，网页会加载不全。几个任务并行时必须关掉
FLAGS = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]
LOCK = os.path.join(os.path.dirname(HOST), "cdp.lock") if HOST else "/tmp/osa-cdp.lock"
SNAPSHOT_JS = r"""
(() => {
  document.querySelectorAll('[data-osa-ref]').forEach(e => e.removeAttribute('data-osa-ref'));
  const sel = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=radio],[role=option],[role=switch],[role=combobox],[role=searchbox],[role=textbox],' +
              '[contenteditable=""],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const vh = innerHeight, vw = innerWidth, out = [];
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    return r.width >= 2 && r.height >= 2 && st.visibility !== 'hidden' && st.display !== 'none'; };
  // 有弹窗（登录框、对话框）时只列弹窗里的元素，并提示模型
  // 判断标准：标了模态的，或者屏幕正中间那一点落在一个 fixed 定位的浮层里（吸顶导航栏不在正中，不会误判）
  let modal = [...document.querySelectorAll('[aria-modal=true],dialog:modal')].filter(visible).pop() || null;
  if (!modal) {
    for (let e = document.elementFromPoint(vw / 2, vh / 2); e && e !== document.body; e = e.parentElement) {
      if (getComputedStyle(e).position === 'fixed') { modal = e; break; }
    }
  }
  const dialogs = modal && modal.querySelector(sel) ? [modal] : [];
  const root = dialogs.length ? modal : document;
  let n = 0;
  for (const e of root.querySelectorAll(sel)) {
    if (!visible(e) || e.disabled || e.type === 'hidden') continue;
    if ([...e.querySelectorAll(sel)].some(visible)) continue;   // 外层包着可点元素的（如 menuitem 里的 a），只留里层
    const r = e.getBoundingClientRect();
    const inView = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    if (!inView && out.length > 80) continue;
    n += 1; e.setAttribute('data-osa-ref', n);
    const tag = e.tagName.toLowerCase(), role = e.getAttribute('role');
    let kind = role || tag;
    if (tag === 'input') kind = 'input(' + (e.type || 'text') + ')';
    let name = clean(e.getAttribute('aria-label') || e.innerText || e.value || e.getAttribute('title') || e.getAttribute('alt') || '');
    if (!name && tag === 'a') { const img = e.querySelector('img[alt]'); if (img) name = clean(img.alt); }
    let line = `[${n}] ${kind}`;
    if (name) line += ` "${name.slice(0, 80)}"`;
    if (e.placeholder) line += ` placeholder="${clean(e.placeholder).slice(0, 40)}"`;
    if ((tag === 'input' || tag === 'textarea') && e.value && !/password/.test(e.type)) line += ` value="${clean(e.value).slice(0, 40)}"`;
    if (e.checked) line += ' (已选中)';
    if (tag === 'a') { const h = e.getAttribute('href') || ''; if (h && !h.startsWith('javascript')) line += ` → ${h.slice(0, 80)}`; }
    if (!inView) line += ' (屏幕外)';
    out.push(line);
    if (out.length >= 150) break;
  }
  const text = clean(root === document ? (document.body ? document.body.innerText : '') : root.innerText).slice(0, 1500);
  const scroll = Math.round(scrollY), total = Math.round(document.documentElement.scrollHeight);
  return JSON.stringify({url: location.href, title: document.title, elements: out, text, dialog: dialogs.length > 0,
                         scroll: `${scroll}/${total}，视口高 ${vh}`});
})()
"""


# “演示一遍”的录制脚本：注入每个页面，把用户的点击、输入、选择、回车交给 CDP 绑定 __osaRec。
# 密码、验证码一类的输入只记成 ******。
RECORDER_JS = r"""
(() => {
  if (window.__osaRecOn) return; window.__osaRecOn = true;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const send = ev => { try { window.__osaRec(JSON.stringify(Object.assign(ev, {url: location.href, title: document.title}))); } catch (e) {} };
  const SEL = 'a[href],button,input,textarea,select,summary,label,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=radio],[role=option],[role=switch],[role=combobox],[onclick]';
  const label = e => {
    if (!e || !e.getAttribute) return '';
    const l = e.labels && e.labels[0] ? e.labels[0].innerText : '';
    return clean(e.getAttribute('aria-label') || l || e.placeholder || e.getAttribute('name') || e.getAttribute('title') || '');
  };
  const describe = e => {
    const tag = e.tagName.toLowerCase(), role = e.getAttribute('role');
    let kind = role || tag;
    if (tag === 'input') kind = 'input(' + (e.type || 'text') + ')';
    let name = clean(e.getAttribute('aria-label') || e.innerText || e.value || e.title || e.alt || '');
    if (!name && tag === 'a') { const img = e.querySelector('img[alt]'); if (img) name = clean(img.alt); }
    if (!name) name = label(e);
    const d = {kind, name};
    const h = tag === 'a' ? e.getAttribute('href') || '' : '';
    if (h && !h.startsWith('javascript')) d.href = e.href.slice(0, 200);
    return d;
  };
  const secret = e => e.type === 'password' ||
    /pass|pwd|密码|otp|验证码|captcha|cvv|cvc|card/i.test([e.name, e.id, e.autocomplete, label(e)].join(' '));
  const typed = e => ({type: 'input', field: label(e) || describe(e).name, value: secret(e) ? '******' : (e.value || '').slice(0, 300), secret: secret(e)});
  const TEXT = /^(text|search|email|url|tel|number|password)$/;
  document.addEventListener('click', ev => {
    const e = ev.target && ev.target.closest ? ev.target.closest(SEL) : null;
    if (!e) return;
    if ((e.tagName === 'INPUT' && TEXT.test(e.type || 'text')) || e.tagName === 'TEXTAREA') return;   // 点输入框只是聚焦，输入时再记
    send(Object.assign({type: 'click'}, describe(e)));
  }, true);
  document.addEventListener('change', ev => {
    const e = ev.target; if (!e || !e.tagName) return;
    const tag = e.tagName.toLowerCase();
    if (tag === 'select') return send({type: 'select', field: label(e), value: clean(e.selectedOptions[0] && e.selectedOptions[0].innerText)});
    if (tag === 'input' && (e.type === 'checkbox' || e.type === 'radio')) return send({type: 'check', field: label(e) || describe(e).name, checked: e.checked});
    if (tag === 'input' || tag === 'textarea') send(typed(e));
  }, true);
  document.addEventListener('keydown', ev => {
    const e = ev.target;
    if (ev.key === 'Enter' && e && e.tagName === 'INPUT') return send(Object.assign(typed(e), {submit: true}));   // 回车提交时 change 还没来
    if (ev.key === 'Escape') send({type: 'key', key: 'Escape'});
  }, true);
  document.addEventListener('focusout', ev => {
    const e = ev.target;
    if (e && e.isContentEditable) send({type: 'input', field: label(e) || '编辑区', value: clean(e.innerText).slice(0, 300)});
  }, true);
})()
"""


class WS:
    """最小 WebSocket 客户端（只支持文本帧，够 CDP 用）。"""

    def __init__(self, url: str):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.sock.recv(4096)
            if not c:
                raise ConnectionError("DevTools 握手失败")
            buf += c
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("DevTools 拒绝连接：" + head.split(b"\r\n")[0].decode(errors="replace"))
        self.n = 0
        self.events: list[dict] = []   # 等回复时顺便收到的事件

    def send(self, text: str):
        data = text.encode()
        h = bytearray([0x81])
        n = len(data)
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126)
            h += struct.pack(">H", n)
        else:
            h.append(0x80 | 127)
            h += struct.pack(">Q", n)
        mask = os.urandom(4)
        h += mask
        self.sock.sendall(bytes(h) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise EOFError("DevTools 连接断开")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        msg = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b1 & 0x0F
            if op == 8:
                raise EOFError("DevTools 连接关闭")
            if op in (9, 10):
                continue
            msg += data
            if b1 & 0x80:
                return msg.decode(errors="replace")

    def call(self, method: str, params: dict | None = None, timeout: float = 30):
        self.n += 1
        mid = self.n
        self.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(self.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            if "method" in m:
                self.events = (self.events + [m])[-50:]
        raise TimeoutError(method)

    def wait_event(self, method: str, timeout: float = 5) -> dict | None:
        import select
        end = time.time() + timeout
        while True:
            for m in self.events:
                if m["method"] == method:
                    self.events.remove(m)
                    return m.get("params") or {}
            left = end - time.time()
            if left <= 0:
                return None
            if self.buf or select.select([self.sock], [], [], left)[0]:
                m = json.loads(self.recv())
                if "method" in m:
                    self.events.append(m)


# ---------- 浏览器 ----------
def _targets(timeout: float = 2):
    global PORT
    if HOST:
        PORT = PORT or _read_port()
        if not PORT:
            raise ConnectionError("Chrome 还没开")
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=timeout) as r:
        return [t for t in json.load(r) if t.get("type") == "page" and not t["url"].startswith("devtools://")]


def _cmdline(raw: bytes) -> list[str]:
    """/proc/<pid>/cmdline 拆成参数。有的 Chromium 会把自己的命令行改写成用空格连起来的一整串，那就按空格拆。"""
    args = [x.decode(errors="replace") for x in raw.split(b"\0") if x]
    return args[0].split() if len(args) == 1 and " --" in args[0] else args


def _browser_proc() -> tuple[int | None, list[str]]:
    """开着 DevTools 端口的浏览器主进程：(pid, 命令行)。"""
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                args = _cmdline(f.read())
        except OSError:
            continue
        if any(x.startswith(f"--remote-debugging-port={PORT}") for x in args) and not any(x.startswith("--type=") for x in args):
            return int(d), args
    return None, []


def _launch(argv: list[str] | None = None) -> list:
    if HOST:
        return _launch_host()
    if not argv:
        b = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if not b:
            raise RuntimeError("电脑里没有浏览器")
        argv = [b, f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1", "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check", "--password-store=basic", "about:blank"]
    argv = argv[:1] + [f for f in FLAGS if f not in argv] + argv[1:]
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")}
    subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(60):
        time.sleep(0.25)
        try:
            ts = _targets()
            if ts:
                return ts
        except Exception:
            pass
    raise RuntimeError("浏览器没有在 15 秒内开启 DevTools 端口")


def _prefs():
    """本机模式：下载存进工作文件夹、不弹“存到哪里”；上次没正常退出也不弹恢复提示。Chrome 没开着时才能改。"""
    downloads = os.environ.get("OSA_CDP_DOWNLOADS")
    path = os.path.join(HOST, "Default", "Preferences")
    try:
        with open(path, encoding="utf-8") as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        prefs = {}
    if downloads:
        os.makedirs(downloads, exist_ok=True)
        prefs.setdefault("download", {}).update(default_directory=downloads, prompt_for_download=False, directory_upgrade=True)
        prefs.setdefault("savefile", {})["default_directory"] = downloads
    prefs.setdefault("profile", {})["exit_type"] = "Normal"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs, f)


def _launch_host() -> list:
    global PORT
    chrome = os.environ.get("OSA_CDP_CHROME") or ""
    if not os.path.isfile(chrome):
        raise RuntimeError("没找到 Chrome：先安装 Google Chrome，或者在 config.yaml 的 computer.chrome_path 写上它的路径")
    try:
        os.remove(os.path.join(HOST, "DevToolsActivePort"))   # 上次留下的旧端口
    except OSError:
        pass
    _prefs()
    size = os.environ.get("OSA_CDP_SIZE") or "1280,860"
    argv = [chrome, f"--user-data-dir={HOST}", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            "--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble", f"--window-size={size}",
            *FLAGS, "about:blank"]
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(80):
        time.sleep(0.25)
        PORT = _read_port()
        if not PORT:
            continue
        try:
            ts = _targets()
            if ts:
                return ts
        except Exception:
            pass
    raise RuntimeError("Chrome 没有在 20 秒内开启调试端口。Bot 用的 Chrome 可能已经开着但没开调试端口，关掉它再试")


def _profile_dir(pid: int) -> str | None:
    """浏览器正在用的配置目录（登录状态在里面）：从它打开的文件里找。"""
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    for fd in fds:   # 缓存目录（~/.cache/chromium/Default/…）里也有 /Default/，有 Local State 的才是配置目录
        try:
            t = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if "/Default/" in t and os.path.isfile(os.path.join(t[:t.index("/Default/")], "Local State")):
            return t[:t.index("/Default/")]
    return None


def _upgrade(ts: list) -> list:
    """老浏览器没带 FLAGS：按原来的命令行加上参数重开一次。配置目录写明是原来那个，登录状态不会丢。
    拿不准的（别的用户开的、找不到配置目录、关不掉）就不动它，只是被挡住的窗口会慢一些。"""
    pid, args = _browser_proc()
    if not pid or all(f in args for f in FLAGS):
        return ts
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return ts
    except OSError:
        return ts
    if not any(a.startswith("--user-data-dir=") for a in args):
        d = _profile_dir(pid)
        if not d:
            return ts
        args = args[:1] + [f"--user-data-dir={d}"] + args[1:]
    try:
        os.kill(pid, 15)   # 正常退出，cookie 会存好
    except OSError:
        return ts
    for _ in range(80):
        time.sleep(0.25)
        if not _alive(pid):
            break
    else:
        return ts   # 没退出：不去硬杀，下次再说
    return _launch(args)


def _alive(pid: int) -> bool:
    """进程还在跑。退出了但没人回收的（容器里 1 号进程不管子进程时）算已经退出。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def ensure_browser(restart_ok: bool = False):
    """确保 Chromium 开着 DevTools 端口。旧的浏览器进程没开端口时重启它（登录状态在配置目录里，不会丢）。
    restart_ok：没有别人在用时，顺便把没带 FLAGS 的旧浏览器重开一次（_upgrade）。
    本机模式：浏览器是自己开的，参数都带着；连不上就开一个，不碰你别的 Chrome。"""
    try:
        ts = _targets()
        if HOST or not restart_ok or all(f in _browser_proc()[1] for f in FLAGS):
            return ts
    except Exception:
        pass
    with open(LOCK, "w") as lock:   # 几个动作同时发现要开浏览器时，只开一次
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ts = _targets()
        except Exception:
            ts = None
        if HOST:
            return ts if ts is not None else _launch()
        if ts is not None:
            return _upgrade(ts) if restart_ok else ts
        pid, _ = _browser_proc()
        if pid and _alive(pid):   # 开着端口的浏览器还在：可能只是忙（几个任务同时加载网页），多等一会儿，别杀掉
            try:
                return _targets(10)
            except Exception:
                pass
        if subprocess.run(["pgrep", "-f", "chrom"], capture_output=True).returncode == 0:
            subprocess.run(["pkill", "-f", "chrom"], capture_output=True)
            time.sleep(1.5)
        return _launch()


def page() -> WS:
    ts = ensure_browser()
    if not ts:   # 所有标签页都关了：新开一个
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT"), timeout=5) as r:
            ts = [json.load(r)]
    t = ts[0]    # 列表第一个是最近激活 / 新开的标签页（点链接新开的标签也会排到最前）
    ws = WS(t["webSocketDebuggerUrl"])
    if not HOST:
        ws.call("Page.bringToFront")
    return ws


BROWSER_ID = ""   # 这次开着的浏览器的标识：浏览器重开过，窗口编号可能和以前的撞上


def _browser_ws() -> WS:
    global BROWSER_ID
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as r:
        url = json.load(r)["webSocketDebuggerUrl"]
    BROWSER_ID = url.rsplit("/", 1)[-1]
    return WS(url)


class Ctx:
    """这次操作在哪个窗口、哪个标签页。

    win 没给时沿用老办法：最前面的标签页（演示录制用）。给了时（{"id", "avoid", "front", "keep_front", "router", "restart_ok"}）：
    - 任务带着自己窗口的 id 来；窗口没了报 WINDOW_GONE，由服务端重新挑。第一次（id 为空）挑一个没人用的窗口
      （不在 avoid 里，最近用过的优先），都有人用就新开一个，大小、位置和现在最前面的窗口一样。
    - 对话里的直接操作（router）没有自己的窗口时，用屏幕上最前面的那个；那个窗口正被任务用着就报 FRONT_BUSY，
      打开网页（open）例外，换一个空窗口或新开一个。
    - front：放到最前面。keep_front：用户固定在看的窗口，这次操作把别的窗口顶到前面时（点击会让窗口到前面）再放回去。
    """

    def __init__(self, a: dict):
        self.win, self.op = a.get("win"), a["op"]
        self.window = None
        if self.win is None:
            self.ws = page()
            return
        w = self.win
        ts = ensure_browser(bool(w.get("restart_ok")))
        self.b = _browser_ws()
        tw = self.windows(ts)
        avoid = set(w.get("avoid") or [])
        front = tw[0][1] if tw else None
        t, self.window, created = None, w.get("id"), False
        if self.window and w.get("browser") and w["browser"] != BROWSER_ID:
            raise RuntimeError(f"WINDOW_GONE {self.window}")   # 浏览器重开过，原来的窗口已经没了
        if self.window:
            t = next((t for t, x in tw if x == self.window), None)
            if t is None:
                raise RuntimeError(f"WINDOW_GONE {self.window}")
        elif w.get("router") and front is not None and front not in avoid:
            t, self.window = tw[0]
        elif w.get("router") and w.get("op", self.op) != "open":
            raise RuntimeError(f"FRONT_BUSY {front}")
        else:
            t, self.window = next(((t, x) for t, x in tw if x not in avoid), (None, None))
            if t is None:
                t, self.window = self.new_window(front)
                created = True
        self.ws = WS(t["webSocketDebuggerUrl"])
        if HOST:
            pass   # 本机模式不把窗口提到前面：你可能正在用别的程序（要看就点“显示窗口”，即 op front）
        elif w.get("front"):
            self.ws.call("Page.bringToFront")
        elif created and front is not None:   # 新窗口会抢到最前面：不该它在前面时把原来的放回去
            self.to_front(front)
        self.seen = {o["id"] for o, _ in tw} | {t["id"]}                            # 动作之前所有的标签页
        self.before = {o["id"] for o, x in tw if x == self.window} | {t["id"]}     # 其中自己窗口里的

    def windows(self, ts: list) -> list[tuple[dict, int]]:
        """每个标签页和它所在的窗口，按最近激活排序（每个窗口里排在最前的就是它正显示的标签页）。"""
        out = []
        for t in ts:
            try:
                out.append((t, self.b.call("Browser.getWindowForTarget", {"targetId": t["id"]})["windowId"]))
            except Exception:
                pass   # 刚关掉的标签页
        return out

    def new_window(self, like: int | None) -> tuple[dict, int]:
        tid = self.b.call("Target.createTarget", {"url": "about:blank", "newWindow": True,
                                                  **({"background": True} if HOST else {})})["targetId"]
        w = self.b.call("Browser.getWindowForTarget", {"targetId": tid})["windowId"]
        if like is not None:   # 和已有的窗口一样大、一样位置，屏幕上看起来像同一个浏览器
            try:
                bd = self.b.call("Browser.getWindowBounds", {"windowId": like})["bounds"]
                st = bd.get("windowState", "normal")
                self.b.call("Browser.setWindowBounds", {"windowId": w, "bounds": {k: bd[k] for k in ("left", "top", "width", "height")}
                                                        if st == "normal" else {"windowState": st}})
            except Exception:
                pass
        for _ in range(20):
            t = next((t for t in _targets() if t["id"] == tid), None)
            if t:
                return t, w
            time.sleep(0.1)
        raise RuntimeError("新窗口没有打开")

    def to_front(self, window: int):
        t = next((t for t, x in self.windows(_targets()) if x == window), None)
        if t:
            WS(t["webSocketDebuggerUrl"]).call("Page.bringToFront")

    def again(self) -> WS:
        """动作之后重新找这个窗口正显示的标签页：点链接可能新开了标签页，或者弹出了新窗口（登录窗口这类）。"""
        if self.win is None:
            return page()
        tw = self.windows(_targets())
        avoid = set(self.win.get("avoid") or [])
        for t, x in tw:   # 这次动作弹出的新窗口：是从自己窗口里的页面打开的就跟过去（别的任务弹出的不管）
            if t["id"] not in self.seen and x != self.window and x not in avoid:
                try:
                    opener = self.b.call("Target.getTargetInfo", {"targetId": t["id"]})["targetInfo"].get("openerId")
                except Exception:
                    opener = None
                if opener in self.before:
                    self.window = x
                    break
        t = next((t for t, x in tw if x == self.window), None)
        if t is None:
            # 动作做完窗口自己关了（登录弹窗提交后关掉这类）：回到一个没人用的窗口（一般就是打开它的那个）。
            # 不能报 WINDOW_GONE，否则服务端会把刚做过的动作在别的页面上再做一遍
            t, self.window = next(((t, x) for t, x in tw if x not in avoid), (None, None))
            if t is None:
                raise RuntimeError("WINDOW_CLOSED 窗口在操作之后被页面关掉了，用 browser_open 重新打开要用的网页")
        self.seen = {o["id"] for o, _ in tw}
        self.before = {o["id"] for o, x in tw if x == self.window}
        self.ws = WS(t["webSocketDebuggerUrl"])
        return self.ws

    def done(self):
        """用户固定在看别的窗口时，把它放回最前面（点击会让操作的窗口跑到前面）。"""
        k = (self.win or {}).get("keep_front")
        if k and not HOST and k != self.window and self.op in ("open", "click", "type", "click_xy", "upload"):
            try:
                self.to_front(k)
            except Exception:
                pass


def js(ws: WS, expr: str):
    r = ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in r:
        raise RuntimeError("页面脚本出错：" + str(r["exceptionDetails"].get("exception", {}).get("description", ""))[:200])
    return r.get("result", {}).get("value")


def settle(ws: WS, timeout: float = 15):
    """等页面加载完，再多等一点让脚本渲染。"""
    time.sleep(0.3)
    end = time.time() + timeout
    while time.time() < end:
        try:
            if js(ws, "document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.3)
    time.sleep(0.5)


def snapshot(ws: WS) -> dict:
    return json.loads(js(ws, SNAPSHOT_JS))


def center(ws: WS, ref) -> tuple[float, float]:
    r = js(ws, f"""(() => {{ const e = document.querySelector('[data-osa-ref="{int(ref)}"]');
      if (!e) return null; e.scrollIntoView({{block: 'center', inline: 'center'}});
      const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
    if not r:
        raise RuntimeError(f"找不到元素 [{ref}]，页面可能变了，先用 browser_snapshot 重新看一下")
    return r


KEYS = {"enter": ("Enter", 13, "\r"), "tab": ("Tab", 9, ""), "escape": ("Escape", 27, ""), "esc": ("Escape", 27, ""),
        "backspace": ("Backspace", 8, ""), "delete": ("Delete", 46, ""), "arrowdown": ("ArrowDown", 40, ""),
        "down": ("ArrowDown", 40, ""), "arrowup": ("ArrowUp", 38, ""), "up": ("ArrowUp", 38, ""),
        "arrowleft": ("ArrowLeft", 37, ""), "left": ("ArrowLeft", 37, ""), "arrowright": ("ArrowRight", 39, ""),
        "right": ("ArrowRight", 39, ""), "pagedown": ("PageDown", 34, ""), "pageup": ("PageUp", 33, ""),
        "home": ("Home", 36, ""), "end": ("End", 35, ""), "space": (" ", 32, " ")}


def key(ws: WS, name: str):
    k = KEYS.get(name.lower().replace(" ", ""))
    if not k:
        raise RuntimeError(f"不支持的按键 {name}；组合键请用 computer_key")
    base = {"key": k[0], "code": k[0] if len(k[0]) > 1 else "Space", "windowsVirtualKeyCode": k[1]}
    ws.call("Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": k[2]} if k[2] else {})})
    ws.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})


MODS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "super": 4, "win": 4, "shift": 8}


def press(ws: WS, combo: str):
    """按键或组合键（ctrl+a、shift+Tab、Enter、F5），发给页面。浏览器自己的快捷键（新标签页、地址栏）不起作用。"""
    combo = combo.replace(" ", "")
    parts = [x for x in combo.split("+") if x]
    if combo.endswith("++") or combo == "+":
        parts.append("+")
    if not parts:
        raise RuntimeError("没有按键")
    mods = 0
    for m in parts[:-1]:
        if m.lower() not in MODS:
            raise RuntimeError(f"不认识的修饰键 {m}")
        mods |= MODS[m.lower()]
    k = parts[-1]
    low = k.lower()
    if low in KEYS:
        name, vk, text = KEYS[low]
        code = name if len(name) > 1 else "Space"
    elif low in ("return",):
        name, vk, text, code = "Enter", 13, "\r", "Enter"
    elif low == "del":
        name, vk, text, code = "Delete", 46, "", "Delete"
    elif low[:1] == "f" and low[1:].isdigit() and 1 <= int(low[1:]) <= 12:
        name, vk, text, code = k.upper(), 111 + int(low[1:]), "", k.upper()
    elif len(k) == 1:
        name = k.upper() if mods & 8 and k.isalpha() else k
        vk = ord(k.upper()) if k.isalnum() and k.isascii() else 0
        code = ("Key" + k.upper()) if k.isalpha() and k.isascii() else ("Digit" + k) if k.isdigit() else ""
        text = name
    elif low in MODS:
        name, vk, text, code = {1: "Alt", 2: "Control", 4: "Meta", 8: "Shift"}[MODS[low]], 0, "", ""
    else:
        raise RuntimeError(f"不认识的按键 {k}")
    if mods & 7:
        text = ""   # 带 ctrl / alt 的组合键不输入字符
    base = {"key": name, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mods}
    ws.call("Input.dispatchKeyEvent", {"type": "keyDown" if text else "rawKeyDown", **base, **({"text": text} if text else {})})
    ws.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})


# ---------- 演示录制 ----------
def record(path: str, max_s: float):
    """后台进程：连到浏览器，自动附加到所有标签页（包括之后新开的），把操作一行一个 JSON 写进 path。"""
    import select
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ensure_browser()
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as r:
        ws = WS(json.load(r)["webSocketDebuggerUrl"])
    out = open(path, "a", buffering=1, encoding="utf-8")
    n = 0

    def send(method: str, params: dict | None = None, session: str | None = None):
        nonlocal n
        n += 1
        ws.send(json.dumps({"id": n, "method": method, "params": params or {}, **({"sessionId": session} if session else {})}))

    def emit(ev: dict):
        ev["t"] = round(time.time(), 2)
        out.write(json.dumps(ev, ensure_ascii=False) + "\n")

    send("Target.setDiscoverTargets", {"discover": True})
    send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
    urls: dict[str, str] = {}
    started = time.time()
    end = started + max_s
    while time.time() < end:
        if not ws.buf and not select.select([ws.sock], [], [], 1)[0]:
            continue   # 先确认有数据再读，免得读到一半超时把帧读乱
        m = json.loads(ws.recv())
        method, p = m.get("method"), m.get("params") or {}
        if method == "Target.attachedToTarget" and p.get("targetInfo", {}).get("type") == "page":
            sid = p["sessionId"]
            send("Runtime.addBinding", {"name": "__osaRec"}, sid)
            send("Runtime.enable", {}, sid)
            send("Page.enable", {}, sid)    # 不开 Page 域，下面的脚本不会注入到之后打开的页面
            send("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER_JS}, sid)
            send("Runtime.evaluate", {"expression": RECORDER_JS}, sid)
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            t = p.get("targetInfo") or {}
            url = t.get("url", "")
            if t.get("type") == "page" and urls.get(t["targetId"]) != url:
                # 开始录制时已经开着的标签页也会报一次 targetCreated：只记下网址，不算操作（起始页由 rec_start 记）
                if url.startswith("http") and not (method == "Target.targetCreated" and time.time() - started < 2):
                    emit({"type": "navigate", "url": url[:300], "title": t.get("title", "")[:100],
                          "new_tab": method == "Target.targetCreated"})
                urls[t["targetId"]] = url
        elif method == "Runtime.bindingCalled" and p.get("name") == "__osaRec":
            try:
                emit(json.loads(p.get("payload") or "{}"))
            except ValueError:
                pass


def rec_start(path: str, max_s: float) -> dict:
    rec_stop(path, keep=False)
    ts = ensure_browser()
    if ts and ts[0]["url"].startswith("http"):   # 最前面的标签页就是演示开始时的页面
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "navigate", "url": ts[0]["url"][:300], "title": ts[0].get("title", "")[:100],
                                "start": True, "t": round(time.time(), 2)}, ensure_ascii=False) + "\n")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--record", path, str(max_s)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(path + ".err", "w"),
                         start_new_session=True)
    with open(path + ".pid", "w") as f:
        f.write(str(p.pid))
    time.sleep(1.0)
    if p.poll() is not None:
        raise RuntimeError("录制没能启动：" + open(path + ".err").read()[-300:])
    return {"ok": True}


def rec_stop(path: str, keep: bool = True) -> dict:
    """停掉录制进程，返回录到的操作，然后删掉临时文件。"""
    try:
        pid = int(open(path + ".pid").read())
        os.kill(pid, 15)
        for _ in range(20):
            time.sleep(0.1)
            os.kill(pid, 0)
    except (OSError, ValueError):
        pass   # 进程已经结束（比如到了时长上限）
    events = []
    if keep and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    for f in (path, path + ".pid", path + ".err"):
        try:
            os.remove(f)
        except OSError:
            pass
    return {"events": events}


def close_window(a: dict) -> dict:
    """任务做完后，窗口多于 keep 个就关掉它的窗口。"""
    try:
        ts = _targets()
    except Exception:
        return {"closed": False}
    c = Ctx.__new__(Ctx)
    c.b = _browser_ws()
    if a.get("browser") and a["browser"] != BROWSER_ID:
        return {"closed": False}   # 浏览器重开过，这个编号已经不是那个窗口了
    tw = c.windows(ts)
    if a["window"] not in {x for _, x in tw} or len({x for _, x in tw}) <= int(a.get("keep") or 1):
        return {"closed": False}
    for t, x in tw:
        if x == a["window"]:
            c.b.call("Target.closeTarget", {"targetId": t["id"]})
    return {"closed": True}


def click_at(ws: WS, x: float, y: float, button: str = "left", count: int = 1):
    ws.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    for n in range(1, count + 1):
        for t in ("mousePressed", "mouseReleased"):
            ws.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": button, "clickCount": n})


def upload(ws: WS, ref, files: list[str]):
    """点一下上传按钮或文件输入框，把电脑里的文件交给弹出的文件选择框（不会真的弹出来）。"""
    for f in files:
        if not os.path.isfile(f):
            raise RuntimeError(f"文件不存在：{f}")
    ws.call("Page.enable")
    ws.call("Page.setInterceptFileChooserDialog", {"enabled": True})
    click_at(ws, *center(ws, ref))
    ev = ws.wait_event("Page.fileChooserOpened", 5)
    if not ev:
        raise RuntimeError(f"点了 [{ref}] 没有弹出选择文件；找页面上的“上传 / 选择文件”按钮或文件输入框再试")
    if len(files) > 1 and ev.get("mode") != "selectMultiple":
        raise RuntimeError("这里一次只能选一个文件")
    ws.call("DOM.setFileInputFiles", {"files": files, "backendNodeId": ev["backendNodeId"]})
    ws.call("Page.setInterceptFileChooserDialog", {"enabled": False})


# 这一步算不算“提交”（本机电脑默认要先问你）：点“发送 / 发布 / 付款”这类按钮、POST 表单的提交按钮，
# 或者在发消息的输入框（多行、富文本）、POST 表单里回车。真链接、搜索框不算。参数 [ref, x, y, mode]，mode 是 click / enter
INSPECT_JS = r"""
(([ref, x, y, mode]) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  let e = ref !== null ? document.querySelector('[data-osa-ref="' + ref + '"]')
        : mode === 'enter' ? document.activeElement : document.elementFromPoint(x, y);
  while (mode === 'enter' && e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e || !e.closest) return {submit: false, what: ''};
  const form = e.form || e.closest('form');
  const post = !!form && (form.getAttribute('method') || '').toLowerCase() === 'post';
  if (mode === 'enter') {
    const name = e.getAttribute('name') || '';
    const search = e.type === 'search' || /^(q|query|search|keywords?|wd|kw|s)$/i.test(name) || !!e.closest('[role=search]');
    const multi = e.tagName === 'TEXTAREA' || e.isContentEditable;
    const label = clean(e.getAttribute('aria-label') || e.getAttribute('placeholder') || name).slice(0, 40);
    return {submit: !search && (multi || post), what: '在' + (label ? '「' + label + '」' : '输入框') + '里按回车'};
  }
  const el = e.closest('button,a[href],input[type=submit],input[type=button],input[type=image],[role=button],[role=menuitem],[onclick]') || e;
  const label = clean(el.innerText || el.value || el.getAttribute('aria-label') || el.title || '');
  const link = el.tagName === 'A' && !/^(#|javascript:)/i.test(el.getAttribute('href') || '#') && el.getAttribute('role') !== 'button';
  const submitBtn = el.matches('button[type=submit],input[type=submit],input[type=image]') || (el.tagName === 'BUTTON' && !el.getAttribute('type') && !!form);
  const words = label.length <= 40 && (/发送|发布|发表|提交|确认|支付|付款|购买|下单|结算|删除|转账|回复/.test(label) ||
    /\b(send|post|publish|submit|confirm|pay|buy|order|checkout|delete|transfer|reply|tweet)\b/i.test(label));
  return {submit: !link && (words || submitBtn && post), what: '点击「' + (label.slice(0, 40) || el.tagName.toLowerCase()) + '」'};
})(%s)
"""


def shot(ws: WS, fmt: str = "png") -> bytes:
    """截当前标签页看得到的部分。高分屏上按页面像素截（不是两倍大），点击坐标才对得上。"""
    params: dict = {"format": fmt, **({"quality": 70} if fmt == "jpeg" else {})}
    try:
        dpr, w, h, x, y = js(ws, "[devicePixelRatio, innerWidth, innerHeight, visualViewport ? visualViewport.pageLeft : scrollX, "
                                 "visualViewport ? visualViewport.pageTop : scrollY]")
        if abs(dpr - 1) > 0.01:
            params["clip"] = {"x": x, "y": y, "width": w, "height": h, "scale": 1 / dpr}
    except Exception:
        pass   # 页面上跑不了脚本（PDF、浏览器自己的页面）：按原样截
    return base64.b64decode(ws.call("Page.captureScreenshot", params)["data"])


def peek(a: dict) -> dict:
    """给界面看的画面：某个窗口（或最近用过的标签页）现在的样子。不开浏览器、不挑窗口、不抢焦点。"""
    ts = _targets()
    t = None
    if a.get("window"):
        c = Ctx.__new__(Ctx)
        c.b = _browser_ws()
        if not a.get("browser") or a["browser"] == BROWSER_ID:
            t = next((t for t, x in c.windows(ts) if x == a["window"]), None)
    t = t or (ts[0] if ts else None)
    if not t:
        raise RuntimeError("浏览器里没有打开的页面")
    data = shot(WS(t["webSocketDebuggerUrl"]), a.get("format") or "png")
    return {"data": base64.b64encode(data).decode(), "url": t.get("url", ""), "title": t.get("title", "")}


def run(a: dict) -> dict:
    op = a["op"]
    if op == "ensure":   # 只是确保浏览器开着
        ensure_browser()
        _browser_ws()
        return {"ok": True, "browser": BROWSER_ID}
    if op == "quit":
        try:
            _targets()
            _browser_ws().call("Browser.close")
        except Exception:
            pass   # 本来就没开
        return {"ok": True}
    if op == "peek":
        return peek(a)
    if op == "rec_start":
        return rec_start(a["file"], float(a.get("max_s") or 600))
    if op == "rec_stop":
        return rec_stop(a["file"])
    if op == "close_window":
        return close_window(a)
    ctx = Ctx(a)
    ws = ctx.ws
    out = None
    if op == "front":
        ws.call("Page.bringToFront")
        out = {"ok": True}
    elif op == "window":   # 只挑窗口
        out = {"ok": True}
    elif op == "inspect":   # 动作之前看一眼要点的元素（见 INSPECT_JS）
        ref = a.get("ref")
        args = [None if ref is None else int(ref), float(a.get("x") or 0), float(a.get("y") or 0), a.get("mode") or "click"]
        out = {**(js(ws, INSPECT_JS % json.dumps(args)) or {"submit": False, "what": ""}),
               "url": js(ws, "location.href"), "title": js(ws, "document.title")}
    elif op == "open":
        ws.call("Page.enable")
        ws.call("Page.navigate", {"url": a["url"]})
        settle(ws)
    elif op == "click":
        x, y = center(ws, a["ref"])
        click_at(ws, x, y)
        settle(ws, 8)
        ws = ctx.again()   # 点链接可能新开了标签页
    elif op == "type":
        if a.get("ref") is not None:
            x, y = center(ws, a["ref"])
            for t in ("mousePressed", "mouseReleased"):
                ws.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
            if a.get("clear", True):
                js(ws, f"""(() => {{ const e = document.querySelector('[data-osa-ref="{int(a['ref'])}"]');
                  if (e && 'select' in e && typeof e.select === 'function') e.select();
                  else document.execCommand('selectAll'); }})()""")
        ws.call("Input.insertText", {"text": a["text"]})   # 直接插入文字，中文也不会丢字
        if a.get("submit"):
            key(ws, "enter")
            settle(ws, 10)
            ws = ctx.again()
        else:
            time.sleep(0.4)
        if a.get("ref") is None:   # computer_type：只是在焦点处输入
            out = {"ok": True}
    elif op == "key":
        key(ws, a.get("name") or a.get("key") or "")
        settle(ws, 8)
    elif op == "upload":
        upload(ws, a["ref"], [str(f) for f in (a.get("files") or [])])
        time.sleep(0.8)
    elif op == "scroll":
        d = {"down": "0, innerHeight * 0.8", "up": "0, -innerHeight * 0.8"}.get(a.get("direction", "down"), "0, innerHeight * 0.8")
        js(ws, f"window.scrollBy({d})")
        time.sleep(0.5)
    elif op == "back":
        tabs = len(_targets()) if ctx.win is None else sum(1 for _, x in ctx.windows(_targets()) if x == ctx.window)
        if js(ws, "history.length") <= 1 and tabs > 1:
            ws.call("Page.close")    # 点链接新开的标签页没有历史：关掉它，回到原来的标签页
            time.sleep(0.5)
            ws = ctx.again()
        else:
            js(ws, "history.back()")
            settle(ws, 10)
    elif op == "read":
        start = int(a.get("offset") or 0)
        text = js(ws, "document.body ? document.body.innerText : ''") or ""
        out = {"url": js(ws, "location.href"), "title": js(ws, "document.title"), "total": len(text),
               "offset": start, "text": text[start:start + 12000]}
    # 下面几个是 computer_* 工具：按截图上的像素坐标操作页面
    elif op == "screenshot":
        data = shot(ws)
        if HOST:   # 本机模式：脚本和服务在同一台电脑上，直接带回去
            out = {"data": base64.b64encode(data).decode()}
        else:
            path = f"/tmp/osa-shot-{time.time_ns()}.png"
            with open(path, "wb") as f:
                f.write(data)
            out = {"path": path}
    elif op == "click_xy":
        click_at(ws, float(a["x"]), float(a["y"]), a.get("button") or "left", 2 if a.get("double") else 1)
        settle(ws, 8)
        ws = ctx.again()
        out = {"ok": True}
    elif op == "press":
        press(ws, str(a["keys"]))
        settle(ws, 8)
        ws = ctx.again()
        out = {"ok": True}
    elif op == "wheel":
        w, h = js(ws, "[innerWidth, innerHeight]")
        n = max(1, min(100, int(a.get("amount") or 3))) * 100
        dx, dy = {"down": (0, n), "up": (0, -n), "left": (-n, 0), "right": (n, 0)}.get(a.get("direction") or "down", (0, n))
        ws.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": w / 2, "y": h / 2, "deltaX": dx, "deltaY": dy})
        time.sleep(0.5)
        out = {"ok": True}
    elif op != "snapshot":
        raise RuntimeError(f"未知操作 {op}")
    ctx.done()
    out = snapshot(ws) if out is None else out
    out["window"] = ctx.window
    out["browser"] = BROWSER_ID
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["--record"]:
        record(sys.argv[2], float(sys.argv[3]))
        sys.exit(0)
    try:
        print(json.dumps(run(json.loads(sys.stdin.read())), ensure_ascii=False))
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
