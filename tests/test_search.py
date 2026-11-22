"""検索と tsuzuki search のテスト。台帳の表に合成した行を直接入れて確かめる。"""
import contextlib
import io
import random
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tsuzuki import cli
from tsuzuki import search as search_mod
from tsuzuki.search import SNIPPET_RADIUS, SearchQuery, SearchResult, make_snippet, parse_query, search
from tsuzuki.store import get_meta, has_trigram
from tests.fixtures import make_ledger, run_cli, sid, t

S1, S2, S3, S4, S5, S6, S9 = sid(1), sid(2), sid(3), sid(4), sid(5), sid(6), sid(9)
ALL_TARGETS = frozenset({"conversation", "documents", "history", "headless"})

# FTS5 の式の記号、LIKE の記号、SQLite に渡せない文字、長すぎる語、多すぎる語
ODD_INPUTS = [
    '"', 'a"b', '*', '-x', 'NEAR(', ':', '(', ')', '   ', '',
    '""', '"""', '" "', 'AND', 'OR', 'NOT', 'a OR b', 'NEAR(a b)', '^x', '+', '{', '}', "'", ';', '--',
    '%', '_', '\\', '%_\\', 'text:引き継ぎ', '引き継ぎ*', '-引き継ぎ', '\x00', 'a\x00bcd', '\ud800', '引き\udcff継ぎ',
    '\u3000', '\n\t', 'x' * 20000, '引' * 20000, '%' * 20000, '\U0001F600' * 20000,
    ' '.join(str(i) for i in range(3000)),
]


def _sqlite_has_trigram() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        return has_trigram(conn)
    finally:
        conn.close()


# この SQLite で FTS5 の trigram が使えるか。使えなければ、trigram を前提にするテストは飛ばす
TRIGRAM_AVAILABLE = _sqlite_has_trigram()
NO_TRIGRAM_REASON = "この SQLite では FTS5 の trigram が使えない"


def at(minutes: float) -> float:
    """t(minutes) と同じ時刻の UNIX 時刻。"""
    return datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() + minutes * 60


def local(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M")


def ids(result: SearchResult):
    return [h.session_id for h in result.sessions]


class _Recorder:
    """search に渡す接続の代わり。実行した SQL と引数を覚え、本物の接続に回す。"""

    def __init__(self, conn):
        self.conn, self.seen = conn, []

    def execute(self, sql, params=()):
        self.seen.append((sql, list(params)))
        return self.conn.execute(sql, params)


class SearchTest(unittest.TestCase):
    trigram = True

    def setUp(self):
        if self.trigram and not TRIGRAM_AVAILABLE:
            self.skipTest(NO_TRIGRAM_REASON)
        if not self.trigram:
            for target in ("tsuzuki.store.has_trigram", "tsuzuki.search.has_trigram"):
                patcher = mock.patch(target, return_value=False)
                patcher.start()
                self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.conn = make_ledger(Path(tmp.name))
        self.addCleanup(self.conn.close)
        self.seqs = {}
        self.place(1, "/w/garden", "work/garden")
        self.place(2, "/w/plan", "work/plan")
        self.place(3, "/w/old", "work/old")
        # 対話 S1＝ターン1「料理の引き継ぎ資料を作って」/「作りました」、ターン2「設計を見直して」/「料理の設計を直しました」
        self.session(S1, place=1, first_prompt="料理の引き継ぎ資料を作って")
        self.messages(S1, [(1, "user", "料理の引き継ぎ資料を作って", 100), (1, "assistant", "作りました", 101),
                           (2, "user", "設計を見直して", 102), (2, "assistant", "料理の設計を直しました", 103)])
        # 対話 S2＝ターン1「引き継ぎをお願い」/「はい」
        self.session(S2, place=2, first_prompt="引き継ぎをお願い")
        self.messages(S2, [(1, "user", "引き継ぎをお願い", 10), (1, "assistant", "はい", 11)])
        # 資料 D1、入力履歴 H1（記録のないセッション）、ヘッドレス S9 の digest
        self.document(1, "/w/garden/handoff.md", place=1, title="料理の仕事の引き継ぎ", body="# 料理の仕事の引き継ぎ",
                      minute=100)
        self.history(1, session=sid(7), project="/w/old", place=3, minute=5, text="引き継ぎをお願いします")
        self.session(S9, kind="headless", place=2, ended=50)
        self.digest(S9, "計画の課題1〜7を実装")
        self.conn.commit()

    # --- 台帳に行を入れる補助

    def place(self, place_id, path, display):
        self.conn.execute("INSERT INTO places(id, path, display) VALUES (?, ?, ?)", (place_id, path, display))

    def session(self, session_id, *, kind="interactive", place=None, first_prompt=None, ended=None, exists=1):
        self.conn.execute(
            "INSERT INTO sessions(id, transcript_path, transcript_exists, kind, place_id, ended_at, first_prompt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, "/x/%s.jsonl" % session_id, exists, kind, place,
             t(ended) if ended is not None else None, first_prompt))

    def messages(self, session_id, rows):
        for turn, role, text, minute in rows:
            seq = self.seqs.get(session_id, 0)
            self.seqs[session_id] = seq + 1
            self.conn.execute(
                "INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, seq, turn, t(minute) if minute is not None else None, role, text))

    def document(self, doc_id, path, *, place=None, title=None, body=None, minute=0):
        self.conn.execute(
            "INSERT INTO documents(id, path, place_id, kind, ext, mtime, title, body) "
            "VALUES (?, ?, ?, 'other', '.md', ?, ?, ?)", (doc_id, path, place, at(minute), title, body))

    def history(self, history_id, *, session=None, project=None, place=None, minute=0, text):
        self.conn.execute(
            "INSERT INTO history_prompts(id, session_id, project, place_id, ts, text) VALUES (?, ?, ?, ?, ?, ?)",
            (history_id, session, project, place, t(minute), text))

    def digest(self, session_id, text):
        self.conn.execute("INSERT INTO digests(session_id, text) VALUES (?, ?)", (session_id, text))

    def find(self, text, **kw):
        return search(self.conn, SearchQuery(text, **kw))

    def cli(self, *words):
        self.conn.commit()
        return run_cli(["--ledger-dir", self.dir, "search"] + list(words))

    def cli_exit(self, *args):
        """終了コードと出力を返す（argparse の SystemExit も終了コードとして受け取る）。"""
        self.conn.commit()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--ledger-dir", self.dir] + list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    # --- 例

    def test_two_char_term_and_longer_term(self):
        r = self.find("料理")
        self.assertEqual(ids(r), [S1])
        r = self.find("引き継ぎ")
        self.assertEqual(set(ids(r)), {S1, S2})
        self.assertEqual(len(r.documents), 1)
        self.assertEqual(len(r.history), 1)

    def test_and_is_per_turn(self):
        self.assertEqual(self.find("料理 引き継ぎ").sessions[0].turns, [1])
        self.assertEqual(ids(self.find("引き継ぎ 作りました")), [S1])
        self.assertEqual(self.find("見直して 引き継ぎ").sessions, [])

    def test_phrase_and_role(self):
        self.assertEqual(self.find('"料理の設計"').sessions[0].turns, [2])
        self.assertEqual(self.find('"設計の料理"').sessions, [])
        self.assertEqual(self.find("作りました", role="user").sessions, [])
        self.assertEqual(ids(self.find("作りました", role="assistant")), [S1])

    def test_headless_only_when_targeted(self):
        self.assertEqual(self.find("課題1〜7").headless, [])
        self.assertEqual(len(self.find("課題1〜7", targets=frozenset({"headless"})).headless), 1)

    def test_snippet_marks(self):
        s = self.find("引き継ぎ").sessions[0].snippet
        a, b = s.marks[0]
        self.assertEqual(s.text[a:b], "引き継ぎ")

    def test_order_by_count(self):
        self.messages(S2, [(2, "user", "引き継ぎの続き", 20), (2, "assistant", "了解", 21),
                           (3, "user", "引き継ぎ完了", 30)])
        self.assertEqual(ids(self.find("引き継ぎ")), [S1, S2])
        r = self.find("引き継ぎ", order="count")
        self.assertEqual(ids(r), [S2, S1])
        hit = r.sessions[0]
        self.assertEqual((hit.hit_count, hit.turns, hit.last_ts), (3, [3, 2, 1], t(30)))

    def test_special_input_never_raises(self):
        for text in ODD_INPUTS:
            for q in (SearchQuery(text),
                      SearchQuery(text, targets=ALL_TARGETS, role="assistant", place_ids=[1, 2], days=30)):
                with self.subTest(q=text[:16], targets=sorted(q.targets)):
                    result = search(self.conn, q)
                    self.assertIsInstance(result, SearchResult)

    def test_search_cli(self):
        out = self.cli("引き継ぎ")
        self.assertIn("引き継ぎ", out)
        lines = out.splitlines()
        self.assertLess(lines.index("会話"), lines.index("資料"))
        self.assertLess(lines.index("資料"), lines.index("入力履歴"))
        i = lines.index("料理の引き継ぎ資料を作って  work/garden  %s" % local(t(100)))
        self.assertEqual(lines[i + 1], "  料理の引き継ぎ資料を作って")
        self.assertIn("引き継ぎをお願い  work/plan  %s" % local(t(10)), lines)
        i = lines.index("/w/garden/handoff.md")
        self.assertEqual(lines[i + 1], "  # 料理の仕事の引き継ぎ")
        i = lines.index("work/old  %s" % local(t(5)))
        self.assertEqual(lines[i + 1], "  引き継ぎをお願いします")

    # --- 発言者の絞り込み（assistant は tool も含む）

    def test_role_assistant_includes_tool(self):
        self.session(S3, place=1, first_prompt="手順を書いて")
        self.messages(S3, [(1, "user", "手順を書いて", 200), (1, "tool", "Write docs/設計メモ.md", 201),
                           (1, "assistant", "書きました", 202)])
        self.assertEqual(ids(self.find("設計メモ")), [S3])
        r = self.find("設計メモ", role="assistant")
        self.assertEqual(ids(r), [S3])
        self.assertEqual(r.sessions[0].snippet.role, "tool")
        self.assertEqual(self.find("設計メモ", role="user").sessions, [])
        self.assertEqual(ids(self.find("メモ", role="assistant")), [S3])
        self.assertEqual(ids(self.find("手順", role="user")), [S3])
        self.assertEqual(self.find("手順", role="assistant").sessions, [])

    def test_invalid_options_raise_value_error(self):
        for kw in ({"role": "tool"}, {"order": "old"}, {"targets": frozenset({"conversation", "nope"})}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.find("料理", **kw)

    # --- 語の分け方

    def test_parse_query(self):
        self.assertEqual(parse_query("料理  引き継ぎ"), ["料理", "引き継ぎ"])
        self.assertEqual(parse_query("料理\u3000引き継ぎ\n設計\t"), ["料理", "引き継ぎ", "設計"])
        self.assertEqual(parse_query('"料理の 設計" 資料'), ["料理の 設計", "資料"])
        self.assertEqual(parse_query('資料 "料理の 設計'), ["資料", "料理の 設計"])
        self.assertEqual(parse_query('"  前後の空白  "'), ["前後の空白"])
        self.assertEqual(parse_query('"" " " 語'), ["語"])
        self.assertEqual(parse_query("語 語 語2 語"), ["語", "語2"])
        self.assertEqual(parse_query("API api"), ["API", "api"])
        self.assertEqual(parse_query('a"b'), ["a", "b"])
        self.assertEqual(parse_query('x"y z"w'), ["x", "y z", "w"])
        for empty in ("", "   ", '"', '""'):
            self.assertEqual(parse_query(empty), [])

    def test_unclosed_quote_and_fullwidth_space(self):
        self.assertEqual(set(ids(self.find('"引き継ぎ'))), {S1, S2})
        self.assertEqual(ids(self.find("料理\u3000引き継ぎ")), [S1])

    # --- 語ごとの探し方

    def test_like_wildcards_are_literal(self):
        self.session(S4, place=1, first_prompt="割引")
        self.messages(S4, [(1, "user", "割引50%です", 300), (1, "assistant", "a_b の件", 301),
                           (1, "assistant", "path\\to", 302)])
        self.session(S5, place=1, first_prompt="値段")
        self.messages(S5, [(1, "user", "50ドルです", 310), (1, "assistant", "axb の件", 311)])
        for q in ("%", "_", "\\", "0%", "a_", "a_b", "50%で", "h\\t"):
            with self.subTest(q=q):
                self.assertEqual(ids(self.find(q)), [S4])

    def test_ascii_case_insensitive(self):
        self.session(S4, place=1, first_prompt="GitHub")
        self.messages(S4, [(1, "user", "GitHub の PR を出した", 300)])
        for q in ("github", "GITHUB", "pr", "gitHUB の pr"):
            with self.subTest(q=q):
                self.assertEqual(ids(self.find(q)), [S4])
        s = self.find("github pr").sessions[0].snippet
        self.assertEqual([s.text[a:b] for a, b in s.marks], ["GitHub", "PR"])

    def test_fts_only_for_three_or_more_chars(self):
        seen = []
        self.conn.set_trace_callback(seen.append)
        self.addCleanup(self.conn.set_trace_callback, None)
        self.find("料理", targets=ALL_TARGETS)
        self.assertFalse(any("MATCH" in s for s in seen))
        self.assertTrue(any("LIKE" in s for s in seen))
        del seen[:]
        self.find("引き継ぎ", targets=ALL_TARGETS)
        # 資料のパスは、語の長さによらず LIKE で探す（題名・本文・会話などの LIKE とは別）
        used = (any("MATCH" in s for s in seen), any("LIKE" in s.replace("d.path LIKE", "") for s in seen))
        self.assertEqual(used, (True, False) if self.trigram else (False, True))
        self.assertTrue(any("d.path LIKE" in s for s in seen))
        if not self.trigram:
            self.assertEqual(get_meta(self.conn, "fts"), "none")

    # --- 会話

    def test_conversation_only_interactive(self):
        self.session(S4, kind="empty", place=1)
        self.messages(S4, [(1, "user", "引き継ぎ", 300)])
        self.session(S5, kind="headless", place=1)
        self.messages(S5, [(1, "user", "引き継ぎ", 300)])
        self.assertEqual(set(ids(self.find("引き継ぎ"))), {S1, S2})

    def test_hit_fields_and_turn_order(self):
        hit = {h.session_id: h for h in self.find("引き継ぎ").sessions}[S1]
        self.assertEqual((hit.turns, hit.hit_count, hit.last_ts), ([1], 1, t(100)))
        # ターンは、一致した発言の最も新しい時刻の順（ターンの番号の順ではない）。
        # last_ts は一致した発言の時刻（後の「OK」は一致していない）
        self.session(S4, place=1, first_prompt="確認A")
        self.messages(S4, [(1, "user", "確認A", 400), (1, "assistant", "OK", 405), (2, "user", "確認B", 300),
                           (3, "user", "確認C", 350)])
        hit = self.find("確認").sessions[0]
        self.assertEqual((hit.session_id, hit.turns, hit.hit_count, hit.last_ts), (S4, [1, 3, 2], 3, t(400)))

    def test_turn_order_and_last_ts_use_every_term(self):
        # 語ごとの最も新しい一致の時刻が違うとき、ターンの並びと last_ts は、すべての語の一致から決める。
        # ターン1の最も新しい一致は「手順書」の 330（最後の語「資料」だけなら 300）
        self.session(S4, place=1, first_prompt="手順書と資料")
        self.messages(S4, [(1, "user", "手順書と資料", 300), (1, "assistant", "手順書です", 330),
                           (2, "user", "手順書と資料", 320)])
        hit = self.find("手順書 資料").sessions[0]
        self.assertEqual((hit.session_id, hit.turns, hit.last_ts), (S4, [1, 2], t(330)))

    def test_count_order_breaks_ties_by_last_ts(self):
        self.session(S4, place=1, first_prompt="引き継ぎの新しい話")
        self.messages(S4, [(1, "user", "引き継ぎの新しい話", 500)])
        self.messages(S2, [(2, "user", "引き継ぎの続き", 20)])
        # S2 は2ターン。S4 と S1 は1ターンずつで、新しい S4 が先
        self.assertEqual(ids(self.find("引き継ぎ", order="count")), [S2, S4, S1])

    def test_extreme_days(self):
        def now():
            return at(200)

        for days in (36500, 36501, 10 ** 9, float("inf"), float("nan")):
            with self.subTest(days=days):
                r = search(self.conn, SearchQuery("引き継ぎ", days=days, targets=ALL_TARGETS), now=now)
                self.assertEqual((set(ids(r)), len(r.documents), len(r.history)), ({S1, S2}, 1, 1))
        for days in (-1, -10 ** 9, float("-inf")):  # 0日と同じ（今より後の一致だけ）
            with self.subTest(days=days):
                r = search(self.conn, SearchQuery("引き継ぎ", days=days, targets=ALL_TARGETS), now=now)
                self.assertEqual((r.sessions, r.documents, r.history, r.headless), ([], [], [], []))

    def test_place_filter(self):
        r = self.find("引き継ぎ", place_ids=[2], targets=ALL_TARGETS)
        self.assertEqual((ids(r), r.documents, r.history), ([S2], [], []))
        r = self.find("引き継ぎ", place_ids=[1, 3])
        self.assertEqual(ids(r), [S1])
        self.assertEqual([d.doc_id for d in r.documents], [1])
        self.assertEqual([h.history_id for h in r.history], [1])
        self.assertEqual([h.session_id for h in self.find("課題", place_ids=[2], targets=ALL_TARGETS).headless], [S9])
        self.assertEqual(self.find("課題", place_ids=[1], targets=ALL_TARGETS).headless, [])
        r = self.find("引き継ぎ", place_ids=[], targets=ALL_TARGETS)
        self.assertEqual((r.sessions, r.documents, r.history, r.headless, r.places), ([], [], [], [], []))

    def test_days_filter(self):
        def now():
            return at(100 + 24 * 60)  # S1 の最初の発言のちょうど1日後

        self.document(2, "/w/plan/old.md", place=2, title="古い引き継ぎ", body="", minute=99)
        r = search(self.conn, SearchQuery("引き継ぎ", days=1, targets=ALL_TARGETS), now=now)
        self.assertEqual(ids(r), [S1])
        self.assertEqual([d.doc_id for d in r.documents], [1])
        self.assertEqual(r.history, [])
        self.assertEqual(search(self.conn, SearchQuery("課題", days=1, targets=ALL_TARGETS), now=now).headless, [])
        r = search(self.conn, SearchQuery("引き継ぎ", days=2, targets=ALL_TARGETS), now=now)
        self.assertEqual(set(ids(r)), {S1, S2})
        self.assertEqual([d.doc_id for d in r.documents], [1, 2])
        self.assertEqual(len(r.history), 1)
        self.assertEqual(len(search(self.conn, SearchQuery("課題", days=2, targets=ALL_TARGETS), now=now).headless), 1)

    def test_limit_and_snippets_only_within_limit(self):
        self.messages(S2, [(2, "user", "引き継ぎの続き", 20)])
        self.document(2, "/w/plan/b.md", place=2, title="引き継ぎ2", body="引き継ぎの本文", minute=200)
        self.history(2, minute=6, text="引き継ぎの入力")
        with mock.patch.object(search_mod, "_conversation_snippet",
                               wraps=search_mod._conversation_snippet) as built:
            r = self.find("引き継ぎ", limit=1)
        self.assertEqual(ids(r), [S1])
        self.assertEqual(built.call_count, 1)
        self.assertEqual([d.doc_id for d in r.documents], [2])
        self.assertEqual([h.history_id for h in r.history], [2])
        self.assertEqual(ids(self.find("引き継ぎ", limit=1, order="count")), [S2])
        r = self.find("引き継ぎ", limit=0, targets=ALL_TARGETS)
        self.assertEqual((r.sessions, r.documents, r.history, r.places), ([], [], [], []))

    # --- 抜粋

    def test_snippet_window_and_ellipsis(self):
        self.session(S4, place=1, first_prompt="長文")
        self.messages(S4, [(1, "user", "あ" * 60 + "引き継ぎ" + "い" * 60, 300)])
        s = self.find("引き継ぎ").sessions[0].snippet
        self.assertEqual(s.text, "…" + "あ" * SNIPPET_RADIUS + "引き継ぎ" + "い" * SNIPPET_RADIUS + "…")
        self.assertEqual(s.marks, [(1 + SNIPPET_RADIUS, 5 + SNIPPET_RADIUS)])
        self.assertEqual((s.role, s.ts), ("user", t(300)))

    def test_snippet_collapses_whitespace(self):
        self.session(S4, place=1, first_prompt="改行")
        self.messages(S4, [(1, "user", "前の行\n\n  引き継ぎ\tです", 300)])
        s = self.find("引き継ぎ").sessions[0].snippet
        self.assertEqual(s.text, "前の行 引き継ぎ です")
        self.assertEqual(s.marks, [(4, 8)])
        # 空白の続く句も、まとめた空白のまま印を付ける
        s = self.find('"引き継ぎ\tです"').sessions[0].snippet
        self.assertEqual([s.text[a:b] for a, b in s.marks], ["引き継ぎ です"])

    def test_snippet_prefers_longer_term_at_same_position(self):
        s = self.find("引き継ぎ 引き継ぎ資料").sessions[0].snippet
        self.assertEqual((s.text, s.marks), ("料理の引き継ぎ資料を作って", [(3, 9)]))

    def test_snippet_window_starts_at_first_term_and_marks_all_terms(self):
        self.session(S4, place=1, first_prompt="複数")
        self.messages(S4, [(1, "user", "x" * 50 + "設計の引き継ぎ" + "y" * 20 + "設計" + "z" * 100 + "設計", 300)])
        s = self.find("引き継ぎ 設計").sessions[0].snippet
        self.assertEqual(s.text, "…" + "x" * 45 + "設計の引き継ぎ" + "y" * 20 + "設計" + "z" * 18 + "…")
        self.assertEqual(s.marks, [(46, 48), (49, 53), (73, 75)])

    def test_snippet_marks_do_not_overlap(self):
        self.session(S4, place=1, first_prompt="重なり")
        self.messages(S4, [(1, "user", "引き継ぎ資料と継ぎ資料", 300)])
        s = self.find("引き継ぎ 継ぎ資料").sessions[0].snippet
        self.assertEqual(s.marks, [(0, 4), (7, 11)])

    def test_snippet_message_choice(self):
        self.session(S4, place=1, first_prompt="選び方")
        self.messages(S4, [
            (1, "user", "料理の引き継ぎ", 300),
            (1, "assistant", "料理です", 301),
            (2, "user", "料理について", 310),
            (2, "assistant", "引き継ぎ料理", 311),
            (2, "tool", "Write 料理の引き継ぎ.md", 312),
            (2, "assistant", "料理", 313),
        ])
        # 最も新しい一致ターン（2）の中で、語を最も多く含む発言。同数なら後の発言
        hit = self.find("料理 引き継ぎ").sessions[0]
        self.assertEqual((hit.session_id, hit.turns), (S4, [2, 1]))
        self.assertEqual((hit.snippet.text, hit.snippet.role, hit.snippet.ts), ("Write 料理の引き継ぎ.md", "tool", t(312)))
        # 発言者で絞ると、ターンの判定も抜粋の発言も絞った発言者だけで決まる
        hit = self.find("料理 引き継ぎ", role="user").sessions[0]
        self.assertEqual(hit.turns, [1])
        self.assertEqual((hit.snippet.text, hit.snippet.role, hit.snippet.ts), ("料理の引き継ぎ", "user", t(300)))
        hit = self.find("料理 引き継ぎ", role="assistant").sessions[0]
        self.assertEqual(hit.turns, [2])
        self.assertEqual(hit.snippet.role, "tool")

    def test_snippet_message_honours_role_filter(self):
        # 絞り込まなければ、語の数が同じなので後の発言（assistant）から抜粋する
        self.session(S4, place=1, first_prompt="料理の件")
        self.messages(S4, [(1, "user", "料理の件", 300), (1, "assistant", "料理の件ですね", 301)])
        self.assertEqual(self.find("料理").sessions[0].snippet.role, "assistant")
        hit = self.find("料理", role="user").sessions[0]
        self.assertEqual((hit.session_id, hit.snippet.role, hit.snippet.text), (S4, "user", "料理の件"))

    def test_make_snippet_without_occurrence(self):
        s = make_snippet("ÄBC" + "d" * 200, ["äbc"], role="user", ts=None)
        self.assertEqual((s.text, s.marks), ("ÄBC" + "d" * (2 * SNIPPET_RADIUS - 3) + "…", []))
        self.assertEqual(make_snippet("", ["語"], role="user", ts=None).text, "")
        for terms, marks in (([], []), ([""], []), (["", "継ぎ"], [(2, 4)])):
            s = make_snippet("引き継ぎ", terms, role="user", ts=None)
            self.assertEqual((s.text, s.marks), ("引き継ぎ", marks))

    # --- 資料

    def test_documents(self):
        self.document(2, "/w/plan/design.md", place=2, title="設計の資料", body="本文に引き継ぎの話がある", minute=200)
        self.document(3, "/w/plan/empty.md", place=2, title="引き継ぎの題名だけ", body=None, minute=150)
        r = self.find("設計 引き継ぎ")
        self.assertEqual([d.doc_id for d in r.documents], [2])
        s = r.documents[0].snippet
        self.assertEqual((s.text, s.marks, s.role, s.ts), ("本文に引き継ぎの話がある", [(3, 7)], "document", t(200)))
        r = self.find("引き継ぎ")
        self.assertEqual([d.doc_id for d in r.documents], [2, 3, 1])
        snippets = {d.doc_id: d.snippet.text for d in r.documents}
        self.assertEqual(snippets[3], "引き継ぎの題名だけ")
        self.assertEqual(snippets[1], "# 料理の仕事の引き継ぎ")
        s = self.find("設計").documents[0].snippet
        self.assertEqual((s.text, s.marks), ("設計の資料", [(0, 2)]))

    def test_document_found_by_file_name(self):
        # 資料 layout.html は本文に layout の語がない。notes.md は本文に「画面の layout」がある
        base = "/w/proj"
        self.document(4, base + "/.superpowers/brainstorm/content/layout.html", place=1, title="配置の案",
                      body="画面の配置を決める", minute=300)
        self.document(5, base + "/docs/notes.md", place=1, title="メモ", body="画面の layout を考える", minute=310)
        r = self.find("layout.html")
        self.assertEqual([d.doc_id for d in r.documents], [4])
        hit = r.documents[0]
        self.assertTrue(hit.path_only)
        self.assertIn("layout.html", hit.snippet.text)
        self.assertEqual([hit.snippet.text[a:b] for a, b in hit.snippet.marks], ["layout.html"])
        # パスと本文で別々の語に当たる。本文にも語があれば、抜粋は本文から
        r = self.find("brainstorm 画面")
        self.assertEqual([d.doc_id for d in r.documents], [4])
        self.assertFalse(r.documents[0].path_only)
        self.assertEqual(r.documents[0].snippet.text, "画面の配置を決める")
        # フォルダー名にも、2文字の語にも当たる
        self.assertEqual([d.doc_id for d in self.find("superpowers").documents], [4])
        self.assertEqual([d.doc_id for d in self.find("br").documents], [4])
        # 本文に語があれば本文の抜粋、パスにだけあれば path_only（新しい順）
        r = self.find("layout")
        self.assertEqual([(d.doc_id, d.path_only) for d in r.documents], [(5, False), (4, True)])
        self.assertEqual(r.documents[0].snippet.text, "画面の layout を考える")
        # % と _ は文字として探す
        self.document(6, base + "/a_b%c.md", place=1, title="x", body="y", minute=320)
        self.assertEqual([d.doc_id for d in self.find("b%c").documents], [6])
        self.assertEqual([d.doc_id for d in self.find("a_b").documents], [6])
        self.assertEqual(self.find("a_c").documents, [])
        # 会話・入力履歴の検索は変わらない（パスは見ない）
        self.assertEqual(self.find("layout.html").history, [])

    # --- 入力履歴

    def test_history_only_without_transcript(self):
        self.history(2, project="/w/x", minute=6, text="引き継ぎのメモ")
        self.history(3, session=S1, project="/w/garden", place=1, minute=7, text="引き継ぎ資料")
        self.session(S4, place=1, exists=0, first_prompt="消えた")
        self.history(4, session=S4, project="/w/garden", place=1, minute=8, text="引き継ぎの続き")
        self.history(5, minute=9, text="引き継ぎ5")
        r = self.find("引き継ぎ")
        self.assertEqual([h.history_id for h in r.history], [5, 4, 2, 1])
        h = r.history[1]
        self.assertEqual((h.session_id, h.project, h.ts, h.place_id), (S4, "/w/garden", t(8), 1))
        self.assertEqual((h.snippet.text, h.snippet.role, h.snippet.ts), ("引き継ぎの続き", "user", t(8)))
        self.assertEqual((r.history[0].session_id, r.history[0].project, r.history[0].place_id), (None, "", None))
        self.assertEqual([h.history_id for h in self.find("引き継ぎ のメモ").history], [2])

    def test_history_is_not_repeated_while_the_conversation_is_in_the_ledger(self):
        # 記録が消えても会話の本文が台帳に残っていれば（retention: all）、入力は会話として出し、入力履歴には出さない
        self.conn.execute("UPDATE sessions SET transcript_exists = 0 WHERE id = ?", (S2,))
        self.history(2, session=S2, project="/w/plan", place=2, minute=10, text="引き継ぎをお願い")
        r = self.find("引き継ぎ")
        self.assertIn(S2, ids(r))
        self.assertEqual([h.history_id for h in r.history], [1])
        # 本文がもうなければ（retention: card）、入力履歴に出す
        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (S2,))
        r = self.find("引き継ぎ")
        self.assertNotIn(S2, ids(r))
        self.assertEqual([h.history_id for h in r.history], [2, 1])
        # 記録が残っているセッション（本文のないヘッドレスも）の入力は、これまでどおり出さない
        self.history(3, session=S9, project="/w/plan", place=2, minute=11, text="引き継ぎの課題")
        self.assertEqual([h.history_id for h in self.find("引き継ぎ").history], [2, 1])

    def test_short_terms_read_documents_and_history_newest_first_by_index(self):
        # 1〜2文字の語は本文を LIKE で走査する。資料と入力履歴は時刻の索引で新しい順に読み、上限の件数が
        # そろえば止まる（並べ替えのために、当たる行をすべて集めない）
        for q in (SearchQuery("料理", targets=ALL_TARGETS),
                  SearchQuery("料理", targets=ALL_TARGETS, days=30, place_ids=[1, 2])):
            recorder = _Recorder(self.conn)
            search(recorder, q)
            plans = {}
            for sql, params in recorder.seen:
                for table, index in (("FROM documents d ", "documents_mtime"),
                                     ("FROM history_prompts h ", "history_prompts_ts")):
                    if table in sql:
                        plans[index] = " / ".join(r[3] for r in self.conn.execute("EXPLAIN QUERY PLAN " + sql,
                                                                                  params))
            with self.subTest(days=q.days, places=q.place_ids):
                self.assertEqual(sorted(plans), ["documents_mtime", "history_prompts_ts"])
                for index, plan in plans.items():
                    self.assertIn("USING INDEX %s" % index, plan)
                    self.assertNotIn("TEMP B-TREE", plan)

    # --- ヘッドレス

    def test_headless_hits(self):
        self.session(S5, place=2, ended=60, first_prompt="課題")
        self.digest(S5, "課題の残り")  # ヘッドレスでないセッションの行は出さない
        self.digest(sid(8), "課題の行方")  # セッションの行がない
        self.session(S6, kind="headless", place=1, ended=70)
        self.digest(S6, "別の課題")
        r = self.find("課題", targets=frozenset({"headless"}))
        self.assertEqual(r.sessions, [])
        self.assertEqual([h.session_id for h in r.headless], [S6, S9])
        h = r.headless[1]
        self.assertEqual((h.turns, h.hit_count, h.last_ts), ([], 1, t(50)))
        self.assertEqual((h.snippet.text, h.snippet.marks, h.snippet.ts), ("計画の課題1〜7を実装", [(3, 5)], t(50)))
        self.assertEqual([h.session_id for h in self.find("課題 計画", targets=ALL_TARGETS).headless], [S9])

    # --- 場所ごとの数と時間

    def test_places_counts(self):
        self.history(2, minute=6, text="引き継ぎのメモ")  # 場所なしは数えない
        r = self.find("引き継ぎ", targets=ALL_TARGETS)
        self.assertEqual(r.places, [(1, 2), (2, 1), (3, 1)])
        self.assertEqual(self.find("課題", targets=ALL_TARGETS).places, [(2, 1)])
        self.assertEqual(self.find("").places, [])

    def test_elapsed_ms_uses_perf_counter(self):
        with mock.patch.object(search_mod.time, "perf_counter", side_effect=[10.0, 10.25]):
            r = self.find("料理")
        self.assertAlmostEqual(r.elapsed_ms, 250.0)
        r = self.find("")
        self.assertIsInstance(r.elapsed_ms, float)
        self.assertEqual((r.sessions, r.headless, r.documents, r.history), ([], [], [], []))

    # --- tsuzuki search

    def test_search_cli_sections(self):
        self.assertEqual(self.cli("存在しない語").strip(), "一致するものはありませんでした")
        lines = self.cli("作りました").splitlines()
        self.assertIn("会話", lines)
        self.assertNotIn("資料", lines)
        self.assertNotIn("入力履歴", lines)
        lines = self.cli("料理", "引き継ぎ").splitlines()  # 複数の語は空白でつないで1つの検索にする
        self.assertEqual([x for x in lines if x.startswith("料理の")], ["料理の引き継ぎ資料を作って  work/garden  %s"
                                                                       % local(t(100))])
        self.assertNotIn("入力履歴", lines)

    def test_search_cli_period_speaker_and_order(self):
        # 絞り込みと並び順を、画面と同じくコマンドでも使える（オプションは語の前に置く）
        def sessions(*args):
            return [x for x in self.cli(*args).splitlines() if "  work/" in x]
        s1, s2 = "料理の引き継ぎ資料を作って  work/garden  %s" % local(t(100)), "引き継ぎをお願い  work/plan  %s" % local(t(10))
        self.assertEqual(sessions("引き継ぎ"), [s1, s2])
        # 発言者は会話だけに効く（資料と入力履歴は絞らない）。時刻は一致した応答のもの
        self.assertEqual(sessions("--role", "assistant", "作りました"),
                         ["料理の引き継ぎ資料を作って  work/garden  %s" % local(t(101))])
        out = self.cli("--role", "user", "作りました")
        self.assertEqual(out.strip(), "一致するものはありませんでした")
        lines = self.cli("--role", "assistant", "引き継ぎ").splitlines()
        self.assertNotIn("会話", lines)
        self.assertIn("資料", lines)
        self.assertIn("入力履歴", lines)
        # 一致したターンの多い順
        self.messages(S2, [(2, "user", "引き継ぎの続き", 20), (3, "user", "引き継ぎ完了", 30)])
        self.assertEqual(sessions("--order", "count", "引き継ぎ"), ["引き継ぎをお願い  work/plan  %s" % local(t(30)), s1])
        self.assertEqual(sessions("--order", "recent", "引き継ぎ")[0], s1)
        # 期間：合成した記録は 2026-11-01 ごろ。今に近い発言だけが7日以内に入る
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.session(S4, place=1, first_prompt="最近の引き継ぎ")
        self.conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, 0, 1, ?, 'user', ?)",
                          (S4, recent, "最近の引き継ぎ"))
        lines = self.cli("--days", "7", "--order", "count", "引き継ぎ").splitlines()
        self.assertEqual(lines[0], "会話")  # 資料と入力履歴も期間で絞る（どれも古い）
        self.assertEqual(lines[1].split("  ")[:2], ["最近の引き継ぎ", "work/garden"])
        self.assertEqual(len([x for x in lines if "  work/" in x]), 1)
        # 「-」で始まる語も、オプションの後なら語として探す
        self.conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) "
                          "VALUES (?, 1, 1, ?, 'assistant', '-引き継ぎ の件')", (S4, recent))
        self.assertIn("  -引き継ぎ の件", self.cli("--days", "7", "-引き継ぎ").splitlines())
        # 誤った値は、検索せずに理由を出して終わる（終了コード 2）
        for args, reason in ((("--role", "bot", "語"), "--role"), (("--order", "old", "語"), "--order"),
                             (("--days", "0", "語"), "1以上の整数"), (("--days", "-3", "語"), "1以上の整数"),
                             (("--days", "x", "語"), "1以上の整数")):
            with self.subTest(args=args):
                code, _, err = self.cli_exit("search", *args)
                self.assertEqual(code, 2)
                self.assertIn(reason, err)

    def test_search_cli_says_when_sections_were_cut(self):
        # 上限まで返った種類は、見出しにそう書く（黙って切らない）
        with mock.patch.object(search_mod, "RESULT_LIMIT", 1):
            lines = self.cli("引き継ぎ").splitlines()
        self.assertEqual([x for x in lines if x.startswith(("会話", "資料", "入力履歴"))],
                         ["会話（新しい順に1件まで）", "資料（新しい順に1件まで）", "入力履歴（新しい順に1件まで）"])
        lines = self.cli("引き継ぎ").splitlines()
        self.assertEqual([x for x in lines if x.startswith(("会話", "資料", "入力履歴"))], ["会話", "資料", "入力履歴"])

    def test_search_cli_titles_and_fallbacks(self):
        self.conn.execute("UPDATE sessions SET ai_title = ? WHERE id = ?", ("引き継ぎの依頼", S2))
        self.session(S4, first_prompt="一行目\n二行目の引き継ぎ")
        self.messages(S4, [(1, "user", "一行目\n二行目の引き継ぎ", 300)])
        self.history(2, project="/w/x", minute=6, text="引き継ぎのメモ")
        self.history(3, minute=7, text="引き継ぎの話")
        lines = self.cli("引き継ぎ").splitlines()
        self.assertIn("引き継ぎの依頼  work/plan  %s" % local(t(10)), lines)
        i = lines.index("一行目 二行目の引き継ぎ  %s" % local(t(300)))
        self.assertEqual(lines[i + 1], "  一行目 二行目の引き継ぎ")
        self.assertIn("/w/x  %s" % local(t(6)), lines)
        i = lines.index(local(t(7)))
        self.assertEqual(lines[i + 1], "  引き継ぎの話")

    def test_search_cli_argument_with_space_is_one_phrase(self):
        self.session(S4, place=1, first_prompt="料理 の話")
        self.messages(S4, [(1, "user", "料理 の話", 300)])
        expected = ["料理 の話  work/garden  %s" % local(t(300))]
        # 「料理」と「の」の2語なら S1 にも当たる。空白を含む引数は、その並びのまま1語として探す
        for words in (["料理 の"], ['"料理 の"'], ['"料理 の" 話']):
            with self.subTest(words=words):
                lines = self.cli(*words).splitlines()
                self.assertEqual([x for x in lines if x.startswith("料理")], expected)

    def test_search_cli_words_starting_with_dash(self):
        self.session(S4, place=1, first_prompt="メモ")
        self.messages(S4, [(1, "user", "メモ -引き継ぎ の件", 300)])
        self.session(S5, place=2, first_prompt="設定")
        self.messages(S5, [(1, "user", "設定 --full の件", 310)])
        # 「--」はオプションの終わりの印で、語にはしない（S4 の本文に「--」はない）
        for words in (["-引き継ぎ"], ["メモ", "-引き継ぎ"], ["--", "-引き継ぎ"]):
            with self.subTest(words=words):
                lines = self.cli(*words).splitlines()
                self.assertEqual(lines, ["会話", "メモ  work/garden  %s" % local(t(300)), "  メモ -引き継ぎ の件"])
        self.assertEqual(self.cli("--full", "の件").splitlines(),
                         ["会話", "設定  work/plan  %s" % local(t(310)), "  設定 --full の件"])
        code, out, _ = self.cli_exit("search", "-h")
        self.assertEqual(code, 0)
        self.assertIn("語", out)
        self.assertEqual(self.cli("--he").strip(), "一致するものはありませんでした")  # --help の略とはみなさない
        code, _, err = self.cli_exit("search")
        self.assertEqual(code, 2)
        self.assertIn("探す語を指定してください", err)
        # search の前の知らないオプションや、ほかのサブコマンドの知らないオプションは、これまでどおり誤り
        self.assertEqual(self.cli_exit("--bogus", "search", "メモ")[0], 2)
        self.assertEqual(self.cli_exit("forget", "--bogus", sid(1))[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0], 0)


class SearchWithoutTrigramTest(SearchTest):
    """trigram が使えない SQLite（全文検索の表がない台帳）でも、すべての語を LIKE で探して同じ結果になる。"""
    trigram = False


class DocumentSpeedTest(unittest.TestCase):
    """本文が上限（512KiB）近い資料が100件当たっても、1秒未満で返る（抜粋のために本文全体を写して変換しない）。

    全文検索の索引を作ると準備だけで数秒かかるので、索引のない台帳で測る。すべての語を LIKE で
    本文全体から探す、遅い方の道である（抜粋の作り方は trigram のときと同じ）。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        with mock.patch("tsuzuki.store.has_trigram", return_value=False):
            cls.conn = make_ledger(Path(cls._tmp.name))
        # 英字やコードの混じった日本語の Markdown。語は末尾にだけある
        line = "料理の設計を確認した。`tsuzuki search --days 30` で API の結果を見る。\n"
        tail = "最後に引き継ぎ資料と Handoff の話"
        body = line * ((512 * 1024 - len(tail.encode())) // len(line.encode())) + tail
        cls.conn.executemany(
            "INSERT INTO documents(id, path, place_id, kind, ext, mtime, title, body) "
            "VALUES (?, ?, 1, 'other', '.md', ?, ?, ?)",
            [(n, "/w/doc%d.md" % n, at(n), "資料%d" % n, body) for n in range(1, 101)])
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()

    def test_long_bodies_are_fast(self):
        size = self.conn.execute("SELECT LENGTH(CAST(body AS BLOB)) FROM documents WHERE id = 1").fetchone()[0]
        self.assertGreater(size, 500 * 1000)
        with mock.patch("tsuzuki.search.has_trigram", return_value=False):
            for text, term in (("引き継ぎ資料", "引き継ぎ資料"), ("handoff", "Handoff")):
                with self.subTest(text=text):
                    started = time.perf_counter()
                    result = search(self.conn, SearchQuery(text))
                    seconds = time.perf_counter() - started
                    self.assertEqual(len(result.documents), 100)
                    s = result.documents[0].snippet
                    self.assertEqual([s.text[a:b] for a, b in s.marks], [term])
                    self.assertLess(seconds, 1.0)


class SearchSpeedTest(unittest.TestCase):
    """合成した 200 セッション・2万発言で、2文字の語と3文字以上の語の検索が1秒未満で返る。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.conn = make_ledger(Path(cls._tmp.name))
        rnd = random.Random(12)
        words = ["料理", "設計", "引き継ぎ資料", "画面", "検索", "の", "を", "に", "した。", "確認", "実装", "計画",
                 "テスト", "API", "commit"]
        sessions, rows = [], []
        for n in range(200):
            session_id = sid(1000 + n)
            sessions.append((session_id, "/x/%s.jsonl" % session_id, "interactive", 1 + n % 5))
            for i in range(100):
                text = "".join(rnd.choice(words) for _ in range(30))
                rows.append((session_id, i, 1 + i // 2, t(n * 200 + i), "user" if i % 2 == 0 else "assistant", text))
        cls.conn.executemany("INSERT INTO sessions(id, transcript_path, kind, place_id) VALUES (?, ?, ?, ?)", sessions)
        cls.conn.executemany("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, ?, ?, ?, ?, ?)",
                             rows)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()

    def test_two_char_and_longer_terms_are_fast(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 20000)
        for trigram in (True, False) if TRIGRAM_AVAILABLE else (False,):
            for text in ("料理", "引き継ぎ資料", "料理 引き継ぎ資料"):
                with self.subTest(trigram=trigram, text=text), \
                        mock.patch("tsuzuki.search.has_trigram", return_value=trigram):
                    started = time.perf_counter()
                    result = search(self.conn, SearchQuery(text))
                    seconds = time.perf_counter() - started
                    self.assertEqual(len(result.sessions), 100)
                    self.assertLess(seconds, 1.0)


if __name__ == "__main__":
    unittest.main()
