"""画面のサーバー（server.py）と tsuzuki serve のテスト。合成した記録だけを使う。"""
import contextlib
import html
import http.client
import io
import json
import os
import pkgutil
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from tsuzuki import cli, documents, indexer, server, taskwrite
from tsuzuki.config import Config, read_config, write_tasks_root
from tsuzuki.indexer import update
from tsuzuki.server import COOKIE, NO_TASKS_ROOT, TICKET_URL_RESET, TOKEN_FILE, make_server, serve
from tsuzuki.tasks import parse_task_md
from tsuzuki.taskwrite import CHANGED, TaskWriteError
from tests.fixtures import FakeClaude, assistant, make_git_repo, make_ledger, sid, t, tool_result, user, write_file

def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


S1, S8, S9 = sid(1), sid(8), sid(9)  # 対話、親が分からないヘッドレス、S1 の子のヘッドレス
GONE = sid(77)  # 記録がなく、入力履歴だけに残るセッション
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-src 'self'; "
       "base-uri 'none'; form-action 'self'")
DOC_CSP = ("sandbox allow-scripts allow-popups; default-src 'none'; script-src 'unsafe-inline'; "
           "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; "
           "form-action 'none'; base-uri 'none'")
MERMAID_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
               "img-src data:; font-src data:; form-action 'none'; base-uri 'none'")
NOT_AUTHORIZED ="tsuzuki serve が表示した URL から開いてください"
HTML_BODY = "<html><head><title>報告</title></head><body><h1>報告</h1><script>parent.x = 1</script></body></html>"


def headless(text, ts, session, cwd):
    return user(text, ts, session, cwd, human=False, entrypoint="sdk-cli")


def ms(minutes: float) -> int:
    """t(minutes) と同じ時刻の、入力履歴の形（UNIX ミリ秒）。"""
    return int((datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() + minutes * 60) * 1000)


class Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)


class ServerCase(unittest.TestCase):
    """偽の ~/.claude から作った台帳で、port=0 のサーバーをスレッドで動かす。
    S1（proj の対話。「引き継ぎ」を含む2ターン、HTML と Markdown の資料を書き、子 S9 を起こす）、S8（proj の
    親が分からないヘッドレス）、入力履歴だけの場所 garden（記録のないセッション GONE）。作業ディレクトリは実在する。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        # 一時ディレクトリの資料も台帳に入れる（サーバーの更新のスレッドにも効くよう、テストの間ずっと）
        patcher = mock.patch.object(documents, "TMP_PREFIXES", ("/private/tmp/claude-", "/tmp/claude-"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = os.path.join(self.root, "work")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.proj = make_git_repo(self.work, "proj")
        self.garden = os.path.join(self.work, "garden")
        os.makedirs(self.garden)
        self.ledger = Path(self.root) / "tsuzuki"
        self.cfg = Config(display_strip_prefixes=[self.work])
        self.fake = FakeClaude(Path(self.root))
        p = self.proj
        self.html_path, self.md_path = p + "/docs/report.html", p + "/docs/plan.md"
        write_file(self.html_path, HTML_BODY)
        write_file(self.md_path, "# 計画\n\n引き継ぎの**本文**")
        self.fake.write(p, S1, [
            user("引き継ぎ資料を作って", t(0), S1, p),
            assistant(t(1), S1, p, text="作りました",
                      tools=[("Write", {"file_path": self.html_path}), ("Write", {"file_path": self.md_path})]),
            tool_result('{"session_id": "%s"}' % S9, t(1.5), S1, p),
            user("引き継ぎの確認をお願いします", t(2), S1, p),
            assistant(t(3), S1, p, text="確認しました"),
        ])
        self.fake.write(p, S9, [headless("子の課題", t(1.2), S9, p)])
        # S1 の動きから5分より離す（近いと S1 の子と推定される）
        self.fake.write(p, S8, [headless("親のない課題", t(60), S8, p)])
        self.fake.history([{"display": "引き継ぎの履歴だけ", "timestamp": ms(30), "project": self.garden,
                            "sessionId": GONE}])
        self.reindex()
        with self.db() as conn:
            one = lambda sql, arg: conn.execute(sql, (arg,)).fetchone()[0]
            self.place = one("SELECT place_id FROM sessions WHERE id = ?", S1)
            self.garden_place = one("SELECT id FROM places WHERE path = ?", self.garden)
            self.html_doc = one("SELECT id FROM documents WHERE path = ?", self.html_path)
            self.md_doc = one("SELECT id FROM documents WHERE path = ?", self.md_path)
        self.calls = []
        self.httpd, self.app = self.start_server()
        self.origin = "http://127.0.0.1:%d" % self.app.port

    # 台帳とサーバー

    def reindex(self, **kw):
        conn = make_ledger(self.ledger)
        try:
            return update(conn, self.cfg, claude_dir=self.fake.dir, ledger_dir=self.ledger, home=self.home, **kw)
        finally:
            conn.close()

    @contextlib.contextmanager
    def db(self):
        conn = make_ledger(self.ledger)
        try:
            yield conn
        finally:
            conn.close()

    def runner(self, args, **kw):
        self.calls.append(args)
        return Proc(0)

    def start_server(self, **options):
        kw = dict(ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home, port=0, runner=self.runner,
                  sleep=lambda s: None, start_indexer=False)
        kw.update(options)
        httpd, app = make_server(self.cfg, **kw)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, httpd, app, thread)
        return httpd, app

    @staticmethod
    def stop_server(httpd, app, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        app.stop()

    # 要求

    def request(self, method, path, *, body=None, cookie=False, host=None, headers=None, app=None):
        app = app or self.app
        h = {"Host": host or "127.0.0.1:%d" % app.port}
        if cookie:
            h["Cookie"] = "%s=%s" % (COOKIE, app.token)
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            return Response(r.status, r.headers, r.read().decode("utf-8", "replace"))
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body, *, cookie=False, csrf=None, origin=None, content_type="application/json", app=None,
             headers=None):
        h = dict(headers or {})
        if csrf is not None:
            h["X-Tsuzuki-CSRF"] = csrf
        if origin is not None:
            h["Origin"] = origin
        if content_type is not None:
            h["Content-Type"] = content_type
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        return self.request("POST", path, body=data, cookie=cookie, headers=h, app=app)

    def action(self, path, body, app=None):
        """画面と同じ POST（クッキー・Origin・CSRF のそろったもの）。"""
        app = app or self.app
        return self.post(path, body, cookie=True, csrf=app.csrf, origin="http://127.0.0.1:%d" % app.port, app=app)

    def raw(self, data: bytes) -> bytes:
        """手で組んだ要求を送り、応答を接続が閉じるまで読む（http.client では送れない形のため）。"""
        chunks = []
        with socket.create_connection(("127.0.0.1", self.app.port), timeout=10) as s:
            s.sendall(data)
            try:
                while True:
                    b = s.recv(65536)
                    if not b:
                        break
                    chunks.append(b)
            except ConnectionResetError:  # 読まれなかった本文が残ったまま閉じられたとき
                pass
        return b"".join(chunks)

    def status(self, app=None):
        return self.get("/api/status", cookie=True, app=app).json()

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("時間内に条件が満たされませんでした")


class ServerTest(ServerCase):

    def test_requires_key_and_sets_cookie(self):
        self.assertEqual(self.get("/").status, 403)
        self.assertEqual(self.get("/?key=bad").status, 403)
        r = self.get("/?key=" + self.app.token)
        self.assertEqual(r.status, 302)
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", r.headers["Set-Cookie"])
        r = self.get("/", cookie=True)
        self.assertEqual(r.status, 200)
        self.assertIn('name="tsuzuki-csrf" content="' + self.app.csrf + '"', r.body)

    def test_host_header_is_checked(self):
        self.assertEqual(self.get("/frag/list", cookie=True, host="evil.example:%d" % self.app.port).status, 403)

    def test_post_requires_origin_and_csrf(self):
        body = {"session_id": S1, "how": "terminal", "confirm": True}
        self.assertEqual(self.post("/api/resume", body, cookie=True).status, 403)
        self.assertEqual(self.post("/api/resume", body, cookie=True, csrf=self.app.csrf,
                                   origin="http://evil.example").status, 403)
        r = self.post("/api/resume", body, cookie=True, csrf=self.app.csrf,
                      origin="http://127.0.0.1:%d" % self.app.port)
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.body)["message"], "Terminal で開きました")

    def test_security_headers(self):
        h = self.get("/frag/list", cookie=True).headers
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Referrer-Policy"], "no-referrer")

    def test_doc_raw_is_sandboxed_and_limited(self):
        r = self.get("/doc-raw/%d" % self.html_doc, cookie=True)
        self.assertIn("sandbox allow-scripts", r.headers["Content-Security-Policy"])
        self.assertEqual(self.get("/doc-raw/99999", cookie=True).status, 404)
        self.assertEqual(self.get("/doc-raw/%d" % self.md_doc, cookie=True).status, 404)
        self.assertEqual(self.get("/frag/doc/..%2f..%2fetc%2fpasswd", cookie=True).status, 404)

    def test_fragments_and_status(self):
        for path in ("/frag/places", "/frag/list", "/frag/card/" + S1, "/frag/search?q=" + quote("引き継ぎ"),
                     "/frag/place/%d" % self.place, "/frag/orphans/%d" % self.place, "/frag/docs",
                     "/frag/doc/%d" % self.md_doc, "/frag/conversation/" + S1):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie=True).status, 200)
        st = json.loads(self.get("/api/status", cookie=True).body)
        self.assertIn("last_update_at", st)

    def test_reads_while_updating(self):
        def app_update():
            for n in range(3):  # 毎回、取り込む行を足してから更新する
                self.fake.append(self.proj, S1, [user("引き継ぎの追記 %d" % n, t(10 + n), S1, self.proj)])
                self.app.run_update()
        th = threading.Thread(target=app_update)
        th.start()
        codes = [self.get("/frag/list", cookie=True).status for _ in range(20)]
        th.join()
        self.assertEqual(set(codes), {200})
        self.assertIsNone(self.status()["indexing"])

    # 鍵と Host（決めごと 2〜4・16）

    def test_cookie_and_redirect_exactly(self):
        r = self.get("/?key=" + self.app.token)
        self.assertEqual(r.headers["Set-Cookie"], "tsuzuki_key=%s; HttpOnly; SameSite=Strict; Path=/" % self.app.token)
        self.assertEqual(r.headers["Location"], "/")
        r = self.get("/")
        self.assertEqual((r.status, r.body), (403, NOT_AUTHORIZED))
        self.assertTrue(r.headers["Content-Type"].startswith("text/plain"))
        wrong = self.get("/frag/list", headers={"Cookie": "%s=%s" % (COOKIE, "0" * 64)})
        self.assertEqual((wrong.status, wrong.body), (403, NOT_AUTHORIZED))
        # 鍵がほかのクッキーと並んでいても読む。同じ名前の偽のクッキーがあっても、正しい値が1つあれば通る
        mixed = "theme=dark; %s=bogus; %s=%s" % (COOKIE, COOKIE, self.app.token)
        self.assertEqual(self.get("/frag/list", headers={"Cookie": mixed}).status, 200)
        # 鍵の付いた URL を開き直しても、正しい鍵ならクッキーを付け直す。違う鍵はクッキーがあっても断る
        self.assertEqual(self.get("/?key=" + self.app.token, cookie=True).status, 302)
        self.assertEqual(self.get("/?key=" + "f" * 64, cookie=True).status, 403)
        self.assertEqual(self.get("/?key=" + quote("日本語"), cookie=True).status, 403)

    def test_key_is_accepted_only_on_the_root(self):
        key = "?key=" + self.app.token
        for path in ("/frag/list" + key, "/api/status" + key, "/static/app.js" + key,
                     "/doc-raw/%d" % self.html_doc + key, "/index.html" + key):
            with self.subTest(path=path):
                r = self.get(path)
                self.assertEqual((r.status, r.body), (403, NOT_AUTHORIZED))
                self.assertNotIn("Set-Cookie", r.headers)

    def test_host_variants(self):
        port = self.app.port
        self.assertEqual(self.get("/frag/list", cookie=True, host="localhost:%d" % port).status, 200)
        for host in ("127.0.0.1", "localhost", "127.0.0.1:%d" % (port + 1), "127.0.0.1:%d.evil.example" % port,
                     "[::1]:%d" % port, "evil.example"):
            with self.subTest(host=host):
                self.assertEqual(self.get("/frag/list", cookie=True, host=host).status, 403)
        # 鍵の付いた URL でも、Host が違えばクッキーを渡さない（DNS rebinding）
        r = self.get("/?key=" + self.app.token, host="evil.example:%d" % port)
        self.assertEqual(r.status, 403)
        self.assertNotIn("Set-Cookie", r.headers)
        cookie = "Cookie: %s=%s\r\n" % (COOKIE, self.app.token)
        missing = self.raw(("GET /frag/list HTTP/1.0\r\n" + cookie + "\r\n").encode())
        self.assertTrue(missing.startswith(b"HTTP/1.0 403"), missing[:40])
        twice = self.raw(("GET /frag/list HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nHost: evil.example\r\n" % port
                          + cookie + "\r\n").encode())
        self.assertTrue(twice.startswith(b"HTTP/1.0 403"), twice[:40])

    def test_token_file_is_private_and_reused(self):
        path = self.ledger / TOKEN_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), self.app.token)
        self.assertRegex(self.app.token, r"^[0-9a-f]{64}$")
        _, again = self.start_server()
        self.assertEqual(again.token, self.app.token)
        self.assertNotEqual(again.csrf, self.app.csrf)  # CSRF の鍵は起動ごとに作る
        self.assertGreaterEqual(len(again.csrf), 32)
        path.chmod(0o644)
        self.start_server()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # 空や壊れた鍵のファイルは使わず、作り直す（空の鍵で通してしまわないため）
        for broken in ("", "short\n", "z" * 64):
            with self.subTest(broken=broken):
                path.write_text(broken, encoding="utf-8")
                _, fresh = self.start_server()
                self.assertRegex(fresh.token, r"^[0-9a-f]{64}$")
                self.assertEqual(path.read_text(encoding="utf-8").strip(), fresh.token)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(self.get("/frag/list", headers={"Cookie": "%s=" % COOKIE}, app=fresh).status, 403)
        # 前の起動の書きかけ（token.tmp）が残っていても作り直せる
        stale = self.ledger / (TOKEN_FILE + ".tmp")
        stale.write_text("old", encoding="utf-8")
        stale.chmod(0o644)
        path.write_text("", encoding="utf-8")
        _, fresh = self.start_server()
        self.assertRegex(fresh.token, r"^[0-9a-f]{64}$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(stale.exists())

    # POST（決めごと 3・9・16）

    def test_post_checks(self):
        body = {"session_id": S1, "how": "terminal", "confirm": True}
        local = "http://localhost:%d" % self.app.port
        self.assertEqual(self.post("/api/resume", body, cookie=True, csrf=self.app.csrf, origin=local).status, 200)
        for name, kw in (("no cookie", dict(csrf=self.app.csrf, origin=self.origin)),
                         ("no origin", dict(cookie=True, csrf=self.app.csrf)),
                         ("null origin", dict(cookie=True, csrf=self.app.csrf, origin="null")),
                         ("other port", dict(cookie=True, csrf=self.app.csrf,
                                             origin="http://127.0.0.1:%d" % (self.app.port + 1))),
                         ("https", dict(cookie=True, csrf=self.app.csrf,
                                        origin="https://127.0.0.1:%d" % self.app.port)),
                         ("wrong csrf", dict(cookie=True, csrf="x" * 32, origin=self.origin)),
                         ("token as csrf", dict(cookie=True, csrf=self.app.token, origin=self.origin)),
                         ("empty csrf", dict(cookie=True, csrf="", origin=self.origin))):
            with self.subTest(name):
                self.assertEqual(self.post("/api/resume", body, **kw).status, 403)
        self.assertEqual(len(self.calls), 1)  # 断った要求では何も実行しない
        # CSRF の見出しが2つ（正しいものと違うもの）あれば、どちらの順でも断る
        data = json.dumps(body)
        for first, second in ((self.app.csrf, "bad"), ("bad", self.app.csrf)):
            r = self.raw(("POST /api/resume HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nCookie: %s=%s\r\nOrigin: %s\r\n"
                          "X-Tsuzuki-CSRF: %s\r\nX-Tsuzuki-CSRF: %s\r\nContent-Type: application/json\r\n"
                          "Content-Length: %d\r\n\r\n%s" % (self.app.port, COOKIE, self.app.token, self.origin,
                                                            first, second, len(data), data)).encode())
            self.assertTrue(r.startswith(b"HTTP/1.0 403"), r[:40])
        self.assertEqual(len(self.calls), 1)

    def test_post_body_rules(self):
        cases = (
            ("text/plain", b'{"session_id": "%s", "how": "terminal"}' % S1.encode(), 400),
            (None, b"{}", 400),
            ("application/json", b"not json", 400),
            ("application/json", b"[1, 2]", 400),
            ("application/json", b"", 400),
            ("application/json", b"\xff\xfe", 400),
            ("application/json", b"[" * 5000 + b"]" * 5000, 400),
            ("application/json", json.dumps({"session_id": 1, "how": "terminal"}).encode(), 400),
            ("application/json", json.dumps({"session_id": S1, "how": None}).encode(), 400),
            ("application/json", json.dumps({"session_id": S1, "how": "terminal", "confirm": "yes"}).encode(), 400),
            ("application/json", json.dumps({"session_id": S1, "how": "terminal", "pad": "x" * 70000}).encode(), 413),
        )
        for ctype, data, code in cases:
            with self.subTest(ctype=ctype, data=data[:30]):
                r = self.post("/api/resume", data, cookie=True, csrf=self.app.csrf, origin=self.origin,
                              content_type=ctype)
                self.assertEqual(r.status, code)
                self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.calls, [])
        r = self.post("/api/resume", {"session_id": S1, "how": "terminal", "confirm": True}, cookie=True,
                      csrf=self.app.csrf, origin=self.origin, content_type="application/json; charset=utf-8")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        # 大きすぎる要求は、Origin や CSRF が違えば 403 のまま（確かめる順番どおり）
        big = json.dumps({"pad": "x" * 70000}).encode()
        self.assertEqual(self.post("/api/resume", big, cookie=True, csrf="bad", origin=self.origin).status, 403)
        r = self.raw(("POST /api/reindex HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nCookie: %s=%s\r\nOrigin: %s\r\n"
                      "X-Tsuzuki-CSRF: %s\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                      "0\r\n\r\n" % (self.app.port, COOKIE, self.app.token, self.origin, self.app.csrf)).encode())
        self.assertTrue(r.startswith(b"HTTP/1.0 400"), r[:40])

    def test_actions(self):
        # 実行中のセッションは確かめてから再開する（live を渡している）
        self.fake.live(os.getpid(), S1, self.proj)
        r = self.action("/api/resume", {"session_id": S1, "how": "terminal"})
        self.assertEqual(r.json(), {"ok": False, "needs_confirm": True,
                                    "message": "このセッションは今開いています。続けますか？"})
        self.assertEqual(self.calls, [])
        r = self.action("/api/resume", {"session_id": S1, "how": "vscode", "confirm": True})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "VS Code で開きました"})
        self.assertEqual(self.calls[-1], ["open", "vscode://anthropic.claude-code/open?session=" + S1])
        self.assertEqual(self.action("/api/resume", {"session_id": "x; rm -rf /", "how": "terminal"}).json()["message"],
                         "台帳にないセッションです")
        r = self.action("/api/open", {"doc_id": self.md_doc, "how": "finder"})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "開きました"})
        self.assertEqual(self.calls[-1], ["open", "-R", self.md_path])
        self.assertEqual(self.action("/api/open", {"doc_id": 99999, "how": "finder"}).json()["message"],
                         "台帳にない資料です")
        r = self.action("/api/open-place", {"place_id": self.place, "how": "finder"})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "開きました"})
        self.assertEqual(self.calls[-1], ["open", self.proj])
        for path, body in (("/api/open", {"doc_id": "1", "how": "finder"}), ("/api/open", {"doc_id": True, "how": "x"}),
                           ("/api/open", {"doc_id": 1.0, "how": "finder"}), ("/api/open", {"doc_id": -1, "how": "x"}),
                           ("/api/open", {"doc_id": 2 ** 70, "how": "finder"}),
                           ("/api/open-place", {"how": "finder"}), ("/api/open-place", {"place_id": self.place})):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.action(path, body).status, 400)
        self.assertEqual(self.action("/api/reindex", {}).json(), {"ok": True})
        r = self.post("/api/reindex", b"", cookie=True, csrf=self.app.csrf, origin=self.origin)
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(self.action("/api/nothing", {}).status, 404)
        self.assertEqual(self.get("/api/resume", cookie=True).status, 404)  # 状態を変える操作は POST だけ

    # 応答の見出し（決めごと 5・8・16）

    def test_headers_on_every_response(self):
        for path, code in (("/frag/list", 200), ("/api/status", 200), ("/", 200), ("/static/app.js", 200),
                           ("/frag/card/" + sid(4), 404), ("/nothing", 404)):
            with self.subTest(path=path):
                r = self.get(path, cookie=True)
                self.assertEqual(r.status, code)
                self.assertEqual(r.headers.get_all("Content-Security-Policy"), [CSP])
                self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(r.headers["X-Frame-Options"], "SAMEORIGIN")
                self.assertEqual(r.headers["Cache-Control"], "no-store")
        r = self.get("/frag/list")  # 鍵のない 403 にも付ける
        self.assertEqual((r.status, r.headers["Content-Security-Policy"]), (403, CSP))
        self.assertEqual(self.get("/frag/list", cookie=True).headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(self.get("/api/status", cookie=True).headers["Content-Type"],
                         "application/json; charset=utf-8")
        # http.server が自分で返す誤り（知らない方法）にも付ける
        r = self.request("PUT", "/frag/list", body=b"", cookie=True)
        self.assertEqual((r.status, r.body), (501, "この方法の要求は受け付けません"))
        self.assertEqual(r.headers["Content-Security-Policy"], CSP)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_failures_are_logged_without_details(self):
        # 台帳が使用中（locked・busy）のときだけ 503。SQL の誤りなどほかの失敗は 500。
        # stderr には例外の種類と呼び出しの位置（ファイル:行:関数）を1行だけ書き、例外の文と要求の中身は書かない
        busy, internal, sql = "台帳を読めませんでした（使用中かもしれません）", "内部のエラーが起きました", sqlite3.OperationalError
        cases = ((sql("database is locked"), 503, busy, "sqlite3.OperationalError"),
                 (sql("database table is locked: messages"), 503, busy, "sqlite3.OperationalError"),
                 (sql("database is busy"), 503, busy, "sqlite3.OperationalError"),
                 (sql("no such table: messages_fts"), 500, internal, "sqlite3.OperationalError"),
                 (RuntimeError("秘密の中身"), 500, internal, "RuntimeError"))
        secret = "秘密の語"
        for error, code, body, kind in cases:
            with self.subTest(error=str(error)):
                err = io.StringIO()
                with mock.patch.object(server, "search", side_effect=error), contextlib.redirect_stderr(err):
                    r = self.get("/frag/search?q=%s&targets=conversation" % quote(secret), cookie=True)
                self.assertEqual((r.status, r.body), (code, body))
                self.assertEqual(r.headers["Content-Security-Policy"], CSP)
                lines = err.getvalue().splitlines()
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("（%d %s）" % (code, kind), lines[0])
                self.assertRegex(lines[0], r"server\.py:\d+:_handle > server\.py:\d+:_route > ")
                self.assertIn("server.py:", lines[0])
                self.assertRegex(lines[0], r"server\.py:\d+:_frag_search")
                for hidden in (str(error), "messages", "秘密", quote(secret), "/frag/search", "q=", "targets"):
                    self.assertNotIn(hidden, lines[0])
        self.assertEqual(self.get("/frag/list", cookie=True).status, 200)

    def test_doc_raw_details(self):
        r = self.get("/doc-raw/%d" % self.html_doc, cookie=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, HTML_BODY)
        self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
        # ページの CSP は付けず、隔離の CSP だけ。枠に入れるので X-Frame-Options も付けない
        self.assertEqual(r.headers.get_all("Content-Security-Policy"), [DOC_CSP])
        self.assertIsNone(r.headers["X-Frame-Options"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.get("/doc-raw/%d" % self.html_doc).status, 403)
        for path in ("/doc-raw/", "/doc-raw/-1", "/doc-raw/1.0", "/doc-raw/%d/x" % self.html_doc,
                     "/doc-raw/..%2f..%2fetc%2fpasswd", "/doc-raw/" + "9" * 30, "/doc-raw/%EF%BC%91"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie=True).status, 404)
        with mock.patch.object(server, "MAX_DOC_BYTES", 20):  # 上限はバイトで数える
            self.assertEqual(self.get("/doc-raw/%d" % self.html_doc, cookie=True).body,
                             HTML_BODY.encode("utf-8")[:20].decode("utf-8", "replace"))
        # 台帳の拡張子とファイルの拡張子の、どちらかが HTML でなければ返さない
        with self.db() as conn:
            conn.execute("UPDATE documents SET ext = '.html' WHERE id = ?", (self.md_doc,))
            conn.execute("UPDATE documents SET ext = '.md' WHERE id = ?", (self.html_doc,))
            conn.commit()
        self.assertEqual(self.get("/doc-raw/%d" % self.md_doc, cookie=True).status, 404)
        self.assertEqual(self.get("/doc-raw/%d" % self.html_doc, cookie=True).status, 404)
        with self.db() as conn:
            conn.execute("UPDATE documents SET ext = '.HTM' WHERE id = ?", (self.html_doc,))
            conn.commit()
        self.assertEqual(self.get("/doc-raw/%d" % self.html_doc, cookie=True).status, 200)
        os.remove(self.html_path)
        self.assertEqual(self.get("/doc-raw/%d" % self.html_doc, cookie=True).status, 404)

    def test_mermaid_frame(self):
        resp = self.get("/mermaid-frame", cookie=True)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Security-Policy"], MERMAID_CSP)
        self.assertIn("mermaid", resp.body)
        self.assertIn("tsuzuki-mermaid", resp.body)
        self.assertEqual(self.get("/mermaid-frame", cookie=False).status, 403)

    def test_mermaid_frame_details(self):
        server._mermaid_page.cache_clear()
        self.addCleanup(server._mermaid_page.cache_clear)
        library = pkgutil.get_data("tsuzuki", "ui/vendor/mermaid.min.js").decode("utf-8")
        with mock.patch.object(server, "_asset", wraps=server._asset) as asset:
            first = self.get("/mermaid-frame", cookie=True)
            second = self.get("/mermaid-frame", cookie=True)
        # 組み立てたページは覚えておき、ライブラリを読むのは1回だけ
        self.assertEqual(first.body, second.body)
        self.assertEqual([c.args for c in asset.call_args_list].count(("ui/vendor/mermaid.min.js",)), 1)
        page = first.body
        self.assertEqual(first.headers["Content-Type"], "text/html; charset=utf-8")
        # ページの CSP は付けず、隔離の CSP だけ（/doc-raw/ と同じく、枠に入れるので X-Frame-Options も付けない）
        self.assertEqual(first.headers.get_all("Content-Security-Policy"), [MERMAID_CSP])
        self.assertIsNone(first.headers["X-Frame-Options"])
        self.assertEqual(first.headers["X-Content-Type-Options"], "nosniff")
        # ライブラリは書き換えずにページの中に入れる。外から読み込むものはない
        self.assertEqual(page.count(library), 1)
        self.assertEqual(page.count("<script>" + library + "</script>"), 1)
        rest = page.replace(library, "")
        self.assertNotIn("{{", rest)
        self.assertNotRegex(rest, r"\b(src|href)=")
        self.assertIn('securityLevel: "strict"', rest)
        self.assertIn('"tsuzuki-mermaid-result"', rest)
        # 鍵の確かめと Host の確かめは、ほかの経路と同じ
        self.assertEqual(self.get("/mermaid-frame").status, 403)
        self.assertEqual(self.get("/mermaid-frame", cookie=True, host="evil.example:%d" % self.app.port).status, 403)
        self.assertEqual(self.get("/mermaid-frame/x", cookie=True).status, 404)

    def test_mermaid_frame_is_cached_at_its_versioned_url(self):
        # 枠（約3.5MB）を図ごとに取り直さないよう、版を付けた URL ではブラウザに1日覚えさせる。鍵の確かめは同じ
        self.assertEqual(server.MERMAID_FRAME_URL, "/mermaid-frame?v=" + server.MERMAID_VERSION)
        cached = self.get(server.MERMAID_FRAME_URL, cookie=True)
        self.assertEqual(cached.status, 200)
        self.assertEqual(cached.headers.get_all("Cache-Control"), ["private, max-age=86400"])
        self.assertEqual(cached.headers.get_all("Content-Security-Policy"), [MERMAID_CSP])
        self.assertEqual(self.get(server.MERMAID_FRAME_URL).status, 403)
        # 版のない URL と違う版の URL は、これまでどおり覚えさせない
        for path in ("/mermaid-frame", "/mermaid-frame?v=0.0.1"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie=True).headers.get_all("Cache-Control"), ["no-store"])
        # 版は同梱のライブラリの記録（VENDOR.md）の版と同じ
        record = pkgutil.get_data("tsuzuki", "ui/vendor/VENDOR.md").decode("utf-8")
        self.assertIn("| 版 | %s（" % server.MERMAID_VERSION, record)

    def test_mermaid_frame_refuses_a_library_that_would_end_the_script(self):
        # ライブラリの文に <script や </script があれば、<script> の中に入れると壊れるので、ページを返さない
        real = server._asset

        def fake(name):
            return b"var a = '</SCRIPT >';" if name == "ui/vendor/mermaid.min.js" else real(name)

        server._mermaid_page.cache_clear()
        self.addCleanup(server._mermaid_page.cache_clear)
        with mock.patch.object(server, "_asset", side_effect=fake), contextlib.redirect_stderr(io.StringIO()):
            r = self.get("/mermaid-frame", cookie=True)
        self.assertEqual((r.status, r.body), (500, server.INTERNAL_ERROR))
        self.assertEqual(self.get("/mermaid-frame", cookie=True).status, 200)

    def test_page_and_static_files(self):
        page = self.get("/", cookie=True)
        self.assertEqual(page.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn('name="tsuzuki-resume-default" content="vscode"', page.body)
        self.assertIn('name="tsuzuki-headless-in-list" content="0"', page.body)
        self.assertNotIn("{{", page.body)
        self.cfg.headless_in_list, self.cfg.resume_default = True, "copy"
        page = self.get("/", cookie=True)
        self.assertIn('name="tsuzuki-headless-in-list" content="1"', page.body)
        self.assertIn('name="tsuzuki-resume-default" content="copy"', page.body)
        js = self.get("/static/app.js", cookie=True)
        self.assertEqual((js.status, js.headers["Content-Type"]), (200, "application/javascript; charset=utf-8"))
        css = self.get("/static/app.css", cookie=True)
        self.assertEqual((css.status, css.headers["Content-Type"]), (200, "text/css; charset=utf-8"))
        self.assertEqual(self.get("/static/app.js").status, 403)
        for path in ("/static/index.html", "/static/../server.py", "/static/..%2fserver.py", "/static/", "/ui/app.js",
                     "/static/app.js/", "/static//app.js", "/index.html", "/static/APP.JS"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie=True).status, 404)

    # 断片（決めごと 6・7・18）

    def test_fragment_contents(self):
        def frag(path):
            r = self.get(path, cookie=True)
            self.assertEqual(r.status, 200, path)
            return r.body
        rows = frag("/frag/list?days=all")
        self.assertIn('data-session="%s"' % S1, rows)
        self.assertNotIn(S8, rows)
        self.assertIn('id="children-%s"' % S1, rows)
        self.assertIn('class="row on" data-session="%s"' % S1, frag("/frag/list?days=all&s=" + S1))
        self.assertIn(S8, frag("/frag/list?days=all&headless=1"))
        self.assertIn(S1, frag("/frag/list?days=all&place=%d" % self.place))
        self.assertIn("セッションがありません", frag("/frag/list?days=all&place=%d" % self.garden_place))
        self.assertIn(S9, frag("/frag/children/" + S1))
        self.assertIn(S8, frag("/frag/orphans/%d" % self.place))
        # 子のカードのつながりに、更新が残した根拠を添える。場所は短い名前で、表示名は title に
        child = frag("/frag/card/" + S9)
        self.assertIn('<span class="rel">親</span><span class="ev">記録に ID</span>', child)
        self.assertIn('<div class="meta"><span title="proj">proj</span> ・ ', child)
        card = frag("/frag/card/" + S1)
        self.assertIn('data-turns=""', card)
        self.assertIn('data-copy="cd ', card)
        card = frag("/frag/card/%s?turns=2,1&i=1&q=%s" % (S1, quote("引き継ぎ")))
        self.assertIn("一致した箇所（2/2）", card)
        self.assertIn("<mark>引き継ぎ</mark>資料を作って", card)
        self.assertIn('data-index="1"', card)
        self.assertIn("一致した箇所（2/2）", frag("/frag/card/%s?turns=2,1&i=9" % S1))  # 範囲の外は端に寄せる
        place = frag("/frag/place/%d" % self.place)
        self.assertIn("proj", place)
        self.assertIn('data-action="orphans"', place)
        found = frag("/frag/search?q=" + quote("引き継ぎ"))
        self.assertIn("会話 1セッション ・ 資料 1件 ・ 入力履歴 1件", found)
        self.assertIn('data-turns="2,1"', found)
        self.assertIn("<mark>", found)
        self.assertIn("一致するものはありませんでした", frag("/frag/search?q=" + quote("存在しない語句")))
        self.assertIn("一致するものはありませんでした", frag("/frag/search?q=" + quote("引き継ぎ") +
                                                       "&place=%d" % self.garden_place + "&targets=conversation"))
        self.assertIn("ヘッドレス 2件", frag("/frag/search?q=%s&targets=conversation,headless" % quote("課題")))
        # 検索の状態の場所：一致のある所だけ。入力履歴だけの場所（list_places にない）も出す（決めごと 18）
        places = frag("/frag/places?q=" + quote("引き継ぎ"))
        self.assertIn("場所（一致がある所）", places)
        self.assertIn('data-place="%d"' % self.garden_place, places)
        self.assertIn('<span title="garden">garden</span>', places)
        self.assertIn('data-place="%d"' % self.place, places)
        # 場所を選んでいても、左にはほかの一致のある場所も出す（選んだ場所に印を付けるだけ）
        chosen = frag("/frag/places?q=%s&place=%d" % (quote("引き継ぎ"), self.place))
        self.assertIn('class="place on" data-place="%d"' % self.place, chosen)
        self.assertIn('data-place="%d"' % self.garden_place, chosen)
        normal = frag("/frag/places")
        self.assertNotIn("garden", normal)
        self.assertIn('class="place on" data-place="%d"' % self.place, frag("/frag/places?place=%d" % self.place))
        filters = frag("/frag/docs-filters")
        self.assertIn('data-kind="plan"', filters)
        self.assertIn('data-place="%d"' % self.place, filters)
        self.assertIn('class="place on" data-kind="plan"', frag("/frag/docs-filters?kind=plan&place=%d" % self.place))
        # 種類ごとの数は、選んだ場所の資料だけを数える
        everything = '<div class="place%s" data-kind=""><span>すべて</span><em>%d</em></div>'
        self.assertIn(everything % (" on", 2), filters)
        self.assertIn(everything % (" on", 0), frag("/frag/docs-filters?place=%d" % self.garden_place))
        docs = frag("/frag/docs")
        self.assertIn('data-doc="%d"' % self.md_doc, docs)
        self.assertIn('data-doc="%d"' % self.html_doc, docs)
        self.assertNotIn('data-doc="%d"' % self.html_doc, frag("/frag/docs?kind=plan"))
        self.assertIn('class="row on" data-doc="%d"' % self.md_doc, frag("/frag/docs?s=%d" % self.md_doc))
        hits = frag("/frag/docs?q=" + quote("引き継ぎ"))
        self.assertIn('data-doc="%d"' % self.md_doc, hits)
        self.assertNotIn('data-doc="%d"' % self.html_doc, hits)
        self.assertIn('<div class="r">', hits)
        self.assertIn("<mark>引き継ぎ</mark>", hits)
        self.assertNotIn('class="r"', docs)
        self.assertIn("資料がありません", frag("/frag/docs?q=%s&kind=other" % quote("引き継ぎ")))
        self.assertIn('<div class="md"><h1>計画</h1>', frag("/frag/doc/%d" % self.md_doc))
        self.assertIn('src="/doc-raw/%d"' % self.html_doc, frag("/frag/doc/%d" % self.html_doc))
        self.assertIn("<mark>引き継ぎ</mark>の<strong>本文</strong>", frag("/frag/doc/%d?q=%s" % (self.md_doc, quote("引き継ぎ"))))
        self.assertNotIn("<mark>", frag("/frag/doc/%d" % self.md_doc))
        conv = frag("/frag/conversation/%s?turn=1&turns=2,1&q=%s" % (S1, quote("引き継ぎ")))
        self.assertIn('id="turn-1"', conv)
        self.assertIn('data-turns="1,2"', conv)  # サーバーが古い順で求める（turns の問い合わせは使わない）
        self.assertIn("<mark>引き継ぎ</mark>", conv)
        self.assertIn("子の課題", frag("/frag/conversation/" + S9))

    def test_conversation_reads_more(self):
        # 長い会話は窓で読み、前後の残りは before・after（ターン）か offset（ヘッドレスの記録の項目）で読み足す
        long_id, task_id = sid(5), sid(6)
        records = []
        for n in range(1, 61):
            records += [user("質問%d" % n, t(500 + n), long_id, self.proj),
                        assistant(t(500 + n + 0.5), long_id, self.proj, text="答え%d" % n)]
        self.fake.write(self.proj, long_id, records)
        steps = [headless("長い課題", t(900), task_id, self.proj)]
        steps += [assistant(t(900 + n), task_id, self.proj, text="手順%d" % n, entrypoint="sdk-cli",
                            tools=[("Bash", {"command": "ls"})]) for n in range(1, 126)]
        self.fake.write(self.proj, task_id, steps)
        self.reindex()

        def frag(path):
            r = self.get(path, cookie=True)
            self.assertEqual(r.status, 200, path)
            return r.body

        def button(direction, key, value, label):
            session = task_id if key == "offset" else long_id
            return ('<button class="conv-more" data-action="read-more" data-session="%s" data-dir="%s" '
                    'data-%s="%s">%s</button>' % (session, direction, key, value, label))
        whole = frag("/frag/conversation/" + long_id)
        self.assertIn(button("before", "turn", 40, "前の 20 ターンを読む（残り 39）"), whole)
        self.assertNotIn('data-dir="after"', whole)
        part = frag("/frag/conversation/%s?before=40&q=%s" % (long_id, quote("質問")))
        self.assertTrue(part.startswith(button("before", "turn", 20, "前の 19 ターンを読む（残り 19）")), part[:300])
        self.assertEqual(re.findall(r'id="turn-(\d+)"', part), [str(n) for n in range(20, 40)])
        self.assertEqual(part.count("<mark>質問</mark>"), 20)
        self.assertNotIn('class="conv"', part)
        hits = frag("/frag/conversation/%s?q=%s" % (long_id, quote("質問5")))  # 質問5・質問50〜59
        self.assertIn('data-turns="5,50,51,52,53,54,55,56,57,58,59"', hits)
        self.assertIn('id="turn-59"', hits)  # 窓は一致の最後のターンの周り
        both = frag("/frag/conversation/%s?q=%s" % (long_id, quote("質問 答え1")))
        self.assertIn('data-turns="1,10,11,12,13,14,15,16,17,18,19"', both)
        no_hits = frag("/frag/conversation/%s?before=40&q=%s" % (long_id, quote("質問5")))
        self.assertEqual(no_hits.count("<mark>"), 0)  # ターン20〜39 には一致がない
        rest = frag("/frag/conversation/%s?before=20" % long_id)
        self.assertEqual(re.findall(r'id="turn-(\d+)"', rest), [str(n) for n in range(1, 20)])
        self.assertNotIn("read-more", rest)
        # 一致のターンから開いたときは、後ろに読み足す
        middle = frag("/frag/conversation/%s?turn=5" % long_id)
        self.assertIn(button("after", "turn", 25, "後の 20 ターンを読む（残り 35）"), middle)
        self.assertNotIn('data-dir="before"', middle)
        later = frag("/frag/conversation/%s?after=25" % long_id)
        self.assertEqual(re.findall(r'id="turn-(\d+)"', later), [str(n) for n in range(26, 46)])
        self.assertTrue(later.endswith(button("after", "turn", 45, "後の 15 ターンを読む（残り 15）")), later[-300:])
        self.assertEqual(frag("/frag/conversation/%s?after=60" % long_id), "")
        # ヘッドレスの記録（1 + 125 × 2 = 251 項目）
        task = frag("/frag/conversation/" + task_id)
        self.assertIn(button("after", "offset", 200, "後の 51 件を読む（残り 51）"), task)
        more = frag("/frag/conversation/%s?offset=200" % task_id)  # 項目 199 は「手順100」、200 はそのツール
        self.assertTrue(more.startswith('<details class="tools" data-turn="0"><summary>ツール1回：Bash（ls）</summary>'),
                        more[:200])
        self.assertIn(">手順101<", more)
        self.assertIn(">手順125<", more)
        self.assertNotIn(">手順100<", more)
        self.assertIn(">手順100<", task)
        self.assertNotIn("read-more", more)
        self.assertNotIn('id="turn-', more)
        # 会話の種類に合わない指定は、何も返さない（同じ発言を2回足さない）
        self.assertEqual(frag("/frag/conversation/%s?offset=0" % long_id), "")
        self.assertEqual(frag("/frag/conversation/%s?before=1" % task_id), "")
        for bad in ("before=x", "after=-1", "offset=1.5"):
            with self.subTest(bad=bad):
                self.assertEqual(self.get("/frag/conversation/%s?%s" % (long_id, bad), cookie=True).status, 400)

    def test_list_says_when_rows_were_cut(self):
        # 一覧の行の上限（views.LIST_LIMIT）で切ったときだけ、条件に合う数を数えて最後に1行出す
        with mock.patch.object(server.views, "LIST_LIMIT", 1):
            cut = self.get("/frag/list?days=all&headless=1", cookie=True).body
            whole = self.get("/frag/list?days=all", cookie=True).body
        self.assertEqual(len(re.findall(r'class="row[ "]', cut)), 1)
        self.assertIn('<div class="listnote">新しい順に1件まで表示しています（全3件）。期間を短くするか、場所を選ぶか、'
                      '「ヘッドレスも並べる」を外すと絞れます</div>', cut)
        self.assertIn('data-session="%s"' % S1, whole)
        self.assertNotIn("listnote", whole)  # 対話は1件だけなので切っていない
        self.assertNotIn("listnote", self.get("/frag/list?days=all&headless=1", cookie=True).body)

    def test_docs_list_says_when_rows_were_cut(self):
        # 資料の一覧も、上限（views.DOCS_LIMIT）まで並んだときだけ条件に合う数を数え、切っていれば最後に1行出す
        with mock.patch.object(server.views, "DOCS_LIMIT", 1):
            cut = self.get("/frag/docs", cookie=True).body
            plans = self.get("/frag/docs?kind=plan", cookie=True).body
            found = self.get("/frag/docs?q=" + quote("引き継ぎ"), cookie=True).body
        self.assertEqual(len(re.findall(r'class="row[ "]', cut)), 1)
        self.assertIn('<div class="listnote">新しい順に1件まで表示しています（全2件）。種類を選ぶか、場所を選ぶか、'
                      '検索すると絞れます</div>', cut)
        self.assertIn('data-doc="%d"' % self.md_doc, plans)
        self.assertNotIn("listnote", plans)  # 計画は1件だけなので切っていない
        self.assertIn('data-doc="%d"' % self.md_doc, found)
        self.assertNotIn("listnote", found)  # 検索の一致の一覧は、この上限で切らない
        self.assertNotIn("listnote", self.get("/frag/docs", cookie=True).body)

    def test_place_counts_follow_the_search_filters(self):
        # 左の場所の一致の数は、一覧と同じ絞り込み（期間・発言者・並び順）の検索から数える
        q = quote("引き継ぎ")
        counts = lambda path: re.findall(r'data-place="(\d*)"><span[^>]*>[^<]*</span><em>([^<]*)</em>',  # noqa
                                         self.get(path, cookie=True).body)
        everything = counts("/frag/places?q=" + q)
        self.assertEqual(everything, [("", "3"), (str(self.place), "2"), (str(self.garden_place), "1")])
        # 発言者は会話だけに効く。S1 の「引き継ぎ」は本人の入力だけにあり、資料と入力履歴は残る
        self.assertEqual(counts("/frag/places?q=%s&role=assistant" % q),
                         [("", "2"), (str(self.place), "1"), (str(self.garden_place), "1")])
        self.assertEqual(counts("/frag/places?q=%s&role=user&order=count" % q), everything)
        # 期間：合成した会話と入力履歴は 2026-11-01 ごろのもの。資料はテストが今書いたファイル
        self.assertEqual(counts("/frag/places?q=%s&days=7" % q), [("", "1"), (str(self.place), "1")])
        self.assertIn("会話 0セッション ・ 資料 1件 ・ 入力履歴 0件",
                      self.get("/frag/search?q=%s&days=7" % q, cookie=True).body)

    def test_search_says_when_sections_were_cut(self):
        # 検索の一致は種類ごとに上限まで。上限まで返った種類は、見出しにそう書く（資料の画面の検索は最後の1行）
        q = quote("引き継ぎ")
        with mock.patch.object(server, "RESULT_LIMIT", 1):
            cut = self.get("/frag/search?q=" + q, cookie=True).body
            by_count = self.get("/frag/search?q=%s&order=count" % q, cookie=True).body
            docs = self.get("/frag/docs?q=" + q, cookie=True).body
        self.assertIn("会話 1セッション（新しい順に1件まで） ・ 資料 1件（新しい順に1件まで） ・ "
                      "入力履歴 1件（新しい順に1件まで）", cut)
        self.assertIn("会話 1セッション（一致の多い順に1件まで）", by_count)
        self.assertIn('<div class="listnote">一致は新しい順に1件まで表示しています</div>', docs)
        whole = self.get("/frag/search?q=" + q, cookie=True).body
        self.assertIn("会話 1セッション ・ 資料 1件 ・ 入力履歴 1件", whole)
        self.assertNotIn("listnote", self.get("/frag/docs?q=" + q, cookie=True).body)

    def test_orphans_are_paged(self):
        batch = os.path.join(self.work, "batch")
        os.makedirs(batch)
        ids = [sid(1000 + n) for n in range(51)]
        for n, session in enumerate(ids):
            self.fake.write(batch, session, [headless("一括 %d" % n, t(100 + n), session, batch)])
        self.reindex()
        with self.db() as conn:
            pid = conn.execute("SELECT id FROM places WHERE path = ?", (batch,)).fetchone()[0]
        first = self.get("/frag/orphans/%d" % pid, cookie=True).body
        self.assertEqual(first.count('class="row child"'), 50)
        self.assertIn('data-action="orphans" data-place="%d" data-offset="50"' % pid, first)
        rest = self.get("/frag/orphans/%d?offset=50" % pid, cookie=True).body
        self.assertEqual(rest.count('class="row child"'), 1)
        self.assertIn(ids[0], rest)  # 新しい順なので、最も古いものが最後のページに
        self.assertNotIn("さらに表示", rest)
        exact = self.get("/frag/orphans/%d?offset=1" % pid, cookie=True).body  # 残りがちょうど1ページ分
        self.assertEqual(exact.count('class="row child"'), 50)
        self.assertNotIn("さらに表示", exact)
        self.assertEqual(self.get("/frag/orphans/%d?offset=60" % pid, cookie=True).body, "")

    def test_children_are_paged(self):
        fleet = make_git_repo(self.work, "fleet")
        parent, kids = sid(2000), [sid(2001 + n) for n in range(51)]
        self.fake.write(fleet, parent, [user("一括で起こす", t(200), parent, fleet),
                                        tool_result(" ".join(kids), t(201), parent, fleet)])
        for n, session in enumerate(kids):  # 親の記録に ID があるので、どれも親の子（強い推定）
            self.fake.write(fleet, session, [headless("子 %d" % n, t(202 + n), session, fleet)])
        self.reindex()
        first = self.get("/frag/children/" + parent, cookie=True).body
        self.assertEqual(first.count('class="row child"'), 50)
        self.assertIn('data-action="children" data-session="%s" data-offset="50"' % parent, first)
        rest = self.get("/frag/children/%s?offset=50" % parent, cookie=True).body
        self.assertEqual(rest.count('class="row child"'), 1)
        self.assertIn(kids[0], rest)
        self.assertNotIn("さらに表示", rest)
        exact = self.get("/frag/children/%s?offset=1" % parent, cookie=True).body  # 残りがちょうど1ページ分
        self.assertEqual(exact.count('class="row child"'), 50)
        self.assertNotIn("さらに表示", exact)

    def test_bad_parameters_and_unknown_ids(self):
        bad = ["/frag/list?days=-1", "/frag/list?days=abc", "/frag/list?days=1.5", "/frag/list?place=x",
               "/frag/list?headless=yes", "/frag/list?s=not-an-id", "/frag/places?place=-2",
               "/frag/places?q=a&targets=conversation,mail", "/frag/search?q=a&role=bot", "/frag/search?q=a&order=old",
               "/frag/search?q=a&days=%EF%BC%93", "/frag/card/%s?turns=1,x" % S1, "/frag/card/%s?turns=1,,2" % S1,
               "/frag/card/%s?i=-1" % S1, "/frag/children/%s?offset=-1" % S1, "/frag/children/%s?offset=1e3" % S1,
               "/frag/orphans/%d?offset=x" % self.place, "/frag/docs?place=x", "/frag/docs?s=x",
               "/frag/docs-filters?place=1.0", "/frag/conversation/%s?turn=x" % S1,
               "/frag/list?" + "&".join("a%d=1" % n for n in range(300))]
        for path in bad:
            with self.subTest(path=path):
                r = self.get(path, cookie=True)
                self.assertEqual(r.status, 400)
                self.assertEqual(r.headers["Content-Type"], "text/plain; charset=utf-8")
        missing = ["/frag/card/" + sid(4), "/frag/card/" + S1 + "%20", "/frag/card/x", "/frag/card/",
                   "/frag/children/" + sid(4), "/frag/conversation/" + sid(4), "/frag/orphans/99999",
                   "/frag/place/99999", "/frag/place/abc", "/frag/doc/99999", "/frag/doc/-1", "/frag/doc/1/2",
                   "/frag/card/%s/x" % S1, "/frag/list/", "/frag", "/api/status/x"]
        for path in missing:
            with self.subTest(path=path):
                r = self.get(path, cookie=True)
                self.assertEqual((r.status, r.body), (404, "見つかりません"))
        self.assertEqual(self.get("/frag/list?days=all&place=", cookie=True).status, 200)  # 空は指定なし

    def test_status_fields(self):
        st = self.status()
        self.assertEqual(sorted(st), ["counts", "errors", "format_warning", "indexing", "last_update_at",
                                      "source_warning"])
        self.assertRegex(st["last_update_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(st["counts"], {"interactive": 1, "headless": 2, "documents": 2})
        self.assertEqual((st["indexing"], st["format_warning"], st["source_warning"], st["errors"]),
                         (None, None, None, []))
        os.rename(self.fake.dir / "projects", self.fake.dir / "projects-away")
        self.assertIsNotNone(self.app.run_update())
        st = self.status()
        self.assertIn("Claude Code の記録の場所が見つかりません", st["source_warning"])

    # 検索の覚え書き（決めごと 7）

    def test_search_is_cached(self):
        real, calls = server.search, []

        def counting(conn, q, **kw):
            calls.append(q)
            return real(conn, q, **kw)
        q = quote("引き継ぎ")
        with mock.patch.object(server, "search", counting):
            self.assertEqual(self.get("/frag/places?q=" + q, cookie=True).status, 200)
            self.assertEqual(self.get("/frag/search?q=" + q, cookie=True).status, 200)
            self.assertEqual(len(calls), 1)
            # 語の区切りの違いは同じ検索とみなす
            self.get("/frag/search?q=%20" + q + "%20", cookie=True)
            self.assertEqual(len(calls), 1)
            # 場所を選ぶと一覧は絞った検索、左の場所は絞らない検索（覚えてある）
            self.get("/frag/search?q=%s&place=%d" % (q, self.place), cookie=True)
            self.get("/frag/places?q=%s&place=%d" % (q, self.place), cookie=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1].place_ids, [self.place])
            self.get("/frag/search?q=%s&order=count" % q, cookie=True)
            self.assertEqual(len(calls), 3)
            # 30秒たてば検索し直す
            later = time.monotonic() + 31
            self.app.clock = lambda: later
            self.get("/frag/search?q=" + q, cookie=True)
            self.assertEqual(len(calls), 4)
            # 覚えるのは8件まで（古いものから捨てる）
            for n in range(8):
                self.get("/frag/search?q=w%d" % n, cookie=True)
            self.assertEqual(len(calls), 12)
            self.get("/frag/search?q=" + q, cookie=True)
            self.assertEqual(len(calls), 13)
            # 更新が終わったら、覚えた結果を捨てる
            self.app.run_update()
            self.get("/frag/search?q=" + q, cookie=True)
            self.assertEqual(len(calls), 14)

    def test_search_finished_after_an_update_is_not_kept(self):
        # 検索の途中で更新が終わったら、その結果（更新の前の台帳から作ったもの）は覚えない
        real, calls = server.search, []

        def racing(conn, q, **kw):
            result = real(conn, q, **kw)
            calls.append(q)
            if len(calls) == 1:
                self.app.run_update()
            return result
        q = quote("引き継ぎ")
        with mock.patch.object(server, "search", racing):
            self.get("/frag/search?q=" + q, cookie=True)
            self.get("/frag/search?q=" + q, cookie=True)
            self.assertEqual(len(calls), 2)
            self.get("/frag/search?q=" + q, cookie=True)
            self.assertEqual(len(calls), 2)  # 更新の後に始めた検索の結果は覚えている

    # 記録と同時の読み書き（決めごと 11・12・17）

    def test_reads_while_a_write_transaction_is_open(self):
        writer = sqlite3.connect(str(self.ledger / "ledger.db"), isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE sessions SET ai_title = '書きかけ'")
        try:
            started = time.monotonic()
            for path in ("/frag/list", "/frag/card/" + S1, "/frag/search?q=" + quote("引き継ぎ"), "/api/status",
                         "/frag/places?q=" + quote("確認"), "/frag/doc/%d" % self.md_doc):
                with self.subTest(path=path):
                    r = self.get(path, cookie=True)
                    self.assertEqual(r.status, 200)
                    self.assertNotIn("書きかけ", r.body)
            r = self.action("/api/open", {"doc_id": self.md_doc, "how": "vscode"})
            self.assertEqual(r.json()["message"], "開きました")
            self.assertLess(time.monotonic() - started, 4.0)
        finally:
            writer.execute("ROLLBACK")
            writer.close()

    def test_requests_are_not_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.get("/frag/search?q=" + quote("秘密の語"), cookie=True)
            self.get("/frag/card/" + sid(4), cookie=True)
            self.get("/frag/list")
            self.request("PUT", "/frag/list", body=b"")
            self.raw(b"BROKEN\r\n\r\n")
        self.assertEqual(err.getvalue(), "")


TASK_MD = ("---\ntitle: ログインのセッション切れを延ばす\nticket: \"#123\"\nstage: チケット\nwaiting: false\n"
           "repo: %s\n---\n# ログインのセッション切れを延ばす\n\n## 現在地\n単体テストを足した\n")


class TaskServerTest(ServerCase):
    """タスクの断片と操作。一時ディレクトリのタスクの置き場所に、リポジトリ proj の
    2026-11-20-a と、リポジトリのない 2026-11-22-ログイン と、共通の資料（shared）を置く。"""

    def setUp(self):
        super().setUp()
        self.tasks_root = os.path.join(self.root, "tasks")
        self.task_md = os.path.join(self.tasks_root, "2026-11-20-a", "task.md")
        write_file(self.task_md, TASK_MD % self.proj)
        write_file(os.path.join(self.tasks_root, "2026-11-22-ログイン", "task.md"), "# ログイン\n")
        write_file(os.path.join(self.tasks_root, "shared", "notes-team.md"), "# チームのメモ\n")
        self.reindex(tasks_root=Path(self.tasks_root))
        self.httpd, self.app = self.start_server(tasks_root=Path(self.tasks_root))
        self.origin = "http://127.0.0.1:%d" % self.app.port
        self.runner_calls = self.calls

    def frag(self, path):
        r = self.get(path, cookie=True)
        self.assertEqual(r.status, 200, path)
        return r.body

    def text(self, path=None):
        with open(path or self.task_md, encoding="utf-8") as f:
            return f.read()

    def card_stamp(self, card):
        """カードが持つ task.md の更新時刻と大きさ（画面が段階と待ちの操作に添えて送るもの）。"""
        return (float(re.search(r'data-mtime="([^"]*)"', card).group(1)),
                int(re.search(r'data-size="([^"]*)"', card).group(1)))


    def test_resume_ignores_a_task_field(self):
        # 続きの再開はタスクのワークスペースを使わない（task は読まない）
        ws_dir = os.path.join(self.root, "ws")
        os.makedirs(os.path.join(ws_dir, "app"))
        ws = os.path.join(ws_dir, "x.code-workspace")
        write_file(ws, json.dumps({"folders": [{"path": "app"}]}))
        write_file(self.task_md, (TASK_MD % self.proj).replace("repo:", "workspace: %s\nrepo:" % ws, 1))
        self.reindex(tasks_root=Path(self.tasks_root))
        for task in ("2026-11-20-a", "nope"):
            self.calls.clear()
            r = self.action("/api/resume", {"session_id": S1, "how": "vscode", "confirm": False, "task": task})
            self.assertTrue(r.json()["ok"], r.json())
            self.assertEqual(self.calls[0], ["open", "-a", "Visual Studio Code", self.proj])

    def test_task_fragments(self):
        self.assertIn('data-task="2026-11-20-a"', self.get("/frag/tasks", cookie=True).body)
        self.assertEqual(self.get("/frag/task/" + quote("2026-11-22-ログイン"), cookie=True).status, 200)
        for bad in ("nope", "..%2F..%2Fetc", "shared"):
            self.assertEqual(self.get("/frag/task/" + bad, cookie=True).status, 404)

    def test_ticket_url_from_the_screen(self):
        r = self.action("/api/ticket-url", {"url": "https://redmine.example.com/issues"}).json()
        self.assertEqual((r["ok"], r["message"]), (True, "チケットのリンク先を変えました"))
        self.assertEqual(self.app.cfg.ticket_url, "https://redmine.example.com/issues/{n}")
        self.assertEqual(read_config(self.ledger).ticket_url, "https://redmine.example.com/issues/{n}")
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn('href="https://redmine.example.com/issues/123"', card)
        self.assertIn("https://redmine.example.com/issues/{n}", self.frag("/frag/task-filters"))
        bad = self.action("/api/ticket-url", {"url": "redmine.example.com"}).json()
        self.assertFalse(bad["ok"])
        self.assertEqual(self.app.cfg.ticket_url, "https://redmine.example.com/issues/{n}")
        r = self.action("/api/ticket-url", {"url": ""}).json()
        self.assertEqual((r["ok"], r["message"]), (True, TICKET_URL_RESET))
        self.assertIsNone(self.app.cfg.ticket_url)
        self.assertIsNone(read_config(self.ledger).ticket_url)

    def test_tasks_root_switch_to_a_root_that_has_the_tasks(self):
        new = os.path.join(self.root, "moved")
        for name in ("2026-11-20-a", "2026-11-22-ログイン"):
            write_file(os.path.join(new, name, "task.md"), "# x\n")
        self.assertEqual(self.post("/api/tasks-root", {"path": new}, cookie=True, origin=self.origin).status, 403)
        r = self.action("/api/tasks-root", {"path": new}).json()
        self.assertEqual((r["ok"], r["needs_confirm"]), (True, False), r)
        self.assertIn("変えました", r["message"])
        self.assertEqual(self.app.tasks_root, Path(new))
        self.assertEqual(self.app.cfg.tasks_root, new)
        self.assertEqual(read_config(self.ledger).tasks_root, new)
        self.assertTrue(self.app._wake.is_set())
        shown = self.frag("/frag/task-filters")
        self.assertIn(new, shown)
        self.assertNotIn("README.md がありません", shown)
        same = self.action("/api/tasks-root", {"path": new}).json()
        self.assertEqual((same["ok"], same["message"]), (True, "変わった所はありません"))

    def test_tasks_readme_is_placed_once_from_the_template(self):
        from tsuzuki.export import display_path, template_text
        readme = os.path.join(self.tasks_root, "README.md")
        self.app.cfg = Config(tasks_root=self.tasks_root)  # 点検は設定の置き場所を見る
        self.assertIn('data-action="tasks-readme"', self.frag("/frag/task/2026-11-20-a"))
        self.assertEqual(self.post("/api/tasks-readme", {}, cookie=True, origin=self.origin).status, 403)
        self.assertFalse(os.path.exists(readme))
        r = self.action("/api/tasks-readme", {}).json()
        self.assertEqual((r["ok"], r["needs_confirm"]), (True, False), r)
        shown = display_path(Path(self.tasks_root), self.home)
        self.assertEqual(r["message"], "タスクの決まり（README.md）を置きました: %s/README.md" % shown)
        with open(readme, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, template_text("templates/tasks-README.md", tasks_root_display=shown))
        self.assertNotIn("~/tasks", text)  # 雛形の ~/tasks が、今の置き場所の表記になっている
        self.assertIn(shown + "/shared/", text)
        self.assertNotIn('data-action="tasks-readme"', self.frag("/frag/task/2026-11-20-a"))
        again = self.action("/api/tasks-readme", {}).json()
        self.assertFalse(again["ok"])
        self.assertIn("README.md はもうあります", again["message"])
        with open(readme, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_the_left_column_has_no_prep_block(self):
        left = self.frag("/frag/task-filters")
        self.assertNotIn("prep", left)
        self.assertNotIn("Claude で使う準備", left)

    def card_prep(self, task="2026-11-20-a"):
        """カードの「Claude で使う準備」の（項目の名前、説明、ボタンの項目）の並びと、まとめの文。"""
        card = self.frag("/frag/task/" + quote(task))
        names = re.findall(r'<div class="prep-name">[✗△] ([^<]*)</div>', card)
        whats = [html.unescape(w) for w in re.findall(r'<div class="prep-what">([^<]*)</div>', card)]
        items = re.findall(r'<details class="prep"><summary>([^<]*)</summary>', card)
        return card, names, whats, items

    def test_task_card_prep_lists_what_the_start_folder_lacks_and_each_button_places_it(self):
        from tsuzuki.export import display_path, template_text
        card, names, whats, summary = self.card_prep()
        proj, root = self.proj, self.tasks_root
        self.assertEqual(summary, ["Claude で使う準備：あと 4つ"])
        self.assertEqual(names, ["決まりのファイル", "CLAUDE.local.md の決まり", "スキル", "書き込みの許可"])
        self.assertEqual(whats, ["%s/README.md がありません" % root,
                                 "%s/CLAUDE.local.md に、タスクの決まりを読む文を足します" % proj,
                                 "task-start・task-handoff を %s/.claude/skills に置きます" % proj,
                                 "%s/.claude/settings.local.json の additionalDirectories に、タスクの置き場所を足します"
                                 % proj])
        self.assertIn("このタスクの会話を始めるフォルダー（%s）に置きます" % proj, html.unescape(card))
        self.assertIn('data-action="tasks-readme"', card)
        self.assertLess(card.index("ブランチ・PR"), card.index('<details class="prep">'))
        self.assertLess(card.index('<details class="prep">'), card.index(">会話 <") if ">会話 <" in card else len(card))
        shown = display_path(Path(root), self.home)
        # 置く前の確認：許可していない送信元は 403
        self.assertEqual(self.post("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"}, cookie=True,
                                   origin=self.origin).status, 403)
        self.assertFalse(os.path.exists(os.path.join(proj, "CLAUDE.local.md")))
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"}).json()
        self.assertEqual((r["ok"], r["needs_confirm"], r["message"]),
                         (True, False, "%s/CLAUDE.local.md に決まりの文を足しました" % proj), r)
        with open(os.path.join(proj, "CLAUDE.local.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), template_text("templates/claude-md-snippet.md", tasks_root_display=shown))
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "skills"}).json()
        self.assertEqual((r["ok"], r["message"]), (True, "%s/.claude/skills にスキルを置きました" % proj), r)
        self.assertTrue(os.path.isfile(os.path.join(proj, ".claude", "skills", "task-handoff", "SKILL.md")))
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "allow"}).json()
        self.assertEqual((r["ok"], r["message"]),
                         (True, "%s/.claude/settings.local.json の additionalDirectories にタスクの置き場所を足しました"
                          % proj), r)
        with open(os.path.join(proj, ".claude", "settings.local.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"permissions": {"additionalDirectories": [root]}})
        self.assertEqual(self.card_prep()[2], ["%s/README.md がありません" % root])
        self.action("/api/tasks-readme", {})
        card, names, _, summary = self.card_prep()
        self.assertEqual((names, summary), ([], []))
        self.assertNotIn("prep", card)
        again = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"}).json()
        self.assertEqual((again["ok"], again["message"]), (True, "もう整っています"))

    def test_open_task_folder_in_finder(self):
        folder = os.path.join(self.tasks_root, "2026-11-20-a")
        r = self.action("/api/open-task", {"task": "2026-11-20-a"})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "開きました"})
        self.assertEqual(self.calls[-1], ["open", folder])
        n = len(self.calls)
        for task in ("nope", "../2026-11-20-a", "shared"):
            self.assertEqual(self.action("/api/open-task", {"task": task}).json()["message"], "台帳にないタスクです")
        self.assertEqual(self.action("/api/open-task", {}).status, 400)
        self.assertEqual(self.post("/api/open-task", {"task": "2026-11-20-a"}, cookie=True, origin=self.origin).status,
                         403)
        self.assertEqual(len(self.calls), n)

    def test_a_linked_stale_readme_is_refused_with_the_link_as_the_reason(self):
        self.app.cfg = Config(tasks_root=self.tasks_root)
        target = os.path.join(self.root, "real-readme.md")
        write_file(target, "# 古い決まり\n")
        os.symlink(target, os.path.join(self.tasks_root, "README.md"))
        r = self.action("/api/tasks-readme", {"mode": "section"}).json()
        self.assertFalse(r["ok"])
        self.assertIn("シンボリックリンクなので書きません", r["message"])
        self.assertEqual(self.text(target), "# 古い決まり\n")

    def test_task_card_lists_work_files(self):
        path = os.path.join(self.tasks_root, "2026-11-20-a", "check.sql")
        write_file(path, "select 1;\n")
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn('<div class="sec">作業用のファイル 1</div>', card)
        self.assertIn('data-copy="@%s"' % path, card)

    def test_stale_readme_and_rule_are_brought_up_to_date_from_the_card(self):
        from tsuzuki.export import display_path, readme_section, rule_line
        self.app.cfg = Config(tasks_root=self.tasks_root)
        shown = display_path(Path(self.tasks_root), self.home)
        readme = os.path.join(self.tasks_root, "README.md")
        write_file(readme, "# 古い決まり\n")
        local = os.path.join(self.proj, "CLAUDE.local.md")
        old = "## タスクと資料\n- %s/README.md を読む\n" % self.tasks_root
        write_file(local, old)
        card, names, whats, summary = self.card_prep()
        self.assertEqual(names[:2], ["決まりのファイル（古い）", "CLAUDE.local.md の決まり（古い）"])
        self.assertIn('data-action="tasks-readme" data-mode="section"', card)
        r = self.action("/api/tasks-readme", {"mode": "section"}).json()
        self.assertEqual((r["ok"], r["message"]),
                         (True, "タスクの決まり（README.md）に、作業用のファイルの節を足しました: %s/README.md" % shown))
        self.assertEqual(self.text(readme), "# 古い決まり\n\n" + readme_section(tasks_root_display=shown))
        # 2つ目の窓から押しても書かず、成功として返す（画面がカードを読み直し、「古い」が消える）
        again = self.action("/api/tasks-readme", {"mode": "section"}).json()
        self.assertEqual((again["ok"], again["message"]), (True, "もう足してあります"))
        self.assertEqual(self.text(readme), "# 古い決まり\n\n" + readme_section(tasks_root_display=shown))
        # 「置く」（mode なし）は今までどおり、あれば上書きせずに断る
        placed = self.action("/api/tasks-readme", {}).json()
        self.assertFalse(placed["ok"])
        self.assertIn("README.md はもうあります", placed["message"])
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"}).json()
        self.assertEqual((r["ok"], r["message"]), (True, "%s/CLAUDE.local.md に作業用のファイルの1行を足しました" % self.proj))
        self.assertEqual(self.text(local), old + "\n" + rule_line(tasks_root_display=shown))
        self.assertNotIn("（古い）", self.card_prep()[0])
        done = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"}).json()
        self.assertEqual((done["ok"], done["message"]), (True, "もう整っています"))

    def test_task_card_prep_for_a_task_without_a_repo_uses_the_task_folder(self):
        folder = os.path.join(self.tasks_root, "2026-11-22-ログイン")
        card, names, whats, summary = self.card_prep("2026-11-22-ログイン")
        self.assertEqual(summary, ["Claude で使う準備：あと 4つ"])
        self.assertIn("%s/CLAUDE.local.md に" % folder, whats[1])
        r = self.action("/api/task-prep", {"task": "2026-11-22-ログイン", "item": "allow"}).json()
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.isfile(os.path.join(folder, ".claude", "settings.local.json")))

    def test_task_card_prep_follows_the_workspace_folder(self):
        ws_dir = os.path.join(self.root, "ws")
        os.makedirs(ws_dir)
        ws = os.path.join(ws_dir, "x.code-workspace")
        write_file(ws, json.dumps({"folders": [{"path": "."}]}))
        write_file(self.task_md, (TASK_MD % self.proj).replace("repo:", "workspace: %s\nrepo:" % ws, 1))
        self.reindex(tasks_root=Path(self.tasks_root))
        _, _, whats, _ = self.card_prep()
        self.assertIn("%s/CLAUDE.local.md に" % ws_dir, whats[1])

    def test_task_prep_skills_keep_an_existing_one_and_refusals_say_why(self):
        write_file(os.path.join(self.proj, ".claude", "skills", "task-handoff", "SKILL.md"), "自分の")
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "skills"}).json()
        self.assertEqual((r["ok"], r["message"]), (
            True, "%s/.claude/skills にスキルを置きました（task-handoff は前からあるので残しました）" % self.proj))
        write_file(os.path.join(self.proj, ".claude", "settings.local.json"), "{broken")
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "allow"}).json()
        self.assertFalse(r["ok"])
        self.assertIn("JSON として読めない", r["message"])
        with open(os.path.join(self.proj, ".claude", "settings.local.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{broken")
        self.assertFalse(self.action("/api/task-prep", {"task": "nope", "item": "rule"}).json()["ok"])
        for item in ("readme", "", "nope"):  # 知らない項目は、答えの ok が false（400 ではない）
            r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": item}).json()
            self.assertEqual((r["ok"], r["message"]), (False, "準備の項目が正しくありません"))
        for body in ({"task": "2026-11-20-a", "item": 5}, {"task": "2026-11-20-a"}, {"item": "rule"}):
            self.assertEqual(self.action("/api/task-prep", body).status, 400, body)
        self.app.tasks_root = None
        self.assertFalse(self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"}).json()["ok"])

    def test_task_prep_unreadable_places_never_make_a_500(self):
        with mock.patch("tsuzuki.taskprep.check", side_effect=PermissionError(13, "Permission denied")):
            card = self.get("/frag/task/2026-11-20-a", cookie=True)
            self.assertEqual(card.status, 200)
            self.assertNotIn("prep", card.body)
            r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"})
            self.assertEqual(r.status, 200)
            self.assertFalse(r.json()["ok"])
            self.assertIn("Permission denied", r.json()["message"])

    @unittest.skipIf(os.geteuid() == 0, "root は読めてしまう")
    def test_task_prep_with_an_unreadable_dot_claude_still_answers(self):
        dot = os.path.join(self.proj, ".claude")
        os.makedirs(dot)
        os.chmod(dot, 0)
        self.addCleanup(os.chmod, dot, 0o755)
        self.assertEqual(self.get("/frag/task/2026-11-20-a", cookie=True).status, 200)
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "skills"})
        self.assertEqual(r.status, 200)
        self.assertFalse(r.json()["ok"])

    def test_task_prep_skills_never_land_in_the_claude_dir_through_a_link(self):
        claude_skill = os.path.join(str(self.app.claude_dir), "skills", "task-start")
        os.makedirs(claude_skill)
        os.makedirs(os.path.join(self.proj, ".claude", "skills"))
        os.symlink(claude_skill, os.path.join(self.proj, ".claude", "skills", "task-start"))
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "skills"}).json()
        self.assertFalse(r["ok"], r)
        self.assertEqual(os.listdir(claude_skill), [])

    def test_task_prep_for_a_task_that_starts_in_home_shows_one_line_and_refuses(self):
        write_file(self.task_md, TASK_MD % self.home)
        self.reindex(tasks_root=Path(self.tasks_root))
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn("このタスクの会話はホーム（またはディスクの根）で始まるので、ここには置きません。"
                      "リポジトリかワークスペースを決めてください", card)
        self.assertNotIn("prep-item", card)
        self.assertNotIn("task-prep", card)
        for item in ("rule", "skills", "allow"):
            r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": item}).json()
            self.assertFalse(r["ok"])
            self.assertIn("ホーム（またはディスクの根）で始まる", r["message"])
        self.assertEqual(os.listdir(self.home), [])

    def test_task_prep_refuses_a_start_folder_inside_the_claude_dir(self):
        inside = os.path.join(str(self.app.claude_dir), "work")
        os.makedirs(inside)
        write_file(self.task_md, (TASK_MD % inside))
        self.reindex(tasks_root=Path(self.tasks_root))
        r = self.action("/api/task-prep", {"task": "2026-11-20-a", "item": "rule"}).json()
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(inside, "CLAUDE.local.md")))

    def test_tasks_readme_refusals(self):
        r = self.action("/api/tasks-readme", {}).json()  # 設定の置き場所があるので、まず通る形を確かめる
        self.assertTrue(r["ok"], r)
        os.remove(os.path.join(self.tasks_root, "README.md"))
        shutil.rmtree(self.tasks_root)
        gone = self.action("/api/tasks-readme", {}).json()
        self.assertFalse(gone["ok"])
        self.assertIn("タスクの置き場所がありません", gone["message"])
        inside = self.fake.dir / "tasks"
        inside.mkdir(parents=True, exist_ok=True)
        self.app.tasks_root = inside
        refused = self.action("/api/tasks-readme", {}).json()
        self.assertFalse(refused["ok"])
        self.assertIn("Claude Code の設定ディレクトリ", refused["message"])
        self.assertFalse((inside / "README.md").exists())
        self.app.tasks_root = None
        none = self.action("/api/tasks-readme", {}).json()
        self.assertEqual((none["ok"], none["message"]), (False, NO_TASKS_ROOT))

    def test_tasks_root_asks_before_switching_to_a_root_without_the_tasks(self):
        new = os.path.join(self.root, "empty")
        os.makedirs(new)
        write_file(os.path.join(new, "2026-11-20-a", "task.md"), "# x\n")  # 2件のうち1件だけある
        r = self.action("/api/tasks-root", {"path": new}).json()
        self.assertEqual((r["ok"], r["needs_confirm"]), (False, True), r)
        self.assertIn("今の置き場所のタスク 1件は、新しい置き場所にありません", r["message"])
        self.assertEqual(self.app.tasks_root, Path(self.tasks_root))
        self.assertFalse(os.path.exists(self.ledger / "config.json") and
                         read_config(self.ledger).tasks_root == new)
        r = self.action("/api/tasks-root", {"path": new, "confirm": True}).json()
        self.assertEqual((r["ok"], r["needs_confirm"]), (True, False), r)
        self.assertEqual(self.app.tasks_root, Path(new))
        self.assertEqual(read_config(self.ledger).tasks_root, new)

    def test_tasks_root_refuses_bad_input(self):
        for bad in ("", "tasks", os.path.join(self.root, "nowhere"), self.home, str(self.fake.dir)):
            r = self.action("/api/tasks-root", {"path": bad}).json()
            self.assertEqual((r["ok"], r["needs_confirm"]), (False, False), bad)
            self.assertTrue(r["message"], bad)
        self.assertEqual(self.app.tasks_root, Path(self.tasks_root))

    def test_tasks_root_rejects_a_non_boolean_confirm(self):
        r = self.action("/api/tasks-root", {"path": self.tasks_root, "confirm": "yes"})
        self.assertEqual(r.status, 400)

    def test_tasks_root_switch_after_moving_folders_reindexes_them(self):
        # Finder の移動と同じ rename（更新時刻は変わらない）で動かしても、新しい置き場所の path で読み直す
        new = os.path.join(self.root, "moved")
        os.makedirs(new)
        for name in ("2026-11-20-a", "2026-11-22-ログイン", "shared"):
            os.rename(os.path.join(self.tasks_root, name), os.path.join(new, name))
        with self.app.connect() as conn:
            conn.execute("INSERT INTO task_overrides VALUES ('2026-11-20-a', ?, 'add', 'x')", (S1,))
            conn.commit()
        r = self.action("/api/tasks-root", {"path": new}).json()
        self.assertEqual((r["ok"], r["needs_confirm"]), (True, False), r)
        self.app.run_update()
        with self.app.connect() as conn:
            paths = [row[0] for row in conn.execute("SELECT path FROM tasks")]
            docs = [row[0] for row in conn.execute("SELECT path FROM documents")]
            overrides = conn.execute("SELECT COUNT(*) FROM task_overrides").fetchone()[0]
            linked = conn.execute("SELECT rule FROM task_sessions WHERE task = '2026-11-20-a' AND session_id = ?",
                                  (S1,)).fetchone()
        self.assertEqual(linked and linked[0], "manual")  # 残した記録が、また効く
        self.assertEqual(sorted(paths), [new + "/2026-11-20-a", new + "/2026-11-22-ログイン"])
        self.assertIn(new + "/shared/notes-team.md", docs)
        self.assertFalse([d for d in docs if d.startswith(self.tasks_root + "/")], docs)
        self.assertEqual(overrides, 1)  # 手で直した記録は、切り替えでも消さない

    def test_another_server_on_the_ledger_follows_a_switch_and_the_list_stays_stable(self):
        write_tasks_root(self.ledger, self.tasks_root)
        self.app.cfg = Config(tasks_root=self.tasks_root)
        other = server.App(Config(tasks_root=self.tasks_root), ledger_dir=self.ledger, claude_dir=self.fake.dir,
                           home=self.home, port=1, token="t", runner=self.runner, sleep=lambda s: None,
                           tasks_root=Path(self.tasks_root))
        self.app.run_update()
        other.run_update()
        new = os.path.join(self.root, "moved")
        os.makedirs(new)
        for name in ("2026-11-20-a", "2026-11-22-ログイン", "shared"):
            os.rename(os.path.join(self.tasks_root, name), os.path.join(new, name))
        r = self.action("/api/tasks-root", {"path": new}).json()
        self.assertEqual(r["ok"], True, r)
        self.assertEqual(other.tasks_root, Path(self.tasks_root))  # 切り替えはまだ知らない

        def state():
            with self.app.connect() as conn:
                return (sorted(row[0] for row in conn.execute("SELECT path FROM tasks")),
                        sorted(row[0] for row in conn.execute("SELECT id FROM documents WHERE path LIKE ?",
                                                              (new + "/%",))))
        for _ in range(2):
            other.run_update()
            self.assertEqual(other.tasks_root, Path(new))
            self.assertEqual(other.cfg.tasks_root, new)
            first = state()
            self.app.run_update()
            self.assertEqual(state(), first)  # 交互に更新しても、一覧も資料の ID も変わらない
        self.assertEqual(first[0], [new + "/2026-11-20-a", new + "/2026-11-22-ログイン"])

    def test_update_keeps_its_root_when_config_json_cannot_be_used(self):
        (self.ledger / "config.json").write_text("{broken", encoding="utf-8")
        before = self.app.tasks_root
        self.app.run_update()
        self.assertEqual(self.app.tasks_root, before)
        (self.ledger / "config.json").write_text(json.dumps({"tasks_root": str(self.fake.dir / "x")}), encoding="utf-8")
        self.app.run_update()
        self.assertEqual(self.app.tasks_root, before)

    def test_vanished_folders_keep_manual_link_records_through_an_update(self):
        with self.app.connect() as conn:
            conn.execute("INSERT INTO task_overrides VALUES ('2026-11-20-a', 's1', 'add', 'x')")
            conn.commit()
        new = os.path.join(self.root, "elsewhere")
        os.makedirs(new)
        os.rename(os.path.join(self.tasks_root, "2026-11-20-a"), os.path.join(new, "2026-11-20-a"))
        self.app.run_update()  # 古い置き場所のまま更新（フォルダーだけが消えた）
        with self.app.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_overrides").fetchone()[0], 1)

    def test_task_stage_post(self):
        body = {"task": "2026-11-20-a", "stage": "PR"}
        self.assertEqual(self.post("/api/task-stage", body, cookie=True, origin=self.origin).status, 403)  # CSRF なし
        st = os.stat(self.task_md)
        ok = self.action("/api/task-stage", {"task": "2026-11-20-a", "stage": "PR", "mtime": st.st_mtime,
                                             "size": st.st_size})
        self.assertEqual(ok.json()["ok"], True)
        self.assertIn("stage: PR", self.text())
        stale = self.action("/api/task-stage", {"task": "2026-11-20-a", "stage": "修正", "mtime": st.st_mtime,
                                                "size": st.st_size})
        self.assertEqual(stale.json(), {"ok": False, "needs_confirm": False, "message": CHANGED})

    def test_task_new_link_and_conversation(self):
        made = self.action("/api/task-new", {"title": "ログイン", "short_name": "login", "ticket": "123",
                                             "repo": ""}).json()
        self.assertTrue(made["ok"])
        self.assertTrue(os.path.isfile(made["task_md"]))
        self.assertTrue(made["task_md"].startswith(self.tasks_root + "/"))
        self.action("/api/task-link", {"task": made["task"], "session_id": S1, "action": "add"})
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT rule FROM task_sessions WHERE session_id = ?", (S1,)).fetchone()[0],
                             "manual")
        copy = self.action("/api/task-conversation", {"task": made["task"], "how": "copy"}).json()
        self.assertTrue(copy["command"].startswith("cd "))
        self.assertEqual(self.runner_calls, [])  # copy では何も起動しない

    def test_task_new_with_a_workspace(self):
        ws_dir = os.path.join(self.root, "wsn")
        os.makedirs(os.path.join(ws_dir, "app"))
        ws = os.path.join(ws_dir, "n.code-workspace")
        write_file(ws, json.dumps({"folders": [{"path": "app"}]}))
        made = self.action("/api/task-new", {"title": "W", "short_name": "with-ws", "workspace": ws}).json()
        self.assertEqual((made["ok"], made["message"]),
                         (True, 'タスクを作りました。ワークスペースの folders の先頭に "." を足しました'))
        self.assertIn("workspace: %s\n" % ws, read_file(made["task_md"]))
        self.assertEqual(json.loads(read_file(ws))["folders"][0], {"path": "."})
        present = os.path.join(ws_dir, "p.code-workspace")
        write_file(present, json.dumps({"folders": [{"path": "."}]}))
        made = self.action("/api/task-new", {"title": "P", "short_name": "present-ws", "workspace": present}).json()
        self.assertEqual((made["ok"], made["message"]), (True, "タスクを作りました"))
        bad = self.action("/api/task-new", {"title": "B", "short_name": "bad-ws",
                                            "workspace": os.path.join(ws_dir, "none.code-workspace")}).json()
        self.assertFalse(bad["ok"])
        self.assertIn("ファイルがありません", bad["message"])
        self.assertEqual([n for n in os.listdir(self.tasks_root) if n.endswith("bad-ws")], [])

    def test_task_trash(self):
        trash = os.path.join(self.root, "trash")
        os.makedirs(trash)
        write_file(os.path.join(self.tasks_root, "2026-11-20-a", "a.md"), "# メモ\n")
        self.reindex(tasks_root=Path(self.tasks_root))
        with self.db() as conn:
            conn.execute("INSERT INTO task_overrides (task, session_id, action, at) VALUES (?, ?, ?, ?)",
                         ("2026-11-20-a", S1, "add", "2026-11-24T00:00:00Z"))
            conn.commit()
        with mock.patch("tsuzuki.taskwrite.find_trash", return_value=trash):
            answer = self.action("/api/task-trash", {"task": "2026-11-20-a"}).json()
        self.assertEqual(answer, {"ok": True, "needs_confirm": False, "message": server.TRASHED % self.tasks_root})
        self.assertTrue(os.path.isfile(os.path.join(trash, "2026-11-20-a", "task.md")))
        self.assertFalse(os.path.exists(os.path.join(self.tasks_root, "2026-11-20-a")))
        self.assertNotIn('data-task="2026-11-20-a"', self.frag("/frag/tasks?status=all"))
        self.assertEqual(self.get("/frag/task/2026-11-20-a", cookie=True).status, 404)
        with self.db() as conn:
            for table in ("task_sessions", "task_overrides"):
                self.assertIsNone(conn.execute("SELECT 1 FROM %s WHERE task = ?" % table,
                                               ("2026-11-20-a",)).fetchone(), table)
            self.assertIsNone(conn.execute("SELECT 1 FROM documents WHERE path LIKE ?",
                                           (os.path.join(self.tasks_root, "2026-11-20-a") + "/%",)).fetchone())

    def test_task_copy_doc(self):
        task = "2026-11-20-a"
        self.action("/api/task-link", {"task": task, "session_id": S1, "action": "add"})
        before = self.frag("/frag/task/" + task)
        self.assertIn('data-action="task-copy-doc" data-doc="%d"' % self.md_doc, before)
        self.assertNotIn("コピー済み", before)
        answer = self.action("/api/task-copy-doc", {"task": task, "doc_id": self.md_doc}).json()
        self.assertEqual(answer, {"ok": True, "needs_confirm": False, "message": server.COPIED % "plan.md"})
        copy = os.path.join(self.tasks_root, task, "plan.md")
        self.assertEqual(self.text(copy), "# 計画\n\n引き継ぎの**本文**")
        self.assertEqual(self.text(self.md_path), "# 計画\n\n引き継ぎの**本文**")  # 元は変わらない
        card = self.frag("/frag/task/" + task)
        folder = card[card.index("このタスクのフォルダー"):card.index("ほかの場所の資料")]
        self.assertIn('<span class="fn">plan.md</span>', folder)
        self.assertIn("コピー済み", card[card.index("ほかの場所の資料"):])
        # もう一度コピーすると、上書きせずに plan-2.md にする
        again = self.action("/api/task-copy-doc", {"task": task, "doc_id": self.md_doc}).json()
        self.assertEqual(again["message"], server.COPIED % "plan-2.md")
        self.assertTrue(os.path.isfile(os.path.join(self.tasks_root, task, "plan-2.md")))

    def test_task_copy_doc_refusals(self):
        task = "2026-11-20-a"
        unknown = {"ok": False, "needs_confirm": False, "message": server.UNKNOWN_DOC}
        self.assertEqual(self.action("/api/task-copy-doc", {"task": task, "doc_id": 99999}).json(), unknown)
        self.assertEqual(self.action("/api/task-copy-doc", {"task": "nope", "doc_id": self.md_doc}).json(),
                         {"ok": False, "needs_confirm": False, "message": server.UNKNOWN_TASK})
        self.assertEqual(self.action("/api/task-copy-doc", {"task": task, "doc_id": "x"}).status, 400)
        self.assertEqual(self.post("/api/task-copy-doc", {"task": task, "doc_id": self.md_doc}, cookie=True,
                                   origin=self.origin).status, 403)  # CSRF なし
        os.remove(self.md_path)  # 台帳にあっても、ファイルがなくなっていれば断る
        gone = self.action("/api/task-copy-doc", {"task": task, "doc_id": self.md_doc}).json()
        self.assertEqual((gone["ok"], gone["message"]), (False, taskwrite.NOT_A_FILE % self.md_path))
        self.assertEqual(sorted(os.listdir(os.path.join(self.tasks_root, task))), ["task.md"])

    def test_task_trash_refusals(self):
        for task in ("nope", "shared"):
            self.assertEqual(self.action("/api/task-trash", {"task": task}).json(),
                             {"ok": False, "needs_confirm": False, "message": server.UNKNOWN_TASK})
        self.assertEqual(self.post("/api/task-trash", {"task": "2026-11-20-a"}, cookie=True,
                                   origin=self.origin).status, 403)  # CSRF なし
        self.assertTrue(os.path.isdir(os.path.join(self.tasks_root, "2026-11-20-a")))

    # 断片

    def test_task_list_and_filters(self):
        tasks = self.frag("/frag/tasks")
        self.assertIn('data-action="new-task"', tasks)
        self.assertIn('data-task="%s"' % "2026-11-22-ログイン", tasks)
        self.assertIn('<div class="row on" data-task="2026-11-20-a">', self.frag("/frag/tasks?task=2026-11-20-a"))
        self.assertIn("タスクがありません", self.frag("/frag/tasks?status=done"))
        self.assertIn('data-task="2026-11-20-a"', self.frag("/frag/tasks?status=all"))
        self.assertIn('data-task="2026-11-20-a"', self.frag("/frag/tasks?status=open"))
        # 場所で絞る。0 は「場所なし」
        by_place = self.frag("/frag/tasks?place=%d" % self.place)
        self.assertIn('data-task="2026-11-20-a"', by_place)
        self.assertNotIn('data-task="2026-11-22-ログイン"', by_place)
        no_place = self.frag("/frag/tasks?place=0")
        self.assertIn('data-task="2026-11-22-ログイン"', no_place)
        self.assertNotIn("2026-11-20-a", no_place)
        filters = self.frag("/frag/task-filters")
        self.assertIn('<div class="place on" data-status=""><span>進行中と待ち</span><em>2</em></div>', filters)
        self.assertIn('<div class="place" data-place="%d"><span>proj</span><em>1</em></div>' % self.place, filters)
        self.assertIn('<div class="place" data-place="0"><span>場所なし</span><em>1</em></div>', filters)
        with self.db() as conn:
            shared = conn.execute("SELECT id FROM places WHERE path = ?",
                                  (os.path.join(self.tasks_root, "shared"),)).fetchone()[0]
        self.assertIn('data-action="docs" data-place="%d"' % shared, filters)
        # 場所の数は選んだ状況のタスクで数え、選んだ場所は 0件でも出す
        done = self.frag("/frag/task-filters?status=done&place=%d" % self.place)
        self.assertIn('<div class="place on" data-status="done"><span>完了</span><em>0</em></div>', done)
        self.assertIn('<div class="place on" data-place="%d"><span>proj</span><em>0</em></div>' % self.place, done)
        self.assertNotIn('data-place="0"', done)
        for path in ("/frag/tasks?status=x", "/frag/tasks?place=x", "/frag/tasks?place=-1",
                     "/frag/task-filters?status=open2", "/frag/task-filters?place=1.5"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie=True).status, 400)

    def test_task_card_vscode_terminal_button(self):
        extra = 'data-action="task-conversation" data-how="vscode-terminal">新しい会話（VS Code のターミナル）'
        self.assertNotIn(extra, self.frag("/frag/task/2026-11-20-a"))
        with mock.patch.object(server.VSCodeTerminal, "installed", return_value=True):
            self.assertIn(extra, self.frag("/frag/task/2026-11-20-a"))

    def test_task_card(self):
        card = self.frag("/frag/task/2026-11-20-a")
        st = os.stat(self.task_md)
        self.assertEqual(self.card_stamp(card), (st.st_mtime, st.st_size))
        self.assertIn('<div class="task-card" data-task="2026-11-20-a" data-mtime="%r" data-size="%d">'
                      % (st.st_mtime, st.st_size), card)
        self.assertIn('data-copy="@%s"' % self.task_md, card)
        self.assertIn('data-action="task-conversation" data-how="vscode"', card)
        self.assertIn('class="stp cur" data-action="task-stage" data-stage="チケット"', card)
        self.assertIn("単体テストを足した", card)
        with self.db() as conn:
            doc = conn.execute("SELECT id FROM documents WHERE path = ?", (self.task_md,)).fetchone()[0]
        self.assertIn('data-action="open-doc" data-doc="%d" data-how="vscode"' % doc, card)
        # 続きからのボタンはない（既定の再開がコピーでも）
        self.action("/api/task-link", {"task": "2026-11-20-a", "session_id": S1, "action": "add"})
        self.cfg.resume_default = "copy"
        copy = self.frag("/frag/task/2026-11-20-a")
        self.assertNotIn("続きから", copy)
        self.assertNotIn('data-action="resume"', copy)
        self.assertIn('data-action="task-unlink" data-session="%s"' % S1, copy)
        for path in ("/frag/task/", "/frag/task/a%2Fb", "/frag/task/.hidden", "/frag/task/_old", "/frag/task/%00",
                     "/frag/task/2026-11-20-a/x", "/frag/tasks/", "/frag/task"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie=True).status, 404)

    def test_session_card_shows_task_row_and_add(self):
        card = self.frag("/frag/card/" + S1)
        self.assertIn('data-action="task-link"', card)
        self.assertIn('<option value="2026-11-20-a">', card)
        self.assertIn('<option value="2026-11-22-ログイン">', card)
        self.action("/api/task-link", {"task": "2026-11-20-a", "session_id": S1, "action": "add"})
        card = self.frag("/frag/card/" + S1)
        self.assertIn('data-action="task-open" data-task="2026-11-20-a"', card)
        # 結び付いたタスクがあっても、ほかの進行中のタスクに足せる
        self.assertIn('data-action="task-link"', card)
        self.assertNotIn('<option value="2026-11-20-a">', card)
        self.assertIn('<option value="2026-11-22-ログイン">', card)
        # 会話の画面の見出しにも同じ欄
        conv = self.frag("/frag/conversation/" + S1)
        self.assertIn('data-action="task-link" data-session="%s"' % S1, conv)
        self.assertNotIn('<option value="2026-11-20-a">', conv)
        self.assertIn('<option value="2026-11-22-ログイン">', conv)
        # 足せるタスクがなくなれば、欄を出さない
        self.action("/api/task-link", {"task": "2026-11-22-ログイン", "session_id": S1, "action": "add"})
        self.assertNotIn('data-action="task-link"', self.frag("/frag/card/" + S1))
        self.assertNotIn('data-action="task-link"', self.frag("/frag/conversation/" + S1))

    def test_new_task_form_offers_places(self):
        form = self.frag("/frag/new-task")
        self.assertIn('<form id="new-task"', form)
        self.assertIn('<option value="%s"' % self.proj, form)
        self.assertNotIn('value="%s"' % self.tasks_root, form)  # タスクの置き場所はリポジトリにしない
        self.assertNotIn('value="%s"' % self.garden, form)  # 入力履歴にしかない場所は一覧にない

    def test_page_says_whether_there_are_tasks(self):
        template = b'<meta name="tsuzuki-has-tasks" content="{{HAS_TASKS}}">'
        with mock.patch.object(server, "_asset", return_value=template):
            self.assertEqual(self.get("/", cookie=True).body, '<meta name="tsuzuki-has-tasks" content="1">')
            with self.db() as conn:
                conn.execute("DELETE FROM tasks")
                conn.commit()
            self.assertEqual(self.get("/", cookie=True).body, '<meta name="tsuzuki-has-tasks" content="0">')

    # 操作

    def test_task_waiting_without_a_reason_keeps_a_section_with_headings_and_fences(self):
        section = "### 山田さん\n- API\n```sh\nmake\n```\n"
        text = (TASK_MD % self.proj).replace("waiting: false", "waiting: true") + "\n## 待っていること\n" + section
        write_file(self.task_md, text)
        self.reindex(tasks_root=Path(self.tasks_root))
        stamp = self.card_stamp(self.frag("/frag/task/2026-11-20-a"))
        r = self.action("/api/task-waiting", {"task": "2026-11-20-a", "waiting": False, "mtime": stamp[0],
                                              "size": stamp[1]})
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(self.text(), text.replace("waiting: true", "waiting: false"))

    def test_task_waiting_with_a_reason(self):
        stamp = self.card_stamp(self.frag("/frag/task/2026-11-20-a"))
        r = self.action("/api/task-waiting", {"task": "2026-11-20-a", "waiting": True, "reason": "#45 のレビュー",
                                              "mtime": stamp[0], "size": stamp[1]})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "待ちにしました"})
        text = self.text()
        self.assertIn("waiting: true\n", text)
        self.assertEqual(parse_task_md(text).sections["待っていること"], "#45 のレビュー")
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn('<div class="k">待っていること</div><div class="v"><div class="md"><p>#45 のレビュー</p>', card)
        # 古いカードの時刻では書かない
        stale = self.action("/api/task-waiting", {"task": "2026-11-20-a", "waiting": False, "reason": "x",
                                                  "mtime": stamp[0], "size": stamp[1]})
        self.assertEqual(stale.json(), {"ok": False, "needs_confirm": False, "message": CHANGED})
        # 見出しになる行は断る
        stamp = self.card_stamp(card)
        bad = self.action("/api/task-waiting", {"task": "2026-11-20-a", "waiting": False, "reason": "## x",
                                                "mtime": stamp[0], "size": stamp[1]}).json()
        self.assertFalse(bad["ok"])
        self.assertIn("見出し", bad["message"])
        self.assertEqual(self.text(), text)
        # 文字でない reason は 400
        self.assertEqual(self.action("/api/task-waiting", {"task": "2026-11-20-a", "waiting": True, "reason": 5,
                                                           "mtime": stamp[0], "size": stamp[1]}).status, 400)

    def test_task_settings_form_and_save(self):
        form = self.frag("/frag/task-settings/2026-11-20-a")
        self.assertIn('<form id="task-settings"', form)
        self.assertIn('value="ログインのセッション切れを延ばす"', form)
        self.assertIn('value="#123"', form)
        self.assertIn('<option value="%s"' % self.proj, form)
        stamp = self.card_stamp(form)
        ws = os.path.join(self.root, "work.code-workspace")
        write_file(ws, json.dumps({"folders": [{"path": self.proj}]}))
        body = {"task": "2026-11-20-a", "title": "新しい題", "ticket": "#123", "repo": self.proj, "workspace": ws,
                "mtime": stamp[0], "size": stamp[1]}
        r = self.action("/api/task-settings", body)
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message":
                                    '設定を変えました。ワークスペースの folders の先頭に "." を足しました'})
        folders = json.loads(read_file(ws))["folders"]
        self.assertEqual(folders, [{"path": "."}, {"path": self.proj}])
        text = self.text()
        self.assertIn("title: 新しい題\n", text)
        self.assertIn("# 新しい題\n", text)
        self.assertEqual(parse_task_md(text).workspace, ws)
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn("<h4>新しい題</h4>", card)
        # チケットが複数でも、書き方をそろえ、一覧には番号を並べる
        stamp = self.card_stamp(card)
        multi = self.action("/api/task-settings", dict(body, ticket="123、456", mtime=stamp[0], size=stamp[1]))
        self.assertTrue(multi.json()["ok"])
        self.assertIn('ticket: "#123, #456"\n', self.text())
        self.assertIn("#123 #456", self.frag("/frag/tasks"))
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertEqual(card.count("チケットを開く"), 0)
        stamp = self.card_stamp(card)  # 元の値に戻す
        self.assertTrue(self.action("/api/task-settings", dict(body, mtime=stamp[0], size=stamp[1])).json()["ok"])
        card = self.frag("/frag/task/2026-11-20-a")
        # 同じ値で、取り直した時刻で送れば、書かない
        stamp = self.card_stamp(card)
        before = os.stat(self.task_md).st_mtime_ns
        ws_before = (read_file(ws), os.stat(ws).st_mtime_ns)
        same = self.action("/api/task-settings", dict(body, mtime=stamp[0], size=stamp[1]))
        self.assertEqual(same.json(), {"ok": True, "needs_confirm": False, "message": "変わった所はありません"})
        self.assertEqual(os.stat(self.task_md).st_mtime_ns, before)
        self.assertEqual((read_file(ws), os.stat(ws).st_mtime_ns), ws_before)
        # 古いフォームの時刻では書かない
        stale = self.action("/api/task-settings", dict(body, title="また別", mtime=0.5, size=1))
        self.assertEqual(stale.json(), {"ok": False, "needs_confirm": False, "message": CHANGED})
        self.assertIn('<form id="task-settings"', self.frag("/frag/task-settings/" + quote("2026-11-22-ログイン")))
        for bad in ("nope", "shared", "..%2Fx"):
            self.assertEqual(self.get("/frag/task-settings/" + bad, cookie=True).status, 404)

    def settings_body(self, ws):
        card = self.frag("/frag/task/2026-11-20-a")
        stamp = self.card_stamp(card)
        return {"task": "2026-11-20-a", "title": "ログインのセッション切れを延ばす", "ticket": "#123",
                "repo": self.proj, "workspace": ws, "mtime": stamp[0], "size": stamp[1]}

    def test_task_settings_workspace_variants(self):
        ws_dir = os.path.join(self.root, "wsv")
        os.makedirs(ws_dir)
        cases = (
            ("present.code-workspace", {"folders": [{"path": "."}, {"path": self.proj}]}, "設定を変えました"),
            ("elsewhere.code-workspace", {"folders": [{"path": self.proj}, {"path": "."}]},
             '設定を変えました。ワークスペースの folders に "." がありますが、先頭ではありません。'
             "パネルの新しい会話をこのフォルダーで始めるには、先頭に移してください"),
        )
        for name, content, message in cases:
            ws = os.path.join(ws_dir, name)
            write_file(ws, json.dumps(content))
            before = (read_file(ws), os.stat(ws).st_mtime_ns)
            r = self.action("/api/task-settings", self.settings_body(ws))
            self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": message}, name)
            self.assertIn("workspace: %s\n" % ws, self.text())
            self.assertEqual((read_file(ws), os.stat(ws).st_mtime_ns), before, name)
        # 古い時刻なら、新しいワークスペースでもファイルを触らない
        stale_ws = os.path.join(ws_dir, "stale.code-workspace")
        write_file(stale_ws, json.dumps({"folders": [{"path": self.proj}]}))
        os.utime(stale_ws, (1000000, 1000000))
        stale = self.action("/api/task-settings", dict(self.settings_body(stale_ws), mtime=0.5, size=1))
        self.assertFalse(stale.json()["ok"])
        self.assertEqual(json.loads(read_file(stale_ws)), {"folders": [{"path": self.proj}]})
        self.assertEqual(os.stat(stale_ws).st_mtime, 1000000)
        # 足せないときも task.md は書けている
        bad = os.path.join(ws_dir, "bad.code-workspace")
        write_file(bad, json.dumps({"folders": [{"path": self.proj}]}))
        real = os.path.join(ws_dir, "real.code-workspace")
        os.rename(bad, real)
        os.symlink(real, bad)
        r = self.action("/api/task-settings", self.settings_body(bad)).json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["message"], '設定を変えました。ただし、ワークスペースに "." を足せませんでした'
                         '（シンボリックリンクのワークスペースには足せません）。手で folders の先頭に {"path": "."} を足してください')
        self.assertIn("workspace: %s\n" % bad, self.text())
        self.assertEqual(json.loads(read_file(real))["folders"], [{"path": self.proj}])

    def test_task_settings_repo_must_be_a_place(self):
        stamp = self.card_stamp(self.frag("/frag/task/2026-11-20-a"))
        body = {"task": "2026-11-20-a", "title": "x", "ticket": "", "repo": self.root, "workspace": "",
                "mtime": stamp[0], "size": stamp[1]}
        r = self.action("/api/task-settings", body)
        self.assertEqual(r.json(), {"ok": False, "needs_confirm": False, "message": "リポジトリは場所の一覧から選んでください"})
        self.assertEqual(self.text(), TASK_MD % self.proj)
        # 空と、今の task.md の repo の値（一覧になくても）は通る
        self.assertTrue(self.action("/api/task-settings", dict(body, repo="")).json()["ok"])
        write_file(self.task_md, self.text().replace("repo:\n", "repo: /gone/repo\n"))
        self.reindex(tasks_root=Path(self.tasks_root))
        stamp = self.card_stamp(self.frag("/frag/task/2026-11-20-a"))
        r = self.action("/api/task-settings", dict(body, repo="/gone/repo", title="y", mtime=stamp[0], size=stamp[1]))
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(self.action("/api/task-settings", dict(body, workspace=5)).status, 400)

    def test_task_stage_and_waiting(self):
        stamp = self.card_stamp(self.frag("/frag/task/2026-11-20-a"))
        r = self.action("/api/task-stage", {"task": "2026-11-20-a", "stage": "実装", "mtime": stamp[0],
                                            "size": stamp[1]})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "段階を「実装・テスト」にしました"})
        self.assertEqual(self.text(), (TASK_MD % self.proj).replace("stage: チケット", "stage: 実装"))
        # 書いた後はすぐ台帳に取り込み直すので、取り直したカードの時刻と大きさで続けて操作できる
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn('class="stp cur" data-action="task-stage" data-stage="実装"', card)
        stamp = self.card_stamp(card)
        r = self.action("/api/task-waiting", {"task": "2026-11-20-a", "waiting": True, "mtime": stamp[0],
                                              "size": stamp[1]})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "待ちにしました"})
        self.assertIn("waiting: true\n", self.text())
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn('data-action="task-waiting" data-waiting="false">待ちを解く', card)
        stamp = self.card_stamp(card)
        r = self.action("/api/task-waiting", {"task": "2026-11-20-a", "waiting": "false", "mtime": stamp[0],
                                              "size": stamp[1]})
        self.assertEqual(r.json()["message"], "待ちを解きました")
        self.assertIn("waiting: false\n", self.text())
        self.assertIn('<div class="grp">進行中 2</div>', self.frag("/frag/tasks"))

    def test_changed_task_md_is_read_again(self):
        stamp = self.card_stamp(self.frag("/frag/task/2026-11-20-a"))
        with open(self.task_md, "a", encoding="utf-8") as f:  # カードを出した後に Claude が書き足した
            f.write("\n## 次の一手\n結合テストを通す\n")
        r = self.action("/api/task-stage", {"task": "2026-11-20-a", "stage": "調査", "mtime": stamp[0],
                                            "size": stamp[1]})
        self.assertEqual(r.json(), {"ok": False, "needs_confirm": False, "message": CHANGED})
        self.assertIn("stage: チケット", self.text())
        # 読み直したカードは新しい中身と時刻を持ち、それで送れば書ける
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn("結合テストを通す", card)
        st = os.stat(self.task_md)
        self.assertEqual(self.card_stamp(card), (st.st_mtime, st.st_size))
        r = self.action("/api/task-stage", {"task": "2026-11-20-a", "stage": "調査", "mtime": st.st_mtime,
                                            "size": st.st_size})
        self.assertEqual(r.json()["message"], "段階を「調査」にしました")
        self.assertTrue(self.text().endswith("## 次の一手\n結合テストを通す\n"))

    def test_task_new(self):
        body = {"title": "ログイン", "short_name": "login", "ticket": "", "repo": ""}
        made = self.action("/api/task-new", body).json()
        self.assertEqual(sorted(made), ["message", "ok", "task", "task_md"])
        self.assertEqual((made["ok"], made["message"]), (True, "タスクを作りました"))
        self.assertRegex(made["task"], r"^\d{4}-\d\d-\d\d-login$")
        self.assertEqual(made["task_md"], os.path.join(self.tasks_root, made["task"], "task.md"))
        # すぐに台帳に入るので、カードを開ける
        card = self.frag("/frag/task/" + quote(made["task"]))
        self.assertIn("<h4>ログイン</h4>", card)
        again = self.action("/api/task-new", body).json()
        self.assertEqual((again["ok"], again["needs_confirm"]), (False, False))
        self.assertIn("同じ名前のフォルダーがあります", again["message"])
        # リポジトリは場所の一覧から選ぶ。チケットの番号には # を付ける
        made = self.action("/api/task-new", dict(body, short_name="with repo", ticket="45", repo=self.proj)).json()
        self.assertTrue(made["task"].endswith("-with-repo"))
        self.assertIn("repo: %s\n" % self.proj, self.text(made["task_md"]))
        self.assertIn('ticket: "#45"\n', self.text(made["task_md"]))
        many = self.action("/api/task-new", dict(body, short_name="many", ticket="123, 456")).json()
        self.assertIn('ticket: "#123, #456"\n', self.text(many["task_md"]))
        # 任意の欄は省いてもよい
        self.assertTrue(self.action("/api/task-new", {"title": "省略", "short_name": "short"}).json()["ok"])
        for changes, message in (({"short_name": "../x"}, "タスクの短い名前に使えない文字があります"),
                                 ({"short_name": "a/b"}, "タスクの短い名前に使えない文字があります"),
                                 ({"short_name": "a\x01b"}, "タスクの短い名前に制御文字は使えません"),
                                 ({"title": "  "}, "題を入れてください"),
                                 ({"repo": "/etc"}, "リポジトリは場所の一覧から選んでください"),
                                 ({"repo": self.tasks_root}, "リポジトリは場所の一覧から選んでください")):
            with self.subTest(changes=changes):
                r = self.action("/api/task-new", dict(dict(body, short_name="other"), **changes))
                self.assertEqual(r.status, 200)
                answer = r.json()
                self.assertEqual((answer["ok"], answer["needs_confirm"]), (False, False))
                self.assertIn(message, answer["message"])
        self.assertFalse([n for n in os.listdir(self.tasks_root) if n.endswith(("-other", "-x", "-b"))])

    def test_task_link_add_and_remove(self):
        r = self.action("/api/task-link", {"task": "2026-11-20-a", "session_id": S1, "action": "add"})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "タスクに足しました"})
        card = self.frag("/frag/task/2026-11-20-a")
        self.assertIn('<div class="link" data-action="card" data-session="%s">' % S1, card)
        self.assertIn("手で足した ・ 確定", card)
        r = self.action("/api/task-link", {"task": "2026-11-20-a", "session_id": S1, "action": "remove"})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "タスクから外しました"})
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM task_sessions WHERE session_id = ?", (S1,)).fetchone())
            self.assertEqual([tuple(r) for r in conn.execute("SELECT task, action FROM task_overrides "
                                                             "WHERE session_id = ?", (S1,))],
                             [("2026-11-20-a", "remove")])
        self.assertNotIn('data-session="%s"' % S1, self.frag("/frag/task/2026-11-20-a"))

    def test_task_conversation(self):
        r = self.action("/api/task-conversation", {"task": "2026-11-20-a", "how": "terminal"})
        self.assertEqual(r.json(), {"ok": True, "needs_confirm": False, "message": "Terminal で新しい会話を始めました",
                                    "command": None})
        self.assertEqual(self.calls[-1][0], "osascript")
        copy = self.action("/api/task-conversation", {"task": "2026-11-20-a", "how": "copy"}).json()
        self.assertEqual(copy["command"], "cd %s && claude '@%s を読んで、次の一手から進めてください。'"
                         % (self.proj, self.task_md))
        self.assertEqual(len(self.calls), 1)

    def test_task_conversation_in_a_workspace(self):
        ws_dir = os.path.join(self.root, "ws")
        os.makedirs(os.path.join(ws_dir, "app"))
        ws = os.path.join(ws_dir, "x.code-workspace")
        write_file(ws, json.dumps({"folders": [{"path": "app"}]}))
        write_file(self.task_md, (TASK_MD % self.proj).replace("repo:", "workspace: %s\nrepo:" % ws, 1))
        self.reindex(tasks_root=Path(self.tasks_root))
        copy = self.action("/api/task-conversation", {"task": "2026-11-20-a", "how": "copy"}).json()
        self.assertTrue(copy["command"].startswith("cd %s && claude '" % ws_dir), copy["command"])
        self.assertEqual(self.calls, [])

    def test_unknown_tasks_sessions_and_values(self):
        st = os.stat(self.task_md)
        stamp = {"mtime": st.st_mtime, "size": st.st_size}
        cases = (
            ("/api/task-stage", dict(stamp, task="nope", stage="PR"), "台帳にないタスクです"),
            ("/api/task-stage", dict(stamp, task="../2026-11-20-a", stage="PR"), "台帳にないタスクです"),
            ("/api/task-stage", dict(stamp, task="2026-11-20-a/../2026-11-20-a", stage="PR"), "台帳にないタスクです"),
            ("/api/task-stage", dict(stamp, task="2026-11-20-a", stage="途中"), "段階の値が正しくありません: 途中"),
            ("/api/task-waiting", dict(stamp, task="shared", waiting=True), "台帳にないタスクです"),
            ("/api/task-link", {"task": "nope", "session_id": S1, "action": "add"}, "台帳にないタスクです"),
            ("/api/task-link", {"task": "2026-11-20-a", "session_id": sid(4), "action": "add"}, "台帳にないセッションです"),
            ("/api/task-link", {"task": "2026-11-20-a", "session_id": S1, "action": "toggle"},
             "結び付けの操作が正しくありません"),
            ("/api/task-conversation", {"task": "nope", "how": "copy"}, "台帳にないタスクです"),
            ("/api/task-conversation", {"task": "2026-11-20-a", "how": "fax"}, "開き方が正しくありません"),
        )
        for path, body, message in cases:
            with self.subTest(path=path, body=body):
                r = self.action(path, body)
                self.assertEqual(r.status, 200)
                answer = r.json()
                self.assertEqual((answer["ok"], answer["needs_confirm"], answer["message"]), (False, False, message))
        self.assertIn("stage: チケット", self.text())
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM task_overrides").fetchone())
        self.assertEqual(self.calls, [])

    def test_task_actions_reject_malformed_bodies(self):
        st = os.stat(self.task_md)
        good = {"task": "2026-11-20-a", "mtime": st.st_mtime, "size": st.st_size}
        cases = (
            ("/api/task-stage", dict(good, stage=1)),
            ("/api/task-stage", dict(good, stage="PR", task=["2026-11-20-a"])),
            ("/api/task-stage", {"task": "2026-11-20-a", "stage": "PR"}),
            ("/api/task-stage", dict(good, stage="PR", mtime="1.5")),
            ("/api/task-stage", dict(good, stage="PR", mtime=True)),
            ("/api/task-stage", dict(good, stage="PR", mtime=float("nan"))),
            ("/api/task-stage", dict(good, stage="PR", size=1.5)),
            ("/api/task-stage", dict(good, stage="PR", size=-1)),
            ("/api/task-stage", dict(good, stage="PR", size=True)),
            ("/api/task-waiting", dict(good, waiting="yes")),
            ("/api/task-waiting", dict(good, waiting=1)),
            ("/api/task-waiting", good),
            ("/api/task-new", {"title": "x", "short_name": 3}),
            ("/api/task-new", {"title": None, "short_name": "x"}),
            ("/api/task-new", {"title": "x", "short_name": "x", "repo": 1}),
            ("/api/task-new", {"title": "x", "short_name": "x", "ticket": ["1"]}),
            ("/api/task-new", {"title": "\ud800", "short_name": "x"}),  # UTF-8 にできない文字
            ("/api/task-link", {"task": "2026-11-20-a", "session_id": 1, "action": "add"}),
            ("/api/task-link", {"task": "2026-11-20-a", "session_id": S1}),
            ("/api/task-conversation", {"task": "2026-11-20-a", "how": None}),
            ("/api/task-conversation", {"task": 5, "how": "copy"}),
        )
        for path, body in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.action(path, body).status, 400)
        self.assertIn("stage: チケット", self.text())
        self.assertEqual(sorted(os.listdir(self.tasks_root)), ["2026-11-20-a", "2026-11-22-ログイン", "shared"])
        self.assertEqual(self.calls, [])

    def test_task_actions_need_origin_and_csrf(self):
        st = os.stat(self.task_md)
        bodies = {"/api/task-new": {"title": "x", "short_name": "x", "ticket": "", "repo": ""},
                  "/api/task-stage": {"task": "2026-11-20-a", "stage": "PR", "mtime": st.st_mtime, "size": st.st_size},
                  "/api/task-waiting": {"task": "2026-11-20-a", "waiting": True, "mtime": st.st_mtime,
                                        "size": st.st_size},
                  "/api/task-link": {"task": "2026-11-20-a", "session_id": S1, "action": "add"},
                  "/api/task-conversation": {"task": "2026-11-20-a", "how": "terminal"}}
        checks = (("no csrf", dict(cookie=True, origin=self.origin)),
                  ("wrong csrf", dict(cookie=True, origin=self.origin, csrf="x" * 32)),
                  ("token as csrf", dict(cookie=True, origin=self.origin, csrf=self.app.token)),
                  ("other origin", dict(cookie=True, csrf=self.app.csrf, origin="http://evil.example")),
                  ("no origin", dict(cookie=True, csrf=self.app.csrf)),
                  ("no cookie", dict(csrf=self.app.csrf, origin=self.origin)))
        for path, body in bodies.items():
            for name, kw in checks:
                with self.subTest(path=path, check=name):
                    self.assertEqual(self.post(path, body, **kw).status, 403)
            with self.subTest(path=path, check="GET"):
                self.assertEqual(self.get(path, cookie=True).status, 404)  # 状態を変える操作は POST だけ
        self.assertEqual(sorted(os.listdir(self.tasks_root)), ["2026-11-20-a", "2026-11-22-ログイン", "shared"])
        self.assertEqual(self.text(), TASK_MD % self.proj)
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM task_overrides").fetchone())
        self.assertEqual(self.calls, [])

    def test_write_failures_are_answers_not_errors(self):
        # 書けない（権限・ディスク・UTF-8 にできない文字）ときも 500 にせず、文で返す
        st = os.stat(self.task_md)
        stage = {"task": "2026-11-20-a", "stage": "PR", "mtime": st.st_mtime, "size": st.st_size}
        new = {"title": "x", "short_name": "x", "ticket": "", "repo": ""}
        errors = ((PermissionError(13, "Permission denied"), "%s: [Errno 13] Permission denied"),
                  (OSError(28, "No space left on device"), "%s: [Errno 28] No space left on device"),
                  (UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"), "%s: 'utf-8' codec can't "
                   "encode character '\\ud800' in position 0: surrogates not allowed"),
                  (TaskWriteError("task.md を書けません: \ud800"), "task.md を書けません: ?"))
        for name, path, body, lead in (("set_fields", "/api/task-stage", stage, "task.md を書けませんでした"),
                                       ("create_task", "/api/task-new", new, "タスクを作れませんでした")):
            for error, message in errors:
                with self.subTest(target=name, error=type(error).__name__), \
                        mock.patch.object(server, name, side_effect=error):
                    r = self.action(path, body)
                    self.assertEqual(r.status, 200)
                    self.assertEqual(r.json(), {"ok": False, "needs_confirm": False,
                                                "message": message.replace("%s", lead)})
        self.assertIn("stage: チケット", self.text())

    def test_actions_without_a_tasks_root(self):
        _, app = self.start_server()  # タスクの置き場所を渡さないサーバー
        st = os.stat(self.task_md)
        for path, body in (("/api/task-new", {"title": "x", "short_name": "x"}),
                           ("/api/task-stage", {"task": "2026-11-20-a", "stage": "PR", "mtime": st.st_mtime,
                                                "size": st.st_size}),
                           ("/api/task-link", {"task": "2026-11-20-a", "session_id": S1, "action": "add"})):
            with self.subTest(path=path):
                answer = self.action(path, body, app=app).json()
                self.assertEqual((answer["ok"], answer["message"]), (False, "タスクの置き場所が決まっていません"))
        self.assertIn("stage: チケット", self.text())

    def test_updates_keep_the_tasks_root(self):
        # 更新は必ずタスクの置き場所を渡す（渡さないと、場所の表示名「タスク」「共通の資料」が戻ってしまう）
        real, calls = server.update, []

        def recording(conn, cfg, **kw):
            calls.append(kw)
            return real(conn, cfg, **kw)
        with mock.patch.object(server, "update", recording):
            self.assertIsNotNone(self.app.run_update())
        self.assertEqual(calls[0]["tasks_root"], Path(self.tasks_root))
        with self.db() as conn:
            displays = {r[0] for r in conn.execute("SELECT display FROM places")}
        self.assertTrue({"タスク", "共通の資料"} <= displays, displays)


class IndexerThreadTest(ServerCase):
    """更新のスレッド（決めごと 11）。更新を差し替えて、失敗と進み具合を確かめる。"""

    def test_runs_at_start_and_survives_an_exception(self):
        real, calls = server.update, []

        def flaky(conn, cfg, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise RuntimeError("壊れた記録")
            return real(conn, cfg, **kw)
        with mock.patch.object(server, "update", flaky):
            _, app = self.start_server(start_indexer=True)
            self.wait_for(lambda: self.status(app)["errors"] == ["更新に失敗しました: 壊れた記録"])
            self.assertEqual(self.get("/frag/list", cookie=True, app=app).status, 200)
            self.assertEqual(calls[0]["claude_dir"], self.fake.dir)
            self.assertTrue(callable(calls[0]["progress"]))
            self.assertEqual(self.action("/api/reindex", {}, app=app).json(), {"ok": True})
            self.wait_for(lambda: len(calls) >= 2 and self.status(app)["errors"] == [])
            # スレッドは生きていて、また頼めば更新する
            self.action("/api/reindex", {}, app=app)
            self.wait_for(lambda: len(calls) >= 3)

    def test_status_shows_progress_while_updating(self):
        real, calls = server.update, []
        started, release = threading.Event(), threading.Event()

        def slow(conn, cfg, *, progress=None, **kw):
            calls.append(1)
            progress(3, 10)
            started.set()
            release.wait(10)
            return real(conn, cfg, progress=progress, **kw)
        with mock.patch.object(server, "update", slow):
            _, app = self.start_server(start_indexer=True)
            try:
                self.assertTrue(started.wait(10))
                st = self.status(app)
                self.assertEqual(st["indexing"], {"done": 3, "total": 10})
                self.assertEqual(self.get("/frag/list", cookie=True, app=app).status, 200)
                self.assertEqual(self.action("/api/reindex", {}, app=app).json(), {"ok": True})
                self.assertEqual(len(calls), 1)  # 同時には走らせない
            finally:
                release.set()
            # 更新中に頼まれた分は、今の更新が終わった後にもう1回走る（refresh_seconds の30秒を待たずに）
            self.wait_for(lambda: len(calls) >= 2)
            self.wait_for(lambda: self.status(app)["indexing"] is None)

    def test_refreshes_every_interval(self):
        # 頼まれなくても refresh_seconds ごとに更新し、その間は待つ（短い間隔を差し込んで数える）
        real, calls = server.update, []

        def counting(conn, cfg, **kw):
            calls.append(time.monotonic())
            return real(conn, cfg, **kw)
        self.cfg.refresh_seconds = 0.2
        with mock.patch.object(server, "update", counting), mock.patch.object(server, "REFRESH_RANGE", (0.1, 60.0)):
            self.start_server(start_indexer=True)
            self.wait_for(lambda: len(calls) >= 3)
        gaps = [later - earlier for earlier, later in zip(calls, calls[1:3])]
        self.assertTrue(all(gap >= 0.15 for gap in gaps), gaps)

    def test_refresh_interval_is_clamped(self):
        # 間隔は1秒から1日まで。数でない値は既定の30秒（0 で回り続けたり、大きすぎてスレッドが止まったりしない）
        for value, seconds in ((30, 30.0), (2.5, 2.5), (0, 1.0), (-5, 1.0), (0.2, 1.0), (10 ** 12, 86400.0),
                               (float("inf"), 86400.0), (float("nan"), 30.0), ("30", 30.0), (True, 30.0),
                               (None, 30.0)):
            with self.subTest(value=value):
                self.assertEqual(server._refresh_seconds(value), seconds)
        real, calls = server.update, []

        def counting(conn, cfg, **kw):
            calls.append(1)
            return real(conn, cfg, **kw)
        self.cfg.refresh_seconds = 0
        with mock.patch.object(server, "update", counting):
            self.start_server(start_indexer=True)
            self.wait_for(lambda: len(calls) >= 1)
            time.sleep(0.5)
            self.assertEqual(len(calls), 1)  # 設定が 0 でも、次の更新まで1秒は待つ

    def test_updates_never_overlap(self):
        real, active, overlaps = server.update, [], []
        lock = threading.Lock()

        def watched(conn, cfg, **kw):
            with lock:
                active.append(1)
                if len(active) > 1:
                    overlaps.append(1)
            try:
                time.sleep(0.05)
                return real(conn, cfg, **kw)
            finally:
                with lock:
                    active.pop()
        with mock.patch.object(server, "update", watched):
            _, app = self.start_server(start_indexer=True)
            threads = [threading.Thread(target=app.run_update) for _ in range(3)]
            for th in threads:
                th.start()
            self.action("/api/reindex", {}, app=app)
            for th in threads:
                th.join()
        self.assertEqual(overlaps, [])


class ServeTest(ServerCase):
    def test_port_in_use(self):
        s = socket.socket()
        self.addCleanup(s.close)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = serve(self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home,
                         port=s.getsockname()[1], open_browser=False)
        self.assertEqual(code, 2)
        self.assertIn("使用中", err.getvalue())
        self.assertEqual(err.getvalue().strip(),
                         "ポート %d は使用中です。--port か設定 port で別の番号を指定してください" % s.getsockname()[1])

    def test_port_in_use_by_other_listeners(self):
        # SO_REUSEADDR のままでは、*:port や [::]:port で待ち受けている物があっても 127.0.0.1:port に bind できて
        # しまう。::1 だけで待ち受けている物は localhost を横取りする。どれも「使用中」として断る
        listeners = [(socket.AF_INET, "0.0.0.0", False), (socket.AF_INET6, "::", True),
                     (socket.AF_INET6, "::1", False)]
        for family, host, dual in listeners:
            with self.subTest(host=host):
                try:
                    s = socket.socket(family)
                except OSError:
                    self.skipTest("この環境では IPv6 のソケットを作れません")
                self.addCleanup(s.close)
                try:
                    if dual:
                        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    s.bind((host, 0))
                except OSError:
                    self.skipTest("この環境では %s で待ち受けられません" % host)
                s.listen(1)
                port = s.getsockname()[1]
                err = io.StringIO()
                # 待ち受けを始めてしまったときに止まらないよう、serve_forever はすぐ終わらせる
                with mock.patch.object(server._Server, "serve_forever", side_effect=KeyboardInterrupt), \
                        contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = serve(self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home,
                                 port=port, open_browser=False)
                self.assertEqual(code, 2)
                self.assertEqual(err.getvalue().strip(),
                                 "ポート %d は使用中です。--port か設定 port で別の番号を指定してください" % port)

    def test_serve_prints_the_url_and_stops_on_ctrl_c(self):
        out = io.StringIO()
        with mock.patch.object(server._Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(server.webbrowser, "open") as browser, contextlib.redirect_stdout(out):
            code = serve(self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home, port=0,
                         open_browser=True)
        self.assertEqual(code, 0)
        line = out.getvalue().strip()
        token = (self.ledger / TOKEN_FILE).read_text(encoding="utf-8").strip()
        self.assertRegex(line, r"^tsuzuki: http://127\.0\.0\.1:\d+/\?key=%s$" % token)
        browser.assert_called_once_with(line[len("tsuzuki: "):])
        with mock.patch.object(server._Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(server.webbrowser, "open") as browser, contextlib.redirect_stdout(io.StringIO()):
            serve(self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home, port=0,
                  open_browser=False)
        browser.assert_not_called()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--claude-dir", str(self.fake.dir), "--ledger-dir", str(self.ledger)] + list(args)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_serve_command(self):
        with mock.patch.object(server, "serve", return_value=0) as fake_serve:
            code, _, _ = self.cli("serve", "--port", "9123", "--no-open")
            self.assertEqual(code, 0)
            kw = fake_serve.call_args.kwargs
            self.assertEqual((kw["port"], kw["open_browser"], kw["ledger_dir"], kw["claude_dir"]),
                             (9123, False, self.ledger, self.fake.dir))
            self.assertEqual(kw["home"], os.path.expanduser("~"))
            self.cli("serve", "--port", "9124")
            self.assertEqual(fake_serve.call_args.kwargs["open_browser"], True)
            with mock.patch.dict(os.environ, {"TSUZUKI_PORT": "9125"}):
                self.cli("serve")
            self.assertEqual(fake_serve.call_args.kwargs["port"], 9125)
            (self.ledger / "config.json").write_text('{"open_browser": false, "port": 9126}', encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TSUZUKI_PORT", None)
                self.cli("serve")
            self.assertEqual((fake_serve.call_args.kwargs["port"], fake_serve.call_args.kwargs["open_browser"]),
                             (9126, False))
            calls = fake_serve.call_count
            for port in ("65536", "-1"):
                with self.subTest(port=port):
                    code, _, err = self.cli("serve", "--port", port)
                    self.assertEqual(code, 2)
                    self.assertEqual(err.strip(), "ポートは 0〜65535 で指定してください")
            self.assertEqual(fake_serve.call_count, calls)
            # CLI の --port は 0（空いている番号）を受け取る。--exit-on-stdin-eof は付けたときだけ真
            code, _, _ = self.cli("serve", "--port", "0", "--no-open")
            self.assertEqual(code, 0)
            kw = fake_serve.call_args.kwargs
            self.assertEqual((kw["port"], kw["exit_on_stdin_eof"]), (0, False))
            self.cli("serve", "--port", "0", "--no-open", "--exit-on-stdin-eof")
            self.assertIs(fake_serve.call_args.kwargs["exit_on_stdin_eof"], True)

    def test_serve_and_index_pass_the_tasks_root(self):
        # serve と index は、設定 tasks_root を解いた置き場所を必ず渡す
        tasks = os.path.join(self.root, "my tasks")
        (self.ledger / "config.json").write_text(json.dumps({"tasks_root": tasks}), encoding="utf-8")
        with mock.patch.object(server, "serve", return_value=0) as fake_serve:
            code, _, _ = self.cli("serve", "--port", "9123", "--no-open")
        self.assertEqual(code, 0)
        self.assertEqual(fake_serve.call_args.kwargs["tasks_root"], Path(tasks))
        with mock.patch.object(indexer, "update", wraps=indexer.update) as spy:
            code, out, _ = self.cli("index")
        self.assertEqual(code, 0, out)
        self.assertEqual(spy.call_args.kwargs["tasks_root"], Path(tasks))
        # 置き場所にできない所（ホームの直下など）なら、理由を出して 1 で終える。サーバーも更新も始めない
        for raw, reason in (("~", "設定 tasks_root にホームやボリュームの直下は指定できません"),
                            (str(self.fake.dir / "tasks"), "設定 tasks_root に Claude Code の設定ディレクトリの中は"
                                                           "指定できません")):
            (self.ledger / "config.json").write_text(json.dumps({"tasks_root": raw}), encoding="utf-8")
            for args in (("serve", "--port", "9123", "--no-open"), ("index",)):
                with self.subTest(raw=raw, args=args), mock.patch.object(server, "serve") as fake_serve, \
                        mock.patch.object(indexer, "update") as fake_update:
                    code, _, err = self.cli(*args)
                    self.assertEqual(code, 1)
                    self.assertTrue(err.startswith("tsuzuki: " + reason), err)
                    fake_serve.assert_not_called()
                    fake_update.assert_not_called()

    def test_serve_passes_the_tasks_root_to_the_app(self):
        tasks = Path(self.root) / "tasks"
        with mock.patch.object(server._Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(server, "make_server", wraps=server.make_server) as spy, \
                contextlib.redirect_stdout(io.StringIO()):
            serve(self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home, port=0,
                  open_browser=False, tasks_root=tasks)
        self.assertEqual(spy.call_args.kwargs["tasks_root"], tasks)

    def test_exit_on_stdin_eof_stops_the_server(self):
        r, w = os.pipe()
        result = []
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            th = threading.Thread(target=lambda: result.append(serve(
                self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home, port=0,
                open_browser=False, exit_on_stdin_eof=True, stdin=os.fdopen(r, "rb"))), daemon=True)
            th.start()
            deadline = time.monotonic() + 10
            m = None
            while m is None and time.monotonic() < deadline:
                m = re.search(r"tsuzuki: http://127\.0\.0\.1:(\d+)/", out.getvalue())
                if m is None:
                    time.sleep(0.05)
            try:
                self.assertIsNotNone(m, out.getvalue())
                port = int(m.group(1))
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
            finally:
                os.close(w)
            th.join(5)
        self.assertFalse(th.is_alive())
        self.assertEqual(result, [0])
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_port_zero_runs_beside_a_fixed_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            fixed = s.getsockname()[1]
        httpd, app = make_server(self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home, port=fixed,
                                  start_indexer=False)
        self.addCleanup(httpd.server_close)
        out = io.StringIO()
        with mock.patch.object(server._Server, "serve_forever", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(out):
            code = serve(self.cfg, ledger_dir=self.ledger, claude_dir=self.fake.dir, home=self.home, port=0,
                         open_browser=False)
        self.assertEqual(code, 0)
        got = int(re.search(r"tsuzuki: http://127\.0\.0\.1:(\d+)/", out.getvalue()).group(1))
        self.assertNotEqual(got, fixed)
        self.assertGreater(got, 0)

    def test_cli_reports_a_busy_ledger(self):
        # 台帳を開けない・書けない（使用中など）ときは、どのサブコマンドでも日本語の案内で終える（決めごと 17）
        busy = sqlite3.OperationalError("database is locked")
        with socket.socket() as s:  # serve が待ち受けられる、空いているポート
            s.bind(("127.0.0.1", 0))
            free = str(s.getsockname()[1])
        for args in (("search", "x"), ("index",), ("forget", S1), ("purge", "--before", "2026-01-01"),
                     ("resume", S1), ("serve", "--port", free, "--no-open")):
            with self.subTest(args=args), mock.patch("tsuzuki.store.sqlite3.connect", side_effect=busy):
                code, _, err = self.cli(*args)
                self.assertEqual(code, 1)
                self.assertEqual(err.strip(), "台帳を開けませんでした（使用中かもしれません）: database is locked")


if __name__ == "__main__":
    unittest.main()
