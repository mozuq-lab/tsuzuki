"""導入物の雛形と tsuzuki templates のテスト。"""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki import cli
from tsuzuki.export import (TEMPLATES, display_path, export_templates, readme_is_current, readme_section, rule_line,
                            template_text)

RULE_LINE = ("- コミットしない作業用のファイル（テスト用の SQL、確かめ用のスクリプト、データ、実行結果）は、一時領域ではなく、"
             "その会話のタスクのフォルダーに置く（タスクが分からなければ、これまでどおり）。\n")
SNIPPET = (
    "## タスクと資料\n"
    "- タスクの資料は ~/tasks/<タスクのフォルダー>/ に置く。決まりは ~/tasks/README.md にあるので、資料や引き継ぎを書く前に読む。\n"
    "- 設計書や計画を書くスキル（superpowers など）の既定の置き場所より、この決まりを優先する。\n"
    "- タスクの状況（現在地・次の一手など）は、自動メモリではなく、そのタスクの task.md に書く。\n"
    + RULE_LINE)
README_SECTION = (
    "## 作業用のファイル\n\n"
    "- コミットしない作業用のファイル（テスト用の SQL、確かめ用のスクリプト、サンプルのデータ、実行結果、ログ、画面の写し）は、"
    "その会話のタスクのフォルダーに置く。一時領域（/tmp など）には置かない\n"
    "- コミットする物（ソースコード、コミットするテスト、設定）は、これまでどおりリポジトリに置く\n"
    "- 増えたらフォルダーを分けてよい（例：`sql/`・`out/`）。`.` で始まる名前と `build` はカードに並ばないので使わない\n"
    "- 名前の先頭に資料の種類の語（`research-` など）を付けない\n"
    "- その場かぎりの中間の出力と、タスクが分からない会話のファイルは、これまでどおりでよい\n")
EXPORTED = ["tasks/README.md", "CLAUDE.md-snippet.md", "skills/task-start/SKILL.md", "skills/task-handoff/SKILL.md"]


class TemplateTextTest(unittest.TestCase):
    def test_snippet_is_spec_text(self):
        self.assertEqual(template_text("templates/claude-md-snippet.md", tasks_root_display="~/tasks"), SNIPPET)
        self.assertLessEqual(len(SNIPPET), 300)
        self.assertEqual(rule_line(tasks_root_display="~/tasks"), RULE_LINE)

    def test_readme_limits_and_contents(self):
        text = template_text("templates/tasks-README.md", tasks_root_display="~/tasks")
        self.assertLessEqual(len(text), 2300)
        self.assertTrue(text.endswith("\n\n" + README_SECTION))
        self.assertEqual(readme_section(tasks_root_display="~/tasks"), README_SECTION)
        for word in ("YYYY-MM-DD-", "shared/", "task.md", "research-", "design-", "plan-", "review-", "notes-",
                     "目的", "現在地", "次の一手", "待っていること", "決めたこと", "資料",
                     "チケット", "調査", "調整", "実装", "PR", "修正", "完了"):
            self.assertIn(word, text)

    def test_readme_is_current(self):
        self.assertTrue(readme_is_current(template_text("templates/tasks-README.md", tasks_root_display="~/tasks")))
        self.assertTrue(readme_is_current("前\n## 作業用のファイル  \r\n"))
        for text in ("", "# 決まり\n", "### 作業用のファイル\n", "作業用のファイルは置く\n", "## 作業用のファイルの話\n"):
            with self.subTest(text=text):
                self.assertFalse(readme_is_current(text))

    def test_skills(self):
        for name, words in (("task-start", ("tsuzuki task new", "gh issue view", "gh issue create", "@")),
                            ("task-handoff", ("date '+%Y-%m-%dT%H:%M'", "tsuzuki task list", "現在地", "次の一手"))):
            text = template_text("templates/skills/%s/SKILL.md" % name, tasks_root_display="~/tasks")
            front = text.split("---")[1]
            self.assertIn("name: %s\n" % name, front)
            description = [l for l in front.splitlines() if l.startswith("description:")][0][len("description:"):].strip()
            self.assertLessEqual(len(description), 150)
            for word in words:
                self.assertIn(word, text)
            # Claude Code は見出し情報を YAML として読む。「: 」と空白の後の「#」は値を壊す。
            # 置き場所のパスは置き換えで長さが変わるので、説明には書かない
            self.assertNotIn(": ", description, name)
            self.assertIsNone(re.search(r"(^|\s)#", description), name)
            self.assertNotIn("~/tasks", description, name)

    def test_templates_table(self):
        self.assertEqual([rel for _, rel in TEMPLATES], EXPORTED)


class _TmpCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.out = self.base / "out"


class ExportTest(_TmpCase):
    def test_export_and_substitution(self):
        out = self.out
        written = export_templates(out, tasks_root_display="/data/tasks")
        self.assertEqual([p.relative_to(out).as_posix() for p in written], EXPORTED)
        self.assertIn("/data/tasks/README.md", (out / "CLAUDE.md-snippet.md").read_text(encoding="utf-8"))
        self.assertNotIn("~/tasks", (out / "tasks/README.md").read_text(encoding="utf-8"))
        for path in written:
            self.assertNotIn("~/tasks", path.read_text(encoding="utf-8"), path)

    def test_export_writes_the_templates_as_they_are(self):
        written = export_templates(self.out, tasks_root_display="~/tasks")
        for (name, _), path in zip(TEMPLATES, written):
            self.assertEqual(path.read_text(encoding="utf-8"), template_text(name, tasks_root_display="~/tasks"))

    def test_export_refuses_existing(self):
        out = self.out
        (out / "skills/task-start").mkdir(parents=True)
        (out / "skills/task-start/SKILL.md").write_text("mine", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            export_templates(out, tasks_root_display="~/tasks")
        self.assertFalse((out / "tasks/README.md").exists())
        self.assertEqual((out / "skills/task-start/SKILL.md").read_text(encoding="utf-8"), "mine")

    def test_export_refuses_a_file_in_the_way(self):
        # 途中のフォルダーの名前にファイルがあっても、何も書かない
        self.out.mkdir()
        (self.out / "skills").write_text("mine", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            export_templates(self.out, tasks_root_display="~/tasks")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["skills"])

    def test_display_path(self):
        self.assertEqual(display_path(Path("/Users/me/tasks"), "/Users/me"), "~/tasks")
        self.assertEqual(display_path(Path("/Users/me/work/tasks/"), "/Users/me/"), "~/work/tasks")
        self.assertEqual(display_path(Path("/Users/meme/tasks"), "/Users/me"), "/Users/meme/tasks")
        self.assertEqual(display_path(Path("/Volumes/data/tasks"), "/Users/me"), "/Volumes/data/tasks")


class CliTest(_TmpCase):
    def setUp(self):
        super().setUp()
        self.home = self.base / "home"
        self.claude = self.home / ".claude"
        self.ledger = self.base / "ledger"
        self.home.mkdir()

    def templates(self, out, config=None):
        if config is not None:
            self.ledger.mkdir()
            (self.ledger / "config.json").write_text(json.dumps(config), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["--ledger-dir", str(self.ledger), "--claude-dir", str(self.claude),
                             "templates", str(out)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_prints_each_file_and_where_to_put_it(self):
        code, out, err = self.templates(self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "書き出しました: %s → ~/tasks/README.md に置く" % (self.out / "tasks" / "README.md"),
            "書き出しました: %s → 中身を ~/.claude/CLAUDE.md に足す" % (self.out / "CLAUDE.md-snippet.md"),
            "書き出しました: %s → ~/.claude/skills/task-start/SKILL.md に置く"
            % (self.out / "skills" / "task-start" / "SKILL.md"),
            "書き出しました: %s → ~/.claude/skills/task-handoff/SKILL.md に置く"
            % (self.out / "skills" / "task-handoff" / "SKILL.md"),
        ])
        self.assertIn("~/tasks/README.md", (self.out / "CLAUDE.md-snippet.md").read_text(encoding="utf-8"))
        self.assertFalse(self.claude.exists())  # Claude Code の設定ディレクトリには何も書かない
        self.assertFalse(self.ledger.exists())  # 台帳のディレクトリも作らない

    def test_tasks_root_outside_home_is_written_as_absolute_path(self):
        tasks = self.base / "elsewhere" / "tasks"
        code, out, err = self.templates(self.out, config={"tasks_root": str(tasks)})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("→ %s/README.md に置く" % tasks, out.splitlines()[0])
        self.assertIn("%s/README.md" % tasks, (self.out / "CLAUDE.md-snippet.md").read_text(encoding="utf-8"))
        self.assertNotIn("~/tasks", (self.out / "tasks" / "README.md").read_text(encoding="utf-8"))

    def test_refuses_existing_file(self):
        self.out.mkdir()
        (self.out / "CLAUDE.md-snippet.md").write_text("mine", encoding="utf-8")
        code, out, err = self.templates(self.out)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("同じ名前のファイルがあります", err)
        self.assertIn(str(self.out / "CLAUDE.md-snippet.md"), err)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["CLAUDE.md-snippet.md"])
        self.assertEqual((self.out / "CLAUDE.md-snippet.md").read_text(encoding="utf-8"), "mine")

    def test_refuses_claude_dir(self):
        self.claude.mkdir()
        code, out, err = self.templates(self.claude / "exports")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Claude Code の設定ディレクトリ", err)
        self.assertEqual(list(self.claude.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
