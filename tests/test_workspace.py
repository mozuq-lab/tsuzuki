import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsuzuki.privacy import PRIVACY_HINT
from tsuzuki.workspace import WorkspaceError, first_folder, strip_jsonc


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


if __name__ == "__main__":
    unittest.main()
