"""Claude Code の入力履歴（history.jsonl）を台帳へ取り込む。pastedContents は読まない。"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Set

from tsuzuki.sources._tail import read_new_lines, save_state


def ms_to_iso(ms: int) -> str:
    d = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def sync_history(conn: sqlite3.Connection, path: Path, *, tombstoned: Set[str],
                 place_of: Callable[[str], Optional[int]]) -> int:
    inserted = 0
    with conn:
        got = read_new_lines(conn, "history", path, ("history_prompts",))
        if got is None:
            return 0
        lines, size, inode, offset = got
        for raw in lines:
            try:
                rec = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            text = rec.get("display")
            ts = rec.get("timestamp")
            if not isinstance(text, str) or not text:
                continue
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            try:
                iso = ms_to_iso(int(ts))
            except (ValueError, OverflowError, OSError):
                continue
            session = rec.get("sessionId")
            session = session if isinstance(session, str) else None
            if session is not None and session in tombstoned:
                continue
            project = rec.get("project")
            project = project if isinstance(project, str) else None
            place = place_of(project) if project is not None else None
            conn.execute(
                "INSERT INTO history_prompts(session_id, project, place_id, ts, text) VALUES (?, ?, ?, ?, ?)",
                (session, project, place, iso, text))
            inserted += 1
        save_state(conn, "history", size, inode, offset)
    return inserted
