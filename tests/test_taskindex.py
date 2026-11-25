import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki.config import Config
from tsuzuki.indexer import _Places, update
from tsuzuki.store import get_meta
from tsuzuki.taskindex import refresh_task, sync_tasks
from tests.fixtures import FakeClaude, allow_system_tmp, count, make_ledger, write_file
from tests.test_tasks import SAMPLE


class TaskIndexTest(unittest.TestCase):
    def setUp(self):
        ctx = allow_system_tmp()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.d, "home")
        self.root = os.path.join(self.d, "tasks")
        self.cfg = Config()
        self.conn = make_ledger(Path(self.d) / "ledger")
        self.addCleanup(self.conn.close)
        login = self.root + "/2026-11-20-login"
        write_file(login + "/task.md", SAMPLE)
        write_file(login + "/research-a.md", "# 調査\n本文\n")
        write_file(login + "/shot.png", "png")
        write_file(self.root + "/2026-11-21-csv/task.md", "# CSV\n")
        write_file(self.root + "/shared/notes-team.md", "# メモ\n")
        write_file(self.root + "/_old/task.md", "# 古い\n")

    def sync(self):
        places = _Places(self.conn, self.cfg, self.home, self.root)
        return sync_tasks(self.conn, self.cfg, tasks_root=self.root, home=self.home, places=places)

    def test_sync_reads_tasks(self):
        sync = self.sync()
        self.assertEqual((sync.changed, sync.tasks), (True, 2))
        rows = dict(self.conn.execute("SELECT name, stage FROM tasks"))
        self.assertEqual(rows, {"2026-11-20-login": "実装", "2026-11-21-csv": "チケット"})
        kinds = dict(self.conn.execute("SELECT path, kind FROM documents"))
        self.assertEqual(kinds[self.root + "/2026-11-20-login/research-a.md"], "research")
        self.assertEqual(kinds[self.root + "/2026-11-20-login/task.md"], "cover")
        self.assertEqual(kinds[self.root + "/shared/notes-team.md"], "notes")
        self.assertNotIn(self.root + "/2026-11-20-login/shot.png", kinds)
        displays = {r[0] for r in self.conn.execute("SELECT display FROM places")}
        self.assertTrue({"タスク", "共通の資料"} <= displays)

    def test_row_values(self):
        self.sync()
        row = self.conn.execute("SELECT title, ticket, repo, created, frontmatter_ok, refs, sections "
                                "FROM tasks WHERE name = '2026-11-20-login'").fetchone()
        self.assertEqual(row[:5], ("ログインのセッション切れを延ばす", "#123", self.home + "/dev/web-app", "2026-11-20", 1))
        self.assertEqual(row[5], '["%s"]' % (self.root + "/shared/auth.md"))
        self.assertIn("結合テスト", row[6])
        csv = self.conn.execute("SELECT created, repo, frontmatter_ok FROM tasks WHERE name = '2026-11-21-csv'").fetchone()
        self.assertEqual(tuple(csv), ("2026-11-21", None, 0))

    def test_unchanged_tasks_are_not_reparsed(self):
        self.sync()
        with mock.patch("tsuzuki.taskindex.parse_task_md", side_effect=AssertionError("読み直した")):
            self.assertFalse(self.sync().changed)

    def test_edit_is_picked_up(self):
        self.sync()
        write_file(self.root + "/2026-11-21-csv/task.md", "---\nstage: 調整\nwaiting: true\n---\n")
        self.assertTrue(self.sync().changed)
        row = self.conn.execute("SELECT stage, waiting FROM tasks WHERE name = '2026-11-21-csv'").fetchone()
        self.assertEqual(tuple(row), ("調整", 1))

    def test_deleted_document_is_dropped(self):
        self.sync()
        os.remove(self.root + "/2026-11-20-login/research-a.md")
        self.sync()
        paths = [r[0] for r in self.conn.execute("SELECT path FROM documents")]
        self.assertNotIn(self.root + "/2026-11-20-login/research-a.md", paths)

    def test_vanished_task_drops_rows_but_keeps_overrides(self):
        self.sync()
        self.conn.execute("INSERT INTO task_overrides VALUES ('2026-11-21-csv', 's1', 'add', 'x')")
        self.conn.commit()
        shutil.rmtree(self.root + "/2026-11-21-csv")
        self.assertTrue(self.sync().changed)
        self.assertEqual(count(self.conn, "task_overrides"), 1)  # 手で直した記録は、フォルダーが消えても残す
        self.assertEqual([r[0] for r in self.conn.execute("SELECT name FROM tasks")], ["2026-11-20-login"])
        paths = [r[0] for r in self.conn.execute("SELECT path FROM documents")]
        self.assertFalse([p for p in paths if "2026-11-21-csv" in p])

    def test_a_moved_task_gets_its_new_path(self):
        # フォルダーを rename で動かすと、task.md の更新時刻も大きさも変わらない。それでも path は付け替える
        self.sync()
        new_root = os.path.join(self.d, "moved")
        os.makedirs(new_root)
        os.rename(self.root + "/2026-11-20-login", new_root + "/2026-11-20-login")
        self.root = new_root
        self.sync()
        self.assertEqual(self.conn.execute("SELECT path FROM tasks WHERE name = '2026-11-20-login'").fetchone()[0],
                         new_root + "/2026-11-20-login")

    def sync_at(self, root):
        places = _Places(self.conn, self.cfg, self.home, root)
        return sync_tasks(self.conn, self.cfg, tasks_root=root, home=self.home, places=places)

    def test_first_sync_records_the_root_without_a_reset(self):
        self.assertIsNone(get_meta(self.conn, "tasks:root"))
        self.sync()
        self.assertEqual(get_meta(self.conn, "tasks:root"), self.root)
        self.assertEqual(count(self.conn, "tasks"), 2)

    def test_changing_the_root_without_the_screen_drops_the_old_root_rows(self):
        # config.json を手で直して起動し直した場合や tsuzuki index でも、古い置き場所の行を残さない
        self.sync()
        self.conn.execute("INSERT INTO task_overrides VALUES ('2026-11-21-csv', 's1', 'add', 'x')")
        self.conn.commit()
        a, b = self.root, os.path.join(self.d, "b")
        os.makedirs(b)
        for name in ("2026-11-20-login", "2026-11-21-csv", "shared"):
            os.rename(os.path.join(a, name), os.path.join(b, name))  # 更新時刻は変わらない
        self.sync_at(b)
        paths = [r[0] for r in self.conn.execute("SELECT path FROM documents")]
        self.assertFalse([p for p in paths if p.startswith(a + "/")], paths)
        self.assertIn(b + "/shared/notes-team.md", paths)
        self.assertIn(b + "/2026-11-20-login/research-a.md", paths)
        self.assertEqual(sorted(r[0] for r in self.conn.execute("SELECT path FROM tasks")),
                         [b + "/2026-11-20-login", b + "/2026-11-21-csv"])
        self.assertEqual(count(self.conn, "task_overrides"), 1)
        self.assertEqual(get_meta(self.conn, "tasks:root"), b)

    def test_missing_root_keeps_overrides(self):
        self.sync()
        self.conn.execute("INSERT INTO task_overrides VALUES ('2026-11-20-login', 's1', 'add', 'x')")
        self.conn.commit()
        shutil.rmtree(self.root)
        self.assertTrue(self.sync().changed)
        self.assertEqual(count(self.conn, "tasks"), 0)
        self.assertEqual(count(self.conn, "task_overrides"), 1)
        self.assertFalse(self.sync().changed)

    def test_briefly_missing_task_md_keeps_overrides(self):
        # task.md が一時的に見えないだけ（保存のときの名前の付け替えなど）では、手で直した記録を消さない
        self.sync()
        self.conn.execute("INSERT INTO task_overrides VALUES ('2026-11-21-csv', 's1', 'add', 'x')")
        self.conn.commit()
        md = self.root + "/2026-11-21-csv/task.md"
        os.rename(md, md + ".bak")
        self.assertTrue(self.sync().changed)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT name FROM tasks")], ["2026-11-20-login"])
        self.assertEqual(count(self.conn, "task_overrides"), 1)
        os.rename(md + ".bak", md)
        self.sync()
        self.assertEqual(count(self.conn, "task_overrides"), 1)
        self.assertIn("2026-11-21-csv", [r[0] for r in self.conn.execute("SELECT name FROM tasks")])

    def test_failed_listing_deletes_nothing_and_reports(self):
        self.sync()
        self.conn.execute("INSERT INTO task_overrides VALUES ('2026-11-20-login', 's1', 'add', 'x')")
        self.conn.commit()
        before = count(self.conn, "documents")
        with mock.patch("tsuzuki.tasks.os.scandir", side_effect=PermissionError("denied")):
            sync = self.sync()
        self.assertFalse(sync.changed)
        self.assertIn("一覧できませんでした", sync.error)
        self.assertEqual(count(self.conn, "tasks"), 2)
        self.assertEqual(count(self.conn, "task_overrides"), 1)
        self.assertEqual(count(self.conn, "documents"), before)
        self.assertIsNone(self.sync().error)

    def test_failed_listing_is_reported_by_update(self):
        fake = FakeClaude(Path(self.d) / "claude")
        ledger = Path(self.d) / "l3"
        with mock.patch("tsuzuki.tasks.os.scandir", side_effect=PermissionError("denied")):
            rep = update(self.conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home,
                         tasks_root=Path(self.root))
        self.assertTrue([e for e in rep.errors if "一覧できませんでした" in e])

    def test_refresh_task(self):
        self.sync()
        write_file(self.root + "/2026-11-20-login/task.md", "---\nstage: PR\n---\n")
        self.assertTrue(refresh_task(self.conn, self.cfg, tasks_root=self.root, home=self.home,
                                     name="2026-11-20-login"))
        self.assertEqual(self.conn.execute("SELECT stage FROM tasks WHERE name = '2026-11-20-login'").fetchone()[0], "PR")
        self.assertFalse(refresh_task(self.conn, self.cfg, tasks_root=self.root, home=self.home, name="nothing"))

    def test_refresh_task_before_sync(self):
        self.assertTrue(refresh_task(self.conn, self.cfg, tasks_root=self.root, home=self.home,
                                     name="2026-11-21-csv"))
        self.assertEqual(count(self.conn, "tasks"), 1)

    def test_update_runs_sync_only_with_tasks_root(self):
        fake = FakeClaude(Path(self.d) / "c1")
        fake.dir.mkdir(parents=True, exist_ok=True)
        update(self.conn, self.cfg, claude_dir=fake.dir, ledger_dir=Path(self.d) / "l1", home=self.home,
               tasks_root=Path(self.root))
        self.assertEqual(count(self.conn, "tasks"), 2)
        conn2 = make_ledger(Path(self.d) / "l2")
        self.addCleanup(conn2.close)
        update(conn2, self.cfg, claude_dir=fake.dir, ledger_dir=Path(self.d) / "l2", home=self.home)
        self.assertEqual(count(conn2, "tasks"), 0)

    def test_full_keeps_overrides(self):
        fake = FakeClaude(Path(self.d) / "c1")
        fake.dir.mkdir(parents=True, exist_ok=True)
        ledger = Path(self.d) / "l1"
        kw = dict(claude_dir=fake.dir, ledger_dir=ledger, home=self.home, tasks_root=Path(self.root))
        update(self.conn, self.cfg, **kw)
        self.conn.execute("INSERT INTO task_overrides VALUES ('2026-11-20-login', 's1', 'add', 'x')")
        self.conn.commit()
        update(self.conn, self.cfg, full=True, **kw)
        self.assertEqual(count(self.conn, "task_overrides"), 1)
        self.assertEqual(count(self.conn, "tasks"), 2)

    def doc_paths(self):
        return {r[0] for r in self.conn.execute("SELECT path FROM documents")}

    def test_moved_in_old_file_is_indexed(self):
        self.sync()
        src = self.d + "/outside.md"
        write_file(src, "# 古い\n")
        os.utime(src, (1000, 1000))
        for dest in (self.root + "/2026-11-20-login/design-old.md", self.root + "/shared/plan-old.md"):
            shutil.move(src, dest)
            write_file(src, "# 古い\n")
            os.utime(src, (1000, 1000))
            self.sync()
            self.assertIn(dest, self.doc_paths())

    def test_renamed_document_is_replaced(self):
        self.sync()
        old = self.root + "/2026-11-20-login/research-a.md"
        new = self.root + "/2026-11-20-login/research-b.md"
        os.rename(old, new)
        self.sync()
        self.assertNotIn(old, self.doc_paths())
        self.assertIn(new, self.doc_paths())
        old, new = self.root + "/shared/notes-team.md", self.root + "/shared/notes-all.md"
        os.rename(old, new)
        self.sync()
        self.assertNotIn(old, self.doc_paths())
        self.assertIn(new, self.doc_paths())

    def test_failed_document_is_retried(self):
        from tsuzuki import taskindex
        real = taskindex.upsert_document

        def flaky(conn, path, **kw):
            if path.endswith("research-a.md"):
                raise OSError("x")
            return real(conn, path, **kw)
        with mock.patch("tsuzuki.taskindex.upsert_document", side_effect=flaky):
            self.sync()
        self.assertNotIn(self.root + "/2026-11-20-login/research-a.md", self.doc_paths())
        self.sync()
        self.assertIn(self.root + "/2026-11-20-login/research-a.md", self.doc_paths())

    def test_failed_shared_document_is_retried(self):
        from tsuzuki import taskindex
        real = taskindex.upsert_document

        def flaky(conn, path, **kw):
            if path.endswith("notes-team.md"):
                raise ValueError("x")
            return real(conn, path, **kw)
        with mock.patch("tsuzuki.taskindex.upsert_document", side_effect=flaky):
            self.sync()
        self.assertNotIn(self.root + "/shared/notes-team.md", self.doc_paths())
        self.sync()
        self.assertIn(self.root + "/shared/notes-team.md", self.doc_paths())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root は読めてしまう")
    def test_unreadable_task_md_does_not_abort(self):
        self.sync()
        md = self.root + "/2026-11-20-login/task.md"
        write_file(md, "---\nstage: PR\n---\n")
        write_file(self.root + "/2026-11-21-csv/task.md", "---\nstage: 調整\n---\n")
        os.chmod(md, 0)
        self.addCleanup(os.chmod, md, 0o644)
        sync = self.sync()
        self.assertEqual(sync.tasks, 2)
        rows = dict(self.conn.execute("SELECT name, stage FROM tasks"))
        self.assertEqual(rows, {"2026-11-20-login": "実装", "2026-11-21-csv": "調整"})
