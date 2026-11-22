"""tsuzuki doctor（doctor.py）のテスト。本物の ~/.claude も claude コマンドも使わない。"""
import contextlib
import functools
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki import cli, doctor
from tsuzuki.config import Config
from tsuzuki.doctor import Check, build_report, hook_configured, run_checks, task_checks, task_report
from tsuzuki.indexer import update
from tsuzuki.store import LEDGER_FILENAME, set_meta
from tests.fixtures import FakeClaude, assistant, make_ledger, recap, sid, t, title, tool_result, user

MARKER = "SECRET-MARKER-42"
NOW = 1_800_000_000.0  # 2027-01-15 ごろ
SRC = str(Path(__file__).resolve().parent.parent / "src")


def ok_runner(argv, **kw):
    return subprocess.CompletedProcess(argv, 0, stdout="2.1.330 (Claude Code)\n", stderr="")


def write_json(directory: Path, name: str, data) -> Path:
    p = directory / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def hook_settings(command="\"$HOME/.local/bin/tsuzuki\" hook session-start"):
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}


class DoctorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.fake = FakeClaude(self.root)
        self.ledger = self.root / "ledger"
        self.home = str(self.root / "home")

    def status(self, **kw):
        kw.setdefault("runner", ok_runner)
        kw.setdefault("now", lambda: NOW)
        kw.setdefault("home", self.home)
        checks = run_checks(Config(), ledger_dir=self.ledger, claude_dir=self.fake.dir, **kw)
        return {c.name: c for c in checks}

    def index(self):
        self.ledger.mkdir(mode=0o700, exist_ok=True)
        conn = make_ledger(self.ledger)
        update(conn, Config(), claude_dir=self.fake.dir, ledger_dir=self.ledger, home=self.home)
        return conn


class ReportTest(DoctorTestBase):
    def secret_fixture(self):
        cwd = "/w/%s" % MARKER
        s = sid(1)
        self.fake.write(cwd, s, [
            user("%s を含む依頼" % MARKER, t(0), s, cwd),
            assistant(t(1), s, cwd, text="%s の返事" % MARKER, tools=[("Read", {"file_path": "%s/a.md" % cwd})]),
            tool_result("%s の結果" % MARKER, t(2), s, cwd),
            title("custom", "%s の題" % MARKER, s),
            recap("%s の要約" % MARKER, t(3), s, cwd),
        ])

    def test_report_has_no_content(self):
        self.secret_fixture()
        conn = self.index()
        text = json.dumps(build_report(conn, claude_dir=self.fake.dir), ensure_ascii=False)
        self.assertNotIn(MARKER, text)
        report = json.loads(text)
        self.assertIn("user", report["keys_by_type"])
        self.assertIn("message", report["keys_by_type"]["user"])
        self.assertIn("system:away_summary", report["keys_by_type"])
        self.assertTrue(report["format_stats"])
        self.assertEqual(report["transcripts"], 1)
        self.assertEqual(sum(report["sessions"].values()), 1)
        self.assertEqual(report["unreadable_lines"], 0)
        self.assertTrue(report["generated_at"].endswith("Z"))
        for key in ("tsuzuki_version", "python", "sqlite", "trigram"):
            self.assertIn(key, report)
        self.assertEqual(set(report["format_stats"][0]), {"version", "record_type", "count"})

    def test_sample_limits_files(self):
        s1, s2 = sid(1), sid(2)
        p1 = self.fake.write("/w", s1, [{"type": "only-old", "x": 1}])
        p2 = self.fake.write("/w", s2, [{"type": "only-new", "y": 1}])
        os.utime(str(p1), (1000, 1000))
        os.utime(str(p2), (2000, 2000))
        report = build_report(None, claude_dir=self.fake.dir, sample=1)
        self.assertEqual(list(report["keys_by_type"]), ["only-new"])

    def test_without_ledger(self):
        report = build_report(None, claude_dir=self.fake.dir)
        self.assertEqual((report["sessions"], report["format_stats"], report["unreadable_lines"]), ({}, [], 0))

    def test_unreadable_lines_from_meta(self):
        conn = self.index()
        set_meta(conn, "unreadable_lines", "7")
        self.assertEqual(build_report(conn, claude_dir=self.fake.dir)["unreadable_lines"], 7)

    def test_write_report_mode(self):
        p = self.root / "new.json"
        doctor.write_report({"a": 1}, p)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        old = self.root / "old.json"
        old.write_text("x" * 100)
        os.chmod(str(old), 0o644)
        doctor.write_report({"a": 1}, old)
        self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o600)
        self.assertEqual(json.loads(old.read_text()), {"a": 1})


class HookConfiguredTest(DoctorTestBase):
    def test_hook_configured(self):
        self.assertTrue(hook_configured(write_json(self.root, "a.json", hook_settings())))
        self.assertFalse(hook_configured(write_json(self.root, "b.json",
                                                    {"hooks": {"SessionStart": [{"matcher": "*", "hooks": []}]}})))
        self.assertFalse(hook_configured(write_json(self.root, "c.json", hook_settings("other hook session-start"))))
        self.assertFalse(hook_configured(write_json(self.root, "d.json", hook_settings("tsuzuki index"))))

    def test_unreadable_settings(self):
        bad = self.root / "bad.json"
        bad.write_text("{not json")
        self.assertFalse(hook_configured(bad))
        self.assertFalse(hook_configured(self.root / "missing.json"))
        self.assertFalse(hook_configured(write_json(self.root, "e.json", {"hooks": {"SessionStart": "x"}})))
        self.assertFalse(hook_configured(write_json(self.root, "f.json", [1])))


class ChecksTest(DoctorTestBase):
    def test_environment(self):
        s = self.status()
        self.assertEqual(s["Python"].status, "ok")
        self.assertEqual(s["SQLite"].status, "ok")
        self.assertEqual(s["FTS5 trigram"].status, "ok")

    def test_python_too_old(self):
        with mock.patch.object(doctor.sys, "version_info", (3, 8, 10, "final", 0)):
            self.assertEqual(self.status()["Python"].status, "fail")

    def test_sqlite_old_and_no_trigram(self):
        with mock.patch.object(doctor.sqlite3, "sqlite_version_info", (3, 30, 0)), \
                mock.patch.object(doctor, "has_trigram", return_value=False):
            s = self.status()
        self.assertEqual(s["SQLite"].status, "warn")
        self.assertEqual((s["FTS5 trigram"].status, s["FTS5 trigram"].detail),
                         ("warn", "使えません。部分一致だけで検索します"))

    def test_records(self):
        s = self.status()
        self.assertEqual(s["Claude Code の記録"].status, "fail")
        self.assertIn(str(self.fake.dir / "projects"), s["Claude Code の記録"].detail)
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.assertEqual((self.status()["Claude Code の記録"].status, self.status()["Claude Code の記録"].detail),
                         ("ok", "記録 1件"))

    def test_claude_command(self):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return ok_runner(argv, **kw)
        c = self.status(runner=runner)["claude コマンド"]
        self.assertEqual((c.status, c.detail), ("ok", "2.1.330 (Claude Code)"))
        self.assertEqual(calls[0][0], ["claude", "--version"])
        self.assertEqual(calls[0][1]["timeout"], 10)

        def missing(argv, **kw):
            raise FileNotFoundError()

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10)

        def failing(argv, **kw):
            return subprocess.CompletedProcess(argv, 3, stdout="", stderr="")
        self.assertEqual(self.status(runner=missing)["claude コマンド"].detail, "見つかりません")
        for r in (missing, slow, failing):
            self.assertEqual(self.status(runner=r)["claude コマンド"].status, "warn")

    def test_ledger_dir(self):
        c = self.status()["台帳のディレクトリ"]
        self.assertEqual(c.status, "warn")
        self.assertIn("まだありません", c.detail)
        self.assertFalse(self.ledger.exists())
        self.ledger.mkdir(mode=0o700)
        os.chmod(str(self.ledger), 0o700)
        self.assertEqual(self.status()["台帳のディレクトリ"].status, "ok")
        os.chmod(str(self.ledger), 0o755)
        c = self.status()["台帳のディレクトリ"]
        self.assertEqual(c.status, "warn")
        self.assertIn("0o755", c.detail)

    def test_hook(self):
        self.assertEqual(self.status()["フック"].detail, "未設定（任意）")
        self.fake.dir.mkdir(parents=True, exist_ok=True)
        write_json(self.fake.dir, "settings.json", hook_settings())
        self.ledger.mkdir()
        self.assertEqual(self.status()["フック"].status, "warn")
        lineage = self.ledger / "lineage.jsonl"
        recent = {"ts": "2027-01-13T00:00:00.000Z", "session_id": sid(1)}  # NOW は 2027-01-15 08:00 UTC
        lineage.write_text(json.dumps(recent) + "\n")
        c = self.status()["フック"]
        self.assertEqual((c.status, c.detail), ("ok", "設定済み・最近の記録あり"))
        old = {"ts": "2027-01-01T00:00:00.000Z", "session_id": sid(1)}
        lineage.write_text(json.dumps(recent) + "\n" + json.dumps(old) + "\n")  # 最後の行で決まる
        self.assertEqual(self.status()["フック"].status, "warn")
        lineage.write_text("not json\n")
        self.assertEqual(self.status()["フック"].status, "warn")

    def test_format(self):
        c = self.status()["記録の形式"]
        self.assertEqual((c.status, c.detail), ("ok", "台帳がまだありません"))
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        conn = self.index()
        self.assertEqual(self.status()["記録の形式"].status, "ok")
        set_meta(conn, "format_warning", "形式が変わったかもしれません")
        conn.commit()
        c = self.status()["記録の形式"]
        self.assertEqual((c.status, c.detail), ("warn", "形式が変わったかもしれません"))
        set_meta(conn, "format_warning", "")
        set_meta(conn, "source_warning", "取り込み元が減りました")
        conn.commit()
        self.assertEqual(self.status()["記録の形式"].detail, "取り込み元が減りました")

    def test_order(self):
        self.assertEqual(list(self.status()), ["Python", "SQLite", "FTS5 trigram", "Claude Code の記録", "claude コマンド",
                                               "台帳のディレクトリ", "フック", "記録の形式",
                                               "タスクの置き場所", "タスクの決まり（README.md）",
                                               "Claude Code の書き込みの許可", "スキル（task-start・task-handoff）",
                                               "CLAUDE.md の決まり"])


class CliTest(DoctorTestBase):
    def run_doctor(self, *extra, runner=ok_runner):
        argv = ["--claude-dir", str(self.fake.dir), "--ledger-dir", str(self.ledger), "doctor"] + list(extra)
        out = io.StringIO()
        patched = functools.partial(doctor.run_checks, runner=runner)
        with mock.patch.object(doctor, "run_checks", patched), mock.patch.dict(os.environ, {"HOME": self.home}), \
                contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_lines_and_no_ledger_created(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        code, out = self.run_doctor()
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 13)
        self.assertTrue(lines[0].startswith("✔ Python："))
        self.assertIn("✔ Claude Code の記録：記録 1件", lines)
        self.assertIn("⚠ 台帳のディレクトリ：まだありません（初回の serve か index で作られます）", lines)
        self.assertFalse(self.ledger.exists())

    def test_fail_exit_code(self):
        code, out = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("✘ Claude Code の記録：見つかりません", out)

    def test_does_not_fix_permissions(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        self.index().close()
        os.chmod(str(self.ledger), 0o755)
        _, out = self.run_doctor()
        self.assertIn("⚠ 台帳のディレクトリ：権限が 0o755 です", out)
        self.assertEqual(stat.S_IMODE(self.ledger.stat().st_mode), 0o755)

    def test_report(self):
        self.fake.write("/w", sid(1), [user("%s の依頼" % MARKER, t(0), sid(1), "/w")])
        self.index().close()
        target = self.root / "r.json"
        code, out = self.run_doctor("--report", str(target))
        self.assertEqual(code, 0)
        self.assertIn("診断ファイルを書きました（会話の中身は含みません）: %s" % target, out)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(MARKER, target.read_text())
        self.assertTrue(json.loads(target.read_text())["format_stats"])

    def test_report_without_ledger(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        target = self.root / "r.json"
        self.run_doctor("--report", str(target))
        self.assertEqual(json.loads(target.read_text())["format_stats"], [])
        self.assertFalse(self.ledger.exists())

    def test_subprocess_with_fake_claude(self):
        self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        bindir = self.root / "bin"
        bindir.mkdir()
        exe = bindir / "claude"
        exe.write_text("#!/bin/sh\necho '9.9.9 (fake)'\n")
        os.chmod(str(exe), 0o755)
        env = dict(os.environ, PATH="%s:/usr/bin:/bin" % bindir, PYTHONPATH=SRC)
        proc = subprocess.run([sys.executable, "-m", "tsuzuki", "--claude-dir", str(self.fake.dir),
                               "--ledger-dir", str(self.ledger), "doctor"],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("✔ claude コマンド：9.9.9 (fake)", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class TaskChecksTest(DoctorTestBase):
    def setUp(self):
        super().setUp()
        self.home_dir = self.root / "home"
        self.home_dir.mkdir()
        self.tasks = self.home_dir / "tasks"
        self.cfg = Config(tasks_root=str(self.tasks))
        self.claude = self.root / "claude"
        self.claude.mkdir(exist_ok=True)

    def install(self, *, claude_md="~/tasks/README.md を読む", dirs=None):
        self.tasks.mkdir(exist_ok=True)
        (self.tasks / "README.md").write_text("x", encoding="utf-8")
        write_json(self.claude, "settings.json",
                   {"permissions": {"additionalDirectories": dirs if dirs is not None else [str(self.tasks)]}})
        for name in ("task-start", "task-handoff"):
            (self.claude / "skills" / name).mkdir(parents=True, exist_ok=True)
            (self.claude / "skills" / name / "SKILL.md").write_text("x", encoding="utf-8")
        (self.claude / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

    def checks(self):
        return {c.name: c.status for c in task_checks(self.cfg, claude_dir=self.claude, home=self.home)}

    def test_all_missing(self):
        checks = self.checks()
        self.assertEqual(set(checks.values()), {"warn"})
        self.assertEqual(len(checks), 5)

    def test_all_present(self):
        self.install()
        self.assertEqual(set(self.checks().values()), {"ok"})

    def test_absolute_path_in_claude_md_and_ancestor_dir(self):
        self.install(claude_md="%s/README.md" % self.tasks, dirs=["~"])
        self.assertEqual(set(self.checks().values()), {"ok"})

    def test_unrelated_dir_and_broken_files_warn(self):
        self.install(claude_md="何もなし", dirs=[str(self.root / "other")])
        checks = self.checks()
        self.assertEqual(checks["Claude Code の書き込みの許可"], "warn")
        self.assertEqual(checks["CLAUDE.md の決まり"], "warn")
        (self.claude / "settings.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.checks()["Claude Code の書き込みの許可"], "warn")

    def test_additional_directories_must_be_a_list(self):
        # 文字列や辞書は、1文字ずつ（キーごとに）見ると "/" がすべてを含んでしまう。リストでなければ ⚠
        for dirs in ("/", str(self.tasks), {"/": True}, {str(self.tasks): True}):
            with self.subTest(dirs=dirs):
                self.install(dirs=dirs)
                self.assertEqual(self.checks()["Claude Code の書き込みの許可"], "warn")
        self.install(dirs=["/"])
        self.assertEqual(self.checks()["Claude Code の書き込みの許可"], "ok")

    def test_forbidden_root_is_warn_not_crash(self):
        cfg = Config(tasks_root=self.home)
        checks = task_checks(cfg, claude_dir=self.claude, home=self.home)
        self.assertEqual({c.status for c in checks}, {"warn"})
        self.assertEqual(len(checks), 5)

    def test_report_has_only_booleans(self):
        report = build_report(None, claude_dir=self.claude, sample=0,
                              tasks=task_report(self.cfg, claude_dir=self.claude, home=self.home))
        self.assertEqual(set(report["tasks"]), {"root_exists", "root_writable", "readme", "additional_dirs",
                                                "skills", "claude_md"})
        flat = json.dumps(report["tasks"])
        self.assertNotIn("README", flat)
        self.assertNotIn(str(self.root), flat)
        self.assertNotIn("tasks", build_report(None, claude_dir=self.claude, sample=0))

    def test_report_all_present(self):
        self.install()
        r = task_report(self.cfg, claude_dir=self.claude, home=self.home)
        self.assertEqual(r, {"root_exists": True, "root_writable": True, "readme": True, "additional_dirs": True,
                             "skills": {"task-start": True, "task-handoff": True}, "claude_md": True})
