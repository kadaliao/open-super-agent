"""价格到线：A 股、港股、美股、加密货币、外汇。

行情都用免费、不用 key 的接口：股票用腾讯行情（qt.gtimg.cn，A 股、港股、美股都有），加密货币用 CoinGecko，
外汇用 open.er-api.com（一天只更新一次，所以外汇的涨跌幅是和上一次更新比）。
到线只报一次：从“没到”变成“到了”才报；价格回到线的另一边（多留 0.5% 的余量，免得在线附近来回报）以后再到线才再报。
涨跌幅一天只报一次。
"""
import re
import time
from ..i18n import N_
from ..registry import POLL, Source
from . import _util

MARGIN = 0.005     # 回到线另一边多少才算“回去了”，免得价格贴着线来回跳时一直报

# 常见的币：代号 → CoinGecko 的 id。不在这里的当成 id 直接用
CRYPTO = {
    "BTC": "bitcoin", "ETH": "ethereum", "USDT": "tether", "USDC": "usd-coin", "BNB": "binancecoin", "SOL": "solana",
    "XRP": "ripple", "DOGE": "dogecoin", "ADA": "cardano", "TRX": "tron", "TON": "the-open-network", "AVAX": "avalanche-2",
    "DOT": "polkadot", "LINK": "chainlink", "LTC": "litecoin", "BCH": "bitcoin-cash", "SHIB": "shiba-inu",
    "POL": "polygon-ecosystem-token", "MATIC": "matic-network", "UNI": "uniswap", "ATOM": "cosmos", "XLM": "stellar",
    "ETC": "ethereum-classic", "FIL": "filecoin", "APT": "aptos", "ARB": "arbitrum", "OP": "optimism", "SUI": "sui",
    "PEPE": "pepe", "NEAR": "near", "ICP": "internet-computer", "HBAR": "hedera-hashgraph", "XMR": "monero",
}
CCY = {"cn": "CNY", "hk": "HKD", "us": "USD"}


# ---------- 代码 ----------
def _exchange(code: str) -> str:
    """A 股 6 位代码 → 交易所。000001 这种和指数撞号的按股票算，要指数写 sh000001。"""
    if code.startswith(("92", "4", "8")):
        return "bj"
    if code.startswith(("6", "9", "5")):
        return "sh"
    return "sz"


def norm_symbol(market: str, symbol: str) -> str:
    """把用户写的代码整理成统一的样子；认不出来的 raise ValueError。"""
    s = re.sub(r"\s+", "", symbol or "")
    if not s:
        raise ValueError("要填代码")
    if market == "cn":
        m = re.fullmatch(r"(sh|sz|bj)?(\d{6})(?:\.(sh|sz|bj|ss))?", s.lower())
        if not m:
            raise ValueError("A 股代码是 6 位数字，比如 600519、sz000001（上证指数写 sh000001）")
        return (m[1] or {"ss": "sh"}.get(m[3] or "", m[3] or "") or _exchange(m[2])) + m[2]
    if market == "hk":
        s = s.upper().removeprefix("HK").removesuffix(".HK")
        if s.isdigit() and len(s) <= 5:
            return s.zfill(5)
        if re.fullmatch(r"[A-Z]{2,10}", s):     # 指数：HSI、HSTECH
            return s
        raise ValueError("港股代码是 5 位数字，比如 00700")
    if market == "us":
        s = s.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
            raise ValueError("美股写代号，比如 AAPL、TSLA")
        return s
    if market == "crypto":
        base, _, quote = s.partition("/")
        quote = (quote or "USD").upper()
        if not re.fullmatch(r"[A-Za-z0-9\-]{1,60}", base) or not re.fullmatch(r"[A-Z]{3,5}", quote):
            raise ValueError("加密货币写代号（BTC）或 CoinGecko 的 id（bitcoin），要人民币价格写 BTC/CNY")
        base = base.upper() if base.upper() in CRYPTO else base.lower()
        return base if quote == "USD" else f"{base}/{quote}"
    if market == "fx":
        m = re.fullmatch(r"([A-Za-z]{3})[/\-_]?([A-Za-z]{3})", s)
        if not m:
            raise ValueError("外汇写货币对，比如 USD/CNY")
        return f"{m[1]}/{m[2]}".upper()
    raise ValueError(f"不认识的市场：{market}")


def _num(x: float) -> str:
    if abs(x) >= 100:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.6g}"


def _clock(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


# ---------- 行情 ----------
def _tencent_time(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}" if len(d) >= 12 else (s or "")


async def _stock(market: str, symbol: str) -> dict:
    q = symbol if market == "cn" else f"{market}{symbol}"
    r = await _util.get(f"https://qt.gtimg.cn/q={q}", timeout=15, max_bytes=200_000)
    text = r.content.decode("gbk", errors="replace")
    m = re.search(r'v_[\w.\-]*="([^"]*)"', text)
    f = m[1].split("~") if m else []
    if len(f) < 33 or not f[1] or "none_match" in text:
        raise ValueError(f"没找到 {symbol} 的行情，检查一下代码和市场选对了没有")
    price, prev = float(f[3] or 0), float(f[4] or 0)
    try:
        pct = float(f[32])
    except ValueError:
        pct = (price / prev - 1) * 100 if prev else None
    when = _tencent_time(f[30])
    return {"name": f[1], "code": symbol, "price": price, "prev": prev, "pct": pct, "time": when,
            "day": when[:10], "ccy": CCY[market]}


async def _crypto(symbol: str) -> dict:
    base, _, quote = symbol.partition("/")
    quote = (quote or "USD").lower()
    cid = CRYPTO.get(base.upper(), base.lower())
    r = await _util.get("https://api.coingecko.com/api/v3/simple/price", timeout=15, max_bytes=200_000,
                        params={"ids": cid, "vs_currencies": quote, "include_24hr_change": "true",
                                "include_last_updated_at": "true"})
    d = (r.json() or {}).get(cid) or {}
    if d.get(quote) is None:
        raise ValueError(f"CoinGecko 上找不到 {symbol}：写常见代号（BTC、ETH）或者币的 id（bitcoin）")
    ts = d.get("last_updated_at") or time.time()
    return {"name": symbol, "code": symbol, "price": float(d[quote]),
            "prev": None, "pct": d.get(f"{quote}_24h_change"), "time": _clock(ts),
            "day": time.strftime("%Y-%m-%d", time.gmtime(ts)), "ccy": quote.upper(), "pct_what": "24 小时"}


async def _fx(symbol: str, st: dict) -> dict:
    a, b = symbol.split("/")
    r = await _util.get(f"https://open.er-api.com/v6/latest/{a}", timeout=15, max_bytes=500_000)
    d = r.json() or {}
    if d.get("result") != "success":
        raise ValueError(f"查不到 {a} 的汇率：{d.get('error-type') or '接口没返回数据'}")
    rate = (d.get("rates") or {}).get(b)
    if rate is None:
        raise ValueError(f"没有 {b} 这种货币的汇率")
    ts = d.get("time_last_update_unix") or 0
    fx = st.get("fx") if (st.get("fx") or {}).get("code") == symbol else {}
    prev = fx.get("prev")
    if fx.get("t") and fx["t"] != ts:       # 数据更新了：上一次的汇率当“昨收”
        prev = fx.get("rate")
    st["fx"] = {"code": symbol, "t": ts, "rate": rate, "prev": prev}
    return {"name": symbol, "code": symbol, "price": float(rate), "prev": prev,
            "pct": (rate / prev - 1) * 100 if prev else None, "time": _clock(ts),
            "day": time.strftime("%Y-%m-%d", time.gmtime(ts)), "ccy": b, "pct_what": "和上一次更新比"}


async def quote(params: dict, st: dict) -> dict:
    market = params.get("market") or "cn"
    symbol = norm_symbol(market, params.get("symbol") or "")
    if market in ("cn", "hk", "us"):
        return await _stock(market, symbol)
    if market == "crypto":
        return await _crypto(symbol)
    return await _fx(symbol, st)


# ---------- 判断 ----------
def _cond_text(cond: str, th) -> str:
    return {"above": f"涨到 {th} 以上", "below": f"跌到 {th} 以下"}.get(cond, f"涨跌幅超过 {th}%")


def judge(params: dict, q: dict, state: dict | None) -> tuple[list, dict]:
    """拿到行情以后：该不该报、新的 state。state 里 hit 是“现在在线的那一边”，day 是涨跌幅报过的那天。"""
    st = dict(state or {})
    cond, th = params.get("condition") or "above", float(params.get("threshold") or 0)
    price, pct = q["price"], q.get("pct")
    if not price:      # 停牌、还没开盘的新股：没有成交价，不动
        return [], st
    fire = False
    if cond in ("above", "below"):
        hit = price >= th if cond == "above" else price <= th
        back = price < th * (1 - MARGIN) if cond == "above" else price > th * (1 + MARGIN)
        if hit and not st.get("hit"):
            fire, st["hit"] = True, True
        elif st.get("hit") and back:
            st["hit"] = False
    else:
        if pct is not None and abs(pct) >= th and st.get("day") != q["day"]:
            fire, st["day"] = True, q["day"]
    st["last"] = price
    if not fire:
        return [], st
    sign = f"{pct:+.2f}%" if pct is not None else "—"
    what = _cond_text(cond, params.get("threshold"))
    code = f"{q['code']} " if q["name"] == q["code"] else f"{q['name']}（{q['code']}）"
    lines = [f"现价：{_num(price)} {q['ccy']}",
             f"涨跌：{sign}（{q.get('pct_what') or '今天'}" + (f"，昨收 {_num(q['prev'])}" if q.get("prev") and q.get("pct_what") is None else "") + "）",
             f"行情时间：{q['time']}" if q.get("time") else "",
             f"条件：{what}"]
    return [{"id": f"{q['code']}-{cond}-{q.get('time') or time.time()}",
             "title": f"{code}{what}：现价 {_num(price)}（{sign}）",
             "text": "\n".join(x for x in lines if x)}], st


async def check(params: dict, state):
    st = dict(state or {})
    q = await quote(params, st)
    return judge(params, q, st)


def validate(p: dict) -> dict:
    p["market"] = p.get("market") or "cn"
    p["condition"] = p.get("condition") or "above"
    p["symbol"] = norm_symbol(p["market"], p.get("symbol") or "")
    th = p.get("threshold")
    if th is None or th <= 0:
        raise ValueError("要填一个大于 0 的数：到价写价格，涨跌幅写百分比")
    return p


def summary(p: dict) -> str:
    th = p.get("threshold")
    return f"{p.get('symbol', '')} " + {"above": f"≥ {th}", "below": f"≤ {th}"}.get(p.get("condition") or "above", f"±{th}%")


PRICE = Source(
    id="price", mode=POLL, category=N_("金融"), icon="📈",
    title=N_("价格到线"),
    description=N_("股票（A 股、港股、美股）、加密货币、外汇的价格涨到、跌到某个数，或者涨跌幅超过几个百分点时提醒一次。"),
    fields=[
        {"key": "market", "label": N_("市场"), "type": "select", "default": "cn",
         "options": [{"value": "cn", "label": N_("A 股")}, {"value": "hk", "label": N_("港股")},
                     {"value": "us", "label": N_("美股")}, {"value": "crypto", "label": N_("加密货币")},
                     {"value": "fx", "label": N_("外汇")}]},
        {"key": "symbol", "label": N_("代码"), "type": "text", "required": True,
         "placeholder": N_("600519 / 00700 / AAPL / BTC / USD/CNY"),
         "help": N_("A 股写 6 位代码（600519、sz000001，上证指数写 sh000001）；港股写 5 位代码（00700）；美股写代号（AAPL）；"
                    "加密货币写代号（BTC、ETH）或 CoinGecko 的 id，默认按美元，要人民币写 BTC/CNY；外汇写货币对（USD/CNY）")},
        {"key": "condition", "label": N_("什么时候提醒"), "type": "select", "default": "above",
         "options": [{"value": "above", "label": N_("涨到（不低于）")}, {"value": "below", "label": N_("跌到（不高于）")},
                     {"value": "change", "label": N_("涨跌幅超过（%）")}]},
        {"key": "threshold", "label": N_("价格 / 百分比"), "type": "number", "required": True,
         "help": N_("到价写价格；涨跌幅写百分比，比如 5 就是涨或跌超过 5%。到线只报一次，回到线的另一边以后再到才再报；涨跌幅一天最多报一次")},
    ],
    check=check, every=15, min_every=5, fire_first=True, validate=validate, summary=summary,
    sample={"title": "贵州茅台（sh600519）涨到 1500 以上：现价 1,502.30（+3.46%）",
            "text": "现价：1,502.30 CNY\n涨跌：+3.46%（今天，昨收 1,452.00）\n行情时间：2026-11-06 14:32\n条件：涨到 1500 以上"},
    scenarios=[
        {"title": N_("股价到价提醒"), "description": N_("A 股涨到或跌到你设的价格时提醒一次"),
         "name": N_("到价提醒"), "params": {"market": "cn", "symbol": "600519", "condition": "above", "threshold": 1500},
         "prompt": N_("价格到了我设的线。用一两句话告诉我现在的价格和今天的涨跌；查一下今天有没有相关的新闻，有就附一句。不要给买卖建议。")},
        {"title": N_("股票大涨大跌提醒"), "description": N_("一天涨跌超过 5% 时提醒，顺便查查为什么"),
         "name": N_("大涨大跌"), "params": {"market": "cn", "symbol": "600519", "condition": "change", "threshold": 5},
         "prompt": N_("这只股票今天涨跌幅很大。查一下新闻和公告，用三五句话说说可能的原因；找不到明确原因就直说。不要给买卖建议。")},
        {"title": N_("比特币价格提醒"), "description": N_("BTC 跌破或涨过某个价格时告诉你"),
         "name": N_("比特币"), "params": {"market": "crypto", "symbol": "BTC", "condition": "below", "threshold": 60000},
         "prompt": N_("比特币到了我设的价格。告诉我现在的价格和 24 小时涨跌，查一下最近有没有大新闻，有就用一句话说。不要给买卖建议。")},
        {"title": N_("换汇时机"), "description": N_("比如美元兑人民币低于某个值时提醒你换汇"),
         "name": N_("换汇提醒"), "params": {"market": "fx", "symbol": "USD/CNY", "condition": "below", "threshold": 7},
         "prompt": N_("汇率到了我设的线，可以考虑换汇了。告诉我现在的汇率；提醒一句银行实际的买卖价会有点差价。")},
        {"title": N_("港股 / 美股自选到价"), "description": N_("港股、美股的自选股到价时提醒"),
         "name": N_("自选到价"), "params": {"market": "hk", "symbol": "00700", "condition": "above", "threshold": 450},
         "prompt": N_("自选股到了我设的价格。用一两句话告诉我现在的价格和今天的涨跌，有相关新闻就附一句。不要给买卖建议。")},
    ])

SOURCES = [PRICE]
