"""Tsuzuki 自身のフックが書く系譜の記録（lineage.jsonl）を台帳へ取り込む。"""
import json
import sqlite3
from pathlib import Path

from tsuzuki.sources._tail import read_new_lines, save_state

LINEAGE_FIELDS = ("ts", "session_id", "parent_id", "parent_pid", "source", "cwd", "entrypoint")


def sync_lineage(conn: sqlite3.Connection, path: Path) -> int:
    written = 0
    with conn:
        got = read_new_lines(conn, "lineage", path, ("lineage",))
        if got is None:
            return 0
        lines, size, inode, offset = got
        for raw in lines:
            try:
                rec = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("session_id"), str):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO lineage(session_id, parent_id, ts, source, cwd) VALUES (?, ?, ?, ?, ?)",
                (rec["session_id"],) + tuple(
                    rec.get(k) if isinstance(rec.get(k), str) else None
                    for k in ("parent_id", "ts", "source", "cwd")))
            written += 1
        save_state(conn, "lineage", size, inode, offset)
    return written
