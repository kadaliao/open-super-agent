"""字数统计：数一段文字有多少字（按 Word 的算法）、多少汉字和英文单词、含不含空格和标点的字符数、行数、段落数，估算阅读和朗读要多久，还能数某个词出现了几次。

大模型数字数很不准（它看到的是 token，不是字），差个几十上百字很常见。装上以后要数字数、看稿子念多久、某个词用了几次，Bot 就调这个工具，对话里和后台任务里都能用。

- “字数”按 Word / WPS 的算法：一个汉字、一个全角标点各算 1，一个英文单词、一串数字各算 1。
- 阅读时间按默读每分钟 400 个汉字、230 个英文单词，朗读每分钟 250 个汉字、150 个英文单词估算。
- 段落：有空行时按空行分，没有空行就一行算一段。数关键词默认不分大小写。
"""
import re
import unicodedata
from osa.registry import ROUTER, WORKER, Tool

META = {"title": "字数统计", "version": "1.0", "author": "open-super-agent"}
MAX_TEXT = 1_000_000
CJK = re.compile("[㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]")
WORD = re.compile(r"[A-Za-zÀ-ɏ]+(?:['’\-][A-Za-zÀ-ɏ]+)*")
NUMBER = re.compile(r"\d+(?:[.,:]\d+)*%?")
READ = (400, 230)    # 默读：汉字 / 分钟，英文单词 / 分钟
SPEAK = (250, 150)   # 朗读

SCHEMA = {"type": "function", "function": {
    "name": "text_count",
    "description": "字数统计，要数字数、字符数、单词数、段落数、估算阅读或朗读时间、数某个词出现几次时一定用它，"
                   "不要自己数（模型数不准）。把要统计的原文一字不差地放进 text，不要概括、改写或省略；keyword 是要数的词（可选）。",
    "parameters": {"type": "object", "properties": {
        "text": {"type": "string", "description": "要统计的原文，原样放进来"},
        "keyword": {"type": "string", "description": "要数出现几次的词，可选"},
        "case_sensitive": {"type": "boolean", "description": "数关键词时分不分大小写，默认不分"}},
        "required": ["text"]}}}


def _wide_punct(c: str) -> bool:
    """中文（全角）标点：CJK 符号区、全角区，还有中文里常用的 “”‘’…—·。"""
    if not unicodedata.category(c).startswith(("P", "S")):
        return False
    o = ord(c)
    return 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF or c in "“”‘’…—·"


def _minutes(cn: int, en: int, speed: tuple[int, int]) -> str:
    sec = round((cn / speed[0] + en / speed[1]) * 60)
    if sec < 60:
        return f"约 {max(sec, 1)} 秒" if cn or en else "0 秒"
    m, s = divmod(sec, 60)
    if m >= 60:
        return f"约 {m // 60} 小时 {m % 60} 分钟"
    return f"约 {m} 分钟" + (f" {s} 秒" if m < 10 and s else "")


def stats(text: str) -> dict:
    cjk = len(CJK.findall(text))
    words = len(WORD.findall(text))
    numbers = len(NUMBER.findall(text))
    punct = sum(1 for c in text if unicodedata.category(c).startswith("P"))
    wide = sum(1 for c in text if _wide_punct(c))
    lines = text.splitlines()
    nonblank = [ln for ln in lines if ln.strip()]
    if re.search(r"\n[ \t　]*\n", text.strip()):
        paragraphs = len([b for b in re.split(r"\n[ \t　]*\n", text.strip()) if b.strip()])
    else:
        paragraphs = len(nonblank)
    return {
        "chars": len(text), "no_space": len(re.sub(r"\s", "", text)),
        "no_space_punct": sum(1 for c in text if not c.isspace() and not unicodedata.category(c).startswith(("P", "S"))),
        "cjk": cjk, "words": words, "numbers": numbers, "punct": punct, "wide_punct": wide,
        "word_count": cjk + wide + words + numbers,
        "lines": len(lines), "nonblank": len(nonblank), "paragraphs": paragraphs,
        "bytes": len(text.encode("utf-8")),
    }


def keyword(text: str, kw: str, case_sensitive: bool = False) -> str:
    flags = 0 if case_sensitive else re.I
    hits = [m.start() for m in re.finditer(re.escape(kw), text, flags)]
    if not hits:
        return f"“{kw}”没有出现" + ("" if case_sensitive else "（不分大小写）") + "。"
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    lines = sorted({sum(1 for s in starts if s <= h) for h in hits})
    where = "、".join(map(str, lines[:20])) + ("……" if len(lines) > 20 else "")
    return (f"“{kw}”出现 {len(hits)} 次" + ("" if case_sensitive else "（不分大小写）")
            + f"，在第 {where} 行。")


def calc(args: dict) -> str:
    text = args.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("text 是空的：把要统计的原文放进来")
    if len(text) > MAX_TEXT:
        raise ValueError(f"文字太长（最多 {MAX_TEXT:,} 个字符），分几段统计")
    s = stats(text)
    out = [
        f"字数（按 Word 的算法）：{s['word_count']:,}",
        f"- 汉字 {s['cjk']:,} 个，英文单词 {s['words']:,} 个，数字 {s['numbers']:,} 串，标点 {s['punct']:,} 个（其中中文标点 {s['wide_punct']:,} 个）",
        f"- 字符数：含空格 {s['chars']:,}，不含空格 {s['no_space']:,}，不含空格和标点符号 {s['no_space_punct']:,}；UTF-8 是 {s['bytes']:,} 字节",
        f"- {s['lines']:,} 行（非空 {s['nonblank']:,} 行），{s['paragraphs']:,} 段",
        f"- 默读{_minutes(s['cjk'], s['words'], READ)}，朗读{_minutes(s['cjk'], s['words'], SPEAK)}",
    ]
    if kw := args.get("keyword"):
        out.append(keyword(text, str(kw), bool(args.get("case_sensitive"))))
    return "\n".join(out)


def apply(ctx):
    async def run(call):
        try:
            return calc(call.args)
        except (ValueError, TypeError) as e:
            return f"没统计：{e}"
    ctx.tool(Tool("text_count", SCHEMA, run, where=frozenset({ROUTER, WORKER}),
                  describe="字数统计：字数、汉字、英文单词、字符数、行数段落、阅读朗读时间、关键词出现次数",
                  progress=lambda a: f"统计字数（{len(str(a.get('text') or '')):,} 个字符）"))
