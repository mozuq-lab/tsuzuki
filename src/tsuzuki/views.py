"""画面に出すデータ。台帳を読み、一覧・状況カード・場所の概要・資料・会話の形にする。
HTML は作らない（資料の中身の変換だけ render に任せる）。"""
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from tsuzuki.documents import classify_kind
from tsuzuki.ingest import choose_title
from tsuzuki.places import short_labels
from tsuzuki.relations import PRECEDENCE
from tsuzuki.render import render_markdown, render_pre
from tsuzuki.resume import is_session_id, resume_command, resume_cwd
from tsuzuki.retention import DAY_SECONDS, iso_utc
from tsuzuki.sources.live import LiveSession
from tsuzuki.sources.transcript import AssistantText, ToolUse, UserPrompt, extract_events, iter_lines, tool_summary
from tsuzuki.tasks import TASK_MD, within

RELATION_LABELS = {
    ("fork", "in"): "分岐元", ("fork", "out"): "分岐先",
    ("continued", "in"): "前の会話", ("continued", "out"): "続きの会話",
    ("child", "in"): "親", ("child", "out"): "子",
    ("handoff", "in"): "引き継ぎ資料を読んで開始", ("handoff", "out"): "引き継ぎ先",
    ("reference", "in"): "参照した会話", ("reference", "out"): "参照された会話",
}
WEAK_SUFFIX = "（推定）"
# 場所の時系列の矢印に添える言葉（状況カードの言葉とは別）
TIMELINE_LABELS = {"fork": "分岐", "continued": "続きの会話へ", "child": "子",
                   "handoff": "引き継ぎ資料を読んで開始", "reference": "参照"}
MOVED_LABEL = "別の場所へ移った"

LIST_LIMIT = 200  # 一覧に出すセッションの数（切ったときは、画面が条件に合う数を count_sessions で出す）
DOCS_LIMIT = 300  # 資料の一覧に出す数（切ったときは、画面が条件に合う数を count_documents で出す）
CARD_DOCS = 5  # 状況カードに出す資料の数
TIMELINE_LIMIT = 20
FOLDER_LIMIT = 5
FOLDER_DOCS = 5
FIRST_LINE_CHARS = 120
CONVERSATION_TURNS = 20  # 会話を読み込む単位（ターン）。最初は一致のターンの前後に、続きは前か後ろにこれだけ
TRANSCRIPT_ITEMS = 200  # ヘッドレスの記録から一度に読む項目の数
_CHUNK = 500  # IN (...) に1回で渡す数

_REL_RANK = {"created": 0, "edited": 1, "named": 2, "read": 3}  # 資料との関わりの強い順
_MARKDOWN_EXTS = (".md", ".markdown", ".mdx")
_PLAIN_EXTS = (".txt", ".csv")


@dataclass
class SessionRow:
    id: str
    title: str
    kind: str
    place_id: Optional[int]
    place_display: str
    ended_at: Optional[str]
    n_prompts: int
    n_children: int
    prs: List[str]
    summary: str  # 要約。なければ最後の依頼（summary_is_recap で区別）
    summary_is_recap: bool
    live: bool
    transcript_exists: bool
    place_label: str  # 場所の短い名前（places.short_labels）。place_display は表示名の全体


@dataclass
class RelationView:
    type: str
    confidence: str
    label: str
    peer_id: str
    peer_title: str
    peer_place: str
    peer_started_at: Optional[str]
    peer_ended_at: Optional[str]
    cross_place: bool
    evidence: str  # つながりの根拠（relations.evidence。例「資料 handoff.md」「記録に ID」）
    peer_place_label: str


@dataclass
class DocLinkView:
    doc_id: Optional[int]
    path: str
    display_path: str
    kind: str
    rel: str
    mtime: Optional[float]
    size: Optional[int]


@dataclass
class SessionDetail(SessionRow):
    branch: Optional[str]
    started_at: Optional[str]
    n_agents: int
    recap: Optional[str]
    recap_at: Optional[str]
    prompts_after_recap: int
    first_prompt: Optional[str]
    last_prompt: Optional[str]
    last_reply: Optional[str]
    cwd: Optional[str]
    docs: List[DocLinkView]
    docs_total: int
    relations: List[RelationView]
    children_total: int
    resume_command: Optional[str]
    tasks: List[Tuple[str, str, str, str]] = field(default_factory=list)  # （名前、題、根拠、確かさ）


@dataclass
class ChildRow:
    id: str
    started_at: Optional[str]
    first_line: str


@dataclass
class PlaceItem:
    id: int
    display: str
    n_interactive: int
    n_headless: int
    n_docs: int
    last_at: Optional[str]
    label: str  # 短い名前（画面の狭い所に出す）


@dataclass
class TimelineItem:
    session_id: str
    title: str
    started_at: Optional[str]
    ended_at: Optional[str]
    n_prompts: int
    n_children: int
    via_label: Optional[str]
    other_place: Optional[str]
    via_evidence: Optional[str]  # 矢印のつながりの根拠
    other_place_label: Optional[str]


@dataclass
class DocItem:
    id: int
    path: str
    name: str
    kind: str
    place_display: str
    folder: str
    mtime: float
    size: int
    ext: str
    place_label: str
    task: Optional[Tuple[str, str]] = None  # タスクのフォルダーの中の資料なら（名前、題）
    task_cover: bool = False  # そのタスクのフォルダーの直下の task.md（表紙）か


@dataclass
class PlaceOverview:
    place: PlaceItem
    timeline: List[TimelineItem]
    folders: List[Tuple[str, List[DocItem]]]
    orphans_total: int  # その場所の、親が分からないヘッドレスの数


@dataclass
class DocumentDetail:
    item: DocItem
    title: Optional[str]
    html: Optional[str]  # HTML の資料・PDF・Office は None（画面は /doc-raw/<id> の枠か外のアプリで出す）
    links: List[Tuple[str, SessionRow]]


@dataclass
class ConvItem:
    kind: str  # user・assistant・tools（続けて呼んだツールは1つにまとめる）
    ts: Optional[str]
    text: str
    turn: int
    tools: List[str]


@dataclass
class ConversationWindow:
    session_id: str
    items: List[ConvItem]
    first_turn: int
    last_turn: int
    total_turns: int
    source: str  # ledger・transcript・none
    # 窓の前と後ろに残っている分（台帳の会話は発言のあるターンの数、記録から読んだときは項目の数）
    before: int = 0
    after: int = 0
    offset: int = 0  # 記録から読んだとき、窓の最初の項目の番号


_SESSION_SELECT = (
    "SELECT s.id, s.kind, s.place_id, COALESCE(p.display, '') AS place_display, s.started_at, s.ended_at, "
    "s.n_prompts, s.n_agents, s.custom_title, s.agent_name, s.ai_title, s.first_prompt, s.last_prompt, "
    "s.recap, s.recap_at, s.last_reply, s.transcript_exists, s.cwd, s.branch, d.first_prompt AS digest_first, "
    "(SELECT COUNT(*) FROM relations r WHERE r.src = s.id AND r.type = 'child') AS n_children "
    "FROM sessions s LEFT JOIN places p ON p.id = s.place_id LEFT JOIN digests d ON d.session_id = s.id")
_TIMELINE_SELECT = (
    "SELECT s.id, s.custom_title, s.agent_name, s.ai_title, s.first_prompt, d.first_prompt AS digest_first, "
    "s.started_at, s.ended_at, s.n_prompts, COALESCE(p.display, '') AS place_display, "
    "(SELECT COUNT(*) FROM relations r WHERE r.src = s.id AND r.type = 'child') AS n_children "
    "FROM sessions s LEFT JOIN places p ON p.id = s.place_id LEFT JOIN digests d ON d.session_id = s.id "
    "WHERE s.kind = 'interactive' AND ")
_CHILD_SELECT = ("SELECT s.id, s.started_at, s.first_prompt, d.first_prompt FROM sessions s "
                 "LEFT JOIN digests d ON d.session_id = s.id ")
_ORPHAN = ("s.kind = 'headless' AND s.place_id = ? AND NOT EXISTS "
           "(SELECT 1 FROM relations r WHERE r.dst = s.id AND r.type = 'child')")
_NEWEST_FIRST = " ORDER BY s.started_at IS NULL, s.started_at DESC, s.id LIMIT ? OFFSET ?"
_DOC_COLUMNS = ("d.id, d.path, d.kind, d.ext, d.size, d.mtime, d.title, p.path AS place_path, "
                "COALESCE(p.display, '') AS place_display")
_DOC_FROM = " FROM documents d LEFT JOIN places p ON p.id = d.place_id"
_DOC_SELECT = "SELECT " + _DOC_COLUMNS + _DOC_FROM  # 一覧では本文（最大 512KB）を読まない


# セッションの一覧と状況カード

def list_sessions(conn: sqlite3.Connection, *, place_id: Optional[int] = None, days: Optional[float] = None,
                  include_headless: bool = False, limit: int = LIST_LIMIT, live: Dict[str, LiveSession],
                  now: Callable[[], float] = time.time) -> List[SessionRow]:
    """最後の動きの新しい順。空のセッションは出さず、ヘッドレスは include_headless のときだけ出す。"""
    where, params = _list_filter(place_id, days, include_headless, now)
    rows = _rows(conn, _SESSION_SELECT + " WHERE " + where
                 + " ORDER BY s.ended_at IS NULL, s.ended_at DESC, s.id LIMIT ?", params + [max(0, limit)])
    prs, labels = _prs(conn, [r["id"] for r in rows]), _labels(conn)
    return [SessionRow(**_row_values(r, prs.get(r["id"], []), live, labels)) for r in rows]


def count_sessions(conn: sqlite3.Connection, *, place_id: Optional[int] = None, days: Optional[float] = None,
                   include_headless: bool = False, now: Callable[[], float] = time.time) -> int:
    """list_sessions と同じ条件に合うセッションの数（一覧を上限で切ったときに、画面に出す）。"""
    where, params = _list_filter(place_id, days, include_headless, now)
    return conn.execute("SELECT COUNT(*) FROM sessions s WHERE " + where, params).fetchone()[0]


def session_detail(conn: sqlite3.Connection, session_id: str, *, live: Dict[str, LiveSession],
                   home: str) -> Optional[SessionDetail]:
    found = _rows(conn, _SESSION_SELECT + " WHERE s.id = ?", (session_id,))
    if not found:
        return None
    r = found[0]
    docs, docs_total = _card_docs(conn, session_id, r["cwd"], home)
    command = None
    if r["transcript_exists"] and r["cwd"] and is_session_id(session_id):
        # 名前を変えたフォルダーで始めた会話は、今のパスで再開する
        command = resume_command(resume_cwd(conn, r["cwd"]), session_id)
    labels = _labels(conn)
    return SessionDetail(
        **_row_values(r, _prs(conn, [session_id]).get(session_id, []), live, labels),
        branch=r["branch"], started_at=r["started_at"], n_agents=r["n_agents"], recap=r["recap"],
        recap_at=r["recap_at"], prompts_after_recap=_prompts_after(conn, session_id, r["recap"], r["recap_at"]),
        first_prompt=_first_prompt(r), last_prompt=r["last_prompt"], last_reply=r["last_reply"], cwd=r["cwd"],
        docs=docs, docs_total=docs_total, relations=_relations(conn, session_id, r["place_id"], labels),
        children_total=r["n_children"], resume_command=command, tasks=_session_tasks(conn, session_id))


def session_rows(conn: sqlite3.Connection, ids: Sequence[str], *,
                 live: Dict[str, LiveSession]) -> Dict[str, SessionRow]:
    """ID ごとの一覧の行（検索の一致に題名や場所を添えるため）。台帳にない ID は含めない。"""
    return _session_rows(conn, ids, live)


def children(conn: sqlite3.Connection, session_id: str, *, offset: int = 0, limit: int = 50) -> List[ChildRow]:
    """子セッション（child のつながりの先）。新しい順。"""
    return _child_rows(conn, _CHILD_SELECT + "JOIN relations r ON r.dst = s.id WHERE r.src = ? AND r.type = 'child'",
                       [session_id], offset, limit)


def orphan_headless(conn: sqlite3.Connection, place_id: int, *, offset: int = 0, limit: int = 50) -> List[ChildRow]:
    """その場所のヘッドレスのうち、child のつながりで親が決まっていないもの。新しい順。"""
    return _child_rows(conn, _CHILD_SELECT + "WHERE " + _ORPHAN, [place_id], offset, limit)


# 場所

def list_places(conn: sqlite3.Connection) -> List[PlaceItem]:
    """空でないセッションか資料のある場所を、最後の動きの新しい順に。"""
    places = [p for p in _place_items(conn, None, _labels(conn)) if p.n_interactive or p.n_headless or p.n_docs]
    places.sort(key=lambda p: (p.display, p.id))
    places.sort(key=lambda p: p.last_at or "", reverse=True)
    return places


def place_overview(conn: sqlite3.Connection, place_id: int, *, home: Optional[str] = None) -> Optional[PlaceOverview]:
    labels = _labels(conn)
    found = _place_items(conn, place_id, labels)
    if not found:
        return None
    orphans = conn.execute("SELECT COUNT(*) FROM sessions s WHERE " + _ORPHAN, (place_id,)).fetchone()[0]
    return PlaceOverview(place=found[0], timeline=_timeline(conn, place_id, labels),
                         folders=_folders(conn, place_id, _home(home), labels), orphans_total=orphans)


def place_displays(conn: sqlite3.Connection, ids: Sequence[int]) -> Dict[int, str]:
    """場所の ID ごとの表示名。台帳にない ID は含めない。"""
    found = {}  # type: Dict[int, str]
    for chunk in _chunks(ids):
        found.update(conn.execute("SELECT id, display FROM places WHERE id IN (%s)" % _marks(len(chunk)), chunk))
    return found


def place_labels(conn: sqlite3.Connection, ids: Sequence[int]) -> Dict[int, str]:
    """場所の ID ごとの短い名前（台帳のすべての場所の中で重ならないように作る）。台帳にない ID は含めない。"""
    labels = _labels(conn)
    return {pid: _label(labels, display) for pid, display in place_displays(conn, ids).items()}


# 資料

def documents_list(conn: sqlite3.Connection, *, kind: Optional[str] = None, place_id: Optional[int] = None,
                   limit: int = DOCS_LIMIT, home: Optional[str] = None) -> List[DocItem]:
    """資料を新しい順に（更新時刻の新しい順）。種類と場所で絞れる。"""
    where, params = _docs_filter(kind, place_id)
    sql = _DOC_SELECT + where + " ORDER BY d.mtime DESC, d.id DESC LIMIT ?"
    home, labels = _home(home), _labels(conn)
    items = [_doc_item(r, home, labels) for r in _rows(conn, sql, params + [max(0, limit)])]
    _add_tasks(conn, items)
    return items


def count_documents(conn: sqlite3.Connection, *, kind: Optional[str] = None, place_id: Optional[int] = None) -> int:
    """documents_list と同じ条件に合う資料の数（一覧を上限で切ったときに、画面に出す）。"""
    where, params = _docs_filter(kind, place_id)
    return conn.execute("SELECT COUNT(*) FROM documents d" + where, params).fetchone()[0]


def document_detail(conn: sqlite3.Connection, doc_id: int, *, home: str,
                    live: Optional[Dict[str, LiveSession]] = None) -> Optional[DocumentDetail]:
    found = _rows(conn, "SELECT " + _DOC_COLUMNS + ", d.body" + _DOC_FROM + " WHERE d.id = ?", (doc_id,))
    if not found:
        return None
    r = found[0]
    item = _doc_item(r, home, _labels(conn))
    _add_tasks(conn, [item])
    body = r["body"] or ""
    ext = (item.ext or "").lower()
    if ext in _MARKDOWN_EXTS:
        content = render_markdown(body)  # type: Optional[str]
    elif ext in _PLAIN_EXTS:
        content = render_pre(body)
    else:
        content = None  # HTML は隔離した枠で、PDF と Office は外のアプリで開く
    return DocumentDetail(item=item, title=r["title"], html=content, links=_doc_links(conn, item.path, live or {}))


def document_kinds(conn: sqlite3.Connection, *, place_id: Optional[int] = None) -> Dict[str, int]:
    """資料の種類ごとの数（資料の絞り込み）。place_id を指定すれば、その場所の資料だけを数える。"""
    sql, params = "SELECT kind, COUNT(*) FROM documents", []  # type: str, List[Any]
    if place_id is not None:
        sql, params = sql + " WHERE place_id = ?", [place_id]
    return {kind: n for kind, n in conn.execute(sql + " GROUP BY kind", params)}


def ledger_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """台帳にある対話・ヘッドレスのセッションと資料の数（画面の状態の表示。空のセッションは数えない）。"""
    kinds = {kind: n for kind, n in conn.execute(
        "SELECT kind, COUNT(*) FROM sessions WHERE kind IN ('interactive', 'headless') GROUP BY kind")}
    return {"interactive": kinds.get("interactive", 0), "headless": kinds.get("headless", 0),
            "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]}


def doc_items(conn: sqlite3.Connection, ids: Sequence[int], *, home: Optional[str] = None) -> Dict[int, DocItem]:
    """資料の ID ごとの一覧の項目（検索の一致に名前や場所を添えるため）。台帳にない ID は含めない。"""
    home, labels = _home(home), _labels(conn)
    found = {}  # type: Dict[int, DocItem]
    for chunk in _chunks(ids):
        for r in _rows(conn, _DOC_SELECT + " WHERE d.id IN (%s)" % _marks(len(chunk)), chunk):
            found[r["id"]] = _doc_item(r, home, labels)
    _add_tasks(conn, list(found.values()))
    return found


# 会話

def conversation_window(conn: sqlite3.Connection, session_id: str, *, around_turn: Optional[int] = None,
                        radius: int = CONVERSATION_TURNS, turns: Optional[Tuple[int, int]] = None,
                        item_offset: int = 0) -> Optional[ConversationWindow]:
    """会話の一部（窓）と、その前後に残っている分の数。対話は台帳の発言から、turns（最初と最後のターン）か、
    around_turn（既定は最後のターン）の前後 radius ターンを返す。ヘッドレスは記録から、item_offset 番目の項目から
    TRANSCRIPT_ITEMS 項目を返す。本文がなければ source="none"。"""
    found = conn.execute("SELECT kind, turn, transcript_path, transcript_exists, cwd FROM sessions WHERE id = ?",
                         (session_id,)).fetchone()
    if found is None:
        return None
    kind, total, path, exists, cwd = found
    if kind == "interactive" and conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)).fetchone():
        if turns is not None:
            first, last = max(turns[0], 0), min(turns[1], total)
        else:
            around = total if around_turn is None else min(max(around_turn, 0), total)
            first, last = max(around - max(radius, 0), 0), min(around + max(radius, 0), total)
        rows = conn.execute("SELECT turn, ts, role, text FROM messages WHERE session_id = ? AND turn BETWEEN ? AND ? "
                            "ORDER BY seq", (session_id, first, last))
        items = _ledger_items(rows)
        # 残りは発言のあるターンで数える（ターン 0 は、最初の入力の前に発言があるときだけ）
        before, after = conn.execute(
            "SELECT COUNT(DISTINCT CASE WHEN turn < ? THEN turn END), COUNT(DISTINCT CASE WHEN turn > ? THEN turn END) "
            "FROM messages WHERE session_id = ?", (first, last, session_id)).fetchone()
        return ConversationWindow(session_id, items, first, last, total, "ledger", before=before, after=after)
    if kind == "headless" and exists:
        offset = max(item_offset, 0)
        found_items = _transcript_items(path, cwd, offset)
        if found_items is not None:
            items, count = found_items
            return ConversationWindow(session_id, items, 0, 0, total, "transcript", before=min(offset, count),
                                      after=max(count - offset - len(items), 0), offset=offset)
    return ConversationWindow(session_id, [], 0, 0, total, "none")


def turn_excerpt(conn: sqlite3.Connection, session_id: str, turn: int) -> List[ConvItem]:
    """1つのターンの発言（本人の入力・Claude の応答・まとめたツール）。検索の一致箇所を状況カードに出すため。
    台帳に発言がなければ空。"""
    rows = conn.execute("SELECT turn, ts, role, text FROM messages WHERE session_id = ? AND turn = ? ORDER BY seq",
                        (session_id, turn))
    return _ledger_items(rows)


# 内部

def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _marks(n: int) -> str:
    return ", ".join("?" * n)


def _chunks(items: Sequence[Any]) -> List[List[Any]]:
    items = list(items)
    return [items[k:k + _CHUNK] for k in range(0, len(items), _CHUNK)]


def _home(home: Optional[str]) -> str:
    return home if home is not None else os.path.expanduser("~")


def _labels(conn: sqlite3.Connection) -> Dict[str, str]:
    """表示名ごとの場所の短い名前。入力履歴にしかない場所も含め、台帳のすべての場所の中で作る。"""
    return short_labels(display for (display,) in conn.execute("SELECT display FROM places"))


def _label(labels: Dict[str, str], display: Optional[str]) -> str:
    return labels.get(display or "", display or "")


def _list_filter(place_id: Optional[int], days: Optional[float], include_headless: bool,
                 now: Callable[[], float]) -> Tuple[str, List[Any]]:
    """一覧の条件（WHERE の中身と値）。list_sessions と count_sessions で同じものを使う。"""
    kinds = ["interactive", "headless"] if include_headless else ["interactive"]
    where, params = ["s.kind IN (%s)" % _marks(len(kinds))], list(kinds)  # type: List[str], List[Any]
    if place_id is not None:
        where.append("s.place_id = ?")
        params.append(place_id)
    since = _since(days, now)
    if since is not None:
        where.append("s.ended_at >= ?")
        params.append(since)
    return " AND ".join(where), params


def _docs_filter(kind: Optional[str], place_id: Optional[int]) -> Tuple[str, List[Any]]:
    """資料の一覧の条件（" WHERE …" か空と、値）。documents_list と count_documents で同じものを使う。"""
    where, params = [], []  # type: List[str], List[Any]
    if kind is not None:
        where.append("d.kind = ?")
        params.append(kind)
    if place_id is not None:
        where.append("d.place_id = ?")
        params.append(place_id)
    return (" WHERE " + " AND ".join(where) if where else ""), params


def _since(days: Optional[float], now: Callable[[], float]) -> Optional[str]:
    """期間の始まり（台帳の時刻の形）。表せないほど長い期間は、絞らないのと同じにする。"""
    if days is None:
        return None
    try:
        return iso_utc(now() - max(days, 0) * DAY_SECONDS)
    except (OverflowError, OSError, ValueError):
        return None


def _first_prompt(r: Dict[str, Any]) -> Optional[str]:
    # ヘッドレスは本人の入力がないので、要約（digests）の最初の依頼を使う
    return r["first_prompt"] or r["digest_first"]


def _title(r: Dict[str, Any]) -> str:
    return choose_title(r["custom_title"], r["agent_name"], r["ai_title"], _first_prompt(r))


def _row_values(r: Dict[str, Any], prs: List[str], live: Dict[str, LiveSession],
                labels: Dict[str, str]) -> Dict[str, Any]:
    if r["recap"] and r["recap"].strip():
        summary, is_recap = r["recap"], True
    else:
        summary, is_recap = r["last_prompt"] or r["digest_first"] or r["last_reply"] or "", False
    return dict(id=r["id"], title=_title(r), kind=r["kind"], place_id=r["place_id"],
                place_display=r["place_display"], ended_at=r["ended_at"], n_prompts=r["n_prompts"],
                n_children=r["n_children"], prs=prs, summary=summary, summary_is_recap=is_recap,
                live=r["id"] in live, transcript_exists=bool(r["transcript_exists"]),
                place_label=_label(labels, r["place_display"]))


def _prs(conn: sqlite3.Connection, ids: Sequence[str]) -> Dict[str, List[str]]:
    found = {}  # type: Dict[str, List[str]]
    for chunk in _chunks(ids):
        for session_id, url in conn.execute(
                "SELECT session_id, url FROM session_prs WHERE session_id IN (%s)" % _marks(len(chunk)), chunk):
            found.setdefault(session_id, []).append(url)
    for urls in found.values():
        urls.sort()
    return found


def _session_rows(conn: sqlite3.Connection, ids: Sequence[str], live: Dict[str, LiveSession]) -> Dict[str, SessionRow]:
    rows = {}  # type: Dict[str, Dict[str, Any]]
    for chunk in _chunks(ids):
        for r in _rows(conn, _SESSION_SELECT + " WHERE s.id IN (%s)" % _marks(len(chunk)), chunk):
            rows[r["id"]] = r
    prs, labels = _prs(conn, list(rows)), _labels(conn)
    return {sid: SessionRow(**_row_values(r, prs.get(sid, []), live, labels)) for sid, r in rows.items()}


def _prompts_after(conn: sqlite3.Connection, session_id: str, recap: Optional[str], recap_at: Optional[str]) -> int:
    if not (recap and recap.strip()) or not recap_at:
        return 0
    return conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user' AND ts > ?",
                        (session_id, recap_at)).fetchone()[0]


def _strongest(rows: Iterable[Tuple[str, str, Optional[str]]]) -> List[Tuple[str, str, Optional[str]]]:
    """(鍵, 関わり, 時刻) を鍵ごとにまとめ、(鍵, 最も強い関わり, 最も新しい時刻) を新しい順に返す
    （時刻のないものは最後、同じ時刻は鍵の順）。"""
    best = {}  # type: Dict[str, Tuple[str, Optional[str]]]
    weakest = len(_REL_RANK)
    for key, rel, ts in rows:
        if key not in best:
            best[key] = (rel, ts)
            continue
        old_rel, old_ts = best[key]
        if _REL_RANK.get(rel, weakest) < _REL_RANK.get(old_rel, weakest):
            old_rel = rel
        if ts is not None and (old_ts is None or ts > old_ts):
            old_ts = ts
        best[key] = (old_rel, old_ts)
    out = sorted((key, rel, ts) for key, (rel, ts) in best.items())
    out.sort(key=lambda x: x[2] or "", reverse=True)
    return out


def _card_docs(conn: sqlite3.Connection, session_id: str, cwd: Optional[str],
               home: str) -> Tuple[List[DocLinkView], int]:
    linked = _strongest(conn.execute("SELECT path, rel, ts FROM doc_links WHERE session_id = ? "
        "AND rel IN ('created', 'edited', 'named', 'read')", (session_id,)))
    docs = []
    for path, rel, _ in linked[:CARD_DOCS]:
        found = conn.execute("SELECT id, kind, mtime, size FROM documents WHERE path = ?", (path,)).fetchone()
        if found is not None:
            doc_id, kind, mtime, size = found
        else:  # 台帳の資料にない（消えた・扱わない場所の）パス
            doc_id, kind, mtime, size = None, classify_kind(path, home), None, None
        docs.append(DocLinkView(doc_id, path, _display_path(path, cwd, home), kind, rel, mtime, size))
    return docs, len(linked)


def _relations(conn: sqlite3.Connection, session_id: str, place_id: Optional[int],
               labels: Dict[str, str]) -> List[RelationView]:
    """入ってくるつながりすべてと、出ていくつながり（子を除く。子は数だけ出す）。相手の開始の順。"""
    rows = _rows(
        conn,
        "SELECT r.src, r.dst, r.type, r.confidence, r.evidence, s.id AS peer_id, s.custom_title, s.agent_name, "
        "s.ai_title, s.first_prompt, d.first_prompt AS digest_first, s.place_id, "
        "COALESCE(p.display, '') AS place_display, s.started_at, s.ended_at FROM relations r "
        "JOIN sessions s ON s.id = CASE WHEN r.dst = ? THEN r.src ELSE r.dst END "
        "LEFT JOIN places p ON p.id = s.place_id LEFT JOIN digests d ON d.session_id = s.id "
        "WHERE r.dst = ? OR (r.src = ? AND r.type != 'child') "
        "ORDER BY s.started_at IS NULL, s.started_at, s.id", (session_id, session_id, session_id))
    out = []
    for r in rows:
        label = RELATION_LABELS.get((r["type"], "in" if r["dst"] == session_id else "out"), r["type"])
        if r["confidence"] == "weak":
            label += WEAK_SUFFIX
        cross = place_id is not None and r["place_id"] is not None and r["place_id"] != place_id
        out.append(RelationView(type=r["type"], confidence=r["confidence"], label=label, peer_id=r["peer_id"],
                                peer_title=_title(r), peer_place=r["place_display"],
                                peer_started_at=r["started_at"], peer_ended_at=r["ended_at"], cross_place=cross,
                                evidence=r["evidence"], peer_place_label=_label(labels, r["place_display"])))
    return out


def _child_rows(conn: sqlite3.Connection, sql: str, params: List[Any], offset: int, limit: int) -> List[ChildRow]:
    rows = conn.execute(sql + _NEWEST_FIRST, params + [max(0, limit), max(0, offset)])
    return [ChildRow(sid, started, _first_line(first or digest_first)) for sid, started, first, digest_first in rows]


def _first_line(text: Optional[str]) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:FIRST_LINE_CHARS]
    return ""


def _place_items(conn: sqlite3.Connection, place_id: Optional[int], labels: Dict[str, str]) -> List[PlaceItem]:
    """場所ごとの数（place_id を指定すればその場所だけ）。last_at は空でないセッションの最後の動き。"""
    cond, params = ("place_id = ?", [place_id]) if place_id is not None else ("place_id IS NOT NULL", [])
    sessions = {}  # type: Dict[int, Dict[str, Tuple[int, Optional[str]]]]
    for pid, kind, n, last in conn.execute(
            "SELECT place_id, kind, COUNT(*), MAX(ended_at) FROM sessions "
            "WHERE kind IN ('interactive', 'headless') AND %s GROUP BY place_id, kind" % cond, params):
        sessions.setdefault(pid, {})[kind] = (n, last)
    docs = {pid: n for pid, n in conn.execute(
        "SELECT place_id, COUNT(*) FROM documents WHERE %s GROUP BY place_id" % cond, params)}
    items = []
    for pid, display in conn.execute(
            "SELECT id, display FROM places" + (" WHERE id = ?" if place_id is not None else ""), params):
        counts = sessions.get(pid, {})
        lasts = [last for _, last in counts.values() if last]
        items.append(PlaceItem(id=pid, display=display, n_interactive=counts.get("interactive", (0, None))[0],
                               n_headless=counts.get("headless", (0, None))[0], n_docs=docs.get(pid, 0),
                               last_at=max(lasts) if lasts else None, label=_label(labels, display)))
    return items


def _timeline(conn: sqlite3.Connection, place_id: int, labels: Dict[str, str]) -> List[TimelineItem]:
    """その場所の最近始まった対話（古い順）と、そこから別の場所へ出ていくつながりの相手（開始の順）。
    場所のない相手は、場所の名前を出せないので足さない。矢印には、優先順位の最も高いつながりの根拠を添える。"""
    rows = _rows(conn, _TIMELINE_SELECT + "s.place_id = ? "
                 "ORDER BY s.started_at IS NULL, s.started_at DESC, s.id DESC LIMIT ?", (place_id, TIMELINE_LIMIT))
    rows.reverse()
    order = {r["id"]: k for k, r in enumerate(rows)}
    rank = {key: k for k, key in enumerate(PRECEDENCE)}
    # 相手 → (優先順位, 種類, 根拠)。via は時系列の前の項目から入ってくるもの、leaving は時系列の外へ出ていくもの
    via, leaving = {}, {}  # type: Dict[str, Tuple[int, str, str]], Dict[str, Tuple[int, str, str]]
    for chunk in _chunks(list(order)):
        for src, dst, typ, confidence, evidence in conn.execute(
                "SELECT src, dst, type, confidence, evidence FROM relations WHERE src IN (%s)" % _marks(len(chunk)),
                chunk):
            if dst not in order:
                found = leaving
            elif order[src] < order[dst]:
                found = via
            else:
                continue
            k = rank.get((typ, confidence), len(rank))
            if dst not in found or k < found[dst][0]:
                found[dst] = (k, typ, evidence)
    items = []  # type: List[TimelineItem]
    for r in rows:
        label = TIMELINE_LABELS.get(via[r["id"]][1]) if r["id"] in via else None
        items.append(_timeline_item(r, label, via[r["id"]][2] if label else None, None, None))
    moved = []  # type: List[Dict[str, Any]]
    for chunk in _chunks(sorted(leaving)):
        moved += _rows(conn, _TIMELINE_SELECT + "s.id IN (%s) AND s.place_id IS NOT NULL AND s.place_id != ?"
                       % _marks(len(chunk)), chunk + [place_id])
    moved.sort(key=lambda r: (r["started_at"] is None, r["started_at"] or "", r["id"]))
    return items + [_timeline_item(r, MOVED_LABEL, leaving[r["id"]][2], r["place_display"],
                                   _label(labels, r["place_display"])) for r in moved]


def _timeline_item(r: Dict[str, Any], via_label: Optional[str], via_evidence: Optional[str],
                   other_place: Optional[str], other_place_label: Optional[str]) -> TimelineItem:
    return TimelineItem(session_id=r["id"], title=_title(r), started_at=r["started_at"], ended_at=r["ended_at"],
                        n_prompts=r["n_prompts"], n_children=r["n_children"], via_label=via_label,
                        other_place=other_place, via_evidence=via_evidence, other_place_label=other_place_label)


def _folders(conn: sqlite3.Connection, place_id: int, home: str,
             labels: Dict[str, str]) -> List[Tuple[str, List[DocItem]]]:
    groups = {}  # type: Dict[str, List[DocItem]]
    for r in _rows(conn, _DOC_SELECT + " WHERE d.place_id = ? ORDER BY d.mtime DESC, d.id DESC", (place_id,)):
        item = _doc_item(r, home, labels)
        groups.setdefault(item.folder, []).append(item)
    # 資料を新しい順に読んだので、フォルダーは最も新しい資料の新しい順に並んでいる
    return [(folder, items[:FOLDER_DOCS]) for folder, items in list(groups.items())[:FOLDER_LIMIT]]


def _session_tasks(conn: sqlite3.Connection, session_id: str) -> List[Tuple[str, str, str, str]]:
    from tsuzuki.taskviews import session_tasks  # taskviews が views を使うので、使う時に読む
    return session_tasks(conn, session_id)


def _add_tasks(conn: sqlite3.Connection, items: Sequence[DocItem]) -> None:
    """タスクのフォルダーの中の資料に、タスクの（名前、題）を付ける（入れ子なら深いフォルダーを選ぶ）。
    フォルダーの直下の task.md には、表紙の印（task_cover）も付ける。"""
    if not items:
        return
    folders = [(path, name, title) for name, title, path in conn.execute("SELECT name, title, path FROM tasks")]
    folders.sort(key=lambda f: -len(f[0]))
    for item in items:
        found = next(((path, name, title) for path, name, title in folders if within(item.path, path)), None)
        item.task = (found[1], found[2]) if found else None
        item.task_cover = found is not None and item.path == os.path.join(found[0], TASK_MD)


def _doc_item(r: Dict[str, Any], home: str, labels: Dict[str, str]) -> DocItem:
    path = r["path"]
    return DocItem(id=r["id"], path=path, name=os.path.basename(path), kind=r["kind"],
                   place_display=r["place_display"], folder=_folder(path, r["place_path"], home),
                   mtime=r["mtime"], size=r["size"], ext=r["ext"], place_label=_label(labels, r["place_display"]))


def _doc_links(conn: sqlite3.Connection, path: str, live: Dict[str, LiveSession]) -> List[Tuple[str, SessionRow]]:
    """その資料に関わった空でないセッション。1つのセッションは最も強い関わりで1回、新しい順。"""
    linked = _strongest(conn.execute(
        "SELECT l.session_id, l.rel, l.ts FROM doc_links l JOIN sessions s ON s.id = l.session_id "
        "WHERE l.path = ? AND s.kind != 'empty' AND l.rel IN ('created', 'edited', 'named', 'read')", (path,)))
    rows = _session_rows(conn, [session_id for session_id, _, _ in linked], live)
    return [(rel, rows[session_id]) for session_id, rel, _ in linked if session_id in rows]


def _under(path: str, root: Optional[str]) -> Optional[str]:
    """path が root の下にあれば root からの相対パス。"""
    if not root:
        return None
    prefix = root.rstrip("/") + "/"
    if prefix == "/" or not path.startswith(prefix):
        return None
    return path[len(prefix):] or None


def _tilde(path: str, home: str) -> str:
    h = home.rstrip("/")
    if h and (path == h or path.startswith(h + "/")):
        return "~" + path[len(h):]
    return path


def _display_path(path: str, cwd: Optional[str], home: str) -> str:
    return _under(path, cwd) or _tilde(path, home)


def _folder(path: str, place_path: Optional[str], home: str) -> str:
    """場所のパスからの相対のディレクトリ（場所の直下は "."）。場所の外か場所がなければ、ホームを ~ にした絶対パス。"""
    directory = os.path.dirname(path)
    if place_path and os.path.isabs(place_path):
        if directory == place_path.rstrip("/"):
            return "."
        rel = _under(directory, place_path)
        if rel:
            return rel
    return _tilde(directory, home)


def _ledger_items(rows: Iterable[Tuple[int, Optional[str], str, str]]) -> List[ConvItem]:
    items = []  # type: List[ConvItem]
    for turn, ts, role, text in rows:
        if role != "tool":
            items.append(ConvItem(role, ts, text, turn, []))
        elif items and items[-1].kind == "tools":
            items[-1].tools.append(text)
        else:
            items.append(ConvItem("tools", ts, "", turn, [text]))
    return items


def _transcript_items(path: str, cwd: Optional[str], offset: int = 0) -> Optional[Tuple[List[ConvItem], int]]:
    """ヘッドレスの記録から会話の項目を作り、offset 番目から TRANSCRIPT_ITEMS 項目と、項目の総数を返す
    （続けて呼んだツールは1つの項目にまとめる）。読めなければ None。"""
    items = []  # type: List[ConvItem]
    count, last_kind = 0, None  # これまでに作った項目の数と、直前の項目の種類
    end = offset + TRANSCRIPT_ITEMS
    try:
        for line in iter_lines(Path(path), 0):
            if line.record is None:
                continue
            try:
                events = extract_events(line.record)
            except Exception:
                continue  # 形の崩れた記録は飛ばす
            for e in events:
                if isinstance(e, ToolUse):
                    summary = tool_summary(e.name, e.input, cwd)
                    if last_kind == "tools":
                        if offset <= count - 1 < end:  # 直前の項目（窓の中なら items の最後）に足す
                            items[-1].tools.append(summary)
                        continue
                    item = ConvItem("tools", e.ts, "", 0, [summary])
                elif isinstance(e, UserPrompt):
                    item = ConvItem("user", e.ts, e.text, 0, [])
                elif isinstance(e, AssistantText):
                    item = ConvItem("assistant", e.ts, e.text, 0, [])
                else:
                    continue
                if offset <= count < end:
                    items.append(item)
                count, last_kind = count + 1, item.kind
    except OSError:
        return (items, count) if count else None
    return items, count
