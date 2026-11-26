"""タスクごとの「Claude で使う準備」（taskstart.py・taskprep.py）のテスト。一時ディレクトリだけを使う。"""
import json
import os
from unittest import mock
import stat
import tempfile
import unittest
from pathlib import Path

from tsuzuki import taskprep
from tsuzuki.export import readme_section, rule_line, template_text
from tsuzuki.pathguard import is_inside
from tsuzuki.taskprep import TaskPrepError
from tsuzuki.taskstart import git_root, start_dir


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.top = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.top, "home")
        self.claude = os.path.join(self.top, "home", ".claude")
        self.root = os.path.join(self.top, "home", "tasks")
        self.repo = os.path.join(self.top, "home", "dev", "app")
        for d in (self.claude, self.root, os.path.join(self.repo, ".git"), os.path.join(self.repo, "src")):
            os.makedirs(d)
        self.task = os.path.join(self.root, "2026-11-25-x")
        os.makedirs(self.task)

    def write(self, path, text="x", binary=False):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8", "newline": ""})) as f:
            f.write(text)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def missing(self, start):
        return taskprep.check(start, self.root, home=self.home, claude_dir=self.claude).missing

    def stale(self, start):
        return taskprep.check(start, self.root, home=self.home, claude_dir=self.claude).stale


class StaleTest(Base):
    def line(self):
        return rule_line(tasks_root_display="~/tasks")

    def section(self):
        return readme_section(tasks_root_display="~/tasks")

    def test_readme_without_the_section_is_stale_not_missing(self):
        self.write(os.path.join(self.root, "README.md"), "# 古い決まり\n")
        c = taskprep.check(self.repo, self.root, home=self.home, claude_dir=self.claude)
        self.assertNotIn("readme", c.missing)
        self.assertEqual(c.stale, ("readme",))
        self.write(os.path.join(self.root, "README.md"), "# 決まり\n\n## 作業用のファイル\n")
        self.assertEqual(self.stale(self.repo), ())

    def test_an_unreadable_readme_is_not_called_stale(self):
        readme = os.path.join(self.root, "README.md")
        self.write(readme, "# 古い決まり\n")
        os.chmod(readme, 0)
        self.addCleanup(os.chmod, readme, 0o644)
        self.assertEqual(self.stale(self.repo), ())

    def test_rule_without_the_line_is_stale_wherever_the_line_lives(self):
        self.write(os.path.join(self.claude, "CLAUDE.md"), "~/tasks/README.md を読む\n")
        self.assertNotIn("rule", self.missing(self.repo))
        self.assertIn("rule", self.stale(self.repo))
        self.write(os.path.join(self.repo, "CLAUDE.local.md"), self.line())  # 1行が別のファイルにあってもよい
        self.assertNotIn("rule", self.stale(self.repo))

    def test_missing_rule_is_not_also_stale(self):
        self.assertIn("rule", self.missing(self.repo))
        self.assertNotIn("rule", self.stale(self.repo))

    def test_add_rule_line_appends_only_the_line_under_the_existing_heading(self):
        path = os.path.join(self.repo, "CLAUDE.local.md")
        old = "## タスクと資料\n- ~/tasks/README.md を読む\n"
        self.write(path, old)
        self.assertEqual(taskprep.add_rule_line(self.repo, self.line(), claude_dir=self.claude), path)
        self.assertEqual(self.read(path).decode("utf-8"), old + "\n" + self.line())
        self.assertNotIn("rule", self.stale(self.repo))

    def test_add_rule_line_brings_the_heading_when_the_file_lacks_it(self):
        glob = os.path.join(self.claude, "CLAUDE.md")
        self.write(glob, "~/tasks/README.md を読む\n")
        path = taskprep.add_rule_line(self.repo, self.line(), claude_dir=self.claude)
        self.assertEqual(self.read(path).decode("utf-8"), "## タスクと資料\n" + self.line())
        self.assertNotIn("rule", self.stale(self.repo))
        self.assertEqual(self.read(glob).decode("utf-8"), "~/tasks/README.md を読む\n")

    def test_add_rule_line_twice_adds_once(self):
        taskprep.add_rule_line(self.repo, self.line(), claude_dir=self.claude)
        first = self.read(os.path.join(self.repo, "CLAUDE.local.md"))
        taskprep.add_rule_line(self.repo, self.line(), claude_dir=self.claude)
        self.assertEqual(self.read(os.path.join(self.repo, "CLAUDE.local.md")), first)

    def test_add_rule_line_keeps_crlf_and_refuses_links_and_the_claude_dir(self):
        path = os.path.join(self.repo, "CLAUDE.local.md")
        self.write(path, "## タスクと資料\r\nx\r\n".encode("utf-8"), binary=True)
        taskprep.add_rule_line(self.repo, self.line(), claude_dir=self.claude)
        data = self.read(path)
        self.assertTrue(data.startswith("## タスクと資料\r\nx\r\n\r\n- ".encode("utf-8")))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        os.unlink(path)
        target = os.path.join(self.top, "elsewhere.md")
        self.write(target, "x")
        os.symlink(target, path)
        with self.assertRaises(TaskPrepError):
            taskprep.add_rule_line(self.repo, self.line(), claude_dir=self.claude)
        self.assertEqual(self.read(target), b"x")
        with self.assertRaises(TaskPrepError):
            taskprep.add_rule_line(self.claude, self.line(), claude_dir=self.claude)

    def test_readme_section_is_appended_once_and_old_bytes_stay(self):
        readme = os.path.join(self.root, "README.md")
        for old, tail in ((b"# old\nx\n", b"\n"), (b"# old\nx", b"\n\n"), (b"# old\n\n", b""),
                          (b"# old\r\nx\r\n", b"\r\n")):
            with self.subTest(old=old):
                self.write(readme, old, binary=True)
                os.chmod(readme, 0o600)
                self.assertEqual(taskprep.append_readme_section(self.root, self.section(), claude_dir=self.claude),
                                 readme)
                data = self.read(readme)
                nl = b"\r\n" if b"\r\n" in old else b"\n"
                self.assertEqual(data, old + tail + self.section().encode("utf-8").replace(b"\n", nl))
                self.assertEqual(stat.S_IMODE(os.stat(readme).st_mode), 0o600)
                self.assertEqual(self.stale(self.repo), ())
                with self.assertRaises(TaskPrepError):
                    taskprep.append_readme_section(self.root, self.section(), claude_dir=self.claude)
                self.assertEqual(self.read(readme), data)

    def test_readme_section_refuses_a_missing_file_a_link_and_the_claude_dir(self):
        readme = os.path.join(self.root, "README.md")
        with self.assertRaises(TaskPrepError) as e:
            taskprep.append_readme_section(self.root, self.section(), claude_dir=self.claude)
        self.assertEqual(str(e.exception), "README.md がありません: %s" % readme)
        target = os.path.join(self.top, "real-readme.md")
        self.write(target, "x")
        os.symlink(target, readme)
        with self.assertRaises(TaskPrepError):
            taskprep.append_readme_section(self.root, self.section(), claude_dir=self.claude)
        self.assertEqual(self.read(target), b"x")
        inside = os.path.join(self.claude, "tasks")
        self.write(os.path.join(inside, "README.md"), "x")
        with self.assertRaises(TaskPrepError):
            taskprep.append_readme_section(inside, self.section(), claude_dir=self.claude)
        self.assertEqual(self.read(os.path.join(inside, "README.md")), b"x")


class StartDirTest(Base):
    def test_workspace_then_repo_then_task_folder(self):
        ws = os.path.join(self.repo, "x.code-workspace")
        self.assertEqual(start_dir(self.task, self.repo, ws), self.repo)
        self.assertEqual(start_dir(self.task, self.repo, None), self.repo)
        self.assertEqual(start_dir(self.task, None, None), self.task)
        self.assertEqual(start_dir(self.task, "/no/such", None), self.task)
        self.assertEqual(start_dir(self.task, self.repo, None, isdir=lambda p: False), self.task)

    def test_git_root_walks_up_and_accepts_a_git_file(self):
        self.assertEqual(git_root(os.path.join(self.repo, "src")), self.repo)
        self.assertIsNone(git_root(self.task))
        wt = os.path.join(self.top, "wt")
        self.write(os.path.join(wt, ".git"), "gitdir: x")
        self.assertEqual(git_root(wt), wt)


class CheckTest(Base):
    def test_everything_missing_for_a_repo(self):
        c = taskprep.check(self.repo, self.root, home=self.home, claude_dir=self.claude)
        self.assertEqual(c.missing, ("readme", "rule", "skills", "allow"))
        self.assertEqual(c.allow_file, os.path.join(self.repo, ".claude", "settings.local.json"))

    def test_allow_is_not_needed_when_the_tasks_root_is_the_start_folder_or_inside_it(self):
        self.assertEqual(self.missing(self.root), ("readme", "rule", "skills"))
        self.assertEqual(self.missing(self.task), ("readme", "rule", "skills", "allow"))

    def test_readme(self):
        self.write(os.path.join(self.root, "README.md"))
        self.assertNotIn("readme", self.missing(self.repo))

    def test_rule_in_start_parent_local_and_global(self):
        ref = "~/tasks/README.md を読む"
        for where in (os.path.join(self.repo, "CLAUDE.md"), os.path.join(self.repo, "CLAUDE.local.md"),
                      os.path.join(self.repo, ".claude", "CLAUDE.md"), os.path.join(self.top, "home", "dev", "CLAUDE.md"),
                      os.path.join(self.claude, "CLAUDE.md")):
            with self.subTest(where=where):
                self.write(where, ref)
                self.assertNotIn("rule", self.missing(os.path.join(self.repo, "src")))
                os.unlink(where)
        self.write(os.path.join(self.repo, "CLAUDE.md"), "%s/README.md" % self.root)
        self.assertNotIn("rule", self.missing(self.repo))
        self.write(os.path.join(self.repo, "CLAUDE.md"), "関係ない")
        self.assertIn("rule", self.missing(self.repo))

    def test_skills_in_start_parent_up_to_git_root_and_global(self):
        def put(base):
            for n in ("task-start", "task-handoff"):
                self.write(os.path.join(base, ".claude", "skills", n, "SKILL.md"))
        put(self.repo)
        self.assertNotIn("skills", self.missing(os.path.join(self.repo, "src")))
        self.assertNotIn("skills", self.missing(self.repo))
        os.unlink(os.path.join(self.repo, ".claude", "skills", "task-handoff", "SKILL.md"))
        self.assertIn("skills", self.missing(self.repo))
        put(os.path.join(self.top, "home", "dev"))  # git root より上の親には見に行かない
        self.assertIn("skills", self.missing(self.repo))
        for n in ("task-start", "task-handoff"):
            self.write(os.path.join(self.claude, "skills", n, "SKILL.md"))
        self.assertNotIn("skills", self.missing(self.repo))

    def test_skills_outside_a_repo_look_only_at_the_start_folder(self):
        for n in ("task-start", "task-handoff"):
            self.write(os.path.join(self.root, ".claude", "skills", n, "SKILL.md"))
        self.assertIn("skills", self.missing(self.task))
        for n in ("task-start", "task-handoff"):
            self.write(os.path.join(self.task, ".claude", "skills", n, "SKILL.md"))
        self.assertNotIn("skills", self.missing(self.task))

    def test_allow_from_local_settings_at_git_root_not_cwd(self):
        sub = os.path.join(self.repo, "src")
        self.write(os.path.join(sub, ".claude", "settings.local.json"), json.dumps(
            {"permissions": {"additionalDirectories": [self.root]}}))
        self.assertIn("allow", self.missing(sub))  # git root の設定だけを Claude は読む
        self.assertEqual(taskprep.check(sub, self.root, home=self.home, claude_dir=self.claude).allow_file,
                         os.path.join(self.repo, ".claude", "settings.local.json"))
        self.write(os.path.join(self.repo, ".claude", "settings.local.json"), json.dumps(
            {"permissions": {"additionalDirectories": ["~/tasks"]}}))
        self.assertNotIn("allow", self.missing(sub))

    def test_allow_from_settings_json_and_global_and_parent_dir(self):
        self.write(os.path.join(self.repo, ".claude", "settings.json"), json.dumps(
            {"permissions": {"additionalDirectories": [self.home]}}))
        self.assertNotIn("allow", self.missing(self.repo))
        os.unlink(os.path.join(self.repo, ".claude", "settings.json"))
        self.assertIn("allow", self.missing(self.repo))
        self.write(os.path.join(self.claude, "settings.json"), json.dumps(
            {"permissions": {"additionalDirectories": [self.root]}}))
        self.assertNotIn("allow", self.missing(self.repo))

    def test_unreadable_settings_count_as_missing(self):
        self.write(os.path.join(self.repo, ".claude", "settings.local.json"), "{broken")
        self.assertIn("allow", self.missing(self.repo))


class CheckRobustnessTest(Base):
    def test_a_dotdot_start_walks_the_real_parents(self):
        self.write(os.path.join(self.top, "home", "dev", "CLAUDE.md"), "~/tasks/README.md")
        odd = os.path.join(self.repo, "..", "app")
        self.assertNotIn("rule", self.missing(odd))

    @unittest.skipIf(os.geteuid() == 0, "root は読めてしまう")
    def test_unreadable_folders_count_as_missing_and_never_raise(self):
        dot = os.path.join(self.repo, ".claude")
        os.makedirs(dot)
        os.chmod(dot, 0)
        self.addCleanup(os.chmod, dot, 0o755)
        self.assertEqual(self.missing(self.repo), ("readme", "rule", "skills", "allow"))


class RuleTest(Base):
    def snippet(self):
        return template_text("templates/claude-md-snippet.md", tasks_root_display="~/tasks")

    def place(self, start=None):
        return taskprep.place_rule(start or self.repo, self.snippet(), claude_dir=self.claude)

    def test_creates_the_file(self):
        path = self.place()
        self.assertEqual(path, os.path.join(self.repo, "CLAUDE.local.md"))
        self.assertEqual(self.read(path), self.snippet().encode("utf-8"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertNotIn("rule", self.missing(self.repo))

    def test_appends_after_a_blank_line_and_keeps_the_old_bytes(self):
        path = os.path.join(self.repo, "CLAUDE.local.md")
        for old in ("自分用\n", "自分用", "自分用\n\n", "自分用\n\n\n"):
            with self.subTest(old=old):
                self.write(path, old.encode("utf-8"), binary=True)
                os.chmod(path, 0o600)
                self.place()
                data = self.read(path)
                self.assertTrue(data.startswith(old.encode("utf-8")))
                rest = data[len(old.encode("utf-8")):]
                if old.endswith("\n\n"):
                    self.assertEqual(rest, self.snippet().encode("utf-8"))
                elif old.endswith("\n"):
                    self.assertEqual(rest, b"\n" + self.snippet().encode("utf-8"))
                else:
                    self.assertEqual(rest, b"\n\n" + self.snippet().encode("utf-8"))
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_empty_existing_file_gets_just_the_snippet(self):
        path = os.path.join(self.repo, "CLAUDE.local.md")
        self.write(path, b"", binary=True)
        self.place()
        self.assertEqual(self.read(path), self.snippet().encode("utf-8"))

    def test_crlf_files_get_crlf(self):
        path = os.path.join(self.repo, "CLAUDE.local.md")
        self.write(path, b"a\r\nb\r\n", binary=True)
        self.place()
        data = self.read(path)
        self.assertTrue(data.startswith(b"a\r\nb\r\n\r\n"))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertIn(b"## \xe3\x82\xbf\xe3\x82\xb9\xe3\x82\xaf\xe3\x81\xa8\xe8\xb3\x87\xe6\x96\x99\r\n", data)

    def test_refuses_a_symlink_and_leaves_the_target(self):
        target = os.path.join(self.top, "real.md")
        self.write(target, "元")
        os.symlink(target, os.path.join(self.repo, "CLAUDE.local.md"))
        with self.assertRaises(TaskPrepError) as cm:
            self.place()
        self.assertIn("シンボリックリンク", str(cm.exception))
        self.assertEqual(self.read(target), "元".encode("utf-8"))

    @unittest.skipIf(os.geteuid() == 0, "root は読み取り専用でも書ける")
    def test_refuses_a_read_only_file_untouched(self):
        path = os.path.join(self.repo, "CLAUDE.local.md")
        self.write(path, "元")
        os.chmod(path, 0o444)
        with self.assertRaises(TaskPrepError) as cm:
            self.place()
        self.assertIn(path, str(cm.exception))
        self.assertEqual(self.read(path), "元".encode("utf-8"))
        self.assertEqual(os.listdir(self.repo).count("CLAUDE.local.md"), 1)
        self.assertFalse([n for n in os.listdir(self.repo) if "tmp" in n])

    def test_refuses_inside_the_claude_dir_and_a_missing_folder(self):
        with self.assertRaises(TaskPrepError):
            self.place(self.claude)
        with self.assertRaises(TaskPrepError):
            self.place(os.path.join(self.claude, "sub"))
        with self.assertRaises(TaskPrepError):
            self.place(os.path.join(self.top, "nowhere"))
        self.assertEqual(os.listdir(self.claude), [])


class SkillsTest(Base):
    def place(self, start=None):
        return taskprep.place_skills(start or self.repo, tasks_root_display="~/tasks", claude_dir=self.claude)

    def test_places_both_from_the_templates(self):
        skills_dir, placed, kept = self.place()
        self.assertEqual(skills_dir, os.path.join(self.repo, ".claude", "skills"))
        self.assertEqual(placed, ["task-start", "task-handoff"])
        self.assertEqual(kept, [])
        for n in placed:
            path = os.path.join(skills_dir, n, "SKILL.md")
            self.assertEqual(self.read(path), template_text("templates/skills/%s/SKILL.md" % n,
                                                           tasks_root_display="~/tasks").encode("utf-8"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertNotIn("skills", self.missing(self.repo))

    def test_substitutes_the_tasks_root(self):
        taskprep.place_skills(self.repo, tasks_root_display="~/work/t", claude_dir=self.claude)
        text = self.read(os.path.join(self.repo, ".claude", "skills", "task-start", "SKILL.md")).decode("utf-8")
        self.assertIn("~/work/t/README.md", text)
        self.assertNotIn("~/tasks", text)

    def test_existing_one_is_kept(self):
        mine = os.path.join(self.repo, ".claude", "skills", "task-handoff", "SKILL.md")
        self.write(mine, "自分の")
        _, placed, kept = self.place()
        self.assertEqual((placed, kept), (["task-start"], ["task-handoff"]))
        self.assertEqual(self.read(mine), "自分の".encode("utf-8"))

    def test_a_skill_link_to_a_file_inside_the_start_folder_is_kept_untouched(self):
        target = os.path.join(self.repo, "docs", "t.md")
        self.write(target, "元")
        d = os.path.join(self.repo, ".claude", "skills", "task-start")
        os.makedirs(d)
        os.symlink(target, os.path.join(d, "SKILL.md"))
        _, placed, kept = self.place()
        self.assertEqual(kept, ["task-start"])
        self.assertEqual(self.read(target), "元".encode("utf-8"))

    def test_a_skill_link_to_a_file_outside_the_start_folder_is_refused_untouched(self):
        target = os.path.join(self.top, "t.md")
        self.write(target, "元")
        d = os.path.join(self.repo, ".claude", "skills", "task-start")
        os.makedirs(d)
        os.symlink(target, os.path.join(d, "SKILL.md"))
        with self.assertRaises(TaskPrepError):
            self.place()
        self.assertEqual(self.read(target), "元".encode("utf-8"))

    def test_refuses_inside_the_claude_dir(self):
        with self.assertRaises(TaskPrepError):
            self.place(self.claude)
        self.assertEqual(os.listdir(self.claude), [])


class SkillsLinkTest(Base):
    def place(self):
        return taskprep.place_skills(self.repo, tasks_root_display="~/tasks", claude_dir=self.claude)

    def test_a_skill_folder_linked_into_the_claude_dir_is_refused(self):
        target = os.path.join(self.claude, "skills", "task-start")
        os.makedirs(target)
        os.makedirs(os.path.join(self.repo, ".claude", "skills"))
        os.symlink(target, os.path.join(self.repo, ".claude", "skills", "task-start"))
        with self.assertRaises(TaskPrepError):
            self.place()
        self.assertEqual(os.listdir(target), [])

    def test_a_dot_claude_linked_outside_the_start_folder_is_refused(self):
        outside = os.path.join(self.top, "elsewhere")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, ".claude"))
        with self.assertRaises(TaskPrepError) as cm:
            self.place()
        self.assertIn("外", str(cm.exception))
        self.assertEqual(os.listdir(outside), [])

    def test_a_dangling_skill_link_is_refused_with_a_clear_reason(self):
        d = os.path.join(self.repo, ".claude", "skills", "task-start")
        os.makedirs(d)
        os.symlink(os.path.join(self.repo, "gone.md"), os.path.join(d, "SKILL.md"))
        with self.assertRaises(TaskPrepError) as cm:
            self.place()
        self.assertIn("シンボリックリンク", str(cm.exception))
        self.assertIn("消してから", str(cm.exception))

    def test_a_failure_after_the_first_skill_says_what_was_placed(self):
        d = os.path.join(self.repo, ".claude", "skills", "task-handoff")
        os.makedirs(d)
        os.symlink(os.path.join(self.repo, "gone.md"), os.path.join(d, "SKILL.md"))
        with self.assertRaises(TaskPrepError) as cm:
            self.place()
        self.assertTrue(str(cm.exception).startswith("task-start は置きました。"), str(cm.exception))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".claude", "skills", "task-start", "SKILL.md")))


class FileLinkAndMessageTest(Base):
    def test_a_start_folder_that_is_a_link_into_the_claude_dir_is_refused(self):
        link = os.path.join(self.top, "link")
        os.symlink(self.claude, link)
        with self.assertRaises(TaskPrepError):
            taskprep.place_rule(link, "x\n", claude_dir=self.claude)

    def test_whitespace_only_or_empty_settings_are_refused_untouched(self):
        path = os.path.join(self.repo, ".claude", "settings.local.json")
        for text in ("", " \n"):
            with self.subTest(text=text):
                self.write(path, text)
                with self.assertRaises(TaskPrepError):
                    taskprep.place_allow(self.repo, self.root, claude_dir=self.claude)
                self.assertEqual(self.read(path), text.encode("utf-8"))

    def test_errors_name_the_target_not_the_temp_file(self):
        path = os.path.join(self.repo, "CLAUDE.local.md")
        self.write(path, "元")
        err = PermissionError(13, "Permission denied", path + ".tsuzuki-tmp-1")
        with mock.patch("tsuzuki.taskprep.os.replace", side_effect=err):
            with self.assertRaises(TaskPrepError) as cm:
                taskprep.place_rule(self.repo, "x\n", claude_dir=self.claude)
        self.assertIn(path, str(cm.exception))
        self.assertNotIn("tsuzuki-tmp", str(cm.exception))
        self.assertEqual(self.read(path), "元".encode("utf-8"))
        self.assertFalse([n for n in os.listdir(self.repo) if "tmp" in n])


class AllowTest(Base):
    def place(self, start=None):
        return taskprep.place_allow(start or self.repo, self.root, claude_dir=self.claude)

    def settings(self):
        return os.path.join(self.repo, ".claude", "settings.local.json")

    def test_creates_the_file_with_the_absolute_root(self):
        path = self.place()
        self.assertEqual(path, self.settings())
        raw = self.read(path).decode("utf-8")
        self.assertEqual(json.loads(raw), {"permissions": {"additionalDirectories": [self.root]}})
        self.assertTrue(raw.endswith("}\n"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertNotIn("allow", self.missing(self.repo))

    def test_uses_the_git_root_from_a_subfolder(self):
        self.assertEqual(self.place(os.path.join(self.repo, "src")), self.settings())

    def test_merges_and_preserves_other_keys_and_mode(self):
        self.write(self.settings(), json.dumps({"model": "x", "permissions": {"allow": ["Read"],
                                                                              "additionalDirectories": ["/a"]}}))
        os.chmod(self.settings(), 0o600)
        self.place()
        data = json.loads(self.read(self.settings()))
        self.assertEqual(data, {"model": "x", "permissions": {"allow": ["Read"],
                                                              "additionalDirectories": ["/a", self.root]}})
        self.assertEqual(stat.S_IMODE(os.stat(self.settings()).st_mode), 0o600)

    def test_adds_missing_keys(self):
        self.write(self.settings(), "{}")
        self.place()
        self.assertEqual(json.loads(self.read(self.settings())),
                         {"permissions": {"additionalDirectories": [self.root]}})

    def test_non_ascii_is_kept_readable(self):
        self.write(self.settings(), json.dumps({"メモ": "日本語"}, ensure_ascii=False))
        self.place()
        self.assertIn("日本語", self.read(self.settings()).decode("utf-8"))

    def test_broken_or_wrongly_shaped_json_is_refused_untouched(self):
        for text in ("{broken", "[]", '{"permissions": []}', '{"permissions": {"additionalDirectories": "/x"}}'):
            with self.subTest(text=text):
                self.write(self.settings(), text)
                with self.assertRaises(TaskPrepError):
                    self.place()
                self.assertEqual(self.read(self.settings()), text.encode("utf-8"))

    def test_refuses_a_symlink(self):
        target = os.path.join(self.top, "s.json")
        self.write(target, "{}")
        os.makedirs(os.path.join(self.repo, ".claude"))
        os.symlink(target, self.settings())
        with self.assertRaises(TaskPrepError):
            self.place()
        self.assertEqual(self.read(target), b"{}")

    @unittest.skipIf(os.geteuid() == 0, "root は読み取り専用でも書ける")
    def test_refuses_a_read_only_file(self):
        self.write(self.settings(), "{}")
        os.chmod(self.settings(), 0o444)
        with self.assertRaises(TaskPrepError) as cm:
            self.place()
        self.assertIn(self.settings(), str(cm.exception))
        self.assertEqual(self.read(self.settings()), b"{}")

    def test_refuses_inside_the_claude_dir(self):
        with self.assertRaises(TaskPrepError):
            self.place(self.claude)
        self.assertEqual(os.listdir(self.claude), [])


def _case_insensitive(path):
    swapped = os.path.join(os.path.dirname(path), os.path.basename(path).swapcase())
    return os.path.exists(swapped)


class Task21Test(Base):
    def test_home_and_disk_roots_are_broad_and_get_no_items(self):
        for start in (self.home, self.home + "/", "/", "/Volumes", "/Volumes/Data"):
            with self.subTest(start=start):
                c = taskprep.check(start, self.root, home=self.home, claude_dir=self.claude)
                self.assertEqual((c.broad, c.missing), (True, ()))
        link = os.path.join(self.top, "homelink")
        os.symlink(self.home, link)
        self.assertTrue(taskprep.check(link, self.root, home=self.home, claude_dir=self.claude).broad)
        self.assertFalse(taskprep.check(self.repo, self.root, home=self.home, claude_dir=self.claude).broad)

    def test_git_root_and_skills_follow_a_start_folder_that_is_a_link_into_a_repo(self):
        link = os.path.join(self.top, "linked")
        os.symlink(os.path.join(self.repo, "src"), link)
        self.assertEqual(git_root(link), self.repo)
        for n in ("task-start", "task-handoff"):
            self.write(os.path.join(self.repo, ".claude", "skills", n, "SKILL.md"))
        self.assertNotIn("skills", self.missing(link))
        self.assertEqual(taskprep.allow_file_for(link), os.path.join(self.repo, ".claude", "settings.local.json"))

    def test_a_skill_file_that_is_a_folder_is_refused_with_a_reason(self):
        d = os.path.join(self.repo, ".claude", "skills", "task-start", "SKILL.md")
        os.makedirs(d)
        with self.assertRaises(TaskPrepError) as cm:
            taskprep.place_skills(self.repo, tasks_root_display="~/tasks", claude_dir=self.claude)
        self.assertIn("はフォルダーです。消すか名前を変えてから", str(cm.exception))

    @unittest.skipUnless(os.path.exists("/System/Volumes/Data"), "macOS のデータボリュームの別名がない")
    def test_the_data_volume_alias_of_the_claude_dir_is_refused(self):
        real = os.path.realpath(self.claude)
        alias = "/System/Volumes/Data" + real
        if not os.path.exists(alias):
            self.skipTest("別名で見えない")
        work = os.path.join(alias, "work")
        os.makedirs(work)
        with self.assertRaises(TaskPrepError):
            taskprep.place_rule(work, "x\n", claude_dir=self.claude)
        self.assertEqual(os.listdir(os.path.join(real, "work")), [])

    def test_case_variants_of_the_claude_dir_are_refused_on_a_case_insensitive_disk(self):
        if not _case_insensitive(self.claude):
            self.skipTest("大文字小文字を区別するディスク")
        variant = os.path.join(os.path.dirname(self.claude), os.path.basename(self.claude).swapcase())
        work = os.path.join(variant, "work")
        os.makedirs(work)
        with self.assertRaises(TaskPrepError):
            taskprep.place_rule(work, "x\n", claude_dir=self.claude)
        self.assertEqual(os.listdir(os.path.join(self.claude, "work")), [])
        self.assertTrue(is_inside(work, self.claude))

    def test_a_dot_claude_link_spelled_in_another_case_but_inside_start_is_not_refused(self):
        if not _case_insensitive(self.repo):
            self.skipTest("大文字小文字を区別するディスク")
        os.makedirs(os.path.join(self.repo, "Shared"))
        os.symlink(os.path.join(self.repo, "SHARED"), os.path.join(self.repo, ".claude"))
        taskprep.place_skills(self.repo, tasks_root_display="~/tasks", claude_dir=self.claude)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "Shared", "skills", "task-start", "SKILL.md")))

    def test_is_inside_by_identity_and_by_text(self):
        self.assertTrue(is_inside(os.path.join(self.claude, "a", "b"), self.claude))
        self.assertTrue(is_inside(self.claude, self.claude))
        self.assertFalse(is_inside(self.claude + "x", self.claude))
        self.assertFalse(is_inside(self.repo, self.claude))
        self.assertTrue(is_inside("/no/such/dir/x", "/no/such"))  # 比べられないときは文字で比べる


if __name__ == "__main__":
    unittest.main()
