"""追記される JSONL を差分で読む補助。"""
import os
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple


def read_new_lines(conn: sqlite3.Connection, name: str, path: Path,
                   reset_tables: Tuple[str, ...]) -> Optional[Tuple[List[bytes], int, int, int]]:
    """未読の完全な行を返す。ファイルが無ければ None。
    小さくなった・inode が変わった場合は reset_tables を空にして頭から読む。"""
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    row = conn.execute("SELECT size, inode, offset FROM sources WHERE name = ?", (name,)).fetchone()
    offset = 0
    if row is not None:
        if st.st_size < row[0] or st.st_ino != row[1] or st.st_size < row[2]:
            for table in reset_tables:
                conn.execute("DELETE FROM %s" % table)
        else:
            offset = row[2]
    with open(str(path), "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1  # 末尾の改行なしの行は読まない
    lines = data[:end].split(b"\n")[:-1] if end else []
    return lines, st.st_size, st.st_ino, offset + end


def save_state(conn: sqlite3.Connection, name: str, size: int, inode: int, offset: int) -> None:
    conn.execute("INSERT OR REPLACE INTO sources(name, size, inode, offset) VALUES (?, ?, ?, ?)",
                 (name, size, inode, offset))
