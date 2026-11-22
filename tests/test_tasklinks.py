"""セッションをタスクに結び付ける規則。"""
import os
import tempfile
import unittest
from pathlib import Path

from tsuzuki.config import Config
from tsuzuki.indexer import update
from tsuzuki.store import get_meta
from tsuzuki.tasklinks import RELINK_KEY, link_tasks, set_override
from tsuzuki.tasks import within
from tests.fixtures import FakeClaude, allow_system_tmp, make_ledger, t, user, write_file
from tests.fixtures import sid as session_id

T = "/Users/me/tasks"
A = T + "/2026-11-20-a"
B = T + "/2026-11-21-b"
TS = "2026-11-21T01:00:00Z"
NOW = "2026-11-22T00:00:00Z"


class LinkTasksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = make_ledger(Path(self._tmp.name))
        self.addCleanup(self.conn.close)
        with self.conn:
            for name, path, created in (("2026-11-20-a", A, "2026-11-20"), ("2026-11-21-b", B, "2026-11-21")):
                self.conn.execute("INSERT INTO tasks(name, path, title, stage, created) VALUES (?, ?, ?, '調査', ?)",
                                  (name, path, name, created))

    # 台帳に行を直接入れる助け

    def add_session(self, sid, *, kind="interactive", cwd, place=None, branch="main",
                    started="2026-11-21T00:00:00Z", ended=None, custom_title=None, agent_name=None):
        """place を省くと、cwd がタスクの置き場所の中なら置き場所、ほかは cwd を場所のパスにする。"""
        if place is None:
            place = T if within(cwd, T) else cwd
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO places(path, display) VALUES (?, ?)", (place, place))
            pid = self.conn.execute("SELECT id FROM places WHERE path = ?", (place,)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO sessions(id, transcript_path, kind, cwd, place_id, branch, started_at, ended_at,"
                " custom_title, agent_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "/x/%s.jsonl" % sid, kind, cwd, pid, branch, started, ended, custom_title, agent_name))

    def add_link(self, sid, path, rel, *, prompt_index=None, ts):
        with self.conn:
            self.conn.execute("INSERT INTO doc_links(session_id, path, rel, ts, prompt_index) VALUES (?, ?, ?, ?, ?)",
                              (sid, path, rel, ts, prompt_index))

    def add_relation(self, src, dst, type, confidence="confirmed"):
        with self.conn:
            self.conn.execute("INSERT INTO relations(src, dst, type, confidence, evidence)"
                              " VALUES (?, ?, ?, ?, '記録')", (src, dst, type, confidence))

    def add_derived(self, sid, source, via):
        with self.conn:
            self.conn.execute("INSERT INTO derived_from(session_id, source_id, via) VALUES (?, ?, ?)",
                              (sid, source, via))

    def set_task(self, name, *, repo=None, ticket=None, created=None):
        """setUp で作ったタスクの行の repo と ticket を書き換える（created は渡したときだけ）。"""
        with self.conn:
            self.conn.execute("UPDATE tasks SET repo = ?, ticket = ?, created = COALESCE(?, created) WHERE name = ?",
                              (repo, ticket, created, name))

    def links(self):
        return {(r[0], r[1]): (r[2], r[3])
                for r in self.conn.execute("SELECT task, session_id, rule, confidence FROM task_sessions")}

    def repo_of(self, name):
        return self.conn.execute("SELECT repo_resolved FROM tasks WHERE name = ?", (name,)).fetchone()[0]


    def test_direct_rules(self):
        self.add_session("s1", cwd=A + "/sub")
        self.add_session("s2", cwd="/r")
        self.add_session("s3", cwd="/r")
        self.add_session("s4", cwd="/r")
        self.add_session("s5", cwd="/r")
        self.add_session("s6", cwd="/r")
        self.add_link("s2", A + "/data/q.sql", "created", ts="2026-11-21T01:00:00Z")
        self.add_link("s3", A + "/task.md", "named", prompt_index=2, ts="2026-11-21T01:00:00Z")
        self.add_link("s4", A + "/task.md", "named", prompt_index=4, ts="2026-11-21T01:00:00Z")
        self.add_link("s5", A + "/research-x.md", "named", prompt_index=1, ts="2026-11-21T01:00:00Z")
        self.add_link("s6", A + "/task.md", "read", ts="2026-11-21T01:00:00Z")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "s1"): ("cwd", "confirmed"),
                                        ("2026-11-20-a", "s2"): ("wrote", "confirmed"),
                                        ("2026-11-20-a", "s3"): ("named_task_md", "confirmed"),
                                        ("2026-11-20-a", "s5"): ("named_file", "strong"),
                                        ("2026-11-20-a", "s6"): ("read_task_md", "strong")})

    def test_reading_other_docs_or_shared_does_not_link(self):
        self.add_session("s1", cwd="/r")
        self.add_link("s1", A + "/research-x.md", "read", ts="2026-11-21T01:00:00Z")
        self.add_link("s1", T + "/shared/notes-a.md", "edited", ts="2026-11-21T01:00:00Z")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {})

    def test_one_task_per_session(self):
        self.add_session("s1", cwd="/r")
        self.add_session("s2", cwd=A)
        self.add_link("s1", A + "/x.md", "created", ts="2026-11-21T01:00:00Z")
        self.add_link("s1", B + "/y.md", "created", ts="2026-11-21T02:00:00Z")  # 後で触れた B
        self.add_link("s2", B + "/y.md", "created", ts="2026-11-21T03:00:00Z")  # cwd の A が勝つ
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-21-b", "s1"): ("wrote", "confirmed"),
                                        ("2026-11-20-a", "s2"): ("cwd", "confirmed")})

    def test_inheritance(self):
        self.add_session("s0", cwd=A)  # s0 → s1 → s2 と分岐
        self.add_session("s1", cwd="/r")
        self.add_session("s2", cwd="/r")
        self.add_relation("s0", "s1", "fork")
        self.add_relation("s1", "s2", "fork")
        self.add_session("p", cwd="/r")  # 子 c が B に書く
        self.add_session("c", kind="headless", cwd="/r")
        self.add_relation("p", "c", "child")
        self.add_link("c", B + "/research-z.md", "created", ts="2026-11-21T01:00:00Z")
        self.add_session("k", kind="headless", cwd="/r")  # 親 s0 の子
        self.add_relation("s0", "k", "child")
        self.add_session("after", cwd="/r")
        self.add_derived("after", "s0", "clear")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual(got[("2026-11-20-a", "s2")], ("from_source", "strong"))
        self.assertEqual(got[("2026-11-21-b", "p")], ("from_child", "strong"))
        self.assertEqual(got[("2026-11-20-a", "k")], ("from_source", "strong"))
        self.assertEqual(got[("2026-11-20-a", "after")], ("from_clear", "weak"))

    def test_weak_links_are_not_inherited(self):
        self.set_task("2026-11-20-a", ticket="#123", repo="/r")
        self.add_session("w", cwd="/r", branch="fix/123-timeout")
        self.add_session("next", cwd="/r")
        self.add_derived("next", "w", "clear")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links()[("2026-11-20-a", "w")], ("ticket", "weak"))
        self.assertNotIn(("2026-11-20-a", "next"), self.links())

    def test_same_branch_and_ticket(self):
        # A：repo なし、ticket "#123"。s_conf は /r の fix/x で A に確定（cwd ではなく資料を書いた）
        self.set_task("2026-11-20-a", ticket="#123")
        self.add_session("s_conf", cwd="/r", branch="fix/x")
        self.add_link("s_conf", A + "/a.md", "created", ts="2026-11-21T01:00:00Z")
        self.add_session("same", cwd="/r", branch="fix/x")
        self.add_session("main", cwd="/r", branch="main")
        self.add_session("other", cwd="/q", branch="fix/x")
        self.add_session("tk", cwd="/r", branch="fix/123-timeout")
        self.add_session("tk2", cwd="/r", branch="fix/1234")
        self.add_session("named", cwd="/r", branch="main", custom_title="対応 #123")
        self.add_session("auto", cwd="/r", branch="main", custom_title="#123 の件 (Branch)",
                         agent_name="#123 の件 (Branch)")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual(got[("2026-11-20-a", "same")], ("same_branch", "weak"))
        self.assertEqual(got[("2026-11-20-a", "tk")], ("ticket", "weak"))
        self.assertEqual(got[("2026-11-20-a", "named")], ("ticket", "weak"))
        for sid in ("main", "other", "tk2", "auto"):
            self.assertNotIn(("2026-11-20-a", sid), got)
        self.assertEqual(self.conn.execute("SELECT repo_resolved FROM tasks WHERE name = '2026-11-20-a'").fetchone()[0],
                         "/r")

    def test_repo_ignores_weak_sessions(self):
        # A の確定のセッションは /r1 に2つ、推定（弱）のセッションは /r2 に3つ → repo_resolved は /r1。
        # 弱い結び付きを作るため、初めは task.md の repo を /r2 にしておき（チケット番号の規則は同じリポジトリの
        # ときだけ当たる）、後で task.md から repo を消す
        self.set_task("2026-11-20-a", ticket="#123", repo="/r2")
        for sid in ("c1", "c2"):
            self.add_session(sid, cwd="/r1")
            self.add_link(sid, A + "/a.md", "created", ts="2026-11-21T01:00:00Z")
        for sid in ("w1", "w2", "w3"):
            self.add_session(sid, cwd="/r2", branch="fix/123-timeout")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual([got.get(("2026-11-20-a", sid)) for sid in ("w1", "w2", "w3")], [("ticket", "weak")] * 3)
        self.set_task("2026-11-20-a", ticket="#123")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.repo_of("2026-11-20-a"), "/r1")

    def test_overrides(self):
        self.add_session("s1", cwd=A)
        set_override(self.conn, "2026-11-20-a", "s1", "remove", now_iso="2026-11-22T00:00:00Z")
        set_override(self.conn, "2026-11-21-b", "s1", "add", now_iso="2026-11-22T00:00:00Z")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-21-b", "s1"): ("manual", "confirmed")})

    def test_manual_add_keeps_auto_link(self):
        self.add_session("s1", cwd=A)
        set_override(self.conn, "2026-11-21-b", "s1", "add", now_iso="2026-11-22T00:00:00Z")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(set(self.links()), {("2026-11-20-a", "s1"), ("2026-11-21-b", "s1")})

    def test_empty_sessions_and_idempotence(self):
        self.add_session("e", kind="empty", cwd=A)
        self.assertEqual(link_tasks(self.conn, tasks_root=T, default_branches=["main"]), 0)
        self.add_session("s1", cwd=A)
        first = (link_tasks(self.conn, tasks_root=T, default_branches=["main"]), self.links())
        self.assertEqual(first, (link_tasks(self.conn, tasks_root=T, default_branches=["main"]), self.links()))

    # 細かい所

    def test_any_write_and_the_third_prompt_count(self):
        self.add_session("s1", cwd="/r")
        self.add_session("s2", cwd="/r")
        self.add_session("s3", cwd="/r")
        self.add_link("s1", A + "/data/run.sh", "task_file", ts=TS)  # 資料でないファイルへの書き込み
        self.add_link("s2", B + "/notes-x.md", "edited", ts=TS)
        self.add_link("s3", B + "/task.md", "named", prompt_index=3, ts=TS)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "s1"): ("wrote", "confirmed"),
                                        ("2026-11-21-b", "s2"): ("wrote", "confirmed"),
                                        ("2026-11-21-b", "s3"): ("named_task_md", "confirmed")})

    def test_source_chain_follows_continued_and_stops_on_cycles(self):
        self.add_session("s0", cwd=A)
        self.add_session("s1", cwd="/r")
        self.add_relation("s0", "s1", "continued")
        for sid in ("x", "y", "z"):  # 分岐が輪になっている（どれも直接の根拠を持たない）
            self.add_session(sid, cwd="/r")
        self.add_relation("x", "y", "fork")
        self.add_relation("y", "z", "fork")
        self.add_relation("z", "x", "fork")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "s0"): ("cwd", "confirmed"),
                                        ("2026-11-20-a", "s1"): ("from_source", "strong")})

    def test_parent_takes_strongest_rule_then_newest_child(self):
        # p の子：新しい c1 は A の task.md を読んだ（推定（強））、古い c2 は B に書いた（確定）→ 規則の強い B
        self.add_session("p", cwd="/r")
        self.add_session("c1", kind="headless", cwd="/r", started="2026-11-21T02:00:00Z")
        self.add_session("c2", kind="headless", cwd="/r", started="2026-11-21T01:00:00Z")
        self.add_link("c1", A + "/task.md", "read", ts=TS)
        self.add_link("c2", B + "/x.md", "created", ts=TS)
        # q の子：3つとも書いた。いちばん新しい d2 の B
        self.add_session("q", cwd="/r")
        self.add_session("d1", kind="headless", cwd="/r", started="2026-11-21T01:00:00Z")
        self.add_session("d2", kind="headless", cwd="/r", started="2026-11-21T03:00:00Z")
        self.add_session("d3", kind="headless", cwd="/r", started="2026-11-21T02:00:00Z")
        self.add_link("d1", A + "/y.md", "created", ts=TS)
        self.add_link("d2", B + "/z.md", "created", ts=TS)
        self.add_link("d3", A + "/w.md", "created", ts=TS)
        for parent, child in (("p", "c1"), ("p", "c2"), ("q", "d1"), ("q", "d2"), ("q", "d3")):
            self.add_relation(parent, child, "child")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual(got[("2026-11-21-b", "p")], ("from_child", "strong"))
        self.assertEqual(got[("2026-11-21-b", "q")], ("from_child", "strong"))

    def test_removed_pairs_are_not_linked_by_any_rule(self):
        self.set_task("2026-11-20-a", ticket="#123", repo="/r")
        self.add_session("s0", cwd=A)
        self.add_session("s1", cwd="/r")
        self.add_relation("s0", "s1", "fork")
        self.add_session("w", cwd="/r", branch="fix/123-timeout")
        for sid in ("s1", "w"):
            set_override(self.conn, "2026-11-20-a", sid, "remove", now_iso=NOW)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "s0"): ("cwd", "confirmed")})

    def test_weak_rules_choose_newest_task_created_before_session(self):
        self.set_task("2026-11-20-a", ticket="#123", repo="/r")
        self.set_task("2026-11-21-b", ticket="#123", repo="/r")
        self.add_session("early", cwd="/r", branch="fix/123", started="2026-11-20T09:00:00Z")  # B を作る前
        self.add_session("late", cwd="/r", branch="fix/123", started="2026-11-21T09:00:00Z")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "early"): ("ticket", "weak"),
                                        ("2026-11-21-b", "late"): ("ticket", "weak")})

    def test_repo_from_sessions(self):
        # (タスク, 作業ディレクトリ, 最後の動き)。どれもタスクのフォルダーで始めたか、そこに書いて確定で結び付く
        sessions = [
            # A：セッションの数が最も多い /r1。タスクの置き場所の中の場所（3つ）と、下で足す /clear の後の会話
            # （推定（弱）で /r3 に3つ）は数えない
            ("2026-11-20-a", A, None), ("2026-11-20-a", A, None), ("2026-11-20-a", A + "/sub", None),
            ("2026-11-20-a", "/r1", "2026-11-21T01:00:00Z"), ("2026-11-20-a", "/r1", "2026-11-21T02:00:00Z"),
            ("2026-11-20-a", "/r2", "2026-11-21T09:00:00Z"),
            # B：どの場所も2つなので、最後の動きが新しい /q2
            ("2026-11-21-b", "/q1", "2026-11-21T04:00:00Z"), ("2026-11-21-b", "/q1", "2026-11-21T05:00:00Z"),
            ("2026-11-21-b", "/q2", "2026-11-21T01:00:00Z"), ("2026-11-21-b", "/q2", "2026-11-21T07:00:00Z"),
            ("2026-11-21-b", "/q3", "2026-11-21T06:00:00Z"), ("2026-11-21-b", "/q3", "2026-11-21T06:30:00Z"),
        ]
        for i, (task, cwd, ended) in enumerate(sessions):
            self.add_session("s%d" % i, cwd=cwd, ended=ended)
            self.add_link("s%d" % i, "%s/%s/x.md" % (T, task), "created", ts=TS)
        for sid in ("w1", "w2", "w3"):
            self.add_session(sid, cwd="/r3")
            self.add_derived(sid, "s3", "clear")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links()[("2026-11-20-a", "w1")], ("from_clear", "weak"))
        self.assertEqual((self.repo_of("2026-11-20-a"), self.repo_of("2026-11-21-b")), ("/r1", "/q2"))

    def test_repo_from_task_md_and_no_stale_value(self):
        self.add_session("s1", cwd="/r1")
        self.add_link("s1", A + "/a.md", "created", ts=TS)
        self.set_task("2026-11-20-a", repo="/Users/me/dev/web")  # task.md の repo がセッションの場所より先
        with self.conn:  # 前の計算の値は残さない
            self.conn.execute("UPDATE tasks SET repo_resolved = '/stale' WHERE name = '2026-11-21-b'")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual((self.repo_of("2026-11-20-a"), self.repo_of("2026-11-21-b")), ("/Users/me/dev/web", None))

    def test_parents_pass_links_down_one_level_only(self):
        # g → m → n と子が続く。m は親 g から引き継ぐが、孫の n には渡らない（親子は1段だけ見る）
        self.add_session("g", cwd=A)
        self.add_session("m", kind="headless", cwd="/r")
        self.add_session("n", kind="headless", cwd="/r")
        self.add_relation("g", "m", "child")
        self.add_relation("m", "n", "child")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "g"): ("cwd", "confirmed"),
                                        ("2026-11-20-a", "m"): ("from_source", "strong")})

    # つながり自体の確かさ。引き継いだ確かさは、表の確かさ、相手の確かさ、つながりの確かさのうち最も低いもの

    def test_weak_child_relation_gives_weak_links(self):
        # 前後5分の推定（弱）の親子。親 p（A のフォルダーで開始）から子 c へも、B に書いた子 c2 から親 q へも推定（弱）。
        # 推定（弱）はリポジトリに数えず、/clear の後の y にも引き継がない
        self.add_session("p", cwd=A)
        self.add_session("c", kind="headless", cwd="/r")
        self.add_relation("p", "c", "child", confidence="weak")
        self.add_session("q", cwd="/r")
        self.add_session("c2", kind="headless", cwd="/q")
        self.add_relation("q", "c2", "child", confidence="weak")
        self.add_link("c2", B + "/x.md", "created", ts=TS)
        self.add_session("y", cwd="/r")
        self.add_derived("y", "q", "clear")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "p"): ("cwd", "confirmed"),
                                        ("2026-11-20-a", "c"): ("from_source", "weak"),
                                        ("2026-11-21-b", "c2"): ("wrote", "confirmed"),
                                        ("2026-11-21-b", "q"): ("from_child", "weak")})
        self.assertEqual((self.repo_of("2026-11-20-a"), self.repo_of("2026-11-21-b")), (None, "/q"))

    def test_strong_child_relation_keeps_from_source_strong(self):
        self.add_session("p", cwd=A)
        self.add_session("k", kind="headless", cwd="/r")
        self.add_relation("p", "k", "child", confidence="strong")  # 記録に ID
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links()[("2026-11-20-a", "k")], ("from_source", "strong"))

    def test_relation_confidence_folds_along_the_source_chain(self):
        # 分岐と続きの会話のつながりはいまは確定だけだが、同じように畳む（途中に推定（弱）があれば推定（弱））
        self.add_session("s0", cwd=A)
        self.add_session("s1", cwd="/r")
        self.add_session("s2", cwd="/r")
        self.add_relation("s0", "s1", "fork")
        self.add_relation("s1", "s2", "continued", confidence="weak")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual((got[("2026-11-20-a", "s1")], got[("2026-11-20-a", "s2")]),
                         (("from_source", "strong"), ("from_source", "weak")))

    def test_strong_inheritance_beats_weak_inheritance(self):
        # c は p（A のフォルダーで開始）の推定（弱）の子で、B に書いた d の確定の親。推定（弱）の「元の会話から」に
        # 1つの枠を取らせず、推定（強）の「子の会話から」を選ぶ
        self.add_session("p", cwd=A)
        self.add_session("c", kind="headless", cwd="/r")
        self.add_session("d", kind="headless", cwd="/r")
        self.add_relation("p", "c", "child", confidence="weak")
        self.add_relation("c", "d", "child")
        self.add_link("d", B + "/x.md", "created", ts=TS)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual(got[("2026-11-21-b", "c")], ("from_child", "strong"))
        self.assertNotIn(("2026-11-20-a", "c"), got)

    def test_from_child_prefers_the_stronger_result(self):
        # q の子：c1 は A に書いた（確定）が親子は推定（弱）、c2 は B の task.md を読んだ（推定（強））が親子は確定。
        # 規則は c1 が強いが、引き継いだ確かさが高い（推定（強））c2 の B を選ぶ
        self.add_session("q", cwd="/r")
        self.add_session("c1", kind="headless", cwd="/r", started="2026-11-21T02:00:00Z")
        self.add_session("c2", kind="headless", cwd="/r", started="2026-11-21T01:00:00Z")
        self.add_relation("q", "c1", "child", confidence="weak")
        self.add_relation("q", "c2", "child")
        self.add_link("c1", A + "/x.md", "created", ts=TS)
        self.add_link("c2", B + "/task.md", "read", ts=TS)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual(got[("2026-11-21-b", "q")], ("from_child", "strong"))
        self.assertNotIn(("2026-11-20-a", "q"), got)

    def test_fork_source_before_parent(self):
        # s は A の s0 の分岐で、B の p（後で始めた）の子でもある。どちらも推定（強）の「元の会話から」なので、
        # 分岐の元を先にする順で A
        self.add_session("s0", cwd=A)
        self.add_session("p", cwd=B, started="2026-11-21T05:00:00Z")
        self.add_session("s", cwd="/r", started="2026-11-21T06:00:00Z")
        self.add_relation("s0", "s", "fork")
        self.add_relation("p", "s", "child")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links()[("2026-11-20-a", "s")], ("from_source", "strong"))

    def test_weak_rules_prefer_same_branch_over_ticket(self):
        # A は /r の fix/123-x に確定のセッションを持つ。B は /r で ticket #123、A より1日後に作った。後の fix/123-x の
        # セッションは、作った日の新しい B のチケット番号ではなく、表の上の規則（同じブランチ）の A に結び付く
        self.set_task("2026-11-20-a", repo="/r")
        self.set_task("2026-11-21-b", repo="/r", ticket="#123")
        self.add_session("a1", cwd="/r", branch="fix/123-x")
        self.add_link("a1", A + "/a.md", "created", ts=TS)
        self.add_session("s", cwd="/r", branch="fix/123-x", started="2026-11-21T09:00:00Z")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "a1"): ("wrote", "confirmed"),
                                        ("2026-11-20-a", "s"): ("same_branch", "weak")})

    # 手で足した組は、表の1行目の直接の根拠（確定）。リポジトリ、同じブランチ、引き継ぎの元に数える

    def test_manual_add_counts_for_repo(self):
        self.add_session("m", cwd="/r")
        set_override(self.conn, "2026-11-20-a", "m", "add", now_iso=NOW)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.repo_of("2026-11-20-a"), "/r")

    def test_same_branch_as_manual_session(self):
        self.add_session("m", cwd="/r", branch="fix/x")
        self.add_session("s", cwd="/r", branch="fix/x")
        set_override(self.conn, "2026-11-20-a", "m", "add", now_iso=NOW)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links()[("2026-11-20-a", "s")], ("same_branch", "weak"))

    def test_child_of_manual_parent_inherits(self):
        self.add_session("p", cwd="/r")
        self.add_session("c", kind="headless", cwd="/r")
        self.add_relation("p", "c", "child")
        set_override(self.conn, "2026-11-20-a", "p", "add", now_iso=NOW)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links()[("2026-11-20-a", "c")], ("from_source", "strong"))

    def test_source_passes_its_automatic_task_else_newest_manual(self):
        # s0 は A のフォルダーで始め、手で B にも足した → 分岐先 s1 は自動の根拠の A
        self.add_session("s0", cwd=A)
        self.add_session("s1", cwd="/r")
        self.add_relation("s0", "s1", "fork")
        set_override(self.conn, "2026-11-21-b", "s0", "add", now_iso=NOW)
        # x0 は自動の根拠がなく、手で A、後で B に足した → 分岐先 x1 は新しく足した B
        self.add_session("x0", cwd="/r")
        self.add_session("x1", cwd="/r")
        self.add_relation("x0", "x1", "fork")
        set_override(self.conn, "2026-11-20-a", "x0", "add", now_iso="2026-11-22T00:00:00Z")
        set_override(self.conn, "2026-11-21-b", "x0", "add", now_iso="2026-11-22T01:00:00Z")
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual(got[("2026-11-20-a", "s1")], ("from_source", "strong"))
        self.assertEqual(got[("2026-11-21-b", "x1")], ("from_source", "strong"))
        self.assertNotIn(("2026-11-21-b", "s1"), got)
        self.assertNotIn(("2026-11-20-a", "x1"), got)

    def test_manual_sessions_pass_links_to_parent_and_after_clear(self):
        # 子 c を手で A に足した → 親 p は子の会話から。x を手で B に足した → /clear の後の y は /clear の前の会話から
        self.add_session("p", cwd="/r")
        self.add_session("c", kind="headless", cwd="/r")
        self.add_relation("p", "c", "child")
        set_override(self.conn, "2026-11-20-a", "c", "add", now_iso=NOW)
        self.add_session("x", cwd="/r")
        self.add_session("y", cwd="/r")
        self.add_derived("y", "x", "clear")
        set_override(self.conn, "2026-11-21-b", "x", "add", now_iso=NOW)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        got = self.links()
        self.assertEqual(got[("2026-11-20-a", "p")], ("from_child", "strong"))
        self.assertEqual(got[("2026-11-21-b", "y")], ("from_clear", "weak"))

    def test_own_manual_add_is_not_evidence_for_itself(self):
        # m を手で B に足した。m の自動の結び付きは m 自身の手の結び付きを根拠にしない：B の同じブランチ（m 自身）
        # を数えると A と B の2つに当たり、新しい B を選んで A を落としてしまう
        self.add_session("a1", cwd="/r", branch="fix/x")
        self.add_link("a1", A + "/a.md", "created", ts=TS)
        self.add_session("m", cwd="/r", branch="fix/x", started="2026-11-21T09:00:00Z")
        set_override(self.conn, "2026-11-21-b", "m", "add", now_iso=NOW)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-20-a", "a1"): ("wrote", "confirmed"),
                                        ("2026-11-20-a", "m"): ("same_branch", "weak"),
                                        ("2026-11-21-b", "m"): ("manual", "confirmed")})

    def test_manual_session_does_not_inherit_its_own_link(self):
        # x を手で B に足した。分岐が x → y → x と輪になっていても、x は自分の結び付きを引き継がない
        # （引き継ぐと自動で結び付いたことになり、x のチケット番号の推定（弱）が当たらなくなる）
        self.set_task("2026-11-20-a", ticket="#123", repo="/r")
        self.add_session("x", cwd="/r", branch="fix/123")
        self.add_session("y", cwd="/r")
        self.add_relation("x", "y", "fork")
        self.add_relation("y", "x", "fork")
        set_override(self.conn, "2026-11-21-b", "x", "add", now_iso=NOW)
        link_tasks(self.conn, tasks_root=T, default_branches=["main"])
        self.assertEqual(self.links(), {("2026-11-21-b", "x"): ("manual", "confirmed"),
                                        ("2026-11-20-a", "x"): ("ticket", "weak"),
                                        ("2026-11-21-b", "y"): ("from_source", "strong")})

    def test_set_override_replaces_the_previous_choice(self):
        set_override(self.conn, "2026-11-20-a", "s1", "add", now_iso="2026-11-22T00:00:00Z")
        set_override(self.conn, "2026-11-20-a", "s1", "remove", now_iso="2026-11-22T01:00:00Z")
        rows = [tuple(r) for r in self.conn.execute("SELECT task, session_id, action, at FROM task_overrides")]
        self.assertEqual(rows, [("2026-11-20-a", "s1", "remove", "2026-11-22T01:00:00Z")])
        self.assertEqual(get_meta(self.conn, RELINK_KEY), "1")
        with self.assertRaises(ValueError):
            set_override(self.conn, "2026-11-20-a", "s1", "toggle", now_iso="2026-11-22T02:00:00Z")


class UpdateTest(unittest.TestCase):
    """更新（indexer.update）が結び付けを計算し直すのは、タスクが変わった回、記録を取り込んだ回、手で直した後。"""

    def setUp(self):
        ctx = allow_system_tmp()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = os.path.realpath(self._tmp.name)
        self.root = os.path.join(self.d, "tasks")
        self.task = os.path.join(self.root, "2026-11-20-a")
        write_file(self.task + "/task.md", "# A\n")
        self.fake = FakeClaude(Path(self.d))
        self.ledger = Path(self.d) / "tsuzuki"
        self.conn = make_ledger(self.ledger)
        self.addCleanup(self.conn.close)
        self.sid = session_id(1)
        self.fake.write(self.task, self.sid, [user("進めて", t(0), self.sid, self.task)])

    def run_update(self):
        update(self.conn, Config(), claude_dir=self.fake.dir, ledger_dir=self.ledger,
               home=os.path.join(self.d, "home"), tasks_root=Path(self.root))

    def rules(self):
        return [tuple(r) for r in self.conn.execute("SELECT task, session_id, rule FROM task_sessions")]

    def clear_links(self):
        with self.conn:
            self.conn.execute("DELETE FROM task_sessions")

    def test_links_when_transcripts_are_ingested(self):
        self.run_update()
        self.assertEqual(self.rules(), [("2026-11-20-a", self.sid, "cwd")])
        self.clear_links()
        self.fake.append(self.task, self.sid, [user("続けて", t(1), self.sid, self.task)])
        self.run_update()  # タスクは変わらず、記録だけを取り込んだ
        self.assertEqual(self.rules(), [("2026-11-20-a", self.sid, "cwd")])

    def test_skips_linking_when_nothing_changed(self):
        self.run_update()
        self.clear_links()
        self.run_update()
        self.assertEqual(self.rules(), [])

    def test_relinks_after_override_and_task_change(self):
        self.run_update()
        set_override(self.conn, "2026-11-20-a", self.sid, "add", now_iso=NOW)
        self.run_update()  # 記録もタスクも変わらないが、手で足した印がある
        self.assertEqual(self.rules(), [("2026-11-20-a", self.sid, "manual")])
        self.assertIsNone(get_meta(self.conn, RELINK_KEY))
        self.clear_links()
        write_file(self.task + "/task.md", "# A の題を変えた\n")
        self.run_update()
        self.assertEqual(self.rules(), [("2026-11-20-a", self.sid, "manual")])

    def test_quoted_task_md_in_a_root_with_a_space(self):
        # 空白を含む置き場所の task.md は @"<パス>" の形で渡る。その形でも「task.md を渡した」で
        # 結び付ける
        root = os.path.join(self.d, "My Tasks")
        task_md = os.path.join(root, "2026-11-21-b", "task.md")
        write_file(task_md, "# B\n")
        repo = os.path.join(self.d, "repo")
        os.makedirs(repo)
        sid = session_id(2)
        self.fake.write(repo, sid, [user('@"%s" を読んで、次の一手から進めてください。' % task_md, t(0), sid, repo)])
        update(self.conn, Config(), claude_dir=self.fake.dir, ledger_dir=self.ledger,
               home=os.path.join(self.d, "home"), tasks_root=Path(root))
        self.assertIn(("2026-11-21-b", sid, "named_task_md"), self.rules())


if __name__ == "__main__":
    unittest.main()
