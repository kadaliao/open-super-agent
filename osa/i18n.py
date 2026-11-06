"""界面语言。中文原文就是翻译的键：t("没有这个 Bot") 在英文界面返回 locales/en.json 里对应的英文，没有翻译就返回中文。

只翻用户会看到的文字（接口报错、对话里的系统消息、卡片、推送、聊天软件里的按钮）；写给模型的提示词和工具说明保持中文。
存进数据库、之后还要按原文比对的标记（比如 “[新话题]”）不要翻，在显示的地方翻。

语言存在 settings 表的 language 里，前后端共用。没设过时：新装的按浏览器的语言定，升级上来的老用户保持中文。

`uv run python -m osa.i18n` 检查前后端：代码里用到但 en.json 里没有的、en.json 里多余的、占位符对不上的，
以及前端还没包进 t() 的中文。"""
import json
import re
from pathlib import Path

LANGS = ("zh", "en")
EN: dict = json.loads((Path(__file__).parent / "locales" / "en.json").read_text())
_lang: str | None = None


def chosen() -> str:
    """设过的语言；没设过返回空。"""
    global _lang
    if _lang is None:
        from .memory import conn
        r = conn.execute("select value from settings where key='language'").fetchone()
        _lang = json.loads(r[0]) if r else ""
    return _lang or ""


def lang() -> str:
    return chosen() or "zh"


def set_lang(value: str):
    global _lang
    if value not in LANGS:
        raise ValueError(t("不支持这种语言：{lang}", lang=value))
    from .memory import conn
    conn.execute("insert or replace into settings values('language', ?)", (json.dumps(value),))
    conn.commit()
    _lang = value


def from_header(accept: str | None) -> str:
    """按浏览器的 Accept-Language 猜：排在最前面的是中文就用中文，否则用英文。"""
    first = (accept or "").split(",")[0].strip().lower()
    return "zh" if not first or first.startswith("zh") else "en"


def _fill(s: str, kw: dict) -> str:
    # 只替换给了值的 {名字}，原文里别的花括号（JSON 例子之类）原样保留
    return re.sub(r"\{(\w+)\}", lambda m: str(kw[m[1]]) if m[1] in kw else m[0], s) if kw else s


def t(zh: str, /, in_lang: str | None = None, **kw) -> str:
    """翻译一句话，变量写成 {名字}：t("最多 {n} MB", n=5)。英文可以写成 ["{n} task", "{n} tasks"]，按 n 是不是 1 选。
    in_lang 指定语言（比如登录页还没设语言时按浏览器的）；不给就用设置里的。"""
    s = zh
    if (in_lang or lang()) == "en":
        v = EN.get(zh)
        if isinstance(v, list):
            v = v[0 if kw.get("n") == 1 else 1]
        if v:
            s = v
    return _fill(s, kw)


class Later:
    """存起来、过一会儿才显示的说明（应用的连接状态、聊天软件的报错）：str() 时才按当时的语言翻，中途切换语言也对。
    两个 Later 的原文和变量一样就算相等，状态没变时不会白白通知前端。"""
    __slots__ = ("zh", "kw")

    def __init__(self, zh: str, kw: dict):
        self.zh, self.kw = zh, kw   # 变量原样留着：t() 要按 n == 1 选单复数

    def __str__(self) -> str:
        return t(self.zh, **{k: str(v) if isinstance(v, Later) else v for k, v in self.kw.items()})

    def _key(self):
        return self.zh, {k: str(v) for k, v in self.kw.items()}

    def __eq__(self, other):
        return isinstance(other, Later) and self._key() == other._key()

    def __hash__(self):
        return hash(self.zh)


def later(zh: str, /, **kw) -> Later:
    """和 t() 一样，但到显示的时候才翻。用的地方要 str() 一下。"""
    return Later(zh, kw)


def N_(zh: str) -> str:
    """只标记、不翻译：模块里的常量表（名字、说明）用它包起来，检查时能找到；显示的时候再 t(变量)。
    语言能在运行中切换，所以后端不要在 import 时就翻好。"""
    return zh


def model_hint() -> str:
    """写给模型的一句话：英文界面时默认用英文回复。中文界面什么都不加，行为和以前一样。"""
    return "" if lang() == "zh" else "\n用户的界面语言是英文：默认用英文回复，结果、页面和记下的记忆也用英文；用户用别的语言说话时跟着用那种语言。"


def both(zh: str) -> set[str]:
    """一句话的中英文两种说法：解析用户回复的文字命令时，两种语言都认。"""
    return {zh, *([EN[zh]] if isinstance(EN.get(zh), str) else EN.get(zh) or [])}


# ---------- 检查 ----------
ROOT = Path(__file__).resolve().parent.parent
CJK = re.compile(r"[㐀-鿿＀-￯　-〿]")
PH = re.compile(r"\{(\w+)\}")


def _py_keys() -> dict[str, list[str]]:
    import ast
    keys: dict[str, list[str]] = {}
    for f in sorted((ROOT / "osa").rglob("*.py")):
        tree = ast.parse(f.read_text(), str(f))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name not in ("t", "N_", "later", "both") or not node.args:
                continue
            a = node.args[0]
            where = f"{f.relative_to(ROOT)}:{node.lineno}"
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                keys.setdefault(a.value, []).append(where)
            elif isinstance(a, ast.JoinedStr):
                keys.setdefault("!f-string", []).append(where)
    return keys


# t('…') / tn("…")，tc('场景', '…')：取引号里的内容
_Q = r"""(['"`])((?:\\.|(?!\{q}).)*?)\{q}"""
_JS_T = re.compile(r"\b(?:t|tn)\(\s*" + _Q.format(q=1), re.S)
_JS_TC = re.compile(r"\btc\(\s*" + _Q.format(q=1) + r"\s*,\s*" + _Q.format(q=3), re.S)
HAN = re.compile(r"[\u3400-\u9fff]")


def _js_unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), s)


def _strip_js_comments(src: str) -> str:
    # 粗略去掉注释：/* … */、{/* … */} 和 // …（前面是空白或行首，避免误伤网址）；保留换行，行号不变
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m[0].count("\n"), src, flags=re.S)
    return re.sub(r"(^|\s)//.*$", r"\1", src, flags=re.M)


def _js_scan() -> tuple[dict[str, list[str]], list[str]]:
    keys: dict[str, list[str]] = {}
    left: list[str] = []
    for f in sorted((ROOT / "web" / "src").rglob("*.ts*")):
        rel = f.relative_to(ROOT)
        raw_lines = f.read_text().split("\n")
        src = _strip_js_comments("\n".join(raw_lines))
        found = [(m, None, m[2]) for m in _JS_T.finditer(src)] + [(m, m[2], m[4]) for m in _JS_TC.finditer(src)]
        for m, ctx, key in found:
            where = f"{rel}:{src.count(chr(10), 0, m.start()) + 1}"
            if "${" in key:
                keys.setdefault("!template", []).append(where)
                continue
            key = _js_unescape(key)
            keys.setdefault(f"{_js_unescape(ctx)}::{key}" if ctx else key, []).append(where)
        rest = _JS_TC.sub(lambda m: " " * len(m[0]), _JS_T.sub(lambda m: " " * len(m[0]), src))
        for i, ln in enumerate(rest.split("\n")):
            if HAN.search(ln) and "i18n-ignore" not in raw_lines[i]:
                left.append(f"{rel}:{i + 1}: {ln.strip()[:120]}")
    return keys, left


def _report(name: str, used: dict[str, list[str]], path: Path) -> int:
    d = json.loads(path.read_text())
    bad = 0
    for k in ("!f-string", "!template"):
        for w in used.pop(k, []):
            print(f"  {w}: 翻译的键不能是 f-string / 带 ${{}} 的模板字符串，改成 {{名字}} 占位符")
            bad += 1
    plain = {k.split("::", 1)[-1] for k in used}
    missing = [k for k in used if k not in d and not ("::" in k and k.split("::", 1)[1] in d)]
    unused = [k for k in d if k not in used and k not in plain]
    for k in missing:
        print(f"  缺翻译 {used[k][0]}: {k!r}")
    for k in unused:
        print(f"  多余的 {k!r}")
    for k, v in d.items():
        want = set(PH.findall(k.split("::", 1)[-1]))
        forms = v if isinstance(v, list) else [v]
        if isinstance(v, list) and len(v) != 2:
            print(f"  单复数要正好两个 {k!r}")
            bad += 1
        for i, s in enumerate(forms):
            # 单数那个可以不写 {n}："Every hour"；其余的占位符要和中文一样
            got = set(PH.findall(s)) if isinstance(s, str) else set()
            ok = got <= want and want - got <= {"n"} if isinstance(v, list) and i == 0 else got == want
            if not isinstance(s, str) or not s:
                print(f"  空的翻译 {k!r}")
                bad += 1
            elif not ok:
                print(f"  占位符对不上 {k!r} → {s!r}")
                bad += 1
            elif CJK.search(s):
                print(f"  英文里有中文 {k!r} → {s!r}")
                bad += 1
    print(f"{name}：用到 {len(used)} 句，en.json {len(d)} 句，缺 {len(missing)}，多余 {len(unused)}")
    return bad + len(missing)


def check() -> int:
    print("后端 osa/")
    bad = _report("后端", _py_keys(), ROOT / "osa" / "locales" / "en.json")
    print("前端 web/src/")
    keys, left = _js_scan()
    bad += _report("前端", keys, ROOT / "web" / "src" / "locales" / "en.json")
    for ln in left:
        print("  没翻译 " + ln)
    print(f"前端还有 {len(left)} 行中文没包进 t()（数据、协议标记这类不用翻的，在行尾注释 i18n-ignore）")
    return bad + len(left)


if __name__ == "__main__":
    import sys
    sys.exit(1 if check() else 0)
