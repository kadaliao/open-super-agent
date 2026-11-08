"""日期计算：两个日期差几天、几个工作日，某天往后或往前几天、几个月是哪天，某天是星期几。

大模型算日期经常出错（尤其跨月、算工作日）。装上以后 Bot 遇到日期计算直接调这个工具，对话里和后台任务里都能用。
工作日只算周一到周五，不知道法定节假日和调休。日期按服务器的时区算“今天”。
"""
import calendar
from datetime import date, timedelta
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "日期计算", "version": "1.0", "author": "open-super-agent"}
WEEK = "一二三四五六日"

SCHEMA = {"type": "function", "function": {
    "name": "date_calc",
    "description": "日期计算，算日期时一定用它，不要自己心算。op=diff：from 到 to 相差几天、几周、几个工作日；"
                   "op=add：from 加上 days / weeks / months（可以是负数）是哪天；op=weekday：from 是星期几、第几周。"
                   "日期写 YYYY-MM-DD，from 不写就是今天。",
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["diff", "add", "weekday"]},
        "from": {"type": "string", "description": "起始日期 YYYY-MM-DD，默认今天"},
        "to": {"type": "string", "description": "diff 用：结束日期 YYYY-MM-DD"},
        "days": {"type": "integer"}, "weeks": {"type": "integer"}, "months": {"type": "integer"}},
        "required": ["op"]}}}


def _day(s, default: date | None = None) -> date:
    if not s:
        if default is None:
            raise ValueError("缺少日期")
        return default
    return date.fromisoformat(str(s).strip())


def _fmt(d: date) -> str:
    return f"{d.isoformat()}（星期{WEEK[d.weekday()]}）"


def add_months(d: date, n: int) -> date:
    """加几个月；目标月没有这一天时取月底（1 月 31 日加 1 个月是 2 月 28 / 29 日）。"""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def workdays(a: date, b: date) -> int:
    """a 之后到 b（含 b）有几个周一到周五；b 在 a 之前时是负数。"""
    sign, (lo, hi) = (1, (a, b)) if b >= a else (-1, (b, a))
    n = 0
    d = lo + timedelta(days=1)
    while d <= hi:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return sign * n


def calc(args: dict, today: date | None = None) -> str:
    today = today or date.today()
    op = args.get("op")
    start = _day(args.get("from"), today)
    if op == "diff":
        end = _day(args.get("to"))
        n = (end - start).days
        w, r = divmod(abs(n), 7)
        weeks = f"{w} 周" + (f" {r} 天" if r else "")
        return (f"从 {_fmt(start)} 到 {_fmt(end)}：{'相差' if n >= 0 else '倒退'} {abs(n)} 天（{weeks}），"
                f"其中工作日（周一到周五，不算节假日）{abs(workdays(start, end))} 个。")
    if op == "add":
        days, weeks, months = (int(args.get(k) or 0) for k in ("days", "weeks", "months"))
        d = add_months(start, months) + timedelta(days=days, weeks=weeks)
        parts = [f"{v:+d} {u}" for v, u in ((months, "个月"), (weeks, "周"), (days, "天")) if v]
        return f"{_fmt(start)} {' '.join(parts) or '+0 天'} 是 {_fmt(d)}，离今天 {(d - today).days:+d} 天。"
    if op == "weekday":
        iso = start.isocalendar()
        return (f"{_fmt(start)}：{iso.year} 年第 {iso.week} 周，这一年的第 {start.timetuple().tm_yday} 天，"
                f"离今天 {(start - today).days:+d} 天。")
    raise ValueError(f"不认识的 op：{op}")


def apply(ctx):
    async def run(call):
        try:
            return calc(call.args)
        except ValueError as e:
            return f"没算：{e}（日期写 YYYY-MM-DD）"
    ctx.tool(Tool("date_calc", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="日期计算：相差几天 / 几个工作日、加减天数和月数、星期几"))
