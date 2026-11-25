"""Tsuzuki.app を作る scripts/build_app.py のテスト。"""
import importlib.util
import io
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tsuzuki import __version__

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_app.py"
HASH_A = "A" * 40
HASH_B = "B" * 40


def load():
    spec = importlib.util.spec_from_file_location("tsuzuki_build_app", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class InfoPlistTest(unittest.TestCase):
    def test_info_plist(self):
        plist = plistlib.loads(load().info_plist("1.2.3"))
        self.assertEqual(plist, {
            "CFBundleIdentifier": "local.tsuzuki.app",
            "CFBundleName": "Tsuzuki",
            "CFBundleDisplayName": "Tsuzuki",
            "CFBundleExecutable": "Tsuzuki",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.2.3",
            "CFBundleVersion": "1.2.3",
            "CFBundleIconFile": "AppIcon",
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
            "NSAppleEventsUsageDescription": "Terminal で Claude Code のセッションを再開するために使います。",
            "NSRemovableVolumesUsageDescription": "外付けのディスクにある資料・ワークスペース・タスクの置き場所を読み書きするために使います。",
        })


class SigningIdentityTest(unittest.TestCase):
    def test_signing_identity_prefers_the_environment(self):
        runner = mock.Mock()
        self.assertEqual(load().signing_identity({"TSUZUKI_CODESIGN_IDENTITY": "X"}, runner), ("X", "X"))
        runner.assert_not_called()

    def test_signing_identity_picks_apple_development(self):
        out = ('  1) %s "Developer ID Application: A (T1)"\n'
               '  2) %s "Apple Development: B (T2)"\n'
               '     2 valid identities found\n' % (HASH_A, HASH_B))
        runner = mock.Mock(return_value=result(out))
        self.assertEqual(load().signing_identity({}, runner), (HASH_B, "Apple Development: B (T2)"))

    def test_signing_identity_falls_back_to_ad_hoc(self):
        module = load()
        no_match = mock.Mock(return_value=result('  1) %s "Developer ID Application: A (T1)"\n' % HASH_A))
        self.assertEqual(module.signing_identity({}, no_match), ("-", "ad-hoc"))
        for error in (subprocess.CalledProcessError(1, "security"), OSError("no security")):
            self.assertEqual(module.signing_identity({}, mock.Mock(side_effect=error)), ("-", "ad-hoc"))


class BuildAppTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))

    def test_missing_swiftc_fails_without_leaving_an_app(self):
        module = load()
        dist = self.tmp / "dist"
        err = io.StringIO()
        with mock.patch.object(module, "DIST", dist), mock.patch.object(module.shutil, "which", return_value=None), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = module.main()
        self.assertEqual(code, 1)
        self.assertIn("xcode-select --install", err.getvalue())
        self.assertFalse((dist / "Tsuzuki.app").exists())

    def test_failed_step_keeps_the_previous_app(self):
        module = load()
        out = self.tmp / "Tsuzuki.app"
        out.mkdir()
        (out / "old").write_text("old")

        def runner(cmd, **kwargs):
            if cmd[0] == "swiftc":
                raise subprocess.CalledProcessError(1, cmd, stderr="boom")
            return result()

        with self.assertRaises(subprocess.CalledProcessError):
            module.build_app(ROOT / "src", ROOT / "macos", out, identity="-", version="1.2.3", runner=runner)
        self.assertEqual((out / "old").read_text(), "old")

    @unittest.skipUnless(shutil.which("swiftc"), "swiftc がない")
    def test_builds_a_signed_app(self):
        module = load()
        out = module.build_app(ROOT / "src", ROOT / "macos", self.tmp / "Tsuzuki.app",
                               identity="-", version=__version__)
        contents = out / "Contents"
        for rel in ("Info.plist", "MacOS/Tsuzuki", "Resources/tsuzuki", "Resources/AppIcon.icns"):
            self.assertTrue((contents / rel).is_file(), rel)
        self.assertTrue(os.access(str(contents / "MacOS" / "Tsuzuki"), os.X_OK))
        self.assertEqual(plistlib.loads((contents / "Info.plist").read_bytes())["CFBundleShortVersionString"],
                         __version__)
        version = subprocess.run(["/usr/bin/python3", str(contents / "Resources" / "tsuzuki"), "--version"],
                                 capture_output=True, text=True, check=True)
        self.assertEqual(version.stdout.strip(), "tsuzuki %s" % __version__)
        subprocess.run(["codesign", "--verify", "--strict", str(out)], check=True)


if __name__ == "__main__":
    unittest.main()
