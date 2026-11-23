"""1つの実行ファイルにまとめる scripts/build.py と、ログイン時に起動する launchd の設定のテスト。"""
import importlib.util
import os
import plistlib
import socket
import stat
import subprocess
import tempfile
import time
import unittest
import zipfile
from http.client import HTTPConnection
from pathlib import Path

from tsuzuki import __version__
from tsuzuki.server import COOKIE, TOKEN_FILE
from tests.fixtures import FakeClaude, assistant, sid, t, user

ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = ROOT / "scripts" / "build.py"
PLIST = ROOT / "docs" / "launchd" / "local.tsuzuki.serve.plist"
START_SECONDS = 10  # serve が待ち受けを始めるまで待つ上限
# 実行ファイルに外から効く環境変数。テストでは渡さない（PYTHONPATH があると src/ を読んでしまう）
OUTSIDE_ENV = ("PYTHONPATH", "TSUZUKI_HOME", "TSUZUKI_PORT", "CLAUDE_CONFIG_DIR")


def build(src: Path, out: Path) -> Path:
    spec = importlib.util.spec_from_file_location("tsuzuki_build", str(BUILD_SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(src, out)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BuildTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in OUTSIDE_ENV}
        env["HOME"] = str(self.home)
        env.update(extra)
        return env

    def run_app(self, args):
        return subprocess.run([str(a) for a in args], capture_output=True, text=True, env=self.env(),
                              cwd=str(self.root), timeout=60)

    def test_leaves_out_bytecode_and_passes_on_the_exit_code(self):
        pkg = self.root / "src" / "tsuzuki"
        (pkg / "__pycache__").mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "cli.py").write_text("def main():\n    print('main')\n    return 3\n")
        (pkg / "__pycache__" / "cli.cpython-39.pyc").write_bytes(b"stale")
        (pkg / "old.pyc").write_bytes(b"stale")
        out = build(self.root / "src", self.root / "dist" / "tsuzuki")
        self.assertEqual(out, self.root / "dist" / "tsuzuki")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o755)
        self.assertEqual(out.read_bytes().split(b"\n", 1)[0], b"#!/usr/bin/env python3")
        with zipfile.ZipFile(str(out)) as z:
            files = sorted(name for name in z.namelist() if not name.endswith("/"))
        self.assertEqual(files, ["__main__.py", "tsuzuki/__init__.py", "tsuzuki/cli.py"])
        p = self.run_app([out])
        self.assertEqual((p.returncode, p.stdout, p.stderr), (3, "main\n", ""))

    def test_zipapp_runs_and_reads_assets(self):
        out = build(ROOT / "src", self.root / "tsuzuki")
        self.assertTrue(os.access(str(out), os.X_OK))
        fake = FakeClaude(self.root)
        work = str(self.root / "work")
        os.makedirs(work)
        fake.write(work, sid(1), [user("続きを作る", t(0), sid(1), work), assistant(t(1), sid(1), work, text="作りました")])
        ledger = self.root / "ledger"
        app = [out, "--claude-dir", fake.dir, "--ledger-dir", ledger]

        p = self.run_app(app + ["--version"])
        self.assertEqual(p.stdout.strip(), "tsuzuki %s" % __version__)
        p = self.run_app(app + ["forget", "abc"])
        self.assertEqual((p.returncode, p.stderr.strip()), (2, "ID は6文字以上で指定してください"))
        p = self.run_app(app + ["index"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("記録 1件（取り込み 1件）"), p.stdout)

        # 画面の資材は zip の中から読む（serve の台帳は TSUZUKI_HOME で渡す）
        port = free_port()
        log_path = self.root / "serve.log"
        with open(str(log_path), "wb") as log:
            proc = subprocess.Popen([str(out), "--claude-dir", str(fake.dir), "serve", "--no-open", "--port", str(port)],
                                    stdout=log, stderr=subprocess.STDOUT, env=self.env(TSUZUKI_HOME=str(ledger)),
                                    cwd=str(self.root))
            try:
                token = self.wait_until_serving(proc, port, ledger / TOKEN_FILE)
                self.assertIsNotNone(token, log_path.read_text(encoding="utf-8", errors="replace"))
                status, body = self.get(port, "/static/app.js", token)
                frame_status, frame = self.get(port, "/mermaid-frame", token)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / "src" / "tsuzuki" / "ui" / "app.js").read_bytes())
        # Mermaid の枠のページも、zip の中のライブラリから組み立てる
        self.assertEqual(frame_status, 200)
        self.assertIn((ROOT / "src" / "tsuzuki" / "ui" / "vendor" / "mermaid.min.js").read_bytes(), frame)

    def test_zip_contains_templates(self):
        out = build(ROOT / "src", self.root / "tsuzuki")
        with zipfile.ZipFile(str(out)) as z:
            names = z.namelist()
        for name in ("tsuzuki/templates/tasks-README.md", "tsuzuki/templates/claude-md-snippet.md",
                     "tsuzuki/templates/skills/task-start/SKILL.md", "tsuzuki/templates/skills/task-handoff/SKILL.md"):
            self.assertIn(name, names)

    def test_zip_contains_vscode_extension(self):
        out = build(ROOT / "src", self.root / "tsuzuki")
        with zipfile.ZipFile(str(out)) as z:
            names = z.namelist()
        self.assertIn("tsuzuki/vscode/extension.js", names)
        self.assertIn("tsuzuki/vscode/package.json", names)
        vsix = self.root / "t.vsix"
        p = self.run_app([out, "--ledger-dir", str(self.root / "ledger"), "vscode", "install", "--vsix", str(vsix)])
        self.assertEqual(p.returncode, 0, p.stderr)
        with zipfile.ZipFile(str(vsix)) as z:
            self.assertIn("extension/extension.js", z.namelist())

    def test_zip_contains_mermaid(self):
        built = build(ROOT / "src", self.root / "tsuzuki")
        with zipfile.ZipFile(str(built)) as z:
            names = z.namelist()
        self.assertIn("tsuzuki/ui/vendor/mermaid.min.js", names)
        self.assertIn("tsuzuki/ui/vendor/mermaid-LICENSE.txt", names)
        self.assertIn("tsuzuki/ui/mermaid-frame.html", names)

    def test_built_file_exports_templates(self):
        out = build(ROOT / "src", self.root / "tsuzuki")
        dest = self.root / "templates-out"
        p = self.run_app([out, "--ledger-dir", self.root / "ledger", "templates", dest])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((dest / "skills" / "task-handoff" / "SKILL.md").is_file())
        self.assertTrue((dest / "tasks" / "README.md").is_file())

    @staticmethod
    def wait_until_serving(proc, port, token_path):
        """ポートが応え、鍵のファイルができるまで待って鍵を返す。START_SECONDS を過ぎるか、終わってしまったら None。"""
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    pass
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                time.sleep(0.05)
                continue
            if token:
                return token
            time.sleep(0.05)
        return None

    @staticmethod
    def get(port, path, token):
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % port, "Cookie": "%s=%s" % (COOKIE, token)})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()


class LaunchdTest(unittest.TestCase):
    def test_plist_starts_serve_at_login(self):
        with open(str(PLIST), "rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(plist, {
            "Label": "local.tsuzuki.serve",
            "ProgramArguments": ["/Users/YOUR_NAME/.local/bin/tsuzuki", "serve", "--no-open"],
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": "/Users/YOUR_NAME/.tsuzuki/serve.log",
            "StandardErrorPath": "/Users/YOUR_NAME/.tsuzuki/serve.log",
        })


if __name__ == "__main__":
    unittest.main()
