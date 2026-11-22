import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki import store
from tsuzuki.store import SCHEMA_VERSION, ensure_schema, get_meta, has_trigram, open_ledger, set_meta
from tsuzuki.store import REREAD_KEY
from tests.fixtures import count, downgrade_to_v1, downgrade_to_v2, make_ledger


def fts_hits(conn, table, query):
    return conn.execute(
        "SELECT rowid FROM %s WHERE %s MATCH ?" % (table, table), (query,)
    ).fetchall()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_v3_tables(self):
        conn = make_ledger(self.tmp)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"tasks", "task_sessions", "task_overrides"} <= names)
        self.assertEqual(get_meta(conn, "schema_version"), "3")

    def test_upgrade_from_v2_keeps_rows(self):
        conn = make_ledger(self.tmp)
        conn.execute("INSERT INTO sessions(id, transcript_path) VALUES ('s1', '/x.jsonl')")
        conn.commit()
        downgrade_to_v2(conn)
        conn.close()
        conn = make_ledger(self.tmp)
        self.assertEqual(get_meta(conn, "schema_version"), "3")
        self.assertEqual(count(conn, "sessions"), 1)
        self.assertEqual(count(conn, "tasks"), 0)
        self.assertIsNone(get_meta(conn, REREAD_KEY))

    def test_schema_is_idempotent_and_versioned(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.db"
            open_ledger(p).close()
            conn = open_ledger(p)
            self.assertEqual(get_meta(conn, "schema_version"), "3")
            self.assertEqual(get_meta(conn, "fts"), "trigram")
            self.assertIsNone(get_meta(conn, "reread_transcripts"))  # 新しい台帳には読み直す記録がない
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            ensure_schema(conn)
            conn.close()

    def test_time_indexes_for_newest_first_lists(self):
        # 資料と入力履歴は新しい順に上限まで出す。時刻の索引があれば、上限の件数がそろったところで読むのを止められる
        with tempfile.TemporaryDirectory() as d:
            conn = open_ledger(Path(d) / "ledger.db")
            indexes = {name: sql for name, sql in conn.execute("SELECT name, sql FROM sqlite_master "
                                                               "WHERE type = 'index' AND sql IS NOT NULL")}
            self.assertIn("ON documents(mtime)", indexes.get("documents_mtime", ""))
            self.assertIn("ON history_prompts(ts)", indexes.get("history_prompts_ts", ""))
            conn.close()

    def test_parent_directory_is_created_private(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "ledger.db"
            open_ledger(p).close()
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)

    def test_existing_file_is_chmodded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.db"
            open_ledger(p).close()
            p.chmod(0o644)
            open_ledger(p).close()
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_pragmas_and_meta_helpers(self):
        with tempfile.TemporaryDirectory() as d:
            conn = open_ledger(Path(d) / "ledger.db")
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertIsNone(get_meta(conn, "nope"))
            self.assertEqual(get_meta(conn, "nope", "x"), "x")
            set_meta(conn, "k", "v1")
            set_meta(conn, "k", "v2")
            self.assertEqual(get_meta(conn, "k"), "v2")

    def test_trigram_finds_japanese_substring_and_follows_delete(self):
        with tempfile.TemporaryDirectory() as d:
            conn = open_ledger(Path(d) / "ledger.db")
            self.assertTrue(has_trigram(conn))
            conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES ('s', 0, 1, NULL, 'user', '引き継ぎ資料を作りました')")
            hit = conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", ('"継ぎ資料"',)).fetchall()
            self.assertEqual(len(hit), 1)
            conn.execute("DELETE FROM messages WHERE session_id = 's'")
            self.assertEqual(conn.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?", ('"継ぎ資料"',)).fetchone()[0], 0)

    def test_update_trigger_replaces_index_entry(self):
        with tempfile.TemporaryDirectory() as d:
            conn = open_ledger(Path(d) / "ledger.db")
            conn.execute("INSERT INTO messages(session_id, seq, turn, role, text) VALUES ('s', 0, 1, 'user', '古い本文です')")
            conn.execute("UPDATE messages SET text = '新しい内容です' WHERE session_id = 's'")
            self.assertEqual(len(fts_hits(conn, "messages_fts", '"古い本文"')), 0)
            self.assertEqual(len(fts_hits(conn, "messages_fts", '"新しい内"')), 1)

    def test_documents_fts_covers_title_and_body(self):
        with tempfile.TemporaryDirectory() as d:
            conn = open_ledger(Path(d) / "ledger.db")
            conn.execute("INSERT INTO documents(path, kind, ext, title, body) VALUES ('/a.md', 'plan', '.md', '料理の仕事', '本文の内容です')")
            self.assertEqual(len(fts_hits(conn, "documents_fts", '"料理の仕"')), 1)
            self.assertEqual(len(fts_hits(conn, "documents_fts", '"本文の"')), 1)
            conn.execute("DELETE FROM documents WHERE path = '/a.md'")
            self.assertEqual(len(fts_hits(conn, "documents_fts", '"料理の仕"')), 0)

    def test_documents_with_null_body_and_title(self):
        with tempfile.TemporaryDirectory() as d:
            conn = open_ledger(Path(d) / "ledger.db")
            conn.execute("INSERT INTO documents(path, kind, ext, title, body) VALUES ('/b.html', 'other', '.html', '題名だけ', NULL)")
            conn.execute("INSERT INTO documents(path, kind, ext, title, body) VALUES ('/c.bin', 'other', '.bin', NULL, NULL)")
            self.assertEqual(len(fts_hits(conn, "documents_fts", '"題名だ"')), 1)
            conn.execute("UPDATE documents SET body = '後から入った' WHERE path = '/b.html'")
            self.assertEqual(len(fts_hits(conn, "documents_fts", '"後から入"')), 1)
            self.assertEqual(len(fts_hits(conn, "documents_fts", '"題名だ"')), 1)
            conn.execute("DELETE FROM documents")
            self.assertEqual(len(fts_hits(conn, "documents_fts", '"題名だ"')), 0)

    def test_digests_and_history_fts(self):
        with tempfile.TemporaryDirectory() as d:
            conn = open_ledger(Path(d) / "ledger.db")
            conn.execute("INSERT INTO digests(session_id, text) VALUES ('h', '最初の依頼と最後の応答')")
            conn.execute("INSERT INTO history_prompts(session_id, text) VALUES ('h', '履歴の入力です')")
            self.assertEqual(len(fts_hits(conn, "digests_fts", '"最後の応"')), 1)
            self.assertEqual(len(fts_hits(conn, "history_fts", '"履歴の入"')), 1)
            conn.execute("DELETE FROM digests")
            conn.execute("DELETE FROM history_prompts")
            self.assertEqual(len(fts_hits(conn, "digests_fts", '"最後の応"')), 0)
            self.assertEqual(len(fts_hits(conn, "history_fts", '"履歴の入"')), 0)

    def test_opening_an_up_to_date_ledger_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.db"
            open_ledger(p).close()
            conn = open_ledger(p)
            self.assertEqual(conn.total_changes, 0)
            self.assertFalse(conn.in_transaction)
            ensure_schema(conn)
            self.assertEqual(conn.total_changes, 0)
            conn.close()

    def test_readers_open_while_another_connection_writes(self):
        # 更新が書き込みのトランザクションを持っている間も、読み手は待たずに開いて読める（WAL）
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.db"
            open_ledger(p).close()
            writer = sqlite3.connect(str(p), isolation_level=None)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO meta(key, value) VALUES ('pending', 'x')")
            try:
                started = time.monotonic()
                conn = open_ledger(p)
                self.assertEqual(get_meta(conn, "schema_version"), str(SCHEMA_VERSION))
                self.assertIsNone(get_meta(conn, "pending"))  # 確定していない書き込みは見えない
                conn.close()
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                writer.execute("ROLLBACK")
                writer.close()

    def test_old_or_partial_ledgers_are_brought_up_to_date(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.db"
            conn = open_ledger(p)
            set_meta(conn, "schema_version", "0")
            conn.execute("DROP TABLE tombstones")
            conn.commit()
            conn.close()
            conn = open_ledger(p)
            self.assertEqual(get_meta(conn, "schema_version"), str(SCHEMA_VERSION))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0], 0)
            conn.close()
            # meta の表がまだない台帳（作りかけ）も読めるようにする
            raw = sqlite3.connect(str(Path(d) / "bare.db"))
            raw.execute("CREATE TABLE unrelated (x)")
            raw.close()
            conn = open_ledger(Path(d) / "bare.db")
            self.assertEqual(get_meta(conn, "schema_version"), str(SCHEMA_VERSION))
            conn.close()

    def test_version_1_ledger_is_upgraded(self):
        # 版1の台帳：sessions に後から足した列がなく、derived_from の表もない
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.db"
            conn = open_ledger(p)
            downgrade_to_v1(conn)
            with conn:
                conn.execute("INSERT INTO sessions(id, transcript_path, kind, n_prompts, offset) "
                             "VALUES ('s1', '/x/s1.jsonl', 'interactive', 2, 120)")
                conn.execute("INSERT INTO messages(session_id, seq, turn, role, text) VALUES ('s1', 0, 1, 'user', '残る本文')")
            conn.close()
            conn = open_ledger(p)
            self.assertEqual(get_meta(conn, "schema_version"), str(SCHEMA_VERSION))
            # 記録の読み直しは次の更新に任せる（開くときは印を付けるだけ）
            self.assertEqual(get_meta(conn, "reread_transcripts"), "1")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            self.assertIn("pending_commands", columns)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM derived_from").fetchone()[0], 0)
            self.assertEqual(tuple(conn.execute("SELECT id, kind, n_prompts, offset, pending_commands FROM sessions").fetchone()),
                             ("s1", "interactive", 2, 120, None))
            self.assertEqual(len(fts_hits(conn, "messages_fts", '"残る本"')), 1)
            conn.close()
            conn = open_ledger(p)  # 上げた後は何も書かない
            self.assertEqual(conn.total_changes, 0)
            conn.close()

    def test_fts_is_added_when_trigram_becomes_available(self):
        # trigram のない SQLite で作った台帳を、使える SQLite で開いたら検索の索引を作る（以前の振る舞いを保つ）
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.db"
            with mock.patch.object(store, "has_trigram", lambda c: False):
                open_ledger(p).close()
            conn = open_ledger(p)
            self.assertEqual(get_meta(conn, "fts"), "trigram")
            self.assertIsNone(get_meta(conn, "reread_transcripts"))  # 版は変わっていないので読み直さない
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_fts'")}
            self.assertIn("messages_fts", names)
            conn.close()
            conn = open_ledger(p)
            self.assertEqual(conn.total_changes, 0)
            conn.close()

    def test_without_trigram_no_fts_tables(self):
        conn = sqlite3.connect(":memory:")
        import tsuzuki.store as store
        orig = store.has_trigram
        store.has_trigram = lambda c: False
        try:
            ensure_schema(conn)
        finally:
            store.has_trigram = orig
        self.assertEqual(get_meta(conn, "fts"), "none")
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_fts%'")]
        self.assertEqual(names, [])


if __name__ == "__main__":
    unittest.main()
