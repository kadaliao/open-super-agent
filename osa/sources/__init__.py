"""内置的事件来源（registry.Source）。用户在 Bot 的“事件”里选来源、填参数，事件来了 Bot 在后台处理一次（见 events.py）。

每个模块把自己的来源放进模块级的 SOURCES 列表，register() 按 ORDER 的顺序（没写进去的按文件名排在后面）全部登记。
写一个新来源：
- 文字（title、description、字段的 label / help / placeholder、场景）写中文并用 N_() 包起来，英文写进 osa/locales/en.json；
  场景里的 prompt 是给 Bot 的要求，也要翻（英文界面装的是英文的）。
- 联网用 _util.get()：只抓公网地址（和 fetch_url 一样挡内网）、限大小、自己跟跳转；测试里换 _util.TRANSPORT。
- poll 的 check 第一次（state 是 None）通常只记下现状；state 要能存成 JSON、别太大（几十 KB 以内）。
- 事件的 text 是给 Bot 看的，写清楚发生了什么、有链接给链接；太长的截断。
"""
import importlib
import pkgutil

ORDER = ["webhook", "feeds", "web", "github_repo", "mail", "calendar", "weather", "earthquake", "epic_free", "market",
         "files", "ip_change", "system"]


def modules() -> list[str]:
    names = [m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")]
    return [n for n in ORDER if n in names] + sorted(n for n in names if n not in ORDER)


def register(ctx):
    for name in modules():
        mod = importlib.import_module(f"{__name__}.{name}")
        for src in getattr(mod, "SOURCES", []):
            ctx.source(src)
