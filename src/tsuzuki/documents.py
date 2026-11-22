"""資料の判定・種類・走査・本文の取り出し。"""
import os
import re
import sqlite3
from html.parser import HTMLParser
from typing import List, Optional, Sequence, Tuple

from tsuzuki.tasks import task_doc_kind

KINDS = ("memory", "handoff", "cover", "research", "design", "plan", "response", "notes", "progress", "review",
         "readme", "other")
KIND_LABELS = {"memory": "メモリ", "handoff": "引き継ぎ", "cover": "表紙", "research": "調査", "design": "設計書",
               "plan": "計画", "response": "レビュー対応", "notes": "メモ", "progress": "台帳・進捗",
               "review": "レビュー・知見", "readme": "説明・規約", "other": "その他"}
TEXT_EXTS = (".md", ".markdown", ".mdx", ".txt", ".csv", ".html", ".htm")
MAX_BODY_BYTES = 512 * 1024
TMP_PREFIXES = ("/private/tmp/", "/tmp/", "/private/var/folders/", "/var/folders/")

# (種類, ディレクトリ名, 英字の語, 日本語の語)
_DIR_RULES = (
    ("handoff", ("handoff",)),
    ("plan", ("plans",)),
    ("design", ("specs", "adr")),
)
_NAME_RULES = (
    ("handoff", ("handoff",), ("引き継ぎ", "再開メモ")),
    ("plan", ("plan",), ("計画",)),
    ("design", ("design",), ("設計",)),
    ("progress", ("progress", "ledger"), ("台帳", "進捗")),
    ("review", ("review", "learnings", "learning", "retrospective"), ("ふりかえり", "知見")),
)


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def is_document(path: str, exts: Sequence[str]) -> bool:
    return _ext(path) in [e.lower() for e in exts]


def allowed_doc_path(path: str, home: str) -> bool:
    if path.startswith(TMP_PREFIXES):
        return False
    claude = os.path.join(home, ".claude") + "/"
    if path.startswith(claude):
        parts = path[len(claude):].split("/")
        if len(parts) == 2 and parts[0] == "plans":
            return parts[1].endswith(".md")
        if len(parts) == 4 and parts[0] == "projects" and parts[2] == "memory":
            return parts[3].endswith(".md")
        return False
    return True


def classify_kind(path: str, home: str, tasks_root: Optional[str] = None) -> str:
    if tasks_root:
        kind = task_doc_kind(path, tasks_root)
        if kind:
            return kind
    claude_projects = os.path.join(home, ".claude", "projects") + "/"
    dirs = path.split("/")[:-1]
    name = os.path.basename(path).lower()
    if path.startswith(claude_projects) and "memory" in path[len(claude_projects):].split("/")[1:-1]:
        return "memory"
    for kind, names in _DIR_RULES:
        if any(d in names for d in dirs):
            return kind
    tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]
    for kind, words, ja in _NAME_RULES:
        for w in words:
            if w in tokens or (w + "s") in tokens:
                return kind
        if any(j in name for j in ja):
            return kind
    if name.startswith("readme") or name in ("claude.md", "agents.md") or name.startswith("glossary"):
        return "readme"
    return "other"


def scan_documents(root: str, *, exts: Sequence[str], exclude_dirs: Sequence[str],
                   max_depth: int, max_files: int) -> List[str]:
    root = os.path.abspath(root)
    out = []  # type: List[str]
    excluded = set(exclude_dirs)

    def walk(d: str, depth: int) -> None:
        if len(out) >= max_files:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if len(out) >= max_files:
                return
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in excluded and depth < max_depth:
                        walk(e.path, depth + 1)
                elif e.is_file() and is_document(e.name, exts):
                    out.append(e.path)
            except OSError:
                continue

    walk(root, 0)
    return sorted(out)


def should_scan_place(root: str, home: str) -> bool:
    r = os.path.normpath(root)
    if r == os.path.normpath(home) or r in ("/", "/Volumes"):
        return False
    parts = r.split("/")
    if len(parts) == 3 and parts[1] == "Volumes":
        return False
    return True


_BLOCK_TAGS = frozenset((
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "table",
    "section", "article", "header", "footer", "nav", "main", "aside", "pre", "blockquote", "hr", "dl",
    "dt", "dd", "figure", "figcaption"))


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.svg = 0
        self.in_title = False
        self.in_h1 = False
        self.title = None  # type: Optional[str]
        self.title_buf = ""
        self.h1 = None  # type: Optional[str]
        self.h1_buf = ""
        self.chunks = []  # type: List[str]

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "svg":
            self.svg += 1
        elif tag == "title" and not self.svg and self.title is None:
            self.in_title = True
        elif tag == "h1" and self.h1 is None:
            self.in_h1 = True
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")
            if self.in_h1:
                self.h1_buf += "\n"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag == "svg" and self.svg:
            self.svg -= 1
        elif tag == "title" and self.in_title:
            self.in_title = False
            self.title = self.title_buf
        elif tag == "h1" and self.in_h1:
            self.in_h1 = False
            self.h1 = self.h1_buf
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title_buf += data
            return
        if self.in_h1:
            self.h1_buf += data
        self.chunks.append(data)


def _cap(text: str) -> str:
    b = text.encode("utf-8")
    if len(b) <= MAX_BODY_BYTES:
        return text
    return b[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")


def extract_text(path: str) -> Tuple[Optional[str], Optional[str]]:
    ext = _ext(path)
    if ext not in TEXT_EXTS:
        return None, None
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BODY_BYTES)
    except OSError:
        return None, None
    text = raw.decode("utf-8", errors="replace")
    title = None  # type: Optional[str]
    if ext in (".md", ".markdown", ".mdx"):
        m = re.search(r"^# (.*)$", text, re.MULTILINE)
        if m:
            title = m.group(1).strip() or None
    elif ext in (".html", ".htm"):
        p = _HtmlText()
        try:
            p.feed(text)
            p.close()
        except Exception:
            pass
        title = " ".join((p.title or "").split()) or " ".join((p.h1 or "").split()) or None
        text = " ".join("".join(p.chunks).split())
    return title, _cap(text)


def upsert_document(conn: sqlite3.Connection, path: str, *, place_id: Optional[int], home: str,
                    exts: Sequence[str], tasks_root: Optional[str] = None) -> Optional[int]:
    st = None
    if is_document(path, exts) and allowed_doc_path(path, home):
        try:
            st = os.stat(path)
        except OSError:
            st = None
    if st is None or not os.path.isfile(path):
        with conn:
            conn.execute("DELETE FROM documents WHERE path = ?", (path,))
        return None
    row = conn.execute("SELECT id, size, mtime, place_id FROM documents WHERE path = ?", (path,)).fetchone()
    if row is not None and row[1] == st.st_size and row[2] == st.st_mtime:
        if row[3] != place_id:
            with conn:
                conn.execute("UPDATE documents SET place_id = ? WHERE id = ?", (place_id, row[0]))
        return row[0]
    title, body = extract_text(path)
    kind = classify_kind(path, home, tasks_root)
    ext = _ext(path)
    with conn:
        if row is None:
            cur = conn.execute(
                "INSERT INTO documents(path, place_id, kind, ext, size, mtime, title, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (path, place_id, kind, ext, st.st_size, st.st_mtime, title, body))
            return cur.lastrowid
        conn.execute(
            "UPDATE documents SET place_id=?, kind=?, ext=?, size=?, mtime=?, title=?, body=? WHERE id=?",
            (place_id, kind, ext, st.st_size, st.st_mtime, title, body, row[0]))
        return row[0]
