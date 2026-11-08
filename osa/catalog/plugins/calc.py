"""精确计算器：四则运算、百分比、乘方开方、对数、三角函数、排列组合，还能算房贷月供（等额本息、等额本金）和复利、定投。

大模型心算多位数、小数常出错，算钱差一分也是错。装上以后 Bot 要算数就调这个工具，对话里和后台任务里都能用。

- 加减乘除用十进制算，0.1 + 0.2 就是 0.3，不会出现 0.30000000000000004。1/3 这种除不尽的保留 20 位有效数字，前面标“≈”。
- 三角函数按“度”算：sin(30) 是 0.5，asin 这些返回度。弧度先换成度：sin(deg(pi/6))。三角函数用的是浮点数，结果保留 12 位小数。
- 15% 就是 0.15（百分号后面跟数字或括号时是取余：10 % 3 是 1）。3.5万、2亿 能直接写；^ 和 ** 都是乘方；×、÷、全角括号也认。
- round(x, n) 是四舍五入（不是“银行家舍入”）。log(x) 以 10 为底，ln(x) 是自然对数，log(x, b) 以 b 为底。
- 防止把服务算卡：表达式最长 500 个字，乘方的指数最大 100000，阶乘、排列组合最大 10000。
"""
import ast
import math
import re
import unicodedata
from contextlib import contextmanager
from decimal import (ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal, DecimalException, DivisionByZero,
                     Inexact, getcontext, localcontext)
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "精确计算器", "version": "1.0", "author": "open-super-agent"}
MAX_EXPR = 500
MAX_NODES = 300
MAX_POW = 100_000
MAX_FACT = 10_000
SHOW = 20          # 除不尽时显示几位有效数字
CTX = Context(prec=40, Emax=10**5, Emin=-10**5)   # 结果超过 10 的 10 万次方就报“超出范围”
PI = Decimal("3.141592653589793238462643383279502884197")
E = Decimal("2.718281828459045235360287471352662497757")
METHODS = ("等额本息", "等额本金")

SCHEMA = {"type": "function", "function": {
    "name": "calc",
    "description": "精确计算。要算数时一定用它，不要自己心算：加减乘除、百分比、乘方开方、对数、三角函数、排列组合、房贷月供、复利。"
                   "op=eval（默认）：算 expr，比如 (1200*0.85+30)/3、2^10、sqrt(2)、80*15%、3.5万*12、sin(30)（三角函数按度）、"
                   "log(100)（以 10 为底）、ln(e)、round(2.675, 2)（四舍五入）、comb(5, 2)、avg(3, 4, 5)；数字不要写千分位逗号。"
                   "op=loan：贷款月供，principal 本金（元），rate 年利率（百分数里的数，3.1% 写 3.1），years 或 months，"
                   "method 是等额本息或等额本金（不写两种都算）。"
                   "op=compound：复利和定投，principal 本金，rate 年化收益率（百分数里的数），years，"
                   "per_year 每年计息几次（默认 1，按月写 12），deposit 每期末再存多少（不写就是 0）。",
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["eval", "loan", "compound"]},
        "expr": {"type": "string", "description": "eval 用：算式"},
        "principal": {"type": "number", "description": "本金（元）"},
        "rate": {"type": "number", "description": "年利率 / 年化收益率，百分数里的数：3.1% 写 3.1"},
        "years": {"type": "number"}, "months": {"type": "integer"},
        "method": {"type": "string", "enum": ["等额本息", "等额本金"]},
        "per_year": {"type": "integer", "description": "compound 用：每年计息几次，默认 1"},
        "deposit": {"type": "number", "description": "compound 用：每期末再存多少"}},
        "required": []}}}


# ---------- 把人写的算式整理成 Python 表达式 ----------
REPLACE = {"×": "*", "✕": "*", "·": "*", "÷": "/", "−": "-", "–": "-", "π": "pi", "^": "**", "√": "sqrt"}
UNITS = {"万亿": "1000000000000", "亿": "100000000", "万": "10000", "千": "1000"}
NUM = r"\d+(?:\.\d+)?"
SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _percent(s: str) -> str:
    """把“百分之”的 % 换成 /100：15% → (15/100)，(1+5)% → ((1+5)/100)。% 后面跟数字、括号、名字时是取余，不动。"""
    out = ""
    i = 0
    while i < len(s):
        c = s[i]
        if c != "%":
            out += c
            i += 1
            continue
        rest = s[i + 1:].lstrip()
        if rest and (rest[0].isalnum() or rest[0] in "(."):
            out += c   # 取余
            i += 1
            continue
        body = out.rstrip()
        if m := re.search(r"(\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)$", body):
            out = body[:m.start()] + f"({m.group(1)}/100)"
        elif body.endswith(")"):
            depth, j = 0, len(body) - 1
            while j >= 0:
                depth += {")": 1, "(": -1}.get(body[j], 0)
                if depth == 0:
                    break
                j -= 1
            while j > 0 and (body[j - 1].isalnum() or body[j - 1] == "_"):
                j -= 1   # sqrt(…)% 连函数名一起
            out = body[:j] + f"({body[j:]}/100)"
        else:
            raise ValueError("% 前面要是数字或者括号")
        i += 1
    return out


def normalize(expr: str) -> str:
    # 上标先换成乘方：NFKC 会把 3² 变成 32
    s = re.sub("[⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda m: "**" + m.group(0).translate(SUPERSCRIPT), str(expr))
    s = unicodedata.normalize("NFKC", s).strip()   # 全角数字、括号、逗号、百分号换成半角
    for a, b in REPLACE.items():
        s = s.replace(a, b)
    s = re.sub(r"sqrt\s*(" + NUM + ")", r"sqrt(\1)", s)    # √2
    s = re.sub(r"(\d+)\s*!", r"factorial(\1)", s)          # 5!
    for u, n in UNITS.items():
        s = re.sub(f"({NUM})\\s*{u}", f"(\\1*{n})", s)
    if not re.search(r"[A-Za-z_]", s):   # 没有函数时，1,234,567 里的逗号是千分位
        s = re.sub(r"(?<![\d.])\d{1,3}(?:,\d{3})+(?![\d,])", lambda m: m.group(0).replace(",", ""), s)
    s = s.rstrip("=").strip()
    return _percent(s)


# ---------- 求值 ----------
@contextmanager
def keep_exact():
    """里面的运算有舍入也不算“近似”：四舍五入是要求的，三角函数自己判断。"""
    c = getcontext()
    before = c.flags[Inexact]
    try:
        yield
    finally:
        c.flags[Inexact] = before


class _Eval:
    def __init__(self, src: str):
        self.src = src
        self.approx = False   # 用过浮点数（三角函数）

    def num(self, node) -> Decimal:
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("只能算数字")
        seg = ast.get_source_segment(self.src, node) or repr(v)
        return Decimal(seg)   # 用原文，不经过浮点数：0.1 就是 0.1

    def __call__(self, node) -> Decimal:
        if isinstance(node, ast.Expression):
            return self(node.body)
        if isinstance(node, ast.Constant):
            return self.num(node)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = self(node.operand)
            return -v if isinstance(node.op, ast.USub) else +v
        if isinstance(node, ast.BinOp):
            return self.binop(node.op, self(node.left), self(node.right))
        if isinstance(node, ast.Name):
            if node.id in ("pi",):
                return +PI
            if node.id == "e":
                return +E
            raise ValueError(f"不认识 {node.id}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            fn = FUNCS.get(node.func.id)
            if not fn:
                raise ValueError(f"不支持函数 {node.func.id}，能用的有：{'、'.join(sorted(FUNCS))}")
            return fn(self, *[self(a) for a in node.args])
        raise ValueError(f"不支持这种写法：{ast.get_source_segment(self.src, node) or type(node).__name__}")

    def binop(self, op, a: Decimal, b: Decimal) -> Decimal:
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)) and b == 0:
            raise ValueError("除数是 0")
        if isinstance(op, ast.Div):
            return a / b
        if isinstance(op, (ast.FloorDiv, ast.Mod)):
            # Decimal 的 // 和 % 向 0 取整；换成向下取整，余数和除数同号（-7 % 3 是 2），和数学课上一样
            q, r = a // b, a % b
            if r and (r < 0) != (b < 0):
                q, r = q - 1, r + b
            return q if isinstance(op, ast.FloorDiv) else r
        if isinstance(op, ast.Pow):
            if abs(b) > MAX_POW:
                raise ValueError(f"指数太大（最大 {MAX_POW}）")
            if a < 0 and b != b.to_integral_value():
                raise ValueError("负数不能开非整数次方")
            if a == 0 and b < 0:
                raise ValueError("0 不能做负数次方")
            return a ** b
        raise ValueError("不支持这个运算符")

    def float_fn(self, f, x: Decimal, degrees: bool = False) -> Decimal:
        """三角函数：浮点数算，保留 12 位小数（sin(30) 是 0.5 而不是 0.49999999999999994）。
        结果是 0.5、1 这种短小数的当准确值，别的标近似。"""
        with keep_exact():
            if degrees:
                x = x * PI / 180
            try:
                r = round(f(float(x)), 12)
            except (ValueError, OverflowError):
                raise ValueError("超出了这个函数能算的范围") from None
            out = Decimal(repr(r)) + 0
        if out != out.quantize(Decimal("0.001")):
            self.approx = True
        return out


def _int(x: Decimal, what: str) -> int:
    if x != x.to_integral_value() or x < 0 or x > MAX_FACT:
        raise ValueError(f"{what}要是 0 到 {MAX_FACT} 的整数")
    return int(x)


def _round(ev, x, n=Decimal(0)):
    n = int(n)
    if abs(n) > 30:
        raise ValueError("round 最多保留 30 位小数")
    with keep_exact():
        return x.quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)


def _log(ev, x, base=None):
    if x <= 0 or (base is not None and (base <= 0 or base == 1)):
        raise ValueError("对数的真数要大于 0，底数要大于 0 且不是 1")
    if base is None:
        return x.log10()
    with keep_exact():
        r = x.ln() / base.ln()
        n = r.to_integral_value()
        if abs(r - n) < Decimal("1e-30") and abs(n) <= MAX_POW and base ** n == x:
            return n   # log(8, 2) 就是 3
    return x.ln() / base.ln()


def _sqrt(ev, x):
    if x < 0:
        raise ValueError("负数不能开平方")
    return x.sqrt()


def _cbrt(ev, x):
    if x == 0:
        return x
    with keep_exact():
        r = abs(x) ** (Decimal(1) / 3)
        near = r.to_integral_value()
        if near ** 3 == abs(x):   # 27 的立方根就是 3，不是 2.999…
            r = near
        else:
            ev.approx = True
    return r if x > 0 else -r


def _ln(ev, x):
    if x <= 0:
        raise ValueError("对数的真数要大于 0")
    return x.ln()


def _tan(ev, x):
    if (x - 90) % 180 == 0:
        raise ValueError(f"tan({x}°) 没有定义（无穷大）")
    return ev.float_fn(math.tan, x, degrees=True)


def _args(name, fn, lo=1, hi=1):
    """检查参数个数，写错了给一句能看懂的话，而不是 Python 的 TypeError。"""
    def call(ev, *a):
        if not lo <= len(a) <= hi:
            want = f"{lo} 个" if lo == hi else f"{lo} 到 {hi} 个" if hi < 99 else f"至少 {lo} 个"
            raise ValueError(f"{name} 要 {want}参数")
        return fn(ev, *a)
    return call


FUNCS = {
    "sqrt": _args("sqrt", _sqrt),
    "cbrt": _args("cbrt", _cbrt),
    "abs": _args("abs", lambda ev, x: abs(x)),
    "round": _args("round", _round, 1, 2),
    "floor": _args("floor", lambda ev, x: x.to_integral_value(ROUND_FLOOR)),
    "ceil": _args("ceil", lambda ev, x: x.to_integral_value(ROUND_CEILING)),
    "log": _args("log", _log, 1, 2),
    "lg": _args("lg", _log),
    "ln": _args("ln", _ln),
    "log2": _args("log2", lambda ev, x: _log(ev, x, Decimal(2))),
    "exp": _args("exp", lambda ev, x: x.exp()),
    "sin": _args("sin", lambda ev, x: ev.float_fn(math.sin, x, degrees=True)),
    "cos": _args("cos", lambda ev, x: ev.float_fn(math.cos, x, degrees=True)),
    "tan": _args("tan", _tan),
    "asin": _args("asin", lambda ev, x: ev.float_fn(lambda v: math.degrees(math.asin(v)), x)),
    "acos": _args("acos", lambda ev, x: ev.float_fn(lambda v: math.degrees(math.acos(v)), x)),
    "atan": _args("atan", lambda ev, x: ev.float_fn(lambda v: math.degrees(math.atan(v)), x)),
    "deg": _args("deg", lambda ev, x: x * 180 / PI),
    "rad": _args("rad", lambda ev, x: x * PI / 180),
    "min": _args("min", lambda ev, *a: min(a), 1, 999),
    "max": _args("max", lambda ev, *a: max(a), 1, 999),
    "sum": _args("sum", lambda ev, *a: sum(a, Decimal(0)), 1, 999),
    "avg": _args("avg", lambda ev, *a: sum(a, Decimal(0)) / len(a), 1, 999),
    "factorial": _args("factorial", lambda ev, n: Decimal(math.factorial(_int(n, "阶乘")))),
    "comb": _args("comb", lambda ev, n, k: Decimal(math.comb(_int(n, "comb 的参数"), _int(k, "comb 的参数"))), 2, 2),
    "perm": _args("perm", lambda ev, n, k: Decimal(math.perm(_int(n, "perm 的参数"), _int(k, "perm 的参数"))), 2, 2),
    "gcd": _args("gcd", lambda ev, *a: Decimal(math.gcd(*(_int(x, "gcd 的参数") for x in a))), 1, 999),
    "lcm": _args("lcm", lambda ev, *a: Decimal(math.lcm(*(_int(x, "lcm 的参数") for x in a))), 1, 999),
}
FUNCS["mean"], FUNCS["fact"] = FUNCS["avg"], FUNCS["factorial"]


def fmt(x: Decimal, approx: bool = False, places: int | None = None) -> str:
    """数字给人看：大数加千分位，很大很小的用科学计数法；places 固定小数位（钱写 2）。"""
    if places is not None:
        x = x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        return f"{x:,f}"
    if approx:
        x = Decimal(f"{x:.{SHOW}g}")
    if x == 0:
        return "0"
    x = x.normalize()
    if not approx and x == x.to_integral_value() and abs(x) < Decimal("1e40"):
        return f"{x:,f}" if abs(x) >= 10000 else f"{x:f}"   # 准确的整数全写出来：2^100 也是
    if abs(x) >= Decimal("1e21") or abs(x) < Decimal("1e-8"):
        return f"{x:E}".replace("E", "×10^").replace("^+", "^")
    s = f"{x:f}"
    return f"{x:,f}" if abs(x) >= 10000 else s


def chinese_size(x: Decimal) -> str:
    """1234567 → 约 123.46 万：中文里大数习惯用万、亿说。"""
    a = abs(x)
    for unit, n in (("万亿", Decimal("1e12")), ("亿", Decimal("1e8")), ("万", Decimal("1e4"))):
        if Decimal("1e4") <= a < Decimal("1e16") and a >= n:
            v = (x / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()
            return f"{'约 ' if v * n != x else ''}{v:f} {unit}"
    return ""


def evaluate(expr: str) -> tuple[Decimal, bool, str]:
    """算一个表达式：(结果, 是不是近似值, 整理后的表达式)。"""
    if not expr or not str(expr).strip():
        raise ValueError("expr 是空的")
    if len(str(expr)) > MAX_EXPR:
        raise ValueError(f"表达式太长（最多 {MAX_EXPR} 个字）")
    src = normalize(expr)
    if not src.isascii():
        bad = "".join(dict.fromkeys(c for c in src if not c.isascii()))
        raise ValueError(f"表达式里有不认识的字：{bad}")
    try:
        tree = ast.parse(src, mode="eval")
    except (SyntaxError, RecursionError, MemoryError, ValueError):
        raise ValueError(f"算式写得不对：{src}") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ValueError("算式太复杂了，拆成几步算")
    ev = _Eval(src)
    with localcontext(CTX) as c:
        try:
            r = ev(tree)
        except DivisionByZero:
            raise ValueError("除数是 0") from None
        except RecursionError:
            raise ValueError("算式太复杂了，拆成几步算") from None
        except DecimalException as e:
            raise ValueError(f"结果超出范围或者没有意义（{type(e).__name__}）") from None
        r = +r
        return r, ev.approx or bool(c.flags[Inexact]), src


def expr_text(expr: str) -> str:
    r, approx, src = evaluate(expr)
    with localcontext(CTX):
        out = f"{src} {'≈' if approx else '='} {fmt(r, approx)}"
        if size := chinese_size(r):
            out += f"（{size}）"
    return out


# ---------- 房贷、复利 ----------
def _d(args: dict, key: str, default=None) -> Decimal:
    v = args.get(key)
    if v is None or v == "":
        if default is None:
            raise ValueError(f"缺少 {key}")
        return Decimal(default)
    try:
        return Decimal(str(v))
    except DecimalException:
        raise ValueError(f"{key} 要是数字") from None


def _periods(args: dict) -> int:
    if args.get("months"):
        n = _d(args, "months")
    elif args.get("years"):
        n = _d(args, "years") * 12
    else:
        raise ValueError("缺少 years 或 months")
    if n != n.to_integral_value() or not 1 <= n <= 1200:
        raise ValueError("期数要是 1 到 1200 个月的整月")
    return int(n)


def _span(months: int) -> str:
    y, m = divmod(months, 12)
    return " ".join(x for x in (f"{y} 年" if y else "", f"{m} 个月" if m else "") if x)


def loan(args: dict) -> str:
    with localcontext(CTX):
        p, rate, n = _d(args, "principal"), _d(args, "rate", 0), _periods(args)
        if p <= 0 or rate < 0 or rate > 100:
            raise ValueError("本金要大于 0，年利率在 0 到 100 之间（3.1% 写 3.1）")
        r = rate / 100 / 12
        method = args.get("method") or ""
        if method and method not in METHODS:
            raise ValueError("method 是等额本息或等额本金")
        lines = [f"贷款 {fmt(p, places=2)} 元，年利率 {fmt(rate)}%，{n} 期（{_span(n)}）："]
        if method in ("", "等额本息"):
            m = p / n if r == 0 else p * r * (1 + r) ** n / ((1 + r) ** n - 1)
            m = m.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total = m * n   # 和常见的房贷计算器一样：四舍五入后的月供 × 期数
            lines.append(f"- 等额本息：每月还 {fmt(m, places=2)} 元，总利息 {fmt(total - p, places=2)} 元，"
                         f"一共还 {fmt(total, places=2)} 元。")
        if method in ("", "等额本金"):
            base = p / n
            first, drop = base + p * r, base * r
            interest = p * r * (n + 1) / 2
            lines.append(f"- 等额本金：每月还本金 {fmt(base, places=2)} 元，第一个月还 {fmt(first, places=2)} 元，"
                         f"以后每月少 {fmt(drop, places=2)} 元，最后一个月还 {fmt(base + drop, places=2)} 元；"
                         f"总利息 {fmt(interest, places=2)} 元，一共还 {fmt(p + interest, places=2)} 元。")
        lines.append("（月利率 = 年利率 ÷ 12，金额四舍五入到分；银行一般在最后一期补齐尾差，总数可能差几块钱）")
        return "\n".join(lines)


def compound(args: dict) -> str:
    with localcontext(CTX):
        p, rate = _d(args, "principal", 0), _d(args, "rate", 0)
        years, k, dep = _d(args, "years"), int(_d(args, "per_year", 1)), _d(args, "deposit", 0)
        if p < 0 or dep < 0 or (p == 0 and dep == 0):
            raise ValueError("principal（本金）和 deposit（每期存）至少写一个，不能是负数")
        if not 1 <= k <= 365 or rate < -100 or rate > 1000:
            raise ValueError("per_year 在 1 到 365 之间，rate 是百分数里的数")
        n = years * k
        if n != n.to_integral_value() or not 1 <= n <= 100_000:
            raise ValueError("years × per_year 要是整数期，最多 10 万期")
        n, i = int(n), rate / 100 / k
        grow = (1 + i) ** n
        fv = p * grow + (dep * n if i == 0 else dep * (grow - 1) / i)
        paid = p + dep * n
        how = {1: "每年", 2: "每半年", 4: "每季度", 12: "每月", 52: "每周", 365: "每天"}.get(k, f"每年 {k} 次")
        head = "，".join(x for x in (f"本金 {fmt(p, places=2)} 元" if p else "",
                                     f"{how}末存 {fmt(dep, places=2)} 元" if dep else "") if x)
        return (f"{head}，年化 {fmt(rate)}%，{how}计息一次，{fmt(years)} 年（{n} 期）后：\n"
                f"- 一共有 {fmt(fv, places=2)} 元" + (f"（{chinese_size(fv)}）" if chinese_size(fv) else "") + "\n"
                f"- 自己投入 {fmt(paid, places=2)} 元，收益 {fmt(fv - paid, places=2)} 元"
                + (f"，是投入的 {fmt((fv / paid).quantize(Decimal('0.01')))} 倍" if paid else "") + "。")


def calc(args: dict) -> str:
    op = args.get("op") or "eval"
    if op == "eval":
        return expr_text(args.get("expr") or "")
    if op == "loan":
        return loan(args)
    if op == "compound":
        return compound(args)
    raise ValueError(f"不认识的 op：{op}")


def apply(ctx):
    async def run(call):
        try:
            return calc(call.args)
        except (ValueError, TypeError, ArithmeticError) as e:
            return f"没算：{e}"
    ctx.tool(Tool("calc", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="精确计算：四则运算、百分比、乘方开方、对数、三角函数、房贷月供、复利定投",
                  progress=lambda a: f"计算：{a.get('expr') or {'loan': '贷款月供', 'compound': '复利'}.get(a.get('op'), '')}"[:60]))
