"""タスクの一覧とカードのデータ。

台帳の行から作る（HTML は作らない）。計算に使う結び付けは確定と推定（強）だけで、
推定（弱）はカードのセッションの一覧にだけ出す。task list は台帳を使わず task.md から作る。
"""
import calendar
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from tsuzuki import tasks as T
from tsuzuki.documents import KIND_LABELS
from tsuzuki.sources.live import LiveSession
from tsuzuki.tasklinks import CONFIDENCE_LABELS, RULE_LABELS
from tsuzuki.views import SessionRow, place_labels, session_rows

STATUS_LABELS = {"active": "進行中", "waiting": "待ち", "done": "完了"}
TASK_FILES_LIMIT = 300
DOC_BUNDLE_LIMIT = 50  # カードの共通の資料とほかの場所の資料に、それぞれ新しい順に並べる数
EXCERPT_CHARS = 80
# task.md の updated は分までなので、書いた分の終わり（＋60秒）までを「書く前」とみなす（引き継ぎの依頼を「その後」にしない）
UPDATED_PRECISION = 60.0
NO_PLACE = "場所なし"
_TRUSTED = ("confirmed", "strong")
_KIND_ORDER = ("research", "design", "plan", "response", "notes")
_REL_LABELS = {"created": "作った", "edited": "直した", "read": "読んだ", "named": "名指し"}
_REL_RANK = {"created": 0, "edited": 1, "read": 2, "named": 3}
_PR_NUMBER = re.compile(r"/(\d+)/?$")
_BULLET_PREFIX = re.compile(r"^[-*+]\s+")


@dataclass
class TaskRow:
    name: str
    title: str
    ticket: Optional[str]
    stage: str
    stage_label: str
    stage_from_pr: bool
    status: str
    place_id: Optional[int]
    place_label: str
    last_at: Optional[str]
    n_sessions: int
    live: bool
    pr_numbers: List[str]
    line2_label: str
    line2: str


@dataclass
class TaskFileItem:
    path: str
    rel_path: str
    kind: Optional[str]
    kind_label: str
    title: str
    excerpt: str
    relation: Optional[str]
    doc_id: Optional[int]
    missing: bool
    copied: bool = False  # ほかの場所の資料：タスクのフォルダーに同じ名前・同じ大きさのファイルがある


@dataclass
class TaskSessionItem:
    row: SessionRow
    rule_label: str
    confidence: str
    confidence_label: str
    n_children: int


@dataclass
class TaskDetail:
    row: TaskRow
    path: str
    task_md: str
    md_mtime: float
    md_size: int
    task_md_doc_id: Optional[int]
    created: Optional[str]
    updated_at: Optional[str]
    sections: Dict[str, str]
    after: Optional[Tuple[str, str]]
    warnings: List[str]
    ticket_url: Optional[str]
    branch: Optional[str]
    prs: List[str]
    folder_files: List[TaskFileItem]
    folder_cut: bool
    shared_docs: List[TaskFileItem]
    other_docs: List[TaskFileItem]
    sessions: List[TaskSessionItem]
    resume_session: Optional[str]
    repo: Optional[str]
    shared_total: int = 0  # 共通の資料の全部の数（shared_docs は DOC_BUNDLE_LIMIT まで）
    other_total: int = 0  # ほかの場所の資料の全部の数（other_docs は DOC_BUNDLE_LIMIT まで）


# 時刻

def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def _local_epoch(local: Optional[str]) -> Optional[float]:
    """task.md の updated（地方時、時差なし）を UTC の秒にする。"""
    if not local:
        return None
    try:
        return time.mktime(time.strptime(local, "%Y-%m-%dT%H:%M"))
    except (ValueError, OverflowError):
        return None


# 一覧

def list_tasks(conn: sqlite3.Connection, *, status: str = "open", place_id: Optional[int] = None,
               live: Dict[str, LiveSession]) -> List[TaskRow]:
    rows = _task_rows(conn, live)
    if status == "open":
        rows = [r for r in rows if r.status != "done"]
    elif status == "done":
        rows = [r for r in rows if r.status == "done"]
    if place_id is not None:
        rows = [r for r in rows if r.place_id == place_id]
    return rows


def task_counts(conn: sqlite3.Connection) -> Tuple[Dict[str, int], List[Tuple[Optional[int], str, int]]]:
    rows = _task_rows(conn, {})
    n_done = sum(1 for r in rows if r.status == "done")
    counts = {"open": len(rows) - n_done, "done": n_done, "all": len(rows)}
    per_place: Dict[Optional[int], Tuple[str, int]] = {}
    for r in rows:
        if r.status != "done":
            per_place[r.place_id] = (r.place_label, per_place.get(r.place_id, ("", 0))[1] + 1)
    places = [(pid, label, n) for pid, (label, n) in per_place.items()]
    places.sort(key=lambda p: (p[0] is None, -p[2], p[1]))
    return counts, places


def open_task_choices(conn: sqlite3.Connection, session_id: Optional[str] = None) -> List[Tuple[str, str]]:
    """「タスクに足す」の選択肢（進行中と待ちのタスク）。session_id があれば、そのセッションがまだ結び付いて
    いないタスクだけ。"""
    sql, params = "SELECT name, title FROM tasks WHERE stage != '完了'", ()  # type: Tuple[str, Tuple]
    if session_id is not None:
        sql, params = sql + " AND name NOT IN (SELECT task FROM task_sessions WHERE session_id = ?)", (session_id,)
    return [(r["name"], r["title"]) for r in conn.execute(sql + " ORDER BY md_mtime DESC, name", params)]


def session_tasks(conn: sqlite3.Connection, session_id: str) -> List[Tuple[str, str, str, str]]:
    found = conn.execute(
        "SELECT t.name, t.title, ts.rule, ts.confidence FROM task_sessions ts JOIN tasks t ON t.name = ts.task "
        "WHERE ts.session_id = ? ORDER BY t.md_mtime DESC, t.name", (session_id,))
    return [(name, title, RULE_LABELS.get(rule, rule), CONFIDENCE_LABELS.get(conf, conf))
            for name, title, rule, conf in found]


def _trusted_sessions(conn: sqlite3.Connection, task: Optional[str] = None) -> Dict[str, List[sqlite3.Row]]:
    """タスクごとの、確定と推定（強）で結び付いた空でないセッション（task_sessions と sessions の結合）。"""
    sql = ("SELECT ts.task, s.id, s.kind, s.started_at, s.ended_at, s.recap, s.recap_at, s.last_prompt, s.branch "
           "FROM task_sessions ts "
           "JOIN sessions s ON s.id = ts.session_id WHERE ts.confidence IN ('confirmed', 'strong') "
           "AND s.kind != 'empty'")
    params: Tuple = ()
    if task is not None:
        sql, params = sql + " AND ts.task = ?", (task,)
    out: Dict[str, List[sqlite3.Row]] = {}
    for r in conn.execute(sql, params):
        out.setdefault(r["task"], []).append(r)
    return out


def _newest(sessions: Sequence[sqlite3.Row]) -> List[sqlite3.Row]:
    return sorted(sessions, key=lambda s: (_epoch(s["ended_at"]) or 0.0, s["id"]), reverse=True)


def _pr_urls(conn: sqlite3.Connection, ids: Sequence[str]) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    for i in range(0, len(ids), 500):
        chunk = list(ids[i:i + 500])
        for sid, url in conn.execute("SELECT session_id, url FROM session_prs WHERE session_id IN (%s)"
                                     % ",".join("?" * len(chunk)), chunk):
            found.setdefault(sid, []).append(url)
    return found


def _pr_numbers(urls: Sequence[str]) -> List[str]:
    out: List[str] = []
    for url in sorted(set(urls)):
        m = _PR_NUMBER.search(url)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _first_line(text: Optional[str], *, strip_bullet: bool) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if strip_bullet:
            line = _BULLET_PREFIX.sub("", line).strip()
        if line:
            return line
    return ""


def _task_row(r: sqlite3.Row, sessions: List[sqlite3.Row], prs: List[str], place: Optional[Tuple[int, str]],
              labels: Dict[int, str], live: Dict[str, LiveSession]) -> TaskRow:
    stage = r["stage"]
    from_pr = bool(prs) and stage in T.STAGES and T.STAGES.index(stage) < T.STAGES.index("PR")
    if from_pr:
        stage = "PR"
    status = "done" if r["stage"] == "完了" else ("waiting" if r["waiting"] else "active")
    ordered = _newest(sessions)
    candidates = [r["md_mtime"], r["files_mtime"]] + [_epoch(s["ended_at"]) for s in ordered]
    last = max((c for c in candidates if c), default=0.0)
    sections = json.loads(r["sections"] or "{}")
    if status == "waiting" and not T.no_waiting(sections.get("待っていること")):
        label, line2 = "待っていること", _first_line(sections["待っていること"], strip_bullet=False)
    elif status != "waiting" and sections.get("次の一手", "").strip():
        label, line2 = "次の一手", _first_line(sections["次の一手"], strip_bullet=True)
    else:
        label, line2 = "", ""
    if not line2:
        recap = next((s["recap"].strip() for s in ordered if s["recap"] and s["recap"].strip()), "")
        label, line2 = ("要約", recap) if recap else ("", "")
    return TaskRow(
        name=r["name"], title=r["title"], ticket=r["ticket"], stage=stage, stage_label=T.stage_label(stage),
        stage_from_pr=from_pr, status=status, place_id=place[0] if place else None,
        place_label=labels.get(place[0], NO_PLACE) if place else NO_PLACE, last_at=_iso(last) if last else None,
        n_sessions=sum(1 for s in sessions if s["kind"] == "interactive"),
        live=any(s["id"] in live for s in sessions), pr_numbers=_pr_numbers(prs), line2_label=label, line2=line2)


def _places_by_path(conn: sqlite3.Connection, paths: Sequence[str]) -> Dict[str, Tuple[int, str]]:
    found: Dict[str, Tuple[int, str]] = {}
    for i in range(0, len(paths), 500):
        chunk = list(paths[i:i + 500])
        for pid, path in conn.execute("SELECT id, path FROM places WHERE path IN (%s)" % ",".join("?" * len(chunk)),
                                      chunk):
            found[path] = (pid, path)
    return found


def _task_rows(conn: sqlite3.Connection, live: Dict[str, LiveSession],
               only: Optional[str] = None) -> List[TaskRow]:
    where, params = (" WHERE name = ?", (only,)) if only else ("", ())
    tasks = conn.execute("SELECT * FROM tasks" + where, params).fetchall()
    linked = _trusted_sessions(conn, only)
    prs = _pr_urls(conn, [s["id"] for ss in linked.values() for s in ss])
    repos = {r["name"]: r["repo_resolved"] or r["repo"] for r in tasks}
    places = _places_by_path(conn, sorted({p for p in repos.values() if p}))
    labels = place_labels(conn, [pid for pid, _ in places.values()])
    rows = [_task_row(r, linked.get(r["name"], []), [u for s in linked.get(r["name"], []) for u in prs.get(s["id"], [])],
                      places.get(repos[r["name"]] or ""), labels, live) for r in tasks]
    rows.sort(key=lambda r: (r.last_at or "", r.name), reverse=True)
    return rows


# チケットとリポジトリ

def ticket_url(ticket: Optional[str], *, repo: Optional[str], template: Optional[str]) -> Optional[str]:
    first = (ticket or "").split(",")[0].strip()
    if not first:
        return None
    if "://" in first:
        return first
    numbers = T.ticket_numbers(first)
    if not numbers:
        return None
    if template:
        return template.replace("{n}", numbers[0])
    base = origin_web_base(repo) if repo else None
    return "%s/issues/%s" % (base, numbers[0]) if base else None


def _git_config_path(repo: str) -> Optional[str]:
    dot_git = os.path.join(repo, ".git")
    if os.path.isdir(dot_git):
        return os.path.join(dot_git, "config")
    try:
        with open(dot_git, encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = os.path.normpath(os.path.join(repo, first[len("gitdir:"):].strip()))
    return os.path.join(os.path.dirname(os.path.dirname(gitdir)), "config")  # <本体>/.git/worktrees/<名前> の2つ上


def _origin_url(config_path: str) -> Optional[str]:
    try:
        with open(config_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    in_origin = False
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_origin = re.fullmatch(r'\[remote\s+"origin"\]', line) is not None
        elif in_origin and re.match(r"url\s*=", line):
            return line.partition("=")[2].strip() or None
    return None


def origin_web_base(repo: str) -> Optional[str]:
    config_path = _git_config_path(repo)
    url = _origin_url(config_path) if config_path else None
    if not url:
        return None
    m = re.fullmatch(r"[\w.-]+@([^:/]+):(.+)", url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http", "ssh", "git") or not parsed.hostname:
            return None
        host = parsed.netloc.rpartition("@")[2] if parsed.scheme in ("https", "http") else parsed.hostname
        path = parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return "https://%s/%s" % (host, path) if path else None


# カード

def _docs_by_path(conn: sqlite3.Connection, paths: Sequence[str]) -> Dict[str, sqlite3.Row]:
    found: Dict[str, sqlite3.Row] = {}
    for i in range(0, len(paths), 500):
        chunk = list(paths[i:i + 500])
        for r in conn.execute("SELECT id, path, kind, title, body, mtime FROM documents WHERE path IN (%s)"
                              % ",".join("?" * len(chunk)), chunk):
            found[r["path"]] = r
    return found


def _excerpt(body: Optional[str]) -> str:
    lines = (body or "").splitlines()
    i = 0
    if lines and lines[0].strip() == "---":  # 先頭の見出し情報（front matter）は読み飛ばす
        end = next((n for n in range(1, len(lines)) if lines[n].strip() == "---"), None)
        if end is not None:
            i = end + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    para: List[str] = []
    while i < len(lines) and lines[i].strip():
        para.append(lines[i].strip())
        i += 1
    return " ".join(para)[:EXCERPT_CHARS]


def _file_item(path: str, rel_path: str, doc: Optional[sqlite3.Row], kind: Optional[str], relation: Optional[str],
               missing: bool) -> TaskFileItem:
    kind = kind or (doc["kind"] if doc else None) or "other"
    title = (doc["title"] if doc else None) or os.path.basename(path)
    return TaskFileItem(path=path, rel_path=rel_path, kind=kind, kind_label=KIND_LABELS.get(kind, kind),
                        title=title, excerpt=_excerpt(doc["body"]) if doc else "", relation=relation,
                        doc_id=doc["id"] if doc else None, missing=missing)


def _doc_relations(conn: sqlite3.Connection, ids: Sequence[str]) -> Dict[str, Tuple[str, str]]:
    """パスごとの（最も強い関わり、最も新しい時刻）。task_file は資料の関わりに数えない。"""
    found: Dict[str, Tuple[str, str]] = {}
    for i in range(0, len(ids), 500):
        chunk = list(ids[i:i + 500])
        for path, rel, ts in conn.execute(
                "SELECT path, rel, COALESCE(ts, '') FROM doc_links WHERE rel IN ('created', 'edited', 'named', 'read') "
                "AND session_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            old = found.get(path)
            found[path] = (rel if old is None or _REL_RANK[rel] < _REL_RANK[old[0]] else old[0],
                           max(ts, old[1]) if old else ts)
    return found


def _folder_files(conn: sqlite3.Connection, task_dir: str, root: str, exclude_dirs: Sequence[str], max_depth: int,
                  relations: Dict[str, Tuple[str, str]], named: Set[str]) -> Tuple[List[TaskFileItem], bool]:
    paths, cut = T.list_task_files(task_dir, exclude_dirs=exclude_dirs, max_depth=max_depth,
                                   max_files=TASK_FILES_LIMIT)
    docs = _docs_by_path(conn, paths)
    items: List[Tuple[Tuple, TaskFileItem]] = []
    for p in paths:
        kind = T.task_doc_kind(p, root) or (docs[p]["kind"] if p in docs else None) or "other"
        rel = relations.get(p, ("named", "") if p in named else (None, ""))[0]
        try:
            mtime = os.stat(p).st_mtime
        except OSError:
            mtime = 0.0
        order = (0 if os.path.basename(p) == T.TASK_MD and os.path.dirname(p) == task_dir else 1,
                 _KIND_ORDER.index(kind) if kind in _KIND_ORDER else len(_KIND_ORDER), -mtime, p)
        items.append((order, _file_item(p, os.path.relpath(p, task_dir), docs.get(p), kind,
                                        _REL_LABELS.get(rel) if rel else None, False)))
    items.sort(key=lambda x: x[0])
    return [item for _, item in items], cut


def _copied(path: str, task_dir: str) -> bool:
    """タスクのフォルダーの直下に、同じ名前で同じ大きさの普通のファイルがあるか（コピー済みの目安）。"""
    mine = os.path.join(task_dir, os.path.basename(path))
    try:
        return os.path.isfile(mine) and os.path.getsize(mine) == os.path.getsize(path)
    except OSError:
        return False


def _doc_bundles(conn: sqlite3.Connection, task_dir: str, root: str, relations: Dict[str, Tuple[str, str]],
                 named: Set[str]) -> Tuple[List[TaskFileItem], List[TaskFileItem]]:
    """共通の資料とほかの場所の資料。どちらも新しい順で、全部を返す（カードに並べるのは呼ぶ側が切る）。"""
    shared_dir = os.path.join(root, T.SHARED_DIR)
    candidates = {p for p in list(relations) + sorted(named) if not T.within(p, task_dir)}
    docs = _docs_by_path(conn, sorted(candidates))
    shared: List[Tuple[str, TaskFileItem]] = []
    other: List[Tuple[str, TaskFileItem]] = []
    for p in candidates:
        rel, ts = relations.get(p, ("named", ""))
        if p not in docs and p not in named:
            continue  # ほかの場所の資料は、資料とみなされたものだけ（名指しは別）
        missing = p not in docs and not os.path.exists(p)
        item = _file_item(p, p, docs.get(p), None, _REL_LABELS[rel], missing)
        if T.within(p, shared_dir):
            shared.append((ts, item))
        else:
            item.copied = _copied(p, task_dir)
            other.append((ts, item))
    for bundle in (shared, other):
        bundle.sort(key=lambda x: x[1].path)
        bundle.sort(key=lambda x: x[0], reverse=True)
    return [i for _, i in shared], [i for _, i in other]


def _after(conn: sqlite3.Connection, newest: Optional[sqlite3.Row], *, no_here: bool,
           reference: Optional[float]) -> Optional[Tuple[str, str]]:
    """「その後の会話の要約」：最新の対話セッションの（要約か最後の依頼、終わりの時刻）。
    現在地がなければ、時刻によらず最新の要約（なければ最後の依頼）。あれば、本人の最後の入力が reference
    （task.md を書いた時刻）より後のときだけ出し、要約はそれも reference より後のときだけ使う（前なら最後の依頼）。
    引き継ぎを書いたセッションはその後も応答するので、終わりの時刻では比べない。"""
    if newest is None:
        return None
    recap = (newest["recap"] or "").strip()
    prompt = (newest["last_prompt"] or "").strip()
    if no_here:
        text = recap or prompt
    elif reference is not None and (_epoch(_last_prompt_at(conn, newest)) or 0.0) > reference:
        text = recap if recap and (_epoch(newest["recap_at"]) or 0.0) > reference else prompt
    else:
        text = ""
    return (text, newest["ended_at"]) if text else None


def _last_prompt_at(conn: sqlite3.Connection, session: sqlite3.Row) -> Optional[str]:
    """本人の最後の入力の時刻。入力の記録がなければセッションの始まりの時刻。"""
    found = conn.execute("SELECT MAX(ts) FROM messages WHERE session_id = ? AND role = 'user'",
                         (session["id"],)).fetchone()[0]
    return found or session["started_at"]


def _sessions_list(conn: sqlite3.Connection, task: str, live: Dict[str, LiveSession]) -> List[TaskSessionItem]:
    links = {r["session_id"]: r for r in conn.execute(
        "SELECT ts.session_id, ts.rule, ts.confidence FROM task_sessions ts "
        "JOIN sessions s ON s.id = ts.session_id WHERE ts.task = ? AND s.kind != 'empty'", (task,))}
    rows = session_rows(conn, list(links), live=live)
    parent_of: Dict[str, str] = {}
    for src, dst in conn.execute("SELECT src, dst FROM relations WHERE type = 'child'"):
        if src in links and dst in links:
            parent_of[dst] = src
    n_children: Dict[str, int] = {}
    for child, parent in parent_of.items():
        n_children[parent] = n_children.get(parent, 0) + 1
    items = []
    for sid, link in links.items():
        row = rows.get(sid)
        if row is None or sid in parent_of:
            continue  # 親がこのタスクにあるヘッドレスは、親の行の数に畳む
        items.append(TaskSessionItem(row=row, rule_label=RULE_LABELS.get(link["rule"], link["rule"]),
                                     confidence=link["confidence"],
                                     confidence_label=CONFIDENCE_LABELS.get(link["confidence"], link["confidence"]),
                                     n_children=n_children.get(sid, 0)))
    items.sort(key=lambda i: i.row.id)
    items.sort(key=lambda i: _epoch(i.row.ended_at) or 0.0, reverse=True)
    return items


def task_detail(conn: sqlite3.Connection, name: str, *, live: Dict[str, LiveSession], home: str,
                exclude_dirs: Sequence[str], ticket_template: Optional[str],
                default_branches: Sequence[str] = ("main", "master", "develop"),
                max_depth: int = 6) -> Optional[TaskDetail]:
    found = _task_rows(conn, live, only=name)
    if not found:
        return None
    row = found[0]
    r = conn.execute("SELECT * FROM tasks WHERE name = ?", (name,)).fetchone()
    task_dir, root = r["path"], os.path.dirname(r["path"])
    sections = json.loads(r["sections"] or "{}")
    trusted = _newest(_trusted_sessions(conn, name).get(name, []))
    interactive = [s for s in trusted if s["kind"] == "interactive"]
    task_md = os.path.join(task_dir, T.TASK_MD)

    updated = _local_epoch(r["updated"])
    after = _after(conn, interactive[0] if interactive else None, no_here=not sections.get("現在地", "").strip(),
                   reference=updated + UPDATED_PRECISION if updated is not None else (r["md_mtime"] or None))

    warnings = []
    if not r["frontmatter_ok"]:
        warnings.append("task.md の見出し情報を読めません")
    if r["stage_raw"] is not None and r["stage_raw"] not in T.STAGES:
        warnings.append("段階の値を読めません: %s" % r["stage_raw"])
    if not sections.get("現在地", "").strip():
        warnings.append("task.md に現在地がありません")

    repo = r["repo_resolved"] or r["repo"]
    skip = set(default_branches) | {"HEAD"}
    branch = r["branch"] or next((s["branch"] for s in interactive if s["branch"] and s["branch"] not in skip), None)
    urls = _pr_urls(conn, [s["id"] for s in trusted])
    prs = sorted({u for s in trusted for u in urls.get(s["id"], [])})

    relations = _doc_relations(conn, [s["id"] for s in trusted])
    named = set(json.loads(r["refs"] or "[]"))
    folder_files, cut = _folder_files(conn, task_dir, root, exclude_dirs, max_depth, relations, named)
    shared, other = _doc_bundles(conn, task_dir, root, relations, named)
    md_doc = _docs_by_path(conn, [task_md]).get(task_md)
    return TaskDetail(
        row=row, path=task_dir, task_md=task_md, md_mtime=r["md_mtime"], md_size=r["md_size"],
        task_md_doc_id=md_doc["id"] if md_doc else None, created=r["created"],
        updated_at=_iso(updated) if updated is not None else (_iso(r["md_mtime"]) if r["md_mtime"] else None),
        sections=sections, after=after, warnings=warnings, ticket_url=ticket_url(r["ticket"], repo=repo,
                                                                               template=ticket_template),
        branch=branch, prs=prs, folder_files=folder_files, folder_cut=cut, shared_docs=shared[:DOC_BUNDLE_LIMIT],
        other_docs=other[:DOC_BUNDLE_LIMIT], sessions=_sessions_list(conn, name, live),
        resume_session=interactive[0]["id"] if interactive else None, repo=repo, shared_total=len(shared),
        other_total=len(other))


# tsuzuki task list（台帳を使わない）

def task_lines(tasks_root: str, *, include_done: bool) -> List[str]:
    found: List[Tuple[float, str]] = []
    for task_dir in T.discover_tasks(tasks_root):
        md = os.path.join(task_dir, T.TASK_MD)
        try:
            mtime = os.stat(md).st_mtime
            with open(md, encoding="utf-8", errors="replace") as f:
                tf = T.parse_task_md(f.read())
        except OSError:
            continue
        status = "done" if tf.stage == "完了" else ("waiting" if tf.waiting else "active")
        if status == "done" and not include_done:
            continue
        title = T.task_title(tf, os.path.basename(task_dir))
        found.append((mtime, "\t".join((STATUS_LABELS[status], T.stage_label(tf.stage), title, task_dir))))
    found.sort(key=lambda x: (-x[0], x[1]))
    return [line for _, line in found]
