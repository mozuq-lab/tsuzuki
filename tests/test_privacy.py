import errno
import unittest

from tsuzuki.privacy import PRIVACY_HINT, os_error_text


class OsErrorTextTest(unittest.TestCase):
    def test_eperm_gets_the_hint(self):
        e = PermissionError(errno.EPERM, "Operation not permitted", "/x")
        text = os_error_text(e)
        self.assertTrue(text.startswith(str(e)))
        self.assertTrue(text.endswith(PRIVACY_HINT))

    def test_other_errors_are_plain(self):
        for e in (PermissionError(errno.EACCES, "Permission denied", "/x"), FileNotFoundError(errno.ENOENT, "No such file", "/x")):
            self.assertEqual(os_error_text(e), str(e))
