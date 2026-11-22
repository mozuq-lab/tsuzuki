import os
import tempfile
import unittest
from pathlib import Path

from tsuzuki.config import DEFAULT_DOC_EXTENSIONS
from tsuzuki.ingest import IngestContext, UUID_RE, choose_title, find_doc_paths, ingest_transcript
from tsuzuki.store import get_meta
from tests.fixtures import (
    FakeClaude, api_request, assistant, command_text, continued_in, count, derived_from, local_command, local_output,
    allow_system_tmp, make_ledger, pasted, pr_link, recap, relocated, sid, skill_command, skill_tool_use, system_local_command, t, title,
    tool_result, user,
)

HOME = "/Users/u"


class IngestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake = FakeClaude(Path(self._tmp.name))
        self.conn = make_ledger(Path(self._tmp.name))
        self.addCleanup(self.conn.close)
        self.ctx = IngestContext(home=HOME, exts=tuple(DEFAULT_DOC_EXTENSIONS))

    def kind_of(self, session):
        return self.conn.execute("SELECT kind FROM sessions WHERE id = ?", (session,)).fetchone()[0]

    def count(self, table):
        return count(self.conn, table)

    def session(self, session):
        return self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session,)).fetchone()

    def stats(self):
        return {(x[0], x[1]): x[2] for x in self.conn.execute("SELECT version, record_type, count FROM format_stats")}


    def test_interactive_card_fields(self):
        conn, fake, ctx = self.conn, self.fake, self.ctx
        cwd = "/w/repo"
        p = fake.write(cwd, sid(1), [
            user("最初の依頼です", t(0), sid(1), cwd),
            assistant(t(1), sid(1), cwd, text="了解しました",
                      tools=[("Write", {"file_path": cwd + "/docs/a.md"}), ("Agent", {"description": "調査"})]),
            title("ai", "自動の題名", sid(1)),
            recap("作業中です。次は確認です。 (disable recaps in /config)", t(2), sid(1), cwd),
            user("続きをお願いします", t(3), sid(1), cwd),
            pr_link(sid(1), "https://github.com/o/r/pull/4", t(4)),
        ])
        r = ingest_transcript(conn, p, ctx)
        s = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid(1),)).fetchone()
        self.assertEqual((s["kind"], s["n_prompts"], s["n_agents"]), ("interactive", 2, 1))
        self.assertEqual((s["first_prompt"], s["last_prompt"]), ("最初の依頼です", "続きをお願いします"))
        self.assertEqual((s["recap"], s["recap_at"]), ("作業中です。次は確認です。", t(2)))
        self.assertEqual((s["started_at"], s["ended_at"]), (t(0), t(4)))
        self.assertEqual(choose_title(s["custom_title"], s["agent_name"], s["ai_title"], s["first_prompt"]), "自動の題名")
        rows = conn.execute("SELECT turn, role, text FROM messages WHERE session_id = ? ORDER BY seq", (sid(1),)).fetchall()
        self.assertEqual([tuple(x) for x in rows][:4], [(1, "user", "最初の依頼です"), (1, "assistant", "了解しました"),
                                                       (1, "tool", "Write docs/a.md"), (1, "tool", "Agent（調査）")])
        self.assertIn((cwd + "/docs/a.md", "created"), {(x["path"], x["rel"]) for x in conn.execute("SELECT * FROM doc_links")})
        self.assertIn(cwd + "/docs/a.md", r.touched_docs)
        self.assertEqual([x[0] for x in conn.execute("SELECT url FROM session_prs")], ["https://github.com/o/r/pull/4"])

    def test_titles(self):
        self.assertEqual(choose_title("最初の依頼の文 (Branch)", None, "分岐の題名", "x"), "分岐の題名")
        self.assertEqual(choose_title("最初の依頼の文 (Branch)", None, None, "x"), "最初の依頼の文")
        self.assertEqual(choose_title("手で付けた題名", "名前", "自動", "x"), "手で付けた題名")
        self.assertEqual(choose_title(None, None, None, "あ" * 80), "あ" * 50)
        # 「(Branch)」「(Branch N)」で終わる題名は自動の題名：agent_name・ai_title のどちらにも譲る
        self.assertEqual(choose_title("最初の依頼の文 (Branch)", "名前", None, "x"), "名前")
        self.assertEqual(choose_title("最初の依頼の文 (Branch 2)", None, None, "x"), "最初の依頼の文")
        self.assertEqual(choose_title(None, "名前", "自動", "x"), "名前")

    def test_branch_title_in_agent_name(self):
        # /branch は同じ「…(Branch)」を custom-title と agent-name の両方に書く
        b = "最初の依頼の文 (Branch)"
        self.assertEqual(choose_title(b, b, "機能の提案", "x"), "機能の提案")
        self.assertEqual(choose_title(b, b, None, "x"), "最初の依頼の文")
        self.assertEqual(choose_title(None, b, "自動", "x"), "自動")
        self.assertEqual(choose_title(None, "名前 (Branch 3)", None, "x"), "名前")
        self.assertEqual(choose_title("手で付けた題名", b, "自動", "x"), "手で付けた題名")
        self.assertEqual(choose_title(b, "名前", "自動", "x"), "名前")
        self.assertEqual(choose_title(" (Branch)", " (Branch)", None, "最初の依頼"), "最初の依頼")

    def test_headless_keeps_digest_not_messages(self):
        conn, fake, ctx, kind_of, count = self.conn, self.fake, self.ctx, self.kind_of, self.count
        p = fake.write("/w", sid(2), [user("計画の課題1〜7を実装", t(0), sid(2), "/w", human=False, entrypoint="sdk-cli"),
                                      assistant(t(1), sid(2), "/w", text="DONE: 完了")])
        ingest_transcript(conn, p, ctx)
        self.assertEqual(kind_of(sid(2)), "headless")
        self.assertEqual(count("messages"), 0)
        d = conn.execute("SELECT first_prompt, last_reply FROM digests").fetchone()
        self.assertEqual(tuple(d), ("計画の課題1〜7を実装", "DONE: 完了"))

    def test_empty_session(self):
        conn, fake, ctx, kind_of = self.conn, self.fake, self.ctx, self.kind_of
        p = fake.write("/w", sid(3), [assistant(t(0), sid(3), "/w", text="x")])
        ingest_transcript(conn, p, ctx)
        self.assertEqual(kind_of(sid(3)), "empty")

    def test_incremental_append_then_rewrite(self):
        conn, fake, ctx, count = self.conn, self.fake, self.ctx, self.count
        p = fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        ingest_transcript(conn, p, ctx)
        self.assertIsNone(ingest_transcript(conn, p, ctx))
        fake.append("/w", sid(1), [user("b", t(1), sid(1), "/w")])
        r = ingest_transcript(conn, p, ctx)
        self.assertFalse(r.reset)
        self.assertEqual(count("messages"), 2)
        fake.write("/w", sid(1), [user("c", t(2), sid(1), "/w")])
        self.assertTrue(ingest_transcript(conn, p, ctx).reset)
        self.assertEqual([x[0] for x in conn.execute("SELECT text FROM messages")], ["c"])

    # 作り直された記録（縮んだ、または inode が変わった）は、前の内容を何も残さずに頭から読む

    def ingest_old_content(self):
        p = self.fake.write("/w", sid(1), [
            user("a docs/old.md を読んで " + sid(9), t(0), sid(1), "/w"),
            assistant(t(1), sid(1), "/w", text="x"),
            user("b", t(2), sid(1), "/w"),
            pr_link(sid(1), "https://github.com/o/r/pull/1", t(3)),
        ])
        ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((self.count("doc_links"), self.count("mentions"), self.count("session_prs")), (1, 1, 1))
        return p

    def assert_only_new_content(self):
        s = self.session(sid(1))
        self.assertEqual((s["n_prompts"], s["first_prompt"], s["turn"], s["next_seq"], s["n_records"]), (1, "c", 1, 1, 1))
        self.assertEqual([tuple(x) for x in self.conn.execute("SELECT seq, turn, role, text FROM messages")],
                         [(0, 1, "user", "c")])
        self.assertEqual((self.count("doc_links"), self.count("mentions"), self.count("session_prs")), (0, 0, 0))

    def test_shrunk_file_is_read_from_scratch(self):
        p = self.ingest_old_content()
        self.fake.write("/w", sid(1), [user("c", t(4), sid(1), "/w")])
        self.assertTrue(ingest_transcript(self.conn, p, self.ctx).reset)
        self.assert_only_new_content()

    def test_replaced_file_is_read_from_scratch(self):
        p = self.ingest_old_content()
        old = p.stat()
        # 一時ファイルに書いてから置き換える。大きさは前以上にして、inode が変わったことだけで読み直させる
        tmp = self.fake.write("/w", "rewrite-tmp", [user("c", t(4), sid(1), "/w"), api_request(sid(1), size=old.st_size)])
        os.replace(tmp, p)
        self.assertGreaterEqual(p.stat().st_size, old.st_size)
        self.assertNotEqual(p.stat().st_ino, old.st_ino)
        self.assertTrue(ingest_transcript(self.conn, p, self.ctx).reset)
        self.assert_only_new_content()

    def test_headless_flips_to_interactive(self):
        conn, fake, ctx, kind_of = self.conn, self.fake, self.ctx, self.kind_of
        p = fake.write("/w", sid(2), [user("課題", t(0), sid(2), "/w", human=False, entrypoint="sdk-cli"),
                                      assistant(t(1), sid(2), "/w", text="途中の報告", entrypoint="sdk-cli")])
        ingest_transcript(conn, p, ctx)
        fake.append("/w", sid(2), [user("人が続きを頼む", t(2), sid(2), "/w", entrypoint="sdk-cli")])
        ingest_transcript(conn, p, ctx)
        self.assertEqual(kind_of(sid(2)), "interactive")
        self.assertIn("途中の報告", [x[0] for x in conn.execute("SELECT text FROM messages")])

    def test_named_docs_and_mentions(self):
        conn, fake, ctx = self.conn, self.fake, self.ctx
        p = fake.write("/w", sid(1), [user("docs/superpowers/handoff/h.md を読んで " + sid(9) + " " + sid(1), t(0), sid(1), "/w")])
        ingest_transcript(conn, p, ctx)
        self.assertEqual(tuple(conn.execute("SELECT path, rel, prompt_index FROM doc_links").fetchone()),
                         ("/w/docs/superpowers/handoff/h.md", "named", 1))
        self.assertEqual([x[0] for x in conn.execute("SELECT mentioned_id FROM mentions")], [sid(9)])

    def test_find_doc_paths(self):
        self.assertEqual(find_doc_paths("@HANDOFF.md を読んで続きを", "/w", "/Users/u", DEFAULT_DOC_EXTENSIONS), ["/w/HANDOFF.md"])
        self.assertEqual(find_doc_paths("~/notes/a.md と /x/b.html", "/w", "/Users/u", DEFAULT_DOC_EXTENSIONS),
                         ["/Users/u/notes/a.md", "/x/b.html"])

    def test_tombstoned_is_skipped(self):
        conn, fake, ctx = self.conn, self.fake, self.ctx
        conn.execute("INSERT INTO tombstones VALUES (?, 'forget', ?)", (sid(1), t(0)))
        self.assertIsNone(ingest_transcript(conn, fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")]), ctx))

    def test_huge_line_does_not_stop_ingest(self):
        conn, fake, ctx = self.conn, self.fake, self.ctx
        p = fake.write("/w", sid(1), [user("前", t(0), sid(1), "/w")])
        with open(p, "a") as f: f.write('{"type":"user","x":"' + "a" * 9_000_000 + '"}\n')
        fake.append("/w", sid(1), [user("後", t(1), sid(1), "/w")])
        ingest_transcript(conn, p, ctx)
        self.assertEqual(conn.execute("SELECT n_prompts FROM sessions").fetchone()[0], 2)

    # 記録の数え方（判断1）

    def test_record_counters_and_format_stats(self):
        p = self.fake.write("/w", sid(1), [
            user("a", t(0), sid(1), "/w"),
            api_request(sid(1)),
            {"type": "permission-mode", "sessionId": sid(1), "permissionMode": "default"},
            {"type": "brand-new-type", "sessionId": sid(1), "version": "9.9.9"},
        ])
        with open(p, "a") as f:
            f.write("{not json\n")
        r = ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((r.new_records, r.unknown_records), (3, 1))
        self.assertEqual(tuple(self.conn.execute("SELECT n_records, n_recognized FROM sessions").fetchone()), (3, 1))
        self.assertEqual(self.stats(), {("2.1.330", "user"): 1, ("?", "permission-mode"): 1,
                                        ("9.9.9", "brand-new-type"): 1})
        self.assertEqual(get_meta(self.conn, "unreadable_lines"), "1")
        # 追記した分だけを足す（dict でない行も読めない行に数える）
        self.fake.append("/w", sid(1), [user("b", t(1), sid(1), "/w"), recap("要約", t(2), sid(1), "/w")])
        with open(p, "a") as f:
            f.write("[1]\n")
        r = ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((r.new_records, r.unknown_records), (2, 0))
        self.assertEqual(tuple(self.conn.execute("SELECT n_records, n_recognized FROM sessions").fetchone()), (5, 3))
        self.assertEqual((self.stats()[("2.1.330", "user")], self.stats()[("2.1.330", "system:away_summary")]), (2, 1))
        self.assertEqual(get_meta(self.conn, "unreadable_lines"), "2")

    def test_flip_counts_each_line_once(self):
        p = self.fake.write("/w", sid(2), [user("課題", t(0), sid(2), "/w", human=False, entrypoint="sdk-cli"),
                                           {"type": "user", "message": "not a dict", "sessionId": sid(2)}])
        ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual(self.count("digests"), 1)
        self.fake.append("/w", sid(2), [user("人の入力", t(1), sid(2), "/w", entrypoint="sdk-cli")])
        r = ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((r.reset, r.new_records, r.kind), (True, 1, "interactive"))
        self.assertEqual(self.stats(), {("2.1.330", "user"): 2, ("?", "user"): 1})
        self.assertEqual(get_meta(self.conn, "unreadable_lines"), "1")
        s = self.session(sid(2))
        self.assertEqual((s["n_records"], s["n_prompts"], s["first_prompt"]), (3, 1, "人の入力"))
        self.assertEqual(self.count("digests"), 0)

    # 空の要約・題名（判断2）

    def test_empty_recap_and_title_do_not_overwrite(self):
        p = self.fake.write("/w", sid(1), [
            user("a", t(0), sid(1), "/w"),
            recap("要約です", t(1), sid(1), "/w"),
            recap(" (disable recaps in /config)", t(2), sid(1), "/w"),
            recap(" (turn off recaps in /config)", t(2.5), sid(1), "/w"),
            title("custom", "題名", sid(1)),
            title("custom", "  ", sid(1)),
        ])
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((s["recap"], s["recap_at"], s["custom_title"]), ("要約です", t(1), "題名"))

    def test_session_fields_first_and_last(self):
        p = self.fake.write("/w", sid(1), [
            user("a", t(0), sid(1), "/w", version="2.1.1", branch="main", forked_from=sid(7)),
            assistant(t(1), sid(1), "/w/sub", text="x", entrypoint="sdk-cli", version="2.1.2", branch="feature"),
            user("b", t(2), sid(1), "/w", version="2.1.3", branch="", forked_from=sid(8)),
            relocated(sid(1), "/w/.claude/worktrees/a"),
            relocated(sid(1), "/w/.claude/worktrees/b"),
            continued_in(sid(1), sid(5), t(3)),
            continued_in(sid(1), sid(6), t(4)),
            title("custom", "一", sid(1)),
            title("custom", "二", sid(1)),
            title("agent", "名前", sid(1)),
        ])
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((s["cwd"], s["entrypoint"], s["version"], s["branch"]), ("/w", "cli", "2.1.3", "feature"))
        self.assertEqual((s["relocated_cwd"], s["forked_from"], s["continued_in"]),
                         ("/w/.claude/worktrees/b", sid(7), sid(6)))
        self.assertEqual((s["custom_title"], s["agent_name"], s["ai_title"]), ("二", "名前", None))

    # VS Code から始めたセッション（判断11）

    def test_vscode_prompt_is_interactive(self):
        p = self.fake.write("/w", sid(1), [user("こんにちは", t(0), sid(1), "/w", entrypoint="claude-vscode", prompt_source="sdk",
                                                context_block="<browser_instruction>x</browser_instruction>")])
        r = ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((r.kind, s["kind"], s["first_prompt"]), ("interactive", "interactive", "こんにちは"))

    # < で始まる本人の入力

    def rows(self, session):
        return [tuple(x) for x in self.conn.execute(
            "SELECT turn, role, text FROM messages WHERE session_id = ? ORDER BY seq", (session,))]

    def test_pasted_only_session_is_interactive(self):
        p = self.fake.write("/w", sid(1), [user(pasted("貼り付けた手順書の本文", "a1"), t(0), sid(1), "/w"),
                                           assistant(t(1), sid(1), "/w", text="読みました")])
        r = ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((r.kind, s["n_prompts"], s["first_prompt"]), ("interactive", 1, "貼り付けた手順書の本文"))
        self.assertEqual(self.rows(sid(1)), [(1, "user", "貼り付けた手順書の本文"), (1, "assistant", "読みました")])
        hits = self.conn.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?", ('"手順書の"',))
        self.assertEqual(hits.fetchone()[0], 1)
        # 貼り付けの後に打ち込んだ文は、本文（検索）には元の順に残る
        self.fake.append("/w", sid(1), [user(pasted("ログ", "b2") + "\n\nこの失敗を直して", t(2), sid(1), "/w")])
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual(s["n_prompts"], 2)
        self.assertEqual(self.rows(sid(1))[-1], (2, "user", "ログ\nこの失敗を直して"))

    def test_prompt_display_puts_typed_text_first(self):
        # 長い貼り付けの後に打ち込んだ依頼も、最初と最後の依頼・題名の代わり・一覧の要約に出る
        paste = "ログの行\n" * 600  # PROMPT_CHARS（2000字）を超える
        p = self.fake.write("/w", sid(1), [user(pasted(paste, "a1") + "\n\nこの失敗を直して", t(0), sid(1), "/w"),
                                           assistant(t(1), sid(1), "/w", text="直します")])
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertTrue(s["first_prompt"].startswith("この失敗を直して\nログの行\n"), s["first_prompt"][:40])
        self.assertEqual((len(s["first_prompt"]), s["last_prompt"]), (2000, s["first_prompt"]))
        self.assertEqual(choose_title(None, None, None, s["first_prompt"]), "この失敗を直して\n" + "ログの行\n" * 8 + "ロ")
        # 検索に使う本文は、元の順の全文
        text = self.rows(sid(1))[0][2]
        self.assertEqual((text[:5], text[-8:], len(text)), ("ログの行\n", "この失敗を直して", len(paste.strip()) + 9))
        # 打ち込んだ文のない貼り付けだけの入力は、貼り付けた中身を出す
        self.fake.append("/w", sid(1), [user(pasted(paste, "b2"), t(2), sid(1), "/w")])
        ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual(self.session(sid(1))["last_prompt"], paste.strip()[:2000])

    def test_skill_command_only_session_is_interactive(self):
        p = self.fake.write("/w", sid(1), [
            user(command_text("web-bug-audit"), t(0), sid(1), "/w"),
            user("Base directory for this skill: /x\n\n手順", t(0.1), sid(1), "/w", human=False, meta=True),
            assistant(t(1), sid(1), "/w", text="監査します", tools=[("Bash", {"command": "ls"})]),
        ])
        r = ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((r.kind, s["n_prompts"], s["first_prompt"], s["last_prompt"]),
                         ("interactive", 1, "/web-bug-audit", "/web-bug-audit"))
        self.assertEqual(self.rows(sid(1)), [(1, "user", "/web-bug-audit"), (1, "assistant", "監査します"),
                                             (1, "tool", "Bash（ls）")])
        self.assertEqual(self.conn.execute("SELECT ts FROM messages WHERE role = 'user'").fetchone()[0], t(0))

    def test_local_commands_are_not_prompts(self):
        p = self.fake.write("/w", sid(1), [
            user("<local-command-caveat>Caveat: …</local-command-caveat>", t(0), sid(1), "/w", meta=True),
            user(command_text("model", local=True), t(0), sid(1), "/w"),
            local_output("Set model to opus", t(0.1), sid(1), "/w"),
            user(command_text("config", local=True), t(1), sid(1), "/w"),
            user(command_text("exit", local=True), t(2), sid(1), "/w"),  # 応答のないまま終わる
        ])
        r = ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((r.kind, self.session(sid(1))["n_prompts"], self.count("messages")), ("empty", 0, 0))
        # 次の本人の入力より前に応答がなかったコマンドは数えない
        self.fake.append("/w", sid(1), [user("続きをお願いします", t(3), sid(1), "/w"),
                                         assistant(t(4), sid(1), "/w", text="はい")])
        r = ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((r.kind, s["n_prompts"], s["first_prompt"]), ("interactive", 1, "続きをお願いします"))
        self.assertEqual(self.rows(sid(1)), [(1, "user", "続きをお願いします"), (1, "assistant", "はい")])

    def test_command_answered_in_a_later_read(self):
        p = self.fake.write("/w", sid(1), [user("最初", t(0), sid(1), "/w"), assistant(t(1), sid(1), "/w", text="a"),
                                           user(command_text("review", "12"), t(2), sid(1), "/w")])
        ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual(self.session(sid(1))["n_prompts"], 1)
        # 応答が次の回に追記されても数える（読み終えた位置をまたぐ）
        self.fake.append("/w", sid(1), [assistant(t(3), sid(1), "/w", text="PR 12 を見ます")])
        self.assertFalse(ingest_transcript(self.conn, p, self.ctx).reset)
        s = self.session(sid(1))
        self.assertEqual((s["n_prompts"], s["last_prompt"], s["turn"]), (2, "/review 12", 2))
        self.assertEqual(self.rows(sid(1))[2:], [(2, "user", "/review 12"), (2, "assistant", "PR 12 を見ます")])

    def test_local_output_before_any_answer_cancels_the_command(self):
        p = self.fake.write("/w", sid(1), [
            user("作業して", t(0), sid(1), "/w"),
            assistant(t(1), sid(1), "/w", tools=[("Bash", {"command": "make"})]),
            user(command_text("cost", local=True), t(2), sid(1), "/w"),
            local_output("Total cost: $0.10", t(2.1), sid(1), "/w"),
            assistant(t(3), sid(1), "/w", text="続けます"),
        ])
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((s["n_prompts"], s["last_prompt"]), (1, "作業して"))
        self.assertEqual([x[2] for x in self.rows(sid(1)) if x[1] == "user"], ["作業して"])

    def test_local_output_cancels_only_its_own_command(self):
        # スキルのコマンドの直後に /cost とその出力が来ても、スキルは応答を待ったまま。
        # /cost は実データの形（本人の印なし）と、本人の印のある形の両方で確かめる
        for n, cost in ((1, local_command("cost", t(1), sid(1), "/w")),
                        (2, user(command_text("cost", local=True), t(1), sid(2), "/w"))):
            with self.subTest(session=n):
                p = self.fake.write("/w", sid(n), [
                    skill_command("web-bug-audit", t(0), sid(n), "/w"),
                    cost,
                    local_output("Total cost: $0.10", t(1.1), sid(n), "/w"),
                    assistant(t(2), sid(n), "/w", text="監査します"),
                ])
                r = ingest_transcript(self.conn, p, self.ctx)
                s = self.session(sid(n))
                self.assertEqual((r.kind, s["n_prompts"], s["first_prompt"]), ("interactive", 1, "/web-bug-audit"))
                self.assertEqual(self.rows(sid(n)), [(1, "user", "/web-bug-audit"), (1, "assistant", "監査します")])

    def test_pending_commands_survive_between_reads(self):
        p = self.fake.write("/w", sid(1), [skill_command("web-bug-audit", t(0), sid(1), "/w"),
                                           local_command("cost", t(1), sid(1), "/w")])
        self.assertEqual(ingest_transcript(self.conn, p, self.ctx).kind, "empty")
        self.fake.append("/w", sid(1), [local_output("Total cost: $0.10", t(1.1), sid(1), "/w"),
                                         assistant(t(2), sid(1), "/w", text="監査します")])
        r = ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((r.kind, self.session(sid(1))["n_prompts"]), ("interactive", 1))
        self.assertIsNone(self.session(sid(1))["pending_commands"])

    def test_real_shaped_records(self):
        # 実データの形：対話で打ったスキル（origin だけ）、印のないローカルのコマンド、Claude が使ったスキル
        p = self.fake.write("/w", sid(1), [
            local_command("model", t(0), sid(1), "/w"),
            local_output("Set model to opus", t(0.1), sid(1), "/w"),
            local_command("effort", t(1), sid(1), "/w", args="high"),
            local_output("Set effort level to high", t(1.1), sid(1), "/w"),
            assistant(t(1.2), sid(1), "/w", text="（ローカルのコマンドの後の応答）"),
            user("調べて", t(2), sid(1), "/w"),
        ] + skill_tool_use("web-bug-audit", t(3), sid(1), "/w") + [
            assistant(t(4), sid(1), "/w", text="スキルの手順で調べます"),
            local_command("compact", t(5), sid(1), "/w"),
            local_output("Compacted", t(5.1), sid(1), "/w"),
            skill_command("simplify", t(6), sid(1), "/w", args="src"),
            system_local_command("<command-name>/model</command-name>", t(6.05), sid(1), "/w"),
            system_local_command("<local-command-stdout>Set model</local-command-stdout>", t(6.06), sid(1), "/w"),
            user("Base directory for this skill: /x", t(6.1), sid(1), "/w", human=False, meta=True),
            assistant(t(7), sid(1), "/w", tools=[("Read", {"file_path": "/w/src/a.py"})]),
        ])
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((s["n_prompts"], s["first_prompt"], s["last_prompt"]), (2, "調べて", "/simplify src"))
        self.assertEqual([x[2] for x in self.rows(sid(1)) if x[1] == "user"], ["調べて", "/simplify src"])

    def test_thinking_only_record_is_not_an_answer(self):
        thinking = assistant(t(1), sid(1), "/w")
        thinking["message"]["content"] = [{"type": "thinking", "thinking": "", "signature": "x"}]
        p = self.fake.write("/w", sid(1), [skill_command("web-bug-audit", t(0), sid(1), "/w"), thinking])
        ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual(self.session(sid(1))["n_prompts"], 0)
        self.fake.append("/w", sid(1), [assistant(t(2), sid(1), "/w", text="監査します")])
        ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual(self.session(sid(1))["n_prompts"], 1)

    def test_answered_command_turns_headless_into_interactive(self):
        p = self.fake.write("/w", sid(2), [
            user("課題", t(0), sid(2), "/w", human=False, entrypoint="sdk-cli"),
            assistant(t(1), sid(2), "/w", text="途中の報告", entrypoint="sdk-cli"),
            user(command_text("model", local=True), t(2), sid(2), "/w", entrypoint="sdk-cli"),
            local_output("Set model", t(2.1), sid(2), "/w", entrypoint="sdk-cli"),
        ])
        self.assertEqual(ingest_transcript(self.conn, p, self.ctx).kind, "headless")
        self.fake.append("/w", sid(2), [user(command_text("web-bug-audit"), t(3), sid(2), "/w", entrypoint="sdk-cli"),
                                         assistant(t(4), sid(2), "/w", text="監査します", entrypoint="sdk-cli")])
        r = ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(2))
        self.assertEqual((r.kind, s["n_prompts"], s["first_prompt"]), ("interactive", 1, "/web-bug-audit"))
        self.assertEqual(self.count("digests"), 0)

    def test_non_human_command_is_the_headless_first_prompt(self):
        p = self.fake.write("/w", sid(2), [
            user(command_text("review", "12"), t(0), sid(2), "/w", human=False, entrypoint="sdk-cli"),
            assistant(t(1), sid(2), "/w", text="見ました", entrypoint="sdk-cli")])
        self.assertEqual(ingest_transcript(self.conn, p, self.ctx).kind, "headless")
        self.assertEqual(self.conn.execute("SELECT first_prompt FROM digests").fetchone()[0], "/review 12")

    # 記録の derived-from

    def derived(self, session):
        return sorted(tuple(x) for x in self.conn.execute(
            "SELECT source_id, via FROM derived_from WHERE session_id = ?", (session,)))

    def test_derived_from_is_known_and_stored(self):
        p = self.fake.write("/w", sid(2), [
            derived_from(sid(2), [(sid(1), "subprocess"), (sid(3), "zzz")]),
            user("課題", t(0), sid(2), "/w", human=False, entrypoint="sdk-cli"),
            derived_from(sid(2), [(sid(1), "subprocess"), (sid(4), "compact")]),  # 増えたら全体を書き直す
        ])
        r = ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((r.new_records, r.unknown_records), (3, 0))
        self.assertEqual(self.derived(sid(2)), [(sid(1), "subprocess"), (sid(3), "zzz"), (sid(4), "compact")])
        self.assertEqual(self.stats()[("?", "derived-from")], 2)
        # 作り直された記録では、前の行を残さない
        self.fake.write("/w", sid(2), [user("課題", t(0), sid(2), "/w", human=False, entrypoint="sdk-cli")])
        self.assertTrue(ingest_transcript(self.conn, p, self.ctx).reset)
        self.assertEqual(self.derived(sid(2)), [])

    # ターン・長さ・ヘッドレスの要約・資料・言及

    def test_turns_and_seq_continue_across_reads(self):
        p = self.fake.write("/w", sid(1), [assistant(t(0), sid(1), "/w", text="前置き"), user("一", t(1), sid(1), "/w")])
        ingest_transcript(self.conn, p, self.ctx)
        self.fake.append("/w", sid(1), [assistant(t(2), sid(1), "/w", text="返事"),
                                         user("自動の入力", t(3), sid(1), "/w", human=False),
                                         user("二", t(4), sid(1), "/w")])
        self.assertFalse(ingest_transcript(self.conn, p, self.ctx).reset)
        rows = [tuple(x) for x in self.conn.execute("SELECT seq, turn, ts, role, text FROM messages ORDER BY seq")]
        self.assertEqual(rows, [(0, 0, t(0), "assistant", "前置き"), (1, 1, t(1), "user", "一"),
                                (2, 1, t(2), "assistant", "返事"), (3, 2, t(4), "user", "二")])
        s = self.session(sid(1))
        self.assertEqual((s["turn"], s["next_seq"], s["n_prompts"], s["last_prompt"]), (2, 4, 2, "二"))

    def test_truncation(self):
        p = self.fake.write("/w", sid(1), [user("あ" * 2500, t(0), sid(1), "/w"),
                                           assistant(t(1), sid(1), "/w", text="い" * 500)])
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(sid(1))
        self.assertEqual((s["first_prompt"], s["last_prompt"], s["last_reply"]), ("あ" * 2000, "あ" * 2000, "い" * 300))
        self.assertEqual(self.conn.execute("SELECT length(text) FROM messages WHERE role = 'user'").fetchone()[0], 2500)

    def test_headless_digest_follows_appends(self):
        p = self.fake.write("/w", sid(2), [
            user("最初の依頼", t(0), sid(2), "/w", human=False, entrypoint="sdk-cli"),
            user("二つ目の自動入力", t(1), sid(2), "/w", human=False, entrypoint="sdk-cli"),
            assistant(t(2), sid(2), "/w", text="途中の報告", entrypoint="sdk-cli"),
            recap("要約です", t(3), sid(2), "/w", entrypoint="sdk-cli"),
        ])
        ingest_transcript(self.conn, p, self.ctx)
        self.fake.append("/w", sid(2), [assistant(t(4), sid(2), "/w", text="最後の報告", entrypoint="sdk-cli")])
        r = ingest_transcript(self.conn, p, self.ctx)
        self.assertEqual((r.kind, self.count("messages")), ("headless", 0))
        d = self.conn.execute("SELECT first_prompt, last_reply, text FROM digests").fetchone()
        self.assertEqual(tuple(d), ("最初の依頼", "最後の報告", "最初の依頼\n最後の報告\n要約です"))
        hits = lambda q: self.conn.execute("SELECT count(*) FROM digests_fts WHERE digests_fts MATCH ?", (q,)).fetchone()[0]
        self.assertEqual((hits('"途中の報"'), hits('"最後の報"')), (0, 1))

    def test_doc_link_rels_and_filters(self):
        ctx = IngestContext(home=HOME, exts=(".md", ".ipynb"))
        p = self.fake.write("/w", sid(1), [
            assistant(t(0), sid(1), "/w", tools=[
                ("Edit", {"file_path": "/w/a.md"}), ("MultiEdit", {"file_path": "/w/b.md"}),
                ("NotebookEdit", {"notebook_path": "/w/n.ipynb"}), ("Read", {"file_path": "/w/x/../c.md"}),
                ("Read", {"file_path": "/w/d.py"}), ("Write", {"file_path": "/private/tmp/claude-501/e.md"}),
                ("Write", {"file_path": HOME + "/.claude/settings.md"}), ("Read", {"file_path": "rel/f.md"})]),
            assistant(t(1), sid(1), "/w", tools=[("Edit", {"file_path": "/w/a.md"})]),
        ])
        r = ingest_transcript(self.conn, p, ctx)
        links = sorted(tuple(x) for x in self.conn.execute("SELECT path, rel, ts FROM doc_links"))
        self.assertEqual(links, [("/w/a.md", "edited", t(0)), ("/w/b.md", "edited", t(0)),
                                 ("/w/c.md", "read", t(0)), ("/w/n.ipynb", "edited", t(0))])
        self.assertEqual(r.touched_docs, {"/w/a.md", "/w/b.md", "/w/c.md", "/w/n.ipynb"})

    def test_writes_under_tasks_root_any_extension(self):
        tasks_root = os.path.join(os.path.realpath(self._tmp.name), "tasks")
        sql = tasks_root + "/2026-11-20-x/data/query.sql"
        other = os.path.join(os.path.realpath(self._tmp.name), "other/query.sql")
        ctx = IngestContext(home=HOME, exts=tuple(DEFAULT_DOC_EXTENSIONS), tasks_root=tasks_root)
        p = self.fake.write("/w", sid(1), [
            assistant(t(0), sid(1), "/w", tools=[
                ("Write", {"file_path": sql}), ("Write", {"file_path": other}),
                ("Read", {"file_path": tasks_root + "/2026-11-20-x/data/read.sql"})]),
        ])
        with allow_system_tmp():
            result = ingest_transcript(self.conn, p, ctx)
        rows = [tuple(r) for r in self.conn.execute("SELECT path, rel FROM doc_links")]
        self.assertEqual(rows, [(sql, "task_file")])
        self.assertNotIn(sql, result.touched_docs)

    def test_mentions_sources_and_first_ts(self):
        p = self.fake.write("/w", sid(1), [
            user("依頼 " + sid(2), t(0), sid(1), "/w", human=False),
            assistant(t(1), sid(1), "/w", text="応答 " + sid(3), tools=[("Bash", {"command": "cat " + sid(4)})]),
            tool_result("結果 " + sid(5) + " " + sid(2), t(2), sid(1), "/w"),
            user("自分 " + sid(1), t(3), sid(1), "/w"),
        ])
        ingest_transcript(self.conn, p, self.ctx)
        got = {x[0]: x[1] for x in self.conn.execute("SELECT mentioned_id, ts FROM mentions")}
        self.assertEqual(got, {sid(2): t(0), sid(3): t(1), sid(4): t(1), sid(5): t(2)})

    def test_uuid_re(self):
        u = "abcdef01-2345-4789-abcd-ef0123456789"
        self.assertEqual(UUID_RE.findall("ID:" + u + "を見て"), [u])
        self.assertEqual(UUID_RE.findall(u.upper()), [])
        self.assertEqual(UUID_RE.findall("0" + u), [])

    def test_find_doc_paths_japanese_suffix(self):
        def found(text):
            return find_doc_paths(text, "/w", HOME, DEFAULT_DOC_EXTENSIONS)
        self.assertEqual(found("docs/a.mdを読んで"), ["/w/docs/a.md"])
        self.assertEqual(found("HANDOFF.mdの続きをお願いします"), ["/w/HANDOFF.md"])
        self.assertEqual(found("作業再開メモ.mdを開いて"), ["/w/作業再開メモ.md"])
        self.assertEqual(found("v1.mdの写し.mdを見て"), ["/w/v1.mdの写し.md"])  # いちばん長い頭の部分
        self.assertEqual(found("a.md.bak"), [])
        self.assertEqual(found("x.mdx2"), [])

    def test_find_doc_paths_quoted_at(self):
        # 空白を含むパスは @"<パス>" で渡す。引用符の中を1つのパスとして読む
        def found(text):
            return find_doc_paths(text, "/w", HOME, DEFAULT_DOC_EXTENSIONS)
        task_md = HOME + "/My Tasks/2026-11-22-x/task.md"
        self.assertEqual(found('@"%s" を読んで、次の一手から進めてください。' % task_md), [task_md])
        self.assertEqual(found('@"~/My Tasks/a/research-x.md" と @/x/b.md と @"docs/c d.md"を'),
                         [HOME + "/My Tasks/a/research-x.md", "/x/b.md", "/w/docs/c d.md"])
        self.assertEqual(found('@"/x/a b.md" @"/x/e f.html"'), ["/x/a b.md", "/x/e f.html"])
        # @ のない引用符と、閉じていない @" は、これまでどおり空白と引用符で切る
        self.assertEqual(found('"/x/c d.md"'), ["/w/d.md"])
        self.assertEqual(found('@"/x/g h.md'), ["/w/h.md"])

    # 形の崩れた記録で止まらない

    def test_odd_records_are_skipped_without_blocking(self):
        s1 = sid(1)
        odd = [
            {"type": "user", "message": "not a dict", "sessionId": s1, "timestamp": t(1)},
            {"type": "assistant", "message": "not a dict", "sessionId": s1, "timestamp": t(1)},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": 5}]}, "sessionId": s1, "timestamp": t(1)},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": 5}]}]},
             "toolUseResult": {}, "sessionId": s1, "timestamp": t(1)},
            {"type": "assistant", "message": {"content": 5}, "sessionId": s1, "timestamp": t(1)},  # 例外にはならない
        ]
        p = self.fake.write("/w", s1, [user("一つ目", t(0), s1, "/w")] + odd)
        with open(p, "a") as f:
            f.write("[" * 5000 + "]" * 5000 + "\n")  # 深すぎて解釈できない行
        self.fake.append("/w", s1, [user("二つ目", t(2), s1, "/w")])
        r = ingest_transcript(self.conn, p, self.ctx)
        s = self.session(s1)
        self.assertEqual((r.new_records, s["n_prompts"], s["n_records"], s["n_recognized"]), (7, 2, 7, 2))
        self.assertEqual([x[0] for x in self.conn.execute("SELECT text FROM messages WHERE role = 'user' ORDER BY seq")],
                         ["一つ目", "二つ目"])
        self.assertEqual(get_meta(self.conn, "unreadable_lines"), "5")
        # 次の回も続きから進む（同じ行で止まり続けない）
        self.fake.append("/w", s1, [user("三つ目", t(3), s1, "/w")])
        self.assertEqual(ingest_transcript(self.conn, p, self.ctx).new_records, 1)
        self.assertEqual((self.session(s1)["n_prompts"], get_meta(self.conn, "unreadable_lines")), (3, "5"))

    def test_odd_field_types_do_not_poison_the_session(self):
        s1 = sid(1)
        p = self.fake.write("/w", s1, [
            user("一つ目", t(0), s1, "/w"),
            dict(user("型の崩れた入力", t(1), s1, "/w"), cwd=5, timestamp=1700000000, gitBranch=["x"], version={"v": 1}),
            {"type": "ai-title", "aiTitle": ["x"], "sessionId": s1},
            {"type": "pr-link", "prUrl": {"u": 1}, "sessionId": s1, "timestamp": t(1)},
            {"type": "relocated", "relocatedCwd": {"d": 1}, "sessionId": s1},
            {"type": "continued-in", "continuedInSessionId": 7, "sessionId": s1, "timestamp": t(1)},
            assistant(t(1), s1, "/w", tools=[(["Read"], {"file_path": "/w/a.md"})]),
            user("二つ目", t(2), s1, "/w"),
        ])
        with open(p, "a") as f:
            f.write('{"type":"custom-title","customTitle":"題\\ud83d","sessionId":"%s"}\n' % s1)
        ingest_transcript(self.conn, p, self.ctx)
        s = self.session(s1)
        self.assertEqual((s["n_prompts"], s["cwd"], s["started_at"], s["ended_at"], s["branch"], s["version"]),
                         (3, "/w", t(0), t(2), "main", "2.1.330"))
        self.assertEqual((s["ai_title"], s["relocated_cwd"], s["continued_in"], s["custom_title"]),
                         (None, None, None, "題\ufffd"))
        self.assertEqual((self.count("session_prs"), self.count("doc_links")), (0, 0))
        self.assertIsNone(get_meta(self.conn, "unreadable_lines"))

    def test_find_doc_paths_separators_and_urls(self):
        text = "「docs/a.md」と`b.txt`、(c.html)。@~/n.md https://x.com/r.md docs/a.md e.py README.MD"
        self.assertEqual(find_doc_paths(text, "/w", HOME, DEFAULT_DOC_EXTENSIONS),
                         ["/w/docs/a.md", "/w/b.txt", "/w/c.html", "/Users/u/n.md", "/w/README.MD"])
        self.assertEqual(find_doc_paths("a.md /x/./b.md", None, HOME, DEFAULT_DOC_EXTENSIONS), ["/x/b.md"])


if __name__ == "__main__":
    unittest.main()
