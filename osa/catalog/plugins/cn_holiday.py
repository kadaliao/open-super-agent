"""中国节假日：某天在中国大陆要不要上班（法定节假日、调休补班），下一个工作日、几个工作日以后是哪天，两个日期之间有几个工作日，一年的放假安排。

大模型不知道每年国务院发的放假安排，调休更是常算错。装上以后 Bot 问到放假、上班、工作日就调这个工具，对话里和后台任务里都能用。

- 数据来自 holiday-cn 项目（github.com/NateScarlet/holiday-cn，按国务院办公厅的通知整理，2007 年起）。
  第一次用到某一年时联网下载（jsDelivr，不行再试 GitHub，两个加起来最多等 5 秒），存在插件自己的数据目录里。
  过去的年份下载一次就不再联网；今年和以后的每天最多更新一次，更新不了就用存着的。
- 还没公布的年份（一般每年 11 月前后公布下一年）、下载不了又没存过的，按周一到周五算，结果里会写明。
- 只管中国大陆的全国性假日，不管港澳台和各地自己的假日（比如少数民族节日）。“今天”按服务器的时区算。
"""
import asyncio
import json
import os
import time
from datetime import date, timedelta
import httpx
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "中国节假日", "version": "1.0", "author": "open-super-agent"}
URLS = ("https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
        "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json")
TIMEOUTS = (3, 2)          # 两个地址加起来不超过 5 秒：对话里（Router）用也不会卡太久
REFRESH = 86400            # 今年和以后的数据多久更新一次
FIRST_YEAR = 2007
MAX_DAYS = 3700            # count 最多算 10 年
MAX_N = 1000
WEEK = "一二三四五六日"

SCHEMA = {"type": "function", "function": {
    "name": "cn_holiday",
    "description": "中国大陆的法定节假日和调休，问哪天放假、要不要上班、工作日时一定用它，不要凭记忆（每年的调休都不一样）。"
                   "op=check（默认）：date 这天上不上班、为什么（节假日 / 调休补班 / 周末），还有下一个假期；"
                   "op=add：date 往后第 n 个工作日是哪天（n 默认 1，就是下一个工作日；负数往前）；"
                   "op=count：from 到 to（含首尾两天）有几个工作日、几天休息；"
                   "op=list：year 年的放假安排（每个节日放哪几天、哪几天调休上班）。日期写 YYYY-MM-DD，不写就是今天。",
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["check", "add", "count", "list"]},
        "date": {"type": "string", "description": "check、add 用，默认今天"},
        "n": {"type": "integer", "description": "add 用：第几个工作日，默认 1"},
        "from": {"type": "string", "description": "count 用：开始日期"},
        "to": {"type": "string", "description": "count 用：结束日期"},
        "year": {"type": "integer", "description": "list 用，默认今年"}},
        "required": []}}}


# ---------- 数据：下载、缓存 ----------
def parse(data: dict) -> dict[date, tuple[str, bool]]:
    """holiday-cn 的一年 → {日期: (节日名, 是不是放假)}。放假为 False 的是调休上班。"""
    out = {}
    for d in data.get("days") or []:
        if isinstance(d, dict) and isinstance(d.get("date"), str) and isinstance(d.get("isOffDay"), bool):
            out[date.fromisoformat(d["date"])] = (str(d.get("name") or "节假日"), d["isOffDay"])
    return out


async def fetch(year: int) -> dict | None:
    """下载一年的数据。还没公布（404）返回 None；网络出错抛异常。"""
    err = None
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "open-super-agent cn_holiday"}) as c:
        for url, timeout in zip(URLS, TIMEOUTS):
            try:
                r = await c.get(url.format(year=year), timeout=timeout)
                if r.status_code == 404:
                    return None   # jsDelivr 是 GitHub 的镜像，它说没有就是没有，不用再试
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                err = e
    raise RuntimeError(f"下载不了（{type(err).__name__}）")


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    os.replace(tmp, path)


async def load_year(data_dir, year: int, today: date) -> dict | str:
    """一年的数据：{日期: (名字, 放假)}，没有时返回一句原因。缓存在 data_dir/<年>.json。"""
    if year < FIRST_YEAR:
        return f"{year} 年太早，没有数据"
    if year > today.year + 1:
        return f"{year} 年的放假安排还没公布"
    path = data_dir / f"{year}.json"
    cached = _read(path)
    fresh = cached and (year < today.year and cached.get("data") or time.time() - cached.get("fetched", 0) < REFRESH)
    if not fresh:
        try:
            got = await fetch(year)
            cached = {"fetched": time.time(), "data": got}
            _write(path, cached)
        except Exception as e:
            if not cached:
                return f"{year} 年的节假日数据{e}，也没有存过"
    if not cached.get("data"):
        return f"{year} 年的放假安排还没公布"
    return parse(cached["data"])


async def load_years(data_dir, years, today: date) -> dict[int, dict | str]:
    years = sorted(set(years))
    got = await asyncio.gather(*(load_year(data_dir, y, today) for y in years))
    return dict(zip(years, got))


# ---------- 算 ----------
def _day(s, today: date) -> date:
    return date.fromisoformat(str(s).strip()) if s else today


def _fmt(d: date, year: bool = True) -> str:
    return f"{d.isoformat() if year else d.strftime('%m-%d')}（星期{WEEK[d.weekday()]}）"


class Calendar:
    def __init__(self, years: dict[int, dict | str]):
        self.years = years
        self.guessed: set[int] = set()   # 用周一到周五顶替了的年份

    def info(self, d: date) -> tuple[bool, str, str]:
        """(上不上班, 原因, 节日名)。"""
        y = self.years.get(d.year)
        if isinstance(y, dict):
            if d in y:
                name, off = y[d]
                return (False, "放假", name) if off else (True, "调休上班", name)
        else:
            self.guessed.add(d.year)
        return (False, "周末", "") if d.weekday() >= 5 else (True, "工作日", "")

    def work(self, d: date) -> bool:
        return self.info(d)[0]

    def offs(self) -> list[tuple[date, str]]:
        return sorted((d, n) for y in self.years.values() if isinstance(y, dict) for d, (n, off) in y.items() if off)

    def block(self, d: date, name: str) -> tuple[date, date]:
        """d 所在的这个假期从哪天到哪天（同一个名字、连着放的）。"""
        days = {x for x, n in self.offs() if n == name}
        a = b = d
        while a - timedelta(days=1) in days:
            a -= timedelta(days=1)
        while b + timedelta(days=1) in days:
            b += timedelta(days=1)
        return a, b

    def notes(self) -> str:
        out = [self.years[y] if isinstance(self.years.get(y), str) else f"{y} 年没有数据" for y in sorted(self.guessed)]
        return "\n（" + "；".join(out) + "，这些日子按周一到周五上班算。）" if out else ""


def needed(args: dict, today: date) -> set[int]:
    """这次要用到哪几年的数据（先下载好，再算）。"""
    op = args.get("op") or "check"
    if op == "list":
        return {int(args.get("year") or today.year)}
    if op == "count":
        a, b = sorted((_day(args.get("from"), today), _day(args.get("to"), today)))
        if (b - a).days > MAX_DAYS:
            raise ValueError("最多算 10 年")
        return set(range(a.year, b.year + 1))
    d = _day(args.get("date"), today)
    if op == "add":
        n = int(args.get("n") or 1)
        if abs(n) > MAX_N:
            raise ValueError(f"n 最多 {MAX_N}")
        end = d + timedelta(days=int(n * 1.6) + (30 if n > 0 else -30))
        return set(range(min(d, end).year, max(d, end).year + 1))
    return {d.year, d.year + 1}


def calc(args: dict, years: dict[int, dict | str], today: date | None = None) -> str:
    today = today or date.today()
    op = args.get("op") or "check"
    cal = Calendar(years)
    if op == "check":
        d = _day(args.get("date"), today)
        work, why, name = cal.info(d)
        lines = [f"{_fmt(d)}：{'要上班' if work else '不上班'}（" + (f"{name}{why}" if name else why) + "）。"]
        if name and not work:
            a, b = cal.block(d, name)
            back = b + timedelta(days=1)
            while not cal.work(back) and (back - b).days < 30:
                back += timedelta(days=1)
            lines.append(f"这个{name}假期从 {_fmt(a, False)}到 {_fmt(b, False)}，共 {(b - a).days + 1} 天，{_fmt(back)}上班。")
        offs = cal.offs()
        starts = set(offs)
        nxt = next(((x, n) for x, n in offs if x > d and n != (name if not work else None)
                    and (x - timedelta(days=1), n) not in starts), None)   # 下一个假期的第一天
        if nxt:
            a, b = cal.block(*nxt)
            lines.append(f"下一个假期：{nxt[1]}，{_fmt(a)}起放 {(b - a).days + 1} 天，还有 {(a - d).days} 天。")
        return "\n".join(lines) + cal.notes()
    if op == "add":
        d, n = _day(args.get("date"), today), int(args.get("n") or 1)
        step = 1 if n > 0 else -1
        x, left = d, abs(n)
        while left:
            x += timedelta(days=step)
            left -= cal.work(x)
        word = "往后" if n > 0 else "往前"
        return f"{_fmt(d)}{word}第 {abs(n)} 个工作日是 {_fmt(x)}。" + cal.notes()
    if op == "count":
        a, b = sorted((_day(args.get("from"), today), _day(args.get("to"), today)))
        work = holiday = weekend = 0
        swaps = []
        x = a
        while x <= b:
            ok, why, _ = cal.info(x)
            work += ok
            holiday += why == "放假"
            weekend += why == "周末"
            if why == "调休上班":
                swaps.append(_fmt(x, False))
            x += timedelta(days=1)
        total = (b - a).days + 1
        out = (f"{_fmt(a)}到 {_fmt(b)}，含首尾共 {total} 天：工作日 {work} 天，休息 {total - work} 天"
               f"（节假日 {holiday} 天、周末 {weekend} 天）")
        out += f"，其中调休上班 {len(swaps)} 天：{'、'.join(swaps[:10])}{'……' if len(swaps) > 10 else ''}。" if swaps else "。"
        return out + cal.notes()
    if op == "list":
        y = int(args.get("year") or today.year)
        data = years.get(y)
        if not isinstance(data, dict):
            return f"{data}。"
        groups: dict[str, dict] = {}
        for x in sorted(data):
            name, off = data[x]
            groups.setdefault(name, {"off": [], "work": []})["off" if off else "work"].append(x)
        lines = [f"{y} 年放假安排（国务院办公厅通知）："]
        for name, g in sorted(groups.items(), key=lambda kv: min(kv[1]["off"] or kv[1]["work"])):
            ranges, run = [], []
            for x in g["off"]:
                if run and (x - run[-1]).days != 1:
                    ranges.append(run)
                    run = []
                run.append(x)
            if run:
                ranges.append(run)
            parts = [f"{_fmt(r[0], False)}到 {_fmt(r[-1], False)}放假，共 {len(r)} 天" if len(r) > 1
                     else f"{_fmt(r[0], False)}放假 1 天" for r in ranges]
            if g["work"]:
                parts.append("调休上班：" + "、".join(_fmt(x, False) for x in g["work"]))
            lines.append(f"- {name}：" + "；".join(parts))
        total = sum(1 for n, off in data.values() if off)
        lines.append(f"全年放假 {total} 天（含连在一起的周末），调休上班 {sum(1 for n, off in data.values() if not off)} 天。")
        return "\n".join(lines)
    raise ValueError(f"不认识的 op：{op}")


def apply(ctx):
    async def run(call):
        today = date.today()
        try:
            years = await load_years(ctx.data_dir, needed(call.args, today), today)
            return calc(call.args, years, today)
        except (ValueError, TypeError) as e:
            return f"没查：{e}（日期写 YYYY-MM-DD）"
    ctx.tool(Tool("cn_holiday", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="中国节假日：哪天放假、调休上班、下一个工作日、两个日期之间几个工作日、全年放假安排",
                  progress=lambda a: f"查节假日：{a.get('date') or a.get('year') or a.get('from') or '今天'}"))
