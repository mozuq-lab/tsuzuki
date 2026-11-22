import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tsuzuki.hook import LINEAGE_FIELDS, find_parent_session, iso_utc, ppid_table, run_session_start
from tsuzuki.retention import iso_utc as ledger_iso_utc
from tsuzuki.sources.lineage import LINEAGE_FIELDS as LEDGER_FIELDS, sync_lineage
from tests.fixtures import make_ledger, sid

TABLE = {500: 400, 400: 300, 300: 200, 200: 1}


def write_session_json(sessions_dir, pid, session_id):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / ("%d.json" % pid)).write_text(json.dumps({"pid": pid, "sessionId": session_id}))


class HookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(os.path.realpath(self._tmp.name))
        self.claude = root / "claude"
        self.sessions = self.claude / "sessions"
        self.ledger = root / "ledger"
        write_session_json(self.sessions, 400, sid(2))
        write_session_json(self.sessions, 200, sid(1))

    def lines(self):
        return (self.ledger / "lineage.jsonl").read_text().splitlines()

    def start(self, stdin_text, *, env=None, table_fn=lambda: TABLE, start_pid=500, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_session_start(stdin_text, claude_dir=self.claude, ledger_dir=self.ledger,
                                     start_pid=start_pid, env=env or {}, table_fn=table_fn, **kw)
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_finds_parent_through_process_table(self):
        self.assertEqual(find_parent_session(500, sid(2), table=TABLE, sessions_dir=self.sessions), (sid(1), 200))

    def test_cycle_terminates(self):
        table = {500: 400, 400: 300, 300: 400}
        self.assertEqual(find_parent_session(500, sid(9), table=table, sessions_dir=self.sessions / "none"),
                         (None, None))

    def test_max_depth_respected(self):
        self.assertEqual(find_parent_session(500, sid(2), table=TABLE, sessions_dir=self.sessions, max_depth=2),
                         (None, None))
        self.assertEqual(find_parent_session(500, sid(2), table=TABLE, sessions_dir=self.sessions, max_depth=4),
                         (sid(1), 200))

    def test_non_string_session_id_is_skipped(self):
        (self.sessions / "400.json").write_text(json.dumps({"sessionId": 5}))
        (self.sessions / "300.json").write_text("not json")
        self.assertEqual(find_parent_session(500, sid(9), table=TABLE, sessions_dir=self.sessions), (sid(1), 200))

    def test_stops_at_pid_1(self):
        write_session_json(self.sessions, 1, sid(7))
        self.assertEqual(find_parent_session(200, sid(1), table={200: 1}, sessions_dir=self.sessions), (None, None))

    def test_appends_one_line_and_prints_nothing(self):
        self.start(json.dumps({"session_id": sid(2), "cwd": "/w", "source": "startup"}),
                   env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, now=lambda: 1795243515.642)
        rec = json.loads(self.lines()[0])
        self.assertEqual(tuple(rec), LEDGER_FIELDS)
        self.assertEqual((rec["session_id"], rec["parent_id"], rec["parent_pid"], rec["source"], rec["entrypoint"],
                          rec["cwd"], rec["ts"]),
                         (sid(2), sid(1), 200, "startup", "sdk-cli", "/w", "2026-11-21T06:45:15.642Z"))
        self.assertEqual(len(self.lines()), 1)

    def test_lineage_file_is_private(self):
        self.start(json.dumps({"session_id": sid(2)}))
        mode = stat.S_IMODE(os.stat(self.ledger / "lineage.jsonl").st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_session_id_writes_nothing(self):
        for text in ("not json", "{}", '{"session_id": ""}', '{"session_id": 3}', "[1]"):
            self.start(text)
        self.assertFalse((self.ledger / "lineage.jsonl").exists())

    def test_never_fails(self):
        self.start("not json", table_fn=lambda: {}, start_pid=1)

        def boom():
            raise RuntimeError("x")
        self.start('{"session_id": "x"}', table_fn=boom, start_pid=1)

    def test_lookup_failure_still_appends_with_null_parent(self):
        def boom():
            raise RuntimeError("x")
        self.start(json.dumps({"session_id": sid(2)}), table_fn=boom)
        rec = json.loads(self.lines()[0])
        self.assertEqual((rec["session_id"], rec["parent_id"], rec["parent_pid"]), (sid(2), None, None))

    def test_unwritable_ledger_is_silent(self):
        blocker = self.ledger.parent / "file"
        blocker.write_text("x")
        out = run_session_start(json.dumps({"session_id": sid(2)}), claude_dir=self.claude,
                                ledger_dir=blocker / "sub", start_pid=500, env={}, table_fn=lambda: TABLE)
        self.assertEqual(out, 0)

    def test_round_trip_with_sync_lineage(self):
        self.start(json.dumps({"session_id": sid(2), "cwd": "/w", "source": "startup"}))
        conn = make_ledger(self.ledger / "db")
        self.addCleanup(conn.close)
        self.assertEqual(sync_lineage(conn, self.ledger / "lineage.jsonl"), 1)
        row = conn.execute("SELECT session_id, parent_id, source, cwd FROM lineage").fetchone()
        self.assertEqual(tuple(row), (sid(2), sid(1), "startup", "/w"))

    def test_field_tuple_and_formatter_match_the_indexer_side(self):
        self.assertEqual(LINEAGE_FIELDS, LEDGER_FIELDS)
        for ts in (0, 1795243515.642, 1795243515.0, 1795243515.9996, 1.5):
            self.assertEqual(iso_utc(ts), ledger_iso_utc(ts))

    def test_ppid_table_parses_ps_output(self):
        class R:
            stdout = "  1 0\n 20 1\nbad line\n300 20 extra\n x y\n 40 20\n"
        calls = []

        def runner(*a, **k):
            calls.append((a, k))
            return R()
        self.assertEqual(ppid_table(runner), {1: 0, 20: 1, 40: 20})
        self.assertEqual(calls[0][0][0], ["ps", "-axo", "pid=,ppid="])
        self.assertEqual(len(calls), 1)

    def test_ppid_table_failure_is_empty(self):
        def runner(*a, **k):
            raise OSError("no ps")
        self.assertEqual(ppid_table(runner), {})

    def test_real_ppid_table_has_own_process(self):
        table = ppid_table()
        self.assertEqual(table.get(os.getpid()), os.getppid())


class HookCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(os.path.realpath(self._tmp.name))
        self.claude = root / "claude"
        self.claude.mkdir()
        self.ledger = root / "ledger"
        self.ledger.mkdir()

    def run_hook(self, stdin, *, env_extra=None):
        env = {k: v for k, v in os.environ.items() if k not in ("TSUZUKI_HOME", "CLAUDE_CONFIG_DIR")}
        env["PYTHONPATH"] = "src"
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-m", "tsuzuki", "--claude-dir", str(self.claude),
                               "--ledger-dir", str(self.ledger), "hook", "session-start"],
                              input=stdin, capture_output=True, env=env)

    def test_cli_is_silent(self):
        p = self.run_hook(b'{"session_id": "' + sid(2).encode() + b'"}')
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        rec = json.loads((self.ledger / "lineage.jsonl").read_text().splitlines()[0])
        self.assertEqual(rec["session_id"], sid(2))

    def test_broken_config_does_not_matter(self):
        (self.ledger / "config.json").write_text("{ broken")
        p = self.run_hook(b'{"session_id": "' + sid(2).encode() + b'"}')
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertTrue((self.ledger / "lineage.jsonl").exists())

    def test_garbage_stdin_exits_zero(self):
        p = self.run_hook(b"\xff\xfe not json")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertFalse((self.ledger / "lineage.jsonl").exists())

    def test_env_resolution_without_flags(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TSUZUKI_HOME", "CLAUDE_CONFIG_DIR")}
        env.update(PYTHONPATH="src", TSUZUKI_HOME=str(self.ledger), CLAUDE_CONFIG_DIR=str(self.claude))
        p = subprocess.run([sys.executable, "-m", "tsuzuki", "hook", "session-start"],
                           input=b'{"session_id": "' + sid(3).encode() + b'"}', capture_output=True, env=env)
        self.assertEqual((p.returncode, p.stdout), (0, b""))
        self.assertTrue((self.ledger / "lineage.jsonl").exists())


class LazyImportTest(unittest.TestCase):
    def check(self, module, forbidden):
        code = ("import sys, %s\n"
                "print(sorted(m for m in %r if m in sys.modules))" % (module, forbidden))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": "src"})
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "[]"), p.stderr)

    def test_cli_import_is_light(self):
        self.check("tsuzuki.cli", ["tsuzuki.indexer", "tsuzuki.search", "tsuzuki.server", "tsuzuki.views",
                                  "tsuzuki.store", "tsuzuki.retention", "sqlite3"])

    def test_hook_import_is_light(self):
        self.check("tsuzuki.hook", ["sqlite3", "tsuzuki.store", "tsuzuki.retention", "tsuzuki.sources.lineage"])

    def test_hook_run_leaves_sqlite_out(self):
        with tempfile.TemporaryDirectory() as d:
            code = ("import sys\n"
                    "from tsuzuki.cli import main\n"
                    "main(['--claude-dir', %r, '--ledger-dir', %r, 'hook', 'session-start'])\n"
                    "print('sqlite3' in sys.modules)" % (d, d))
            p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               input='{"session_id": "x"}', env={**os.environ, "PYTHONPATH": "src"})
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "False"), p.stderr)


if __name__ == "__main__":
    unittest.main()
