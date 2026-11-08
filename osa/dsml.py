"""DeepSeek 偶尔不走正常的 tool_calls，把工具调用写进正文：

    <｜｜DSML｜｜ invoke name="message_bot">
    <｜｜DSML｜｜ parameter name="to" string="true">Quark</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>

竖线的个数、空格、外面那层 calls 都不固定。认出来就当成真的工具调用去做，标记本身不给用户看。
"""
import json
import re

_TAG = r"<\s*{close}[｜|]+\s*DSML\s*[｜|]+\s*{name}"
START = re.compile(_TAG.format(close=r"/?\s*", name=""))
_INVOKE = re.compile(_TAG.format(close="", name=r'invoke\s+name\s*=\s*"([^"]+)"\s*>')
                     + r"(.*?)(?=" + _TAG.format(close=r"/\s*", name=r"invoke\s*>") + "|" + _TAG.format(close="", name="invoke")
                     + r"|\Z)", re.S)
_PARAM = re.compile(_TAG.format(close="", name=r'parameter\s+name\s*=\s*"([^"]+)"(?:\s+string\s*=\s*"(true|false)")?\s*>')
                    + r"(.*?)" + _TAG.format(close=r"/\s*", name=r"parameter\s*>"), re.S)
_LAST = re.compile(_TAG.format(close=r"/?\s*", name=r"[^>]*>"))
_PARTIAL = re.compile(r"<\s*/?\s*[｜|]*\s*(?:D(?:S(?:M(?:L)?)?)?)?\s*$")


def visible(text: str) -> int:
    """边收边显示时，text 里前多少个字可以先给用户看：标记开始的地方之后都不显示，
    结尾一段可能是标记开头的（比如只收到了“<｜”）也先压着。"""
    if m := START.search(text):
        return m.start()
    i = text.rfind("<")
    return i if i != -1 and _PARTIAL.fullmatch(text, i) else len(text)


def _value(raw: str, is_string: str | None):
    if is_string == "false":
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw


def split(text: str) -> tuple[str, list[dict]]:
    """返回 (去掉标记后的正文, 工具调用)。工具调用和流式收到的一样：{"id", "name", "args"}，args 是 JSON 字符串。"""
    m = START.search(text or "")
    if not m:
        return text, []
    tags = list(_LAST.finditer(text, m.start()))
    # 标记后面还有话的话留着；没写完就断了（最后一个是开始标记）的，后面是参数值，不要
    rest = text[tags[-1].end():] if tags and re.match(r"<\s*/", tags[-1].group(0)) else ""
    calls = []
    for k, (name, body) in enumerate(_INVOKE.findall(text, m.start())):
        args = {p: _value(v, s) for p, s, v in _PARAM.findall(body)}
        calls.append({"id": f"call_dsml_{k}", "name": name.strip(), "args": json.dumps(args, ensure_ascii=False)})
    clean = (text[:m.start()].rstrip() + ("\n\n" + rest.strip() if rest.strip() else "")).strip()
    return clean, calls
