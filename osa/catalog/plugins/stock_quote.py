"""行情查询：A 股、港股、美股、指数和常见加密货币的最新价、涨跌、涨跌幅，对话里直接能查。

股票和指数用腾讯财经的行情接口，加密货币用 CoinGecko，都不用 key、不用设置。
代码的写法：A 股 600519 或 sh600519、港股 00700 或 0700.HK、美股 AAPL、加密货币 BTC、ETH；
几个很常见的也能直接写名字（茅台、腾讯、苹果、上证指数、恒指、纳指、比特币……）。
行情可能有延迟（港股、美股尤其），只供参考，不构成投资建议。
"""
import asyncio
import re
from datetime import datetime
import httpx
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "行情查询", "version": "1.0", "author": "open-super-agent"}
TENCENT = "https://qt.gtimg.cn/q="
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
MAX_SYMBOLS = 20

# 名字 -> 腾讯的代码（只收最常见的；别的写代码）
NAMES = {
    "茅台": "sh600519", "贵州茅台": "sh600519", "腾讯": "hk00700", "腾讯控股": "hk00700", "阿里巴巴": "hk09988",
    "阿里": "hk09988", "美团": "hk03690", "小米": "hk01810", "比亚迪": "sz002594", "宁德时代": "sz300750",
    "中国平安": "sh601318", "招商银行": "sh600036", "工商银行": "sh601398", "苹果": "usAAPL", "特斯拉": "usTSLA",
    "英伟达": "usNVDA", "微软": "usMSFT", "谷歌": "usGOOGL", "亚马逊": "usAMZN",
    "上证指数": "sh000001", "上证": "sh000001", "深证成指": "sz399001", "创业板指": "sz399006", "沪深300": "sh000300",
    "科创50": "sh000688", "恒生指数": "hkHSI", "恒指": "hkHSI", "恒生科技": "hkHSTECH", "恒生科技指数": "hkHSTECH",
    "纳斯达克": "usIXIC", "纳指": "usIXIC", "道琼斯": "usDJI", "道指": "usDJI", "标普500": "usINX", "标普": "usINX",
}
# 加密货币：符号或中文名 -> (CoinGecko 的 id, 显示的符号)
COINS = {
    "BTC": "bitcoin", "比特币": "bitcoin", "ETH": "ethereum", "以太坊": "ethereum", "USDT": "tether", "泰达币": "tether",
    "USDC": "usd-coin", "BNB": "binancecoin", "币安币": "binancecoin", "SOL": "solana", "XRP": "ripple", "瑞波币": "ripple",
    "DOGE": "dogecoin", "狗狗币": "dogecoin", "ADA": "cardano", "TRX": "tron", "波场": "tron", "TON": "the-open-network",
    "AVAX": "avalanche-2", "DOT": "polkadot", "波卡": "polkadot", "LINK": "chainlink", "LTC": "litecoin", "莱特币": "litecoin",
    "BCH": "bitcoin-cash", "SHIB": "shiba-inu", "SUI": "sui", "PEPE": "pepe",
}
COIN_SYMBOL = {cid: s for s, cid in COINS.items() if s.isascii()}
COIN_NAME = {"bitcoin": "比特币", "ethereum": "以太坊", "tether": "泰达币", "ripple": "瑞波币", "dogecoin": "狗狗币",
             "tron": "波场", "polkadot": "波卡", "litecoin": "莱特币", "binancecoin": "币安币"}
UNIT = {"sh": "元", "sz": "元", "bj": "元", "hk": "港元", "us": "美元"}
ZONE = {"sh": "北京时间", "sz": "北京时间", "bj": "北京时间", "hk": "北京时间", "us": "美东时间"}

SCHEMA = {"type": "function", "function": {
    "name": "quote",
    "description": "查股票、指数、加密货币的最新行情（价格、涨跌、涨跌幅、时间）。用户问股价、大盘、币价时用它，不要凭记忆报价。"
                   "代码写法：A 股 600519 或 sh600519，港股 00700 或 0700.HK，美股 AAPL，指数 上证指数 / 恒指 / 纳指，"
                   "加密货币 BTC、ETH；茅台、腾讯、苹果、比特币这类很常见的可以直接写名字，其他的先查到代码再来。"
                   "美股代码和币名撞了（比如 SOL）时美股写 usSOL。",
    "parameters": {"type": "object", "properties": {
        "symbols": {"type": "array", "items": {"type": "string"}, "description": f"代码或名字，最多 {MAX_SYMBOLS} 个"}},
        "required": ["symbols"]}}}


def normalize(s) -> tuple[str, str]:
    """一个代码或名字 -> ("stock", 腾讯的代码) 或 ("coin", CoinGecko 的 id)。认不出的抛 ValueError。"""
    raw = str(s or "").strip()
    if not raw:
        raise ValueError("空的代码")
    if raw in NAMES:
        return "stock", NAMES[raw]
    up = raw.upper()
    coin = re.sub(r"[-/]?(USDT|USD|CNY)$", "", up) if up not in COINS else up   # BTC-USD、BTCUSDT 也算
    if raw in COINS or coin in COINS:
        return "coin", COINS.get(raw) or COINS[coin]
    low = raw.lower()
    if m := re.fullmatch(r"(sh|sz|bj)(\d{6})", low):
        return "stock", m[1] + m[2]
    if m := re.fullmatch(r"(\d{6})\.(sh|ss|sz|bj)", low):
        return "stock", ("sh" if m[2] == "ss" else m[2]) + m[1]
    if re.fullmatch(r"\d{6}", low):   # 只写了 6 位数字：按开头猜交易所
        return "stock", ("sh" if low[0] in "569" else "bj" if low[0] in "48" else "sz") + low
    if m := re.fullmatch(r"hk(\d{1,5})|(\d{1,5})\.hk|(\d{1,5})", low):
        return "stock", "hk" + (m[1] or m[2] or m[3]).zfill(5)
    # 前缀只认小写的 hk、us（或者带点的 US.）：大写的 USB、HKIT 是美股代码本身
    if m := re.fullmatch(r"hk([A-Za-z]+)", raw):   # 港股指数：hkHSI、hkHSTECH（腾讯只认大写）
        return "stock", "hk" + m[1].upper()
    if m := re.fullmatch(r"(?:us\.?|US\.)([A-Za-z][A-Za-z.]{0,7})|([A-Za-z][A-Za-z.]{0,7})", raw):
        return "stock", "us" + (m[1] or m[2]).upper()
    raise ValueError(f"认不出「{raw}」，写股票代码（600519、00700、AAPL）或者币的符号（BTC）")


def parse_tencent(text: str) -> dict[str, list[str]]:
    """腾讯的回复（已经按 GBK 解码）-> {代码: 字段列表}。一行一个：v_sh600519="1~贵州茅台~600519~…";
    没查到的代码不出现（全都没查到时只有一行 v_pv_none_match）。"""
    out = {}
    for line in text.splitlines():
        m = re.match(r'\s*v_([A-Za-z0-9.]+)="(.*)";?\s*$', line)
        if m and m[1] != "pv_none_match":
            fields = m[2].split("~")
            if len(fields) > 34:
                out[m[1]] = fields
    return out


def _f(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def num(x: float) -> str:
    """按实际有几位小数显示：419.800 -> 419.80，0.123 -> 0.123。"""
    for d in (2, 3, 4):
        if round(x, d) == x:
            return f"{x:,.{d}f}"
    return f"{x:,.4f}"


def _time(s: str) -> str:
    d = re.sub(r"\D", "", s or "")   # 20261106161458、2026/11/06 16:08:54、2026-11-06 16:00:01 都有
    return f"{d[:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}" if len(d) >= 12 else (s or "")


def is_index(sym: str, f: list[str]) -> bool:
    return f[2].startswith(".") or bool(re.fullmatch(r"hk[A-Z]+|sh000\d{3}|sz399\d{3}", sym))


def stock_line(sym: str, f: list[str]) -> str:
    """腾讯的一条行情 -> 一行字。字段：1 名字、3 现价、4 昨收、5 今开、30 时间、31 涨跌、32 涨跌幅%、33 最高、34 最低。"""
    market = sym[:2]
    unit = "点" if is_index(sym, f) else UNIT.get(market, "")
    price, prev = _f(f[3]), _f(f[4])
    when = f"{_time(f[30])}（{ZONE.get(market, '')}）"
    if not price:
        return f"{f[1]}（{sym}）：现在没有成交（停牌，或者还没开盘），昨收 {num(prev)} {unit}；{when}"
    chg, pct = _f(f[31]), _f(f[32])
    sign = "+" if chg > 0 else ""
    return (f"{f[1]}（{sym}）：{num(price)} {unit}，{sign}{num(chg)}（{sign}{pct:.2f}%），"
            f"今开 {num(_f(f[5]))}，最高 {num(_f(f[33]))}，最低 {num(_f(f[34]))}，昨收 {num(prev)}；{when}")


def coin_line(cid: str, d: dict) -> str:
    sym = COIN_SYMBOL.get(cid, cid)
    name = f"{COIN_NAME[cid]}（{sym}）" if cid in COIN_NAME else sym
    usd, cny, pct = d.get("usd"), d.get("cny"), d.get("usd_24h_change")
    price = "，".join(x for x in (f"${num(usd)}" if usd is not None else "", f"¥{num(cny)}" if cny is not None else "") if x)
    change = f"，24 小时 {pct:+.2f}%" if pct is not None else ""
    ts = d.get("last_updated_at")
    when = f"；更新于 {datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}" if ts else ""
    return f"{name}：{price or '没有价格'}{change}{when}"


async def fetch_tencent(client: httpx.AsyncClient, syms: list[str]) -> str:
    r = await client.get(TENCENT + ",".join(syms))
    r.raise_for_status()
    return r.content.decode("gbk", errors="replace")


async def fetch_coins(client: httpx.AsyncClient, ids: list[str]) -> dict:
    r = await client.get(COINGECKO, params={"ids": ",".join(ids), "vs_currencies": "usd,cny",
                                            "include_24hr_change": "true", "include_last_updated_at": "true"})
    if r.status_code == 429:
        raise RuntimeError("CoinGecko 说查得太频繁了，过一分钟再试")
    r.raise_for_status()
    return r.json()


def _symbols(v) -> list:
    items = v if isinstance(v, list) else re.split(r"[,，、;\s]+", str(v or ""))
    return [x for x in items if str(x).strip()]


async def lookup(args: dict) -> str:
    items = _symbols(args.get("symbols"))
    if not items:
        return "没查：没给代码"
    if len(items) > MAX_SYMBOLS:
        return f"没查：一次最多 {MAX_SYMBOLS} 个"
    stocks, coins, bad = {}, {}, []
    for it in items:
        try:
            kind, key = normalize(it)
        except ValueError as e:
            bad.append(str(e))
            continue
        (stocks if kind == "stock" else coins).setdefault(key, str(it).strip())
    lines = []
    async with httpx.AsyncClient(timeout=6, headers={"User-Agent": "Mozilla/5.0 open-super-agent stock_quote"}) as c:
        jobs = [fetch_tencent(c, list(stocks)) if stocks else None, fetch_coins(c, list(coins)) if coins else None]
        got = await asyncio.gather(*(j for j in jobs if j), return_exceptions=True)
    got = iter(got)
    if stocks:
        res = next(got)
        if isinstance(res, Exception):
            lines.append(f"股票没查到：行情服务连不上（{res or type(res).__name__}）")
        else:
            rows = parse_tencent(res)
            lines += [stock_line(s, rows[s]) for s in stocks if s in rows]
            bad += [f"没找到「{name}」（{s}）：代码不对，或者这个市场查不了" for s, name in stocks.items() if s not in rows]
    if coins:
        res = next(got)
        if isinstance(res, Exception):
            lines.append(f"加密货币没查到：{res if isinstance(res, RuntimeError) else 'CoinGecko 连不上'}")
        else:
            lines += [coin_line(cid, res[cid]) for cid in coins if cid in res]
            bad += [f"没找到「{name}」" for cid, name in coins.items() if cid not in res]
    out = "\n".join(lines + bad)
    if lines:
        out += "\n行情可能有延迟（港股、美股尤其），只供参考，不构成投资建议。"
    return out


def apply(ctx):
    async def run(call):
        return await lookup(call.args)

    ctx.tool(Tool("quote", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="查 A 股、港股、美股、指数、加密货币的最新行情",
                  progress=lambda a: f"查行情 {'、'.join(map(str, _symbols(a.get('symbols'))[:5]))}"))
