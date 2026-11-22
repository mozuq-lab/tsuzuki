import os
import tempfile
import unittest
from pathlib import Path

from tsuzuki.config import DEFAULT_DOC_EXTENSIONS
from tsuzuki.documents import (KIND_LABELS, KINDS, MAX_BODY_BYTES, allowed_doc_path, classify_kind, extract_text,
                              is_document, scan_documents, should_scan_place, upsert_document)
from tests.fixtures import allow_system_tmp, count, make_ledger, write_file

HOME = "/Users/u"
KIND_CASES = [
    ("/r/docs/superpowers/handoff/2026-11-12-recipe-handoff.md", "handoff"),
    ("/r/docs/superpowers/plans/2026-11-08-weather-app-progress.md", "plan"),
    ("/r/docs/superpowers/specs/2026-11-15-recipe-site-design.md", "design"),
    ("/r/.superpowers/recipe-site/progress.md", "progress"),
    ("/r/.superpowers/coordinator/learnings-recipe.md", "review"),
    ("/r/.superpowers/coordinator/review-html.md", "review"),
    ("/r/HANDOFF.md", "handoff"),
    ("/r/作業再開メモ.md", "handoff"),
    ("/r/README.md", "readme"),
    ("/r/AGENTS.md", "readme"),
    ("/r/docs/explanation.md", "other"),
    (HOME + "/.claude/projects/-r/memory/MEMORY.md", "memory"),
    (HOME + "/.claude/plans/joyful-forging-wilkinson.md", "plan"),
]


class DocumentsTest(unittest.TestCase):
    def test_kind_labels_follow_kinds_order(self):
        self.assertEqual(list(KIND_LABELS), list(KINDS))

    def test_task_kinds(self):
        root = "/Users/me/tasks"
        self.assertEqual(classify_kind(root + "/2026-11-20-x/research-a.md", "/Users/me", root), "research")
        self.assertEqual(classify_kind(root + "/2026-11-20-x/task.md", "/Users/me", root), "cover")
        self.assertEqual(classify_kind(root + "/2026-11-20-x/handoff.md", "/Users/me", root), "handoff")
        self.assertEqual(classify_kind("/Users/me/dev/w/plan-a.md", "/Users/me", root), "plan")
        self.assertEqual(classify_kind(root + "/2026-11-20-x/research-a.md", "/Users/me"), "other")
        self.assertEqual([KIND_LABELS[k] for k in ("cover", "research", "response", "notes")],
                         ["表紙", "調査", "レビュー対応", "メモ"])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)

    def test_kinds(self):
        for path, kind in KIND_CASES:
            with self.subTest(path=path):
                self.assertEqual(classify_kind(path, HOME), kind)

    def test_is_document(self):
        self.assertTrue(is_document("/r/A.MD", DEFAULT_DOC_EXTENSIONS))
        self.assertFalse(is_document("/r/a.py", DEFAULT_DOC_EXTENSIONS))

    def test_allowed_paths(self):
        self.assertFalse(allowed_doc_path("/private/tmp/claude-501/x/a.md", HOME))
        self.assertFalse(allowed_doc_path(HOME + "/.claude/settings.md", HOME))
        self.assertTrue(allowed_doc_path(HOME + "/.claude/projects/-r/memory/a.md", HOME))
        self.assertTrue(allowed_doc_path(HOME + "/.claude/plans/a.md", HOME))
        self.assertFalse(allowed_doc_path(HOME + "/.claude/projects/-r/x.md", HOME))
        self.assertTrue(allowed_doc_path("/r/docs/a.md", HOME))

    def test_scan_respects_excludes_depth_and_limit(self):
        root = os.path.join(self.tmp, "root")
        for rel in ["docs/ok.md", "node_modules/x.md", ".git/y.md", "a/b/c/d/e/f/g/deep.md"]:
            write_file(os.path.join(root, rel), "x")
        found = scan_documents(root, exts=DEFAULT_DOC_EXTENSIONS, exclude_dirs=["node_modules", ".git"],
                               max_depth=6, max_files=3000)
        self.assertEqual([os.path.relpath(p, root) for p in found], ["docs/ok.md"])
        self.assertEqual(len(scan_documents(root, exts=DEFAULT_DOC_EXTENSIONS, exclude_dirs=[],
                                            max_depth=10, max_files=1)), 1)
        deep = scan_documents(root, exts=DEFAULT_DOC_EXTENSIONS, exclude_dirs=["node_modules", ".git"],
                              max_depth=7, max_files=3000)
        self.assertIn("a/b/c/d/e/f/g/deep.md", [os.path.relpath(p, root) for p in deep])

    def test_scan_skips_symlink_dirs(self):
        root = os.path.join(self.tmp, "root")
        write_file(os.path.join(root, "real", "a.md"), "x")
        os.symlink(os.path.join(root, "real"), os.path.join(root, "link"))
        found = scan_documents(root, exts=[".md"], exclude_dirs=[], max_depth=6, max_files=10)
        self.assertEqual([os.path.relpath(p, root) for p in found], ["real/a.md"])

    def test_should_scan_place(self):
        self.assertFalse(should_scan_place(HOME, HOME))
        self.assertFalse(should_scan_place("/", HOME))
        self.assertFalse(should_scan_place("/Volumes", HOME))
        self.assertFalse(should_scan_place("/Volumes/external", HOME))
        self.assertFalse(should_scan_place("/Volumes/external/", HOME))
        self.assertTrue(should_scan_place("/Volumes/external/dev/x", HOME))

    def test_extract_html(self):
        p = os.path.join(self.tmp, "a.html")
        write_file(p, "<title>T</title><script>evil()</script><style>.x{}</style><h1>H</h1><p>本文</p>")
        title, body = extract_text(p)
        self.assertEqual(title, "T")
        self.assertIn("本文", body)
        self.assertNotIn("evil", body)
        self.assertNotIn(".x", body)

    def test_extract_html_h1_fallback(self):
        p = os.path.join(self.tmp, "b.html")
        write_file(p, "<h1>Head</h1><p>x</p>")
        self.assertEqual(extract_text(p)[0], "Head")

    def _html(self, name, text):
        p = os.path.join(self.tmp, name)
        write_file(p, text)
        return extract_text(p)

    def test_extract_html_inline_and_blocks(self):
        self.assertIn("本文の重要な点", self._html("c.html", "<p>本文の<b>重要</b>な点</p>")[1])
        self.assertEqual(self._html("d.html", "<p>一</p><p>二</p>")[1], "一 二")

    def test_extract_html_title_ignores_svg(self):
        title, _ = self._html("e.html", "<html><head><title>T</title></head><body><svg><title>icon</title></svg><p>x</p></body></html>")
        self.assertEqual(title, "T")
        title, _ = self._html("f.html", "<body><svg><title>icon</title></svg><h1>見<i>出</i>し</h1></body>")
        self.assertEqual(title, "見出し")

    def test_extract_markdown_title_and_cap(self):
        md = os.path.join(self.tmp, "a.md")
        write_file(md, "# 見出し\n本文")
        self.assertEqual(extract_text(md)[0], "見出し")
        big = os.path.join(self.tmp, "big.md")
        write_file(big, "# t\n" + "あ" * 200000)
        self.assertLessEqual(len(extract_text(big)[1].encode("utf-8")), MAX_BODY_BYTES)

    def test_extract_other(self):
        self.assertEqual(extract_text(os.path.join(self.tmp, "x.pdf")), (None, None))
        self.assertEqual(extract_text(os.path.join(self.tmp, "missing.md")), (None, None))
        t = os.path.join(self.tmp, "a.txt")
        write_file(t, "hello")
        self.assertEqual(extract_text(t), (None, "hello"))

    def test_upsert_document(self):
        with allow_system_tmp():
            self._upsert_flow()

    def test_allow_system_tmp(self):
        p = os.path.join(tempfile.gettempdir(), "a.md")
        self.assertFalse(allowed_doc_path(p, HOME))
        with allow_system_tmp():
            self.assertTrue(allowed_doc_path(p, HOME))
            self.assertFalse(allowed_doc_path("/private/tmp/claude-501/x/a.md", HOME))
        self.assertFalse(allowed_doc_path(p, HOME))

    def _upsert_flow(self):
        conn = make_ledger(Path(self.tmp))
        p = os.path.join(self.tmp, "docs", "plans", "a.md")
        write_file(p, "# 一\n本文")
        exts = DEFAULT_DOC_EXTENSIONS
        i = upsert_document(conn, p, place_id=None, home=HOME, exts=exts)
        self.assertIsNotNone(i)
        row = conn.execute("SELECT kind, ext, title, body, place_id FROM documents WHERE id=?", (i,)).fetchone()
        self.assertEqual(tuple(row)[:3], ("plan", ".md", "一"))
        # 変更なしなら本文を読み直さない（本文を直接書き換えて確かめる）
        conn.execute("UPDATE documents SET body='SENTINEL' WHERE id=?", (i,))
        conn.commit()
        self.assertEqual(upsert_document(conn, p, place_id=None, home=HOME, exts=exts), i)
        self.assertEqual(conn.execute("SELECT body FROM documents WHERE id=?", (i,)).fetchone()[0], "SENTINEL")
        # 変更あり
        write_file(p, "# 二\n長くした本文")
        os.utime(p, (1, 1))
        self.assertEqual(upsert_document(conn, p, place_id=None, home=HOME, exts=exts), i)
        self.assertEqual(conn.execute("SELECT title FROM documents WHERE id=?", (i,)).fetchone()[0], "二")
        self.assertEqual(count(conn, "documents"), 1)
        # 消えたら行も消える
        os.remove(p)
        self.assertIsNone(upsert_document(conn, p, place_id=None, home=HOME, exts=exts))
        self.assertEqual(count(conn, "documents"), 0)


if __name__ == "__main__":
    unittest.main()
