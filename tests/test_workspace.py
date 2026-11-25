import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki.privacy import PRIVACY_HINT
from tsuzuki.workspace import WorkspaceError, ensure_self_first, first_folder, strip_jsonc


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        (self.base / "work" / "app").mkdir(parents=True)
        (self.base / "work" / "ws").mkdir()

    def write(self, text, name="a.code-workspace"):
        path = self.base / "work" / "ws" / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_first_folder_relative_and_absolute(self):
        rel = self.write(json.dumps({"folders": [{"path": "../app"}, {"path": "/nowhere"}]}))
        self.assertEqual(first_folder(rel), str(self.base / "work" / "app"))
        absolute = self.write(json.dumps({"folders": [{"path": str(self.base / "work" / "app")}]}), "b.code-workspace")
        self.assertEqual(first_folder(absolute), str(self.base / "work" / "app"))

    def test_jsonc_comments_and_trailing_commas(self):
        path = self.write('// 先頭\n{\n  /* 囲み */ "folders": [{"path": "../app",},], // 末尾\n}\n')
        self.assertEqual(first_folder(path), str(self.base / "work" / "app"))
        text = '{"path": "http://x//y", "a": "/* no */, ]"}'
        self.assertEqual(json.loads(strip_jsonc(text)), {"path": "http://x//y", "a": "/* no */, ]"})
        self.assertEqual(json.loads(strip_jsonc('{"a": "q\\"//z",}')), {"a": 'q"//z'})

    def test_blocked_read_points_to_the_privacy_setting(self):
        path = self.write("{}")
        with mock.patch("builtins.open", side_effect=PermissionError(errno.EPERM, "Operation not permitted", path)):
            with self.assertRaises(WorkspaceError) as cm:
                first_folder(path)
        self.assertTrue(str(cm.exception).startswith("読めません（"), str(cm.exception))
        self.assertIn(PRIVACY_HINT, str(cm.exception))
        with self.assertRaises(WorkspaceError) as cm:
            first_folder(str(self.base / "missing.code-workspace"))
        self.assertNotIn(PRIVACY_HINT, str(cm.exception))

    def test_errors(self):
        with self.assertRaises(WorkspaceError) as cm:
            first_folder(str(self.base / "missing.code-workspace"))
        self.assertTrue(str(cm.exception).startswith("読めません（"), str(cm.exception))
        with self.assertRaises(WorkspaceError) as cm:
            first_folder(self.write("{ not json"))
        self.assertEqual(str(cm.exception), "JSON の形が正しくありません")
        for text in ("[]", "{}", '{"folders": []}', '{"folders": [5]}', '{"folders": [{}]}'):
            with self.assertRaises(WorkspaceError, msg=text) as cm:
                first_folder(self.write(text))
            self.assertEqual(str(cm.exception), "folders がありません", text)
        gone = self.write('{"folders": [{"path": "../nope"}]}')
        with self.assertRaises(WorkspaceError) as cm:
            first_folder(gone)
        self.assertEqual(str(cm.exception), "最初のフォルダーがありません: %s" % (self.base / "work" / "nope"))


class EnsureSelfFirstTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(os.path.realpath(self._tmp.name)) / "ws"
        self.dir.mkdir()
        (self.dir / "app").mkdir()

    def write(self, text, name="a.code-workspace"):
        path = self.dir / name
        path.write_bytes(text.encode("utf-8"))
        return str(path)

    def test_inserts_one_line_and_keeps_everything_else(self):
        for eol in ("\n", "\r\n"):
            text = ('// 先頭のコメント{e}{{{e}\t/* "folders": [ */{e}\t"note": "folders [",{e}\t"folders": [ // 一覧{e}'
                    '\t\t{{ "path": "app" }},{e}\t\t// 後ろ{e}\t],{e}\t"settings": {{}},{e}}}{e}').format(e=eol)
            path = self.write(text)
            os.chmod(path, 0o640)
            self.assertEqual(ensure_self_first(path), "added")
            expected = text.replace("// 一覧" + eol, "// 一覧" + eol + '\t\t{ "path": "." },' + eol, 1)
            self.assertEqual(Path(path).read_bytes().decode("utf-8"), expected)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
            self.assertEqual(first_folder(path), str(self.dir))
            self.assertEqual(ensure_self_first(path), "present")

    def test_inline_and_empty_arrays(self):
        path = self.write('{"folders": [{"path": "app"}]}')
        self.assertEqual(ensure_self_first(path), "added")
        self.assertEqual(first_folder(path), str(self.dir))
        self.assertEqual(Path(path).read_text(encoding="utf-8"), '{"folders": [ { "path": "." }, {"path": "app"}]}')
        path = self.write('{"folders": [ /* c */ ]}', "c.code-workspace")
        self.assertEqual(ensure_self_first(path), "added")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), '{"folders": [ { "path": "." }  /* c */ ]}')
        path = self.write('{"folders": [\n\t// c\n]}', "d.code-workspace")
        self.assertEqual(ensure_self_first(path), "added")
        self.assertEqual(first_folder(path), str(self.dir))
        self.assertNotIn('},', Path(path).read_text(encoding="utf-8"))
        path = self.write('{\n  "folders": [\n  ]\n}', "b.code-workspace")
        self.assertEqual(ensure_self_first(path), "added")
        self.assertEqual(first_folder(path), str(self.dir))
        self.assertEqual(Path(path).read_text(encoding="utf-8"), '{\n  "folders": [\n  \t{ "path": "." }\n  ]\n}')

    def test_present_leaves_the_file_untouched(self):
        for i, first in enumerate((".", "./", str(self.dir), "app/..", "../ws")):
            text = json.dumps({"folders": [{"path": first}, {"path": "app"}]})
            path = self.write(text, "p%d.code-workspace" % i)
            os.utime(path, (1000000, 1000000))
            self.assertEqual(ensure_self_first(path), "present", first)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
            self.assertEqual(os.stat(path).st_mtime, 1000000)

    def test_elsewhere_leaves_the_file_untouched(self):
        text = json.dumps({"folders": [{"path": "app"}, {"path": "."}]})
        path = self.write(text)
        os.utime(path, (1000000, 1000000))
        self.assertEqual(ensure_self_first(path), "elsewhere")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
        self.assertEqual(os.stat(path).st_mtime, 1000000)

    def test_read_only_file_is_not_replaced(self):
        text = '{"folders": [{"path": "app"}]}'
        path = self.write(text)
        os.chmod(path, 0o444)
        with self.assertRaises(WorkspaceError) as cm:
            ensure_self_first(path)
        self.assertTrue(str(cm.exception).startswith("書けません（"), str(cm.exception))
        self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o444)

    def test_same_folder_by_another_spelling_is_present(self):
        upper = self.dir.parent / "WS"
        if not upper.exists():
            self.skipTest("case-sensitive file system")
        path = self.write(json.dumps({"folders": [{"path": "../WS"}]}))
        self.assertEqual(ensure_self_first(path), "present")

    def test_claude_config_dir_is_refused(self):
        text = '{"folders": [{"path": "app"}]}'
        path = self.write(text)
        with self.assertRaises(WorkspaceError) as cm:
            ensure_self_first(path, claude_dir=self.dir.parent)
        self.assertEqual(str(cm.exception), "Claude Code の設定ディレクトリの中のワークスペースには足せません")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
        self.assertEqual(ensure_self_first(path, claude_dir=self.dir / "other"), "added")

    def test_symlink_is_refused(self):
        real = self.write('{"folders": [{"path": "app"}]}', "real.code-workspace")
        link = self.dir / "link.code-workspace"
        os.symlink(real, str(link))
        with self.assertRaises(WorkspaceError) as cm:
            ensure_self_first(str(link))
        self.assertEqual(str(cm.exception), "シンボリックリンクのワークスペースには足せません")
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(real).read_text(encoding="utf-8"), '{"folders": [{"path": "app"}]}')

    def test_unparsable_or_without_folders_is_refused(self):
        for text in ("{ not json", "{}", '{"folders": 5}'):
            path = self.write(text)
            with self.assertRaises(WorkspaceError, msg=text):
                ensure_self_first(path)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), text)

    def test_write_failures_name_the_cause(self):
        text = '{"folders": [{"path": "app"}]}'
        path = self.write(text)
        with mock.patch("tempfile.mkstemp", side_effect=PermissionError(errno.EPERM, "Operation not permitted")):
            with self.assertRaises(WorkspaceError) as cm:
                ensure_self_first(path)
        self.assertTrue(str(cm.exception).startswith("書けません（"), str(cm.exception))
        self.assertIn(PRIVACY_HINT, str(cm.exception))
        os.chmod(str(self.dir), 0o555)
        self.addCleanup(os.chmod, str(self.dir), 0o755)
        with self.assertRaises(WorkspaceError) as cm:
            ensure_self_first(path)
        self.assertTrue(str(cm.exception).startswith("書けません（"), str(cm.exception))
        self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name != "app"], ["a.code-workspace"])


if __name__ == "__main__":
    unittest.main()
