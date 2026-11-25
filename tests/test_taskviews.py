"""タスクの一覧とカードのデータ、tsuzuki task list。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tsuzuki.taskviews import (list_tasks, open_task_choices, origin_web_base, session_tasks, task_counts,
                              task_detail, task_lines, ticket_links)
from tests.fixtures import allow_system_tmp, make_ledger

HOME = "/Users/me"


class TaskViewsTest(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        self.addCleanup(self._restore_tz)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.root = self.tmp + "/tasks"
        os.makedirs(self.root)
        self.conn = make_ledger(Path(self.tmp))
        self.addCleanup(self.conn.close)
        self.n = 0

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def add_task(self, name, *, title=None, stage="チケット", stage_raw=None, waiting=0, sections=None, updated=None,
                 frontmatter_ok=1, repo=None, refs=()):
        self.n += 1
        d = "%s/%s" % (self.root, name)
        os.makedirs(d, exist_ok=True)
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks(name, path, title, stage, stage_raw, waiting, updated, frontmatter_ok, sections,"
                " repo, refs, md_mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, d, title or name, stage, stage_raw, waiting, updated, frontmatter_ok,
                 json.dumps(sections or {}, ensure_ascii=False), repo, json.dumps(list(refs)), 1000000.0 + self.n))
        return d

    def add_session(self, sid, kind="interactive"):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO sessions(id, transcript_path, kind) VALUES (?, ?, ?)",
                              (sid, "/x/%s.jsonl" % sid, kind))

    def link(self, task, sid, rule, confidence):
        self.add_session(sid)
        with self.conn:
            self.conn.execute("INSERT INTO task_sessions(task, session_id, rule, confidence) VALUES (?, ?, ?, ?)",
                              (task, sid, rule, confidence))

    def add_pr(self, sid, url):
        with self.conn:
            self.conn.execute("INSERT INTO session_prs(session_id, url) VALUES (?, ?)", (sid, url))

    def set_recap(self, sid, recap):
        with self.conn:
            self.conn.execute("UPDATE sessions SET recap = ? WHERE id = ?", (recap, sid))

    def set_end(self, sid, ended):
        with self.conn:
            self.conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (ended, sid))

    def add_prompt(self, sid, ts, text="依頼"):
        """本人の入力（messages の user の行）を足し、最後の依頼にする。"""
        with self.conn:
            seq = self.conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0]
            self.conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, ?, ?, ?, 'user', ?)",
                              (sid, seq, seq + 1, ts, text))
            self.conn.execute("UPDATE sessions SET last_prompt = ? WHERE id = ?", (text, sid))

    def set_recap_at(self, sid, recap, at):
        with self.conn:
            self.conn.execute("UPDATE sessions SET recap = ?, recap_at = ? WHERE id = ?", (recap, at, sid))

    def add_doc(self, path, kind="other", title=None, body=None):
        with self.conn:
            self.conn.execute("INSERT INTO documents(path, kind, ext, title, body) VALUES (?, ?, '.md', ?, ?)",
                              (path, kind, title, body))

    def add_doc_link(self, sid, path, rel, ts="2026-11-21T01:00:00Z"):
        with self.conn:
            self.conn.execute("INSERT INTO doc_links(session_id, path, rel, ts) VALUES (?, ?, ?, ?)",
                              (sid, path, rel, ts))

    def detail(self, name):
        return task_detail(self.conn, name, live={}, home=HOME, exclude_dirs=[], ticket_template=None)

    def test_status_and_order(self):
        self.add_task("a", stage="実装")
        self.add_task("b", stage="調整", waiting=1)
        self.add_task("c", stage="完了")
        self.assertEqual([(r.name, r.status) for r in list_tasks(self.conn, live={})],
                         [("b", "waiting"), ("a", "active")])
        self.assertEqual([r.name for r in list_tasks(self.conn, status="done", live={})], ["c"])
        self.assertEqual(len(list_tasks(self.conn, status="all", live={})), 3)
        self.assertEqual(task_counts(self.conn)[0], {"open": 2, "done": 1, "all": 3})

    def test_stage_from_pr_uses_strong_links_only(self):
        self.add_task("a", stage="調査")
        self.link("a", "s1", "wrote", "confirmed")
        self.add_pr("s1", "https://github.com/o/r/pull/12")
        self.add_task("b", stage="調査")
        self.link("b", "s2", "ticket", "weak")
        self.add_pr("s2", "https://github.com/o/r/pull/13")
        self.add_task("c", stage="修正")
        self.link("c", "s3", "wrote", "confirmed")
        self.add_pr("s3", "https://github.com/o/r/pull/14")
        rows = {r.name: r for r in list_tasks(self.conn, status="all", live={})}
        self.assertEqual((rows["a"].stage, rows["a"].stage_from_pr, rows["a"].pr_numbers), ("PR", True, ["12"]))
        self.assertEqual((rows["b"].stage, rows["b"].stage_from_pr), ("調査", False))
        self.assertEqual((rows["c"].stage, rows["c"].stage_label), ("修正", "修正"))

    def test_line2(self):
        self.add_task("a", sections={"次の一手": "\n- 結合テストを通す\n- 次"})
        self.add_task("w", waiting=1, sections={"待っていること": "経理の回答\n二行目"})
        self.add_task("r", sections={})
        self.link("r", "s1", "cwd", "confirmed")
        self.set_recap("s1", "要約の文")
        rows = {r.name: r for r in list_tasks(self.conn, status="all", live={})}
        self.assertEqual((rows["a"].line2_label, rows["a"].line2), ("次の一手", "結合テストを通す"))
        self.assertEqual((rows["w"].line2_label, rows["w"].line2), ("待っていること", "経理の回答"))
        self.assertEqual((rows["r"].line2_label, rows["r"].line2), ("要約", "要約の文"))

    def test_after_and_warnings(self):
        self.add_task("a", updated="2026-11-21T18:40", sections={"現在地": "x"})
        self.link("a", "s1", "cwd", "confirmed")
        self.add_prompt("s1", "2026-11-21T09:59:00Z")
        self.set_end("s1", "2026-11-21T10:00:00Z")
        self.set_recap_at("s1", "その後の要約", "2026-11-21T10:00:00Z")
        self.assertEqual(self.detail("a").after, ("その後の要約", "2026-11-21T10:00:00Z"))
        with self.conn:
            self.conn.execute("DELETE FROM messages")
        self.add_prompt("s1", "2026-11-21T09:00:00Z")
        self.set_end("s1", "2026-11-21T09:00:00Z")
        self.assertIsNone(self.detail("a").after)
        self.add_task("b", stage="チケット", stage_raw="実装中", frontmatter_ok=0, sections={})
        self.assertEqual(self.detail("b").warnings, ["task.md の見出し情報を読めません", "段階の値を読めません: 実装中",
                                                     "task.md に現在地がありません"])

    def test_after_without_current_section(self):
        self.add_task("a", updated="2026-11-21T18:40", sections={})
        self.link("a", "s1", "cwd", "confirmed")
        self.set_end("s1", "2026-11-21T08:00:00Z")
        self.set_recap("s1", "最新の要約")
        d = self.detail("a")
        self.assertEqual(d.after, ("最新の要約", "2026-11-21T08:00:00Z"))
        self.assertIn("task.md に現在地がありません", d.warnings)

    def test_after_ignores_the_handoff_session_itself(self):
        # 引き継ぎのスキルは updated を分までで書き、同じセッションがその後も応答するので、終わりの時刻は updated
        # より後になる。最後の依頼（引き継ぎの依頼）が updated の分のうちなら「その後」ではない
        self.add_task("a", updated="2026-11-22T18:40", sections={"現在地": "引き継いだ所"})
        self.link("a", "s1", "cwd", "confirmed")
        self.add_prompt("s1", "2026-11-22T09:20:00Z", "実装して")
        self.add_prompt("s1", "2026-11-22T09:40:10Z", "引き継ぎを書いて")
        self.set_end("s1", "2026-11-22T09:40:35Z")
        self.assertIsNone(self.detail("a").after)
        # 要約が updated の分より前なら、それも「その後」ではない
        self.set_recap_at("s1", "前の要約", "2026-11-22T09:30:00Z")
        self.assertIsNone(self.detail("a").after)
        # updated の分の後に依頼があれば、その後の会話。要約が古ければ最後の依頼を出す
        self.add_prompt("s1", "2026-11-22T09:45:00Z", "次を頼む")
        self.set_end("s1", "2026-11-22T09:46:00Z")
        self.assertEqual(self.detail("a").after, ("次を頼む", "2026-11-22T09:46:00Z"))
        # 要約が updated の分の後なら、要約を出す
        self.set_recap_at("s1", "新しい要約", "2026-11-22T09:46:00Z")
        self.assertEqual(self.detail("a").after, ("新しい要約", "2026-11-22T09:46:00Z"))

    def test_after_without_prompts_uses_the_start(self):
        # 本人の入力の記録がなければ、セッションの始まりの時刻で決める
        self.add_task("a", updated="2026-11-22T18:40", sections={"現在地": "x"})
        self.link("a", "s1", "cwd", "confirmed")
        self.set_recap("s1", "要約")
        self.set_end("s1", "2026-11-22T10:00:00Z")
        with self.conn:
            self.conn.execute("UPDATE sessions SET started_at = '2026-11-22T09:40:30Z', last_prompt = '最後' "
                              "WHERE id = 's1'")
        self.assertIsNone(self.detail("a").after)
        with self.conn:
            self.conn.execute("UPDATE sessions SET started_at = '2026-11-22T09:50:00Z' WHERE id = 's1'")
        self.assertEqual(self.detail("a").after, ("最後", "2026-11-22T10:00:00Z"))

    def test_default_branches_setting(self):
        self.add_task("a", sections={"現在地": "x"})
        self.link("a", "s1", "cwd", "confirmed")
        with self.conn:
            self.conn.execute("UPDATE sessions SET branch = 'trunk' WHERE id = 's1'")
        self.assertEqual(self.detail("a").branch, "trunk")
        kw = dict(live={}, home=HOME, exclude_dirs=[], ticket_template=None)
        self.assertIsNone(task_detail(self.conn, "a", default_branches=["trunk"], **kw).branch)
        with self.conn:
            self.conn.execute("UPDATE sessions SET branch = 'main' WHERE id = 's1'")
        self.assertEqual(task_detail(self.conn, "a", default_branches=["trunk"], **kw).branch, "main")

    def test_max_depth_setting(self):
        d = self.add_task("a")
        os.makedirs(d + "/sub")
        Path(d, "sub", "deep.md").write_text("x")
        kw = dict(live={}, home=HOME, exclude_dirs=[], ticket_template=None)
        self.assertEqual(len(task_detail(self.conn, "a", **kw).folder_files), 1)
        self.assertEqual(len(task_detail(self.conn, "a", max_depth=1, **kw).folder_files), 0)

    def test_excerpt_skips_front_matter(self):
        with allow_system_tmp():
            d = self.add_task("a")
            Path(d, "task.md").write_text("x")
            body = "---\ntitle: 表紙の題\nstage: 調査\n---\n\n# 表紙の題\n\n## 目的\n最初の段落です。\n"
            self.add_doc(d + "/task.md", "cover", "表紙の題", body)
            d_ = self.detail("a")
        cover = d_.folder_files[0]
        self.assertEqual(cover.rel_path, "task.md")
        self.assertEqual(cover.excerpt, "## 目的 最初の段落です。")

    def test_three_bundles(self):
        with allow_system_tmp():
            d = self.add_task("a", refs=["/r/missing.md"])
            for name in ("task.md", "research-a.md", "shot.png"):
                Path(d, name).write_text("x")
            os.makedirs(self.root + "/shared")
            Path(self.root, "shared", "notes-team.md").write_text("x")
            self.add_doc(d + "/task.md", "cover", "表紙の題")
            self.add_doc(d + "/research-a.md", "research", "調査の題", "# 調査の題\n\n冒頭の段落です。\n続き\n\n次")
            self.add_doc(self.root + "/shared/notes-team.md", "notes")
            self.add_doc("/r/docs/api.md", "readme")
            self.add_doc("/r/docs/other.md", "readme")
            self.link("a", "s1", "cwd", "confirmed")
            self.link("a", "s2", "ticket", "weak")
            self.add_doc_link("s1", self.root + "/shared/notes-team.md", "read")
            self.add_doc_link("s1", "/r/docs/api.md", "read")
            self.add_doc_link("s2", "/r/docs/other.md", "read")
            d_ = self.detail("a")
        self.assertEqual([f.rel_path for f in d_.folder_files], ["task.md", "research-a.md", "shot.png"])
        self.assertEqual([f.kind_label for f in d_.folder_files], ["表紙", "調査", "その他"])
        self.assertEqual((d_.folder_files[1].title, d_.folder_files[1].excerpt), ("調査の題", "冒頭の段落です。 続き"))
        self.assertEqual([f.path for f in d_.shared_docs], [self.root + "/shared/notes-team.md"])
        self.assertEqual([(f.path, f.missing) for f in d_.other_docs], [("/r/docs/api.md", False),
                                                                        ("/r/missing.md", True)])
        self.assertEqual(d_.other_docs[0].relation, "読んだ")
        self.assertEqual(sorted(s.confidence for s in d_.sessions), ["confirmed", "weak"])

    def test_other_docs_know_when_they_were_copied(self):
        with allow_system_tmp():
            d = self.add_task("a")
            src = self.root + "/outside"
            os.makedirs(src)
            for name, body in (("same.md", "abc"), ("diff.md", "abc"), ("none.md", "abc")):
                Path(src, name).write_text(body)
                self.add_doc(src + "/" + name, "readme")
                self.add_doc_link("s1", src + "/" + name, "read")
            Path(d, "same.md").write_text("xyz")  # 中身は違っても、同じ名前で同じ大きさ
            Path(d, "diff.md").write_text("abcd")
            os.makedirs(d + "/none.md.d")
            self.link("a", "s1", "cwd", "confirmed")
            found = {i.path.rsplit("/", 1)[1]: i.copied for i in self.detail("a").other_docs}
        self.assertEqual(found, {"same.md": True, "diff.md": False, "none.md": False})

    def test_shared_and_other_bundles_are_capped(self):
        # 共通の資料とほかの場所の資料は、新しい順に50件まで。全部の数も渡す
        with allow_system_tmp():
            self.add_task("a")
            os.makedirs(self.root + "/shared")
            self.link("a", "s1", "cwd", "confirmed")
            for i in range(53):
                ts = "2026-11-21T%02d:%02d:00Z" % (i // 60, i % 60)
                self.add_doc("/r/docs/o%02d.md" % i, "readme")
                self.add_doc_link("s1", "/r/docs/o%02d.md" % i, "read", ts=ts)
                if i < 51:
                    path = "%s/shared/notes-%02d.md" % (self.root, i)
                    Path(path).write_text("x")
                    self.add_doc(path, "notes")
                    self.add_doc_link("s1", path, "read", ts=ts)
            d = self.detail("a")
        self.assertEqual((len(d.other_docs), d.other_total), (50, 53))
        self.assertEqual((len(d.shared_docs), d.shared_total), (50, 51))
        self.assertEqual(d.other_docs[0].path, "/r/docs/o52.md")
        self.assertEqual(d.other_docs[-1].path, "/r/docs/o03.md")
        self.assertEqual(d.shared_docs[0].path, "%s/shared/notes-50.md" % self.root)

    def test_sessions_fold_headless_children(self):
        self.add_task("a")
        self.link("a", "p", "cwd", "confirmed")
        self.link("a", "h", "from_child", "strong")
        self.link("a", "o", "cwd", "confirmed")
        self.add_session("p")
        with self.conn:
            self.conn.execute("UPDATE sessions SET kind='headless' WHERE id IN ('h', 'o')")
            self.conn.execute("INSERT INTO relations(src, dst, type, confidence, evidence) VALUES "
                              "('p', 'h', 'child', 'confirmed', 'x')")
        items = {i.row.id: i for i in self.detail("a").sessions}
        self.assertEqual(sorted(items), ["o", "p"])
        self.assertEqual(items["p"].n_children, 1)
        self.assertEqual(items["o"].row.kind, "headless")

    def test_ticket_links(self):
        self.assertEqual(ticket_links("#12", repo=None, template="https://x.example/issues/{n}"),
                         [("#12", "https://x.example/issues/12")])
        self.assertEqual(ticket_links("#12, 13、https://x/i/5, Redmine 7", repo=None, template="https://t/{n}"),
                         [("#12", "https://t/12"), ("#13", "https://t/13"), ("https://x/i/5", "https://x/i/5"),
                          ("Redmine 7", None)])
        self.assertEqual(ticket_links("https://github.com/o/r/issues/5", repo=None, template=None),
                         [("https://github.com/o/r/issues/5", "https://github.com/o/r/issues/5")])
        self.assertEqual(ticket_links(None, repo=None, template=None), [])
        self.assertEqual(ticket_links("#1, #2", repo=None, template=None), [("#1", None), ("#2", None)])
        for i, (origin, base) in enumerate((("git@github.com:o/r.git", "https://github.com/o/r"),
                                            ("https://github.com/o/r.git", "https://github.com/o/r"),
                                            ("ssh://git@ghe.example.com/o/r.git", "https://ghe.example.com/o/r"))):
            repo = "%s/repo%d" % (self.tmp, i)
            os.makedirs(repo + "/.git")
            Path(repo, ".git", "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = %s\n' % origin)
            self.assertEqual(ticket_links("#12, #13", repo=repo, template=None),
                             [("#12", base + "/issues/12"), ("#13", base + "/issues/13")])
        bare = self.tmp + "/norepo"
        os.makedirs(bare + "/.git")
        Path(bare, ".git", "config").write_text("[core]\n")
        self.assertEqual(ticket_links("#12", repo=bare, template=None), [("#12", None)])

    def test_origin_from_worktree(self):
        main = self.tmp + "/main"
        os.makedirs(main + "/.git/worktrees/w")
        Path(main, ".git", "config").write_text('[remote "origin"]\n\turl = git@github.com:o/r.git\n')
        wt = self.tmp + "/wt"
        os.makedirs(wt)
        Path(wt, ".git").write_text("gitdir: %s/.git/worktrees/w\n" % main)
        self.assertEqual(origin_web_base(wt), "https://github.com/o/r")

    def test_session_tasks_and_choices(self):
        self.add_task("a", title="A の題")
        self.add_task("c", stage="完了")
        self.link("a", "s1", "ticket", "weak")
        self.assertEqual(session_tasks(self.conn, "s1"), [("a", "A の題", "チケット番号", "推定（弱）")])
        self.assertEqual(open_task_choices(self.conn), [("a", "A の題")])
        # セッションに足せるのは、まだ結び付いていない進行中と待ちのタスク
        self.assertEqual(open_task_choices(self.conn, "s1"), [])
        self.add_task("b", title="B の題", waiting=1)
        self.assertEqual(open_task_choices(self.conn, "s1"), [("b", "B の題")])
        self.assertEqual(open_task_choices(self.conn, "s2"), [("b", "B の題"), ("a", "A の題")])

    def test_task_lines(self):
        for name, stage in (("2026-11-20-a", "実装"), ("2026-11-21-c", "完了")):
            os.makedirs("%s/%s" % (self.root, name))
            Path(self.root, name, "task.md").write_text("---\ntitle: %s\nstage: %s\n---\n" % (name[-1].upper(), stage))
        self.assertEqual(task_lines(self.root, include_done=False),
                         ["進行中\t実装・テスト\tA\t%s/2026-11-20-a" % self.root])
        self.assertEqual(len(task_lines(self.root, include_done=True)), 2)


if __name__ == "__main__":
    unittest.main()
