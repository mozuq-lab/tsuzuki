import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tsuzuki import cli
from tsuzuki.config import (
    Config,
    ConfigError,
    load_config,
    read_config,
    resolve_claude_dir,
    resolve_port,
    resolve_tasks_root,
)


class ConfigTest(unittest.TestCase):
    def test_resume_default_values(self):
        for value in ("vscode", "vscode-terminal", "terminal", "copy"):
            with self.subTest(value), tempfile.TemporaryDirectory() as d:
                (Path(d) / "config.json").write_text(json.dumps({"resume_default": value}), encoding="utf-8")
                self.assertEqual(load_config(Path(d)).resume_default, value)

    def test_creates_defaults_and_private_dir(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "tsuzuki"
            cfg = load_config(ledger)
            self.assertEqual(cfg.port, 8765)
            self.assertEqual(cfg.retention, "all")
            self.assertIsNone(cfg.max_age_days)
            self.assertEqual(cfg.resume_default, "vscode")
            self.assertEqual(cfg.doc_scan.max_depth, 6)
            self.assertIn("node_modules", cfg.doc_scan.exclude_dirs)
            self.assertEqual(len(cfg.doc_scan.exclude_dirs), 15)
            self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o700)
            self.assertTrue((ledger / "config.json").exists())

    def test_port_precedence(self):
        cfg = Config(port=8800)
        self.assertEqual(resolve_port(None, {}, cfg), 8800)
        self.assertEqual(resolve_port(None, {"TSUZUKI_PORT": "9000"}, cfg), 9000)
        self.assertEqual(resolve_port(9100, {"TSUZUKI_PORT": "9000"}, cfg), 9100)

    def test_invalid_retention_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text('{"retention": "forever"}', encoding="utf-8")
            with self.assertRaises(ConfigError) as cm:
                load_config(Path(d))
            self.assertIn("retention", str(cm.exception))

    def test_other_invalid_values_are_rejected(self):
        bad = {
            "resume_default": '"emacs"',
            "port": "70000",
            "max_age_days": "0",
        }
        for key, raw in bad.items():
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "config.json").write_text('{"%s": %s}' % (key, raw), encoding="utf-8")
                with self.assertRaises(ConfigError) as cm:
                    load_config(Path(d))
                self.assertIn(key, str(cm.exception))

    def test_unknown_keys_ignored_and_known_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text(
                '{"port": 9001, "nope": 1, "doc_scan": {"max_depth": 3}}', encoding="utf-8"
            )
            cfg = load_config(Path(d))
            self.assertEqual(cfg.port, 9001)
            self.assertEqual(cfg.doc_scan.max_depth, 3)

    def test_claude_dir_precedence(self):
        cfg = Config(claude_dir="~/.claude")
        self.assertEqual(resolve_claude_dir("/x", {"CLAUDE_CONFIG_DIR": "/y"}, cfg), Path("/x"))
        self.assertEqual(resolve_claude_dir(None, {"CLAUDE_CONFIG_DIR": "/y"}, cfg), Path("/y"))

    def test_cli_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), "tsuzuki 0.1.0")


class ReadConfigTest(unittest.TestCase):
    def test_creates_nothing_and_returns_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "missing"
            self.assertEqual(read_config(ledger), Config())
            self.assertFalse(ledger.exists())

    def test_reads_validates_and_keeps_mode(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d)
            ledger.chmod(0o755)
            (ledger / "config.json").write_text('{"port": 9000}', encoding="utf-8")
            self.assertEqual(read_config(ledger).port, 9000)
            self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o755)
            (ledger / "config.json").write_text('{"retention": "x"}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                read_config(ledger)
            (ledger / "config.json").write_text("[1]", encoding="utf-8")
            with self.assertRaises(ConfigError):
                read_config(ledger)


class TaskSettingsTest(unittest.TestCase):
    def write_config(self, data):
        self.ledger = Path(self._tmp.name)
        (self.ledger / "config.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_task_settings_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.tasks_root, "~/tasks")
        self.assertIsNone(cfg.ticket_url)
        self.assertEqual(cfg.new_conversation_prompt, "@{task_md} を読んで、次の一手から進めてください。")
        self.assertEqual(cfg.default_branches, ["main", "master", "develop"])

    def test_resolve_tasks_root(self):
        root = resolve_tasks_root(Config(), home="/Users/me", claude_dir=Path("/Users/me/.claude"))
        self.assertEqual(root, Path("/Users/me/tasks"))

    def test_tasks_root_rejects_top_level_and_claude_dir(self):
        for bad in ("~", "/", "/Volumes", "/Volumes/external", "~/.claude", "~/.claude/tasks"):
            with self.assertRaises(ConfigError, msg=bad):
                resolve_tasks_root(Config(tasks_root=bad), home="/Users/me", claude_dir=Path("/Users/me/.claude"))

    def test_tasks_root_rejects_symlinks_into_claude_dir(self):
        # 実体が Claude Code の設定ディレクトリの中になるリンクは断る。仮の場所だけを使う
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            claude = Path(home) / "cfg" / ".claude"
            (claude / "inside").mkdir(parents=True)
            os.symlink(str(claude / "inside"), os.path.join(home, "tasks"))  # tasks_root がリンク
            os.symlink(str(claude), os.path.join(home, "alias"))  # 設定ディレクトリの別名
            for raw in ("~/tasks", "~/alias/inside", "~/alias"):
                with self.assertRaises(ConfigError, msg=raw):
                    resolve_tasks_root(Config(tasks_root=raw), home=home, claude_dir=claude)
            os.symlink(str(claude), os.path.join(home, "dotclaude"))
            with self.assertRaises(ConfigError):  # 設定ディレクトリ側がリンク、置き場所はその実体の下
                resolve_tasks_root(Config(tasks_root="~/cfg/.claude/inside"), home=home,
                                   claude_dir=Path(home) / "dotclaude")
            ok = resolve_tasks_root(Config(tasks_root="~/work"), home=home, claude_dir=claude)
            self.assertEqual(ok, Path(home) / "work")

    def test_task_settings_are_validated(self):
        for data in ({"ticket_url": "https://example.com/issues/"}, {"new_conversation_prompt": "続けて"},
                     {"default_branches": "main"}, {"default_branches": ["main", 1]}, {"tasks_root": ""}):
            self.write_config(data)
            with self.assertRaises(ConfigError, msg=str(data)):
                read_config(self.ledger)

    def test_valid_task_settings_are_read(self):
        self.write_config({"ticket_url": "https://example.com/issues/{n}", "tasks_root": "/work/tasks"})
        cfg = read_config(self.ledger)
        self.assertEqual(cfg.ticket_url, "https://example.com/issues/{n}")
        self.assertEqual(cfg.tasks_root, "/work/tasks")


if __name__ == "__main__":
    unittest.main()
