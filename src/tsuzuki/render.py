"""画面に出す文字の変換。

Markdown は、入力をすべて HTML エスケープしてから、決まった書式だけをタグに変える。
入力に書かれた HTML がそのまま出ることはない。リンクは http・https のものだけにする。
"""
import html
import re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_QUOTE_DEPTH = 8  # 引用の入れ子をここまで変換する（それより深い印は文字のまま）

_FENCE = re.compile(r"^([ \t]*)(`{3,}(?=[^`]*$)|~{3,})")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?=[ \t]|$)")
_QUOTE = re.compile(r"^ {0,3}&gt; ?(.*)$")
_ITEM = re.compile(r"^([ \t]*)(?:([-*+])|(\d{1,9})[.)])(?:[ \t]+(.*))?$")
_DELIMITER_CELL = re.compile(r"^:?-+:?$")
_MERMAID_BLOCK = '<div class="mermaid-block"><pre class="mermaid-src"><code>%s</code></pre></div>'
_CELL_BORDER = re.compile(r"(?<!\\)\|")

_BACKTICKS = re.compile(r"`+")
_URL = r"[ \t]*((?:[^()\s\x00]|\([^()\s\x00]*\))*)[ \t]*"
_IMAGE = re.compile(r"!\[([^\[\]\n]*)\]\(" + _URL + r"\)")
_LINK = re.compile(r"\[([^\[\]\n]*)\]\(" + _URL + r"\)")
# 強調の中身は印の字を含まない（どの印も次の同じ印までしか見ないので、行の長さに比例する時間で済む。
# 変換した範囲どうしが交差しないので、タグの入れ子も崩れない）。_ は語の途中では強調にせず、
# * で作ったタグをまたがない
_STRONG_EM = re.compile(r"(?<!\*)\*\*\*(?![\s*])([^*]+?)(?<![\s*])\*\*\*(?!\*)")
_STRONG = re.compile(r"(?<!\*)\*\*(?![\s*])([^*]+?)(?<![\s*])\*\*(?!\*)")
_EM = re.compile(r"(?<!\*)\*(?![\s*])([^*]+?)(?<![\s*])\*(?!\*)")
_EM_UNDERSCORE = re.compile(r"(?<!\w)_(?![\s_])([^_<]+?)(?<![\s_])_(?!\w)")
_PLACEHOLDER = re.compile("\x00([0-9]+)\x00")

_SECONDS_FRACTION = re.compile(r"(:\d{2})\.\d+")


def render_markdown(text: str, *, breaks: bool = False) -> str:
    """Markdown の一部（見出し・段落・箇条書き・番号付きの箇条書き・囲みのコード・引用・横線・表と、
    インラインのコード・太字・斜体・リンク）を HTML にする。言語が mermaid の囲みは mermaid-block の div にする。
    breaks なら、段落・項目の中の1つの改行を <br> にする（囲みのコードと表は変えない）。"""
    # NUL は _inline の印に使うので、入力の NUL は置換文字にしておく
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\N{REPLACEMENT CHARACTER}")
    return "\n".join(_blocks(html.escape(text).split("\n"), 0, breaks))


def render_plain(text: str) -> str:
    """エスケープし、改行を <br> にする。"""
    return re.sub(r"\r\n?|\n", "<br>", html.escape(text or ""))


def render_pre(text: str) -> str:
    """エスケープして <pre> で囲む（テキストと CSV の資料）。"""
    return "<pre>%s</pre>" % html.escape(text or "")


def highlight(text: str, marks: Sequence[Tuple[int, int]]) -> str:
    """エスケープし、marks の範囲（文字の位置）を <mark> で囲む。重なる範囲はまとめる。"""
    n = len(text)
    spans = []  # type: List[List[int]]
    for start, end in sorted((min(max(a, 0), n), min(max(b, 0), n)) for a, b in marks):
        if start >= end:
            continue
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    out, pos = [], 0
    for start, end in spans:
        out.append(html.escape(text[pos:start]))
        out.append("<mark>%s</mark>" % html.escape(text[start:end]))
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out)


def fmt_local(iso: Optional[str], *, now: Optional[datetime] = None) -> str:
    """台帳の時刻（UTC の ISO）を地方時で出す。now と同じ年なら「11/21 17:18」、違えば「2026/07/13 20:04」。
    読めなければ空文字。"""
    if not isinstance(iso, str) or not iso.strip():
        return ""
    s = iso.strip()
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    # 3.9 の fromisoformat は秒の端数が3桁か6桁のときだけ読める。分までしか出さないので捨てる
    s = _SECONDS_FRACTION.sub(r"\1", s, count=1)
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        local = d.astimezone()
        ref = (now if now is not None else datetime.now()).astimezone()
    except (ValueError, OverflowError, OSError):
        return ""
    return local.strftime("%m/%d %H:%M" if local.year == ref.year else "%Y/%m/%d %H:%M")


# ブロック（行ごとに見る。lines はエスケープ済み）

def _blocks(lines: List[str], depth: int, breaks: bool = False) -> List[str]:
    out = []  # type: List[str]
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _FENCE.match(line):
            block, i = _fenced(lines, i)
            out.append(block)
            continue
        heading = _heading(line)
        if heading is not None:
            level, content = heading
            out.append("<h%d>%s</h%d>" % (level, _inline(content), level))
            i += 1
            continue
        if _is_hr(line):
            out.append("<hr>")
            i += 1
            continue
        if depth < _QUOTE_DEPTH and _QUOTE.match(line):
            inner = []
            while i < n:
                m = _QUOTE.match(lines[i])
                if m is None:
                    break
                inner.append(m.group(1))
                i += 1
            out.append("<blockquote>\n%s\n</blockquote>" % "\n".join(_blocks(inner, depth + 1, breaks)))
            continue
        aligns = _table_aligns(lines, i)
        if aligns is not None:
            block, i = _table(lines, i, aligns, depth)
            out.append(block)
            continue
        if _ITEM.match(line):
            block, i = _list(lines, i, depth, breaks)
            out.append(block)
            continue
        para = [line.strip()]
        i += 1
        while (i < n and lines[i].strip() and not _opens_block(lines[i], depth, interrupting=True)
               and _table_aligns(lines, i) is None):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % _lines(_inline("\n".join(para)), breaks))
    return out


def _lines(inline: str, breaks: bool) -> str:
    """変換した行の中の改行（行の継ぎ目だけ。コードの中の改行は空白にしてある）を、breaks なら <br> 付きにする。"""
    return inline.replace("\n", "<br>\n") if breaks else inline


def _indent(line: str) -> int:
    """行頭の空白の幅（タブは4字）。"""
    return len(line[:len(line) - len(line.lstrip(" \t"))].expandtabs(4))


def _heading(line: str) -> Optional[Tuple[int, str]]:
    m = _HEADING.match(line)
    if m is None:
        return None
    content = line[m.end():].strip(" \t")
    # 閉じの # の並び（前に空白があるもの）は外す。「C#」の # は残す
    bare = content.rstrip("#")
    if bare != content and (not bare or bare[-1] in " \t"):
        content = bare.rstrip(" \t")
    return len(m.group(1)), content


def _is_hr(line: str) -> bool:
    if _indent(line) > 3:
        return False
    s = line.replace(" ", "").replace("\t", "")
    return len(s) >= 3 and s[0] in "-*_" and s == s[0] * len(s)


def _opens_block(line: str, depth: int, *, interrupting: bool) -> bool:
    """この行が囲みのコード・見出し・横線・引用・箇条書きを始めるか。interrupting は段落の途中のとき
    （空の項目と、1 以外から始まる番号付きの項目は段落を切らない）。"""
    if _FENCE.match(line) or _heading(line) is not None or _is_hr(line):
        return True
    if depth < _QUOTE_DEPTH and _QUOTE.match(line):
        return True
    m = _ITEM.match(line)
    if m is None:
        return False
    return not interrupting or (bool(m.group(4)) and (m.group(2) is not None or m.group(3) == "1"))


def _fenced(lines: List[str], i: int) -> Tuple[str, int]:
    """囲みのコード。閉じの印がなければ最後まで。中身は字下げを開きの印の分だけ外し、そのまま出す。
    言語（情報文字列の最初の語）が mermaid なら、画面が図を描けるよう決まった div で囲む。スクリプトが動かなくても、コードとして読める。"""
    m = _FENCE.match(lines[i])
    indent, marker = _indent(m.group(1)), m.group(2)
    info = lines[i][m.end():].split()
    body = []
    i += 1
    while i < len(lines):
        s = lines[i].strip()
        if len(s) >= len(marker) and s == marker[0] * len(s):
            i += 1
            break
        body.append(_dedent(lines[i], indent))
        i += 1
    if info and info[0].lower() == "mermaid":
        return _MERMAID_BLOCK % "\n".join(body), i
    return "<pre><code>%s</code></pre>" % "\n".join(body), i


def _dedent(line: str, n: int) -> str:
    k = 0
    while k < n and k < len(line) and line[k] == " ":
        k += 1
    return line[k:]


def _cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in _CELL_BORDER.split(s)]


def _table_aligns(lines: List[str], i: int) -> Optional[List[Optional[str]]]:
    """i 行目が表の見出しの行で、次が区切りの行なら、列ごとの寄せ（left・right・center・None）を返す。"""
    if i + 1 >= len(lines) or "|" not in lines[i] or "|" not in lines[i + 1]:
        return None
    head, delimiter = _cells(lines[i]), _cells(lines[i + 1])
    if len(head) != len(delimiter) or not all(_DELIMITER_CELL.match(c) for c in delimiter):
        return None
    aligns = []  # type: List[Optional[str]]
    for c in delimiter:
        left, right = c.startswith(":"), c.endswith(":")
        aligns.append("center" if left and right else "left" if left else "right" if right else None)
    return aligns


def _table(lines: List[str], i: int, aligns: List[Optional[str]], depth: int) -> Tuple[str, int]:
    def row(cells: List[str], tag: str) -> str:
        cells = (cells + [""] * len(aligns))[:len(aligns)]
        return "<tr>%s</tr>" % "".join(
            "<%s%s>%s</%s>" % (tag, ' align="%s"' % a if a else "", _inline(c), tag) for c, a in zip(cells, aligns))

    out = ["<table><thead>", row(_cells(lines[i]), "th"), "</thead>"]
    i += 2
    body = []
    while (i < len(lines) and lines[i].strip() and "|" in lines[i]
           and not _opens_block(lines[i], depth, interrupting=False)):
        body.append(row(_cells(lines[i]), "td"))
        i += 1
    if body:
        out += ["<tbody>"] + body + ["</tbody>"]
    out.append("</table>")
    return "".join(out), i


class _Item:
    """箇条書きの1項目。parts は ("text", 行の並び) と ("code", [HTML]) の並び。
    項目の中に空行があれば、文の部分をそれぞれ段落にする。"""

    def __init__(self, level: int, ordered: bool, start: int, text: str):
        self.level, self.ordered, self.start = level, ordered, start
        self.parts = [("text", [text])]  # type: List[Tuple[str, List[str]]]
        self.loose = False

    def add_text(self, text: str, new_paragraph: bool) -> None:
        self.loose = self.loose or new_paragraph
        kind, lines = self.parts[-1]
        if new_paragraph or kind != "text":
            self.parts.append(("text", [text]))
        else:
            lines.append(text)

    def add_code(self, block: str) -> None:
        self.parts.append(("code", [block]))

    def html(self, breaks: bool = False) -> str:
        out = []
        for kind, lines in self.parts:
            if kind == "code":
                out.append(lines[0])
            else:
                body = _lines(_inline("\n".join(lines)), breaks)
                out.append("<p>%s</p>" % body if self.loose else body)
        return "".join(out)


def _list(lines: List[str], i: int, depth: int, breaks: bool = False) -> Tuple[str, int]:
    """箇条書き。最初の項目より2字以上深い項目を1段下の入れ子にする（2段まで）。"""
    base = _indent(_ITEM.match(lines[i]).group(1))
    items = []  # type: List[_Item]
    blank = False
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            blank = True
            i += 1
            continue
        indent = _indent(line)
        m = _ITEM.match(line)
        if m is not None and not _is_hr(line):
            level = 0 if indent < base + 2 else 1
            items.append(_Item(level, m.group(3) is not None, int(m.group(3) or 1), (m.group(4) or "").strip()))
            blank = False
            i += 1
            continue
        nested = indent >= base + 2
        if not nested and (blank or _opens_block(line, depth, interrupting=False)):
            break
        if nested and _FENCE.match(line):
            block, i = _fenced(lines, i)
            items[-1].add_code(block)
        else:
            items[-1].add_text(line.strip(), new_paragraph=blank)
            i += 1
        blank = False
    return _render_list(items, breaks), i


def _open_list(item: _Item) -> str:
    if not item.ordered:
        return "<ul>"
    return '<ol start="%d">' % item.start if item.start != 1 else "<ol>"


def _render_list(items: List[_Item], breaks: bool = False) -> str:
    out = []  # type: List[str]
    top = sub = None  # type: Optional[str]
    item_open = False
    for item in items:
        tag = "ol" if item.ordered else "ul"
        if item.level == 0:
            if sub:
                out.append("</%s>" % sub)
                sub = None
            if item_open:
                out.append("</li>")
            if top != tag:
                if top:
                    out.append("</%s>" % top)
                out.append(_open_list(item))
                top = tag
            out.append("<li>" + item.html(breaks))
            item_open = True
        else:
            if sub != tag:
                if sub:
                    out.append("</%s>" % sub)
                out.append(_open_list(item))
                sub = tag
            out.append("<li>%s</li>" % item.html(breaks))
    if sub:
        out.append("</%s>" % sub)
    if item_open:
        out.append("</li>")
    out.append("</%s>" % top)
    return "".join(out)


# 行の中（s はエスケープ済み）

def _inline(s: str) -> str:
    """インラインのコード・画像・リンク・強調を変換する。コードとリンクはいったん印に置き換え、
    強調の変換がコードの中身やリンクの URL に及ばないようにする。"""
    pieces = []  # type: List[str]

    def keep(fragment: str) -> str:
        pieces.append(fragment)
        return "\x00%d\x00" % (len(pieces) - 1)

    s = _code_spans(s, keep)
    s = _IMAGE.sub(lambda m: m.group(1), s)  # 画像は読み込まず、代替テキストだけ
    s = _LINK.sub(lambda m: _link(m.group(1), m.group(2), keep), s)
    s = _emphasis(s)
    for _ in range(2):  # リンクの文字の中にコードがありうる
        s = _PLACEHOLDER.sub(lambda m: pieces[int(m.group(1))], s)
    return s


def _code_spans(s: str, keep: Callable[[str], str]) -> str:
    """` の並びで始まり、同じ長さの次の並びで終わる部分をコードにする。"""
    runs = [(m.start(), m.end()) for m in _BACKTICKS.finditer(s)]
    if len(runs) < 2:
        return s
    following = [None] * len(runs)  # type: List[Optional[int]]
    last = {}  # type: Dict[int, int]
    for k in range(len(runs) - 1, -1, -1):
        size = runs[k][1] - runs[k][0]
        following[k] = last.get(size)
        last[size] = k
    out, pos, k = [], 0, 0
    while k < len(runs):
        close = following[k]
        if close is None:
            k += 1
            continue
        start, body_start = runs[k]
        body_end, end = runs[close]
        body = s[body_start:body_end].replace("\n", " ")
        if len(body) >= 2 and body[0] == " " and body[-1] == " " and body.strip(" "):
            body = body[1:-1]
        out.append(s[pos:start])
        out.append(keep("<code>%s</code>" % body))
        pos = end
        k = close + 1
    out.append(s[pos:])
    return "".join(out)


def _link(text: str, url: str, keep: Callable[[str], str]) -> str:
    if url.lower().startswith(("http://", "https://")):
        return keep('<a href="%s" rel="noopener noreferrer" target="_blank">%s</a>' % (url, _emphasis(text)))
    return text


def _emphasis(s: str) -> str:
    s = _STRONG_EM.sub(r"<strong><em>\1</em></strong>", s)
    s = _EM.sub(r"<em>\1</em>", s)
    s = _STRONG.sub(r"<strong>\1</strong>", s)
    s = _EM.sub(r"<em>\1</em>", s)  # 太字を含む斜体（*a **b** c*）
    return _EM_UNDERSCORE.sub(r"<em>\1</em>", s)
