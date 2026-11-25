import os
import tempfile
import unittest

from tsuzuki.tasks import (
    created_from_name, discover_tasks, is_task_dir_name, list_task_files, no_waiting,
    parse_task_md, ref_paths, stage_label, task_doc_kind, task_title, ticket_numbers)

SAMPLE = (
    "---\n"
    "title: ログインのセッション切れを延ばす\n"
    'ticket: "#123"\n'
    "stage: 実装\n"
    "waiting: false\n"
    "repo: ~/dev/web-app\n"
    "created: 2026-11-20\n"
    "updated: 2026-11-21T18:40\n"
    "---\n"
    "# ログインのセッション切れを延ばす\n\n"
    "## 目的\n期限を延ばす\n\n"
    "## 現在地\n- 単体テストを足した\n\n"
    "## 次の一手\n- 結合テストを通す\n\n"
    "## 待っていること\nなし\n\n"
    "## 決めたこと\n- 8 時間\n\n"
    "## 資料\n- [全体像](../shared/auth.md)\n"
)


class ParseTest(unittest.TestCase):
    def test_sample(self):
        tf = parse_task_md(SAMPLE)
        self.assertEqual((tf.title, tf.ticket, tf.stage, tf.waiting), ("ログインのセッション切れを延ばす", "#123", "実装", False))
        self.assertEqual((tf.repo, tf.created, tf.updated), ("~/dev/web-app", "2026-11-20", "2026-11-21T18:40"))
        self.assertTrue(tf.frontmatter_ok)
        self.assertEqual(tf.sections["次の一手"], "- 結合テストを通す")
        self.assertEqual(tf.sections["待っていること"], "なし")

    def test_workspace_is_read(self):
        tf = parse_task_md("---\ntitle: x\nworkspace: /w/a.code-workspace\n---\n")
        self.assertEqual(tf.workspace, "/w/a.code-workspace")
        self.assertIsNone(parse_task_md("---\ntitle: x\nworkspace:\n---\n").workspace)
        self.assertIsNone(parse_task_md(SAMPLE).workspace)

    def test_value_with_colon_and_quotes(self):
        tf = parse_task_md("---\ntitle: 修正: ログイン\nticket: '#9'\n---\n")
        self.assertEqual((tf.title, tf.ticket), ("修正: ログイン", "#9"))

    def test_bom_and_crlf(self):
        tf = parse_task_md("﻿---\r\nstage: 調査\r\nwaiting: TRUE\r\n---\r\n## 現在地\r\n調べた\r\n")
        self.assertEqual((tf.stage, tf.waiting, tf.sections["現在地"]), ("調査", True, "調べた"))

    def test_unknown_and_missing_stage(self):
        tf = parse_task_md("---\nstage: 実装中\n---\n")
        self.assertEqual((tf.stage, tf.stage_raw), ("チケット", "実装中"))
        tf = parse_task_md("---\ntitle: x\n---\n")
        self.assertEqual((tf.stage, tf.stage_raw), ("チケット", None))

    def test_unclosed_and_missing_frontmatter(self):
        tf = parse_task_md("---\ntitle: x\n# 題\n## 次の一手\nやる\n")
        self.assertFalse(tf.frontmatter_ok)
        self.assertIsNone(tf.title)
        self.assertEqual((tf.heading, tf.sections["次の一手"]), ("題", "やる"))
        tf = parse_task_md("# 題だけ\n")
        self.assertFalse(tf.frontmatter_ok)
        self.assertEqual((tf.heading, tf.stage), ("題だけ", "チケット"))

    def test_heading_spaces_and_duplicates(self):
        tf = parse_task_md("## 現在地 \n一つ目\n### 小見出し\n続き\n##  現在地\n二つ目\n")
        self.assertEqual(tf.sections["現在地"], "一つ目\n### 小見出し\n続き")

    def test_headings_inside_fences_do_not_split(self):
        # コードの囲み（``` と ~~~）の中の # の行は見出しではない
        text = ("---\nstage: 実装\n---\n```sh\n# 囲みの中の題\n```\n# 本当の題\n\n## 次の一手\n- 落ちたテストだけ走らせる\n"
                "```sh\n# 1つだけ\n## 囲みの中\nmake test\n```\n- 直す\n\n## 待っていること\nなし\n")
        tf = parse_task_md(text)
        self.assertEqual(tf.heading, "本当の題")
        self.assertEqual(tf.sections["次の一手"], "- 落ちたテストだけ走らせる\n```sh\n# 1つだけ\n## 囲みの中\nmake test\n```\n- 直す")
        self.assertEqual(tf.sections["待っていること"], "なし")

    def test_fence_rules(self):
        # 閉じるのは同じ文字で、開いた数以上の囲み。~~~ の中の ``` や、短い囲みでは閉じない。閉じない囲みは最後まで
        tf = parse_task_md("## 現在地\n~~~\n```\n# a\n~~~\n## 次の一手\n````md\n```\n## b\n````\nやる\n")
        self.assertEqual(tf.sections["現在地"], "~~~\n```\n# a\n~~~")
        self.assertEqual(tf.sections["次の一手"], "````md\n```\n## b\n````\nやる")
        tf = parse_task_md("## 現在地\n  ```\n## 中\n## 決めたこと\nx\n")
        self.assertEqual(tf.sections["現在地"], "```\n## 中\n## 決めたこと\nx")  # 欄の前後の空白は落とす
        self.assertNotIn("決めたこと", tf.sections)
        # ``` の後に ` を含む行は囲みではない（行の中のコード。画面の render と同じ決まり）
        tf = parse_task_md("## 現在地\n``` a ` b\n## 次の一手\nx\n")
        self.assertEqual((tf.sections["現在地"], tf.sections["次の一手"]), ("``` a ` b", "x"))

    def test_title_fallbacks(self):
        self.assertEqual(task_title(parse_task_md("# 見出し\n"), "2026-11-20-x"), "見出し")
        self.assertEqual(task_title(parse_task_md(""), "2026-11-20-x"), "2026-11-20-x")


class HelpersTest(unittest.TestCase):
    def test_stage_label(self):
        self.assertEqual(stage_label("実装"), "実装・テスト")
        self.assertEqual(stage_label("PR"), "PR")

    def test_no_waiting(self):
        for text in (None, "", " なし \n"):
            self.assertTrue(no_waiting(text))
        self.assertFalse(no_waiting("- 経理の回答"))

    def test_ticket_numbers(self):
        self.assertEqual(ticket_numbers("#123"), ["123"])
        self.assertEqual(ticket_numbers("123, #124"), ["123", "124"])
        self.assertEqual(ticket_numbers("https://github.com/o/r/issues/77"), ["77"])
        self.assertEqual(ticket_numbers(None), [])

    def test_ref_paths(self):
        section = ("- [全体像](../shared/auth.md)\n- @/Users/me/dev/web-app/docs/api.md\n"
                   "- ./research-x.md\n- ~/notes/a.md\n- 説明だけの行\n- ./research-x.md\n")
        self.assertEqual(ref_paths(section, "/Users/me/tasks/2026-11-20-x", "/Users/me"), [
            "/Users/me/tasks/shared/auth.md", "/Users/me/dev/web-app/docs/api.md",
            "/Users/me/tasks/2026-11-20-x/research-x.md", "/Users/me/notes/a.md"])
        self.assertEqual(ref_paths(None, "/t", "/h"), [])

    def test_task_dir_names_and_created(self):
        self.assertTrue(is_task_dir_name("2026-11-20-x"))
        for bad in ("shared", ".git", "_archive"):
            self.assertFalse(is_task_dir_name(bad))
        self.assertEqual(created_from_name("2026-11-20-login"), "2026-11-20")
        self.assertIsNone(created_from_name("login"))

    def test_task_doc_kind(self):
        root = "/Users/me/tasks"
        cases = {"/2026-11-20-x/task.md": "cover", "/2026-11-20-x/Research_auth.md": "research",
                 "/2026-11-20-x/design-a.html": "design", "/2026-11-20-x/plan-a.md": "plan",
                 "/2026-11-20-x/review-pr12.md": "response", "/shared/notes-team.md": "notes",
                 "/2026-11-20-x/memo.md": None, "/2026-11-20-x/research.md": None,
                 # 表紙はタスクのフォルダーの直下の task.md だけ
                 "/2026-11-20-x/sub/task.md": None, "/shared/task.md": None}
        for tail, kind in cases.items():
            self.assertEqual(task_doc_kind(root + tail, root), kind, tail)
        self.assertIsNone(task_doc_kind("/Users/me/dev/research-a.md", root))
        self.assertIsNone(task_doc_kind("/Users/me/tasks-x/2026-11-20-x/plan-a.md", root))
        self.assertIsNone(task_doc_kind(root + "/_old/plan-a.md", root))


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")


class FilesTest(unittest.TestCase):
    def test_discover_tasks(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("a", "shared", "_old"):
                _touch(os.path.join(root, d, "task.md"))
            os.makedirs(os.path.join(root, "b"))
            _touch(os.path.join(root, "file.txt"))
            with tempfile.TemporaryDirectory() as outside:
                _touch(os.path.join(outside, "task.md"))
                os.symlink(outside, os.path.join(root, "linked"))
                self.assertEqual(discover_tasks(root), [os.path.join(root, "a")])
            self.assertEqual(discover_tasks(os.path.join(root, "none")), [])

    def test_list_task_files_skips_and_caps(self):
        with tempfile.TemporaryDirectory() as task_dir, tempfile.TemporaryDirectory() as outside:
            _touch(os.path.join(task_dir, "task.md"))
            _touch(os.path.join(task_dir, "img", "shot.png"))
            _touch(os.path.join(task_dir, ".hidden"))
            _touch(os.path.join(task_dir, "node_modules", "x.js"))
            for i in range(1, 6):
                _touch(os.path.join(task_dir, "data", "%d.csv" % i))
            _touch(os.path.join(outside, "o.txt"))
            os.symlink(outside, os.path.join(task_dir, "link"))
            files, cut = list_task_files(task_dir, exclude_dirs=["node_modules"], max_depth=6, max_files=4)
            self.assertEqual(len(files), 4)
            self.assertTrue(cut)
            self.assertFalse(any(".hidden" in f or "node_modules" in f or "/link/" in f for f in files))
            files, cut = list_task_files(task_dir, exclude_dirs=["node_modules"], max_depth=6, max_files=100)
            self.assertEqual(len(files), 7)
            self.assertFalse(cut)
            files, cut = list_task_files(task_dir, exclude_dirs=[], max_depth=1, max_files=100)
            self.assertFalse(any("/data/" in f for f in files))
            self.assertTrue(any(f.endswith("/img/shot.png") for f in files) is False)


if __name__ == "__main__":
    unittest.main()
