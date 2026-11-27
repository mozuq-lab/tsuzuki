"""台帳（SQLite）のスキーマと接続。"""
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

SCHEMA_VERSION = 4
LEDGER_FILENAME = "ledger.db"

_TABLES = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS places (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, display TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, transcript_path TEXT NOT NULL, transcript_exists INTEGER NOT NULL DEFAULT 1,
  size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0, inode INTEGER NOT NULL DEFAULT 0,
  offset INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'empty',
  entrypoint TEXT, version TEXT, cwd TEXT, relocated_cwd TEXT, place_id INTEGER, branch TEXT,
  started_at TEXT, ended_at TEXT,
  custom_title TEXT, agent_name TEXT, ai_title TEXT,
  first_prompt TEXT, last_prompt TEXT,
  n_prompts INTEGER NOT NULL DEFAULT 0, n_agents INTEGER NOT NULL DEFAULT 0,
  recap TEXT, recap_at TEXT, last_reply TEXT,
  forked_from TEXT, continued_in TEXT,
  turn INTEGER NOT NULL DEFAULT 0, next_seq INTEGER NOT NULL DEFAULT 0,
  n_records INTEGER NOT NULL DEFAULT 0, n_recognized INTEGER NOT NULL DEFAULT 0,
  pending_commands TEXT
);
CREATE TABLE IF NOT EXISTS derived_from (session_id TEXT NOT NULL, source_id TEXT NOT NULL, via TEXT NOT NULL,
  PRIMARY KEY (session_id, source_id, via));
CREATE TABLE IF NOT EXISTS session_prs (session_id TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY (session_id, url));
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
  turn INTEGER NOT NULL, ts TEXT, role TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS digests (session_id TEXT PRIMARY KEY, first_prompt TEXT, last_reply TEXT, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history_prompts (id INTEGER PRIMARY KEY, session_id TEXT, project TEXT, place_id INTEGER, ts TEXT, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, place_id INTEGER, kind TEXT NOT NULL,
  ext TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0, title TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS doc_links (session_id TEXT NOT NULL, path TEXT NOT NULL, rel TEXT NOT NULL, ts TEXT, prompt_index INTEGER,
  PRIMARY KEY (session_id, path, rel));
CREATE TABLE IF NOT EXISTS mentions (session_id TEXT NOT NULL, mentioned_id TEXT NOT NULL, ts TEXT, PRIMARY KEY (session_id, mentioned_id));
CREATE TABLE IF NOT EXISTS lineage (session_id TEXT PRIMARY KEY, parent_id TEXT, ts TEXT, source TEXT, cwd TEXT);
CREATE TABLE IF NOT EXISTS relations (src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL, confidence TEXT NOT NULL,
  evidence TEXT NOT NULL, PRIMARY KEY (src, dst));
CREATE TABLE IF NOT EXISTS tombstones (session_id TEXT PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS format_stats (version TEXT NOT NULL, record_type TEXT NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY (version, record_type));
CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, size INTEGER NOT NULL, inode INTEGER NOT NULL, offset INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  name TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  title TEXT NOT NULL, ticket TEXT, stage TEXT NOT NULL, stage_raw TEXT,
  waiting INTEGER NOT NULL DEFAULT 0,
  repo TEXT,
  repo_resolved TEXT,
  branch TEXT, created TEXT, updated TEXT,
  frontmatter_ok INTEGER NOT NULL DEFAULT 1,
  sections TEXT NOT NULL DEFAULT '{}',
  refs TEXT NOT NULL DEFAULT '[]',
  former TEXT NOT NULL DEFAULT '[]',
  md_mtime REAL NOT NULL DEFAULT 0, md_size INTEGER NOT NULL DEFAULT 0,
  files_mtime REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_sessions (task TEXT NOT NULL, session_id TEXT NOT NULL, rule TEXT NOT NULL,
  confidence TEXT NOT NULL,
  PRIMARY KEY (task, session_id));
CREATE TABLE IF NOT EXISTS task_overrides (task TEXT NOT NULL, session_id TEXT NOT NULL,
  action TEXT NOT NULL,
  at TEXT NOT NULL, PRIMARY KEY (task, session_id));
CREATE INDEX IF NOT EXISTS task_sessions_session ON task_sessions(session_id);
CREATE INDEX IF NOT EXISTS messages_session_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS sessions_place_ended ON sessions(place_id, ended_at);
CREATE INDEX IF NOT EXISTS doc_links_path ON doc_links(path);
CREATE INDEX IF NOT EXISTS mentions_mentioned ON mentions(mentioned_id);
CREATE INDEX IF NOT EXISTS relations_dst ON relations(dst);
CREATE INDEX IF NOT EXISTS documents_mtime ON documents(mtime);
CREATE INDEX IF NOT EXISTS history_prompts_ts ON history_prompts(ts);
"""
# documents_mtime と history_prompts_ts は、新しい順に上限まで出す一覧と検索のため（1〜2文字の語の LIKE の走査を、
# 上限の件数がそろったところで止められる）。版2の途中で足したので、それより前に作った版2の台帳にはない

# 版を上げたときに、前の版の台帳の表へ足す列（CREATE TABLE IF NOT EXISTS は、ある表に列を足さない）
_ADDED_COLUMNS = [
    ("sessions", "pending_commands", "TEXT"),  # 版2：応答を待っているスラッシュコマンド（JSON）
    ("tasks", "former", "TEXT NOT NULL DEFAULT '[]'"),  # 版4：前のフォルダー名（JSON）
]
# 版を上げたときに付ける印。次の更新が、記録の残っているセッションを新しい読み方で読み直す（indexer）
REREAD_KEY = "reread_transcripts"

# (索引名, 元の表, 元の表の行番号の列, 索引する列)
_FTS = [
    ("messages_fts", "messages", "id", ["text"]),
    ("digests_fts", "digests", "rowid", ["text"]),
    ("history_fts", "history_prompts", "id", ["text"]),
    ("documents_fts", "documents", "id", ["title", "body"]),
]


def has_trigram(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.tsuzuki_trigram_probe USING fts5(x, tokenize='trigram')")
    except sqlite3.Error:
        return False
    conn.execute("DROP TABLE temp.tsuzuki_trigram_probe")
    return True


def _create_fts(conn: sqlite3.Connection, name: str, table: str, rowid: str, cols: List[str]) -> None:
    collist = ", ".join(cols)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS %s USING fts5(%s, content='%s', content_rowid='%s', tokenize='trigram')"
        % (name, collist, table, rowid)
    )
    new_vals = ", ".join("new.%s" % c for c in cols)
    old_vals = ", ".join("old.%s" % c for c in cols)
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS %s_ai AFTER INSERT ON %s BEGIN "
        "INSERT INTO %s(rowid, %s) VALUES (new.%s, %s); END"
        % (name, table, name, collist, rowid, new_vals)
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS %s_ad AFTER DELETE ON %s BEGIN "
        "INSERT INTO %s(%s, rowid, %s) VALUES ('delete', old.%s, %s); END"
        % (name, table, name, name, collist, rowid, old_vals)
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS %s_au AFTER UPDATE ON %s BEGIN "
        "INSERT INTO %s(%s, rowid, %s) VALUES ('delete', old.%s, %s); "
        "INSERT INTO %s(rowid, %s) VALUES (new.%s, %s); END"
        % (name, table, name, name, collist, rowid, old_vals, name, collist, rowid, new_vals)
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _schema_current(conn: sqlite3.Connection) -> bool:
    """台帳のスキーマが今の版か。読むだけで確かめる（meta の表がまだなければ False）。"""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return False
    if get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
        return False
    # trigram の使えない SQLite で作った台帳を、使える SQLite で開いたときは、検索の索引を作り足す
    return get_meta(conn, "fts") == "trigram" or not has_trigram(conn)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """表と索引を作る。すでに今の版なら何も書かない（読み手が、更新の書き込みの終わりを待たずに済むように）。"""
    if _schema_current(conn):
        return
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    old_version = get_meta(conn, "schema_version") if has_meta else None
    with conn:
        for stmt in _TABLES.split(";\n"):
            if stmt.strip():
                conn.execute(stmt)
        for table, column, decl in _ADDED_COLUMNS:
            if column not in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
        trigram = has_trigram(conn)
        if trigram:
            for name, table, rowid, cols in _FTS:
                _create_fts(conn, name, table, rowid, cols)
        # 版2から版4は表と列を足すだけ（タスクの置き場所は新しい機能なので、読み直す記録はない）
        if old_version is not None and old_version not in (str(SCHEMA_VERSION), "3", "2"):
            set_meta(conn, REREAD_KEY, "1")
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        set_meta(conn, "fts", "trigram" if trigram else "none")


def open_ledger(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
    os.chmod(str(path), 0o600)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")  # 書き込みのトランザクションの間も、ほかの接続は読める
    ensure_schema(conn)
    return conn
