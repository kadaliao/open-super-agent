"""macOS 的整个屏幕、真实的鼠标键盘（本机电脑打开“操作整台电脑”时，host.py 用它）。

只用系统自带的东西，不装依赖：
- 截图：screencapture（只截主显示器，带鼠标指针）+ sips 缩小。给模型的图缩到 1280×800 以内，模型按图上的坐标点，这里换算成屏幕坐标。
- 鼠标键盘：CoreGraphics 发事件（ctypes 调系统库）。
- 打字：写进剪贴板再按 ⌘V，中文输入法开着也不会被它拦下；粘贴完把原来的剪贴板放回去，剪贴板管理器不记这一条。
- 前台 App、窗口标题、鼠标下面是什么按钮：辅助功能接口（AX）。

要两个权限：辅助功能（发鼠标键盘事件、读界面元素）和屏幕录制（截到别的 App 的窗口）。
在桌面 App 里运行时，服务是 App 启动的子进程，权限记在 Open Super Agent 名下；从终端跑就记在终端名下。

函数都是同步的（很快，最多几百毫秒），host.py 放到线程里调。不是 macOS 时 supported() 为 False，别的函数不要调。
"""
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from ctypes import POINTER, Structure, byref, c_bool, c_char_p, c_double, c_float, c_int32, c_int64, c_long, c_uint16, \
    c_uint32, c_uint64, c_ulong, c_void_p

MAX_W, MAX_H = 1280, 800   # 给模型的截图最大尺寸
UTF8 = 0x08000100
SETTINGS = {"accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"}


def supported() -> bool:
    return sys.platform == "darwin"


class Point(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class Size(Structure):
    _fields_ = [("w", c_double), ("h", c_double)]


class Rect(Structure):
    _fields_ = [("origin", Point), ("size", Size)]


class _Libs:
    """第一次用时才加载系统库（不是 macOS 的机器上 import 这个文件也没事）。"""

    def __init__(self):
        fw = "/System/Library/Frameworks/{0}.framework/{0}"
        self.CG = CG = ctypes.cdll.LoadLibrary(fw.format("CoreGraphics"))
        self.CF = CF = ctypes.cdll.LoadLibrary(fw.format("CoreFoundation"))
        self.AX = AX = ctypes.cdll.LoadLibrary(fw.format("ApplicationServices"))
        self.objc = objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary(fw.format("AppKit"))   # NSPasteboard
        sig = {
            CF: {"CFRelease": (None, [c_void_p]), "CFRetain": (c_void_p, [c_void_p]),
                 "CFStringCreateWithCString": (c_void_p, [c_void_p, c_char_p, c_uint32]),
                 "CFStringGetLength": (c_long, [c_void_p]),
                 "CFStringGetCString": (c_bool, [c_void_p, c_char_p, c_long, c_uint32]),
                 "CFGetTypeID": (c_ulong, [c_void_p]), "CFStringGetTypeID": (c_ulong, []),
                 "CFNumberGetTypeID": (c_ulong, []), "CFBooleanGetTypeID": (c_ulong, []),
                 "CFNumberGetValue": (c_bool, [c_void_p, c_long, c_void_p]), "CFBooleanGetValue": (c_bool, [c_void_p]),
                 "CFDictionaryGetValue": (c_void_p, [c_void_p, c_void_p]),
                 "CFDictionaryCreate": (c_void_p, [c_void_p, POINTER(c_void_p), POINTER(c_void_p), c_long, c_void_p, c_void_p]),
                 "CFArrayGetCount": (c_long, [c_void_p]), "CFArrayGetValueAtIndex": (c_void_p, [c_void_p, c_long])},
            CG: {"CGPreflightScreenCaptureAccess": (c_bool, []), "CGRequestScreenCaptureAccess": (c_bool, []),
                 "CGMainDisplayID": (c_uint32, []), "CGDisplayBounds": (Rect, [c_uint32]),
                 "CGDisplayIsAsleep": (c_bool, [c_uint32]),
                 "CGEventCreate": (c_void_p, [c_void_p]), "CGEventGetLocation": (Point, [c_void_p]),
                 "CGEventSourceCreate": (c_void_p, [c_int32]),
                 "CGEventCreateMouseEvent": (c_void_p, [c_void_p, c_uint32, Point, c_uint32]),
                 "CGEventCreateKeyboardEvent": (c_void_p, [c_void_p, c_uint16, c_bool]),
                 "CGEventCreateScrollWheelEvent2": (c_void_p, [c_void_p, c_uint32, c_uint32, c_int32, c_int32, c_int32]),
                 "CGEventSetIntegerValueField": (None, [c_void_p, c_uint32, c_int64]),
                 "CGEventSetFlags": (None, [c_void_p, c_uint64]), "CGEventPost": (None, [c_uint32, c_void_p]),
                 "CGEventSourceSecondsSinceLastEventType": (c_double, [c_int32, c_uint32]),
                 "CGSessionCopyCurrentDictionary": (c_void_p, []),
                 "CGWindowListCopyWindowInfo": (c_void_p, [c_uint32, c_uint32]),
                 "CGRectMakeWithDictionaryRepresentation": (c_bool, [c_void_p, POINTER(Rect)])},
            AX: {"AXIsProcessTrusted": (c_bool, []), "AXIsProcessTrustedWithOptions": (c_bool, [c_void_p]),
                 "AXUIElementCreateSystemWide": (c_void_p, []),
                 "AXUIElementCopyAttributeValue": (c_int32, [c_void_p, c_void_p, POINTER(c_void_p)]),
                 "AXUIElementCopyElementAtPosition": (c_int32, [c_void_p, c_float, c_float, POINTER(c_void_p)]),
                 "AXUIElementGetPid": (c_int32, [c_void_p, POINTER(c_int32)]),
                 "AXUIElementSetMessagingTimeout": (c_int32, [c_void_p, c_float])},
            objc: {"objc_getClass": (c_void_p, [c_char_p]), "sel_registerName": (c_void_p, [c_char_p]),
                   "objc_autoreleasePoolPush": (c_void_p, []), "objc_autoreleasePoolPop": (None, [c_void_p])},
        }
        for lib, fns in sig.items():
            for name, (res, args) in fns.items():
                f = getattr(lib, name)
                f.restype, f.argtypes = res, args
        self.src = CG.CGEventSourceCreate(-1)   # 私有的事件源：你按着的修饰键不会混进 Bot 的按键
        self.sys = AX.AXUIElementCreateSystemWide()
        AX.AXUIElementSetMessagingTimeout(self.sys, 1.5)   # 哪个 App 卡住了也别等太久
        self.warm()
        self._msg: int = ctypes.cast(objc.objc_msgSend, c_void_p).value or 0
        self._protos: dict = {}
        self._strs: dict[str, int] = {}

    def warm(self):
        """系统级元素刚建好时问“前台是哪个 App”会失败（kAXErrorCannotComplete），先按坐标查一次元素就好了。"""
        el = c_void_p()
        if self.AX.AXUIElementCopyElementAtPosition(self.sys, 1.0, 1.0, byref(el)) == 0 and el.value:
            self.CF.CFRelease(el.value)

    def cfstr(self, s: str) -> int:
        """常用的 CFString 键，建一次一直用。"""
        if s not in self._strs:
            self._strs[s] = self.CF.CFStringCreateWithCString(None, s.encode(), UTF8)
        return self._strs[s]

    def msg(self, obj, sel: str, *args, restype: Any = c_void_p, argtypes: tuple = ()):
        """objc_msgSend：每种参数类型组合一个函数原型（arm64 上必须按真实类型调）。"""
        key = (restype, tuple(argtypes))
        if key not in self._protos:
            self._protos[key] = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(self._msg)
        return self._protos[key](obj, self.objc.sel_registerName(sel.encode()), *args)

    def cls(self, name: str) -> int:
        return self.objc.objc_getClass(name.encode())


_L: _Libs | None = None
_lock = threading.Lock()   # 真实的鼠标键盘、剪贴板一次只做一件事


def L() -> _Libs:
    global _L
    if _L is None:
        _L = _Libs()
    return _L


# ---------- CoreFoundation 小工具 ----------
def _py(v):
    """CFString / CFNumber / CFBoolean → Python 值；别的类型返回 None。"""
    if not v:
        return None
    CF = L().CF
    t = CF.CFGetTypeID(v)
    if t == CF.CFStringGetTypeID():
        n = CF.CFStringGetLength(v) * 4 + 1
        buf = ctypes.create_string_buffer(n)
        return buf.value.decode(errors="replace") if CF.CFStringGetCString(v, buf, n, UTF8) else ""
    if t == CF.CFNumberGetTypeID():
        d = c_double()
        CF.CFNumberGetValue(v, 13, byref(d))   # kCFNumberDoubleType
        return d.value
    if t == CF.CFBooleanGetTypeID():
        return bool(CF.CFBooleanGetValue(v))
    return None


def _get(d, key: str):
    return L().CF.CFDictionaryGetValue(d, L().cfstr(key)) if d else None


def _ax(el, attr: str):
    """读一个界面元素的属性，返回 CF 对象（调用方 CFRelease）或 None。"""
    out = c_void_p()
    if el and L().AX.AXUIElementCopyAttributeValue(el, L().cfstr(attr), byref(out)) == 0:
        return out.value
    return None


def _ax_text(el, attr: str) -> str:
    v = _ax(el, attr)
    try:
        s = _py(v)
        return s.strip() if isinstance(s, str) else ""
    finally:
        if v:
            L().CF.CFRelease(v)


def _pid(el) -> int:
    p = c_int32()
    return p.value if el and L().AX.AXUIElementGetPid(el, byref(p)) == 0 else 0


# ---------- 权限和状态 ----------
def permissions() -> dict:
    return {"accessibility": bool(L().AX.AXIsProcessTrusted()), "screen": bool(L().CG.CGPreflightScreenCaptureAccess())}


def request_permissions() -> dict:
    """弹系统的授权提示（每个 App 只弹一次），再把还没打开的那几项的系统设置页打开。"""
    l = L()
    p = permissions()
    if not p["accessibility"]:
        key = c_void_p(c_void_p.in_dll(l.AX, "kAXTrustedCheckOptionPrompt").value)
        val = c_void_p(c_void_p.in_dll(l.CF, "kCFBooleanTrue").value)
        d = l.CF.CFDictionaryCreate(None, byref(key), byref(val), 1,
                                    ctypes.addressof(ctypes.c_char.in_dll(l.CF, "kCFTypeDictionaryKeyCallBacks")),
                                    ctypes.addressof(ctypes.c_char.in_dll(l.CF, "kCFTypeDictionaryValueCallBacks")))
        l.AX.AXIsProcessTrustedWithOptions(d)
        l.CF.CFRelease(d)
    if not p["screen"]:
        l.CG.CGRequestScreenCaptureAccess()
    for k, url in SETTINGS.items():
        if not p[k]:
            subprocess.run(["open", url], check=False, timeout=10)
    return permissions()


def locked() -> bool:
    """屏幕锁着（或者显示器睡了）：这时候打字会打进锁屏的密码框，什么都不能做。"""
    l = L()
    d = l.CG.CGSessionCopyCurrentDictionary()
    try:
        if d and _py(_get(d, "CGSSessionScreenIsLocked")):
            return True
    finally:
        if d:
            l.CF.CFRelease(d)
    return bool(l.CG.CGDisplayIsAsleep(l.CG.CGMainDisplayID()))


def idle() -> float:
    """距离最后一次键盘鼠标输入（人或程序发的都算）过了多少秒。"""
    return float(L().CG.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF))   # HID 状态，任意输入


def screen() -> tuple[float, float, int, int]:
    """主显示器的大小（点）和给模型的截图尺寸。"""
    b = L().CG.CGDisplayBounds(L().CG.CGMainDisplayID())
    w, h = b.size.w, b.size.h
    s = min(1.0, MAX_W / w, MAX_H / h)
    return w, h, max(1, round(w * s)), max(1, round(h * s))


def to_screen(x: float, y: float) -> tuple[float, float]:
    """截图上的坐标 → 屏幕坐标（点）。超出截图的夹回到屏幕里。"""
    w, h, iw, ih = screen()
    return min(max(0.0, x * w / iw), w - 1), min(max(0.0, y * h / ih), h - 1)


def from_screen(x: float, y: float) -> tuple[int, int]:
    w, h, iw, ih = screen()
    return round(x * iw / w), round(y * ih / h)


def cursor() -> tuple[float, float]:
    l = L()
    e = l.CG.CGEventCreate(None)
    try:
        p = l.CG.CGEventGetLocation(e)
        return p.x, p.y
    finally:
        l.CF.CFRelease(e)


# ---------- 截图 ----------
_tmp = None


def capture(fmt: str = "png", quality: int = 70) -> tuple[bytes, int, int]:
    """截主显示器，缩到给模型的尺寸。返回 (图片, 宽, 高)。没有屏幕录制权限时只截得到桌面背景，调用前先查权限。"""
    global _tmp
    if _tmp is None or not os.path.isdir(_tmp):
        _tmp = tempfile.mkdtemp(prefix="osa-desk-")
    _, _, iw, ih = screen()
    raw = os.path.join(_tmp, f"{time.time_ns()}.png")
    out = raw + ("." + ("jpg" if fmt == "jpeg" else "png"))
    try:
        r = subprocess.run(["screencapture", "-x", "-m", "-C", "-t", "png", raw], capture_output=True, timeout=20)
        if r.returncode or not os.path.exists(raw):
            raise RuntimeError((r.stderr or b"").decode(errors="replace").strip() or "screencapture 失败")
        cmd = ["sips", "-z", str(ih), str(iw), "-s", "format", "jpeg" if fmt == "jpeg" else "png"]
        if fmt == "jpeg":
            cmd += ["-s", "formatOptions", str(quality)]
        r = subprocess.run(cmd + [raw, "--out", out], capture_output=True, timeout=20)
        if r.returncode or not os.path.exists(out):
            raise RuntimeError((r.stderr or b"").decode(errors="replace").strip() or "sips 失败")
        with open(out, "rb") as f:
            return f.read(), iw, ih
    finally:
        for p in (raw, out):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------- 鼠标 ----------
BUTTONS = {"left": (1, 2, 6, 0), "right": (3, 4, 7, 1), "middle": (25, 26, 27, 2)}   # 按下、松开、拖动的事件类型，按钮号
MOVED = 5
CLICK_STATE = 1   # kCGMouseEventClickState


def _post(e):
    l = L()
    l.CG.CGEventPost(0, e)   # kCGHIDEventTap：和真的鼠标键盘一样从最底层进去
    l.CF.CFRelease(e)


def _mouse(kind: int, x: float, y: float, button: int = 0, clicks: int = 0, flags: int = 0):
    l = L()
    e = l.CG.CGEventCreateMouseEvent(l.src, kind, Point(x, y), button)
    if clicks:
        l.CG.CGEventSetIntegerValueField(e, CLICK_STATE, clicks)
    l.CG.CGEventSetFlags(e, flags)
    _post(e)


def move(x: float, y: float):
    with _lock:
        _mouse(MOVED, x, y)


def click(x: float, y: float, button: str = "left", count: int = 1):
    down, up, _, b = BUTTONS.get(button, BUTTONS["left"])
    with _lock:
        _mouse(MOVED, x, y)
        time.sleep(0.06)
        for n in range(1, max(1, min(3, count)) + 1):
            _mouse(down, x, y, b, n)
            time.sleep(0.03)
            _mouse(up, x, y, b, n)
            time.sleep(0.05)


def drag(x1: float, y1: float, x2: float, y2: float, button: str = "left"):
    down, up, dragged, b = BUTTONS.get(button, BUTTONS["left"])
    with _lock:
        _mouse(MOVED, x1, y1)
        time.sleep(0.08)
        _mouse(down, x1, y1, b, 1)
        time.sleep(0.12)
        steps = 20
        for i in range(1, steps + 1):
            _mouse(dragged, x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps, b)
            time.sleep(0.015)
        time.sleep(0.08)
        _mouse(up, x2, y2, b, 1)


def scroll(x: float, y: float, dy: int = 0, dx: int = 0):
    """在 (x, y) 滚动：dy 正数往上、负数往下，单位是“格”（一格约 100 像素）。"""
    l = L()
    with _lock:
        _mouse(MOVED, x, y)
        time.sleep(0.05)
        for _ in range(max(abs(dy), abs(dx))):
            e = l.CG.CGEventCreateScrollWheelEvent2(l.src, 0, 2, 100 if dy > 0 else -100 if dy < 0 else 0,
                                                    100 if dx > 0 else -100 if dx < 0 else 0, 0)
            _post(e)
            time.sleep(0.03)


# ---------- 键盘 ----------
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14,
    "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27,
    "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
    "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 51, "esc": 53, "escape": 53,
    "forwarddelete": 117, "del": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126, "arrowleft": 123, "arrowright": 124, "arrowdown": 125, "arrowup": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111, "plus": 24, "minus": 27, "prior": 116, "next": 121,   # 后两个是 xdotool 的翻页键名
}
MODS = {"cmd": (55, 1 << 20), "shift": (56, 1 << 17), "alt": (58, 1 << 19), "ctrl": (59, 1 << 18), "fn": (63, 1 << 23)}
MOD_NAMES = {"cmd": "cmd", "command": "cmd", "meta": "cmd", "super": "cmd", "win": "cmd", "⌘": "cmd",
             "shift": "shift", "⇧": "shift", "alt": "alt", "option": "alt", "opt": "alt", "⌥": "alt",
             "ctrl": "ctrl", "control": "ctrl", "⌃": "ctrl", "fn": "fn"}


SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "_": "-",
           "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`"}   # 美式键盘上按 shift 出来的符号


def parse_keys(keys: str) -> tuple[list[str], str]:
    """'cmd+shift+4' → (['cmd', 'shift'], '4')；'*' → (['shift'], '8')。只按修饰键（比如单按 shift）时第二项为空。认不出的键报 ValueError。"""
    s = (keys or "").strip()
    if s in SHIFTED:
        return ["shift"], SHIFTED[s]
    parts = [p for p in re.split(r"\s*\+\s*", s) if p]
    if not parts:
        raise ValueError("没有按键")
    mods, key = [], ""
    for i, p in enumerate(parts):
        low = p.lower()
        if low in MOD_NAMES:
            if MOD_NAMES[low] not in mods:
                mods.append(MOD_NAMES[low])
        elif i != len(parts) - 1:
            raise ValueError(f"按键名不合法：{keys}")
        elif low in KEYCODES:
            key = low
        elif p in SHIFTED:
            key = SHIFTED[p]
            if "shift" not in mods:
                mods.append("shift")
        else:
            raise ValueError(f"认不出这个键：{p}")
    return mods, key


def _key(code: int, down: bool, flags: int):
    l = L()
    e = l.CG.CGEventCreateKeyboardEvent(l.src, code, down)
    l.CG.CGEventSetFlags(e, flags)
    _post(e)


def press(keys: str):
    """按一个键或组合键：先按下修饰键，再按键，再反过来松开（⌘Tab、⌘空格这类系统快捷键也认）。"""
    mods, key = parse_keys(keys)
    with _lock:
        _press(mods, key)


def press_seq(keys: str, limit: int = 60):
    """按顺序按几个键，用空格隔开：'1 2 3 * 4 5 enter'、'down down enter'。先全部认一遍，有认不出的就一个都不按。"""
    seq = [parse_keys(k) for k in (keys or "").split()][:limit]
    if not seq:
        raise ValueError("没有按键")
    with _lock:
        for mods, key in seq:
            _press(mods, key)
            time.sleep(0.04)


def _press(mods: list[str], key: str):
    flags = 0
    for m in mods:
        flags |= MODS[m][1]
        _key(MODS[m][0], True, flags)
        time.sleep(0.02)
    if key:
        _key(KEYCODES[key], True, flags)
        time.sleep(0.03)
        _key(KEYCODES[key], False, flags)
        time.sleep(0.02)
    for m in reversed(mods):
        flags &= ~MODS[m][1]
        _key(MODS[m][0], False, flags)
        time.sleep(0.02)


# ---------- 打字：剪贴板 + ⌘V ----------
TEXT = "public.utf8-plain-text"
TRANSIENT = "org.nspasteboard.TransientType"   # 剪贴板管理器（Maccy、Paste 等）约定：不记这一条


def _nsstr(s: str):
    return L().msg(L().cls("NSString"), "stringWithUTF8String:", s.encode(), argtypes=(c_char_p,))


def _pystr(ns) -> str:
    v = L().msg(ns, "UTF8String", restype=c_char_p) if ns else None
    return v.decode(errors="replace") if v else ""


def _nsdata(b: bytes):
    return L().msg(L().cls("NSData"), "dataWithBytes:length:", b, len(b), argtypes=(c_char_p, c_ulong))


def _board():
    return L().msg(L().cls("NSPasteboard"), "generalPasteboard")


def _change_count(pb) -> int:
    return int(L().msg(pb, "changeCount", restype=c_long))


def _save(pb, limit: int = 64 << 20) -> list[list[tuple[str, bytes]]] | None:
    """剪贴板现在的所有内容（每一项的每种格式）。太大就不存（返回 None，之后不还原）。"""
    l = L()
    items = l.msg(pb, "pasteboardItems")
    out, total = [], 0
    for i in range(int(l.msg(items, "count", restype=c_ulong)) if items else 0):
        it = l.msg(items, "objectAtIndex:", i, argtypes=(c_ulong,))
        types = l.msg(it, "types")
        entry = []
        for j in range(int(l.msg(types, "count", restype=c_ulong)) if types else 0):
            t = l.msg(types, "objectAtIndex:", j, argtypes=(c_ulong,))
            data = l.msg(it, "dataForType:", t, argtypes=(c_void_p,))
            if not data:
                continue
            n = int(l.msg(data, "length", restype=c_ulong))
            total += n
            if total > limit:
                return None
            entry.append((_pystr(t), ctypes.string_at(l.msg(data, "bytes"), n) if n else b""))
        out.append(entry)
    return out


def _write(pb, items: list[list[tuple[str, bytes]]]):
    l = L()
    arr = l.msg(l.cls("NSMutableArray"), "array")
    for entry in items:
        it = l.msg(l.msg(l.cls("NSPasteboardItem"), "alloc"), "init")
        for t, b in entry:
            l.msg(it, "setData:forType:", _nsdata(b), _nsstr(t), restype=c_bool, argtypes=(c_void_p, c_void_p))
        l.msg(arr, "addObject:", it, restype=None, argtypes=(c_void_p,))
        l.msg(it, "release", restype=None)
    l.msg(pb, "clearContents", restype=c_long)
    if items:
        l.msg(pb, "writeObjects:", arr, restype=c_bool, argtypes=(c_void_p,))


def type_text(text: str, settle: float = 0.6):
    """在当前焦点处输入文字：放进剪贴板、按 ⌘V，等目标 App 读走后把原来的剪贴板放回去（这期间别人改过剪贴板就不放）。"""
    if not text:
        return
    l = L()
    with _lock:
        pool = l.objc.objc_autoreleasePoolPush()
        try:
            pb = _board()
            saved = _save(pb)
            _write(pb, [[(TEXT, text.encode()), (TRANSIENT, b"")]])
            mine = _change_count(pb)
            _press(["cmd"], "v")
            time.sleep(settle)
            if saved is not None and _change_count(pb) == mine:
                _write(pb, saved)
        finally:
            l.objc.objc_autoreleasePoolPop(pool)


def clipboard_text() -> str:
    """剪贴板里的文字（测试用）。"""
    l = L()
    pool = l.objc.objc_autoreleasePoolPush()
    try:
        return _pystr(l.msg(_board(), "stringForType:", _nsstr(TEXT), argtypes=(c_void_p,)))
    finally:
        l.objc.objc_autoreleasePoolPop(pool)


def set_clipboard_text(text: str):
    l = L()
    pool = l.objc.objc_autoreleasePoolPush()
    try:
        _write(_board(), [[(TEXT, text.encode())]])
    finally:
        l.objc.objc_autoreleasePoolPop(pool)


# ---------- 屏幕上有什么 ----------
def front() -> dict:
    """前台 App 和它当前窗口的标题：{"app", "pid", "title"}。"""
    l = L()
    app = _ax(l.sys, "AXFocusedApplication")
    if not app:
        l.warm()
        app = _ax(l.sys, "AXFocusedApplication")
    if not app:   # 没有辅助功能权限，或者前台 App 不响应
        return {"app": _lsapp(), "pid": 0, "title": ""}
    try:
        win = _ax(app, "AXFocusedWindow")
        title = _ax_text(win, "AXTitle") if win else ""
        if win:
            l.CF.CFRelease(win)
        return {"app": _ax_text(app, "AXTitle") or _lsapp(), "pid": _pid(app), "title": title}
    finally:
        l.CF.CFRelease(app)


def _lsapp() -> str:
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=5).stdout.strip()
        r = subprocess.run(["lsappinfo", "info", "-only", "name", asn], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"LSDisplayName"="(.*)"', r)
        return m[1] if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def windows(limit: int = 12) -> list[dict]:
    """屏幕上看得见的普通窗口，从前到后：{"app", "pid", "title", "x", "y", "w", "h"}（点）。窗口标题要屏幕录制权限。"""
    l = L()
    arr = l.CG.CGWindowListCopyWindowInfo(1 | 16, 0)   # 只要屏幕上的，不要桌面
    out = []
    try:
        for i in range(l.CF.CFArrayGetCount(arr) if arr else 0):
            d = l.CF.CFArrayGetValueAtIndex(arr, i)
            if _py(_get(d, "kCGWindowLayer")) != 0 or (_py(_get(d, "kCGWindowAlpha")) or 0) <= 0:
                continue
            r = Rect()
            if not l.CG.CGRectMakeWithDictionaryRepresentation(_get(d, "kCGWindowBounds"), byref(r)) \
                    or r.size.w < 60 or r.size.h < 40:
                continue
            out.append({"app": _py(_get(d, "kCGWindowOwnerName")) or "", "pid": int(_py(_get(d, "kCGWindowOwnerPID")) or 0),
                        "title": _py(_get(d, "kCGWindowName")) or "", "x": r.origin.x, "y": r.origin.y,
                        "w": r.size.w, "h": r.size.h})
            if len(out) >= limit:
                break
    finally:
        if arr:
            l.CF.CFRelease(arr)
    return out


CLICKABLE = {"AXButton", "AXMenuItem", "AXMenuBarItem", "AXLink", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
             "AXMenuButton", "AXTab", "AXCell", "AXRow"}


def element_at(x: float, y: float) -> dict:
    """屏幕坐标 (x, y) 下面是什么：{"role", "label", "pid"}。是按钮里的文字、图片时取那个按钮。"""
    l = L()
    el = c_void_p()
    if l.AX.AXUIElementCopyElementAtPosition(l.sys, x, y, byref(el)) != 0 or not el.value:
        return {"role": "", "label": "", "pid": 0}
    cur, owned, hit = el.value, [el.value], None
    try:
        for _ in range(4):   # 往上找最近的可点的元素
            if _ax_text(cur, "AXRole") in CLICKABLE:
                hit = cur
                break
            parent = _ax(cur, "AXParent")
            if not parent:
                break
            owned.append(parent)
            cur = parent
        cur = hit or el.value
        # 按钮的文字在标题或说明里；AXValue 只对按钮类取（文本框的 AXValue 是里面的全文）
        attrs = ("AXTitle", "AXDescription", "AXValue", "AXHelp") if hit else ("AXTitle", "AXDescription", "AXHelp")
        label = next((s for s in (_ax_text(cur, a) for a in attrs) if s), "")
        return {"role": _ax_text(cur, "AXRole"), "label": label[:80], "pid": _pid(cur)}
    finally:
        for o in owned:
            l.CF.CFRelease(o)


def focused() -> dict:
    """现在有键盘焦点的元素：{"role", "label", "pid"}（按回车之前看一眼是不是在发消息的输入框里）。"""
    l = L()
    el = _ax(l.sys, "AXFocusedUIElement")
    if not el:
        l.warm()
        el = _ax(l.sys, "AXFocusedUIElement")
    if not el:
        return {"role": "", "label": "", "pid": 0}
    try:
        label = next((s for s in (_ax_text(el, a) for a in ("AXDescription", "AXTitle", "AXPlaceholderValue", "AXHelp")) if s), "")
        return {"role": _ax_text(el, "AXRole"), "label": label[:80], "pid": _pid(el)}
    finally:
        l.CF.CFRelease(el)


def find_app(name: str) -> str:
    """按 App 显示的名字找它在哪（中文系统里“计算器”“访达”“微信”这些名字 open -a 不认，Spotlight 认）。找不到返回空。"""
    q = name.replace("\\", "").replace("'", "").replace('"', "")
    try:
        r = subprocess.run(["mdfind", f"kMDItemDisplayName == '{q}'cd && kMDItemContentType == 'com.apple.application-bundle'"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    paths = [p for p in r.stdout.splitlines() if p.endswith(".app")]
    # 系统和“应用程序”里的优先，别的（备份、下载目录里的副本）排后面
    paths.sort(key=lambda p: not p.startswith(("/Applications/", "/System/Applications/", os.path.expanduser("~/Applications/"))))
    return paths[0] if paths else ""


def open_app(name: str) -> str:
    """打开或切到一个 App。返回空表示成功，否则是出错原因。"""
    r = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return ""
    if path := find_app(name):
        r = subprocess.run(["open", "-a", path], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return ""
    return (r.stderr.strip() or f"打不开 {name}")[:300] + "（App 的名字写它在“应用程序”文件夹里的名字，中英文都行）"

