"""名前を変えたタスクの、前のフォルダー名のパスを今のパスに読み替える。

台帳の行だけを読み書きし、ファイルは読まない。前の名前は tasks.former（JSON の配列）に残っている。
"""
import json
import os
import sqlite3
from typing import Dict, Optional

from tsuzuki.taskindex import _bounds


def former_folders(conn: sqlite3.Connection) -> Dict[str, str]:
    """{前のフォルダーの絶対パス: 今のフォルダーの絶対パス}。今あるどれかのタスクのフォルダーと同じ物、
    今のフォルダーと同じ物、JSON が読めない行は入れない。"""
    rows = conn.execute("SELECT path, former FROM tasks").fetchall()
    existing = {os.path.normpath(path) for path, _ in rows}
    out = {}  # type: Dict[str, str]
    for path, former in rows:
        try:
            names = json.loads(former or "[]")
        except ValueError:
            continue
        if not isinstance(names, list):
            continue
        current = os.path.normpath(path)
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            old = os.path.join(os.path.dirname(current), name)
            if old not in existing:
                out[old] = current
    return out


def current_path(path: str, folders: Dict[str, str]) -> Optional[str]:
    """path が前のフォルダーそのものか、その下なら今のパス。そうでなければ None。"""
    for old, new in folders.items():
        if path == old:
            return new
        if path.startswith(old + "/"):
            return new + path[len(old):]
    return None


def rewrite_doc_links(conn: sqlite3.Connection) -> int:
    """doc_links.path のうち前のフォルダーの中の物を今のパスに書き換え、前のパスから動かした行の数
    （書き換えた行と、同じ鍵 (session_id, path, rel) が今のパスにもあって消した古い行）を返す。"""
    folders = former_folders(conn)
    moved = 0
    with conn:
        for old, new in sorted(folders.items()):
            low, high = _bounds(old)
            where = "(path = ? OR (path >= ? AND path < ?))"
            args = (old, low, high)
            # 前のパスの先頭 len(old) 文字を今のパスに差し替える
            cur = conn.execute("UPDATE OR IGNORE doc_links SET path = ? || substr(path, ?) WHERE " + where,
                               (new, len(old) + 1) + args)
            moved += cur.rowcount
            moved += conn.execute("DELETE FROM doc_links WHERE " + where, args).rowcount
    return moved
