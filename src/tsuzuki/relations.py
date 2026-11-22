"""会話どうしのつながりの推定。向きは src が先、dst が後。"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

WEAK_WINDOW_SECONDS = 300
HANDOFF_PROMPT_LIMIT = 3
PRECEDENCE = [("fork", "confirmed"), ("continued", "confirmed"), ("child", "confirmed"),
              ("child", "strong"), ("child", "weak"), ("handoff", "strong"), ("reference", "strong")]
# 記録の derived-from の via と、つながりの種類。ほかの via（clear・resume など）は使わない
DERIVED_TYPES = {"subprocess": "child", "branch": "fork", "fork": "fork", "compact": "continued"}
DERIVED_EVIDENCE = "記録の derived-from（%s）"

# 根拠（evidence）は画面にそのまま出るので、記録のフィールド名だけにせず日本語で枠を付ける（例「記録の forkedFrom」）
Rel = Tuple[str, str, str, str, str]  # src, dst, type, confidence, evidence


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def _shift(ts: str, seconds: int) -> Optional[str]:
    try:
        d = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    frac = ts[19:].rstrip("Z")
    if frac.startswith("."):
        try:
            d = d.replace(microsecond=int((frac[1:] + "000000")[:6]))
        except ValueError:
            pass
    return _iso(d + timedelta(seconds=seconds))


def infer_relations(conn: sqlite3.Connection) -> int:
    sess = {}  # id -> (kind, place_id, started_at, ended_at)
    cands: List[Rel] = []
    for r in conn.execute("SELECT id, kind, place_id, started_at, ended_at, forked_from, continued_in FROM sessions"):
        sess[r[0]] = (r[1], r[2], r[3], r[4])
    for r in conn.execute("SELECT id, forked_from, continued_in FROM sessions"):
        if r[1] and r[1] in sess and r[1] != r[0]:
            cands.append((r[1], r[0], "fork", "confirmed", "記録の forkedFrom"))
        if r[2] and r[2] in sess and r[2] != r[0]:
            cands.append((r[0], r[2], "continued", "confirmed", "記録の continued-in"))
    derived, derived_parents = _derived(conn, sess)
    cands += derived

    # 子
    lineage = {r[0]: r[1] for r in conn.execute("SELECT session_id, parent_id FROM lineage")}
    mentioned_by = {}  # B -> [(ts, A)]
    for a, b, ts in conn.execute("SELECT session_id, mentioned_id, ts FROM mentions"):
        if a != b and a in sess and b in sess:
            mentioned_by.setdefault(b, []).append((ts, a))
    for b, (kind, place, start, end) in sorted(sess.items()):
        parents = derived_parents.get(b)
        if parents and kind != "empty":
            # Claude Code 自身が書いた親なので、フックの記録や推定より先に使う（親は1つ）
            a = min(parents, key=lambda x: (not _contains(sess[x], start), x))
            cands.append((a, b, "child", "confirmed", DERIVED_EVIDENCE % "subprocess"))
            continue
        if kind != "headless":
            continue
        p = lineage.get(b)
        if p and p in sess and p != b:
            cands.append((p, b, "child", "confirmed", "フックの記録"))
            continue
        if start is None:
            continue
        ok = [(ts, a) for ts, a in mentioned_by.get(b, [])
              if sess[a][2] is not None and sess[a][2] <= start]
        if ok:
            inside = [(ts, a) for ts, a in ok if sess[a][3] is not None and sess[a][3] >= start]
            pool = inside or ok
            _, a = min(pool, key=lambda x: (x[0] is None, x[0] or "", x[1]))
            cands.append((a, b, "child", "strong", "記録に ID"))
            continue
        if place is None:
            continue
        lo, hi = _shift(start, -WEAK_WINDOW_SECONDS), _shift(start, WEAK_WINDOW_SECONDS)
        if lo is None or hi is None:
            continue
        found = [r[0] for r in conn.execute(
            "SELECT DISTINCT m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id"
            " WHERE s.kind = 'interactive' AND s.place_id = ? AND s.id != ? AND m.ts >= ? AND m.ts <= ?",
            (place, b, lo, hi))]
        if len(found) == 1:
            cands.append((found[0], b, "child", "weak", "前後5分に動いていた対話が1つ"))

    # 資料で引き継ぎ
    by_path = {}  # path -> [(ts, A)]
    by_base = {}  # basename -> [(ts, A)]
    for a, path, ts in conn.execute(
            "SELECT session_id, path, ts FROM doc_links WHERE rel IN ('created','edited') AND ts IS NOT NULL"):
        if a in sess:
            by_path.setdefault(path, []).append((ts, a))
            by_base.setdefault(os.path.basename(path), []).append((ts, a))
    named = conn.execute(
        "SELECT session_id, path FROM doc_links WHERE rel = 'named' AND prompt_index IS NOT NULL"
        " AND prompt_index <= ? ORDER BY session_id, prompt_index, path", (HANDOFF_PROMPT_LIMIT,)).fetchall()
    for b, path in named:
        if b not in sess or sess[b][0] != "interactive" or sess[b][2] is None:
            continue
        start, place = sess[b][2], sess[b][1]
        best = {}  # A -> latest ts
        pool = list(by_path.get(path, []))
        if place is not None:
            pool += [(ts, a) for ts, a in by_base.get(os.path.basename(path), []) if sess[a][1] == place]
        for ts, a in pool:
            if a != b and ts < start and ts > best.get(a, ""):
                best[a] = ts
        if best:
            top = max(best.values())
            a = min(x for x in best if best[x] == top)
            cands.append((a, b, "handoff", "strong", "資料 %s" % os.path.basename(path)))

    # 参照
    for b, lst in mentioned_by.items():
        if sess[b][0] != "interactive":
            continue
        for _, a in sorted(lst, key=lambda x: x[1]):
            if sess[a][0] == "interactive":
                cands.append((b, a, "reference", "strong", "記録に ID"))

    # 優先順位
    rank = {k: i for i, k in enumerate(PRECEDENCE)}
    chosen: Dict[Tuple[str, str], Rel] = {}
    for c in cands:
        key = tuple(sorted((c[0], c[1])))
        old = chosen.get(key)
        if old is None or rank[(c[2], c[3])] < rank[(old[2], old[3])]:
            chosen[key] = c
    with conn:
        conn.execute("DELETE FROM relations")
        conn.executemany("INSERT INTO relations(src, dst, type, confidence, evidence) VALUES (?,?,?,?,?)",
                         list(chosen.values()))
    return len(chosen)


def _derived(conn: sqlite3.Connection, sess: Dict[str, tuple]) -> Tuple[List[Rel], Dict[str, List[str]]]:
    """記録の derived-from から、分岐と続きの会話のつながりと、子の親の候補（B → [A]）を作る。"""
    rels, parents = [], {}  # type: List[Rel], Dict[str, List[str]]
    for b, a, via in conn.execute(
            "SELECT session_id, source_id, via FROM derived_from ORDER BY session_id, source_id, via"):
        typ = DERIVED_TYPES.get(via)
        if typ is None or a == b or a not in sess or b not in sess:
            continue
        if typ == "child":
            parents.setdefault(b, []).append(a)
        else:
            rels.append((a, b, typ, "confirmed", DERIVED_EVIDENCE % via))
    return rels, parents


def _contains(session: tuple, ts: Optional[str]) -> bool:
    """セッションの期間（開始から最後の動きまで）が ts を含むか。"""
    start, end = session[2], session[3]
    return ts is not None and start is not None and end is not None and start <= ts <= end

