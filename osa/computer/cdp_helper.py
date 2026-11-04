"""在电脑里运行的浏览器控制脚本：通过 Chrome DevTools Protocol（CDP）操作 Chromium。

服务端把它写进电脑，然后每个动作执行一次：stdin 传 JSON {"op": ..., ...}，stdout 返回 JSON。
只用 Python 标准库（电脑里不一定能装包），自带一个最小的 WebSocket 客户端。
DevTools 端口只监听电脑内部的 127.0.0.1，不对外暴露。
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

PORT = 9222
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
        raise TimeoutError(method)


# ---------- 浏览器 ----------
def _targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
        return [t for t in json.load(r) if t.get("type") == "page" and not t["url"].startswith("devtools://")]


def ensure_browser():
    """确保 Chromium 开着 DevTools 端口。旧的浏览器进程没开端口时重启它（登录状态在配置目录里，不会丢）。"""
    try:
        return _targets()
    except Exception:
        pass
    b = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not b:
        raise RuntimeError("电脑里没有浏览器")
    if subprocess.run(["pgrep", "-f", "chrom"], capture_output=True).returncode == 0:
        subprocess.run(["pkill", "-f", "chrom"], capture_output=True)
        time.sleep(1.5)
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")}
    subprocess.Popen([b, f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1",
                      "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
                      "--password-store=basic", "about:blank"],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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


def page() -> WS:
    ts = ensure_browser()
    if not ts:   # 所有标签页都关了：新开一个
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT"), timeout=5) as r:
            ts = [json.load(r)]
    t = ts[0]    # 列表第一个是最近激活 / 新开的标签页（点链接新开的标签也会排到最前）
    ws = WS(t["webSocketDebuggerUrl"])
    ws.call("Page.bringToFront")
    return ws


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


def run(a: dict) -> dict:
    op = a["op"]
    ws = page()
    if op == "open":
        ws.call("Page.enable")
        ws.call("Page.navigate", {"url": a["url"]})
        settle(ws)
    elif op == "click":
        x, y = center(ws, a["ref"])
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            ws.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
        settle(ws, 8)
        ws = page()   # 点链接可能新开了标签页
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
            ws = page()
        else:
            time.sleep(0.4)
    elif op == "key":
        key(ws, a["key"])
        settle(ws, 8)
    elif op == "scroll":
        d = {"down": "0, innerHeight * 0.8", "up": "0, -innerHeight * 0.8"}.get(a.get("direction", "down"), "0, innerHeight * 0.8")
        js(ws, f"window.scrollBy({d})")
        time.sleep(0.5)
    elif op == "back":
        if js(ws, "history.length") <= 1 and len(_targets()) > 1:
            ws.call("Page.close")    # 点链接新开的标签页没有历史：关掉它，回到原来的标签页
            time.sleep(0.5)
            ws = page()
        else:
            js(ws, "history.back()")
            settle(ws, 10)
    elif op == "read":
        start = int(a.get("offset") or 0)
        text = js(ws, "document.body ? document.body.innerText : ''") or ""
        return {"url": js(ws, "location.href"), "title": js(ws, "document.title"), "total": len(text),
                "offset": start, "text": text[start:start + 12000]}
    elif op != "snapshot":
        raise RuntimeError(f"未知操作 {op}")
    return snapshot(ws)


if __name__ == "__main__":
    try:
        print(json.dumps(run(json.loads(sys.stdin.read())), ensure_ascii=False))
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
