import tempfile
import time
import unittest
from pathlib import Path

from tsuzuki.sources import transcript as tr
from tsuzuki.sources.transcript import (
    MAX_LINE_BYTES, ForkedFrom, Meta, Title, ToolResult, UserPrompt, extract_events,
    is_human_prompt, iter_lines, record_type, tool_summary,
)
from tests.fixtures import (
    FakeClaude, api_request, assistant, command_text, continued_in, derived_from, local_output, pasted, pr_link,
    recap, relocated, sid, t, title, tool_result, user,
)


def prompts(record):
    return [e for e in extract_events(record) if isinstance(e, UserPrompt)]


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake = FakeClaude(Path(self._tmp.name))

    def test_skips_heavy_and_reports_unreadable(self):
        fake = self.fake
        p = fake.write("/w", sid(1), [api_request(sid(1))])
        with open(p, "a") as f:
            f.write("{not json\n")
        fake.append("/w", sid(1), [user("こんにちは", t(0), sid(1), "/w")])
        lines = list(iter_lines(p, 0))
        self.assertEqual([l.skipped for l in lines], [True, False, False])
        self.assertIsNone(lines[1].record)
        self.assertEqual(lines[2].record["type"], "user")

    def test_partial_last_line_is_not_consumed(self):
        p = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")], partial_tail='{"type":"us')
        lines = list(iter_lines(p, 0))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[-1].end_offset, p.stat().st_size - len('{"type":"us'))

    def test_start_offset(self):
        p = self.fake.write("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        first = list(iter_lines(p, 0))[0].end_offset
        self.fake.append("/w", sid(1), [user("b", t(1), sid(1), "/w")])
        lines = list(iter_lines(p, first))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].record["message"]["content"], "b")

    def test_oversized_line_is_skipped_and_reading_continues(self):
        p = self.fake.write("/w", sid(1), [api_request(sid(1), size=1)])
        p.write_bytes(b'{"type":"x","pad":"' + b"a" * 9_000_000 + b'"}\n')
        self.fake.append("/w", sid(1), [user("a", t(0), sid(1), "/w")])
        lines = list(iter_lines(p, 0))
        self.assertEqual([l.skipped for l in lines], [True, False])
        self.assertIsNone(lines[0].record)
        self.assertEqual(lines[1].record["type"], "user")
        self.assertEqual(lines[1].end_offset, p.stat().st_size)

    def test_human_prompt_rules(self):
        ev = extract_events(user("依頼です", t(0), sid(1), "/w"))
        self.assertEqual([type(e).__name__ for e in ev], ["Meta", "UserPrompt"])
        self.assertTrue(ev[1].human)
        self.assertFalse(extract_events(user("依頼", t(0), sid(1), "/w", human=False))[1].human)
        self.assertEqual([type(e).__name__ for e in extract_events(user("x", t(0), sid(1), "/w", meta=True))], ["Meta"])
        self.assertIsInstance(extract_events(tool_result("出力 " + sid(9), t(0), sid(1), "/w"))[1], ToolResult)

    # < で始まる本人の入力

    def test_pasted_content_is_unwrapped(self):
        def text_of(content):
            return [e.text for e in prompts(user(content, t(0), sid(1), "/w"))]
        self.assertEqual(prompts(user(pasted("貼った文", "a1"), t(0), sid(1), "/w")), [UserPrompt(t(0), "貼った文", True)])
        self.assertEqual(text_of(pasted("貼った文", "a1") + "\n\nこれを見て"), ["貼った文\nこれを見て"])
        self.assertEqual(text_of("これを見て\n\n" + pasted("後ろに足した文", "b2")), ["これを見て\n後ろに足した文"])
        self.assertEqual(text_of(pasted("一", "1") + pasted("二", "2") + "三"), ["一\n二\n三"])
        # 貼った中身が < で始まっても本人の入力のまま
        self.assertEqual(text_of(pasted("<div>表</div>", "c3")), ["<div>表</div>"])
        self.assertEqual(text_of(pasted("", "d4")), [])
        self.assertEqual(text_of("<system-reminder>" + pasted("x") + "</system-reminder>"), [])
        # text の塊の並び（貼り付けの塊と打ち込んだ塊）
        rec = user("打ち込んだ文", t(0), sid(1), "/w", context_block=pasted("貼った文", "e5"))
        self.assertEqual([e.text for e in prompts(rec)], ["貼った文\n打ち込んだ文"])

    def test_pasted_prompt_display_puts_typed_text_first(self):
        # 本文（検索）は元の順、表示用（最初と最後の依頼・題名の代わり）は打ち込んだ文が先
        def one(content, **kw):
            found = prompts(user(content, t(0), sid(1), "/w", **kw))
            self.assertEqual(len(found), 1, content)
            return found[0].text, found[0].display
        self.assertEqual(one(pasted("貼った文") + "\n\nこれを見て"), ("貼った文\nこれを見て", "これを見て\n貼った文"))
        self.assertEqual(one(pasted("一", "1") + "前" + pasted("二", "2") + "後"), ("一\n前\n二\n後", "前\n後\n一\n二"))
        self.assertEqual(one("これを見て\n\n" + pasted("後ろの文")), ("これを見て\n後ろの文", None))
        self.assertEqual(one(pasted("貼った文だけ")), ("貼った文だけ", None))
        self.assertEqual(one("打ち込んだ文", context_block=pasted("貼った文")), ("貼った文\n打ち込んだ文", "打ち込んだ文\n貼った文"))
        self.assertEqual(one(command_text("review", pasted("長い引数"))), ("/review 長い引数", None))

    def test_long_prompt_with_pasted_tags_is_read_quickly(self):
        # 長い空白の並びがあっても、タグを外すのに時間がかからない（正規表現の後戻りで固まらない）
        text = "a" + " " * 200000 + "b " + pasted("x")
        started = time.monotonic()
        self.assertEqual([e.text for e in prompts(user(text, t(0), sid(1), "/w"))], ["a" + " " * 200000 + "b\nx"])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_slash_command_records(self):
        def one(content, **kw):
            found = prompts(user(content, t(0), sid(1), "/w", **kw))
            self.assertEqual(len(found), 1, content)
            return found[0]
        self.assertEqual(one(command_text("web-bug-audit")), UserPrompt(t(0), "/web-bug-audit", True, command=True))
        self.assertEqual(one(command_text("review", "12 docs/a.md")).text, "/review 12 docs/a.md")
        self.assertEqual(one(command_text("model", local=True)), UserPrompt(t(0), "/model", True, command=True))
        self.assertEqual(one(command_text("compact", "要点だけ", local=True)).text, "/compact 要点だけ")
        self.assertEqual(one("<command-message>x</command-message>\n<command-name>x</command-name>\n"
                             "<skill-format>true</skill-format>").text, "/x")
        self.assertEqual(one(command_text("review", pasted("長い引数", "f6"))).text, "/review 長い引数")
        self.assertEqual(one(command_text("review"), human=False), UserPrompt(t(0), "/review", False, command=True))
        # VS Code の説明の塊が前に付いても、コマンドのまま
        self.assertEqual(one(command_text("web-bug-audit"), context_block="<ide_opened_file>a</ide_opened_file>"),
                         UserPrompt(t(0), "/web-bug-audit", True, command=True))
        self.assertEqual(prompts(user("<command-message>x is running…</command-message>", t(0), sid(1), "/w")), [])
        self.assertEqual(prompts(user("<command-name></command-name>", t(0), sid(1), "/w")), [])

    def test_command_with_typed_block_is_a_plain_prompt(self):
        rec = user("ついでにこれも", t(0), sid(1), "/w", context_block=command_text("review"))
        self.assertEqual(prompts(rec), [UserPrompt(t(0), "/review\nついでにこれも", True)])

    def test_other_tags_stay_excluded(self):
        for text in ("<system-reminder>x</system-reminder>", "<local-command-caveat>Caveat</local-command-caveat>",
                     "<local-command-stdout>a</local-command-stdout>", "<bash-input>ls</bash-input>",
                     "<bash-stdout>a</bash-stdout>", '<wake reason="timer">起きて</wake>',
                     "<VERSION_PREFIX>2.1</VERSION_PREFIX>本文", "<bootstrap>x</bootstrap>"):
            with self.subTest(text=text):
                self.assertEqual(prompts(user(text, t(0), sid(1), "/w")), [])

    def test_local_command_output(self):
        self.assertEqual(extract_events(local_output("Set model to opus", t(0), sid(1), "/w"))[-1],
                         tr.LocalCommandOutput(t(0)))
        self.assertEqual(extract_events(local_output("失敗", t(0), sid(1), "/w", stream="stderr"))[-1],
                         tr.LocalCommandOutput(t(0)))
        self.assertEqual(extract_events(dict(local_output("x", t(0), sid(1), "/w"), isMeta=True))[-1],
                         tr.LocalCommandOutput(t(0)))

    def test_derived_from(self):
        rec = derived_from(sid(2), [(sid(1), "subprocess"), (sid(3), "branch")])
        rec["sessions"] += [{"type": "unknown", "id": None, "via": "launch"},
                            {"type": "claude_code_session", "id": 5, "via": "fork"},
                            {"type": "claude_code_session", "id": sid(4)}, "x"]
        self.assertEqual([e for e in extract_events(rec) if not isinstance(e, Meta)],
                         [tr.DerivedFrom(sid(1), "subprocess"), tr.DerivedFrom(sid(3), "branch")])
        self.assertIn("derived-from", tr.KNOWN_TYPES)
        for odd in ({"type": "derived-from", "sessions": {"id": sid(1), "via": "fork"}},
                    {"type": "derived-from", "sessions": "x"}, {"type": "derived-from"}):
            with self.subTest(odd=odd):
                self.assertEqual(extract_events(odd), [])

    def test_vscode_context_block_is_dropped(self):
        rec = user("こんにちは", t(0), sid(1), "/w", entrypoint="claude-vscode",
                   context_block="<browser_instruction>x</browser_instruction>", prompt_source="sdk")
        self.assertEqual(rec["promptSource"], "sdk")
        self.assertEqual(rec["origin"], {"kind": "human"})
        self.assertEqual(extract_events(rec)[-1], UserPrompt(t(0), "こんにちは", human=True))
        only_ctx = user("<x>y</x>", t(0), sid(1), "/w", context_block="<a>b</a>")
        self.assertEqual([type(e).__name__ for e in extract_events(only_ctx)], ["Meta"])

    def test_is_human_prompt(self):
        self.assertTrue(is_human_prompt(user("a", t(0), sid(1), "/w")))
        self.assertFalse(is_human_prompt(user("a", t(0), sid(1), "/w", human=False)))
        self.assertFalse(is_human_prompt(user("a", t(0), sid(1), "/w", meta=True)))

    def test_assistant_events_and_record_type(self):
        rec = assistant(t(0), sid(1), "/w", text="返答", tools=[("Bash", {"command": "ls"})])
        names = [type(e).__name__ for e in extract_events(rec)]
        self.assertEqual(names, ["Meta", "AssistantText", "ToolUse"])
        self.assertEqual(record_type(rec), "assistant")
        self.assertEqual(record_type(recap("x", t(0), sid(1), "/w")), "system:away_summary")

    def test_recap_strips_suffix(self):
        ev = extract_events(recap("作業中です。次は確認です。 (disable recaps in /config)", t(0), sid(1), "/w"))
        self.assertEqual(ev[-1].text, "作業中です。次は確認です。")
        for text in ("作業中です。次は確認です。 (turn off recaps in /config)",
                     "作業中です。次は確認です。\n(turn off recaps in /config)\n",
                     "作業中です。次は確認です。(disable recaps in /config)  ",
                     "作業中です。次は確認です。  ( turn off recaps in /config )"):
            with self.subTest(text=text):
                self.assertEqual(extract_events(recap(text, t(0), sid(1), "/w"))[-1].text, "作業中です。次は確認です。")
        # 途中の括弧と、ほかの括弧で終わる要約はそのまま
        self.assertEqual(extract_events(recap("A (B) は済み (残り1件)", t(0), sid(1), "/w"))[-1].text,
                         "A (B) は済み (残り1件)")

    def test_titles_links_and_fork(self):
        self.assertEqual(extract_events(title("ai", "自動の題名", sid(1)))[-1], Title("ai", "自動の題名"))
        self.assertEqual(extract_events(title("custom", "題", sid(1)))[-1], Title("custom", "題"))
        self.assertEqual(extract_events(title("agent", "名", sid(1)))[-1], Title("agent", "名"))
        self.assertEqual(extract_events(continued_in(sid(1), sid(2), t(0)))[-1].session_id, sid(2))
        self.assertEqual(extract_events(pr_link(sid(1), "https://github.com/o/r/pull/4", t(0)))[-1].url, "https://github.com/o/r/pull/4")
        self.assertEqual(extract_events(relocated(sid(1), "/x"))[-1].cwd, "/x")
        fe = extract_events(user("x", t(0), sid(2), "/w", forked_from=sid(1)))
        self.assertIn(ForkedFrom(sid(1)), fe)

    def test_odd_field_types_give_typed_events(self):
        rec = dict(user("a", t(0), sid(1), "/w"), cwd=5, timestamp=1, gitBranch=["x"], version={"v": 1}, entrypoint=2)
        self.assertEqual(extract_events(rec), [Meta(sid(1), None, None, None, None, None), UserPrompt(None, "a", True)])
        for odd in ({"type": "ai-title", "aiTitle": ["x"]}, {"type": "custom-title", "customTitle": 1},
                    {"type": "agent-name", "agentName": {"a": 1}}, {"type": "pr-link", "prUrl": {"u": 1}},
                    {"type": "relocated", "relocatedCwd": 1}, {"type": "continued-in", "continuedInSessionId": 7}):
            with self.subTest(odd=odd):
                self.assertEqual(extract_events(odd), [])
        forked = dict(user("x", t(0), sid(1), "/w"), forkedFrom={"sessionId": 3})
        self.assertNotIn("ForkedFrom", [type(e).__name__ for e in extract_events(forked)])
        tool = assistant(t(0), sid(1), "/w", tools=[(["Read"], {"file_path": "/w/a.md"})])
        self.assertEqual(extract_events(tool)[-1].name, "")

    def test_lone_surrogates_are_replaced_and_deep_nesting_is_unreadable(self):
        p = self.fake.write("/w", sid(1), [])
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"type":"ai-title","aiTitle":"題\\ud83d","sessionId":"x"}\n')
            f.write("[" * 5000 + "]" * 5000 + "\n")
            f.write('{"type":"ai-title","aiTitle":"\\ud83d\\ude00","sessionId":"x"}\n')
        lines = list(iter_lines(p, 0))
        self.assertEqual([l.record["aiTitle"] if l.record else None for l in lines], ["題\ufffd", None, "😀"])
        self.assertFalse(lines[1].skipped)

    def test_tool_summary(self):
        self.assertEqual(tool_summary("Bash", {"command": "ls -la", "description": "List files"}, "/w"), "Bash（List files）")
        self.assertEqual(tool_summary("Bash", {"command": "x" * 100}, "/w"), "Bash（" + "x" * 60 + "）")
        self.assertEqual(tool_summary("Write", {"file_path": "/w/docs/a.md"}, "/w"), "Write docs/a.md")
        self.assertEqual(tool_summary("Read", {"file_path": "/o/a.md"}, "/w"), "Read /o/a.md")
        self.assertEqual(tool_summary("Agent", {"description": "調査"}, "/w"), "Agent（調査）")
        self.assertEqual(tool_summary("Skill", {"skill": "tdd"}, "/w"), "Skill（tdd）")
        self.assertEqual(tool_summary("Grep", {"pattern": "foo"}, "/w"), "Grep（foo）")
        self.assertEqual(tool_summary("WebFetch", {"url": "u"}, "/w"), "WebFetch")
        self.assertLessEqual(len(tool_summary("Grep", {"pattern": "p" * 500}, "/w")), 120)


if __name__ == "__main__":
    unittest.main()
