"""名前を変えたタスクの、前のフォルダー名のパスの読み替え。"""
import tempfile
import unittest
from pathlib import Path

from tsuzuki.taskrename import current_path, former_folders, rewrite_doc_links
from tests.fixtures import make_ledger


class TaskRenameTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = make_ledger(Path(self._tmp.name))
        self.addCleanup(self.conn.close)

    def add_task(self, name, path, former="[]"):
        with self.conn:
            self.conn.execute("INSERT INTO tasks(name, path, title, stage, former) VALUES (?, ?, ?, '調査', ?)",
                              (name, path, name, former))

    def add_link(self, sid, path, rel):
        with self.conn:
            self.conn.execute("INSERT INTO doc_links(session_id, path, rel, ts, prompt_index) VALUES (?, ?, ?, 't', 1)",
                              (sid, path, rel))

    def test_former_folders_and_current_path(self):
        self.add_task("2026-11-19-search", "/t/2026-11-19-search", former='["2026-11-19-serch", "2026-11-20-b"]')
        self.add_task("2026-11-20-b", "/t/2026-11-20-b")  # 今あるタスクの名前は読み替えない
        self.add_task("2026-11-21-c", "/t/2026-11-21-c", former="壊れた JSON")
        folders = former_folders(self.conn)
        self.assertEqual(folders, {"/t/2026-11-19-serch": "/t/2026-11-19-search"})
        self.assertEqual(current_path("/t/2026-11-19-serch/notes/a.md", folders), "/t/2026-11-19-search/notes/a.md")
        self.assertEqual(current_path("/t/2026-11-19-serch", folders), "/t/2026-11-19-search")
        self.assertIsNone(current_path("/t/2026-11-19-serch-2/a.md", folders))

    def test_rewrite_doc_links_moves_rows_and_drops_duplicates(self):
        self.add_task("2026-11-19-search", "/t/2026-11-19-search", former='["2026-11-19-serch"]')
        self.add_link("s1", "/t/2026-11-19-serch/a.md", "created")
        self.add_link("s1", "/t/2026-11-19-search/a.md", "created")  # 同じ行が今のパスにもある
        self.add_link("s2", "/t/2026-11-19-serch/task.md", "named")
        self.add_link("s3", "/t/2026-11-19-serch-2/a.md", "created")  # 前のフォルダーの外
        self.assertEqual(rewrite_doc_links(self.conn), 2)
        rows = sorted(tuple(r) for r in self.conn.execute("SELECT session_id, path, rel FROM doc_links"))
        self.assertEqual(rows, [("s1", "/t/2026-11-19-search/a.md", "created"),
                                ("s2", "/t/2026-11-19-search/task.md", "named"),
                                ("s3", "/t/2026-11-19-serch-2/a.md", "created")])
        self.assertEqual(rewrite_doc_links(self.conn), 0)  # 2回目は何もしない


if __name__ == "__main__":
    unittest.main()
