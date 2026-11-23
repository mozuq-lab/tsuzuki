import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tsuzuki import cli
from tsuzuki.vscode_ext import (EXTENSION_ID, REQUEST_MAX_AGE_SECONDS, TERMINAL_NAME, Editor, VSCodeTerminal,
                                build_vsix, code_cli, editor_for, installed_path, installed_requests_dir,
                                requests_dir)

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_JS = ROOT / "src" / "tsuzuki" / "vscode" / "extension.js"
NODE = shutil.which("node")


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(os.path.realpath(self._tmp.name))


class VsixTest(TempDir):
    def test_vsix_contents(self):
        requests = self.d / "ledger" / "vscode-requests"
        out = build_vsix(self.d / "out" / "t.vsix", requests)
        with zipfile.ZipFile(str(out)) as z:
            names = sorted(z.namelist())
            self.assertEqual(names, ["[Content_Types].xml", "extension.vsixmanifest", "extension/extension.js",
                                     "extension/package.json", "extension/tsuzuki.json"])
            manifest = z.read("extension.vsixmanifest").decode("utf-8")
            package = json.loads(z.read("extension/package.json"))
            config = json.loads(z.read("extension/tsuzuki.json"))
            self.assertEqual(z.read("extension/extension.js"), EXTENSION_JS.read_bytes())
        self.assertEqual(package["publisher"] + "." + package["name"], EXTENSION_ID)
        self.assertIn('Id="%s" Version="%s" Publisher="%s"' % (package["name"], package["version"],
                                                               package["publisher"]), manifest)
        self.assertIn('Value="%s"' % package["engines"]["vscode"], manifest)
        self.assertEqual(config, {"requests_dir": str(requests)})
        self.assertEqual(package["activationEvents"], ["onUri"])

    def test_constants_match_extension(self):
        # 拡張の本体と、Tsuzuki の側の決まりがそろっている（依頼の期限・ID の形・ターミナルの名前）
        js = EXTENSION_JS.read_text(encoding="utf-8")
        self.assertEqual(int(re.search(r"MAX_AGE_MS = (\d+) \* 1000", js).group(1)), REQUEST_MAX_AGE_SECONDS)
        self.assertEqual(re.search(r'DEFAULT_NAME = "([^"]+)"', js).group(1), TERMINAL_NAME)
        self.assertIn("REQUEST_ID = /^[0-9a-f]{32}$/", js)
        self.assertEqual(len(VSCodeTerminal(self.d, str(self.d)).write("/w", "claude")), 32)
        self.assertIn('uri.path !== "/run"', js)


class RequestTest(TempDir):
    def terminal(self, now=1000.0, tokens=("a" * 32, "b" * 32)):
        it = iter(tokens)
        return VSCodeTerminal(self.d / "ledger" / "vscode-requests", str(self.d / "home"), now=lambda: now,
                              token=lambda n: next(it))

    def test_write(self):
        t = self.terminal()
        request_id = t.write("/w x", "claude --resume x")
        self.assertEqual(request_id, "a" * 32)
        path = t.requests / (request_id + ".json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"cwd": "/w x", "command": "claude --resume x", "name": "Claude Code"})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(t.requests.stat().st_mode), 0o700)

    def test_write_cleans_up_stale(self):
        t = self.terminal(now=10000.0)
        t.requests.mkdir(parents=True)
        old, fresh, other = t.requests / ("c" * 32 + ".json"), t.requests / ("d" * 32 + ".json"), t.requests / "x.txt"
        for p in (old, fresh, other):
            p.write_text("{}")
        os.utime(str(old), (0, 10000.0 - REQUEST_MAX_AGE_SECONDS - 1))
        os.utime(str(fresh), (0, 10000.0 - 5))
        os.utime(str(other), (0, 0))
        t.write("/w", "claude")
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())

    def test_write_refuses_newline(self):
        with self.assertRaises(ValueError):
            self.terminal().write("/w", "claude 'a\nb'")

    def test_discard(self):
        t = self.terminal()
        request_id = t.write("/w", "claude")
        t.discard(request_id)
        self.assertFalse((t.requests / (request_id + ".json")).exists())
        t.discard(request_id)  # もうなくても失敗しない

    def test_steps_use_the_editor_scheme(self):
        t = VSCodeTerminal(self.d, "/h", "Cursor", editor=Editor("Cursor", "cursor", ".cursor"))
        self.assertEqual(t.steps("/w", "a" * 32), [["open", "-a", "Cursor", "/w"],
                                                   ["open", "cursor://tsuzuki-local.terminal/run?id=" + "a" * 32]])
        t = VSCodeTerminal(self.d, "/h", editor=Editor("Visual Studio Code"))
        self.assertEqual(t.steps("/w", "b" * 32)[1], ["open", "vscode://tsuzuki-local.terminal/run?id=" + "b" * 32])
        self.assertEqual(requests_dir(Path("/l")), Path("/l/vscode-requests"))


class EditorTest(TempDir):
    """設定 vscode_app のアプリの product.json から、URI のスキーム・拡張の置き場所・コマンドを読む。"""

    def make_app(self, name, product, bins=()):
        root = self.d / "home" / "Applications" / (name + ".app") / "Contents" / "Resources" / "app"
        (root / "bin").mkdir(parents=True)
        (root / "product.json").write_text(product if isinstance(product, str) else json.dumps(product))
        for b in bins:
            (root / "bin" / b).write_text("")
        return root

    def test_reads_product_json(self):
        home = str(self.d / "home")
        root = self.make_app("TsuzukiTestCursor", {"urlProtocol": "cursor", "dataFolderName": ".cursor",
                                                   "applicationName": "cursor"}, bins=("code", "cursor"))
        editor = editor_for("TsuzukiTestCursor", home)
        self.assertEqual(editor, Editor("TsuzukiTestCursor", "cursor", ".cursor", str(root / "bin" / "cursor")))
        self.assertEqual(editor.extensions_dir(home), self.d / "home" / ".cursor" / "extensions")
        self.assertEqual(code_cli("TsuzukiTestCursor", home, which=lambda n: "/x/code"),
                         str(root / "bin" / "cursor"))

    def test_falls_back_to_vscode(self):
        home = str(self.d / "home")
        self.assertEqual(editor_for("TsuzukiTestMissing", home), Editor("TsuzukiTestMissing"))
        self.assertIsNone(code_cli("TsuzukiTestMissing", home, which=lambda n: "/x/code"))
        # 形のおかしい値は使わない（スキームの文字・置き場所のパスの区切り）。コマンドは bin/code に戻る
        root = self.make_app("TsuzukiTestOdd", {"urlProtocol": "a b", "dataFolderName": "../x",
                                                "applicationName": "../../sh"}, bins=("code",))
        self.assertEqual(editor_for("TsuzukiTestOdd", home),
                         Editor("TsuzukiTestOdd", "vscode", ".vscode", str(root / "bin" / "code")))
        self.make_app("TsuzukiTestBroken", "{not json")
        self.assertEqual(editor_for("TsuzukiTestBroken", home), Editor("TsuzukiTestBroken"))

    def ext(self, home, data=".vscode", version="0.1.0", requests=None):
        folder = Path(home) / data / "extensions" / ("%s-%s" % (EXTENSION_ID, version))
        folder.mkdir(parents=True)
        (folder / "package.json").write_text("{}")
        if requests is not None:
            (folder / "tsuzuki.json").write_text(json.dumps({"requests_dir": requests}))
        return folder

    def test_installed_path(self):
        home = str(self.d / "home")
        vscode, cursor = Editor("Visual Studio Code"), Editor("Cursor", "cursor", ".cursor")
        self.assertIsNone(installed_path(vscode, home))
        folder = self.ext(home, ".cursor")
        self.assertIsNone(installed_path(vscode, home))  # ほかのエディターに入っていても、入っていない
        self.assertEqual(installed_path(cursor, home), folder)
        old = self.ext(home, version="0.1.0")
        new = self.ext(home, version="0.1.1")
        self.assertEqual(installed_path(vscode, home), new)
        # 外した・入れ替えた拡張（.obsolete）は除く
        exts = Path(home) / ".vscode" / "extensions"
        (exts / ".obsolete").write_text(json.dumps({new.name: True}))
        self.assertEqual(installed_path(vscode, home), old)
        (exts / ".obsolete").write_text(json.dumps({new.name: True, old.name: True}))
        self.assertIsNone(installed_path(vscode, home))
        # extensions.json があれば、そこに載っているものだけ
        (exts / ".obsolete").unlink()
        (exts / "extensions.json").write_text(json.dumps([
            {"identifier": {"id": "other.ext"}, "relativeLocation": "other.ext-1"},
            {"identifier": {"id": "Tsuzuki-Local.Terminal"}, "relativeLocation": old.name}]))
        self.assertEqual(installed_path(vscode, home), old)
        (exts / "extensions.json").write_text(json.dumps([{"identifier": {"id": "other.ext"}}]))
        self.assertIsNone(installed_path(vscode, home))

    def test_installed_requests_dir(self):
        home = str(self.d / "home")
        vscode = Editor("Visual Studio Code")
        self.assertIsNone(installed_requests_dir(vscode, home))
        self.ext(home, requests="/l/vscode-requests")
        self.assertEqual(installed_requests_dir(vscode, home), "/l/vscode-requests")


# 拡張の本体を node で動かす。vscode のモジュールは偽物に差し替え、作ったターミナルと送った文を、起きた順に記録する。
# 場面（argv[7]）：
#   python … 20ms でシェルの統合が来て、30〜80ms に別の拡張のコマンド（source …/activate）が走る
#   plain  … 20ms でシェルの統合が来て、ほかには何も走らない
#   none   … シェルの統合が来ない
#   late   … シェルの統合が待つ上限（100ms）の後の 130ms に来て、170〜220ms に別の拡張のコマンドが走る
#   stuck  … 20ms でシェルの統合が来て、30ms から別の拡張のコマンドが走り続ける
HARNESS = r"""
const ext = require(process.argv[2]);
const scene = process.argv[7] || "plain";
const log = [];
const t0 = Date.now();
const at = () => Date.now() - t0;
const listeners = { si: [], start: [], end: [] };
const on = (key) => (fn) => { listeners[key].push(fn); return { dispose() { listeners[key] = listeners[key].filter((f) => f !== fn); } }; };
const fire = (key, e) => listeners[key].slice().forEach((fn) => fn(e));
let terminal = null;
const vscode = { window: {
  onDidChangeTerminalShellIntegration: on("si"),
  onDidStartTerminalShellExecution: on("start"),
  onDidEndTerminalShellExecution: on("end"),
  createTerminal(opts) {
    log.push(["create", opts]);
    terminal = {
      shellIntegration: undefined,
      show() { log.push(["show"]); },
      sendText(text, nl) { log.push(["send", text, nl, at()]); },
    };
    const late = scene === "late";
    if (scene !== "none") {
      setTimeout(() => {
        terminal.shellIntegration = { executeCommand(cmd) { log.push(["exec", cmd, at()]); } };
        fire("si", { terminal, shellIntegration: terminal.shellIntegration });
      }, late ? 130 : 20);
    }
    if (scene === "python" || late) {
      setTimeout(() => { log.push(["other-start", at()]); fire("start", { terminal }); }, late ? 170 : 30);
      setTimeout(() => { log.push(["other-end", at()]); fire("end", { terminal }); }, late ? 220 : 80);
      setTimeout(() => fire("start", { terminal: {} }), 90); // ほかのターミナルの動きは見ない
    }
    if (scene === "stuck") {
      setTimeout(() => { log.push(["other-start", at()]); fire("start", { terminal }); }, 30);
    }
    return terminal;
  } } };
const uri = { path: process.argv[4], query: process.argv[5] };
const timing = { shellIntegrationMs: 100, quietMs: 50, maxWaitMs: 1000 };
ext._handle(vscode, process.argv[3], uri, Number(process.argv[6]), timing)
  .then(() => null, (e) => e.message)
  .then((error) => process.stdout.write(JSON.stringify({ calls: log, error })));
"""


@unittest.skipIf(NODE is None, "node がありません")
class ExtensionJsTest(TempDir):
    def setUp(self):
        super().setUp()
        self.ext_dir = self.d / "ext"
        self.ext_dir.mkdir()
        self.requests = self.d / "requests"
        self.requests.mkdir()
        (self.ext_dir / "tsuzuki.json").write_text(json.dumps({"requests_dir": str(self.requests)}))
        self.harness = self.d / "harness.js"
        self.harness.write_text(HARNESS)
        self.work = self.d / "work dir"
        self.work.mkdir()

    def request(self, rid="a" * 32, body=None, mtime=None):
        path = self.requests / (rid + ".json")
        path.write_text(json.dumps(body if body is not None else
                                   {"cwd": str(self.work), "command": "claude --resume x", "name": "Claude Code"}))
        if mtime is not None:
            os.utime(str(path), (mtime, mtime))
        return path

    def run_js(self, query, path="/run", now_ms=None, scene="plain"):
        now_ms = now_ms if now_ms is not None else int(os.stat(str(self.requests)).st_mtime * 1000) + 1000
        proc = subprocess.run([NODE, str(self.harness), str(EXTENSION_JS), str(self.ext_dir), path, query,
                               str(now_ms), scene], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_runs_request_and_consumes_it(self):
        path = self.request()
        out = self.run_js("id=" + "a" * 32, now_ms=int(path.stat().st_mtime * 1000) + 1000)
        self.assertIsNone(out["error"])
        calls = out["calls"]
        self.assertEqual(calls[:2], [["create", {"name": "Claude Code", "cwd": str(self.work)}], ["show"]])
        self.assertEqual([c[:2] for c in calls[2:]], [["exec", "claude --resume x"]])  # シェルの統合で実行する
        self.assertGreaterEqual(calls[2][2], 20 + 50)  # シェルの統合が来て、静かな時間が続いてから
        self.assertFalse(path.exists())
        again = self.run_js("id=" + "a" * 32)  # 使用済み
        self.assertEqual(again["calls"], [])
        self.assertIn("見つかりません", again["error"])

    def test_waits_for_other_extension_commands(self):
        # Python の拡張が仮想環境を有効にするコマンドを打ち込んでいる間は待ち、終わって静かになってから送る
        self.request()
        out = self.run_js("id=" + "a" * 32, scene="python")
        self.assertIsNone(out["error"])
        names = [c[0] for c in out["calls"]]
        self.assertEqual(names, ["create", "show", "other-start", "other-end", "exec"])
        end, run = out["calls"][3][1], out["calls"][4][2]
        self.assertGreaterEqual(run - end, 45)

    def test_waits_again_when_shell_integration_comes_late(self):
        # シェルの統合が待つ上限の後に来ても、その後で別の拡張が打ち込むのを待ってから送る
        self.request()
        out = self.run_js("id=" + "a" * 32, scene="late")
        self.assertIsNone(out["error"])
        self.assertEqual([c[0] for c in out["calls"]], ["create", "show", "other-start", "other-end", "exec"])

    def test_does_not_interrupt_a_running_command(self):
        # 上限まで待っても走っているコマンドがあれば、止めずに sendText で後ろに並べる
        self.request()
        out = self.run_js("id=" + "a" * 32, scene="stuck")
        self.assertIsNone(out["error"])
        self.assertEqual([c[0] for c in out["calls"]], ["create", "show", "other-start", "send"])
        self.assertGreaterEqual(out["calls"][-1][3], 1000)

    def test_falls_back_to_send_text(self):
        # シェルの統合が来なければ、待つ上限の後で sendText で送る
        self.request()
        out = self.run_js("id=" + "a" * 32, scene="none")
        self.assertIsNone(out["error"])
        send = out["calls"][-1]
        self.assertEqual(send[:3], ["send", "claude --resume x", True])
        self.assertGreaterEqual(send[3], 100)

    def test_refuses(self):
        mtime = 1_800_000_000
        now_ms = mtime * 1000 + 1000
        cases = [
            ("bad id", "id=../../etc/passwd", "/run", None, "ID が正しくありません"),
            ("unknown path", "id=" + "a" * 32, "/exec", None, "知らない操作です"),
            ("expired", "id=" + "b" * 32, "/run", ("b" * 32, None, mtime - 121), "期限が切れています"),
            ("bad body", "id=" + "c" * 32, "/run", ("c" * 32, {"cwd": "rel", "command": "x"}, mtime), "中身が正しくありません"),
            ("newline", "id=" + "e" * 32, "/run",
             ("e" * 32, {"cwd": str(self.work), "command": "a\nb"}, mtime), "中身が正しくありません"),
            ("no dir", "id=" + "f" * 32, "/run",
             ("f" * 32, {"cwd": str(self.d / "nope"), "command": "claude"}, mtime), "作業ディレクトリが見つかりません"),
        ]
        for label, query, path, req, message in cases:
            with self.subTest(label):
                if req is not None:
                    file = self.request(req[0], req[1], req[2])
                out = self.run_js(query, path, now_ms)
                self.assertEqual(out["calls"], [])
                self.assertIn(message, out["error"])
                if req is not None:
                    self.assertFalse(file.exists())  # 使えない依頼も消す

    def test_missing_config(self):
        (self.ext_dir / "tsuzuki.json").unlink()
        self.request()
        out = self.run_js("id=" + "a" * 32)
        self.assertIn("tsuzuki.json", out["error"])


class InstallCliTest(TempDir):
    def run_cli(self, *rest):
        args = ["--ledger-dir", str(self.d / "ledger")] + list(rest)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_vsix_only(self):
        code, out, _ = self.run_cli("vscode", "install", "--vsix", str(self.d / "t.vsix"))
        self.assertEqual(code, 0)
        self.assertIn("書き出しました", out)
        with zipfile.ZipFile(str(self.d / "t.vsix")) as z:
            config = json.loads(z.read("extension/tsuzuki.json"))
        self.assertEqual(config["requests_dir"], str(self.d / "ledger" / "vscode-requests"))
        self.assertFalse((self.d / "ledger").exists())  # 台帳のディレクトリは作らない

    def test_install_runs_code(self):
        seen = []

        def fake_run(args, **kw):
            seen.append(args)
            with zipfile.ZipFile(args[2]) as z:  # 入れる時点で VSIX がある
                self.assertIn("extension/package.json", z.namelist())
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch("tsuzuki.vscode_ext.code_cli", return_value="/x/code"), \
                mock.patch("subprocess.run", side_effect=fake_run):
            code, out, _ = self.run_cli("vscode", "install")
        self.assertEqual(code, 0)
        self.assertEqual(seen[0][0::3], ["/x/code", "--force"])
        self.assertEqual(seen[0][1], "--install-extension")
        self.assertIn("入れました: " + EXTENSION_ID, out)

    def test_install_failures(self):
        with mock.patch("tsuzuki.vscode_ext.code_cli", return_value=None):
            code, _, err = self.run_cli("vscode", "install")
        self.assertEqual(code, 1)
        self.assertIn("--vsix", err)
        with mock.patch("tsuzuki.vscode_ext.code_cli", return_value="/x/code"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "broken vsix\n")):
            code, _, err = self.run_cli("vscode", "install")
        self.assertEqual(code, 1)
        self.assertIn("broken vsix", err)


if __name__ == "__main__":
    unittest.main()
