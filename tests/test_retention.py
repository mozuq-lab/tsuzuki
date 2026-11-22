import contextlib
import fcntl
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki import cli, ingest
from tsuzuki.indexer import update
from tsuzuki.config import Config
from tsuzuki.retention import forget, forget_undo, purge_before
from tsuzuki.search import SearchQuery, search
from tests.fixtures import FakeClaude, assistant, count, make_ledger, run_cli, sid, t, user

WAITING = "台帳の更新が終わるのを待っています"


def hrow(text, session):
    return {"display": text, "timestamp": 1795243515642, "project": "/w", "sessionId": session}


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = os.path.realpath(self._tmp.name)
        self.fake = FakeClaude(Path(self.d))
        self.ledger = Path(self.d) / "tsuzuki"
        self.conn = make_ledger(self.ledger)
        self.addCleanup(self.conn.close)
        self.home = os.path.join(self.d, "home")

    def run_update(self):
        return update(self.conn, Config(), claude_dir=self.fake.dir, ledger_dir=self.ledger, home=self.home)

    def add_session(self, n, minute):
        self.fake.write("/w", sid(n), [user("hello %d" % n, t(minute), sid(n), "/w"),
                                       assistant(t(minute + 1), sid(n), "/w", text="hi")])

    def cli_args(self, *rest):
        return ["--claude-dir", str(self.fake.dir), "--ledger-dir", str(self.ledger)] + list(rest)

    def cli_fail(self, *rest):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(self.cli_args(*rest))
        return code, err.getvalue()

    def test_purge_does_not_reimport(self):
        self.add_session(1, 0)
        self.fake.history([hrow("入力", sid(1))])
        self.run_update()
        self.assertEqual(count(self.conn, "history_prompts"), 1)
        self.assertEqual(purge_before(self.conn, "2026-11-02", now_iso=t(0)), 1)
        self.run_update()
        self.assertEqual(count(self.conn, "sessions"), 0)
        self.assertEqual(count(self.conn, "history_prompts"), 0)
        r = self.conn.execute("SELECT reason FROM tombstones WHERE session_id = ?", (sid(1),)).fetchone()
        self.assertEqual(r[0], "purge")

    def test_purge_keeps_newer(self):
        self.add_session(1, 0)
        self.add_session(2, 3 * 24 * 60)
        self.run_update()
        self.assertEqual(purge_before(self.conn, "2026-11-02", now_iso=t(0)), 1)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM sessions")], [sid(2)])

    def test_purge_invalid_date(self):
        for bad in ("2026/11/02", "11-02", "2026-13-40", "", "2026-11-02\n", "２０２６-１１-０２"):
            with self.assertRaises(ValueError):
                purge_before(self.conn, bad, now_iso=t(0))
        code, err = self.cli_fail("purge", "--before", "yesterday")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM-DD", err)

    def test_forget_and_undo(self):
        self.add_session(1, 0)
        self.run_update()
        self.assertTrue(forget(self.conn, sid(1), now_iso=t(0)))
        self.run_update()
        self.assertEqual(count(self.conn, "sessions"), 0)
        self.assertTrue(forget_undo(self.conn, sid(1)))
        self.run_update()
        self.assertEqual(count(self.conn, "sessions"), 1)
        self.conn.execute("INSERT OR REPLACE INTO sources (name, size, inode, offset) VALUES ('history', 1, 1, 1)")
        self.conn.commit()
        self.assertFalse(forget_undo(self.conn, sid(1)))
        self.assertEqual(count(self.conn, "sources"), 1 + self.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE name != 'history'").fetchone()[0])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM sources WHERE name = 'history'").fetchone())

    def test_cli_undo_without_tombstone(self):
        code, err = self.cli_fail("forget", "--undo", sid(5))
        self.assertEqual(code, 1)
        self.assertIn("取り消す記録がありません", err)

    def test_forget_unknown_returns_false_but_blocks(self):
        self.assertFalse(forget(self.conn, sid(9), now_iso=t(0)))
        self.assertEqual(count(self.conn, "tombstones"), 1)

    def test_undo_restores_history_prompts(self):
        self.add_session(1, 0)
        self.fake.history([hrow("入力", sid(1))])
        self.run_update()
        forget(self.conn, sid(1), now_iso=t(0))
        self.run_update()
        self.assertEqual(count(self.conn, "history_prompts"), 0)
        forget_undo(self.conn, sid(1))
        self.run_update()
        self.assertEqual(count(self.conn, "history_prompts"), 1)

    def test_undo_reads_history_again_without_duplicates(self):
        # 取り消した後の更新は history.jsonl を頭から読み直す。ほかのセッションの行と ID のない行を二重に入れない
        self.add_session(1, 0)
        self.add_session(2, 5)
        no_session = {"display": "ID のない入力", "timestamp": 1795243515642, "project": "/w"}
        self.fake.history([hrow("一つ目", sid(1)), hrow("二つ目", sid(2)), hrow("三つ目", sid(2)), no_session])
        self.run_update()
        everything = ["ID のない入力", "一つ目", "三つ目", "二つ目"]

        def texts():
            return sorted(r[0] for r in self.conn.execute("SELECT text FROM history_prompts"))
        self.assertEqual(texts(), everything)
        for _ in range(2):  # 消して取り消すのをくり返しても増えない
            forget(self.conn, sid(1), now_iso=t(0))
            self.run_update()
            self.assertEqual(texts(), ["ID のない入力", "三つ目", "二つ目"])
            self.assertTrue(forget_undo(self.conn, sid(1)))
            self.run_update()
            self.assertEqual(texts(), everything)
            self.run_update()
            self.assertEqual(texts(), everything)

    def test_forget_during_an_update_is_not_undone(self):
        # 更新が tombstone を確かめた後、その記録を書き込む前に forget が終わっても、更新の終わりに消す
        # （消さないと、記録を読み直してカードと本文を作り直し、tombstone があるので以後も消えない）
        self.fake.write("/w", sid(1), [user("my secret is hunter2", t(0), sid(1), "/w"),
                                       assistant(t(1), sid(1), "/w", text="hi")])
        self.fake.history([hrow("my secret is hunter2", sid(1))])
        self.run_update()
        self.fake.append("/w", sid(1), [user("続き", t(2), sid(1), "/w")])
        other = make_ledger(self.ledger)
        self.addCleanup(other.close)
        real = ingest._load_session

        def racing(conn, session_id):
            if session_id == sid(1) and not count(other, "tombstones"):
                self.assertTrue(forget(other, session_id, now_iso=t(3)))
            return real(conn, session_id)
        with mock.patch.object(ingest, "_load_session", racing):
            self.run_update()
        for _ in range(2):
            for table in ("sessions", "messages", "history_prompts"):
                self.assertEqual(count(self.conn, table), 0, table)
            self.assertEqual(count(self.conn, "tombstones"), 1)
            self.run_update()

    def test_update_removes_rows_left_for_forgotten_sessions(self):
        # tombstone があるのに台帳に残っている行（同時に動いた forget・purge の取りこぼし）は、更新が消す
        self.add_session(1, 0)
        self.add_session(2, 5)
        self.fake.history([hrow("一つ目", sid(1)), hrow("二つ目", sid(2))])
        self.run_update()
        with self.conn:
            self.conn.execute("INSERT INTO tombstones (session_id, reason, at) VALUES (?, 'forget', ?)",
                              (sid(1), t(9)))
        self.run_update()
        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM sessions")], [sid(2)])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT DISTINCT session_id FROM messages")], [sid(2)])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT text FROM history_prompts")], ["二つ目"])

    def test_cli_forget_and_purge_wait_for_an_update(self):
        # 更新の途中なら（update.lock を持たれていれば）、知らせを出して終わるのを待ち、それから消す
        self.add_session(1, 0)
        self.run_update()
        lock = os.open(str(self.ledger / "update.lock"), os.O_RDWR)
        self.addCleanup(os.close, lock)
        for args, done in ((("forget", sid(1)[:8]), "台帳から消しました"),
                           (("forget", "--undo", sid(1)), "取り消しました"),
                           (("purge", "--before", "2026-11-02"), "1件を台帳から消しました")):
            with self.subTest(args=args):
                if args[0] == "purge":
                    self.run_update()  # 取り消したセッションを取り込み直しておく
                self.assertEqual(count(self.conn, "tombstones"), 0 if args[1] != "--undo" else 1)
                fcntl.flock(lock, fcntl.LOCK_EX)
                out, err, codes = io.StringIO(), io.StringIO(), []

                def work():
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        codes.append(cli.main(self.cli_args(*args)))
                worker = threading.Thread(target=work)
                worker.start()
                try:
                    deadline = time.monotonic() + 5
                    while WAITING not in err.getvalue() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertIn(WAITING, err.getvalue())
                    time.sleep(0.2)
                    self.assertTrue(worker.is_alive())  # 更新が終わるまで、台帳に触れない
                    self.assertEqual(count(self.conn, "tombstones"), 0 if args[1] != "--undo" else 1)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    worker.join(10)
                self.assertEqual(codes, [0])
                self.assertIn(done, out.getvalue())
                self.assertEqual(count(self.conn, "tombstones"), 1 if args[1] != "--undo" else 0)
        # 更新の途中でなければ、待つ知らせは出さない
        code, err = self.cli_fail("forget", "--undo", sid(1))
        self.assertEqual(code, 0)
        self.assertNotIn(WAITING, err)

    def search_after_the_transcript_is_gone(self, retention):
        """入力履歴にもある入力の会話の記録を消して更新し、その入力を検索する（retention ごとに別の台帳で）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        fake, ledger = FakeClaude(Path(root)), Path(root) / "tsuzuki"
        conn = make_ledger(ledger)
        self.addCleanup(conn.close)
        path = fake.write("/w", sid(1), [user("引き継ぎの話", t(0), sid(1), "/w"),
                                         assistant(t(1), sid(1), "/w", text="はい")])
        # 記録が1件も見えないと「消えた」とみなさないので、残る記録を1つ置く
        fake.write("/w", sid(2), [user("別の話", t(5), sid(2), "/w")])
        fake.history([hrow("引き継ぎの話", sid(1))])
        for _ in range(2):
            update(conn, Config(retention=retention), claude_dir=fake.dir, ledger_dir=ledger,
                   home=os.path.join(root, "home"))
            if os.path.exists(str(path)):
                os.remove(str(path))
        self.assertEqual(conn.execute("SELECT transcript_exists FROM sessions").fetchone()[0], 0)
        return search(conn, SearchQuery("引き継ぎ"))

    def test_history_shows_only_inputs_whose_conversation_is_gone(self):
        # 既定（all）では記録が消えても会話が残るので、入力履歴には出さない（同じ入力を2回出さない）
        r = self.search_after_the_transcript_is_gone("all")
        self.assertEqual(([h.session_id for h in r.sessions], r.history), ([sid(1)], []))
        # card では会話の本文を消すので、入力履歴として出す
        r = self.search_after_the_transcript_is_gone("card")
        self.assertEqual(([h.session_id for h in r.sessions], [h.snippet.text for h in r.history]),
                         ([], ["引き継ぎの話"]))

    def test_cli_forget_by_prefix(self):
        self.add_session(1, 0)
        self.run_update()
        out = run_cli(self.cli_args("forget", sid(1)[:8]))
        self.assertIn("台帳から消しました", out)
        self.assertEqual(count(self.conn, "sessions"), 0)
        out = run_cli(self.cli_args("forget", "--undo", sid(1)[:8]))
        self.assertIn("取り消しました", out)

    def test_cli_purge(self):
        self.add_session(1, 0)
        self.run_update()
        out = run_cli(self.cli_args("purge", "--before", "2026-11-02"))
        self.assertIn("1件を台帳から消しました（以後は取り込みません）", out)

    def test_cli_ambiguous_missing_short(self):
        self.add_session(1, 0)
        self.add_session(2, 5)
        self.run_update()
        code, err = self.cli_fail("forget", sid(1)[:20])
        self.assertEqual(code, 1)
        self.assertIn("2件のセッションに当たります。もう少し長く指定してください", err)
        code, err = self.cli_fail("forget", "ffffffff")
        self.assertEqual(code, 1)
        self.assertIn("台帳に該当するセッションがありません: ffffffff", err)
        code, err = self.cli_fail("forget", "abc")
        self.assertEqual(code, 2)
        self.assertIn("ID は6文字以上で指定してください", err)

    def test_cli_forget_full_uuid_not_in_ledger(self):
        # 完全な ID は台帳になくても以後の取り込みを止めるが、消したとは言わない（打ち間違いに気づけるように）
        self.add_session(1, 0)
        self.run_update()
        out = run_cli(self.cli_args("forget", sid(7)))
        self.assertIn("台帳にこのセッションはありません", out)
        self.assertIn("tsuzuki forget --undo", out)
        self.assertNotIn("台帳から消しました", out)
        self.assertIn("Claude Code の記録は消していません", out)
        self.assertEqual(count(self.conn, "tombstones"), 1)
        self.assertEqual(count(self.conn, "sessions"), 1)
        out = run_cli(self.cli_args("forget", sid(1)))
        self.assertIn("台帳から消しました", out)
        self.assertNotIn("台帳にこのセッションはありません", out)


if __name__ == "__main__":
    unittest.main()
