"""台帳からセッションを消す。purge と forget もここ。"""
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

DAY_SECONDS = 86400
# セッションごとの行を持つ表。history_prompts は history.jsonl の写しなので、ここでは消さない
_SESSION_TABLES = ("messages", "digests", "doc_links", "mentions", "session_prs", "derived_from")


def iso_utc(ts: float) -> str:
    """UNIX 時刻を、台帳の時刻の形（UTC の ISO、ミリ秒と Z 付き）にする。"""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def delete_session_data(conn: sqlite3.Connection, session_id: str) -> None:
    """1つのセッションの行を消す。tombstone は作らず、コミットは呼び出し側が行う。"""
    for table in _SESSION_TABLES:
        conn.execute("DELETE FROM %s WHERE session_id = ?" % table, (session_id,))
    conn.execute("DELETE FROM relations WHERE src = ? OR dst = ?", (session_id, session_id))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def apply_missing(conn: sqlite3.Connection, retention: str, session_ids: Iterable[str]) -> int:
    """記録が消えたセッションに設定 retention を当て、当てたセッションの数を返す。
    コミットは呼び出し側が行う（記録の有無の更新と同じトランザクションにするため）。"""
    if retention == "all":
        return 0
    if retention not in ("card", "none"):
        raise ValueError("retention は all・card・none のどれかです: %r" % (retention,))
    ids = list(session_ids)
    for sid in ids:
        if retention == "card":
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        else:
            delete_session_data(conn, sid)
    return len(ids)


def apply_max_age(conn: sqlite3.Connection, max_age_days: Optional[int], now: float) -> int:
    """最後の動き（ended_at）が日数より前のセッションを消す。tombstone は作らないので、
    再開されて記録が増えれば次の更新でまた取り込まれる。消した数を返す。"""
    if max_age_days is None:
        return 0
    cutoff = iso_utc(now - max_age_days * DAY_SECONDS)
    ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE ended_at < ?", (cutoff,))]
    with conn:
        for sid in ids:
            delete_session_data(conn, sid)
    return len(ids)


def purge_before(conn: sqlite3.Connection, date: str, *, now_iso: str) -> int:
    """その日の地方時0時より前に終わったセッションを消し、tombstone を作る。消した数を返す。"""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date or ""):
        raise ValueError("日付は YYYY-MM-DD の形で指定してください: %r" % (date,))
    try:
        y, m, d = (int(x) for x in date.split("-"))
        local_midnight = datetime(y, m, d).astimezone()
    except (ValueError, OverflowError, OSError):
        raise ValueError("日付は YYYY-MM-DD の形で指定してください: %r" % (date,))
    cutoff = iso_utc(local_midnight.timestamp())
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?", (cutoff,))]
    with conn:
        for sid in ids:
            delete_session_data(conn, sid)
            conn.execute("DELETE FROM history_prompts WHERE session_id = ?", (sid,))
            conn.execute("INSERT OR REPLACE INTO tombstones (session_id, reason, at) VALUES (?, 'purge', ?)",
                         (sid, now_iso))
    return len(ids)


def forget(conn: sqlite3.Connection, session_id: str, *, now_iso: str) -> bool:
    """セッションを台帳から消し、以後取り込まないようにする。何かを消したら True。"""
    with conn:
        existed = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None
        delete_session_data(conn, session_id)
        n = conn.execute("DELETE FROM history_prompts WHERE session_id = ?", (session_id,)).rowcount
        conn.execute("INSERT OR REPLACE INTO tombstones (session_id, reason, at) VALUES (?, 'forget', ?)",
                     (session_id, now_iso))
    return existed or n > 0


def drop_forgotten(conn: sqlite3.Connection) -> int:
    """tombstone のあるセッションの行が台帳に残っていれば消し、消したセッションの数を返す。
    更新が tombstone を確かめた後に forget・purge が終わると、更新はそのセッションを読み直して作り直すため
    （tombstone があるので、ここで消さないと以後も消えない）。"""
    ids = [r[0] for r in conn.execute("SELECT s.id FROM sessions s JOIN tombstones t ON t.session_id = s.id")]
    history = conn.execute("SELECT 1 FROM history_prompts h JOIN tombstones t ON t.session_id = h.session_id "
                           "LIMIT 1").fetchone()
    if ids or history:
        with conn:
            for sid in ids:
                delete_session_data(conn, sid)
            conn.execute("DELETE FROM history_prompts WHERE session_id IN (SELECT session_id FROM tombstones)")
    return len(ids)


def forget_undo(conn: sqlite3.Connection, session_id: str) -> bool:
    """tombstone を消す。消せたら、次の更新で history.jsonl を頭から読み直させる
    （入れてある入力履歴も空にする。残すと、読み直した行が二重に入る）。"""
    with conn:
        n = conn.execute("DELETE FROM tombstones WHERE session_id = ?", (session_id,)).rowcount
        if n:
            conn.execute("DELETE FROM history_prompts")
            conn.execute("DELETE FROM sources WHERE name = 'history'")
    return n > 0
