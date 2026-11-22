"""画面に出すデータ（views）と再開のコマンド（resume）のテスト。"""
import os
import shlex
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tsuzuki.config import DEFAULT_DOC_EXTENSIONS, Config
from tsuzuki.indexer import update
from tsuzuki.ingest import IngestContext, ingest_transcript
from tsuzuki.resume import is_session_id, resume_command
from tsuzuki.sources.live import LiveSession
from tsuzuki.views import (
    CONVERSATION_TURNS, MOVED_LABEL, RELATION_LABELS, TIMELINE_LABELS, children, conversation_window, count_documents,
    count_sessions,
    doc_items, document_detail, document_kinds, documents_list, ledger_counts, list_places, list_sessions,
    orphan_headless, place_displays, place_labels, place_overview, session_detail, session_rows, turn_excerpt,
)
from tests.fixtures import FakeClaude, assistant, make_ledger, recap, sid, t, tool_result, user

HOME = "/Users/u"
CWD = "/w"
S0, S1, SE, S9 = sid(0), sid(1), sid(8), sid(9)
PLACE, OTHER = 1, 2


def at(minutes: float) -> float:
    """t(minutes) と同じ時刻の UNIX 時刻。"""
    return datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() + minutes * 60


def headless(text, ts, session, cwd):
    return user(text, ts, session, cwd, human=False, entrypoint="sdk-cli")


class LedgerCase(unittest.TestCase):
    """一時ディレクトリの台帳と、表に行を直接入れる補助。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = os.path.realpath(tmp.name)
        self.ledger = Path(self.d) / "tsuzuki"
        self.conn = make_ledger(self.ledger)
        self.addCleanup(self.conn.close)

    def add_place(self, pid, path, display):
        self.conn.execute("INSERT INTO places(id, path, display) VALUES (?, ?, ?)", (pid, path, display))
        self.conn.commit()

    def add_session(self, session, kind, place, start, end, **cols):
        values = dict(id=session, transcript_path="/x/%s.jsonl" % session, kind=kind, place_id=place,
                      started_at=start, ended_at=end)
        values.update(cols)
        self.conn.execute("INSERT INTO sessions(%s) VALUES (%s)" % (", ".join(values), ", ".join("?" * len(values))),
                          list(values.values()))
        self.conn.commit()

    def add_relation(self, src, dst, type, confidence="strong", evidence=""):
        self.conn.execute("INSERT INTO relations(src, dst, type, confidence, evidence) VALUES (?, ?, ?, ?, ?)",
                          (src, dst, type, confidence, evidence))
        self.conn.commit()

    def add_digest(self, session, first_prompt):
        self.conn.execute("INSERT INTO digests(session_id, first_prompt, text) VALUES (?, ?, ?)",
                          (session, first_prompt, first_prompt))
        self.conn.commit()

    def add_doc(self, path, place, kind, mtime, *, title=None, body=None, size=100):
        cur = self.conn.execute(
            "INSERT INTO documents(path, place_id, kind, ext, size, mtime, title, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (path, place, kind, os.path.splitext(path)[1].lower(), size, mtime, title, body))
        self.conn.commit()
        return cur.lastrowid

    def add_link(self, session, path, rel, ts):
        self.conn.execute("INSERT INTO doc_links(session_id, path, rel, ts) VALUES (?, ?, ?, ?)",
                          (session, path, rel, ts))
        self.conn.commit()


class CardTaskFileTest(LedgerCase):
    def test_task_files_are_not_card_documents(self):
        self.add_place(PLACE, "/r/w", "dev/w")
        self.add_session(S0, "interactive", PLACE, t(0), t(10), first_prompt="x")
        self.add_link(S0, "/tasks/2026-11-20-x/plan.md", "created", t(1))
        self.add_link(S0, "/tasks/2026-11-20-x/data/q.sql", "task_file", t(2))
        d = session_detail(self.conn, S0, live={}, home=HOME)
        self.assertEqual(d.docs_total, 1)
        self.assertEqual([x.path for x in d.docs], ["/tasks/2026-11-20-x/plan.md"])


class TaskLinkViewTest(LedgerCase):
    def add_task(self, name, title, path):
        self.conn.execute("INSERT INTO tasks(name, path, title, stage) VALUES (?, ?, ?, '調査')", (name, path, title))
        self.conn.commit()

    def test_session_detail_has_tasks(self):
        self.add_task("a", "A の題", "/tasks/a")
        self.add_session(S0, "interactive", None, t(0), t(10), first_prompt="x")
        self.conn.execute("INSERT INTO task_sessions(task, session_id, rule, confidence) VALUES ('a', ?, 'cwd', "
                          "'confirmed')", (S0,))
        self.conn.commit()
        d = session_detail(self.conn, S0, live={}, home=HOME)
        self.assertEqual(d.tasks, [("a", "A の題", "フォルダーで開始", "確定")])

    def test_doc_items_have_task(self):
        self.add_task("a", "A の題", "/tasks/a")
        self.add_task("a-b", "別の題", "/tasks/a-b")
        in_task = self.add_doc("/tasks/a/research-x.md", None, "research", at(1))
        in_sibling = self.add_doc("/tasks/a-b/plan-y.md", None, "plan", at(2))
        elsewhere = self.add_doc("/r/docs/api.md", None, "other", at(3))
        items = doc_items(self.conn, [in_task, in_sibling, elsewhere], home=HOME)
        self.assertEqual(items[in_task].task, ("a", "A の題"))
        self.assertEqual(items[in_sibling].task, ("a-b", "別の題"))
        self.assertIsNone(items[elsewhere].task)

    def test_documents_list_has_task(self):
        self.add_task("a", "A の題", "/tasks/a")
        self.add_doc("/tasks/a/task.md", None, "cover", at(1))
        self.assertEqual(documents_list(self.conn, home=HOME)[0].task, ("a", "A の題"))

    def test_only_the_task_folders_own_task_md_is_the_cover(self):
        # タスクのフォルダーの直下の task.md だけが表紙（入れ子の sub/task.md は、そのタスクの資料）
        self.add_task("a", "A の題", "/tasks/a")
        cover = self.add_doc("/tasks/a/task.md", None, "cover", at(1))
        nested = self.add_doc("/tasks/a/sub/task.md", None, "other", at(2))
        items = doc_items(self.conn, [cover, nested], home=HOME)
        self.assertEqual((items[cover].task, items[cover].task_cover), (("a", "A の題"), True))
        self.assertEqual((items[nested].task, items[nested].task_cover), (("a", "A の題"), False))


class IngestedSessionTest(LedgerCase):
    """記録から作った台帳：S0 から分岐した S1（要約 t(2) の後に本人の入力2回、Write の資料1件、子 S9）と、空の SE。"""

    def setUp(self):
        super().setUp()
        fake = FakeClaude(Path(self.d))
        fake.write(CWD, S0, [user("元の会話", t(-30), S0, CWD), assistant(t(-29), S0, CWD, text="はい")])
        fake.write(CWD, S1, [
            user("最初の依頼", t(0), S1, CWD, forked_from=S0),
            assistant(t(1), S1, CWD, text="了解しました",
                      tools=[("Write", {"file_path": CWD + "/docs/a.md"}), ("Agent", {"description": "調査"})]),
            tool_result('{"session_id": "%s"}' % S9, t(1.5), S1, CWD),
            recap("資料を作りました。", t(2), S1, CWD),
            user("2回目の依頼", t(3), S1, CWD),
            assistant(t(3.5), S1, CWD, text="直しました"),
            user("3回目の依頼", t(4), S1, CWD),
        ])
        fake.write(CWD, S9, [headless("子の課題\n詳しくは資料を読む", t(1.2), S9, CWD)])
        fake.write(CWD, SE, [assistant(t(5), SE, CWD, text="応答だけ")])
        update(self.conn, Config(), claude_dir=fake.dir, ledger_dir=self.ledger, home=HOME)


    def test_session_detail(self):
        d = session_detail(self.conn, S1, live={S1: LiveSession(1, S1, "/w", "idle")}, home=HOME)
        self.assertEqual(d.prompts_after_recap, 2)
        self.assertTrue(d.live)
        self.assertEqual(d.docs[0].display_path, "docs/a.md")
        self.assertIn(("分岐元", S0), [(r.label, r.peer_id) for r in d.relations])
        self.assertEqual(d.children_total, 1)
        self.assertEqual(d.resume_command, "cd /w && claude --resume " + S1)

    def test_list_hides_headless_and_empty(self):
        self.assertEqual({r.kind for r in list_sessions(self.conn, live={})}, {"interactive"})
        self.assertIn("headless", {r.kind for r in list_sessions(self.conn, include_headless=True, live={})})

    def test_conversation_groups_tools(self):
        w = conversation_window(self.conn, S1)
        self.assertEqual([i.kind for i in w.items][:3], ["user", "assistant", "tools"])
        self.assertEqual(w.items[2].tools, ["Write docs/a.md", "Agent（調査）"])

    # 状況カード

    def test_session_detail_fields(self):
        d = session_detail(self.conn, S1, live={}, home=HOME)
        self.assertEqual((d.id, d.title, d.kind, d.place_display, d.cwd, d.branch),
                         (S1, "最初の依頼", "interactive", "/w", "/w", "main"))
        self.assertEqual((d.summary, d.summary_is_recap, d.recap, d.recap_at),
                         ("資料を作りました。", True, "資料を作りました。", t(2)))
        self.assertEqual((d.first_prompt, d.last_prompt, d.last_reply), ("最初の依頼", "3回目の依頼", "直しました"))
        self.assertEqual((d.n_prompts, d.n_agents, d.n_children, d.started_at, d.ended_at), (3, 1, 1, t(0), t(4)))
        self.assertEqual((d.live, d.transcript_exists, d.prs), (False, True, []))
        doc = d.docs[0]
        self.assertEqual((d.docs_total, doc.path, doc.rel, doc.doc_id, doc.kind, doc.mtime, doc.size),
                         (1, "/w/docs/a.md", "created", None, "other", None, None))
        # 子はつながりに並べず（数だけ）、子の側では親として出す
        self.assertEqual([(r.type, r.confidence, r.peer_title, r.peer_place, r.peer_started_at, r.peer_ended_at,
                           r.cross_place) for r in d.relations],
                         [("fork", "confirmed", "元の会話", "/w", t(-30), t(-29), False)])
        child = session_detail(self.conn, S9, live={}, home=HOME)
        self.assertEqual([(r.label, r.peer_id) for r in child.relations], [("親", S1)])
        # つながりの根拠（更新が relations.evidence に残したもの）と、場所の短い名前も添える
        self.assertEqual([(r.label, r.evidence) for r in d.relations], [("分岐元", "記録の forkedFrom")])
        self.assertEqual([(r.label, r.evidence) for r in child.relations], [("親", "記録に ID")])
        self.assertEqual((d.place_label, d.relations[0].peer_place_label), ("/w", "/w"))
        self.assertEqual((child.kind, child.title, child.summary, child.summary_is_recap, child.prompts_after_recap),
                         ("headless", "子の課題\n詳しくは資料を読む", "子の課題\n詳しくは資料を読む", False, 0))
        self.assertIsNone(session_detail(self.conn, sid(77), live={}, home=HOME))

    def test_session_detail_docs_and_resume(self):
        path = HOME + "/notes/memo.md"
        for n in range(6):
            self.add_link(S0, "/w/d%d.md" % n, "read", t(-29 + n))
        self.add_link(S0, "/w/d0.md", "edited", t(-30))
        self.add_link(S0, path, "named", None)
        self.add_doc("/w/d5.md", None, "progress", 123.0, size=9)
        d = session_detail(self.conn, S0, live={}, home=HOME)
        self.assertEqual(d.docs_total, 7)
        self.assertEqual([(x.display_path, x.rel) for x in d.docs],
                         [("d5.md", "read"), ("d4.md", "read"), ("d3.md", "read"), ("d2.md", "read"),
                          ("d1.md", "read")])
        self.assertEqual((d.docs[0].kind, d.docs[0].mtime, d.docs[0].size), ("progress", 123.0, 9))
        self.assertIsNotNone(d.docs[0].doc_id)
        self.add_link(S0, path, "created", t(0))
        self.add_link(S0, "/w/d0.md", "created", t(1))
        d = session_detail(self.conn, S0, live={}, home=HOME)
        # 最も強い関わりと、最も新しい時刻で並べる。cwd の外はホームを ~ で示す
        self.assertEqual([(x.display_path, x.rel) for x in d.docs[:2]],
                         [("d0.md", "created"), ("~/notes/memo.md", "created")])
        self.assertEqual(d.docs[1].kind, "other")
        # 記録が消えたセッションと、ID の形でないセッションには再開のコマンドを出さない
        self.conn.execute("UPDATE sessions SET transcript_exists = 0 WHERE id = ?", (S0,))
        self.add_session("x; rm -rf ~", "interactive", None, t(0), t(1), cwd="/w")
        self.assertIsNone(session_detail(self.conn, S0, live={}, home=HOME).resume_command)
        self.assertIsNone(session_detail(self.conn, "x; rm -rf ~", live={}, home=HOME).resume_command)

    # 一覧と会話

    def test_list_order_and_filters(self):
        rows = list_sessions(self.conn, include_headless=True, live={S9: LiveSession(2, S9, "/w", "busy")})
        self.assertEqual([r.id for r in rows], [S1, S9, S0])
        s1 = rows[0]
        self.assertEqual((s1.title, s1.place_display, s1.ended_at, s1.n_prompts, s1.n_children, s1.prs),
                         ("最初の依頼", "/w", t(4), 3, 1, []))
        self.assertEqual((s1.summary_is_recap, s1.live, s1.transcript_exists), (True, False, True))
        self.assertEqual((rows[1].live, rows[1].summary), (True, "子の課題\n詳しくは資料を読む"))
        self.assertEqual((rows[2].summary, rows[2].summary_is_recap), ("元の会話", False))
        self.assertEqual([r.id for r in list_sessions(self.conn, days=1, live={}, now=lambda: at(0) + 86400)], [S1])
        self.assertEqual(list_sessions(self.conn, place_id=999, live={}), [])
        place = self.conn.execute("SELECT place_id FROM sessions WHERE id = ?", (S1,)).fetchone()[0]
        self.assertEqual([r.id for r in list_sessions(self.conn, place_id=place, live={})], [S1, S0])
        self.assertEqual([r.id for r in list_sessions(self.conn, include_headless=True, limit=2, live={})], [S1, S9])

    def test_conversation_window_range(self):
        w = conversation_window(self.conn, S1)
        self.assertEqual((w.session_id, w.source, w.total_turns, w.first_turn, w.last_turn), (S1, "ledger", 3, 0, 3))
        self.assertEqual([(i.kind, i.turn, i.text) for i in w.items],
                         [("user", 1, "最初の依頼"), ("assistant", 1, "了解しました"), ("tools", 1, ""),
                          ("user", 2, "2回目の依頼"), ("assistant", 2, "直しました"), ("user", 3, "3回目の依頼")])
        self.assertEqual((w.items[0].ts, w.items[2].ts, w.items[0].tools), (t(0), t(1), []))
        w = conversation_window(self.conn, S1, around_turn=2, radius=0)
        self.assertEqual(([i.text for i in w.items], w.first_turn, w.last_turn), (["2回目の依頼", "直しました"], 2, 2))
        w = conversation_window(self.conn, S1, around_turn=99, radius=1)
        self.assertEqual(([i.text for i in w.items], w.first_turn, w.last_turn), (["2回目の依頼", "直しました", "3回目の依頼"], 2, 3))
        self.assertIsNone(conversation_window(self.conn, sid(77)))

    def test_turn_excerpt(self):
        # 検索で一致したターンの発言（状況カードの一致箇所）。続けて呼んだツールは1つにまとめる
        items = turn_excerpt(self.conn, S1, 1)
        self.assertEqual([(i.kind, i.turn, i.ts, i.text, i.tools) for i in items],
                         [("user", 1, t(0), "最初の依頼", []), ("assistant", 1, t(1), "了解しました", []),
                          ("tools", 1, t(1), "", ["Write docs/a.md", "Agent（調査）"])])
        self.assertEqual([(i.kind, i.text) for i in turn_excerpt(self.conn, S1, 2)],
                         [("user", "2回目の依頼"), ("assistant", "直しました")])
        self.assertEqual(turn_excerpt(self.conn, S1, 99), [])
        self.assertEqual(turn_excerpt(self.conn, S9, 0), [])  # ヘッドレスは台帳に発言がない
        self.assertEqual(turn_excerpt(self.conn, sid(77), 1), [])


class PlaceViewsTest(LedgerCase):
    """場所 PLACE（dev/w）の対話 A→B（continued）→C（handoff）、C を参照する別の場所 OTHER（dev/other）の D、
    A の子 H1、PLACE の親の決まらないヘッドレス H2。OTHER には親の決まらない H3・H4。"""

    A, B, C, D = sid(11), sid(12), sid(13), sid(14)
    H1, H2, H3, H4 = sid(21), sid(22), sid(23), sid(24)

    def setUp(self):
        super().setUp()
        A, B, C, D, H1, H2, H3, H4 = self.A, self.B, self.C, self.D, self.H1, self.H2, self.H3, self.H4
        self.add_place(PLACE, "/r/w", "dev/w")
        self.add_place(OTHER, "/r/other", "dev/other")
        self.add_place(3, "/r/empty", "dev/empty")  # 空のセッションだけの場所は出さない
        self.add_place(4, "/r/docs", "dev/docs")  # 資料だけの場所は出す
        self.add_session(A, "interactive", PLACE, t(0), t(10), first_prompt="最初の仕事", n_prompts=3)
        self.add_session(B, "interactive", PLACE, t(20), t(30), ai_title="続きの仕事")
        self.add_session(C, "interactive", PLACE, t(40), t(50), custom_title="資料を読んで再開")
        self.add_session(D, "interactive", OTHER, t(60), t(70), first_prompt="別の場所の仕事")
        self.add_session(H1, "headless", PLACE, t(1), t(2))
        self.add_session(H2, "headless", PLACE, t(5), t(6))
        self.add_session(H3, "headless", OTHER, t(65), t(66))
        self.add_session(H4, "headless", OTHER, t(68), t(90))
        self.add_session(sid(31), "empty", 3, t(90), t(95))
        self.add_digest(H1, "子の仕事")
        self.add_digest(H2, "親の分からない仕事")
        self.add_digest(H3, "\n\n  親の分からない別の仕事  \n二行目")
        self.add_digest(H4, "あ" * 200)
        self.add_relation(A, B, "continued", "confirmed", "記録の continued-in")
        self.add_relation(B, C, "handoff", evidence="資料 h.md")
        # C には B からの引き継ぎもあるので、優先順位の高い引き継ぎを使う
        self.add_relation(A, C, "reference", evidence="記録に ID")
        self.add_relation(C, D, "reference", evidence="記録に ID")
        self.add_relation(A, H1, "child", "confirmed", "フックの記録")
        self.add_doc("/r/w/docs/specs/a-design.md", PLACE, "design", 100.0)
        self.add_doc("/r/w/docs/specs/b-design.md", PLACE, "design", 300.0)
        self.add_doc("/r/w/README.md", PLACE, "readme", 200.0)
        self.add_doc("/r/w/notes/memo.txt", PLACE, "other", 50.0)
        self.add_doc("/r/docs/x.md", 4, "other", 10.0)


    def test_place_overview(self):
        ov = place_overview(self.conn, PLACE)
        self.assertEqual([i.via_label for i in ov.timeline], [None, "続きの会話へ", "引き継ぎ資料を読んで開始", "別の場所へ移った"])
        self.assertEqual(ov.timeline[-1].other_place, "dev/other")
        self.assertEqual(ov.orphans_total, 1)

    # 場所

    def test_place_overview_details(self):
        ov = place_overview(self.conn, PLACE, home=HOME)
        self.assertEqual([(i.session_id, i.title, i.started_at, i.ended_at, i.other_place) for i in ov.timeline],
                         [(self.A, "最初の仕事", t(0), t(10), None), (self.B, "続きの仕事", t(20), t(30), None),
                          (self.C, "資料を読んで再開", t(40), t(50), None),
                          (self.D, "別の場所の仕事", t(60), t(70), "dev/other")])
        self.assertEqual((ov.timeline[0].n_prompts, ov.timeline[0].n_children), (3, 1))
        self.assertEqual(ov.timeline[-1].via_label, MOVED_LABEL)
        p = ov.place
        self.assertEqual((p.id, p.display, p.n_interactive, p.n_headless, p.n_docs, p.last_at),
                         (PLACE, "dev/w", 3, 2, 4, t(50)))
        self.assertEqual([(f, [d.name for d in docs]) for f, docs in ov.folders],
                         [("docs/specs", ["b-design.md", "a-design.md"]), (".", ["README.md"]),
                          ("notes", ["memo.txt"])])
        self.assertIsNone(place_overview(self.conn, 999, home=HOME))

    def test_place_overview_limits(self):
        self.add_place(5, "/r/big", "dev/big")
        for n in range(25):
            self.add_session(sid(100 + n), "interactive", 5, t(n), t(n + 0.5))
        for f in range(7):
            for k in range(7):
                self.add_doc("/r/big/f%d/d%d.md" % (f, k), 5, "other", 1000.0 + f * 10 + k)
        # 時系列の後の項目から前の項目へのつながり、時系列の外（古い）からのつながりには言葉を付けない。
        # 同じ場所の時系列の外へのつながりは「別の場所へ移った」にしない
        self.add_relation(sid(112), sid(106), "reference")
        self.add_relation(sid(106), sid(107), "handoff")
        self.add_relation(sid(100), sid(108), "continued", "confirmed")
        self.add_relation(sid(110), sid(101), "reference")
        ov = place_overview(self.conn, 5, home=HOME)
        self.assertEqual([i.session_id for i in ov.timeline], [sid(100 + n) for n in range(5, 25)])
        self.assertEqual({i.session_id: i.via_label for i in ov.timeline if i.via_label},
                         {sid(107): "引き継ぎ資料を読んで開始"})
        self.assertEqual([f for f, _ in ov.folders], ["f6", "f5", "f4", "f3", "f2"])
        self.assertEqual([d.name for d in ov.folders[0][1]], ["d6.md", "d5.md", "d4.md", "d3.md", "d2.md"])
        self.assertEqual(ov.orphans_total, 0)

    def test_moved_needs_a_place(self):
        # 場所のない相手は「別の場所へ移った」に足さない（場所の名前を出せないため）
        nowhere = sid(15)
        self.add_session(nowhere, "interactive", None, t(55), t(56), first_prompt="場所のない仕事")
        self.add_relation(self.C, nowhere, "reference")
        ov = place_overview(self.conn, PLACE, home=HOME)
        self.assertEqual([(i.session_id, i.via_label, i.other_place) for i in ov.timeline[3:]],
                         [(self.D, MOVED_LABEL, "dev/other")])

    def test_list_places(self):
        self.assertEqual([(p.id, p.display, p.n_interactive, p.n_headless, p.n_docs, p.last_at)
                          for p in list_places(self.conn)],
                         [(OTHER, "dev/other", 1, 2, 0, t(90)), (PLACE, "dev/w", 3, 2, 4, t(50)),
                          (4, "dev/docs", 0, 0, 1, None)])

    def test_orphans_and_children(self):
        self.assertEqual([(c.id, c.started_at, c.first_line) for c in orphan_headless(self.conn, OTHER)],
                         [(self.H4, t(68), "あ" * 120), (self.H3, t(65), "親の分からない別の仕事")])
        self.assertEqual([c.id for c in orphan_headless(self.conn, OTHER, offset=1, limit=1)], [self.H3])
        self.assertEqual([c.id for c in orphan_headless(self.conn, PLACE)], [self.H2])
        self.assertEqual([(c.id, c.started_at, c.first_line) for c in children(self.conn, self.A)],
                         [(self.H1, t(1), "子の仕事")])
        self.assertEqual(children(self.conn, self.B), [])

    def test_labels(self):
        # 画面に出す言葉のとおり
        self.assertEqual(RELATION_LABELS, {
            ("fork", "in"): "分岐元", ("fork", "out"): "分岐先", ("continued", "in"): "前の会話",
            ("continued", "out"): "続きの会話", ("child", "in"): "親", ("child", "out"): "子",
            ("handoff", "in"): "引き継ぎ資料を読んで開始", ("handoff", "out"): "引き継ぎ先",
            ("reference", "in"): "参照した会話", ("reference", "out"): "参照された会話"})
        self.assertEqual(TIMELINE_LABELS, {"fork": "分岐", "continued": "続きの会話へ", "child": "子",
                                           "handoff": "引き継ぎ資料を読んで開始", "reference": "参照"})
        self.assertEqual(MOVED_LABEL, "別の場所へ移った")

    def test_short_place_names(self):
        # 場所の短い名前：表示名の最後の要素。台帳のほかの場所と重なれば親を足す
        self.assertEqual({p.id: (p.display, p.label) for p in list_places(self.conn)},
                         {OTHER: ("dev/other", "other"), PLACE: ("dev/w", "w"), 4: ("dev/docs", "docs")})
        # 入力履歴にしかない場所（一覧には出ない）も、台帳の場所なので重なりの数に入る
        self.add_place(5, "/old/w", "old/w")
        self.conn.execute("INSERT INTO history_prompts(id, session_id, project, place_id, ts, text) "
                          "VALUES (1, ?, '/old/w', 5, ?, '前の仕事')", (sid(40), t(1)))
        self.conn.commit()
        self.assertEqual({p.id: (p.display, p.label) for p in list_places(self.conn)},
                         {OTHER: ("dev/other", "other"), PLACE: ("dev/w", "dev/w"), 4: ("dev/docs", "docs")})
        self.assertEqual(place_labels(self.conn, [5, PLACE, 999, 5]), {5: "old/w", PLACE: "dev/w"})
        self.assertEqual(place_labels(self.conn, []), {})
        # 一覧の行・状況カード・つながり・場所の概要と時系列・資料にも、同じ短い名前を添える
        rows = list_sessions(self.conn, include_headless=True, live={})
        self.assertEqual({(r.place_display, r.place_label) for r in rows}, {("dev/w", "dev/w"), ("dev/other", "other")})
        self.assertEqual(session_rows(self.conn, [self.D], live={})[self.D].place_label, "other")
        c = session_detail(self.conn, self.C, live={}, home=HOME)
        self.assertEqual((c.place_display, c.place_label), ("dev/w", "dev/w"))
        self.assertEqual([(r.peer_place, r.peer_place_label) for r in c.relations],
                         [("dev/w", "dev/w"), ("dev/w", "dev/w"), ("dev/other", "other")])
        ov = place_overview(self.conn, PLACE, home=HOME)
        self.assertEqual((ov.place.display, ov.place.label), ("dev/w", "dev/w"))
        self.assertEqual([(i.other_place, i.other_place_label) for i in ov.timeline],
                         [(None, None)] * 3 + [("dev/other", "other")])
        self.assertEqual({(d.place_display, d.place_label) for _, docs in ov.folders for d in docs},
                         {("dev/w", "dev/w")})
        docs = documents_list(self.conn, home=HOME)
        self.assertEqual({(d.place_display, d.place_label) for d in docs}, {("dev/w", "dev/w"), ("dev/docs", "docs")})
        self.assertEqual(doc_items(self.conn, [docs[0].id], home=HOME)[docs[0].id].place_label, docs[0].place_label)

    def test_evidence_of_relations(self):
        # つながりの根拠（relations.evidence）を、状況カードのつながりと、場所の時系列の矢印に添える
        c = session_detail(self.conn, self.C, live={}, home=HOME)
        self.assertEqual([(r.label, r.evidence) for r in c.relations],
                         [("参照した会話", "記録に ID"), ("引き継ぎ資料を読んで開始", "資料 h.md"),
                          ("参照された会話", "記録に ID")])
        ov = place_overview(self.conn, PLACE, home=HOME)
        # 矢印は優先順位の高いつながり（C には B からの引き継ぎ）の根拠。別の場所へ移った相手にも根拠を添える
        self.assertEqual([(i.via_label, i.via_evidence) for i in ov.timeline],
                         [(None, None), ("続きの会話へ", "記録の continued-in"), ("引き継ぎ資料を読んで開始", "資料 h.md"),
                          (MOVED_LABEL, "記録に ID")])

    def test_count_sessions_matches_the_list(self):
        # 一覧を上限で切ったときに、条件に合うセッションの数を画面に出すため
        later = lambda: at(60) + 86400  # noqa: E731  1日の期間が t(60) から始まる
        for kw, n in ((dict(), 4), (dict(include_headless=True), 8), (dict(place_id=PLACE), 3),
                      (dict(place_id=PLACE, include_headless=True), 5), (dict(days=1, now=later), 1),
                      (dict(days=1, include_headless=True, now=later), 3), (dict(place_id=999), 0)):
            with self.subTest(**{k: v for k, v in kw.items() if k != "now"}):
                self.assertEqual(count_sessions(self.conn, **kw), n)
                self.assertEqual(len(list_sessions(self.conn, live={}, limit=1000, **kw)), n)
        # 一覧の行は上限まで。数は上限によらない
        self.assertEqual(len(list_sessions(self.conn, include_headless=True, live={}, limit=2)), 2)

    def test_weak_relation_label(self):
        self.add_relation(self.B, self.H2, "child", "weak")
        h2 = session_detail(self.conn, self.H2, live={}, home=HOME)
        self.assertEqual([(r.label, r.peer_id, r.peer_title) for r in h2.relations], [("親（推定）", self.B, "続きの仕事")])
        c = session_detail(self.conn, self.C, live={}, home=HOME)
        self.assertEqual([(r.label, r.peer_id, r.cross_place) for r in c.relations],
                         [("参照した会話", self.A, False), ("引き継ぎ資料を読んで開始", self.B, False),
                          ("参照された会話", self.D, True)])


class DocumentViewsTest(LedgerCase):
    PATH = "/r/w/docs/specs/x-design.md"

    def setUp(self):
        super().setUp()
        self.add_place(PLACE, "/r/w", "dev/w")
        self.design = self.add_doc(self.PATH, PLACE, "design", at(10), title="設計",
                                   body="# 設計\n\n本文 **大事**\n\n<script>alert(1)</script>")
        self.page = self.add_doc("/r/w/out/index.html", PLACE, "other", at(20), title="ページ", body="ページ 本文")
        self.notes = self.add_doc("/r/w/notes.txt", PLACE, "other", at(5), body="a < b\n")
        self.pdf = self.add_doc("/r/w/slides.pdf", PLACE, "other", at(1))
        self.plan = self.add_doc(HOME + "/plans/p-plan.md", None, "plan", at(30), body="# 計画")
        self.add_session(S0, "interactive", PLACE, t(0), t(10), first_prompt="作った会話")
        self.add_session(S1, "interactive", PLACE, t(20), t(30), first_prompt="読んだ会話")
        self.add_session(SE, "empty", PLACE, t(40), t(41))
        self.add_link(S0, self.PATH, "created", t(1))
        self.add_link(S0, self.PATH, "read", t(25))
        self.add_link(S1, self.PATH, "read", t(22))
        self.add_link(SE, self.PATH, "read", t(40))

    def test_documents_list(self):
        docs = documents_list(self.conn, home=HOME)
        self.assertEqual([(d.name, d.kind, d.folder, d.place_display) for d in docs],
                         [("p-plan.md", "plan", "~/plans", ""), ("index.html", "other", "out", "dev/w"),
                          ("x-design.md", "design", "docs/specs", "dev/w"), ("notes.txt", "other", ".", "dev/w"),
                          ("slides.pdf", "other", ".", "dev/w")])
        d = docs[2]
        self.assertEqual((d.id, d.path, d.ext, d.size, d.mtime), (self.design, self.PATH, ".md", 100, at(10)))
        self.assertEqual([d.id for d in documents_list(self.conn, kind="design", home=HOME)], [self.design])
        self.assertEqual([d.name for d in documents_list(self.conn, kind="other", place_id=PLACE, home=HOME)],
                         ["index.html", "notes.txt", "slides.pdf"])
        self.assertEqual(documents_list(self.conn, kind="memory", home=HOME), [])
        self.assertEqual(len(documents_list(self.conn, limit=2, home=HOME)), 2)

    def test_count_documents_matches_the_list(self):
        # 資料の一覧を上限で切ったときに、条件に合う資料の数を画面に出すため
        for kw, n in ((dict(), 5), (dict(kind="other"), 3), (dict(place_id=PLACE), 4),
                      (dict(kind="other", place_id=PLACE), 3), (dict(kind="plan", place_id=PLACE), 0),
                      (dict(kind="memory"), 0), (dict(place_id=999), 0)):
            with self.subTest(**kw):
                self.assertEqual(count_documents(self.conn, **kw), n)
                self.assertEqual(len(documents_list(self.conn, limit=1000, home=HOME, **kw)), n)
        # 一覧の行は上限まで。数は上限によらない
        self.assertEqual(len(documents_list(self.conn, limit=2, home=HOME)), 2)

    def test_document_detail(self):
        md = document_detail(self.conn, self.design, home=HOME)
        self.assertEqual((md.item.id, md.item.name, md.item.folder, md.title),
                         (self.design, "x-design.md", "docs/specs", "設計"))
        self.assertIn("<h1>設計</h1>", md.html)
        self.assertIn("<strong>大事</strong>", md.html)
        self.assertNotIn("<script>", md.html)
        # 空のセッションは出さず、1つのセッションは最も強い関わりで1回だけ、新しい順に出す
        self.assertEqual([(rel, row.id, row.title, row.live) for rel, row in md.links],
                         [("created", S0, "作った会話", False), ("read", S1, "読んだ会話", False)])
        live = {S1: LiveSession(3, S1, "/r/w", "busy")}
        self.assertEqual([row.live for _, row in document_detail(self.conn, self.design, home=HOME, live=live).links],
                         [False, True])
        page = document_detail(self.conn, self.page, home=HOME)
        self.assertEqual((page.title, page.html, page.links), ("ページ", None, []))
        self.assertIsNone(document_detail(self.conn, self.pdf, home=HOME).html)
        self.assertEqual(document_detail(self.conn, self.notes, home=HOME).html, "<pre>a &lt; b\n</pre>")
        self.assertEqual(document_detail(self.conn, self.plan, home=HOME).item.folder, "~/plans")
        self.assertIsNone(document_detail(self.conn, 999, home=HOME))

    def test_lookups_for_search_hits(self):
        # 検索の一致の ID から、題名・場所・資料の情報を引く。台帳にない ID は含めない
        live = {S1: LiveSession(3, S1, "/r/w", "busy")}
        many = [sid(1000 + n) for n in range(1200)]  # IN (...) を分けて引く数
        rows = session_rows(self.conn, many + [S1, S0, sid(77), S1], live=live)
        self.assertEqual(sorted(rows), [S0, S1])
        self.assertEqual((rows[S0].title, rows[S0].place_display, rows[S0].live, rows[S0].transcript_exists),
                         ("作った会話", "dev/w", False, True))
        self.assertEqual((rows[S1].id, rows[S1].title, rows[S1].live), (S1, "読んだ会話", True))
        self.assertEqual(session_rows(self.conn, [], live={}), {})
        items = doc_items(self.conn, [self.design, self.plan, 999], home=HOME)
        self.assertEqual(sorted(items), sorted([self.design, self.plan]))
        d = items[self.design]
        self.assertEqual((d.id, d.name, d.kind, d.folder, d.place_display, d.mtime, d.ext),
                         (self.design, "x-design.md", "design", "docs/specs", "dev/w", at(10), ".md"))
        self.assertEqual((items[self.plan].folder, items[self.plan].place_display), ("~/plans", ""))
        self.assertEqual(doc_items(self.conn, list(range(2000, 3200)), home=HOME), {})
        self.add_place(OTHER, "/r/other", "dev/other")
        self.assertEqual(place_displays(self.conn, [OTHER, 999, OTHER]), {OTHER: "dev/other"})
        self.assertEqual(place_displays(self.conn, [PLACE]), {PLACE: "dev/w"})
        self.assertEqual(place_displays(self.conn, []), {})

    def test_counts_for_filters_and_status(self):
        # 資料の種類ごとの数（場所で絞れる）と、画面の状態に出す台帳の数（空のセッションは数えない）
        self.assertEqual(document_kinds(self.conn), {"design": 1, "other": 3, "plan": 1})
        self.assertEqual(document_kinds(self.conn, place_id=PLACE), {"design": 1, "other": 3})
        self.assertEqual(document_kinds(self.conn, place_id=999), {})
        self.add_session(S9, "headless", PLACE, t(50), t(51))
        self.assertEqual(ledger_counts(self.conn), {"interactive": 2, "headless": 1, "documents": 5})


class TranscriptConversationTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeClaude(Path(self.d))
        self.ctx = IngestContext(home=HOME, exts=tuple(DEFAULT_DOC_EXTENSIONS))

    def ingest(self, session, records):
        path = self.fake.write(CWD, session, records)
        ingest_transcript(self.conn, path, self.ctx)
        return path

    def test_headless_conversation_from_transcript(self):
        path = self.ingest(S9, [
            headless("課題をやって", t(0), S9, CWD),
            assistant(t(1), S9, CWD, text="読みます", entrypoint="sdk-cli",
                      tools=[("Read", {"file_path": CWD + "/a.md"}), ("Bash", {"description": "テスト"})]),
            tool_result("ok", t(2), S9, CWD, entrypoint="sdk-cli"),
            assistant(t(3), S9, CWD, text="完了しました", entrypoint="sdk-cli"),
        ])
        w = conversation_window(self.conn, S9)
        self.assertEqual((w.session_id, w.source, w.total_turns, w.first_turn, w.last_turn),
                         (S9, "transcript", 0, 0, 0))
        self.assertEqual([(i.kind, i.text, i.turn) for i in w.items],
                         [("user", "課題をやって", 0), ("assistant", "読みます", 0), ("tools", "", 0),
                          ("assistant", "完了しました", 0)])
        self.assertEqual((w.items[2].tools, w.items[1].ts), (["Read a.md", "Bash（テスト）"], t(1)))
        # 記録のファイルが読めなければ、本文なし
        os.remove(str(path))
        self.assertEqual((conversation_window(self.conn, S9).source, conversation_window(self.conn, S9).items),
                         ("none", []))
        self.conn.execute("UPDATE sessions SET transcript_exists = 0 WHERE id = ?", (S9,))
        self.assertEqual(conversation_window(self.conn, S9).source, "none")

    def test_transcript_items_are_capped(self):
        records = [headless("多い", t(0), S9, CWD)]
        for n in range(150):
            records.append(assistant(t(1 + n), S9, CWD, text="応答%d" % n, entrypoint="sdk-cli",
                                     tools=[("Bash", {"command": "ls"})]))
        self.ingest(S9, records)
        w = conversation_window(self.conn, S9)
        self.assertEqual(len(w.items), 200)
        self.assertEqual((w.items[-1].kind, w.items[-1].text), ("assistant", "応答99"))

    def test_conversation_without_body(self):
        self.add_session(S0, "interactive", None, t(0), t(1), turn=2)
        self.add_session(SE, "empty", None, t(0), t(1))
        for session in (S0, SE):
            w = conversation_window(self.conn, session)
            self.assertEqual((w.source, w.items), ("none", []))


class LongConversationTest(LedgerCase):
    """長い会話を、一致の周りから前後に読み足す。ターンは CONVERSATION_TURNS ずつ。"""

    def setUp(self):
        super().setUp()
        self.fake = FakeClaude(Path(self.d))
        self.ctx = IngestContext(home=HOME, exts=tuple(DEFAULT_DOC_EXTENSIONS))

    def ingest(self, session, records):
        ingest_transcript(self.conn, self.fake.write(CWD, session, records), self.ctx)

    def add_turns(self, session, last, first=1):
        """ターン first〜last に、本人の入力と Claude の応答を1つずつ入れる。"""
        rows = []
        for n in range(first, last + 1):
            rows += [(session, 2 * n, n, t(n), "user", "質問%d" % n),
                     (session, 2 * n + 1, n, t(n), "assistant", "答え%d" % n)]
        self.conn.executemany("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, ?, ?, ?, ?, ?)",
                              rows)
        self.conn.commit()

    def window(self, **kw):
        w = conversation_window(self.conn, S1, **kw)
        users = [i.turn for i in w.items if i.kind == "user"]
        return (users[0], users[-1]) if users else None, w.first_turn, w.last_turn, w.before, w.after

    def test_turn_windows(self):
        self.assertEqual(CONVERSATION_TURNS, 20)
        self.add_session(S1, "interactive", None, t(0), t(60), turn=60)
        self.add_turns(S1, 60)
        # 既定は最後のターンの前後（40〜60）。前に 39 ターン残る
        self.assertEqual(self.window(), ((40, 60), 40, 60, 39, 0))
        # 一致のターンの前後。ターン 0 には発言がないので、前の残りに数えない
        self.assertEqual(self.window(around_turn=5), ((1, 25), 0, 25, 0, 35))
        self.assertEqual(self.window(around_turn=30), ((10, 50), 10, 50, 9, 10))
        # 読み足す分は、ターンの範囲で指定する。範囲は記録のターンの数で止める
        self.assertEqual(self.window(turns=(20, 39)), ((20, 39), 20, 39, 19, 21))
        self.assertEqual(self.window(turns=(0, 19)), ((1, 19), 0, 19, 0, 41))
        self.assertEqual(self.window(turns=(51, 70)), ((51, 60), 51, 60, 50, 0))
        self.assertEqual(self.window(turns=(61, 80)), (None, 61, 60, 60, 0))
        # ターン 0 に発言があれば（最初の入力の前の応答など）、前の残りに数える
        self.conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) "
                          "VALUES (?, 0, 0, ?, 'assistant', '前置き')", (S1, t(0)))
        self.assertEqual(self.window(turns=(1, 20))[3], 1)

    def test_transcript_windows(self):
        # ヘッドレスの記録は、項目（続けて呼んだツールは1つ）を TRANSCRIPT_ITEMS ずつ。1 + 225 × 2 = 451 項目
        records = [headless("多い", t(0), S9, CWD)]
        for n in range(225):
            records.append(assistant(t(1 + n), S9, CWD, text="応答%d" % n, entrypoint="sdk-cli",
                                     tools=[("Bash", {"command": "ls"}), ("Bash", {"command": "pwd"})]))
        self.ingest(S9, records)
        w = conversation_window(self.conn, S9)
        self.assertEqual((len(w.items), w.offset, w.before, w.after), (200, 0, 0, 251))
        self.assertEqual((w.items[0].text, w.items[-1].text), ("多い", "応答99"))
        w = conversation_window(self.conn, S9, item_offset=200)
        self.assertEqual((len(w.items), w.offset, w.before, w.after), (200, 200, 200, 51))
        # 窓の境目のツールも、1つの項目にまとめたまま
        both = ["Bash（ls）", "Bash（pwd）"]
        self.assertEqual((w.items[0].kind, w.items[0].tools, w.items[1].text), ("tools", both, "応答100"))
        w = conversation_window(self.conn, S9, item_offset=400)
        self.assertEqual((len(w.items), w.offset, w.before, w.after), (51, 400, 400, 0))
        self.assertEqual(w.items[-1].tools, both)
        w = conversation_window(self.conn, S9, item_offset=600)
        self.assertEqual((w.items, w.before, w.after), ([], 451, 0))


class ResumeTest(unittest.TestCase):
    def test_is_session_id(self):
        self.assertTrue(is_session_id(S1))
        self.assertTrue(is_session_id("abcdef00-0000-4000-8000-00000000000f"))
        for bad in ("", None, "ABCDEF00-0000-4000-8000-000000000001", S1 + "\n", S1[:-1], "x" + S1,
                    "../" + S1, "00000000-0000-4000-8000-00000000000g"):
            self.assertFalse(is_session_id(bad), bad)

    def test_resume_command_quotes_path(self):
        cmd = resume_command("/Users/u/my dir/it's", S1)
        self.assertEqual(cmd, "cd '/Users/u/my dir/it'\"'\"'s' && claude --resume " + S1)
        self.assertEqual(shlex.split(cmd), ["cd", "/Users/u/my dir/it's", "&&", "claude", "--resume", S1])
        self.assertEqual(resume_command("/w", S1), "cd /w && claude --resume " + S1)
        with self.assertRaises(ValueError):
            resume_command("/w", "x; rm -rf ~")


if __name__ == "__main__":
    unittest.main()
