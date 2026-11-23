import contextlib
import fcntl
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tsuzuki import cli, indexer
from tsuzuki.config import Config, DocScan
from tsuzuki.indexer import FORMAT_WARNING, discover_transcripts, update
from tsuzuki.places import TMP_PLACE, encode_project
from tsuzuki.retention import apply_max_age, apply_missing, delete_session_data
from tsuzuki.store import SCHEMA_VERSION, get_meta, set_meta
from tests.fixtures import (
    FakeClaude, allow_system_tmp, assistant, command_text, count, derived_from, downgrade_to_v1, make_git_repo,
    make_ledger, pasted, run_cli, sid, t, tool_result, user, write_file,
)

DAY = 24 * 3600
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def headless(text, ts, session, cwd):
    return user(text, ts, session, cwd, human=False, entrypoint="sdk-cli")


class IndexerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = os.path.realpath(self._tmp.name)
        self.fake = FakeClaude(Path(self.d))
        self.ledger = Path(self.d) / "tsuzuki"
        self.conn = make_ledger(self.ledger)
        self.addCleanup(self.conn.close)
        self.home = os.path.join(self.d, "home")
        self.cfg = Config()
        # tsuzuki index はホームの下のタスクの置き場所（設定 tasks_root の既定 ~/tasks）も読むので、
        # コマンドのテストが利用者の ~/tasks を読まないよう、ホームも一時ディレクトリにする
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_update(self, cfg=None, **kw):
        return update(self.conn, cfg or self.cfg, claude_dir=self.fake.dir, ledger_dir=self.ledger,
                      home=self.home, **kw)

    def cli_args(self, *rest):
        return ["--claude-dir", str(self.fake.dir), "--ledger-dir", str(self.ledger)] + list(rest)

    def fresh_ledger_with_session(self, session, records):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = FakeClaude(Path(tmp.name))
        ledger = Path(tmp.name) / "tsuzuki"
        conn = make_ledger(ledger)
        self.addCleanup(conn.close)
        return conn, fake, fake.write("/w", session, records), ledger

    def fresh_ledger(self):
        """別の一時ディレクトリに、空の偽の ~/.claude と台帳を作る。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        ledger = Path(root) / "tsuzuki"
        conn = make_ledger(ledger)
        self.addCleanup(conn.close)
        return root, FakeClaude(Path(root)), conn, ledger

    def place_path_of(self, session):
        r = self.conn.execute("SELECT p.path FROM sessions s JOIN places p ON p.id = s.place_id WHERE s.id = ?",
                              (session,)).fetchone()
        return r[0] if r else None

    def parent_of(self, child):
        r = self.conn.execute("SELECT src, confidence FROM relations WHERE dst = ? AND type = 'child'",
                              (child,)).fetchone()
        return tuple(r) if r else None

    def doc_exists(self, path):
        return self.conn.execute("SELECT 1 FROM documents WHERE path = ?", (path,)).fetchone() is not None

    def doc_place(self, path):
        r = self.conn.execute("SELECT p.path FROM documents d LEFT JOIN places p ON p.id = d.place_id"
                              " WHERE d.path = ?", (path,)).fetchone()
        self.assertIsNotNone(r, path)
        return r[0]

    def exists_flag(self, session):
        return self.conn.execute("SELECT transcript_exists FROM sessions WHERE id = ?", (session,)).fetchone()[0]

    def session_ids(self, conn=None):
        return [r[0] for r in (conn or self.conn).execute("SELECT id FROM sessions ORDER BY id")]

    def messages_of(self, session, conn=None):
        return [r[0] for r in (conn or self.conn).execute(
            "SELECT text FROM messages WHERE session_id = ? ORDER BY seq", (session,))]


    def test_end_to_end(self):
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            handoff = repo + "/docs/superpowers/handoff/h.md"
            self.fake.write(repo, sid(1), [
                user("課題を進めて", t(0), sid(1), repo),
                assistant(t(1), sid(1), repo, tools=[("Write", {"file_path": handoff})]),
                tool_result('{"session_id": "' + sid(9) + '"}', t(3), sid(1), repo),
                user("ありがとう", t(10), sid(1), repo)])
            write_file(handoff, "# 引き継ぎ\n本文")
            self.fake.write(repo + "/work", sid(9), [headless("子の課題", t(2), sid(9), repo + "/work")])
            rep = self.run_update()
            self.assertEqual((rep.files_total, rep.files_ingested, rep.files_failed), (2, 2, 0))
            self.assertEqual(self.place_path_of(sid(1)), repo)
            self.assertEqual(self.place_path_of(sid(9)), repo)
            self.assertEqual(self.parent_of(sid(9)), (sid(1), "strong"))
            self.assertEqual(rep.relations, 1)
            doc = self.conn.execute("SELECT kind, title FROM documents WHERE path = ?", (handoff,)).fetchone()
            self.assertEqual(tuple(doc), ("handoff", "引き継ぎ"))
            self.assertIsNone(rep.format_warning)
            self.assertTrue(get_meta(self.conn, "last_update_at"))
            self.assertEqual(self.run_update().files_ingested, 0)

    def test_tmp_cwd_maps_to_owner_place(self):
        repo = make_git_repo(self.d, "repo")
        owner = sid(1)
        self.fake.write(repo, owner, [user("a", t(0), owner, repo)])
        tmp_cwd = "/private/tmp/claude-501/%s/%s/scratchpad" % (encode_project(repo), owner)
        self.fake.write(tmp_cwd, sid(9), [headless("子", t(1), sid(9), tmp_cwd)])
        self.run_update()
        self.assertEqual(self.place_path_of(sid(9)), repo)

    def test_scan_cadence(self):
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            self.fake.write(repo, sid(1), [user("a", t(0), sid(1), repo)])
            clock = [0.0]
            self.run_update(now=lambda: clock[0])
            write_file(repo + "/notes/new.md", "# 新しい資料")
            clock[0] = 100.0
            self.run_update(now=lambda: clock[0])
            self.assertFalse(self.doc_exists(repo + "/notes/new.md"))
            clock[0] = 700.0
            self.run_update(now=lambda: clock[0])
            self.assertTrue(self.doc_exists(repo + "/notes/new.md"))

    def test_format_warning(self):
        self.fake.write("/w", sid(7), [{"type": "zzz-new", "sessionId": sid(7)} for _ in range(30)])
        rep = self.run_update()
        self.assertIn("記録の形式が変わった可能性があります", rep.format_warning)
        self.assertEqual(rep.format_warning, FORMAT_WARNING + "（1件のファイルで会話を読み取れませんでした）")
        self.assertEqual(get_meta(self.conn, "format_warning"), rep.format_warning)

    def test_index_cli(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        out = run_cli(self.cli_args("index"))
        self.assertIn("記録", out)
        self.assertRegex(out, r"^記録 1件（取り込み 1件）・資料 0件・つながり 0件・\d+\.\d秒\n$")
        self.assertRegex(run_cli(self.cli_args("index")), r"^記録 1件（取り込み 0件）")
        self.assertRegex(run_cli(self.cli_args("index", "--full")), r"^記録 1件（取り込み 1件）")

    def test_missing_transcript_by_policy(self):
        for policy, expect_row, expect_msgs in (("all", 1, 1), ("card", 1, 0), ("none", 0, 0)):
            with self.subTest(policy=policy):
                conn, fake, p, ledger = self.fresh_ledger_with_session(sid(1), [user("本文", t(0), sid(1), "/w")])
                # 記録が1件も見えない回は消さない決まりなので、残る記録を1つ置く
                fake.write("/w", sid(2), [user("残る", t(0), sid(2), "/w")])
                cfg = Config(retention=policy)
                update(conn, cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)
                p.unlink()
                rep = update(conn, cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)
                self.assertEqual((rep.sessions_missing, rep.source_warning), (1, None))
                self.assertEqual(self.session_ids(conn).count(sid(1)), expect_row)
                self.assertEqual(len(self.messages_of(sid(1), conn)), expect_msgs)
                self.assertEqual(self.messages_of(sid(2), conn), ["残る"])
                if expect_row:
                    self.assertEqual(conn.execute("SELECT transcript_exists FROM sessions WHERE id = ?",
                                                  (sid(1),)).fetchone()[0], 0)
                # 次の更新では「消えた」と数え直さない
                self.assertEqual(update(conn, cfg, claude_dir=fake.dir, ledger_dir=ledger,
                                        home=self.home).sessions_missing, 0)

    def test_unseen_source_does_not_delete(self):
        for policy in ("none", "card"):
            for mode in ("missing", "empty", "no-transcripts"):
                with self.subTest(policy=policy, mode=mode):
                    conn, fake, p, ledger = self.fresh_ledger_with_session(sid(1), [user("本文", t(0), sid(1), "/w")])
                    cfg = Config(retention=policy)

                    def run():
                        return update(conn, cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)

                    run()
                    projects, away = fake.dir / "projects", fake.dir / "projects.away"
                    empty = "Claude Code の記録が1件も見つかりません: %s（消えたとはみなしていません）" % projects
                    if mode == "no-transcripts":
                        # 場所ごとのディレクトリは残り、記録のファイルだけが1件も見えない
                        os.rename(str(p), str(fake.dir / "away.jsonl"))
                        expected = empty
                    else:
                        os.rename(str(projects), str(away))  # 外付けのディスクが外れたときなど
                        if mode == "empty":
                            projects.mkdir()
                            expected = empty
                        else:
                            expected = "Claude Code の記録の場所が見つかりません: %s" % projects
                    rep = run()
                    self.assertEqual(rep.source_warning, expected)
                    self.assertEqual(get_meta(conn, "source_warning"), expected)
                    self.assertEqual(rep.sessions_missing, 0)
                    self.assertEqual(self.session_ids(conn), [sid(1)])
                    self.assertEqual(self.messages_of(sid(1), conn), ["本文"])
                    self.assertEqual(conn.execute("SELECT transcript_exists FROM sessions").fetchone()[0], 1)
                    # 戻れば、ふつうの更新で知らせが消える
                    if mode == "no-transcripts":
                        os.rename(str(fake.dir / "away.jsonl"), str(p))
                    else:
                        if mode == "empty":
                            projects.rmdir()
                        os.rename(str(away), str(projects))
                    rep = run()
                    self.assertIsNone(rep.source_warning)
                    self.assertEqual(get_meta(conn, "source_warning"), "")

    def test_full_rebuild_needs_visible_source(self):
        for mode in ("missing", "empty"):
            with self.subTest(mode=mode):
                conn, fake, p, ledger = self.fresh_ledger_with_session(sid(1), [user("本文", t(0), sid(1), "/w")])
                update(conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)
                projects = fake.dir / "projects"
                os.rename(str(projects), str(fake.dir / "projects.away"))
                if mode == "empty":
                    projects.mkdir()
                    expected = "Claude Code の記録が1件も見つかりません: %s（消えたとはみなしていません）" % projects
                    # 記録が消えたとしてあるセッションしかなくても、作り直しはしない
                    with conn:
                        conn.execute("UPDATE sessions SET transcript_exists = 0")
                else:
                    expected = "Claude Code の記録の場所が見つかりません: %s" % projects
                rep = update(conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home, full=True)
                self.assertEqual(rep.source_warning, expected)
                self.assertEqual(get_meta(conn, "source_warning"), expected)
                self.assertEqual(self.session_ids(conn), [sid(1)])
                self.assertEqual(self.messages_of(sid(1), conn), ["本文"])

    def test_max_age_runs_when_source_unseen(self):
        cfg = Config(max_age_days=30)
        start = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
        p = self.fake.write("/w", sid(1), [user("古い", t(0), sid(1), "/w")])
        os.utime(str(p), (start, start))
        self.run_update(cfg, now=lambda: start + DAY)
        self.assertEqual(self.session_ids(), [sid(1)])
        os.rename(str(self.fake.dir / "projects"), str(self.fake.dir / "projects.away"))
        rep = self.run_update(cfg, now=lambda: start + 31 * DAY)
        self.assertIsNotNone(rep.source_warning)
        self.assertEqual(self.session_ids(), [])  # 期限は記録の有無によらず当てる

    def test_missing_flags_and_policy_commit_together(self):
        cfg = Config(retention="card")
        p = self.fake.write("/w", sid(1), [user("本文", t(0), sid(1), "/w")])
        self.fake.write("/w", sid(2), [user("残る", t(0), sid(2), "/w")])
        self.run_update(cfg)
        p.unlink()
        with mock.patch.object(indexer, "apply_missing", side_effect=RuntimeError("中断")):
            with self.assertRaises(RuntimeError):
                self.run_update(cfg)
        self.assertEqual(self.exists_flag(sid(1)), 1)  # 途中で止まったら、記録の有無も変えない
        rep = self.run_update(cfg)  # 次の回にやり直せる
        self.assertEqual(rep.sessions_missing, 1)
        self.assertEqual(self.messages_of(sid(1)), [])

    @unittest.skipIf(AS_ROOT, "root は読み取りの権限を無視する")
    def test_unreadable_project_dir_is_not_missing(self):
        cfg = Config(retention="none")
        p = self.fake.write("/w", sid(1), [user("本文", t(0), sid(1), "/w")])
        self.fake.write("/other", sid(2), [user("残る", t(0), sid(2), "/other")])
        self.run_update(cfg)
        os.chmod(str(p.parent), 0)
        try:
            rep = self.run_update(cfg)
        finally:
            os.chmod(str(p.parent), 0o700)
        self.assertEqual((rep.files_total, rep.sessions_missing), (1, 0))
        self.assertEqual(self.session_ids(), [sid(1), sid(2)])
        self.assertEqual(self.messages_of(sid(1)), ["本文"])
        self.assertEqual(self.exists_flag(sid(1)), 1)

    def test_deleted_project_dir_counts_as_missing(self):
        cfg = Config(retention="none")
        p = self.fake.write("/w", sid(1), [user("本文", t(0), sid(1), "/w")])
        self.fake.write("/other", sid(2), [user("残る", t(0), sid(2), "/other")])
        self.run_update(cfg)
        shutil.rmtree(str(p.parent))  # Claude Code の片づけで、場所ごとのディレクトリごと消えた
        rep = self.run_update(cfg)
        self.assertEqual(rep.sessions_missing, 1)
        self.assertEqual(self.session_ids(), [sid(2)])

    def test_transcript_gone_rules(self):
        projects = self.fake.dir / "projects"
        projects.mkdir()
        gone = str(projects / "-gone" / "x.jsonl")
        # ファイルがなく projects が見えれば消えた（場所ごとのディレクトリがなくても）
        self.assertTrue(indexer._transcript_gone(gone, projects))
        # projects そのものが見えないときは、消えたとみなさない
        self.assertFalse(indexer._transcript_gone(gone, Path(self.d) / "nowhere" / "projects"))
        there = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.assertFalse(indexer._transcript_gone(str(there), projects))

    def test_card_restored_transcript_is_read_again(self):
        cfg = Config(retention="card")
        p = self.fake.write("/w", sid(1), [user("本文", t(0), sid(1), "/w"),
                                           assistant(t(1), sid(1), "/w", text="応答")])
        self.fake.write("/w", sid(2), [user("残る", t(0), sid(2), "/w")])
        self.run_update(cfg)
        away = p.with_name("away.bak")
        os.rename(str(p), str(away))
        self.run_update(cfg)
        self.assertEqual(self.messages_of(sid(1)), [])
        os.rename(str(away), str(p))  # 同じファイルを戻す（大きさ・更新時刻・inode も同じ）
        rep = self.run_update(cfg)
        self.assertEqual(rep.files_ingested, 1)
        self.assertEqual(self.messages_of(sid(1)), ["本文", "応答"])
        self.assertEqual(self.exists_flag(sid(1)), 1)
        self.assertEqual(self.run_update(cfg).files_ingested, 0)  # 読み直しは1回だけ

    @unittest.skipIf(AS_ROOT, "root は読み取りの権限を無視する")
    def test_card_restored_but_unreadable_is_read_once_readable(self):
        cfg = Config(retention="card")
        p = self.fake.write("/w", sid(1), [user("本文", t(0), sid(1), "/w")])
        self.fake.write("/w", sid(2), [user("残る", t(0), sid(2), "/w")])
        self.run_update(cfg)
        away = p.with_name("away.bak")
        os.rename(str(p), str(away))
        self.run_update(cfg)
        os.rename(str(away), str(p))
        os.chmod(str(p), 0)
        try:
            rep = self.run_update(cfg)
        finally:
            os.chmod(str(p), 0o600)
        self.assertEqual(rep.files_failed, 1)
        self.assertEqual(self.exists_flag(sid(1)), 1)  # ファイルはある
        self.run_update(cfg)
        self.assertEqual(self.messages_of(sid(1)), ["本文"])

    def test_max_age_days(self):
        cfg = Config(max_age_days=30)
        start = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
        p = self.fake.write("/w", sid(1), [user("古い", t(0), sid(1), "/w")])
        os.utime(p, (start + 30 * DAY, start + 30 * DAY))  # ファイルは新しいが、中身は期限より前
        self.fake.write("/w", sid(2), [user("新しい", t(2 * 24 * 60), sid(2), "/w")])
        rep = self.run_update(cfg, now=lambda: start + 31 * DAY)
        self.assertEqual(rep.files_ingested, 2)
        self.assertEqual(self.session_ids(), [sid(2)])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT DISTINCT session_id FROM messages")], [sid(2)])

    # 決めごと 10 のテスト

    @unittest.skipIf(AS_ROOT, "root は読み取りの権限を無視する")
    def test_unreadable_file_is_counted_and_others_continue(self):
        bad = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.fake.write("/w", sid(2), [user("b", t(0), sid(2), "/w")])
        os.chmod(bad, 0)
        try:
            rep = self.run_update()
        finally:
            os.chmod(bad, 0o600)
        self.assertEqual((rep.files_total, rep.files_ingested, rep.files_failed), (2, 1, 1))
        self.assertEqual(self.session_ids(), [sid(2)])
        rep = self.run_update()
        self.assertEqual((rep.files_ingested, rep.files_failed), (1, 0))
        self.assertEqual(self.session_ids(), [sid(1), sid(2)])

    def test_restored_transcript_is_marked_present(self):
        p = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.fake.write("/w", sid(2), [user("b", t(0), sid(2), "/w")])  # 記録の場所が空にならないように
        self.run_update()
        away = p.with_name("away.bak")
        os.rename(str(p), str(away))
        rep = self.run_update()
        self.assertEqual((rep.sessions_missing, self.exists_flag(sid(1))), (1, 0))
        os.rename(str(away), str(p))  # 大きさ・更新時刻・inode が同じまま戻る
        rep = self.run_update()
        self.assertEqual(rep.sessions_missing, 0)
        self.assertEqual(self.exists_flag(sid(1)), 1)
        self.assertEqual(self.messages_of(sid(1)), ["a"])  # 読み直しても二重にならない

    # 更新の流れの細部

    def test_transcript_outside_claude_dir_is_not_missing(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.run_update()
        other = FakeClaude(Path(self.d) / "other")
        other.write("/w", sid(2), [user("b", t(0), sid(2), "/w")])
        rep = update(self.conn, self.cfg, claude_dir=other.dir, ledger_dir=self.ledger, home=self.home)
        self.assertEqual((rep.files_total, rep.sessions_missing, rep.source_warning), (1, 0, None))
        self.assertEqual(self.exists_flag(sid(1)), 1)

    def test_relative_claude_dir_stores_absolute_paths(self):
        p = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        update(self.conn, self.cfg, claude_dir=Path(os.path.relpath(str(self.fake.dir))), ledger_dir=self.ledger,
               home=self.home)
        stored = self.conn.execute("SELECT transcript_path FROM sessions").fetchone()[0]
        self.assertEqual(os.path.realpath(stored), os.path.realpath(str(p)))
        self.assertTrue(os.path.isabs(stored))

    def test_moved_transcript_updates_path(self):
        p = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.run_update()
        moved = self.fake.dir / "projects" / "-moved" / p.name
        moved.parent.mkdir()
        os.rename(str(p), str(moved))
        rep = self.run_update()
        self.assertEqual((rep.files_ingested, rep.sessions_missing), (0, 0))
        row = self.conn.execute("SELECT transcript_path, transcript_exists FROM sessions").fetchone()
        self.assertEqual(tuple(row), (str(moved), 1))

    def test_discover_transcripts_skips_subagents(self):
        p = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        for sub in ("subagents", "tool-results"):
            d = p.parent / sid(1) / sub
            d.mkdir(parents=True)
            (d / "agent-a1.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertEqual(discover_transcripts(self.fake.dir), [p])
        self.assertEqual(discover_transcripts(Path(self.d) / "nothing"), [])

    def test_max_age_skips_old_files(self):
        now = datetime(2026, 11, 21, tzinfo=timezone.utc).timestamp()
        old = self.fake.write("/w", sid(1), [user("古い", t(0), sid(1), "/w")])
        new = self.fake.write("/w", sid(2), [user("新しい", t(0), sid(2), "/w")])
        os.utime(old, (now - 40 * DAY, now - 40 * DAY))
        os.utime(new, (now - DAY, now - DAY))
        rep = self.run_update(Config(max_age_days=30), now=lambda: now)
        self.assertEqual((rep.files_total, rep.files_ingested), (2, 1))
        self.assertEqual(self.session_ids(), [sid(2)])

    def test_progress(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.fake.write("/w", sid(2), [user("b", t(0), sid(2), "/w")])
        seen = []
        self.run_update(progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_full_rebuild(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.fake.write("/w", sid(2), [user("b", t(0), sid(2), "/w")])
        self.run_update()
        with self.conn:
            self.conn.execute("INSERT INTO tombstones(session_id, reason, at) VALUES (?, 'forget', ?)", (sid(2), t(0)))
            self.conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text)"
                              " VALUES ('stale', 0, 0, NULL, 'user', '古い行')")
            self.conn.execute("INSERT INTO derived_from(session_id, source_id, via) VALUES ('stale', ?, 'fork')",
                              (sid(1),))
            set_meta(self.conn, "scan:999", "1")
        rep = self.run_update(full=True)
        self.assertEqual(rep.files_ingested, 1)  # 消したセッションは取り込まない
        self.assertEqual(self.session_ids(), [sid(1)])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT DISTINCT session_id FROM messages")], [sid(1)])
        self.assertEqual(count(self.conn, "derived_from"), 0)
        self.assertEqual(count(self.conn, "tombstones"), 1)
        self.assertIsNone(get_meta(self.conn, "scan:999"))
        self.assertEqual(get_meta(self.conn, "schema_version"), str(SCHEMA_VERSION))
        if get_meta(self.conn, "fts") == "trigram":
            self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('integrity-check')")
        self.assertIsNotNone(get_meta(self.conn, "fts"))

    def test_update_lock(self):
        self.run_update()
        lock = self.ledger / "update.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        done, errors = threading.Event(), []

        def work():
            try:
                self.run_update()
            except Exception as e:  # スレッドの中の失敗をテストに伝える
                errors.append(e)
            done.set()

        fd = os.open(str(lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            th = threading.Thread(target=work)
            th.start()
            self.assertFalse(done.wait(0.3))  # 別の更新が終わるまで待つ
            self.assertEqual(count(self.conn, "sessions"), 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        th.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(done.is_set())
        self.assertEqual(count(self.conn, "sessions"), 1)

    def test_tmp_cwd_rules(self):
        repo = make_git_repo(self.d, "repo")
        other = make_git_repo(self.d, "other")
        # 持ち主が台帳になく、符号化した名前に当たる場所もない → 「一時」
        tmp1 = "/private/tmp/claude-501/%s/%s/scratchpad" % (encode_project(other), sid(1))
        self.fake.write(tmp1, sid(8), [headless("子1", t(1), sid(8), tmp1)])
        self.run_update()
        self.assertEqual(self.place_path_of(sid(8)), TMP_PLACE)
        self.assertEqual(self.conn.execute("SELECT display FROM places WHERE path = ?", (TMP_PLACE,)).fetchone()[0],
                         "一時")
        # 持ち主が現れたら移る。符号化した名前（other）より持ち主の場所（repo）が先
        self.fake.write(repo, sid(1), [user("親", t(0), sid(1), repo)])
        self.fake.write(other, sid(2), [user("別", t(0), sid(2), other)])
        # 持ち主が台帳にないときは、符号化した名前が一致する既知の場所
        tmp2 = "/private/tmp/claude-501/%s/%s/scratchpad" % (encode_project(other), sid(5))
        self.fake.write(tmp2, sid(9), [headless("子2", t(2), sid(9), tmp2)])
        self.run_update()
        self.assertEqual(self.place_path_of(sid(8)), repo)
        self.assertEqual(self.place_path_of(sid(9)), other)

    def test_place_resolution_cached(self):
        repo = make_git_repo(self.d, "repo")
        for n in (1, 2):
            self.fake.write(repo + "/sub", sid(n), [user("a", t(0), sid(n), repo + "/sub")])
        with mock.patch.object(indexer, "resolve_place", wraps=indexer.resolve_place) as rp:
            self.run_update()
        self.assertEqual(len([c for c in rp.call_args_list if c[0][0] == repo + "/sub"]), 1)
        self.assertEqual((self.place_path_of(sid(1)), self.place_path_of(sid(2))), (repo, repo))

    def test_place_kept_when_directory_disappears(self):
        repo = make_git_repo(self.d, "repo")
        self.fake.write(repo + "/sub", sid(1), [user("a", t(0), sid(1), repo + "/sub")])
        self.run_update()
        os.rmdir(os.path.join(repo, ".git"))
        self.fake.append(repo + "/sub", sid(1), [user("b", t(1), sid(1), repo + "/sub")])
        self.assertEqual(self.run_update().files_ingested, 1)
        self.assertEqual(self.place_path_of(sid(1)), repo)

    def test_display_follows_config(self):
        repo = make_git_repo(self.d, "repo")
        self.fake.write(repo, sid(1), [user("a", t(0), sid(1), repo)])
        self.run_update()

        def display():
            return self.conn.execute("SELECT display FROM places WHERE path = ?", (repo,)).fetchone()[0]

        self.assertEqual(display(), repo)
        self.run_update(Config(display_strip_prefixes=[self.d]))
        self.assertEqual(display(), "repo")

    def test_history_and_lineage(self):
        repo = make_git_repo(self.d, "repo")
        self.fake.write(repo, sid(1), [user("親", t(0), sid(1), repo)])
        self.fake.write(repo, sid(9), [headless("子", t(1), sid(9), repo)])
        tmp = "/private/tmp/claude-501/-x/%s/scratchpad" % sid(1)
        self.fake.history([
            {"display": "入力", "timestamp": 1795243515642, "project": repo + "/sub", "sessionId": sid(1)},
            {"display": "一時の入力", "timestamp": 1795243515643, "project": tmp, "sessionId": sid(3)},
            {"display": "消した", "timestamp": 1795243515644, "project": repo, "sessionId": sid(4)}])
        with self.conn:
            self.conn.execute("INSERT INTO tombstones(session_id, reason, at) VALUES (?, 'forget', ?)", (sid(4), t(0)))
        (self.ledger / "lineage.jsonl").write_text(
            json.dumps({"session_id": sid(9), "parent_id": sid(1), "source": "startup"}) + "\n", encoding="utf-8")
        self.run_update()
        rows = self.conn.execute("SELECT h.text, p.path FROM history_prompts h LEFT JOIN places p"
                                 " ON p.id = h.place_id ORDER BY h.id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("入力", repo), ("一時の入力", None)])
        self.assertEqual(self.parent_of(sid(9)), (sid(1), "confirmed"))

    def test_derived_from_and_hook_give_one_confirmed_parent(self):
        repo = make_git_repo(self.d, "repo")
        self.fake.write(repo, sid(1), [user("親の依頼", t(0), sid(1), repo),
                                       assistant(t(1), sid(1), repo, text="子を起動します")])
        self.fake.write(repo, sid(9), [derived_from(sid(9), [(sid(1), "subprocess")]),
                                       headless("子の課題", t(2), sid(9), repo)])
        (self.ledger / "lineage.jsonl").write_text(
            json.dumps({"session_id": sid(9), "parent_id": sid(1), "source": "startup"}) + "\n", encoding="utf-8")
        rep = self.run_update()
        self.assertEqual(rep.relations, 1)
        self.assertIsNone(rep.format_warning)
        row = self.conn.execute("SELECT src, type, confidence, evidence FROM relations WHERE dst = ?", (sid(9),))
        self.assertEqual([tuple(r) for r in row], [(sid(1), "child", "confirmed", "記録の derived-from（subprocess）")])

    def test_version_1_ledger_is_upgraded_and_updated(self):
        root, fake, conn, ledger = self.fresh_ledger()
        fake.write("/w", sid(1), [user("前の版で取り込んだ", t(0), sid(1), "/w")])
        update(conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)
        downgrade_to_v1(conn)
        conn.close()
        conn = make_ledger(ledger)
        self.addCleanup(conn.close)
        self.assertEqual(get_meta(conn, "schema_version"), str(SCHEMA_VERSION))
        fake.append("/w", sid(1), [user(command_text("web-bug-audit"), t(1), sid(1), "/w")])
        fake.write("/w", sid(2), [derived_from(sid(2), [(sid(1), "branch")]),
                                  user(pasted("貼った文"), t(2), sid(2), "/w"), assistant(t(3), sid(2), "/w", text="x")])
        rep = update(conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)
        self.assertEqual((rep.files_ingested, rep.files_failed), (2, 0))
        pending = conn.execute("SELECT pending_commands FROM sessions WHERE id = ?", (sid(1),)).fetchone()[0]
        self.assertEqual(json.loads(pending), [["/web-bug-audit", t(1), True]])
        self.assertEqual(tuple(conn.execute("SELECT kind, first_prompt FROM sessions WHERE id = ?", (sid(2),)).fetchone()),
                         ("interactive", "貼った文"))
        self.assertEqual([tuple(r) for r in conn.execute("SELECT src, dst, type FROM relations")],
                         [(sid(1), sid(2), "fork")])

    def test_upgrade_rereads_existing_transcripts_in_place(self):
        # 版を上げた後の最初の更新で、記録が残っているセッションを新しい読み方で読み直す（--full は要らない）。
        # 記録が消えたセッションは残し、全体の集計（format_stats・読めない行）は数え直さない
        root, fake, conn, ledger = self.fresh_ledger()
        fake.write("/w", sid(1), [user("親の依頼", t(0), sid(1), "/w"), assistant(t(1), sid(1), "/w", text="a")])
        p2 = fake.write("/w", sid(2), [derived_from(sid(2), [(sid(1), "branch")]),
                                       user(pasted("貼り付けだけの依頼"), t(2), sid(2), "/w"),
                                       assistant(t(3), sid(2), "/w", text="b")])
        with open(p2, "a") as f:
            f.write("{not json\n")
        p3 = fake.write("/w", sid(3), [user("記録が消える会話", t(4), sid(3), "/w"),
                                       assistant(t(5), sid(3), "/w", text="c")])
        run = lambda: update(conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)
        run()
        os.remove(str(p3))
        run()
        stats = lambda: sorted(tuple(r) for r in conn.execute("SELECT version, record_type, count FROM format_stats"))
        before = (stats(), get_meta(conn, "unreadable_lines"))
        self.assertEqual(before[1], "1")
        # 版1の読み方で取り込んだ形にする（sid(2) は空、derived_from の表はない）
        downgrade_to_v1(conn)
        with conn:
            conn.execute("UPDATE sessions SET kind = 'empty', n_prompts = 0, turn = 0, first_prompt = NULL, "
                         "last_prompt = NULL WHERE id = ?", (sid(2),))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid(2),))
        conn.close()
        conn = make_ledger(ledger)
        self.addCleanup(conn.close)
        # 開いただけでは読み直さない（読み直すのは次の更新）
        self.assertEqual(get_meta(conn, "reread_transcripts"), "1")
        self.assertEqual(conn.execute("SELECT kind FROM sessions WHERE id = ?", (sid(2),)).fetchone()[0], "empty")
        rep = run()
        self.assertEqual((rep.files_ingested, rep.files_failed, rep.sessions_missing), (2, 0, 0))
        self.assertEqual(tuple(conn.execute("SELECT kind, first_prompt FROM sessions WHERE id = ?", (sid(2),)).fetchone()),
                         ("interactive", "貼り付けだけの依頼"))
        self.assertEqual([tuple(r) for r in conn.execute("SELECT src, dst, type FROM relations")],
                         [(sid(1), sid(2), "fork")])
        gone = conn.execute("SELECT transcript_exists, n_prompts FROM sessions WHERE id = ?", (sid(3),)).fetchone()
        self.assertEqual(tuple(gone), (0, 1))
        self.assertEqual(self.messages_of(sid(3), conn), ["記録が消える会話", "c"])
        self.assertEqual((stats(), get_meta(conn, "unreadable_lines")), before)
        self.assertIsNone(get_meta(conn, "reread_transcripts"))
        self.assertEqual(run().files_ingested, 0)

    def test_reread_mark_counts_only_lines_added_since(self):
        # 読み直しの印が付いた後に追記された行は、全体の集計に数える
        root, fake, conn, ledger = self.fresh_ledger()
        fake.write("/w", sid(1), [user("一", t(0), sid(1), "/w")])
        run = lambda: update(conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home)
        run()
        with conn:
            set_meta(conn, "reread_transcripts", "1")
        fake.append("/w", sid(1), [user("二", t(1), sid(1), "/w")])
        self.assertEqual(run().files_ingested, 1)
        self.assertEqual(conn.execute("SELECT count FROM format_stats WHERE record_type = 'user'").fetchone()[0], 2)
        self.assertEqual(self.messages_of(sid(1), conn), ["一", "二"])

    def test_index_history_off_drops_rows(self):
        self.fake.history([{"display": "入力", "timestamp": 1795243515642, "project": "/w", "sessionId": sid(1)}])
        self.run_update()
        self.assertEqual(count(self.conn, "history_prompts"), 1)
        self.run_update(Config(index_history=False))
        self.assertEqual(count(self.conn, "history_prompts"), 0)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sources WHERE name = 'history'").fetchone())
        self.run_update()  # 戻せば頭から読み直す
        self.assertEqual(count(self.conn, "history_prompts"), 1)

    def test_document_places_under_claude_dir(self):
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            memory = os.path.join(self.home, ".claude", "projects", encode_project(repo), "memory", "m.md")
            plan = os.path.join(self.home, ".claude", "plans", "p.md")
            for p in (memory, plan):
                write_file(p, "# 資料")
            writes = [("Write", {"file_path": p}) for p in (memory, plan)]
            self.fake.write(repo, sid(1), [user("a", t(0), sid(1), repo), assistant(t(1), sid(1), repo, tools=writes)])
            self.run_update()
            self.assertEqual(self.doc_place(memory), repo)
            self.assertIsNone(self.doc_place(plan))

    def test_touched_document_in_repo_without_sessions(self):
        # ~ で動いたセッションが、セッションのないリポジトリ ~/projB の資料を書いた。
        # 書いた資料なので、そのディレクトリの場所（projB）を作る
        with allow_system_tmp():
            projb = make_git_repo(self.home, "projB")
            readme = os.path.join(projb, "README.md")
            write_file(readme, "# B")
            self.fake.write(self.home, sid(1), [
                user("a", t(0), sid(1), self.home),
                assistant(t(1), sid(1), self.home, tools=[("Write", {"file_path": readme})])])
            self.run_update()
            self.assertEqual(self.place_path_of(sid(1)), self.home)
            self.assertEqual(self.doc_place(readme), projb)
            self.assertEqual(sorted(r[0] for r in self.conn.execute("SELECT path FROM places")),
                             sorted([self.home, projb]))

    def test_read_document_creates_no_place(self):
        # 読んだだけ・名指ししただけの資料からは場所を作らない
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            pdf = os.path.join(self.home, "Downloads", "x.pdf")
            named = os.path.join(self.home, "notes", "plan.md")
            other_doc = os.path.join(make_git_repo(self.d, "other"), "README.md")  # セッションのないリポジトリ
            for p, text in ((pdf, "%PDF-1.4"), (named, "# 計画"), (other_doc, "# other")):
                write_file(p, text)
            reads = [("Read", {"file_path": pdf}), ("Read", {"file_path": other_doc})]
            self.fake.write(repo, sid(1), [user("~/notes/plan.md を見て", t(0), sid(1), repo),
                                           assistant(t(1), sid(1), repo, tools=reads)])
            self.run_update()
            self.assertIsNone(self.doc_place(pdf))
            self.assertIsNone(self.doc_place(named))
            self.assertIsNone(self.doc_place(other_doc))  # リポジトリでも、読んだだけなら場所を作らない
            self.assertEqual([r[0] for r in self.conn.execute("SELECT path FROM places")], [repo])
            # すでにある場所の資料なら、読んだだけでもその場所にする
            in_repo = os.path.join(repo, "docs", "r.md")
            write_file(in_repo, "# r")
            self.fake.append(repo, sid(1), [assistant(t(2), sid(1), repo, tools=[("Read", {"file_path": in_repo})])])
            self.run_update()
            self.assertEqual(self.doc_place(in_repo), repo)

    def test_document_in_non_git_place_subdir(self):
        # Git の外の作業ディレクトリのセッションが、その下の docs/ に書いた資料は、作業ディレクトリの場所に入る
        with allow_system_tmp():
            lab = os.path.join(self.d, "lab", "weather")
            doc = os.path.join(lab, "docs", "06-operations.md")
            write_file(doc, "# 運用")
            self.fake.write(lab, sid(1), [user("a", t(0), sid(1), lab),
                                          assistant(t(1), sid(1), lab, tools=[("Write", {"file_path": doc})])])
            self.run_update()
            self.assertEqual(self.doc_place(doc), lab)
            self.assertEqual([r[0] for r in self.conn.execute("SELECT path FROM places")], [lab])

    def test_nested_non_git_places_keep_document(self):
        # Git の外の場所 outer と、その中の場所 outer/inner。outer/inner/sub/y.md は、どちらの場所が
        # 先にできても、どちらだけを走査しても、いつも outer/inner に入る
        for first in ("outer", "inner"):
            with self.subTest(first=first), allow_system_tmp():
                root, fake, conn, ledger = self.fresh_ledger()
                outer = os.path.join(root, "outer")
                inner = os.path.join(outer, "inner")
                y = os.path.join(inner, "sub", "y.md")
                write_file(y, "# y")
                sessions = {outer: sid(1), inner: sid(2)}
                clock = [0.0]

                def run(at):
                    clock[0] = at
                    update(conn, self.cfg, claude_dir=fake.dir, ledger_dir=ledger, home=self.home,
                           now=lambda: clock[0])
                    r = conn.execute("SELECT p.path FROM documents d LEFT JOIN places p ON p.id = d.place_id"
                                     " WHERE d.path = ?", (y,)).fetchone()
                    return r[0] if r else None

                def start(cwd, minute):
                    fake.write(cwd, sessions[cwd], [user("開始", t(minute), sessions[cwd], cwd)])

                def touch(cwd, minute):
                    fake.append(cwd, sessions[cwd], [user("続き", t(minute), sessions[cwd], cwd)])

                one, other = (outer, inner) if first == "outer" else (inner, outer)
                start(one, 0)
                self.assertEqual(run(0.0), one)  # まだ一方しか場所がない
                start(other, 1)
                self.assertEqual(run(100.0), inner)  # 新しくできた場所だけを走査
                touch(outer, 2)
                self.assertEqual(run(200.0), inner)  # outer だけを走査
                touch(inner, 3)
                self.assertEqual(run(300.0), inner)  # inner だけを走査
                self.assertEqual(run(1000.0), inner)  # 両方を走査

    def test_read_document_goes_to_containing_non_git_place(self):
        # ~ が場所なら、~/Downloads の読んだだけの資料は ~（Git の外の、含む場所のうちいちばん深いもの）
        with allow_system_tmp():
            pdf = os.path.join(self.home, "Downloads", "x.pdf")
            write_file(pdf, "%PDF-1.4")
            self.fake.write(self.home, sid(1), [
                user("a", t(0), sid(1), self.home),
                assistant(t(1), sid(1), self.home, tools=[("Read", {"file_path": pdf})])])
            self.run_update()
            self.assertEqual(self.doc_place(pdf), self.home)
            self.assertEqual([r[0] for r in self.conn.execute("SELECT path FROM places")], [self.home])

    def test_alias_anchors_document_place(self):
        # エイリアスで決まる根は、Git でなくても根とみなす（書いた資料なら、その場所を作る）。
        # 含む場所 project-x は根 proj と名前の頭が同じだが、パスの区切りで比べるので proj の中ではない
        with allow_system_tmp():
            px = os.path.join(self.d, "project-x")  # Git の外の場所
            scratch, proj = os.path.join(px, "scratch"), os.path.join(self.d, "proj")
            doc = os.path.join(scratch, "docs", "a.md")
            write_file(doc, "# a")
            self.fake.write(px, sid(1), [user("a", t(0), sid(1), px),
                                         assistant(t(1), sid(1), px, tools=[("Write", {"file_path": doc})])])
            self.run_update(Config(place_aliases={scratch: proj}))
            self.assertEqual(self.place_path_of(sid(1)), px)
            self.assertEqual(self.doc_place(doc), proj)

    def test_deeper_place_inside_repo_wins(self):
        # リポジトリ repo の中に、エイリアスでできた場所 repo/sub がある。repo/sub の資料は repo/sub に入る
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            sub, elsewhere = os.path.join(repo, "sub"), os.path.join(self.d, "elsewhere")
            doc = os.path.join(sub, "docs", "a.md")
            write_file(doc, "# a")
            self.fake.write(repo, sid(1), [user("a", t(0), sid(1), repo)])
            self.fake.write(elsewhere, sid(2), [user("b", t(0), sid(2), elsewhere)])
            self.run_update(Config(place_aliases={elsewhere: sub}))
            self.assertEqual((self.place_path_of(sid(1)), self.place_path_of(sid(2))), (repo, sub))
            self.assertEqual(self.doc_place(doc), sub)

    def test_scanned_document_in_unplaced_nested_repo(self):
        # Git の外の場所の中にある、場所になっていないリポジトリの資料は、走査で見つけても場所なし
        with allow_system_tmp():
            outer = os.path.join(self.d, "outer")
            inner = make_git_repo(outer, "inner")
            doc = os.path.join(inner, "docs", "b.md")
            write_file(doc, "# b")
            self.fake.write(outer, sid(1), [user("a", t(0), sid(1), outer)])
            self.run_update()
            self.assertIsNone(self.doc_place(doc))
            self.assertEqual([r[0] for r in self.conn.execute("SELECT path FROM places")], [outer])

    def test_scanned_documents_keep_nested_place(self):
        with allow_system_tmp():
            outer = os.path.join(self.d, "outer")  # Git の外の場所
            inner = make_git_repo(outer, "inner")  # その中のリポジトリ（書いた資料で場所になる）
            a, b = os.path.join(outer, "notes", "a.md"), os.path.join(inner, "docs", "b.md")
            write_file(a, "# a")
            write_file(b, "# b")
            self.fake.write(outer, sid(1), [user("a", t(0), sid(1), outer),
                                            assistant(t(1), sid(1), outer, tools=[("Write", {"file_path": b})])])
            clock = [0.0]
            self.run_update(now=lambda: clock[0])
            self.assertEqual(self.doc_place(a), outer)  # a を含む場所のうち、いちばん深いもの
            self.assertEqual(self.doc_place(b), inner)
            # outer だけを走査し直しても、inner の資料は inner のまま
            clock[0] = 100.0
            self.fake.append(outer, sid(1), [user("続き", t(2), sid(1), outer)])
            self.run_update(now=lambda: clock[0])
            self.assertEqual(self.doc_place(b), inner)
            self.assertEqual(sorted(r[0] for r in self.conn.execute("SELECT path FROM places")), sorted([inner, outer]))

    def test_scan_when_place_session_moves(self):
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            self.fake.write(repo, sid(1), [user("a", t(0), sid(1), repo)])
            clock = [0.0]
            self.run_update(now=lambda: clock[0])
            write_file(repo + "/notes/new.md", "# 新しい資料")
            clock[0] = 100.0
            self.fake.append(repo, sid(1), [user("続き", t(1), sid(1), repo)])
            self.run_update(now=lambda: clock[0])
            self.assertTrue(self.doc_exists(repo + "/notes/new.md"))

    def test_scan_drops_vanished_documents(self):
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            doc = os.path.join(repo, "notes", "a.md")
            write_file(doc, "# a")
            self.fake.write(repo, sid(1), [user("a", t(0), sid(1), repo)])
            clock = [0.0]
            self.run_update(now=lambda: clock[0])
            self.assertTrue(self.doc_exists(doc))
            os.remove(doc)
            clock[0] = 700.0
            self.run_update(now=lambda: clock[0])
            self.assertFalse(self.doc_exists(doc))

    def test_scan_disabled(self):
        with allow_system_tmp():
            repo = make_git_repo(self.d, "repo")
            scanned, touched = repo + "/notes/s.md", repo + "/docs/t.md"
            write_file(scanned, "# s")
            write_file(touched, "# t")
            self.fake.write(repo, sid(1), [user("a", t(0), sid(1), repo),
                                           assistant(t(1), sid(1), repo, tools=[("Write", {"file_path": touched})])])
            rep = self.run_update(Config(doc_scan=DocScan(enabled=False)))
            self.assertEqual(rep.docs_upserted, 1)
            self.assertTrue(self.doc_exists(touched))
            self.assertFalse(self.doc_exists(scanned))

    def test_bad_document_path_does_not_stop_update(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w"),
                                       assistant(t(1), sid(1), "/w", tools=[("Write", {"file_path": "/w/a\x00b.md"})])])
        rep = self.run_update()
        self.assertEqual(rep.files_ingested, 1)
        self.assertTrue(get_meta(self.conn, "last_update_at"))

    def test_format_warning_needs_enough_records(self):
        # 読んだ行が少ない回は、知らない種類ばかりでも知らせない（管理用の行が数行だけ足された回など）
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.assertIsNone(self.run_update().format_warning)
        self.fake.append("/w", sid(1), [{"type": "zzz-new", "sessionId": sid(1)} for _ in range(5)])
        self.assertIsNone(self.run_update().format_warning)
        self.assertFalse(get_meta(self.conn, "format_warning"))

    def test_bookkeeping_records_are_known(self):
        # Claude Code が会話のほかに書く管理用の行は、知らない種類として数えない
        recs = [user("入力%d" % i, t(i), sid(1), "/w") for i in range(10)]
        recs += [{"type": typ, "sessionId": sid(1)} for typ in ("cost-state", "worktree-state", "frame-link",
                                                                "data-flow-incomplete", "artifact-autoreact-ledger",
                                                                "artifact-comment-monitor") for _ in range(10)]
        self.fake.write("/w", sid(1), recs)
        self.assertIsNone(self.run_update().format_warning)

    def test_format_warning_share_kept_until_healthy_pass(self):
        recs = [user("入力%d" % i, t(i), sid(1), "/w") for i in range(30)]
        recs += [{"type": "zzz-new", "sessionId": sid(1)} for _ in range(30)]
        self.fake.write("/w", sid(1), recs)
        rep = self.run_update()
        self.assertEqual(rep.format_warning, FORMAT_WARNING + "（知らない種類の記録が50%）")
        # 何も読まなかった回は判断の材料がないので、前の知らせを残す
        self.assertEqual(self.run_update().format_warning, rep.format_warning)
        self.assertEqual(get_meta(self.conn, "format_warning"), rep.format_warning)
        # 読んだ行が少なく判断できない回は、知らない種類ばかりでも、問題のない行ばかりでも、前の知らせを残す
        self.fake.append("/w", sid(1), [{"type": "zzz-new", "sessionId": sid(1)} for _ in range(3)])
        self.assertEqual(self.run_update().format_warning, rep.format_warning)
        self.fake.write("/w", sid(3), [user("c", t(0), sid(3), "/w")])
        self.assertEqual(self.run_update().format_warning, rep.format_warning)
        # 形式に問題のない記録を十分に読んだ回で消える
        self.fake.write("/w", sid(2), [user("b%d" % i, t(i), sid(2), "/w") for i in range(50)])
        rep = self.run_update()
        self.assertIsNone(rep.format_warning)
        self.assertEqual(get_meta(self.conn, "format_warning"), "")

    def test_last_update_at_and_seconds(self):
        rep = self.run_update(now=lambda: 1795243515.642)
        self.assertEqual(get_meta(self.conn, "last_update_at"), "2026-11-21T06:45:15.642Z")
        self.assertGreaterEqual(rep.seconds, 0.0)
        self.assertLess(rep.seconds, 60.0)

    @unittest.skipIf(AS_ROOT, "root は読み取りの権限を無視する")
    def test_index_cli_reports_failures_and_warning(self):
        bad = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.fake.write("/w", sid(7), [{"type": "zzz-new", "sessionId": sid(7)} for _ in range(30)])
        os.chmod(bad, 0)
        try:
            lines = run_cli(self.cli_args("index")).splitlines()
        finally:
            os.chmod(bad, 0o600)
        self.assertRegex(lines[0], r"^記録 2件（取り込み 1件）・資料 0件・つながり 0件・\d+\.\d秒（読めなかったファイル 1件）$")
        self.assertEqual(lines[1:], ["読めなかったファイル: %s" % bad,
                                     FORMAT_WARNING + "（1件のファイルで会話を読み取れませんでした）"])

    @unittest.skipIf(AS_ROOT, "root は読み取りの権限を無視する")
    def test_failed_paths_are_listed(self):
        bad = [self.fake.write("/w", sid(n), [user("a", t(0), sid(n), "/w")]) for n in range(1, 26)]
        self.fake.write("/ok", sid(99), [user("b", t(0), sid(99), "/ok")])
        for p in bad:
            os.chmod(str(p), 0)
        try:
            rep = self.run_update()
            lines = run_cli(self.cli_args("index")).splitlines()
        finally:
            for p in bad:
                os.chmod(str(p), 0o600)
        self.assertEqual((rep.files_failed, rep.files_ingested), (25, 1))
        self.assertEqual(rep.failed_paths, [str(p) for p in bad[:20]])  # 先頭の20件まで
        self.assertEqual(lines[1:], ["読めなかったファイル: %s" % p for p in bad[:5]])  # 画面には5件まで

    @unittest.skipIf(AS_ROOT, "root は読み取りの権限を無視する")
    def test_unreadable_history_is_recorded_and_pass_continues(self):
        repo = make_git_repo(self.d, "repo")
        self.fake.write(repo, sid(1), [user("親", t(0), sid(1), repo), tool_result(sid(9), t(2), sid(1), repo)])
        self.fake.write(repo, sid(9), [headless("子", t(1), sid(9), repo)])
        h = self.fake.history([{"display": "入力", "timestamp": 1795243515642, "project": repo, "sessionId": sid(1)}])
        os.chmod(str(h), 0)
        try:
            rep = self.run_update(now=lambda: 1795243515.642)
            last_update_at = get_meta(self.conn, "last_update_at")
            lines = run_cli(self.cli_args("index")).splitlines()
        finally:
            os.chmod(str(h), 0o600)
        self.assertEqual(len(rep.errors), 1)
        self.assertTrue(rep.errors[0].startswith("入力履歴を読めませんでした: %s: " % h), rep.errors)
        self.assertEqual(rep.relations, 1)  # 後の段も進む
        self.assertEqual(last_update_at, "2026-11-21T06:45:15.642Z")
        self.assertIn(rep.errors[0], lines)
        rep = self.run_update()  # 読めるようになれば取り込む
        self.assertEqual((rep.errors, count(self.conn, "history_prompts")), ([], 1))

    @unittest.skipIf(AS_ROOT, "root は読み取りの権限を無視する")
    def test_unreadable_lineage_is_recorded(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        lineage = self.ledger / "lineage.jsonl"
        lineage.write_text(json.dumps({"session_id": sid(1), "parent_id": None}) + "\n", encoding="utf-8")
        os.chmod(str(lineage), 0)
        try:
            rep = self.run_update()
        finally:
            os.chmod(str(lineage), 0o600)
        self.assertEqual(len(rep.errors), 1)
        self.assertTrue(rep.errors[0].startswith("フックの記録を読めませんでした: %s: " % lineage), rep.errors)
        self.assertTrue(get_meta(self.conn, "last_update_at"))

    def test_index_cli_prints_source_warning(self):
        lines = run_cli(self.cli_args("index")).splitlines()  # projects がまだない偽の ~/.claude
        self.assertRegex(lines[0], r"^記録 0件（取り込み 0件）")
        self.assertEqual(lines[1:], ["Claude Code の記録の場所が見つかりません: %s" % (self.fake.dir / "projects")])

    def test_index_cli_config_error(self):
        self.ledger.mkdir(exist_ok=True)
        (self.ledger / "config.json").write_text('{"retention": "forever"}', encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(self.cli_args("index"))
        self.assertEqual(code, 1)
        self.assertIn("retention", err.getvalue())


class RetentionTest(unittest.TestCase):
    TABLES = ("messages", "digests", "doc_links", "mentions", "session_prs", "derived_from")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = make_ledger(Path(self._tmp.name))
        self.addCleanup(self.conn.close)

    def add(self, session, ended_at=None):
        c = self.conn
        with c:
            c.execute("INSERT INTO sessions(id, transcript_path, ended_at) VALUES (?, ?, ?)",
                      (session, "/x/%s.jsonl" % session, ended_at))
            c.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, 0, 1, NULL, 'user', '本文')",
                      (session,))
            c.execute("INSERT INTO digests(session_id, text) VALUES (?, '要約')", (session,))
            c.execute("INSERT INTO doc_links(session_id, path, rel) VALUES (?, '/r/a.md', 'created')", (session,))
            c.execute("INSERT INTO mentions(session_id, mentioned_id) VALUES (?, 'other')", (session,))
            c.execute("INSERT INTO session_prs(session_id, url) VALUES (?, 'https://github.com/o/r/pull/1')", (session,))
            c.execute("INSERT INTO derived_from(session_id, source_id, via) VALUES (?, 'other', 'subprocess')", (session,))
            c.execute("INSERT INTO history_prompts(session_id, text) VALUES (?, '入力')", (session,))

    def owners(self, table):
        col = "id" if table == "sessions" else "session_id"
        return sorted(r[0] for r in self.conn.execute("SELECT %s FROM %s" % (col, table)))

    def test_delete_session_data(self):
        self.add(sid(1))
        self.add(sid(2))
        with self.conn:
            for src, dst in ((sid(2), sid(1)), (sid(1), sid(3)), (sid(2), sid(3))):
                self.conn.execute("INSERT INTO relations VALUES (?, ?, 'reference', 'strong', 'x')", (src, dst))
        with self.conn:
            delete_session_data(self.conn, sid(1))
        for table in self.TABLES + ("sessions",):
            self.assertEqual(self.owners(table), [sid(2)], table)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT src, dst FROM relations")], [(sid(2), sid(3))])
        self.assertEqual(self.owners("history_prompts"), [sid(1), sid(2)])  # 入力履歴は消さない
        self.assertEqual(count(self.conn, "tombstones"), 0)

    def test_apply_missing(self):
        self.add(sid(1))
        self.add(sid(2))
        # コミットは呼び出し側が行う（記録の有無の更新と同じトランザクションにするため）
        self.assertEqual(apply_missing(self.conn, "card", [sid(1)]), 1)
        self.conn.rollback()
        self.assertEqual(self.owners("messages"), [sid(1), sid(2)])
        with self.conn:
            self.assertEqual(apply_missing(self.conn, "all", [sid(1)]), 0)
        self.assertEqual(self.owners("messages"), [sid(1), sid(2)])
        with self.conn:
            self.assertEqual(apply_missing(self.conn, "card", [sid(1)]), 1)
        self.assertEqual(self.owners("messages"), [sid(2)])
        # card はカードとつながりを残す（derived_from はつながりの根拠）
        for table in ("sessions", "digests", "doc_links", "mentions", "session_prs", "derived_from"):
            self.assertEqual(self.owners(table), [sid(1), sid(2)], table)
        with self.conn:
            self.assertEqual(apply_missing(self.conn, "none", iter([sid(1)])), 1)
        self.assertEqual(self.owners("sessions"), [sid(2)])
        self.assertEqual(count(self.conn, "tombstones"), 0)
        with self.assertRaises(ValueError):
            apply_missing(self.conn, "forever", [sid(2)])

    def test_apply_max_age(self):
        now = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
        self.add(sid(1), ended_at="2026-10-31T23:59:59.999Z")  # 30日より前
        self.add(sid(2), ended_at="2026-11-01T00:00:00.000Z")  # ちょうど境目は残す
        self.add(sid(3), ended_at=None)
        self.assertEqual(apply_max_age(self.conn, None, now), 0)
        self.assertEqual(apply_max_age(self.conn, 30, now), 1)
        self.assertEqual(self.owners("sessions"), [sid(2), sid(3)])
        self.assertEqual(count(self.conn, "tombstones"), 0)


if __name__ == "__main__":
    unittest.main()
