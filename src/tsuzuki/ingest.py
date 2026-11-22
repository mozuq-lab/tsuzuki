"""1つの記録（transcript）を台帳に取り込む。前回読み終えた位置から続きを読む。"""
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from tsuzuki.documents import allowed_doc_path, is_document
from tsuzuki.sources.transcript import (
    KNOWN_TYPES, AssistantText, ContinuedIn, DerivedFrom, ForkedFrom, LocalCommandOutput, Meta, PrLink, RawLine,
    Recap, Relocated, Title, ToolResult, ToolUse, UserPrompt, extract_events, iter_lines, record_type, tool_summary,
)
from tsuzuki.store import get_meta, set_meta
from tsuzuki.tasks import within

UUID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])")

PROMPT_CHARS = 2000
REPLY_CHARS = 300
TITLE_CHARS = 50
PENDING_LIMIT = 20  # 応答を待つスラッシュコマンドとして覚えておく数

_BRANCH_TITLE = re.compile(r"\s*\(Branch(?: \d+)?\)$")
_PATH_SEPARATORS = re.compile(r"""[\s、。，,「」『』（）()<>"'`]+""")
# @"<パス>"：空白を含むパスを Claude Code に渡す形。引用符の中を1つの候補にする
_QUOTED_AT = re.compile(r'@"([^"\n]+)"')
# 拡張子のあとに来たら、まだ ASCII のファイル名が続いているとみなす文字
_NAME_CONTINUES = "A-Za-z0-9_./-"
_DOC_RELS = {"Write": "created", "Edit": "edited", "MultiEdit": "edited", "NotebookEdit": "edited",
             "Read": "read"}
_AGENT_TOOLS = ("Agent", "Task")
_TITLE_COLUMNS = {"custom": "custom_title", "agent": "agent_name", "ai": "ai_title"}
# 頭から読み直すときに消す、セッションごとの表
_SESSION_TABLES = ("messages", "digests", "doc_links", "mentions", "session_prs", "derived_from")


@dataclass
class IngestContext:
    home: str
    exts: Tuple[str, ...]
    tasks_root: Optional[str] = None


@dataclass
class IngestResult:
    session_id: str
    new_records: int
    reset: bool
    kind: str
    touched_docs: Set[str]
    unknown_records: int


@dataclass
class _SessionRow:
    """sessions の列のうち、記録を読んで決めるもの。"""
    kind: str = "empty"
    entrypoint: Optional[str] = None
    version: Optional[str] = None
    cwd: Optional[str] = None
    relocated_cwd: Optional[str] = None
    branch: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    custom_title: Optional[str] = None
    agent_name: Optional[str] = None
    ai_title: Optional[str] = None
    first_prompt: Optional[str] = None
    last_prompt: Optional[str] = None
    n_prompts: int = 0
    n_agents: int = 0
    recap: Optional[str] = None
    recap_at: Optional[str] = None
    last_reply: Optional[str] = None
    forked_from: Optional[str] = None
    continued_in: Optional[str] = None
    turn: int = 0
    next_seq: int = 0
    n_records: int = 0
    n_recognized: int = 0
    # 応答を待っているスラッシュコマンド（JSON の [[本文, 時刻, 本人か], …]）。
    # 読み終えた位置をまたいで応答が来ることがあるので台帳に残す
    pending_commands: Optional[str] = None


_ROW_COLUMNS = [f.name for f in fields(_SessionRow)]


def choose_title(custom: Optional[str], agent_name: Optional[str], ai_title: Optional[str],
                 first_prompt: Optional[str]) -> str:
    named, branch = [], None  # type: List[str], Optional[str]
    for cand in (custom, agent_name):
        found = _BRANCH_TITLE.search(cand) if cand else None
        if found:
            # /branch は同じ題名を custom-title と agent-name の両方に書く。自動の題名とみなし、
            # ai-title がないときだけ末尾を外して使う
            branch = branch or cand[:found.start()]
        elif cand:
            named.append(cand)
    for cand in named + [ai_title, branch]:
        if cand:
            return cand
    return (first_prompt or "")[:TITLE_CHARS]


def find_doc_paths(text: str, cwd: Optional[str], home: str, exts: Sequence[str]) -> List[str]:
    alternatives = "|".join(re.escape(e) for e in exts if e)
    if not alternatives:
        return []
    # 拡張子の直後が候補の終わりか、ASCII のファイル名を続けられない文字（「を」など）である所
    ext_end = re.compile("(?:%s)(?![%s])" % (alternatives, _NAME_CONTINUES), re.IGNORECASE | re.ASCII)
    found = []  # type: List[str]
    # split の結果は（引用符の外、引用符の中、外、…）の順。外は区切りの文字で切り、中はそのまま1つの候補にする
    parts = _QUOTED_AT.split(text)
    candidates = [c for i, part in enumerate(parts) for c in ([part] if i % 2 else _PATH_SEPARATORS.split(part))]
    for cand in candidates:
        if cand.startswith("@"):
            cand = cand[1:]
        ends = [m.end() for m in ext_end.finditer(cand)]
        if not ends:
            continue
        cand = cand[:max(ends)]  # 拡張子で終わる、いちばん長い頭の部分
        if "://" in cand:
            continue
        if cand.startswith("~/"):
            cand = os.path.join(home, cand[2:])
        elif not cand.startswith("/"):
            if cwd is None:
                continue
            cand = os.path.join(cwd, cand)
        cand = os.path.normpath(cand)
        if cand not in found:
            found.append(cand)
    return found


def ingest_transcript(conn: sqlite3.Connection, path: Path, ctx: IngestContext) -> Optional[IngestResult]:
    path = Path(path)
    sid = path.stem
    if conn.execute("SELECT 1 FROM tombstones WHERE session_id = ?", (sid,)).fetchone():
        return None
    st = path.stat()
    start, row, digest, reset, count_from = 0, _SessionRow(), (None, None), False, 0
    stored = _load_session(conn, sid)
    if stored is not None:
        size, mtime, inode, offset, prev = stored
        if (size, mtime, inode) == (st.st_size, st.st_mtime, st.st_ino):
            return None
        reset = st.st_size < size or st.st_ino != inode
        if not reset:
            start, row, digest, count_from = offset, prev, _load_digest(conn, sid), offset
        elif inode == 0 and offset <= st.st_size:
            # 読み直しの印（inode 0）。読み終えていた所までの行は、全体の集計にもう数えてある
            count_from = offset
    with conn:
        if reset:
            _clear(conn, sid)
        reader = _Reader(conn, sid, ctx, row, digest, start=start, count_from=count_from)
        if not reader.read(path):
            # ヘッドレスの扱いの途中で本人の入力が現れた。対話の扱いで頭から読み直す
            _clear(conn, sid)
            reader = _Reader(conn, sid, ctx, _SessionRow(), (None, None), start=0, count_from=count_from,
                             force_interactive=True)
            reader.read(path)
            reset = True
        reader.save(path, st)
    return IngestResult(sid, reader.new_records, reset, reader.row.kind, reader.touched, reader.unknown_records)


def _load_session(conn: sqlite3.Connection, sid: str) -> Optional[Tuple[int, float, int, int, _SessionRow]]:
    cols = ", ".join(["size", "mtime", "inode", "offset"] + _ROW_COLUMNS)
    found = conn.execute("SELECT %s FROM sessions WHERE id = ?" % cols, (sid,)).fetchone()
    if found is None:
        return None
    vals = tuple(found)
    return vals[0], vals[1], vals[2], vals[3], _SessionRow(*vals[4:])


def _load_digest(conn: sqlite3.Connection, sid: str) -> Tuple[Optional[str], Optional[str]]:
    found = conn.execute("SELECT first_prompt, last_reply FROM digests WHERE session_id = ?", (sid,)).fetchone()
    return (found[0], found[1]) if found is not None else (None, None)


def _clear(conn: sqlite3.Connection, sid: str) -> None:
    for table in _SESSION_TABLES:
        conn.execute("DELETE FROM %s WHERE session_id = ?" % table, (sid,))


def _pending_from(raw: Optional[str]) -> List[list]:
    """台帳に残した、応答を待っているスラッシュコマンド。読めなければ空。"""
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, list) and len(x) == 3 and isinstance(x[0], str)]


def _kind(row: _SessionRow) -> str:
    if row.n_prompts:
        return "interactive"
    return "headless" if (row.entrypoint or "").startswith("sdk") else "empty"


class _Reader:
    """記録を1回読み通し、出来事を台帳に反映する。"""

    def __init__(self, conn: sqlite3.Connection, sid: str, ctx: IngestContext, row: _SessionRow,
                 digest: Tuple[Optional[str], Optional[str]], *, start: int, count_from: int,
                 force_interactive: bool = False):
        self.conn = conn
        self.sid = sid
        self.ctx = ctx
        self.row = row
        self.digest_first, self.digest_last = digest
        self.offset = start
        # 全体の集計（format_stats・読めない行）と結果の数には、この位置より後の行だけを数える
        self.count_from = count_from
        self.force_interactive = force_interactive
        self.new_records = 0
        self.unknown_records = 0
        self.unreadable = 0
        self.stats = {}  # type: Dict[Tuple[str, str], int]
        self.touched = set()  # type: Set[str]
        self.pending = _pending_from(row.pending_commands)

    @property
    def headless(self) -> bool:
        # 最初に見えた entrypoint で決まり、本人の入力が1つでもあれば対話の扱いになる
        r = self.row
        return not self.force_interactive and r.n_prompts == 0 and (r.entrypoint or "").startswith("sdk")

    def read(self, path: Path) -> bool:
        """読み終えたら True。ヘッドレスの扱いの途中で本人の入力が現れたら、そこで止めて False。"""
        for line in iter_lines(path, self.offset):
            if not self._line(line):
                return False
            self.offset = line.end_offset
        return True

    def save(self, path: Path, st: os.stat_result) -> None:
        row = self.row
        row.kind = _kind(row)
        row.pending_commands = json.dumps(self.pending, ensure_ascii=False) if self.pending else None
        values = asdict(row)
        values.update(transcript_path=str(path), transcript_exists=1, size=st.st_size, mtime=st.st_mtime,
                      inode=st.st_ino, offset=self.offset)
        cols = list(values)
        self.conn.execute(
            "INSERT INTO sessions(id, %s) VALUES (?%s) ON CONFLICT(id) DO UPDATE SET %s"
            % (", ".join(cols), ", ?" * len(cols), ", ".join("%s = excluded.%s" % (c, c) for c in cols)),
            [self.sid] + [values[c] for c in cols])
        if row.kind == "headless":
            # INSERT OR REPLACE では全文検索の索引に古い行が残るので、上書きは UPDATE で行う
            parts = (self.digest_first, self.digest_last, row.recap)
            self.conn.execute(
                "INSERT INTO digests(session_id, first_prompt, last_reply, text) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET first_prompt = excluded.first_prompt, "
                "last_reply = excluded.last_reply, text = excluded.text",
                (self.sid, self.digest_first, self.digest_last, "\n".join(p for p in parts if p)))
        for (version, rtype), n in self.stats.items():
            self.conn.execute(
                "INSERT INTO format_stats(version, record_type, count) VALUES (?, ?, ?) "
                "ON CONFLICT(version, record_type) DO UPDATE SET count = count + excluded.count",
                (version, rtype, n))
        if self.unreadable:
            total = int(get_meta(self.conn, "unreadable_lines") or 0) + self.unreadable
            set_meta(self.conn, "unreadable_lines", str(total))

    def _line(self, line: RawLine) -> bool:
        rec = line.record
        new = line.end_offset > self.count_from
        if rec is None:
            if new and not line.skipped:
                self.unreadable += 1
            return True
        self.row.n_records += 1
        if new:
            self.new_records += 1
            key = (str(rec.get("version") or "?"), str(record_type(rec)))
            self.stats[key] = self.stats.get(key, 0) + 1
            typ = rec.get("type")
            if not (isinstance(typ, str) and typ in KNOWN_TYPES):
                self.unknown_records += 1
        try:
            events = extract_events(rec)
        except Exception:
            # 形の崩れた記録は読めない行として数え、次の行へ進む（ファイルごと止めない）
            if new:
                self.unreadable += 1
            return True
        if any(not isinstance(e, Meta) for e in events):
            self.row.n_recognized += 1
        for e in events:
            if not self._event(e):
                return False
        return True

    def _event(self, e: object) -> bool:
        r = self.row
        if isinstance(e, (AssistantText, ToolUse)) and self.pending and not self._answered():
            return False
        if isinstance(e, Meta):
            self._meta(e)
        elif isinstance(e, UserPrompt):
            if e.command:
                # スラッシュコマンドは応答を待つ。本人のコマンドは、次の本人の入力より前に応答が来たら
                # 本人の入力に数える
                self.pending = (self.pending + [[e.text, e.ts, e.human]])[-PENDING_LIMIT:]
                if e.human:
                    return True
            elif e.human:
                if self.headless:
                    return False
                self.pending = []  # 応答のないまま次の入力が来たコマンドは数えない
            self._mention(e.text, e.ts)
            if e.human:
                self._prompt(e)
            elif self.headless and self.digest_first is None:
                self.digest_first = e.text[:PROMPT_CHARS]
        elif isinstance(e, LocalCommandOutput):
            # Claude Code の中だけで済んだコマンド（/model など）の出力は、直前のコマンドだけを取り消す
            if self.pending:
                self.pending.pop()
        elif isinstance(e, AssistantText):
            self._mention(e.text, e.ts)
            r.last_reply = e.text[:REPLY_CHARS]
            if self.headless:
                self.digest_last = e.text[:PROMPT_CHARS]
            else:
                self._message(e.ts, "assistant", e.text)
        elif isinstance(e, ToolUse):
            self._tool(e)
        elif isinstance(e, ToolResult):
            self._mention(e.text, e.ts)
        elif isinstance(e, Title):
            if e.text.strip():
                setattr(r, _TITLE_COLUMNS[e.kind], e.text)
        elif isinstance(e, Recap):
            if e.text.strip():
                r.recap, r.recap_at = e.text, e.ts
        elif isinstance(e, ContinuedIn):
            r.continued_in = e.session_id
        elif isinstance(e, PrLink):
            self.conn.execute("INSERT OR IGNORE INTO session_prs(session_id, url) VALUES (?, ?)", (self.sid, e.url))
        elif isinstance(e, Relocated):
            r.relocated_cwd = e.cwd
        elif isinstance(e, ForkedFrom):
            r.forked_from = r.forked_from or e.session_id
        elif isinstance(e, DerivedFrom):
            self.conn.execute("INSERT OR IGNORE INTO derived_from(session_id, source_id, via) VALUES (?, ?, ?)",
                              (self.sid, e.session_id, e.via))
        return True

    def _answered(self) -> bool:
        """応答の来た本人のスラッシュコマンドを本人の入力にする。ヘッドレスの扱いの途中なら False（対話として読み直す）。"""
        waiting = [(text, ts) for text, ts, human in self.pending if human]
        if waiting and self.headless:
            return False
        self.pending = []
        for text, ts in waiting:
            self._mention(text, ts)
            self._prompt(UserPrompt(ts, text, True))
        return True

    def _meta(self, e: Meta) -> None:
        r = self.row
        if e.ts:
            r.started_at = min(r.started_at or e.ts, e.ts)
            r.ended_at = max(r.ended_at or e.ts, e.ts)
        r.cwd = r.cwd or e.cwd or None
        r.entrypoint = r.entrypoint or e.entrypoint or None
        r.version = e.version or r.version
        r.branch = e.branch or r.branch

    def _prompt(self, e: UserPrompt) -> None:
        r = self.row
        r.turn += 1
        r.n_prompts += 1
        # 最初と最後の依頼は表示用（打ち込んだ文が先）。本文（検索）は元の順の全文
        shown = e.display or e.text
        r.first_prompt = r.first_prompt or shown[:PROMPT_CHARS]
        r.last_prompt = shown[:PROMPT_CHARS]
        self._message(e.ts, "user", e.text)
        for path in find_doc_paths(e.text, r.cwd, self.ctx.home, self.ctx.exts):
            self._link(path, "named", e.ts, r.n_prompts)

    def _tool(self, e: ToolUse) -> None:
        self._mention(json.dumps(e.input, ensure_ascii=False), e.ts)
        if e.name in _AGENT_TOOLS:
            self.row.n_agents += 1
        rel = _DOC_RELS.get(e.name)
        if rel:
            self._link(e.input.get("notebook_path" if e.name == "NotebookEdit" else "file_path"), rel, e.ts)
        if not self.headless:
            self._message(e.ts, "tool", tool_summary(e.name, e.input, self.row.cwd))

    def _message(self, ts: Optional[str], role: str, text: str) -> None:
        r = self.row
        self.conn.execute(
            "INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, ?, ?, ?, ?, ?)",
            (self.sid, r.next_seq, r.turn, ts, role, text))
        r.next_seq += 1

    def _link(self, raw: object, rel: str, ts: Optional[str], prompt_index: Optional[int] = None) -> None:
        if not isinstance(raw, str) or not raw:
            return
        path = os.path.normpath(raw)
        # 相対パスは台帳に入れない（後で読むときに、どこを基準にするか分からないため）
        if not (os.path.isabs(path) and allowed_doc_path(path, self.ctx.home)):
            return
        is_doc = is_document(path, self.ctx.exts)
        if not is_doc:
            # タスクの置き場所の中への、資料でないファイルの書き込みは、rel を task_file にして残す
            # （資料の一覧には出さない。タスクの「資料を書いた」の結び付けが読む）
            if not (rel in ("created", "edited") and self.ctx.tasks_root and within(path, self.ctx.tasks_root)):
                return
            rel = "task_file"
        self.conn.execute(
            "INSERT OR IGNORE INTO doc_links(session_id, path, rel, ts, prompt_index) VALUES (?, ?, ?, ?, ?)",
            (self.sid, path, rel, ts, prompt_index))
        if is_doc:
            self.touched.add(path)

    def _mention(self, text: str, ts: Optional[str]) -> None:
        for found in UUID_RE.findall(text):
            if found != self.sid:
                self.conn.execute(
                    "INSERT OR IGNORE INTO mentions(session_id, mentioned_id, ts) VALUES (?, ?, ?)",
                    (self.sid, found, ts))
