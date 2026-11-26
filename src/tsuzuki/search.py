"""検索。会話・資料・入力履歴・ヘッドレスの要約を、日本語の部分一致で探す。

3文字以上の語は FTS5 の trigram の索引で、1〜2文字の語（trigram が使えないときはすべての語）は LIKE で探す。
会話の AND はターンごとに判定し、一致はセッションごとにまとめる。
"""
import functools
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Pattern, Sequence, Set, Tuple

from tsuzuki.retention import DAY_SECONDS, iso_utc
from tsuzuki.store import has_trigram

SNIPPET_RADIUS = 45
FTS_MIN_CHARS = 3  # trigram の索引で引ける語の最短の長さ
# 語の数と長さの上限。これを超えた分は使わない（SQLite の式の深さと LIKE の長さの制限、全文の走査の回数を抑える）
MAX_TERMS = 16
MAX_TERM_CHARS = 1000
MAX_DAYS = 36500  # これより長い期間は、期間で絞らないのと同じにする
RESULT_LIMIT = 100  # 会話・資料・入力履歴・ヘッドレスのそれぞれで返す一致の数（画面とコマンドは、上限まで返ったらそう書く）
DEFAULT_TARGETS = frozenset({"conversation", "documents", "history"})
TARGETS = DEFAULT_TARGETS | {"headless"}
ORDERS = ("recent", "count")
# 発言者の指定と、当てる messages.role
_ROLES = {"user": ("user",), "assistant": ("assistant", "tool")}
_ELLIPSIS = "…"
_WHITESPACE = re.compile(r"\s+")
# SQLite に渡せない文字（FTS5 の式を途中で終わらせる NUL と、UTF-8 にできないサロゲート）
_UNUSABLE = re.compile("[\x00\ud800-\udfff]")


@dataclass
class SearchQuery:
    text: str
    place_ids: Optional[List[int]] = None
    days: Optional[int] = None
    targets: FrozenSet[str] = DEFAULT_TARGETS  # headless も入れられる
    role: Optional[str] = None  # user か assistant（assistant は tool の要約も含む）
    order: str = "recent"  # recent か count
    limit: int = RESULT_LIMIT


@dataclass
class Snippet:
    text: str
    marks: List[Tuple[int, int]]
    role: str  # 発言の role。資料は document、入力履歴は user、ヘッドレスは digest
    ts: Optional[str]


@dataclass
class SessionHit:
    session_id: str
    turns: List[int]  # 一致したターン（新しい順）
    hit_count: int
    last_ts: Optional[str]
    snippet: Snippet


@dataclass
class DocHit:
    doc_id: int
    snippet: Snippet
    path_only: bool = False  # 題名にも本文にも語がなく、パスにだけ当たった（抜粋はパス）


@dataclass
class HistoryHit:
    history_id: int
    session_id: Optional[str]
    project: str
    ts: str
    snippet: Snippet
    place_id: Optional[int] = None  # 入力のあった場所（分からなければ None）


@dataclass
class SearchResult:
    sessions: List[SessionHit]
    headless: List[SessionHit]
    documents: List[DocHit]
    history: List[HistoryHit]
    places: List[Tuple[int, int]]  # (場所, その場所の一致の数)
    elapsed_ms: float


def parse_query(text: str) -> List[str]:
    """検索の語に分ける。空白で区切り、"…" で囲んだ部分は空白ごと1語にし、閉じていない " は残りを1語にする。"""
    pieces = []  # type: List[str]
    word = []  # type: List[str]
    quoted = False
    for ch in text or "":
        if ch == '"' or (ch.isspace() and not quoted):
            pieces.append("".join(word))
            word = []
            if ch == '"':
                quoted = not quoted
        else:
            word.append(ch)
    pieces.append("".join(word))
    terms, seen = [], set()
    for piece in pieces:
        piece = piece.strip()
        if piece and piece not in seen:
            seen.add(piece)
            terms.append(piece)
    return terms


def conversation_turns(conn: sqlite3.Connection, session_id: str, terms: Sequence[str]) -> List[int]:
    """1つの会話（台帳の発言）の中で、すべての語がそろうターンを古い順に返す。語ごとに、その語を含む発言の
    あるターンを求め、共通部分を取る（上の検索窓の会話と同じく、語は同じターンの別の発言に分かれていてよい）。
    発言者は問わない。英字だけ大文字と小文字を区別しない（LIKE）。"""
    found = None  # type: Optional[Set[int]]
    for term in terms:
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        turns = {row[0] for row in conn.execute(
            "SELECT DISTINCT turn FROM messages WHERE session_id = ? AND text LIKE ? ESCAPE '\\'",
            (session_id, pattern))}
        found = turns if found is None else found & turns
        if not found:
            return []
    return sorted(found or ())


def search(conn: sqlite3.Connection, q: SearchQuery, *, now: Callable[[], float] = time.time) -> SearchResult:
    started = time.perf_counter()
    _validate(q)
    terms = [term[:MAX_TERM_CHARS] for term in parse_query(_UNUSABLE.sub("\ufffd", q.text or ""))][:MAX_TERMS]
    result = SearchResult(sessions=[], headless=[], documents=[], history=[], places=[], elapsed_ms=0.0)
    if terms:
        run = _Run(conn, q, terms, trigram=has_trigram(conn), now=now)
        if "conversation" in q.targets:
            result.sessions = run.conversation()
        if "documents" in q.targets:
            result.documents = run.documents()
        if "history" in q.targets:
            result.history = run.history()
        if "headless" in q.targets:
            result.headless = run.headless()
        result.places = run.place_counts()
    result.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return result


def make_snippet(text: Optional[str], terms: Sequence[str], *, role: str, ts: Optional[str]) -> Snippet:
    """最初に現れる語の前後 SNIPPET_RADIUS 字を抜き出し、窓の中のすべての語に印を付ける。"""
    text = text or ""
    return _snippet(text, _first(text, terms), terms, role, ts)


def _validate(q: SearchQuery) -> None:
    if q.role is not None and q.role not in _ROLES:
        raise ValueError("role は user か assistant にしてください: %r" % (q.role,))
    if q.order not in ORDERS:
        raise ValueError("order は recent か count にしてください: %r" % (q.order,))
    unknown = set(q.targets) - TARGETS
    if unknown:
        raise ValueError("targets に知らない対象があります: %r" % (sorted(unknown, key=str),))


def _cutoff(days: Optional[float], now: Callable[[], float]) -> Optional[float]:
    """期間の始まり（UNIX 時刻）。MAX_DAYS を超える日数（無限大と NaN も）は絞らず、負の日数は0日とみなす。"""
    if days is None or not days <= MAX_DAYS:
        return None
    return now() - max(days, 0) * DAY_SECONDS


@functools.lru_cache(maxsize=256)
def _pattern(terms: Tuple[str, ...]) -> Pattern[str]:
    """語のどれかに当たる正規表現。英字（ASCII）だけ大文字小文字を区別しない。
    長い語を先に並べるので、同じ位置から始まる語のうち長い方に当たる。語がなければ何にも当たらない。"""
    ordered = sorted({term for term in terms if term}, key=lambda term: (-len(term), term))
    return re.compile("|".join(re.escape(term) for term in ordered) or "(?!)", re.IGNORECASE | re.ASCII)


def _first(text: str, terms: Sequence[str]) -> Optional[Tuple[int, int]]:
    """最も前にある語の位置（同じ位置なら長い語）。本文を写して変換せずに探す。"""
    found = _pattern(tuple(terms)).search(text) if terms and text else None
    return found.span() if found else None


def _snippet(text: str, first: Optional[Tuple[int, int]], terms: Sequence[str], role: str,
             ts: Optional[str]) -> Snippet:
    if first is None:
        start, end = 0, min(len(text), 2 * SNIPPET_RADIUS)
    else:
        start, end = max(0, first[0] - SNIPPET_RADIUS), min(len(text), first[1] + SNIPPET_RADIUS)
    body = _WHITESPACE.sub(" ", text[start:end]).strip()
    head = _ELLIPSIS if start > 0 else ""
    tail = _ELLIPSIS if end < len(text) else ""
    marks = []  # type: List[Tuple[int, int]]
    if terms:
        # 窓の空白はまとめたので、語の中の空白もまとめて探す。finditer は前から順に、重ならずに当てる
        collapsed = tuple(_WHITESPACE.sub(" ", term) for term in terms)
        marks = [(m.start() + len(head), m.end() + len(head)) for m in _pattern(collapsed).finditer(body)]
    return Snippet(head + body + tail, marks, role, ts)


def _later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


def _conversation_snippet(conn: sqlite3.Connection, session_id: str, turn: int, terms: Sequence[str],
                          roles: Optional[Sequence[str]]) -> Snippet:
    """ターンの発言（発言者の絞り込みに合うもの）のうち、語を最も多く含むもの（同数なら後の発言）から抜粋を作る。"""
    sql = "SELECT seq, role, ts, text FROM messages WHERE session_id = ? AND turn = ?"
    params = [session_id, turn]  # type: List[object]
    if roles:
        sql += " AND role IN (%s)" % _placeholders(len(roles))
        params.extend(roles)
    patterns = [_pattern((term,)) for term in terms]
    best = None
    for seq, role, ts, text in conn.execute(sql, params):
        rank = (sum(1 for p in patterns if p.search(text)), seq)
        if best is None or rank > best[0]:
            best = (rank, role, ts, text)
    if best is None:  # 間に台帳が更新されて発言が消えたとき
        return Snippet("", [], "", None)
    return make_snippet(best[3], terms, role=best[1], ts=best[2])


class _Run:
    """1回の検索の語と絞り込み。表ごとに一致を求め、返した一致の場所を覚えておく。"""

    def __init__(self, conn: sqlite3.Connection, q: SearchQuery, terms: List[str], *, trigram: bool,
                 now: Callable[[], float]):
        self.conn = conn
        self.terms = terms
        self.trigram = trigram
        self.place_ids = None if q.place_ids is None else list(q.place_ids)
        self.roles = _ROLES[q.role] if q.role else None
        self.order = q.order
        self.limit = max(0, q.limit)
        self.cutoff = _cutoff(q.days, now)
        self.cutoff_iso = None if self.cutoff is None else iso_utc(self.cutoff)
        self.places = []  # type: List[Optional[int]]

    def _term(self, term: str, fts: str, rowid: str, cols: Sequence[str]) -> Tuple[str, List[object]]:
        """1語に当たる行の条件と、その引数。"""
        if self.trigram and len(term) >= FTS_MIN_CHARS:
            return ("%s IN (SELECT rowid FROM %s WHERE %s MATCH ?)" % (rowid, fts, fts),
                    ['"' + term.replace('"', '""') + '"'])
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return "(%s)" % " OR ".join("%s LIKE ? ESCAPE '\\'" % c for c in cols), [pattern] * len(cols)

    def _all_terms(self, fts: str, rowid: str, cols: Sequence[str]) -> Tuple[List[str], List[object]]:
        where, params = [], []  # type: List[str], List[object]
        for term in self.terms:
            cond, args = self._term(term, fts, rowid, cols)
            where.append(cond)
            params.extend(args)
        return where, params

    def _filters(self, place_col: str, time_col: str, cutoff: object) -> Tuple[List[str], List[object]]:
        where, params = [], []  # type: List[str], List[object]
        if self.place_ids is not None:
            where.append("%s IN (%s)" % (place_col, _placeholders(len(self.place_ids))))
            params.extend(self.place_ids)
        if cutoff is not None:
            where.append("%s >= ?" % time_col)
            params.append(cutoff)
        return where, params

    def conversation(self) -> List[SessionHit]:
        filters, filter_params = self._filters("s.place_id", "m.ts", self.cutoff_iso)
        if self.roles:
            filters.append("m.role IN (%s)" % _placeholders(len(self.roles)))
            filter_params.extend(self.roles)
        # (セッション, ターン) → そのターンで一致した発言の最も新しい時刻。語ごとに求めて共通部分を取る
        turns = None  # type: Optional[Dict[Tuple[str, int], Optional[str]]]
        place_of = {}  # type: Dict[str, Optional[int]]
        for term in self.terms:
            cond, params = self._term(term, "messages_fts", "m.id", ["m.text"])
            sql = ("SELECT m.session_id, m.turn, MAX(m.ts), s.place_id FROM messages m "
                   "JOIN sessions s ON s.id = m.session_id WHERE s.kind = 'interactive' AND "
                   + " AND ".join([cond] + filters) + " GROUP BY m.session_id, m.turn, s.place_id")
            found = {}  # type: Dict[Tuple[str, int], Optional[str]]
            for session_id, turn, ts, place in self.conn.execute(sql, params + filter_params):
                if turns is None or (session_id, turn) in turns:
                    found[(session_id, turn)] = ts
                    place_of[session_id] = place
            # ターンの時刻は、どの語の一致も含めた最も新しい時刻（最後の語の一致だけではない）
            turns = found if turns is None else {k: _later(turns[k], ts) for k, ts in found.items()}
            if not turns:
                return []
        by_session = {}  # type: Dict[str, List[Tuple[Optional[str], int]]]
        for (session_id, turn), ts in (turns or {}).items():
            by_session.setdefault(session_id, []).append((ts, turn))
        groups = []  # (セッション, 新しい順のターン, 最後の一致の時刻)
        for session_id, items in by_session.items():
            items.sort(key=lambda it: (it[0] or "", it[1]), reverse=True)
            groups.append((session_id, [turn for _, turn in items], items[0][0]))
        if self.order == "count":
            groups.sort(key=lambda g: (len(g[1]), g[2] or "", g[0]), reverse=True)
        else:
            groups.sort(key=lambda g: (g[2] or "", g[0]), reverse=True)
        hits = []
        for session_id, turn_list, last_ts in groups[:self.limit]:
            snippet = _conversation_snippet(self.conn, session_id, turn_list[0], self.terms, self.roles)
            hits.append(SessionHit(session_id, turn_list, len(turn_list), last_ts, snippet))
            self.places.append(place_of[session_id])
        return hits

    def _path_term(self, term: str) -> Tuple[str, List[object]]:
        """1語がパス（ファイル名とフォルダー名）に当たる条件。短い語でも LIKE で探す。"""
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return "d.path LIKE ? ESCAPE '\\'", [pattern]

    def documents(self) -> List[DocHit]:
        # 語ごとに「題名・本文に当たる OR パスに当たる」。語ごとに別の場所に当たってもよい
        where, params = [], []  # type: List[str], List[object]
        for term in self.terms:
            cond, args = self._term(term, "documents_fts", "d.id", ["d.title", "d.body"])
            path_cond, path_args = self._path_term(term)
            where.append("(%s OR %s)" % (cond, path_cond))
            params.extend(args + path_args)
        filters, filter_params = self._filters("d.place_id", "d.mtime", self.cutoff)
        sql = ("SELECT d.id, d.place_id, d.mtime, d.title, d.body, d.path FROM documents d WHERE "
               + " AND ".join(where + filters) + " ORDER BY d.mtime DESC, d.id DESC LIMIT ?")
        hits = []
        for doc_id, place, mtime, title, body, path in self.conn.execute(sql, params + filter_params + [self.limit]):
            # 本文から抜粋する。本文がないか、本文に語がなければ題名から。題名にもなければパスから
            ts = iso_utc(mtime)
            first = _first(body or "", self.terms)
            path_only = False
            if first is not None:
                snippet = _snippet(body, first, self.terms, "document", ts)
            elif _first(title or "", self.terms) is not None:
                snippet = make_snippet(title, self.terms, role="document", ts=ts)
            else:
                snippet = make_snippet(path, self.terms, role="document", ts=ts)
                path_only = True
            hits.append(DocHit(doc_id, snippet, path_only))
            self.places.append(place)
        return hits

    def history(self) -> List[HistoryHit]:
        where, params = self._all_terms("history_fts", "h.id", ["h.text"])
        filters, filter_params = self._filters("h.place_id", "h.ts", self.cutoff_iso)
        # 会話が台帳に残っているセッションの入力は会話と重なるので、台帳にないセッションと、記録が消えて会話の
        # 本文も残っていない（retention: card）セッションの分だけを返す
        sql = ("SELECT h.id, h.session_id, h.project, h.place_id, h.ts, h.text FROM history_prompts h "
               "LEFT JOIN sessions s ON s.id = h.session_id "
               "WHERE (s.id IS NULL OR (s.transcript_exists = 0 AND NOT EXISTS "
               "(SELECT 1 FROM messages m WHERE m.session_id = h.session_id))) AND "
               + " AND ".join(where + filters) + " ORDER BY h.ts DESC, h.id DESC LIMIT ?")
        hits = []
        for history_id, session_id, project, place, ts, text in self.conn.execute(
                sql, params + filter_params + [self.limit]):
            snippet = make_snippet(text, self.terms, role="user", ts=ts)
            hits.append(HistoryHit(history_id, session_id, project or "", ts or "", snippet, place))
            self.places.append(place)
        return hits

    def headless(self) -> List[SessionHit]:
        where, params = self._all_terms("digests_fts", "d.rowid", ["d.text"])
        filters, filter_params = self._filters("s.place_id", "s.ended_at", self.cutoff_iso)
        sql = ("SELECT d.session_id, s.place_id, s.ended_at, d.text FROM digests d "
               "JOIN sessions s ON s.id = d.session_id WHERE s.kind = 'headless' AND "
               + " AND ".join(where + filters) + " ORDER BY s.ended_at DESC, d.session_id DESC LIMIT ?")
        hits = []
        for session_id, place, ended_at, text in self.conn.execute(sql, params + filter_params + [self.limit]):
            snippet = make_snippet(text, self.terms, role="digest", ts=ended_at)
            hits.append(SessionHit(session_id, [], 1, ended_at, snippet))
            self.places.append(place)
        return hits

    def place_counts(self) -> List[Tuple[int, int]]:
        counts = {}  # type: Dict[int, int]
        for place in self.places:
            if place is not None:
                counts[place] = counts.get(place, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
