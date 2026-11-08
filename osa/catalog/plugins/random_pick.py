"""抽签和随机：从名单里抽几个（不重复）、打乱顺序、随机数、掷骰子（2d6）、抛硬币、随机分组。

大模型自己“随机”其实不随机：爱挑前几个、偏爱 7 和 42 这种数，同样的问题问两遍可能还是同一个答案。
装上以后要抽签、摇号、排顺序、分组，Bot 就调这个工具，用的是系统的安全随机数（Python 的 secrets），每次都真的随机，谁也没法事先知道。
"""
import re
import secrets
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "抽签和随机", "version": "1.0", "author": "open-super-agent"}
RNG = secrets.SystemRandom()
MAX_ITEMS = 10_000
MAX_N = 1000
MAX_SHOW = 200   # 结果最多列出多少个
NOTE = "（系统安全随机数抽的，真随机）"

SCHEMA = {"type": "function", "function": {
    "name": "random_pick",
    "description": "需要随机结果时一定用它（抽签、抽奖、摇号、点名、决定顺序、分组、掷骰子、抛硬币），不要自己编：模型挑的不随机。"
                   "op=pick：从 items 里抽 n 个（不重复，默认 1 个）；op=shuffle：打乱 items 的顺序；"
                   "op=number：min 到 max 之间的随机整数 n 个（默认不重复，repeat=true 可以重复）；"
                   "op=dice：掷骰子，dice 写 2d6、d20、3d6+2；op=coin：抛 n 次硬币；"
                   "op=group：把 items 随机分成 groups 组，或者每组 size 人。items 是列表，或者用逗号、换行分开的一串。",
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["pick", "shuffle", "number", "dice", "coin", "group"]},
        "items": {"type": "array", "items": {"type": "string"}, "description": "名单或选项，一项一个"},
        "n": {"type": "integer", "description": "抽几个 / 几个数 / 抛几次"},
        "min": {"type": "integer"}, "max": {"type": "integer"},
        "repeat": {"type": "boolean", "description": "number 用：数字能不能重复"},
        "dice": {"type": "string", "description": "dice 用：比如 2d6、d20、3d6+2"},
        "groups": {"type": "integer", "description": "group 用：分几组"},
        "size": {"type": "integer", "description": "group 用：每组几人（和 groups 二选一）"}},
        "required": ["op"]}}}


def items_of(v) -> list[str]:
    """列表或一串文字 → 选项列表。文字先按换行、逗号、顿号、分号分；都没有就按空格分。"""
    if isinstance(v, list):
        out = [str(x).strip() for x in v]
    else:
        s = str(v or "").strip()
        out = re.split(r"[\n,，、;；]+", s) if re.search(r"[\n,，、;；]", s) else s.split()
    out = [x.strip() for x in out if x and x.strip()]
    if not out:
        raise ValueError("items 是空的：写上名单或者选项")
    if len(out) > MAX_ITEMS:
        raise ValueError(f"最多 {MAX_ITEMS} 个选项")
    return out


def _n(args: dict, default: int = 1, hi: int = MAX_N) -> int:
    n = int(args.get("n") or default)
    if not 1 <= n <= hi:
        raise ValueError(f"n 要在 1 到 {hi} 之间")
    return n


def _list(xs, sep: str = "、") -> str:
    xs = [str(x) for x in xs]
    return sep.join(xs[:MAX_SHOW]) + (f"……（一共 {len(xs)} 个）" if len(xs) > MAX_SHOW else "")


def pick(args: dict) -> str:
    items = items_of(args.get("items"))
    n = _n(args)
    if n > len(items):
        raise ValueError(f"只有 {len(items)} 个，抽不出 {n} 个不重复的")
    got = RNG.sample(items, n)
    if n == 1:
        return f"从 {len(items)} 个里抽中：{got[0]}"
    return f"从 {len(items)} 个里抽 {n} 个（按抽中的先后）：{_list(got)}"


def shuffle(args: dict) -> str:
    items = items_of(args.get("items"))
    RNG.shuffle(items)
    return "打乱后的顺序：\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(items[:MAX_SHOW], 1)) + \
        (f"\n……（一共 {len(items)} 个）" if len(items) > MAX_SHOW else "")


def number(args: dict) -> str:
    lo = int(args["min"]) if args.get("min") is not None else 1
    hi = int(args["max"]) if args.get("max") is not None else 100
    if lo > hi:
        lo, hi = hi, lo
    if hi - lo > 10**18:
        raise ValueError("范围太大")
    n, repeat = _n(args), bool(args.get("repeat"))
    size = hi - lo + 1
    if not repeat and n > size:
        raise ValueError(f"{lo} 到 {hi} 只有 {size} 个数，抽不出 {n} 个不重复的（要能重复写 repeat=true）")
    got = [RNG.randint(lo, hi) for _ in range(n)] if repeat else RNG.sample(range(lo, hi + 1), n)
    if n == 1:
        return f"{lo} 到 {hi} 之间的随机整数：{got[0]}"
    out = f"{lo} 到 {hi} 之间抽 {n} 个{'（可以重复）' if repeat else '（不重复）'}：{_list(got)}"
    return out + (f"\n从小到大：{_list(sorted(got))}" if n <= MAX_SHOW else "")


def dice(args: dict) -> str:
    spec = str(args.get("dice") or "1d6").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d*)d(\d+)([+-]\d+)?", spec)
    if not m:
        raise ValueError("dice 写成 2d6、d20、3d6+2 这样")
    count, sides, bonus = int(m.group(1) or 1), int(m.group(2)), int(m.group(3) or 0)
    if not 1 <= count <= 100 or not 2 <= sides <= 1_000_000:
        raise ValueError("最多 100 个骰子，每个 2 到 1000000 面")
    rolls = [RNG.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + bonus
    detail = " + ".join(map(str, rolls)) + (f" {'+' if bonus > 0 else '-'} {abs(bonus)}" if bonus else "")
    return f"掷 {spec}：{detail} = {total}" if count > 1 or bonus else f"掷 {spec}：{total}"


def coin(args: dict) -> str:
    n = _n(args)
    flips = [RNG.choice("正反") for _ in range(n)]
    if n == 1:
        return f"抛硬币：{flips[0]}面"
    heads = flips.count("正")
    return f"抛 {n} 次：{_list(flips, '')}\n正面 {heads} 次，反面 {n - heads} 次"


def group(args: dict) -> str:
    items = items_of(args.get("items"))
    if args.get("groups"):
        k = int(args["groups"])
    elif args.get("size"):
        size = int(args["size"])
        if size < 1:
            raise ValueError("size 要大于 0")
        k = -(-len(items) // size)
    else:
        raise ValueError("写 groups（分几组）或 size（每组几人）")
    if not 1 <= k <= len(items):
        raise ValueError(f"{len(items)} 个人分不成 {k} 组")
    RNG.shuffle(items)
    if args.get("size"):   # 按每组人数分：前面的组满员，最后一组可能少
        teams = [items[i:i + int(args["size"])] for i in range(0, len(items), int(args["size"]))]
    else:                  # 按组数分：每组人数最多差 1
        teams = [items[i::k] for i in range(k)]
    return f"{len(items)} 个分成 {len(teams)} 组：\n" + "\n".join(
        f"第 {i} 组（{len(t)}）：{_list(t)}" for i, t in enumerate(teams, 1))


OPS = {"pick": pick, "shuffle": shuffle, "number": number, "dice": dice, "coin": coin, "group": group}


def calc(args: dict) -> str:
    op = args.get("op") or ("pick" if args.get("items") else "number")
    if op not in OPS:
        raise ValueError(f"不认识的 op：{op}")
    out = OPS[op](args)
    return out + ("\n" if "\n" in out else "") + NOTE


def apply(ctx):
    async def run(call):
        try:
            return calc(call.args)
        except (ValueError, TypeError, KeyError) as e:
            return f"没抽：{e}"
    ctx.tool(Tool("random_pick", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="抽签和随机：从名单抽人、打乱顺序、随机数、掷骰子、抛硬币、随机分组（真随机）",
                  progress=lambda a: f"随机：{a.get('op') or ''}"))
