import json
import os
import tempfile
import unittest
from pathlib import Path

from tsuzuki.sources.history import ms_to_iso, sync_history
from tsuzuki.sources.lineage import sync_lineage
from tsuzuki.sources.live import live_sessions, pid_alive
from tests.fixtures import FakeClaude, count, make_ledger, sid


def row(text, session, ts=1795243515642, project="/w"):
    return {"display": text, "pastedContents": {"1": {"content": "SECRET"}}, "timestamp": ts,
            "project": project, "sessionId": session}


def texts(conn):
    return [x[0] for x in conn.execute("SELECT text FROM history_prompts ORDER BY id")]


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake = FakeClaude(self.tmp)
        self.conn = make_ledger(self.tmp)
        self.addCleanup(self.conn.close)

    def sh(self, h, **kw):
        kw.setdefault("tombstoned", set())
        kw.setdefault("place_of", lambda p: None)
        return sync_history(self.conn, h, **kw)

    def lineage_file(self, lines):
        p = self.tmp / "lineage.jsonl"
        p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
        return p

    def test_ms_to_iso(self):
        self.assertEqual(ms_to_iso(1795243515642), "2026-11-21T06:45:15.642Z")

    def test_history_incremental_and_reload(self):
        h = self.fake.history([row("a", sid(1)), row("b", sid(2))])
        self.assertEqual(self.sh(h), 2)
        with open(h, "a") as f:
            f.write(json.dumps(row("c", sid(3))) + "\n")
        self.assertEqual(self.sh(h), 1)
        self.fake.history([row("only", sid(4))])
        self.sh(h)
        self.assertEqual(texts(self.conn), ["only"])

    def test_history_skips_tombstoned(self):
        h = self.fake.history([row("消したい", sid(1)), row("残す", sid(2))])
        self.sh(h, tombstoned={sid(1)})
        self.assertEqual(texts(self.conn), ["残す"])

    def test_history_partial_last_line_not_consumed(self):
        h = self.fake.history([row("a", sid(1))])
        with open(h, "a") as f:
            f.write(json.dumps(row("b", sid(2)))[:20])
        self.assertEqual(self.sh(h), 1)
        with open(h, "a") as f:
            f.write(json.dumps(row("b", sid(2)))[20:] + "\n")
        self.assertEqual(self.sh(h), 1)
        self.assertEqual(texts(self.conn), ["a", "b"])

    def test_history_fields_and_place_of(self):
        h = self.fake.history([row("a", sid(1), project="/proj")])
        seen = []
        self.sh(h, place_of=lambda p: seen.append(p) or 7)
        self.assertEqual(seen, ["/proj"])
        r = self.conn.execute("SELECT session_id, project, place_id, ts, text FROM history_prompts").fetchone()
        self.assertEqual(tuple(r), (sid(1), "/proj", 7, "2026-11-21T06:45:15.642Z", "a"))

    def test_history_skips_bad_lines(self):
        h = self.fake.history([row("ok", sid(1))])
        with open(h, "a") as f:
            f.write("not json\n[1]\n" + json.dumps({"display": "", "timestamp": 1}) + "\n"
                    + json.dumps({"display": "x"}) + "\n")
        self.assertEqual(self.sh(h), 1)

    def test_history_poisoned_timestamps_do_not_wedge(self):
        h = self.fake.history([row("a", sid(1))])
        with open(h, "a") as f:
            for v in ("NaN", "Infinity", "1e30"):
                f.write('{"display": "bad", "timestamp": %s, "project": "/w", "sessionId": "x"}\n' % v)
            f.write(json.dumps(row("b", sid(2))) + "\n")
        self.assertEqual(self.sh(h), 2)
        self.assertEqual(self.sh(h), 0)
        self.assertEqual(texts(self.conn), ["a", "b"])

    def test_lineage_bad_field_types(self):
        p = self.lineage_file([
            json.dumps({"session_id": sid(5), "parent_id": [1], "cwd": {"a": 1}, "ts": "t", "source": "s"}),
            json.dumps({"session_id": sid(6), "parent_id": sid(5)}),
        ])
        self.assertEqual(sync_lineage(self.conn, p), 2)
        r = self.conn.execute("SELECT parent_id, cwd, ts FROM lineage WHERE session_id = ?", (sid(5),)).fetchone()
        self.assertEqual(tuple(r), (None, None, "t"))
        self.assertEqual(count(self.conn, "lineage"), 2)

    def test_missing_file(self):
        self.assertEqual(self.sh(self.tmp / "nope.jsonl"), 0)
        self.assertEqual(sync_lineage(self.conn, self.tmp / "nope.jsonl"), 0)
        self.assertEqual(count(self.conn, "sources"), 0)

    def test_lineage(self):
        p = self.lineage_file([
            json.dumps({"ts": "t1", "session_id": sid(5), "parent_id": sid(1), "parent_pid": 9,
                        "source": "resume", "cwd": "/w", "entrypoint": "cli"}),
            "{broken",
            json.dumps({"ts": "t2", "session_id": sid(6), "parent_id": None, "source": "startup", "cwd": "/w"}),
        ])
        self.assertEqual(sync_lineage(self.conn, p), 2)
        self.assertEqual(self.conn.execute("SELECT parent_id FROM lineage WHERE session_id = ?",
                                           (sid(5),)).fetchone()[0], sid(1))

    def test_lineage_overwrite_and_shrink_reload(self):
        a = json.dumps({"ts": "t1", "session_id": sid(5), "parent_id": sid(1), "source": "resume", "cwd": "/w"})
        b = json.dumps({"ts": "t2", "session_id": sid(5), "parent_id": sid(2), "source": "resume", "cwd": "/w"})
        p = self.lineage_file([a, b])
        self.assertEqual(sync_lineage(self.conn, p), 2)
        self.assertEqual(count(self.conn, "lineage"), 1)
        short = json.dumps({"session_id": sid(9)})
        p = self.lineage_file([short])
        self.assertEqual(sync_lineage(self.conn, p), 1)
        self.assertEqual([x[0] for x in self.conn.execute("SELECT session_id FROM lineage")], [sid(9)])

    def test_live_sessions(self):
        self.fake.live(111, sid(1), "/w")
        self.fake.live(222, sid(2), "/w")
        live = live_sessions(self.fake.dir, alive=lambda pid: pid == 111)
        self.assertEqual(list(live), [sid(1)])
        self.assertEqual((live[sid(1)].pid, live[sid(1)].cwd, live[sid(1)].status), (111, "/w", "idle"))

    def test_live_sessions_missing_dir_and_bad_files(self):
        self.assertEqual(live_sessions(self.fake.dir), {})
        self.fake.live(111, sid(1), "/w")
        (self.fake.dir / "sessions" / "bad.json").write_text("{", encoding="utf-8")
        (self.fake.dir / "sessions" / "nopid.json").write_text('{"sessionId": "x"}', encoding="utf-8")
        self.assertEqual(list(live_sessions(self.fake.dir, alive=lambda p: True)), [sid(1)])

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(0))


if __name__ == "__main__":
    unittest.main()
