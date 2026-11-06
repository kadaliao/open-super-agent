"""把 Bot 的 Markdown 回复改成聊天软件能显示的样子。

- split：按长度切成几条消息，尽量在空行、换行处切；切点落在代码块里时，前一条补上结尾的 ```，后一条重新打开。
- telegram_html：Telegram 只认一小部分 HTML（b、i、s、code、pre、a、blockquote），其余都要转义。
  解析失败时由调用方退回纯文本，所以这里只求常见写法显示正常。
- feishu_md：飞书富文本的 md 标签不支持标题，标题改成加粗。
- dingtalk_md：钉钉的 markdown 消息里单个换行不换行，行尾补两个空格（标准的硬换行）；代码块原样保留。
- discord_md：Discord 只认一到三级标题，更小的改成加粗；表格不支持，放进代码块里等宽显示。
"""
import html
import re
from .. import i18n

FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_BLOCK = re.compile(r"^([ \t]*)(```|~~~)([^\n]*)\n(.*?)(?:\n[ \t]*\2[ \t]*$|\Z)", re.S | re.M)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_STRIKE = re.compile(r"~~(.+?)~~")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)


def split(text: str, limit: int) -> list[str]:
    """切成每段不超过 limit 个字符。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    room = limit - 8              # 给补上的 ``` 留位置
    out: list[str] = []
    cur: list[str] = []
    fence = False                 # cur 的末尾是不是在代码块里

    def size(lines):
        return sum(len(x) + 1 for x in lines)

    def flush():
        nonlocal cur
        # 不在代码块里时，尽量在后半段的空行处切，剩下的留给下一条
        carry: list[str] = []
        if not fence:
            blanks = [i for i, x in enumerate(cur) if not x.strip() and i >= len(cur) // 2]
            if blanks and not any(FENCE.match(x) for x in cur[blanks[-1]:]):
                carry, cur = cur[blanks[-1] + 1:], cur[:blanks[-1]]
        body = "\n".join(cur).strip("\n")
        if body.strip():
            out.append(body + ("\n```" if fence else ""))
        cur = (["```"] if fence else []) + carry

    for line in text.split("\n"):
        for i in range(0, max(len(line), 1), room):
            piece = line[i:i + room]
            if cur and size(cur) + len(piece) + 1 > room:
                flush()
            cur.append(piece)
        if FENCE.match(line):
            fence = not fence
    body = "\n".join(cur).strip("\n")
    if body.strip():
        out.append(body)
    return out


def _inline(text: str) -> str:
    """一段不含代码块的 Markdown → Telegram HTML。"""
    keep: list[str] = []

    def hold(s: str) -> str:
        keep.append(s)
        return f"\x00{len(keep) - 1}\x00"

    text = _INLINE_CODE.sub(lambda m: hold(f"<code>{html.escape(m[1])}</code>"), text)
    text = _LINK.sub(lambda m: hold(f'<a href="{html.escape(m[2], quote=True)}">{html.escape(m[1])}</a>'), text)
    text = html.escape(text, quote=False)
    text = _HEADING.sub(lambda m: f"<b>{m[1]}</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m[1] or m[2]}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m[1]}</i>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m[1]}</s>", text)
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)
    text = re.sub(r"^\s*([-*_])(\s*\1){2,}\s*$", "──────", text, flags=re.M)
    # 引用：连续的 > 行合成一个 blockquote
    text = re.sub(r"(?:^&gt; ?.*(?:\n|$))+",
                  lambda m: "<blockquote>" + re.sub(r"^&gt; ?", "", m[0].rstrip("\n"), flags=re.M) + "</blockquote>\n",
                  text, flags=re.M)
    # 表格：等宽显示
    text = re.sub(r"(?:^\|.*\|[ \t]*(?:\n|$))+", lambda m: f"<pre>{m[0].rstrip()}</pre>\n", text, flags=re.M)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m[1])], text)


def telegram_html(md: str) -> str:
    out, pos = [], 0
    for m in _CODE_BLOCK.finditer(md):
        out.append(_inline(md[pos:m.start()]))
        lang = m[3].strip().split(" ")[0]
        attr = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
        code = re.sub(f"^{m[1]}", "", m[4], flags=re.M) if m[1] else m[4]   # 列表里缩进的代码块
        out.append(f"<pre><code{attr}>{html.escape(code)}</code></pre>")
        pos = m.end()
    out.append(_inline(md[pos:]))
    return "".join(out).strip()


def plain(md: str) -> str:
    """退回纯文本时用：去掉最显眼的 Markdown 记号。"""
    text = _LINK.sub(lambda m: i18n.t("{text}（{url}）", text=m[1], url=m[2]), md)
    text = _BOLD.sub(lambda m: m[1] or m[2], text)
    return _HEADING.sub(lambda m: m[1], text)


def feishu_md(md: str) -> str:
    out, pos = [], 0
    for m in _CODE_BLOCK.finditer(md):
        out.append(_HEADING.sub(lambda h: f"**{h[1]}**", md[pos:m.start()]))
        out.append(m[0])
        pos = m.end()
    out.append(_HEADING.sub(lambda h: f"**{h[1]}**", md[pos:]))
    return "".join(out)


def fence_segments(md: str) -> list[str]:
    """按代码块切开：每个代码块单独一段。飞书的 md 标签里，紧跟在代码块后面的文字有时会丢。"""
    out, buf, inside = [], [], False
    for line in md.split("\n"):
        is_fence = bool(FENCE.match(line))
        if is_fence and not inside:
            if buf:
                out.append("\n".join(buf))
            buf, inside = [line], True
        elif is_fence and inside:
            buf.append(line)
            out.append("\n".join(buf))
            buf, inside = [], False
        else:
            buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return [s for s in out if s.strip()]


def _outside_code(md: str, fn) -> str:
    """只改代码块以外的部分。"""
    out, pos = [], 0
    for m in _CODE_BLOCK.finditer(md):
        out.append(fn(md[pos:m.start()]))
        out.append(m[0])
        pos = m.end()
    out.append(fn(md[pos:]))
    return "".join(out)


def dingtalk_md(md: str) -> str:
    return _outside_code(md, lambda t: re.sub(r"(?<=\S)[ \t]*\n(?=[ \t]*\S)", "  \n", t))


def discord_md(md: str) -> str:
    def fix(t: str) -> str:
        t = re.sub(r"^#{4,6}\s+(.+?)\s*#*\s*$", r"**\1**", t, flags=re.M)
        return re.sub(r"(?:^\|.*\|[ \t]*(?:\n|$))+", lambda m: f"```\n{m[0].rstrip()}\n```\n", t, flags=re.M)
    return _outside_code(md, fix)


def title_line(md: str, limit: int = 30) -> str:
    """一段 Markdown 的第一行纯文字，当通知标题用。"""
    for line in plain(md).splitlines():
        line = re.sub(r"^[#>\-*+\s]+|[`*_~]", "", line).strip()
        if line:
            return line[:limit]
    return i18n.t("消息")
