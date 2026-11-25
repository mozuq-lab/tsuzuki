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
from tsuzuki.taskwrite import (BAD_SECTION_LINE, CANNOT_TRASH, CHANGED, NO_TRASH, NOT_A_TASK_FOLDER, TaskWriteError, create_task,
                              find_trash, normalize_short_name, normalize_ticket, render_task_md, section_text,
                              set_fields, set_waiting, trash_task, BAD_WORKSPACE, normalize_workspace,
                              replace_heading, update_settings)
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
        for changes in ({"stage": "実装中"}, {"waiting": "yes"}, {"branch": "x"}, {"title": "a\nb"}):
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


class SettingsTest(_TmpCase):
    def setUp(self):
        super().setUp()
        self.root.mkdir()
        (self.base / "app").mkdir()
        (self.base / "w").mkdir()
        self.ws = self.base / "w" / "a.code-workspace"
        self.ws.write_text('{"folders": [{"path": "../app"}]}', encoding="utf-8")

    def task_md(self, text=TEMPLATE):
        d = self.root / "2026-11-22-x"
        d.mkdir(exist_ok=True)
        md = d / "task.md"
        md.write_bytes(text.encode("utf-8"))
        return md

    def update(self, md, **kw):
        values = dict(title="ログインの期限", ticket="#123", repo="/Users/me/dev/web-app", workspace="",
                      home=str(self.base))
        values.update(kw)
        return update_settings(md, self.root, **values)

    def test_only_changed_lines_are_written(self):
        md = self.task_md()
        self.assertTrue(self.update(md, title="新しい題"))
        self.assertEqual(md.read_text(encoding="utf-8"),
                         TEMPLATE.replace("title: ログインの期限", "title: 新しい題").replace("# ログインの期限", "# 新しい題"))

    def test_nothing_changed_writes_nothing(self):
        md = self.task_md()
        before = md.stat().st_mtime_ns
        self.assertFalse(self.update(md))
        self.assertEqual(md.stat().st_mtime_ns, before)
        self.assertEqual(md.read_text(encoding="utf-8"), TEMPLATE)

    def test_heading_is_kept_when_it_differs(self):
        text = TEMPLATE.replace("# ログインの期限\n", "# 別の見出し\n")
        md = self.task_md(text)
        self.assertTrue(self.update(md, title="新しい題"))
        self.assertEqual(md.read_text(encoding="utf-8"), text.replace("title: ログインの期限", "title: 新しい題"))

    def test_heading_follows_the_card_title_without_a_frontmatter_title(self):
        text = "---\nstage: 調査\n---\n```\n# 囲みの中\n```\n# 見出し\n本文\n"
        md = self.task_md(text)
        self.update(md, title="見出し", ticket="", repo="")  # 題は変わらないので見出しも同じ
        self.assertIn("# 見出し\n", md.read_text(encoding="utf-8"))
        self.update(md, title="変えた", ticket="", repo="")
        out = md.read_text(encoding="utf-8")
        self.assertIn("```\n# 囲みの中\n```\n# 変えた\n本文\n", out)

    def test_unclosed_fence_before_heading_leaves_it(self):
        text = TEMPLATE.replace("# ログインの期限\n", "```\n# ログインの期限\n")
        md = self.task_md(text)
        self.assertTrue(self.update(md, title="新しい題"))
        self.assertEqual(md.read_text(encoding="utf-8"), text.replace("title: ログインの期限", "title: 新しい題"))

    def test_replace_heading_keeps_bom_and_line_endings(self):
        text = "﻿---\r\ntitle: a\r\n---\r\n# a\r\n本文\r\n"
        self.assertEqual(replace_heading(text, "a", "b"), text.replace("# a", "# b"))
        self.assertEqual(replace_heading("# a\n# a\n", "a", "b"), "# b\n# a\n")
        self.assertEqual(replace_heading("# x\n", "a", "b"), "# x\n")

    def test_ticket_and_repo_and_workspace(self):
        md = self.task_md()
        self.update(md, ticket="１２３４", repo="/r/other")
        text = md.read_text(encoding="utf-8")
        self.assertIn('ticket: "#1234"\n', text)
        self.assertIn("repo: /r/other\n", text)
        self.assertNotIn("workspace:", text)
        self.update(md, ticket="#123", repo="/r/other", workspace="~/w/a.code-workspace")
        text = md.read_text(encoding="utf-8")
        self.assertIn("workspace: %s\n" % self.ws, text)
        self.assertEqual(parse_task_md(text).workspace, str(self.ws))
        self.update(md, ticket="", repo="/r/other", workspace="")
        text = md.read_text(encoding="utf-8")
        self.assertIn("\nworkspace:\n", text)
        self.assertIn("ticket:\n", text)
        self.assertIsNone(parse_task_md(text).ticket)

    def test_bad_workspace_is_refused(self):
        md = self.task_md()
        before = md.read_bytes()
        (self.base / "w" / "plain.txt").write_text("{}", encoding="utf-8")
        (self.base / "w" / "none.code-workspace").write_text('{"folders": [{"path": "../nope"}]}', encoding="utf-8")
        for bad in ("~/w/plain.txt", "~/w/missing.code-workspace", "~/w/none.code-workspace", "w/a.code-workspace",
                    str(self.base / "w")):
            with self.assertRaises(TaskWriteError, msg=bad) as cm:
                self.update(md, title="新しい題", workspace=bad)
            self.assertTrue(str(cm.exception).startswith(BAD_WORKSPACE % ""), str(cm.exception))
            self.assertEqual(md.read_bytes(), before)
        self.assertIsNone(normalize_workspace("  ", home=str(self.base)))

    def test_settings_refuse_a_stale_form(self):
        md = self.task_md()
        st = md.stat()
        before = md.read_bytes()
        with self.assertRaises(TaskWriteError) as cm:
            self.update(md, title="新しい題", expect_mtime=st.st_mtime + 1, expect_size=st.st_size)
        self.assertEqual(str(cm.exception), CHANGED)
        with self.assertRaises(TaskWriteError) as cm:
            self.update(md, title="新しい題", expect_mtime=st.st_mtime, expect_size=st.st_size + 1)
        self.assertEqual(str(cm.exception), CHANGED)
        self.assertEqual(md.read_bytes(), before)
        self.assertTrue(self.update(md, title="新しい題", expect_mtime=st.st_mtime, expect_size=st.st_size))

    def test_control_characters_are_refused(self):
        md = self.task_md()
        before = md.read_bytes()
        for field, value in (("title", "題\x07"), ("repo", "/r\n/x"), ("ticket", "#1\x00"), ("title", "a\nb")):
            with self.assertRaises(TaskWriteError, msg=field):
                self.update(md, **{field: value})
        with self.assertRaises(TaskWriteError) as cm:
            self.update(md, title="  ")
        self.assertEqual(str(cm.exception), "題を入れてください")
        self.assertEqual(md.read_bytes(), before)

    def test_line_separators_are_refused(self):
        md = self.task_md()
        before = md.read_bytes()
        for sep in ("\u2028", "\u2029", "\x85"):
            d = self.base / ("w stage: 完了 ---%s z" % sep)
            d.mkdir()
            ws = d / "a.code-workspace"
            ws.write_text('{"folders": [{"path": "%s"}]}' % self.base, encoding="utf-8")
            with self.assertRaises(TaskWriteError, msg=repr(sep)):
                self.update(md, workspace=str(ws))
            for field in ("title", "ticket", "repo"):
                with self.assertRaises(TaskWriteError, msg=field):
                    self.update(md, **{field: "a%sb" % sep})
            with self.assertRaises(TaskWriteError):
                set_fields(md, self.root, {"repo": "a%sb" % sep})
        self.assertEqual(md.read_bytes(), before)

    def test_only_allowed_keys_are_settable(self):
        md = self.task_md()
        set_fields(md, self.root, {"title": "x", "workspace": "/a.code-workspace"})
        with self.assertRaises(TaskWriteError):
            set_fields(md, self.root, {"branch": "x"})


class WaitingTest(_TmpCase):
    def setUp(self):
        super().setUp()
        self.root.mkdir()

    def task_md(self, text=TEMPLATE):
        d = self.root / "2026-11-22-x"
        d.mkdir(exist_ok=True)
        md = d / "task.md"
        md.write_bytes(text.encode("utf-8"))
        return md

    def test_waiting_with_a_reason(self):
        md = self.task_md()
        set_waiting(md, self.root, waiting=True, reason="山田さんの返事\n- API の仕様")
        text = md.read_text(encoding="utf-8")
        parsed = parse_task_md(text)
        self.assertTrue(parsed.waiting)
        self.assertEqual(parsed.sections["待っていること"], "山田さんの返事\n- API の仕様")
        self.assertEqual(text[text.index("## 決めたこと"):], TEMPLATE[TEMPLATE.index("## 決めたこと"):])
        self.assertEqual(text[:text.index("waiting:")], TEMPLATE[:TEMPLATE.index("waiting:")])
        self.assertIn("## 次の一手\n\n## 待っていること\n山田さんの返事\n- API の仕様\n\n## 決めたこと", text)

    def test_unwaiting_writes_the_edited_text(self):
        md = self.task_md()
        set_waiting(md, self.root, waiting=True, reason="返事")
        set_waiting(md, self.root, waiting=False, reason="")
        parsed = parse_task_md(md.read_text(encoding="utf-8"))
        self.assertFalse(parsed.waiting)
        self.assertEqual(parsed.sections["待っていること"], "なし")
        set_waiting(md, self.root, waiting=False, reason="CI の結果")
        self.assertEqual(parse_task_md(md.read_text(encoding="utf-8")).sections["待っていること"], "CI の結果")

    def test_reason_none_leaves_the_section_alone(self):
        md = self.task_md()
        set_waiting(md, self.root, waiting=True, reason=None)
        self.assertEqual(md.read_text(encoding="utf-8"), TEMPLATE.replace("waiting: false", "waiting: true"))

    def test_reason_lines_starting_with_a_ticket_are_kept(self):
        md = self.task_md()
        set_waiting(md, self.root, waiting=True, reason="#123 の返事待ち")
        self.assertEqual(parse_task_md(md.read_text(encoding="utf-8")).sections["待っていること"], "#123 の返事待ち")
        before = md.read_bytes()
        for bad in ("## 次", "# 題", "   ### x", "```", "~~~", "返事\n## 次\nあと"):
            with self.assertRaises(TaskWriteError, msg=bad) as cm:
                set_waiting(md, self.root, waiting=True, reason=bad)
            self.assertTrue(str(cm.exception).startswith(BAD_SECTION_LINE % ""), str(cm.exception))
            self.assertEqual(md.read_bytes(), before)

    def test_section_text(self):
        self.assertEqual(section_text(" \r\n\n  "), "なし")
        self.assertEqual(section_text("\n a  \r\nb\t\n\n"), " a\nb")

    def test_section_inside_a_fence_is_not_touched(self):
        text = TEMPLATE.replace("## 決めたこと\n", "## 決めたこと\n```\n## 待っていること\n偽物\n```\n")
        md = self.task_md(text)
        set_waiting(md, self.root, waiting=True, reason="本物")
        new = md.read_text(encoding="utf-8")
        self.assertIn("```\n## 待っていること\n偽物\n```\n", new)
        self.assertIn("## 待っていること\n本物\n\n## 決めたこと", new)
        self.assertEqual(parse_task_md(new).sections["待っていること"], "本物")

    def test_missing_section_is_added_before_decisions(self):
        text = TEMPLATE.replace("## 待っていること\nなし\n\n", "")
        md = self.task_md(text)
        set_waiting(md, self.root, waiting=True, reason="返事")
        self.assertIn("## 次の一手\n\n## 待っていること\n返事\n\n## 決めたこと\n", md.read_text(encoding="utf-8"))
        bare = self.task_md("---\nwaiting: false\n---\n# 題\n\n## 目的\nx")
        set_waiting(bare, self.root, waiting=True, reason="返事")
        self.assertEqual(bare.read_text(encoding="utf-8"),
                         "---\nwaiting: true\n---\n# 題\n\n## 目的\nx\n\n## 待っていること\n返事\n")

    def test_last_section_has_no_trailing_blank_line(self):
        md = self.task_md("---\nwaiting: false\n---\n## 待っていること\nなし\n")
        set_waiting(md, self.root, waiting=True, reason="返事")
        self.assertEqual(md.read_text(encoding="utf-8"), "---\nwaiting: true\n---\n## 待っていること\n返事\n")

    def test_crlf_and_bom_are_kept(self):
        md = self.task_md("\ufeff" + TEMPLATE.replace("\n", "\r\n"))
        set_waiting(md, self.root, waiting=True, reason="返事\n二行目")
        raw = md.read_bytes().decode("utf-8")
        self.assertTrue(raw.startswith("\ufeff---\r\n"))
        self.assertNotRegex(raw, r"(?<!\r)\n")
        self.assertIn("## 待っていること\r\n返事\r\n二行目\r\n\r\n## 決めたこと", raw)
        self.assertEqual(parse_task_md(raw).sections["待っていること"], "返事\n二行目")

    def test_lines_that_parse_as_headings_are_refused(self):
        md = self.task_md()
        before = md.read_bytes()
        for bad in ("返事\n##\u3000決めたこと\n偽の決定", "A\n#\u3000B\nC", "返事\u2028## 決めたこと\n偽", "x\x0c# 題"):
            with self.assertRaises(TaskWriteError, msg=repr(bad)) as cm:
                set_waiting(md, self.root, waiting=True, reason=bad)
            self.assertTrue(str(cm.exception).startswith(BAD_SECTION_LINE % ""), str(cm.exception))
            self.assertEqual(md.read_bytes(), before)

    def test_unclosed_fence_refuses_and_writes_nothing(self):
        for text in (TEMPLATE.replace("## 待っていること\nなし\n", "## 待っていること\n```\n"),
                     TEMPLATE.replace("## 現在地\n", "## 現在地\n```\n")):
            md = self.task_md(text)
            before = md.read_bytes()
            with self.assertRaises(TaskWriteError) as cm:
                set_waiting(md, self.root, waiting=True, reason="なし")
            self.assertIn("コードの囲みが閉じていません", str(cm.exception))
            self.assertEqual(md.read_bytes(), before)

    def test_section_span_follows_splitlines(self):
        md = self.task_md(TEMPLATE.replace("## 決めたこと\n", "古い\u2028## 決めたこと\n中身\n"))
        set_waiting(md, self.root, waiting=True, reason="返事")
        parsed = parse_task_md(md.read_text(encoding="utf-8"))
        self.assertEqual(parsed.sections["待っていること"], "返事")
        self.assertEqual(parsed.sections["決めたこと"], "中身")

    def test_waiting_reason_refuses_a_stale_card(self):
        md = self.task_md()
        st = md.stat()
        before = md.read_bytes()
        for kw in ({"expect_mtime": st.st_mtime - 10, "expect_size": st.st_size},
                   {"expect_mtime": st.st_mtime, "expect_size": st.st_size + 1}):
            with self.assertRaises(TaskWriteError) as cm:
                set_waiting(md, self.root, waiting=True, reason="返事", **kw)
            self.assertEqual(str(cm.exception), CHANGED)
            self.assertEqual(md.read_bytes(), before)


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
