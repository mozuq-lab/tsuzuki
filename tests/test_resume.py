import contextlib
import io
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki import cli
from tsuzuki.config import Config
from tsuzuki.resume import (ActionOutcome, applescript_quote, is_session_id, open_document, open_place, resume,
                           resume_command, run_steps, terminal_steps, vscode_steps, conversation_prompt,
                           new_conversation_command, vscode_new_steps, terminal_new_steps, start_conversation)
from tsuzuki.sources.live import LiveSession
from tests.fixtures import make_ledger

SID = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"
GONE = "11111111-2222-3333-4444-555555555555"
GONE_SID = "22222222-2222-3333-4444-555555555555"


class Proc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode, self.stderr = returncode, stderr


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = os.path.realpath(self._tmp.name)
        self.ledger = Path(self.d) / "tsuzuki"
        self.conn = make_ledger(self.ledger)
        self.addCleanup(self.conn.close)
        self.cfg = Config()
        for sid, exists in ((SID, 1), (GONE_SID, 0)):
            self.conn.execute("INSERT INTO sessions (id, transcript_path, transcript_exists, cwd) VALUES (?,?,?,?)",
                              (sid, "/t/%s.jsonl" % sid, exists, "/w"))
        self.conn.execute("INSERT INTO places (id, path, display) VALUES (1, ?, 'p')", (self.d,))
        self.md = os.path.join(self.d, "a.md")
        self.html = os.path.join(self.d, "b.html")
        for p in (self.md, self.html):
            Path(p).write_text("x", encoding="utf-8")
        for i, (p, ext) in enumerate(((self.md, "md"), (self.html, "html"), (os.path.join(self.d, "no.md"), "md")), 1):
            self.conn.execute("INSERT INTO documents (id, path, place_id, kind, ext) VALUES (?,?,1,'doc',?)", (i, p, ext))
        self.calls = []

    def runner(self, args, **kw):
        self.calls.append(args)
        return Proc(0)

    def go(self, sid=SID, how="vscode", live=None, confirm=False, isdir=lambda p: True, runner=None, cfg=None,
           sleeps=None):
        return resume(self.conn, sid, how, cfg=cfg or self.cfg, live=live or {}, confirm=confirm,
                      runner=runner or self.runner, sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                      isdir=isdir)

    def test_command_quoting(self):
        self.assertEqual(resume_command("/Users/u/My Docs/it's 日本語", SID),
                         "cd '/Users/u/My Docs/it'\"'\"'s 日本語' && claude --resume " + SID)

    def test_applescript_quote(self):
        self.assertEqual(applescript_quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_steps(self):
        self.assertEqual(vscode_steps("/w", SID, "Visual Studio Code"),
                         [["open", "-a", "Visual Studio Code", "/w"],
                          ["open", "vscode://anthropic.claude-code/open?session=" + SID]])
        steps = terminal_steps("/w x", SID)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][0], "osascript")
        self.assertIn('do script "cd \'/w x\' && claude --resume ' + SID + '"', " ".join(steps[0]))

    def test_rejects_non_uuid(self):
        self.assertFalse(is_session_id("x; rm -rf /"))
        with self.assertRaises(ValueError):
            resume_command("/w", "x; rm -rf /")

    def test_resume_flow(self):
        live = {SID: LiveSession(1, SID, "/w", "idle")}
        out = self.go(live=live)
        self.assertTrue(out.needs_confirm)
        self.assertFalse(out.ok)
        self.assertEqual(out.message, "このセッションは今開いています。続けますか？")
        self.assertEqual(self.calls, [])
        out = self.go(live=live, confirm=True)
        self.assertEqual((out.ok, out.message), (True, "VS Code で開きました"))
        self.assertEqual(len(self.calls), 2)

    def test_terminal_success(self):
        out = self.go(how="terminal")
        self.assertEqual((out.ok, out.message), (True, "Terminal で開きました"))
        self.assertEqual(len(self.calls), 1)

    def test_delay_between_vscode_steps(self):
        sleeps = []
        self.go(cfg=Config(vscode_uri_delay_ms=1500), sleeps=sleeps)
        self.assertEqual(sleeps, [1.5])

    def test_refusals(self):
        self.assertEqual(self.go(sid=GONE, how="terminal").message, "台帳にないセッションです")
        self.assertEqual(self.go(sid="x; rm -rf /").message, "台帳にないセッションです")
        self.assertEqual(self.go(sid=GONE_SID).message, "記録は削除済みのため再開できません")
        out = self.go(how="terminal", isdir=lambda p: False)
        self.assertEqual((out.ok, out.needs_confirm, out.message), (False, False, "作業ディレクトリが見つかりません: /w"))
        self.assertEqual(self.go(how="copy").message, "再開の方法が正しくありません")
        self.assertEqual(self.calls, [])

    def test_runner_failure(self):
        out = self.go(runner=lambda args, **kw: Proc(1))
        self.assertFalse(out.ok)
        self.assertTrue(out.message.startswith("開けませんでした"))

    def test_failure_says_why(self):
        # 失敗の理由として、外のコマンドの stderr の空でない最初の行を出す（設定 vscode_app の誤りなど）
        missing = b"\n  Unable to find application named 'Visual Studio Codee'\nLSOpenURLsWithRole() failed\n"
        reason = "開けませんでした: Unable to find application named 'Visual Studio Codee'（終了コード 1）"
        failing = lambda args, **kw: Proc(1, stderr=missing)  # noqa: E731
        self.assertEqual(self.go(runner=failing).message, reason)
        self.assertEqual(open_document(self.conn, 1, "vscode", cfg=self.cfg, runner=failing).message, reason)
        self.assertEqual(open_place(self.conn, 1, "vscode", cfg=self.cfg, runner=failing).message, reason)
        denied = b"35:72: execution error: Not authorized to send Apple events to Terminal. (-1743)\n"
        out = self.go(how="terminal", runner=lambda args, **kw: Proc(1, stderr=denied))
        self.assertEqual(out.message, "開けませんでした: 35:72: execution error: Not authorized to send Apple events "
                                      "to Terminal. (-1743)（終了コード 1）")
        # 読めない文字は置き換え、長い行は切る。stderr が空なら、これまでどおり終了コードだけ
        out = self.go(runner=lambda args, **kw: Proc(2, stderr=b"\xff" + b"x" * 500))
        self.assertEqual(out.message, "開けませんでした: �" + "x" * 199 + "（終了コード 2）")
        for empty in (b"", b" \n\n", None, "テキスト\n"):
            with self.subTest(stderr=empty):
                out = self.go(runner=lambda args, **kw: Proc(1, stderr=empty))
                expected = "テキスト（終了コード 1）" if empty == "テキスト\n" else "終了コード 1"
                self.assertEqual(out.message, "開けませんでした: " + expected)

    def test_runner_oserror_and_timeout(self):
        for exc in (OSError("boom"), subprocess.TimeoutExpired("open", 10)):
            calls = []

            def runner(args, **kw):
                calls.append(args)
                raise exc
            out = self.go(runner=runner)
            self.assertFalse(out.ok)
            self.assertTrue(out.message.startswith("開けませんでした"))
            self.assertEqual(len(calls), 1)  # 失敗で止まる

    def test_run_steps_kwargs(self):
        seen = []
        run_steps([["a"], ["b"]], runner=lambda args, **kw: seen.append((args, kw)) or Proc(0),
                  sleep=lambda s: None)
        self.assertEqual(seen[0][1], {"check": False, "capture_output": True, "timeout": 10})

    def test_open_document(self):
        run = lambda doc, how: open_document(self.conn, doc, how, cfg=self.cfg, runner=self.runner)
        self.assertEqual(run(1, "browser").message, "この資料はブラウザで開けません")
        self.assertFalse(run(1, "browser").ok)
        self.assertEqual(self.calls, [])
        self.assertEqual(run(1, "vscode").message, "開きました")
        self.assertEqual(self.calls[-1], ["open", "-a", "Visual Studio Code", self.md])
        run(1, "finder")
        self.assertEqual(self.calls[-1], ["open", "-R", self.md])
        run(2, "browser")
        self.assertEqual(self.calls[-1], ["open", self.html])
        self.assertEqual(run(99, "finder").message, "台帳にない資料です")
        self.assertEqual(run(3, "finder").message, "ファイルが見つかりません: " + os.path.join(self.d, "no.md"))
        self.assertEqual(run(1, "zzz").message, "開き方が正しくありません")

    def test_open_place(self):
        run = lambda pid, how: open_place(self.conn, pid, how, cfg=self.cfg, runner=self.runner)
        self.assertEqual(run(1, "vscode").message, "開きました")
        self.assertEqual(self.calls[-1], ["open", "-a", "Visual Studio Code", self.d])
        run(1, "finder")
        self.assertEqual(self.calls[-1], ["open", self.d])
        self.assertEqual(run(9, "finder").message, "台帳にない場所です")
        self.assertEqual(run(1, "zzz").message, "開き方が正しくありません")
        self.conn.execute("UPDATE places SET path = '/nonexistent-tsuzuki' WHERE id = 1")
        self.assertEqual(run(1, "finder").message, "フォルダーが見つかりません: /nonexistent-tsuzuki")

    def cli_run(self, *rest):
        args = ["--claude-dir", os.path.join(self.d, "claude"), "--ledger-dir", str(self.ledger)] + list(rest)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_resume_cli_prefix(self):
        self.conn.commit()
        code, out, _ = self.cli_run("resume", SID[:8])
        self.assertEqual(code, 0)
        self.assertIn("claude --resume " + SID, out)
        code, _, err = self.cli_run("resume", "ffffffff")
        self.assertEqual(code, 1)

    def test_resume_cli_terminal(self):
        self.conn.commit()
        self.conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (self.d, SID))
        self.conn.commit()
        with mock.patch("tsuzuki.resume.subprocess.run", side_effect=lambda a, **k: self.calls.append(a) or Proc(0)):
            code, out, _ = self.cli_run("resume", SID[:8], "--terminal")
        self.assertEqual(code, 0)
        self.assertIn("Terminal で開きました", out)
        self.assertEqual(self.calls[0][0], "osascript")
        with mock.patch("tsuzuki.resume.subprocess.run", side_effect=lambda a, **k: Proc(1)):
            code, _, err = self.cli_run("resume", SID, "--terminal")
        self.assertEqual(code, 1)
        self.assertIn("開けませんでした", err)

    def test_resume_cli_gone(self):
        self.conn.commit()
        code, _, err = self.cli_run("resume", GONE_SID[:8], "--terminal")
        self.assertEqual(code, 1)
        self.assertIn("記録は削除済みのため再開できません", err)


class NewConversationTest(unittest.TestCase):
    def test_prompt_and_command(self):
        # @{task_md} は画面のコピーと同じ @ の形（空白を含むパスは @"…"）、@ のない {task_md} はパスのまま
        prompt = conversation_prompt("@{task_md} を読んで、次の一手から進めてください。", "/Users/me/tasks/a b/task.md")
        self.assertEqual(prompt, '@"/Users/me/tasks/a b/task.md" を読んで、次の一手から進めてください。')
        self.assertEqual(conversation_prompt("@{task_md} を読んで", "/Users/me/tasks/a/task.md"),
                         "@/Users/me/tasks/a/task.md を読んで")
        self.assertEqual(conversation_prompt("{task_md} と @{task_md} と {n}", "/t/a b/task.md"),
                         '/t/a b/task.md と @"/t/a b/task.md" と {n}')
        self.assertEqual(new_conversation_command("/Users/me/dev/web app", "@/x/task.md を読んで"),
                         "cd '/Users/me/dev/web app' && claude '@/x/task.md を読んで'")

    def test_vscode_steps_encode_prompt(self):
        steps = vscode_new_steps("/r", "@/x y/task.md を読んで", "Visual Studio Code")
        self.assertEqual(steps[0], ["open", "-a", "Visual Studio Code", "/r"])
        self.assertEqual(steps[1], ["open", "vscode://anthropic.claude-code/open?prompt="
                                    + "%40%2Fx%20y%2Ftask.md%20%E3%82%92%E8%AA%AD%E3%82%93%E3%81%A7"])

    def test_terminal_steps_quote(self):
        steps = terminal_new_steps('/r/a"b', "@/x/task.md")
        script = steps[0][2]
        self.assertIn(applescript_quote(new_conversation_command('/r/a"b', "@/x/task.md")), script)

    def test_start_conversation(self):
        with tempfile.TemporaryDirectory() as t:
            d = os.path.realpath(t)
            conn = make_ledger(Path(d) / "tsuzuki")
            self.addCleanup(conn.close)
            task_dir = os.path.join(d, "tasks", "2026-11-20-a")
            repo = os.path.join(d, "r")
            os.makedirs(task_dir)
            os.makedirs(repo)
            task_md = os.path.join(task_dir, "task.md")
            conn.execute("INSERT INTO tasks (name, path, title, stage, repo_resolved) VALUES (?,?,?,?,?)",
                         ("2026-11-20-a", task_dir, "a", "調査", repo))
            calls = []

            def fake_runner(args, **kw):
                calls.append(args)
                return Proc(0)

            cfg = Config()
            out = start_conversation(conn, "2026-11-20-a", "vscode", cfg=cfg, runner=fake_runner, sleep=lambda s: None)
            self.assertTrue(out.ok)
            self.assertEqual(calls[0][:3], ["open", "-a", "Visual Studio Code"])
            out = start_conversation(conn, "2026-11-20-a", "copy", cfg=cfg, runner=fake_runner)
            self.assertEqual(out.command, new_conversation_command(
                repo, conversation_prompt(cfg.new_conversation_prompt, task_md)))
            self.assertEqual(len(calls), 2)
            shutil.rmtree(repo)
            out = start_conversation(conn, "2026-11-20-a", "copy", cfg=cfg, runner=fake_runner)
            self.assertTrue(out.command.startswith("cd " + shlex.quote(task_dir)))
            self.assertEqual(start_conversation(conn, "nope", "copy", cfg=cfg).message, "台帳にないタスクです")
            self.assertFalse(start_conversation(conn, "2026-11-20-a", "fax", cfg=cfg).ok)
            out = start_conversation(conn, "2026-11-20-a", "terminal", cfg=cfg, runner=fake_runner)
            self.assertEqual(calls[-1][0], "osascript")
            self.assertEqual(out.message, "Terminal で新しい会話を始めました")
            shutil.rmtree(task_dir)
            out = start_conversation(conn, "2026-11-20-a", "copy", cfg=cfg)
            self.assertFalse(out.ok)
            self.assertIn("作業ディレクトリが見つかりません", out.message)


if __name__ == "__main__":
    unittest.main()
