import ast
import re
import tempfile
import unittest
from pathlib import Path

from tsuzuki import relations
from tsuzuki.relations import infer_relations
from tests.fixtures import make_ledger, sid, t

JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")  # ひらがな・カタカナ・漢字


def _evidence_forms():
    """relations.py が relations.evidence に書く根拠の形。(src, dst, 種類, 確かさ, 根拠) の組の5つ目を、文字は
    そのまま、モジュールの定数は値で、% で埋めるものは埋める前の文字で返す。ほかの書き方は認めない。"""
    tree = ast.parse(Path(relations.__file__).read_text(encoding="utf-8"))
    constants = {node.targets[0].id: node.value.value for node in tree.body
                 if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                 and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}

    def text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return text(node.left)
        raise AssertionError("根拠は文字（% で埋めるものを含む）で書く: " + ast.dump(node))

    return [text(node.elts[4]) for node in ast.walk(tree)
            if isinstance(node, ast.Tuple) and len(node.elts) == 5 and isinstance(node.elts[3], ast.Constant)
            and node.elts[3].value in ("confirmed", "strong", "weak")]


class RelationsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = make_ledger(Path(self._tmp.name))

    def add_session(self, id, kind, place, start, end, forked_from=None, continued_in=None):
        self.conn.execute(
            "INSERT INTO sessions(id, transcript_path, kind, place_id, started_at, ended_at, forked_from, continued_in)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (id, "/x/%s.jsonl" % id, kind, place, start, end, forked_from, continued_in))
        self.conn.commit()

    def add_mention(self, src, dst, ts):
        self.conn.execute("INSERT INTO mentions(session_id, mentioned_id, ts) VALUES (?,?,?)", (src, dst, ts))
        self.conn.commit()

    def add_link(self, session, path, rel, ts, prompt_index=None):
        self.conn.execute("INSERT INTO doc_links(session_id, path, rel, ts, prompt_index) VALUES (?,?,?,?,?)",
                          (session, path, rel, ts, prompt_index))
        self.conn.commit()

    def add_lineage(self, child, parent):
        self.conn.execute("INSERT INTO lineage(session_id, parent_id, ts, source, cwd) VALUES (?,?,?,?,?)",
                          (child, parent, None, "hook", None))
        self.conn.commit()

    def add_message(self, session, ts):
        seq = self.conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session,)).fetchone()[0]
        self.conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?,?,?,?,?,?)",
                          (session, seq, 0, ts, "user", "x"))
        self.conn.commit()

    def add_derived(self, session, source, via):
        self.conn.execute("INSERT INTO derived_from(session_id, source_id, via) VALUES (?,?,?)", (session, source, via))
        self.conn.commit()

    def rel(self, src, dst):
        r = self.conn.execute("SELECT type, confidence FROM relations WHERE src=? AND dst=?", (src, dst)).fetchone()
        return tuple(r) if r else None

    def evidence(self, src, dst):
        r = self.conn.execute("SELECT evidence FROM relations WHERE src=? AND dst=?", (src, dst)).fetchone()
        return r[0] if r else None

    def n_relations(self):
        return self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

    def parent_of(self, child):
        r = self.conn.execute("SELECT src, confidence FROM relations WHERE dst=? AND type='child'",
                              (child,)).fetchone()
        return tuple(r) if r else None

    def test_fork_and_continued(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_session(sid(2), "interactive", 1, t(20), t(30), forked_from=sid(1))
        self.add_session(sid(3), "interactive", 1, t(40), t(50), continued_in=sid(4))
        self.add_session(sid(4), "interactive", 1, t(50), t(60))
        infer_relations(self.conn)
        self.assertEqual(self.rel(sid(1), sid(2)), ("fork", "confirmed"))
        self.assertEqual(self.rel(sid(3), sid(4)), ("continued", "confirmed"))

    def test_child_lineage_beats_mention(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_session(sid(2), "interactive", 1, t(0), t(100))
        self.add_session(sid(9), "headless", 1, t(10), t(20))
        self.add_lineage(sid(9), sid(2))
        self.add_mention(sid(1), sid(9), t(21))
        infer_relations(self.conn)
        self.assertEqual(self.parent_of(sid(9)), (sid(2), "confirmed"))

    def test_lineage_parent_missing_falls_back_to_mention(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_session(sid(9), "headless", 1, t(10), t(20))
        self.add_lineage(sid(9), sid(77))
        self.add_mention(sid(1), sid(9), t(21))
        infer_relations(self.conn)
        self.assertEqual(self.parent_of(sid(9)), (sid(1), "strong"))

    def test_child_by_mention_ignores_later_sessions(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(120))
        self.add_session(sid(5), "interactive", 2, t(300), t(310))
        self.add_session(sid(9), "headless", 1, t(60), t(70))
        self.add_mention(sid(5), sid(9), t(301))
        self.add_mention(sid(1), sid(9), t(71))
        infer_relations(self.conn)
        self.assertEqual(self.parent_of(sid(9)), (sid(1), "strong"))

    def test_child_prefers_containing_period_then_earliest_mention(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(30))
        self.add_session(sid(2), "interactive", 1, t(10), t(100))
        self.add_session(sid(3), "interactive", 1, t(5), t(200))
        self.add_session(sid(9), "headless", 1, t(50), t(60))
        self.add_mention(sid(1), sid(9), t(29))
        self.add_mention(sid(3), sid(9), t(80))
        self.add_mention(sid(2), sid(9), t(61))
        infer_relations(self.conn)
        self.assertEqual(self.parent_of(sid(9)), (sid(2), "strong"))

    def test_weak_child_by_time_window(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_message(sid(1), t(58))
        self.add_session(sid(9), "headless", 1, t(60), t(61))
        infer_relations(self.conn)
        self.assertEqual(self.parent_of(sid(9)), (sid(1), "weak"))
        self.add_session(sid(2), "interactive", 1, t(0), t(100))
        self.add_message(sid(2), t(63))
        infer_relations(self.conn)
        self.assertIsNone(self.parent_of(sid(9)))

    def test_handoff_by_document(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_link(sid(1), "/r/docs/h.md", "created", t(5))
        self.add_session(sid(2), "interactive", 1, t(20), t(30))
        self.add_link(sid(2), "/r/docs/h.md", "named", t(20), prompt_index=1)
        self.add_session(sid(3), "interactive", 1, t(40), t(50))
        self.add_link(sid(3), "/r/docs/h.md", "named", t(45), prompt_index=4)
        self.add_session(sid(4), "interactive", 1, t(60), t(70))
        self.add_link(sid(4), "/elsewhere/h.md", "named", t(60), prompt_index=1)
        self.add_session(sid(5), "interactive", 2, t(60), t(70))
        self.add_link(sid(5), "/other/h.md", "named", t(60), prompt_index=1)
        infer_relations(self.conn)
        self.assertEqual(self.rel(sid(1), sid(2)), ("handoff", "strong"))
        self.assertIsNone(self.rel(sid(1), sid(3)))
        self.assertEqual(self.rel(sid(1), sid(4)), ("handoff", "strong"))
        self.assertIsNone(self.rel(sid(1), sid(5)))

    def test_handoff_ignores_link_written_after_start(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_link(sid(1), "/r/h.md", "created", t(50))
        self.add_session(sid(2), "interactive", 1, t(20), t(30))
        self.add_link(sid(2), "/r/h.md", "named", t(20), prompt_index=1)
        infer_relations(self.conn)
        self.assertIsNone(self.rel(sid(1), sid(2)))

    def test_two_handoff_sources_into_one(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_link(sid(1), "/r/a.md", "created", t(5))
        self.add_session(sid(2), "interactive", 1, t(0), t(10))
        self.add_link(sid(2), "/r/b.md", "edited", t(6))
        self.add_session(sid(3), "interactive", 1, t(20), t(30))
        self.add_link(sid(3), "/r/a.md", "named", t(20), prompt_index=1)
        self.add_link(sid(3), "/r/b.md", "named", t(20), prompt_index=2)
        infer_relations(self.conn)
        self.assertEqual(self.rel(sid(1), sid(3)), ("handoff", "strong"))
        self.assertEqual(self.rel(sid(2), sid(3)), ("handoff", "strong"))

    def test_reference_loses_to_fork(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_session(sid(2), "interactive", 1, t(20), t(30), forked_from=sid(1))
        self.add_mention(sid(2), sid(1), t(25))
        self.add_session(sid(3), "interactive", 1, t(40), t(50))
        self.add_mention(sid(3), sid(1), t(45))
        n = infer_relations(self.conn)
        self.assertEqual(self.rel(sid(1), sid(2)), ("fork", "confirmed"))
        self.assertEqual(self.rel(sid(1), sid(3)), ("reference", "strong"))
        self.assertEqual(n, 2)

    def test_self_mention_ignored(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_session(sid(9), "headless", 1, t(60), t(70))
        self.add_mention(sid(1), sid(1), t(5))
        self.add_mention(sid(9), sid(9), t(65))
        self.assertEqual(infer_relations(self.conn), 0)

    def test_handoff_source_may_be_headless(self):
        self.add_session(sid(1), "headless", 1, t(0), t(10))
        self.add_link(sid(1), "/r/h.md", "created", t(5))
        self.add_session(sid(2), "interactive", 1, t(20), t(30))
        self.add_link(sid(2), "/r/h.md", "named", t(20), prompt_index=1)
        infer_relations(self.conn)
        self.assertEqual(self.rel(sid(1), sid(2)), ("handoff", "strong"))

    def test_fork_beats_reference_in_opposite_direction(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_session(sid(2), "interactive", 1, t(20), t(30), forked_from=sid(1))
        self.add_mention(sid(1), sid(2), t(25))  # reference は (2, 1) の向き
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.rel(sid(1), sid(2)), ("fork", "confirmed"))
        self.assertIsNone(self.rel(sid(2), sid(1)))

    def test_fork_beats_continued(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10), continued_in=sid(2))
        self.add_session(sid(2), "interactive", 1, t(20), t(30), forked_from=sid(1))
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.rel(sid(1), sid(2)), ("fork", "confirmed"))

    def test_continued_beats_reference_and_handoff(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10), continued_in=sid(2))
        self.add_link(sid(1), "/r/h.md", "created", t(5))
        self.add_session(sid(2), "interactive", 1, t(20), t(30))
        self.add_link(sid(2), "/r/h.md", "named", t(20), prompt_index=1)
        self.add_mention(sid(2), sid(1), t(25))
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.rel(sid(1), sid(2)), ("continued", "confirmed"))

    def test_handoff_beats_reference_both_directions(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_link(sid(1), "/r/h.md", "created", t(5))
        self.add_session(sid(2), "interactive", 1, t(20), t(30))
        self.add_link(sid(2), "/r/h.md", "named", t(20), prompt_index=1)
        self.add_mention(sid(2), sid(1), t(25))  # reference (1, 2)
        self.add_mention(sid(1), sid(2), t(26))  # reference (2, 1)
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.rel(sid(1), sid(2)), ("handoff", "strong"))
        self.assertIsNone(self.rel(sid(2), sid(1)))

    def test_child_confidence_order_and_no_reference_overlap(self):
        # 子は headless、reference は対話どうしだけなので、child と reference が同じ組になることはない
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_message(sid(1), t(58))
        self.add_session(sid(9), "headless", 1, t(60), t(61))
        self.add_mention(sid(1), sid(9), t(62))
        self.add_session(sid(2), "interactive", 1, t(0), t(100))
        self.add_lineage(sid(9), sid(2))
        self.add_mention(sid(9), sid(1), t(62))
        infer_relations(self.conn)
        self.assertEqual(self.parent_of(sid(9)), (sid(2), "confirmed"))
        self.assertIsNone(self.rel(sid(1), sid(9)))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 1)

    # 記録の derived-from

    def test_derived_from_via_values(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_session(sid(2), "headless", 1, t(10), t(20))
        self.add_derived(sid(2), sid(1), "subprocess")
        self.add_session(sid(3), "interactive", 1, t(30), t(40))
        self.add_derived(sid(3), sid(1), "branch")
        self.add_session(sid(4), "interactive", 1, t(50), t(60))
        self.add_derived(sid(4), sid(1), "fork")
        self.add_session(sid(5), "interactive", 1, t(70), t(80))
        self.add_derived(sid(5), sid(1), "compact")
        self.add_session(sid(6), "headless", 1, t(90), t(95))
        for via in ("teleport", "clear", "resume", "zzz"):
            self.add_derived(sid(6), sid(1), via)
        self.assertEqual(infer_relations(self.conn), 4)
        self.assertEqual(self.rel(sid(1), sid(2)), ("child", "confirmed"))
        self.assertEqual(self.rel(sid(1), sid(3)), ("fork", "confirmed"))
        self.assertEqual(self.rel(sid(1), sid(4)), ("fork", "confirmed"))
        self.assertEqual(self.rel(sid(1), sid(5)), ("continued", "confirmed"))
        self.assertIsNone(self.rel(sid(1), sid(6)))
        self.assertEqual([self.evidence(sid(1), sid(n)) for n in (2, 3, 4, 5)],
                         ["記録の derived-from（subprocess）", "記録の derived-from（branch）",
                          "記録の derived-from（fork）", "記録の derived-from（compact）"])

    def test_derived_from_and_hook_naming_the_same_parent_give_one_relation(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_session(sid(9), "headless", 1, t(10), t(20))
        self.add_lineage(sid(9), sid(1))
        self.add_derived(sid(9), sid(1), "subprocess")
        self.add_mention(sid(1), sid(9), t(21))
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.parent_of(sid(9)), (sid(1), "confirmed"))

    def test_derived_from_parent_beats_hook_and_heuristics(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_session(sid(2), "interactive", 1, t(0), t(100))
        self.add_message(sid(2), t(9))
        self.add_session(sid(9), "headless", 1, t(10), t(20))
        self.add_lineage(sid(9), sid(2))
        self.add_mention(sid(2), sid(9), t(21))
        self.add_derived(sid(9), sid(1), "subprocess")
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.parent_of(sid(9)), (sid(1), "confirmed"))

    def test_derived_from_same_link_as_forked_from_and_continued_in(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(10))
        self.add_session(sid(2), "interactive", 1, t(20), t(30), forked_from=sid(1))
        self.add_derived(sid(2), sid(1), "fork")
        self.add_derived(sid(2), sid(1), "branch")
        self.add_session(sid(3), "interactive", 1, t(40), t(50), continued_in=sid(4))
        self.add_session(sid(4), "interactive", 1, t(50), t(60))
        self.add_derived(sid(4), sid(3), "compact")
        self.assertEqual(infer_relations(self.conn), 2)
        self.assertEqual((self.rel(sid(1), sid(2)), self.evidence(sid(1), sid(2))),
                         (("fork", "confirmed"), "記録の forkedFrom"))
        self.assertEqual((self.rel(sid(3), sid(4)), self.evidence(sid(3), sid(4))),
                         (("continued", "confirmed"), "記録の continued-in"))

    def test_derived_from_unknown_or_self_falls_back(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_session(sid(9), "headless", 1, t(10), t(20))
        self.add_derived(sid(9), sid(77), "subprocess")  # 台帳にない親
        self.add_derived(sid(9), sid(9), "subprocess")
        self.add_derived(sid(9), sid(9), "fork")
        self.add_derived(sid(9), sid(9), "compact")  # 実データ：/compact したセッションが自分を名指す
        self.add_mention(sid(1), sid(9), t(21))
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.parent_of(sid(9)), (sid(1), "strong"))

    def test_derived_from_child_kinds(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(100))
        self.add_session(sid(2), "interactive", 1, t(10), t(20))  # 子のセッションを後から人が再開したもの
        self.add_derived(sid(2), sid(1), "subprocess")
        self.add_session(sid(3), "empty", 1, t(30), t(31))  # 空のセッションは一覧に出さないので子にもしない
        self.add_derived(sid(3), sid(1), "subprocess")
        self.add_session(sid(4), "headless", 1, t(40), t(50))
        self.add_derived(sid(4), sid(9), "subprocess")
        self.add_session(sid(9), "headless", 1, t(35), t(60))  # ヘッドレスの子のヘッドレス
        self.assertEqual(infer_relations(self.conn), 2)
        self.assertEqual(self.parent_of(sid(2)), (sid(1), "confirmed"))
        self.assertIsNone(self.parent_of(sid(3)))
        self.assertEqual(self.parent_of(sid(4)), (sid(9), "confirmed"))

    def test_derived_from_two_parents_pick_one(self):
        self.add_session(sid(1), "interactive", 1, t(0), t(5))
        self.add_session(sid(2), "interactive", 1, t(0), t(100))
        self.add_session(sid(9), "headless", 1, t(10), t(20))
        self.add_derived(sid(9), sid(1), "subprocess")
        self.add_derived(sid(9), sid(2), "subprocess")
        self.assertEqual(infer_relations(self.conn), 1)
        self.assertEqual(self.parent_of(sid(9)), (sid(2), "confirmed"))  # 子の開始を期間に含む方

    def test_precedence_order(self):
        from tsuzuki.relations import PRECEDENCE
        self.assertEqual(PRECEDENCE[0], ("fork", "confirmed"))
        self.assertLess(PRECEDENCE.index(("continued", "confirmed")), PRECEDENCE.index(("handoff", "strong")))
        self.assertLess(PRECEDENCE.index(("handoff", "strong")), PRECEDENCE.index(("reference", "strong")))

    # 根拠の文言（画面に出す）

    def test_every_evidence_is_framed_in_japanese(self):
        # 根拠は状況カードと場所の時系列にそのまま出るので、記録のフィールド名だけにせず日本語の枠を付ける。
        # relations.py のソースから根拠の形をすべて集めて確かめ（後から足した規則も当たる）、すべての形を通る台帳で、
        # 実際に書かれた根拠も確かめる
        forms = _evidence_forms()
        self.assertEqual(len(forms), 9)  # 根拠を書く所の数（集め方が壊れていないことの確かめ）
        for form in set(forms):
            self.assertRegex(form, JAPANESE)
        s = {n: sid(n) for n in range(1, 20)}
        self.add_session(s[1], "interactive", 1, t(0), t(10))  # 分岐（forkedFrom）
        self.add_session(s[2], "interactive", 1, t(20), t(30), forked_from=s[1])
        self.add_session(s[3], "interactive", 1, t(40), t(50), continued_in=s[4])  # 続きの会話（continued-in）
        self.add_session(s[4], "interactive", 1, t(50), t(60))
        self.add_session(s[5], "interactive", 1, t(100), t(200))  # derived-from（子と分岐）
        self.add_session(s[6], "headless", 1, t(110), t(120))
        self.add_derived(s[6], s[5], "subprocess")
        self.add_session(s[7], "interactive", 1, t(130), t(140))
        self.add_derived(s[7], s[5], "branch")
        self.add_session(s[8], "interactive", 1, t(300), t(400))  # フックの記録
        self.add_session(s[9], "headless", 1, t(310), t(320))
        self.add_lineage(s[9], s[8])
        self.add_session(s[10], "interactive", 1, t(500), t(600))  # 記録に ID（子）
        self.add_session(s[11], "headless", 1, t(510), t(520))
        self.add_mention(s[10], s[11], t(521))
        self.add_session(s[12], "interactive", 2, t(700), t(800))  # 前後5分の対話（子・推定）
        self.add_message(s[12], t(709))
        self.add_session(s[13], "headless", 2, t(710), t(715))
        self.add_session(s[14], "interactive", 1, t(900), t(910))  # 資料で引き継ぎ
        self.add_link(s[14], "/w/handoff.md", "created", t(905))
        self.add_session(s[15], "interactive", 1, t(920), t(930))
        self.add_link(s[15], "/w/handoff.md", "named", t(920), prompt_index=1)
        self.add_session(s[16], "interactive", 1, t(1000), t(1010))  # 参照
        self.add_session(s[17], "interactive", 1, t(1020), t(1030))
        self.add_mention(s[17], s[16], t(1021))
        self.assertEqual(infer_relations(self.conn), 9)
        written = {e for (e,) in self.conn.execute("SELECT evidence FROM relations")}
        for text in written:
            self.assertRegex(text, JAPANESE)
        for form in set(forms):  # 台帳がすべての形を通っている
            pattern = re.escape(form).replace("%s", ".+")
            self.assertTrue(any(re.fullmatch(pattern, text) for text in written), (form, written))


if __name__ == "__main__":
    unittest.main()
