"""汇率换算：多少美元是多少人民币，日元、欧元、港币、英镑……都能换，对话里直接能用。

汇率来自 open.er-api.com（免费、不用 key，每天更新一次），是参考汇率，不是银行的买入价、卖出价，真去换钱以银行的牌价为准。
币种写代码（USD、CNY、JPY）或中文名（美元、人民币、日元、港币、韩元、泰铢……）都行，一次可以换成好几种。
查过的汇率在内存里存一小时，不用设置。
"""
import re
import time
from datetime import datetime
import httpx
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "汇率换算", "version": "1.0", "author": "open-super-agent"}
API = "https://open.er-api.com/v6/latest/"
TTL = 3600
MAX_TO = 10

# 代码 -> 中文名（显示用的那个）
NAMES = {
    "CNY": "人民币", "USD": "美元", "EUR": "欧元", "JPY": "日元", "HKD": "港币", "GBP": "英镑", "KRW": "韩元",
    "THB": "泰铢", "TWD": "新台币", "MOP": "澳门元", "AUD": "澳元", "CAD": "加元", "SGD": "新加坡元",
    "CHF": "瑞士法郎", "NZD": "新西兰元", "RUB": "卢布", "INR": "印度卢比", "VND": "越南盾", "MYR": "林吉特",
    "PHP": "菲律宾比索", "IDR": "印尼盾", "AED": "阿联酋迪拉姆", "SAR": "沙特里亚尔", "TRY": "土耳其里拉",
    "BRL": "巴西雷亚尔", "MXN": "墨西哥比索", "ZAR": "南非兰特", "SEK": "瑞典克朗", "NOK": "挪威克朗",
    "DKK": "丹麦克朗", "PLN": "波兰兹罗提", "CZK": "捷克克朗", "HUF": "匈牙利福林", "ILS": "以色列谢克尔",
    "EGP": "埃及镑", "KHR": "柬埔寨瑞尔", "LAK": "老挝基普", "MMK": "缅元", "MNT": "蒙古图格里克",
    "KZT": "哈萨克斯坦坚戈", "PKR": "巴基斯坦卢比", "ARS": "阿根廷比索", "CLP": "智利比索", "NPR": "尼泊尔卢比",
    "LKR": "斯里兰卡卢比", "BDT": "孟加拉塔卡", "QAR": "卡塔尔里亚尔", "KWD": "科威特第纳尔", "UAH": "乌克兰格里夫纳",
}
# 别的叫法
EXTRA = {
    "美金": "USD", "刀": "USD", "$": "USD", "日币": "JPY", "円": "JPY", "€": "EUR", "港元": "HKD", "£": "GBP",
    "韩币": "KRW", "₩": "KRW", "台币": "TWD", "澳门币": "MOP", "澳币": "AUD", "加币": "CAD", "加拿大元": "CAD",
    "新币": "SGD", "坡币": "SGD", "瑞郎": "CHF", "纽币": "NZD", "俄罗斯卢布": "RUB", "卢比": "INR", "马币": "MYR",
    "马来西亚林吉特": "MYR", "迪拉姆": "AED", "里拉": "TRY", "兰特": "ZAR", "RMB": "CNY",
    "人民币元": "CNY", "美国美元": "USD", "澳大利亚元": "AUD", "英国英镑": "GBP", "泰国泰铢": "THB",
}
ALIASES = {name: c for c, name in NAMES.items()} | EXTRA

SCHEMA = {"type": "function", "function": {
    "name": "fx_convert",
    "description": "汇率换算：多少某种货币换成另一种（或几种）货币是多少，也能只查汇率（amount 写 1）。"
                   "问到外币、换汇、海淘、出国花多少钱时用它，不要自己估汇率。"
                   "币种写代码（USD、CNY）或中文名（美元、人民币、日元、欧元、港币……），to 可以写几个、用逗号分开。",
    "parameters": {"type": "object", "properties": {
        "amount": {"type": "number", "description": "金额，默认 1"},
        "from": {"type": "string", "description": "原来的币种，比如 USD 或 美元"},
        "to": {"type": "string", "description": "换成什么，比如 CNY，或者 CNY,JPY；默认人民币"}},
        "required": ["from"]}}}

_cache: dict[str, tuple[float, dict]] = {}   # 基准币种 -> (什么时候查的, er-api 返回的整份)


def code(s) -> str:
    """币种的写法 -> 三个字母的代码。不认识的抛 ValueError。"""
    s = str(s or "").strip()
    if not s:
        raise ValueError("缺少币种")
    if s in ALIASES:
        return ALIASES[s]
    up = s.upper()
    if up in ALIASES:
        return ALIASES[up]
    if re.fullmatch(r"[A-Z]{3}", up):
        return up
    raise ValueError(f"不认识的币种「{s}」，换成三个字母的代码试试（比如 USD、JPY）")


def amount_of(v) -> float:
    """金额：数字，或者“1,000”“3万”这样的文字。"""
    if v is None or v == "":
        return 1.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    mul = 1
    for unit, m in (("亿", 1e8), ("万", 1e4), ("千", 1e3)):
        if s.endswith(unit):
            s, mul = s[:-1], m
            break
    try:
        return float(s) * mul
    except ValueError:
        raise ValueError(f"金额「{v}」不是数字") from None


def targets(v) -> list[str]:
    parts = v if isinstance(v, list) else re.split(r"[,，、/\s]+", str(v or "CNY"))
    out = list(dict.fromkeys(code(p) for p in parts if str(p).strip()))
    if not out:
        raise ValueError("缺少要换成的币种")
    if len(out) > MAX_TO:
        raise ValueError(f"一次最多换 {MAX_TO} 种")
    return out


def label(c: str) -> str:
    return f"{NAMES[c]}（{c}）" if c in NAMES else c


def money(x: float) -> str:
    """金额：两位小数、千分位；不到 1 的小数（1 日元换美元那种）留 4 位有效数字。"""
    return f"{x:,.2f}" if abs(x) >= 1 or x == 0 else f"{x:.4g}"


def rate(x: float) -> str:
    return f"{x:,.6g}"


def convert(data: dict, amount: float, base: str, to: list[str]) -> str:
    """用 er-api 的一份汇率（基准是 base）算结果，写成给模型看的几行字。"""
    rates = data.get("rates") or {}
    missing = [c for c in to if c not in rates]
    if missing:
        raise ValueError(f"汇率表里没有 {'、'.join(missing)}")
    lines = []
    for c in to:
        r = float(rates[c])
        back = f"，1 {c} = {rate(1 / r)} {base}" if r else ""
        lines.append(f"{money(amount)} {label(base)} = {money(amount * r)} {label(c)}（1 {base} = {rate(r)} {c}{back}）")
    ts = data.get("time_last_update_unix")
    when = f"更新于 {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}，" if ts else ""
    return "\n".join(lines) + f"\n汇率{when}来自 open.er-api.com，每天更新一次；是参考汇率，不是银行的买入卖出价，实际换汇以银行牌价为准。"


async def fetch(base: str) -> dict:
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "open-super-agent exchange_rate"}) as c:
        r = await c.get(API + base)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"汇率服务返回的不是 JSON（HTTP {r.status_code}）") from None
    if data.get("result") != "success":
        if data.get("error-type") == "unsupported-code":
            raise ValueError(f"汇率服务不支持 {base}")
        raise RuntimeError(f"汇率服务出错：{data.get('error-type') or r.status_code}")
    return data


async def rates_for(base: str) -> dict:
    hit = _cache.get(base)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    data = await fetch(base)
    _cache[base] = (time.time(), data)
    return data


async def run_convert(args: dict) -> str:
    try:
        amount = amount_of(args.get("amount"))
        base = code(args.get("from"))
        to = [c for c in targets(args.get("to")) if c != base] or [base]
        return convert(await rates_for(base), amount, base, to)
    except ValueError as e:
        return f"没换算：{e}"
    except (httpx.HTTPError, RuntimeError) as e:
        return f"没换算：汇率服务暂时连不上（{e or type(e).__name__}），过一会儿再试"


def apply(ctx):
    async def run(call):
        return await run_convert(call.args)

    ctx.tool(Tool("fx_convert", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="汇率换算：外币和人民币互换，查当天的参考汇率",
                  progress=lambda a: f"查汇率 {a.get('from', '')} → {a.get('to') or 'CNY'}"))
