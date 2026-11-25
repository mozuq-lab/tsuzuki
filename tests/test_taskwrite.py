import json
import os
import stat
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from tsuzuki.tasks import parse_task_md
from tsuzuki import taskwrite
from tsuzuki.taskwrite import (CANNOT_TRASH, CHANGED, NO_TRASH, NOT_A_TASK_FOLDER, TaskWriteError, create_task,
                              find_trash, normalize_short_name, normalize_ticket, render_task_md, set_fields,
                              trash_task)
from tests.fixtures import allow_system_tmp, run_cli

TEMPLATE = (
    "---\ntitle: ログインの期限\nticket: \"#123\"\nstage: チケット\nwaiting: false\nrepo: /Users/me/dev/web-app\n"
    "created: 2026-11-22\nupdated: 2026-11-22T10:05\n---\n# ログインの期限\n\n## 目的\n\n## 現在地\n\n"
    "## 次の一手\n\n## 待っていること\nなし\n\n## 決めたこと\n\n## 資料\n")


class RenderTest(unittest.TestCase):
    def test_template_exact(self):
        self.assertEqual(render_task_md(title="ログインの期限", ticket="#123", repo="/Users/me/dev/web-app",
                                        created="2026-11-22", updated="2026-11-22T10:05"), TEMPLATE)

    def test_without_ticket_and_repo(self):
        text = render_task_md(title="x", ticket=None, repo=None, created="2026-11-22", updated="2026-11-22T10:05")
        self.assertIn("\nticket:\n", text)
        self.assertIn("\nrepo:\n", text)
        self.assertIsNone(parse_task_md(text).ticket)

    def test_names_and_tickets(self):
        self.assertEqual(normalize_short_name(" login  timeout "), "login-timeout")
        self.assertEqual(normalize_short_name("ログイン"), "ログイン")
        for bad in ("", "a/b", ".hidden", "a\x00b", "a\nb"):
            with self.assertRaises(TaskWriteError, msg=repr(bad)):
                normalize_short_name(bad)
        self.assertEqual(normalize_ticket("123"), "#123")
        self.assertEqual(normalize_ticket("#123"), "#123")
        self.assertEqual(normalize_ticket("https://github.com/o/r/issues/5"), "https://github.com/o/r/issues/5")
        self.assertIsNone(normalize_ticket(" "))
        # 全角の数字と ＃ も半角にそろえる
        self.assertEqual(normalize_ticket("１２３"), "#123")
        self.assertEqual(normalize_ticket("＃１２３"), "#123")
        self.assertEqual(normalize_ticket("　１２３　"), "#123")


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        self.root = self.base / "tasks"


class CreateTest(_TmpCase):
    def test_create_task(self):
        md = create_task(self.root, "login timeout", title="ログインの期限", ticket="123",
                         now=datetime(2026, 11, 22, 10, 5))
        self.assertEqual(md, self.root / "2026-11-22-login-timeout" / "task.md")
        tf = parse_task_md(md.read_text(encoding="utf-8"))
        self.assertEqual((tf.title, tf.ticket, tf.stage, tf.created, tf.updated),
                         ("ログインの期限", "#123", "チケット", "2026-11-22", "2026-11-22T10:05"))

    def test_title_defaults_to_short_name(self):
        md = create_task(self.root, "login", now=datetime(2026, 11, 22, 10, 5))
        self.assertEqual(parse_task_md(md.read_text(encoding="utf-8")).title, "login")

    def test_refuses_existing(self):
        md = create_task(self.root, "login", now=datetime(2026, 11, 22, 10, 5))
        before = md.read_bytes()
        with self.assertRaises(TaskWriteError):
            create_task(self.root, "login", title="別", now=datetime(2026, 11, 22, 11, 0))
        self.assertEqual(md.read_bytes(), before)


class ClaudeDirGuardTest(_TmpCase):
    """置き場所の実体が（仮の）Claude Code の設定ディレクトリの中なら、書く直前に断る。"""

    def setUp(self):
        super().setUp()
        self.claude = self.base / "cfg" / ".claude"
        (self.claude / "inside").mkdir(parents=True)
        self.link = self.base / "tasks"
        self.link.symlink_to(self.claude / "inside")

    def test_create_task_refuses_symlinked_root(self):
        with self.assertRaises(TaskWriteError):
            create_task(self.link, "login", title="x", now=datetime(2026, 11, 22, 10, 0), claude_dir=self.claude)
        self.assertEqual(list((self.claude / "inside").iterdir()), [])

    def test_set_fields_refuses_symlinked_root(self):
        folder = self.claude / "inside" / "2026-11-22-x"
        folder.mkdir()
        md = folder / "task.md"
        md.write_text("---\nstage: 調査\n---\n", encoding="utf-8")
        with self.assertRaises(TaskWriteError):
            set_fields(self.link / "2026-11-22-x" / "task.md", self.link, {"stage": "実装"}, claude_dir=self.claude)
        self.assertEqual(md.read_text(encoding="utf-8"), "---\nstage: 調査\n---\n")


class SetFieldsTest(_TmpCase):
    def setUp(self):
        super().setUp()
        self.root.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        self.outside_md = outside / "task.md"
        self.outside_md.write_text("---\nstage: 調査\n---\n", encoding="utf-8")
        (self.root / "2026-11-22-link").symlink_to(outside)

    def task_md(self, text):
        d = self.root / "2026-11-22-x"
        d.mkdir(exist_ok=True)
        md = d / "task.md"
        md.write_bytes(text.encode("utf-8"))
        return md

    def test_changes_only_those_lines(self):
        md = self.task_md("﻿---\r\ntitle: 修正: ログイン\r\nstage: 調査\r\nextra: keep\r\n---\r\n本文 stage: 調査\r\n")
        set_fields(md, self.root, {"stage": "実装", "waiting": "true"})
        self.assertEqual(md.read_bytes().decode("utf-8"),
                         "﻿---\r\ntitle: 修正: ログイン\r\nstage: 実装\r\nextra: keep\r\nwaiting: true\r\n---\r\n本文 stage: 調査\r\n")

    def test_adds_frontmatter_when_missing(self):
        md = self.task_md("# 題\n本文\n")
        set_fields(md, self.root, {"stage": "PR"})
        self.assertEqual(md.read_text(encoding="utf-8"), "---\nstage: PR\n---\n# 題\n本文\n")

    def test_refuses_unclosed_frontmatter(self):
        md = self.task_md("---\ntitle: x\n本文\n")
        with self.assertRaises(TaskWriteError):
            set_fields(md, self.root, {"stage": "PR"})
        self.assertEqual(md.read_text(encoding="utf-8"), "---\ntitle: x\n本文\n")

    def test_checks_expected_stat(self):
        md = self.task_md("---\nstage: 調査\n---\n")
        st = md.stat()
        with self.assertRaises(TaskWriteError) as cm:
            set_fields(md, self.root, {"stage": "PR"}, expect_mtime=st.st_mtime - 10, expect_size=st.st_size)
        self.assertEqual(str(cm.exception), CHANGED)
        self.assertEqual(md.read_text(encoding="utf-8"), "---\nstage: 調査\n---\n")

    def test_rejects_bad_values_and_keys(self):
        md = self.task_md("---\nstage: 調査\n---\n")
        for changes in ({"stage": "実装中"}, {"waiting": "yes"}, {"title": "x"}):
            with self.assertRaises(TaskWriteError, msg=str(changes)):
                set_fields(md, self.root, changes)

    def test_refuses_outside_root(self):
        for md in (self.outside_md, self.root / "2026-11-22-link" / "task.md"):
            with self.assertRaises(TaskWriteError):
                set_fields(md, self.root, {"stage": "PR"})
        self.assertEqual(self.outside_md.read_text(encoding="utf-8"), "---\nstage: 調査\n---\n")

    def test_keeps_mode(self):
        md = self.task_md("---\nstage: 調査\n---\n")
        os.chmod(str(md), 0o640)
        set_fields(md, self.root, {"waiting": "true"})
        self.assertEqual(stat.S_IMODE(md.stat().st_mode), 0o640)


class TrashTest(_TmpCase):
    def setUp(self):
        super().setUp()
        self.home = str(self.base / "home")
        self.folder = self.root / "2026-11-20-a"
        self.folder.mkdir(parents=True)
        (self.folder / "task.md").write_text("# a\n", encoding="utf-8")
        (self.folder / "a.md").write_text("メモ\n", encoding="utf-8")
        self.trash = self.base / "trash"
        self.trash.mkdir()

    def trash_it(self, name="2026-11-20-a", **kw):
        return trash_task(str(self.root / name), self.root, home=self.home, trash_dir=str(self.trash), **kw)

    def test_trash_moves_the_folder(self):
        moved = self.trash_it()
        self.assertEqual(moved, str(self.trash / "2026-11-20-a"))
        self.assertTrue((self.trash / "2026-11-20-a" / "task.md").is_file())
        self.assertTrue((self.trash / "2026-11-20-a" / "a.md").is_file())
        self.assertFalse(self.folder.exists())

    def test_trash_renames_on_collision(self):
        old = self.trash / "2026-11-20-a"
        old.mkdir()
        (old / "keep.md").write_text("先にあった物\n", encoding="utf-8")
        now = datetime(2026, 11, 24, 9, 5, 7)
        moved = self.trash_it(now=now)
        self.assertEqual(moved, str(self.trash / "2026-11-20-a 09.05.07"))
        self.assertTrue((self.trash / "2026-11-20-a 09.05.07" / "a.md").is_file())
        # 時刻つきの名前もあれば、番号を足す
        self.folder.mkdir()
        (self.folder / "task.md").write_text("# a\n", encoding="utf-8")
        moved = self.trash_it(now=now)
        self.assertEqual(moved, str(self.trash / "2026-11-20-a 09.05.07 2"))
        self.assertEqual(os.listdir(str(old)), ["keep.md"])
        self.assertEqual((old / "keep.md").read_text(encoding="utf-8"), "先にあった物\n")

    def test_trash_refuses_other_folders(self):
        (self.root / "shared").mkdir()
        (self.root / ".hidden").mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "2026-11-22-link").symlink_to(outside)
        nested = self.folder / "inner"
        nested.mkdir()
        for target in (self.root, self.root / "shared", self.root / ".hidden", outside, self.root / "2026-11-22-link",
                       nested):
            with self.subTest(target=str(target)):
                with self.assertRaises(TaskWriteError) as caught:
                    trash_task(str(target), self.root, home=self.home, trash_dir=str(self.trash))
                self.assertTrue(str(caught.exception).startswith(NOT_A_TASK_FOLDER.split("%")[0]))
        self.assertEqual(os.listdir(str(self.trash)), [])
        self.assertTrue(outside.is_dir())
        self.assertTrue(self.folder.is_dir())

    def test_trash_refuses_the_claude_dir(self):
        with self.assertRaises(TaskWriteError):
            self.trash_it(claude_dir=self.base)
        self.assertTrue(self.folder.is_dir())

    def test_trash_without_a_trash_dir(self):
        with mock.patch.object(taskwrite, "find_trash", return_value=None):
            with self.assertRaises(TaskWriteError) as caught:
                trash_task(str(self.folder), self.root, home=self.home)
        self.assertEqual(str(caught.exception), NO_TRASH)
        self.assertTrue(self.folder.is_dir())

    def test_trash_reports_permission_errors(self):
        with mock.patch("os.rename", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaises(TaskWriteError) as caught:
                self.trash_it()
        self.assertEqual(str(caught.exception), CANNOT_TRASH % "Operation not permitted")
        self.assertTrue(self.folder.is_dir())

    def test_find_trash_on_the_home_disk(self):
        home = self.base / "home"
        (home / ".Trash").mkdir(parents=True)
        self.assertEqual(find_trash(str(self.folder), str(home)), str(home / ".Trash"))

    def test_find_trash_on_another_disk(self):
        """path が別のディスクなら、そのディスクの根の .Trashes/<uid>。"""
        home = self.base / "home"
        (home / ".Trash").mkdir(parents=True)
        volume = self.base / "volume"
        (volume / ".Trashes" / str(os.getuid())).mkdir(parents=True)
        (volume / "tasks" / "x").mkdir(parents=True)
        bare = self.base / "bare"
        (bare / "tasks").mkdir(parents=True)
        real_dev = os.stat

        def fake_stat(path, *args, **kw):
            result = real_dev(path, *args, **kw)
            if str(path).startswith((str(volume), str(bare))):
                values = list(result)
                values[2] = 999
                return os.stat_result(values)
            return result

        with mock.patch("os.stat", side_effect=fake_stat):
            self.assertEqual(find_trash(str(volume / "tasks" / "x"), str(home)),
                             str(volume / ".Trashes" / str(os.getuid())))
            self.assertIsNone(find_trash(str(bare / "tasks"), str(home)))


class CliTest(_TmpCase):
    def test_task_new(self):
        ledger = self.base / "ledger"
        claude = self.base / "claude"
        ledger.mkdir()
        claude.mkdir()
        tasks = self.base / "tasks"
        (ledger / "config.json").write_text(json.dumps({"tasks_root": str(tasks)}), encoding="utf-8")
        with allow_system_tmp():
            out = run_cli(["--ledger-dir", str(ledger), "--claude-dir", str(claude), "task", "new", "login",
                           "--title", "ログイン", "--ticket", "123"])
        path = tasks / ("%s-login" % date.today().isoformat()) / "task.md"
        self.assertEqual(out.splitlines(), ["作りました: %s" % path, "@%s" % path])

    def cli_new(self, tasks, *args):
        """設定の tasks_root を tasks にして tsuzuki task new を走らせ、出力を返す。"""
        ledger, claude = self.base / "ledger", self.base / "claude"
        ledger.mkdir(exist_ok=True)
        claude.mkdir(exist_ok=True)
        (ledger / "config.json").write_text(json.dumps({"tasks_root": str(tasks)}), encoding="utf-8")
        with allow_system_tmp():
            return run_cli(["--ledger-dir", str(ledger), "--claude-dir", str(claude), "task", "new"] + list(args))

    def test_task_new_makes_a_relative_repo_absolute(self):
        # --repo の相対パスは今のフォルダーから絶対パスにする（task.md の repo は ~ か絶対パスで読む）
        repo = self.base / "dev" / "app"
        (repo / "sub").mkdir(parents=True)
        tasks = self.base / "tasks"
        old = os.getcwd()
        os.chdir(str(repo))
        self.addCleanup(os.chdir, old)
        today = date.today().isoformat()
        for name, given, written in (("a", ".", str(repo)), ("b", "sub/../sub", str(repo / "sub")),
                                     ("c", "~/dev/app", "~/dev/app"), ("d", str(repo), str(repo))):
            with self.subTest(given=given):
                self.cli_new(tasks, name, "--repo", given)
                text = (tasks / ("%s-%s" % (today, name)) / "task.md").read_text(encoding="utf-8")
                self.assertEqual(parse_task_md(text).repo, written)

    def test_task_new_quotes_a_path_with_a_space(self):
        # @ の行は画面のコピーと同じ形（空白を含むパスは @"…"）
        tasks = self.base / "My Tasks"
        out = self.cli_new(tasks, "login")
        path = tasks / ("%s-login" % date.today().isoformat()) / "task.md"
        self.assertEqual(out.splitlines(), ["作りました: %s" % path, '@"%s"' % path])
