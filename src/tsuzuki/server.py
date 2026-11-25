"""画面のサーバー。この Mac の中から、鍵を持って来た
要求だけを受け付ける。

要求ごとに台帳の接続を開いて読み、経路に応じて views・taskviews・ui_render・ui_tasks・search・resume を呼ぶ
（画面の中身の HTML はここで作らない）。台帳の更新は別のスレッドが自分の接続で行い、その間も要求は読める
（WAL。開くときに書かない）。タスクの置き場所に書く操作（新しいタスク・段階・待ち）と結び付けを直す操作は、
更新と同じ鍵（indexer.update_lock）の中で行い、書いたタスクだけをすぐ台帳に取り込み直す。
要求の記録（ログ）は書かない。URL に検索の語が入るため。500・503 にした失敗だけを、例外の種類と呼び出しの位置
（例外の文と要求の中身は除く）で stderr に1行書く。
"""
import errno
import functools
import hmac
import html
import json
import math
import os
import pkgutil
import re
import secrets
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import OrderedDict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote

from tsuzuki import taskviews, ui_render, ui_tasks, views
from tsuzuki.config import (Config, ConfigError, normalize_tasks_root_input, normalize_ticket_url, resolve_tasks_root,
                            write_tasks_root, write_ticket_url)
from tsuzuki.doctor import CHECK_ALLOW, CHECK_CLAUDE_MD, task_checks
from tsuzuki.indexer import UpdateReport, update, update_lock
from tsuzuki.resume import ActionOutcome, is_session_id, open_document, open_place, resume, start_conversation
from tsuzuki.vscode_ext import VSCodeTerminal, requests_dir
from tsuzuki.retention import iso_utc
from tsuzuki.search import (DEFAULT_TARGETS, MAX_TERM_CHARS, MAX_TERMS, ORDERS, RESULT_LIMIT, TARGETS, DocHit,
                           SearchQuery, SearchResult, parse_query, search)
from tsuzuki.sources.live import LiveSession, live_sessions
from tsuzuki.store import LEDGER_FILENAME, get_meta, open_ledger, set_meta
from tsuzuki.taskindex import ROOT_KEY, forget_task, refresh_task, reset_task_index
from tsuzuki.tasklinks import link_tasks, set_override
from tsuzuki.tasks import SHARED_DIR, TASK_MD, is_task_dir_name, parse_task_md, stage_label, task_title, within
from tsuzuki.taskwrite import TaskWriteError, copy_into_task, create_task, set_fields, set_waiting, trash_task, update_settings

TOKEN_FILE = "token"
COOKIE = "tsuzuki_key"
CSRF_HEADER = "X-Tsuzuki-CSRF"
HOST = "127.0.0.1"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-src 'self'; "
       "base-uri 'none'; form-action 'self'")
# HTML の資料（/doc-raw/）には、ページの CSP の代わりにこれを付ける。隔離に加えて、枠の中から外への自動の読み込みを止める
DOC_CSP = ("sandbox allow-scripts allow-popups; default-src 'none'; script-src 'unsafe-inline'; "
           "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; "
           "form-action 'none'; base-uri 'none'")
# Mermaid の図の枠（/mermaid-frame）。ライブラリはページの中にあり、外からは何も読み込まない
MERMAID_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
               "img-src data:; font-src data:; form-action 'none'; base-uri 'none'")
MERMAID_PAGE = "ui/mermaid-frame.html"  # 枠のページ（MERMAID_MARK の所にライブラリを入れる）
MERMAID_LIBRARY = "ui/vendor/mermaid.min.js"
MERMAID_MARK = "{{MERMAID}}"
# 同梱の Mermaid の版（ui/vendor/VENDOR.md）。画面はこの版を付けた URL で枠を開き、サーバーはその URL のときだけ
# ブラウザに1日覚えさせる（約3.5MB のページを図ごとに取り直さない。版を上げれば URL が変わる）
MERMAID_VERSION = "11.17.2"
MERMAID_FRAME_URL = "/mermaid-frame?v=" + MERMAID_VERSION
MERMAID_CACHE = "private, max-age=86400"
NO_STORE = "no-store"
MAX_BODY = 64 * 1024  # POST の本文の上限
MAX_DRAIN = 1024 * 1024  # 大きすぎる本文を、断る前に読み捨てる上限（読まずに閉じると応答が届かないことがある）
MAX_DOC_BYTES = 10 * 1024 * 1024  # /doc-raw/ で返す資料の上限
MAX_FIELDS = 100  # 問い合わせ（?…）の項目の数の上限
PAGE_SIZE = 50  # 子と、親が分からないヘッドレスを1回に出す数
LIST_DAYS = 30  # 一覧の既定の期間（日）
CACHE_SIZE = 8  # 覚えておく検索の結果の数
CACHE_SECONDS = 30.0
REQUEST_TIMEOUT = 30  # 1つの接続を待つ秒数
PROBE_SECONDS = 0.3  # 起動の前に、ほかのプログラムが待ち受けていないか確かめる接続の待ち時間
DEFAULT_REFRESH = 30.0
REFRESH_RANGE = (1.0, 24 * 3600.0)  # 更新の間隔（設定 refresh_seconds）として使う範囲

HTML = "text/html; charset=utf-8"
JS = "application/javascript; charset=utf-8"
CSS = "text/css; charset=utf-8"
JSON = "application/json; charset=utf-8"
TEXT = "text/plain; charset=utf-8"

NOT_AUTHORIZED = "tsuzuki serve が表示した URL から開いてください"
BAD_HOST = "この宛先（Host）への要求は受け付けません"
BAD_ORIGIN = "操作を確かめられませんでした。画面を読み込み直してください"
NOT_FOUND = "見つかりません"
BAD_REQUEST = "要求の形が正しくありません: %s"
TOO_LARGE = "要求が大きすぎます"
LEDGER_BUSY = "台帳を読めませんでした（使用中かもしれません）"
INTERNAL_ERROR = "内部のエラーが起きました"
FAILURE_LOG = "tsuzuki: 要求の処理に失敗しました（%d %s）: %s"  # 状態コード、例外の種類、呼び出しの位置
PORT_IN_USE = "ポート %d は使用中です。--port か設定 port で別の番号を指定してください"
START_FAILED = "tsuzuki: サーバーを起動できませんでした: %s"
UPDATE_FAILED = "更新に失敗しました: %s"
# タスクの操作の答えの文
NO_TASKS_ROOT = "タスクの置き場所が決まっていません"
UNKNOWN_TASK = "台帳にないタスクです"
TRASHED = "ゴミ箱に入れました。戻すときは、Finder でゴミ箱から %s へドラッグしてください"
UNKNOWN_SESSION = "台帳にないセッションです"
UNKNOWN_DOC = "台帳にない資料です"
COPIED = "タスクのフォルダーにコピーしました: %s"
BAD_LINK_ACTION = "結び付けの操作が正しくありません"
NO_TITLE = "題を入れてください"
CANNOT_CREATE = "タスクを作れませんでした: %s"
CANNOT_WRITE = "task.md を書けませんでした: %s"
NOT_A_PLACE = "リポジトリは場所の一覧から選んでください"
TASK_MADE = "タスクを作りました"
STAGE_SET = "段階を「%s」にしました"
WAITING_SET = {True: "待ちにしました", False: "待ちを解きました"}
SETTINGS_SET = "設定を変えました"
NO_SETTINGS_CHANGE = "変わった所はありません"
TICKET_URL_SET = "チケットのリンク先を変えました"
TASKS_ROOT_SET = "タスクの置き場所を変えました。新しい置き場所のタスクを読み込んでいます"
TASKS_ROOT_MISSING = ("今の置き場所のタスク %d件は、新しい置き場所にありません。切り替えると、それらは一覧から消えます。"
                      "手で足した・外した結び付けの記録は残るので、フォルダーを新しい置き場所に移せば元に戻ります。続けますか？")
TICKET_URL_RESET = "チケットのリンク先を、リポジトリの Issue に戻しました"
LINK_SET = {"add": "タスクに足しました", "remove": "タスクから外しました"}
TASK_STATUSES = ("open", "done", "all")  # 一覧と左の状況の絞り込み（空は open）
# http.server が自分で返す誤り（壊れた要求、知らない方法など）の文
_HTTP_ERRORS = {400: "要求の形が正しくありません", 414: "要求が長すぎます", 431: "要求の見出しが大きすぎます",
                501: "この方法の要求は受け付けません"}

_STATIC = {"/static/app.js": ("ui/app.js", JS), "/static/app.css": ("ui/app.css", CSS)}
_HTML_EXTS = (".html", ".htm")
_SCRIPT_TAG = re.compile(r"</?script", re.I)  # <script> の中に入れると、そこで終わってしまう文
_NUMBER = re.compile(r"[0-9]{1,18}")  # 台帳の整数の ID・offset・日数・ターン（SQLite の整数に収まる桁）
_TOKEN = re.compile(r"[0-9a-f]{64}")


class _HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class App:
    """サーバーに1つ。鍵（token）・CSRF の鍵・ポートと、更新の状態（last_report・last_error・progress・running。
    lock で守る）、検索の結果の覚え書きを持つ。更新のスレッドもここにある。tasks_root はタスクの置き場所
    （config.resolve_tasks_root で解いたもの。tsuzuki serve は必ず渡す。None ならタスクの操作は断る）。"""

    def __init__(self, cfg: Config, *, ledger_dir: Path, claude_dir: Path, home: str, port: int, token: str,
                 runner: Callable[..., Any], sleep: Callable[[float], None], tasks_root: Optional[Path] = None):
        self.cfg = cfg
        self.ledger_dir, self.claude_dir, self.home = Path(ledger_dir), Path(claude_dir), home
        self.tasks_root = None if tasks_root is None else Path(tasks_root)
        self.port, self.token = port, token
        self.csrf = secrets.token_urlsafe(24)  # 起動ごとに作る
        self.hosts = ("127.0.0.1:%d" % port, "localhost:%d" % port)
        self.origins = tuple("http://" + h for h in self.hosts)
        self.runner, self.sleep = runner, sleep
        self.clock = time.monotonic  # 検索の覚え書きの期限に使う
        self.lock = threading.Lock()
        self.last_report = None  # type: Optional[UpdateReport]
        self.last_error = None  # type: Optional[str]
        self.progress = None  # type: Optional[Tuple[int, int]]
        self.running = False
        self._updating = threading.Lock()  # 更新を同時に2つ走らせない
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self._cache = OrderedDict()  # type: OrderedDict[Tuple[Any, ...], Tuple[float, SearchResult]]
        self._cache_lock = threading.Lock()
        self._generation = 0  # 更新が終わるたびに増やす（更新の前に始めた検索の結果は覚えない）

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """短い間だけ使う台帳の接続。要求ごとに開く。開くときは書き込まない（store.ensure_schema）ので、
        更新が書き込みのトランザクションを持っていても待たない。"""
        conn = open_ledger(self.ledger_dir / LEDGER_FILENAME)
        try:
            yield conn
        finally:
            conn.close()

    def live(self) -> Dict[str, LiveSession]:
        return live_sessions(self.claude_dir)

    def vscode_terminal(self) -> VSCodeTerminal:
        """VS Code の統合ターミナルへの依頼の書き先（<台帳>/vscode-requests）。"""
        return VSCodeTerminal(requests_dir(self.ledger_dir), self.home, self.cfg.vscode_app)

    # 更新

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tsuzuki-indexer", daemon=True)
        self._thread.start()

    def request_update(self) -> None:
        """すぐに更新させる（更新中なら、その後にもう1回）。"""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        """起動時と、設定 refresh_seconds ごとと、頼まれたとき（/api/reindex）に更新する。"""
        while not self._stop.is_set():
            self.run_update()
            self._wake.wait(_refresh_seconds(self.cfg.refresh_seconds))
            self._wake.clear()

    def run_update(self) -> Optional[UpdateReport]:
        """更新を1回行う（このプロセスでは同時に1つだけ）。失敗は例外にせず、状態（last_error）に残す。"""
        with self._updating:
            with self.lock:
                self.running, self.progress = True, (0, 0)
            report, error = None, None  # type: Optional[UpdateReport], Optional[str]
            try:
                with self.connect() as conn:
                    # タスクの置き場所はいつも渡す（渡さない更新は、場所の表示名「タスク」「共通の資料」を戻してしまう）
                    report = update(conn, self.cfg, claude_dir=self.claude_dir, ledger_dir=self.ledger_dir,
                                    home=self.home, progress=self._progress, tasks_root=self.tasks_root)
            except Exception as e:  # 更新の失敗でサーバーを止めない
                error = UPDATE_FAILED % e
            with self.lock:
                self.running, self.progress, self.last_error = False, None, error
                if report is not None:
                    self.last_report = report
            self.forget_searches()
            return report

    def forget_searches(self) -> None:
        """台帳が変わったので、覚えた検索の結果を捨てる（変わる前に始めた検索の結果も、この後は覚えない）。"""
        with self._cache_lock:
            self._cache.clear()
            self._generation += 1

    def _progress(self, done: int, total: int) -> None:
        with self.lock:
            self.progress = (done, total)

    def status(self) -> Dict[str, Any]:
        """/api/status の中身。知らせ（形式・記録の場所）は台帳の meta から読む（tsuzuki index の分も出る）。"""
        with self.lock:
            running, progress, report, error = self.running, self.progress, self.last_report, self.last_error
        with self.connect() as conn:
            meta = {key: get_meta(conn, key) or None for key in ("last_update_at", "format_warning", "source_warning")}
            counts = views.ledger_counts(conn)
        return {"last_update_at": meta["last_update_at"],
                "indexing": {"done": progress[0], "total": progress[1]} if running and progress else None,
                "format_warning": meta["format_warning"], "source_warning": meta["source_warning"],
                "errors": [error] if error else list(report.errors) if report else [], "counts": counts}

    # 検索

    def find(self, conn: sqlite3.Connection, query: SearchQuery) -> SearchResult:
        """検索する。同じ問い（語と絞り込み）の結果は CACHE_SECONDS の間、CACHE_SIZE 件まで覚えておく
        （左の場所と中央の一覧が同じ検索を2回しないように）。"""
        key = (tuple(_terms(query.text)), None if query.place_ids is None else tuple(query.place_ids), query.days,
               tuple(sorted(query.targets)), query.role, query.order, query.limit)
        now = self.clock()
        with self._cache_lock:
            hit, generation = self._cache.get(key), self._generation
            if hit is not None and now - hit[0] < CACHE_SECONDS:
                self._cache.move_to_end(key)
                return hit[1]
        result = search(conn, query)
        with self._cache_lock:
            if generation == self._generation:  # 検索の間に更新が終わっていれば、古い結果なので覚えない
                self._cache[key] = (now, result)
                self._cache.move_to_end(key)
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
        return result


class _Server(ThreadingHTTPServer):
    daemon_threads = True  # 応答中のスレッドが残っていても、Ctrl+C で終われるように
    app = None  # type: Optional[App]

    def server_bind(self) -> None:
        # HTTPServer.server_bind は名前を引く（socket.getfqdn）ので遅くなりうる。TCPServer の分だけ行う
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]

    def handle_error(self, request: Any, client_address: Any) -> None:
        """接続が切れたときなど。標準では stderr に書くが、何も書かない。"""


class _Params:
    """問い合わせ（?a=b&…）の値。空の値は指定なしとみなし、形の違う値は 400 にする。"""

    def __init__(self, query: str):
        try:
            self._values = parse_qs(query, keep_blank_values=True, max_num_fields=MAX_FIELDS)
        except ValueError:  # 項目が多すぎる
            self._values = None

    def has(self, name: str) -> bool:
        return name in self._all()

    def text(self, name: str) -> str:
        values = self._all().get(name)
        return values[0] if values else ""

    def number(self, name: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.text(name)
        if not raw:
            return default
        if not _NUMBER.fullmatch(raw):
            raise _HttpError(400, BAD_REQUEST % name)
        return int(raw)

    def numbers(self, name: str) -> List[int]:
        raw = self.text(name)
        if not raw:
            return []
        parts = raw.split(",")
        if not all(_NUMBER.fullmatch(part) for part in parts):
            raise _HttpError(400, BAD_REQUEST % name)
        return [int(part) for part in parts]

    def days(self, default: Optional[int]) -> Optional[int]:
        return None if self.text("days") == "all" else self.number("days", default)

    def flag(self, name: str, default: bool) -> bool:
        raw = self.text(name)
        if raw not in ("", "0", "1"):
            raise _HttpError(400, BAD_REQUEST % name)
        return default if not raw else raw == "1"

    def choice(self, name: str, allowed: Sequence[str]) -> Optional[str]:
        raw = self.text(name)
        if raw and raw not in allowed:
            raise _HttpError(400, BAD_REQUEST % name)
        return raw or None

    def session(self, name: str) -> Optional[str]:
        raw = self.text(name)
        if raw and not is_session_id(raw):
            raise _HttpError(400, BAD_REQUEST % name)
        return raw or None

    def search_query(self, place_ids: Optional[List[int]]) -> SearchQuery:
        raw = self.text("targets")
        targets = frozenset(raw.split(",")) if raw else DEFAULT_TARGETS  # type: FrozenSet[str]
        if not targets <= TARGETS:
            raise _HttpError(400, BAD_REQUEST % "targets")
        return SearchQuery(self.text("q"), place_ids=place_ids, days=self.days(None), targets=targets,
                           role=self.choice("role", ("user", "assistant")),
                           order=self.choice("order", ORDERS) or "recent", limit=RESULT_LIMIT)

    def _all(self) -> Dict[str, List[str]]:
        if self._values is None:
            raise _HttpError(400, BAD_REQUEST % "項目が多すぎます")
        return self._values


class _Handler(BaseHTTPRequestHandler):
    server_version = "tsuzuki"
    sys_version = ""
    timeout = REQUEST_TIMEOUT

    def log_message(self, format: str, *args: Any) -> None:
        """要求の記録は残さない（URL に検索の語が入るため）。"""

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """http.server が自分で返す誤りにも、同じ見出しを付ける（要求の中身は返さない）。"""
        self.close_connection = True
        self._reply(code, _HTTP_ERRORS.get(code, "要求を受け付けられませんでした").encode("utf-8"), TEXT)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        # 本文は先に読む（読まないまま断って閉じると、応答が相手に届かないことがある）。断る順番は変えない
        body, body_error = self._read_body() if method == "POST" else (b"", None)
        try:
            self._route(method, body, body_error)
        except _HttpError as e:
            self._reply(e.code, e.message.encode("utf-8"), TEXT)
        except (ConnectionError, socket.timeout):
            raise  # 相手が切れた。もう応答できない
        except Exception as e:  # 台帳が使用中なら 503、ほかは 500。どちらも中身は応答にも記録にも出さない
            code = 503 if _ledger_busy(e) else 500
            _log_failure(code, e)
            self._reply(code, (LEDGER_BUSY if code == 503 else INTERNAL_ERROR).encode("utf-8"), TEXT)

    def _route(self, method: str, body: bytes, body_error: Optional[int]) -> None:
        """確かめる順番：Host → 鍵（クッキー、または GET / の ?key=）→ POST なら Origin と CSRF → 本文。"""
        app = self.server.app
        if not self._single("Host", app.hosts):  # DNS rebinding を防ぐ
            raise _HttpError(403, BAD_HOST)
        path, _, query = self.path.partition("?")
        params = _Params(query)
        if method == "GET" and path == "/" and params.has("key"):
            if not _same(params.text("key"), app.token):
                raise _HttpError(403, NOT_AUTHORIZED)
            cookie = "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (COOKIE, app.token)
            return self._reply(302, b"", TEXT, extra=(("Location", "/"), ("Set-Cookie", cookie)))
        if not self._has_key_cookie(app.token):
            raise _HttpError(403, NOT_AUTHORIZED)
        if method == "GET":
            return self._get(app, path, params)
        csrf = self.headers.get_all(CSRF_HEADER) or []
        if not self._single("Origin", app.origins) or len(csrf) != 1 or not _same(csrf[0], app.csrf):
            raise _HttpError(403, BAD_ORIGIN)
        action = _ACTIONS.get(path)
        if action is None:
            raise _HttpError(404, NOT_FOUND)
        types = self.headers.get_all("Content-Type") or []
        if len(types) != 1 or types[0].split(";")[0].strip().lower() != "application/json":
            raise _HttpError(400, BAD_REQUEST % "Content-Type")
        if body_error is not None:
            raise _HttpError(body_error, TOO_LARGE if body_error == 413 else BAD_REQUEST % "本文")
        self._reply(200, _json(action(app, _json_object(body))), JSON)

    def _get(self, app: App, path: str, params: _Params) -> None:
        if path == "/":
            return self._reply(200, _page(app), HTML)
        if path in _STATIC:
            name, ctype = _STATIC[path]
            return self._reply(200, _asset(name), ctype)
        if path == "/api/status":
            return self._reply(200, _json(app.status()), JSON)
        if path.startswith("/doc-raw/"):
            return self._reply(200, _doc_raw(app, path[len("/doc-raw/"):]), HTML, csp=DOC_CSP)
        if path == "/mermaid-frame":  # 鍵は確かめ済み。覚えさせるのは今の版の URL だけ
            cache = MERMAID_CACHE if params.text("v") == MERMAID_VERSION else NO_STORE
            return self._reply(200, _mermaid_page(), HTML, csp=MERMAID_CSP, cache=cache)
        fragment, ident = _fragment_route(path)
        with app.connect() as conn:
            page = fragment(app, conn, ident, params)
        self._reply(200, page.encode("utf-8"), HTML)

    def _single(self, name: str, allowed: Sequence[str]) -> bool:
        """その見出しがちょうど1つあり、allowed のどれかと一致するか。"""
        values = self.headers.get_all(name) or []
        return len(values) == 1 and values[0] in allowed

    def _has_key_cookie(self, token: str) -> bool:
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name.strip() == COOKIE and _same(value.strip(), token):
                    return True
        return False

    def _read_body(self) -> Tuple[bytes, Optional[int]]:
        """POST の本文を読み、(本文, 断るときの状態コード) を返す。大きすぎる本文は MAX_DRAIN まで読み捨てる。"""
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1:
            return b"", 400  # 長さの分からない本文は受けない
        if not lengths:
            return b"", None
        if not _NUMBER.fullmatch(lengths[0].strip()):
            return b"", 400
        size = int(lengths[0])
        if size <= MAX_BODY:
            return self.rfile.read(size), None
        left = min(size, MAX_DRAIN)
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)
        return b"", 413

    def _reply(self, code: int, body: bytes, ctype: str, *, csp: str = CSP, cache: str = NO_STORE,
               extra: Sequence[Tuple[str, str]] = ()) -> None:
        self.send_response(code)
        headers = [("Content-Type", ctype), ("Content-Length", str(len(body))), ("Content-Security-Policy", csp),
                   ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                   ("Cache-Control", cache)]
        if csp == CSP:  # ほかのページの枠に入れさせない（/doc-raw/ と /mermaid-frame は自分の画面の枠に入るので付けない）
            headers.append(("X-Frame-Options", "SAMEORIGIN"))
        for name, value in headers + list(extra):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


# GET の中身

def _page(app: App) -> bytes:
    page = _asset("ui/index.html").decode("utf-8")
    with app.connect() as conn:  # 最初に開く表示を決めるのに使う（タスクがあればタスク）
        has_tasks = conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone() is not None
    for mark, value in (("{{CSRF}}", app.csrf), ("{{RESUME_DEFAULT}}", app.cfg.resume_default),
                        ("{{HEADLESS_IN_LIST}}", "1" if app.cfg.headless_in_list else "0"),
                        ("{{HAS_TASKS}}", "1" if has_tasks else "0")):
        page = page.replace(mark, html.escape(str(value), quote=True))
    return page.encode("utf-8")


def _asset(name: str) -> bytes:
    """パッケージの中の画面のファイル（zipapp の中でも読める）。"""
    try:
        data = pkgutil.get_data("tsuzuki", name)
    except OSError:
        data = None
    if data is None:
        raise _HttpError(404, NOT_FOUND)
    return data


@functools.lru_cache(maxsize=1)
def _mermaid_page() -> bytes:
    """Mermaid の図の枠のページ。枠は隔離されて起源が不透明なので、サーバーから何も
    取れない（鍵のクッキーも送られない）。そのため、同梱のライブラリ（約3.5MB）をページの中に入れる。
    組み立てたページは覚えておく。ライブラリの文に <script や </script があれば、<script> の中に入れると
    そこで切れてしまうので、組み立てない（例外にする。要求は 500 になる）。"""
    before, mark, after = _asset(MERMAID_PAGE).decode("utf-8").partition(MERMAID_MARK)
    library = _asset(MERMAID_LIBRARY).decode("utf-8")
    if not mark or MERMAID_MARK in after or _SCRIPT_TAG.search(library):
        raise ValueError("Mermaid の枠のページを組み立てられません")
    return (before + library + after).encode("utf-8")


def _doc_raw(app: App, segment: str) -> bytes:
    """台帳の documents にある HTML の資料だけを、MAX_DOC_BYTES まで読んで返す（要求に書かれたパスは開かない）。"""
    doc_id = _number_segment(segment)
    with app.connect() as conn:
        item = views.doc_items(conn, [doc_id], home=app.home).get(doc_id)
    if (item is None or (item.ext or "").lower() not in _HTML_EXTS
            or os.path.splitext(item.path)[1].lower() not in _HTML_EXTS or not os.path.isfile(item.path)):
        raise _HttpError(404, NOT_FOUND)
    try:
        with open(item.path, "rb") as f:
            return f.read(MAX_DOC_BYTES)
    except OSError:
        raise _HttpError(404, NOT_FOUND)


def _fragment_route(path: str) -> Tuple[Callable[..., str], Any]:
    """断片の経路と、パスの中の ID（なければ None）。経路にないか、ID の形が違えば 404。"""
    found = _FRAGMENTS.get(path)
    if found is not None and found[0] is None:
        return found[1], None
    head, sep, segment = path.rpartition("/")
    found = _FRAGMENTS.get(head + sep)
    if found is None or found[0] is None:
        raise _HttpError(404, NOT_FOUND)
    if found[0] == "session":
        if not is_session_id(segment):
            raise _HttpError(404, NOT_FOUND)
        return found[1], segment
    if found[0] == "task":
        name = unquote(segment)  # 画面は encodeURIComponent したフォルダー名を送る
        if not _task_name_ok(name):
            raise _HttpError(404, NOT_FOUND)
        return found[1], name  # 台帳の tasks にある名前かは、断片（_frag_task）が確かめる
    return found[1], _number_segment(segment)


def _number_segment(segment: str) -> int:
    if not _NUMBER.fullmatch(segment):
        raise _HttpError(404, NOT_FOUND)
    return int(segment)


def _task_name_ok(name: str) -> bool:
    """タスクのフォルダー名になりうる形か（区切りの / と NUL を含まず、shared・. や _ で始まる名前でない）。
    要求から来た名前はパスにはせず、台帳の tasks の行を引くのにだけ使う。"""
    return bool(name) and "/" not in name and "\x00" not in name and is_task_dir_name(name)


def _frag_places(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    """左の場所。検索語があれば、場所で絞らない同じ検索の一致の数を出す（選んだ場所は印を付けるだけ）。"""
    selected, query = p.number("place"), p.search_query(None)
    places = views.list_places(conn)
    if not _terms(query.text):
        return ui_render.frag_places(places, selected=selected)
    counts = dict(app.find(conn, query).places)
    listed = {item.id for item in places}
    extra = sorted(pid for pid in counts if pid not in listed)
    names, labels = views.place_displays(conn, extra), views.place_labels(conn, extra)
    # 入力履歴にしかない場所は list_places に出ないので、名前だけの項目を足す
    places += [views.PlaceItem(id=pid, display=names[pid], n_interactive=0, n_headless=0, n_docs=0, last_at=None,
                               label=labels[pid]) for pid in extra if pid in names]
    return ui_render.frag_places(places, selected=selected, counts=counts)


def _frag_list(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    """一覧。上限（views.LIST_LIMIT）で切ったかもしれないときだけ、条件に合う数を数えて知らせに出す。"""
    place, days = p.number("place"), p.days(LIST_DAYS)
    headless = p.flag("headless", bool(app.cfg.headless_in_list))
    limit = views.LIST_LIMIT
    rows = views.list_sessions(conn, place_id=place, days=days, include_headless=headless, limit=limit,
                               live=app.live())
    total = len(rows)
    if total >= limit:
        total = views.count_sessions(conn, place_id=place, days=days, include_headless=headless)
    return ui_render.frag_session_list(rows, selected=p.session("s"), total=total, place_id=place, days=days,
                                       headless=headless)


def _frag_search(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    place = p.number("place")
    query, selected = p.search_query(None if place is None else [place]), p.session("s")
    terms = _terms(query.text)
    result = app.find(conn, query) if terms else SearchResult(sessions=[], headless=[], documents=[], history=[],
                                                              places=[], elapsed_ms=0.0)
    return ui_render.frag_search(conn, result, terms=terms, selected=selected, live=app.live(), home=app.home,
                                 limit=query.limit, order=query.order)


def _frag_card(app: App, conn: sqlite3.Connection, session_id: str, p: _Params) -> str:
    """状況カード。turns（一致したターン、新しい順）があれば、i 番目の一致の箇所を出す（範囲の外は端に寄せる）。"""
    turns, index, terms = p.numbers("turns"), p.number("i", 0), _terms(p.text("q"))
    detail = views.session_detail(conn, session_id, live=app.live(), home=app.home)
    if detail is None:
        raise _HttpError(404, NOT_FOUND)
    index = min(index, len(turns) - 1) if turns else 0
    excerpt = views.turn_excerpt(conn, session_id, turns[index]) if turns else None
    return ui_render.frag_card(detail, resume_default=app.cfg.resume_default, hit_turns=turns, hit_index=index,
                               excerpt=excerpt, terms=terms,
                               task_choices=taskviews.open_task_choices(conn, session_id),
                               vscode_terminal=app.vscode_terminal().installed())


def _frag_children(app: App, conn: sqlite3.Connection, session_id: str, p: _Params) -> str:
    offset = p.number("offset", 0)
    if not views.session_rows(conn, [session_id], live={}):
        raise _HttpError(404, NOT_FOUND)
    rows = views.children(conn, session_id, offset=offset, limit=PAGE_SIZE + 1)
    return ui_render.frag_children(rows[:PAGE_SIZE], session_id=session_id, offset=offset,
                                   has_more=len(rows) > PAGE_SIZE)


def _frag_orphans(app: App, conn: sqlite3.Connection, place_id: int, p: _Params) -> str:
    offset = p.number("offset", 0)
    if not views.place_displays(conn, [place_id]):
        raise _HttpError(404, NOT_FOUND)
    rows = views.orphan_headless(conn, place_id, offset=offset, limit=PAGE_SIZE + 1)
    return ui_render.frag_orphans(rows[:PAGE_SIZE], place_id=place_id, offset=offset, has_more=len(rows) > PAGE_SIZE)


def _frag_place(app: App, conn: sqlite3.Connection, place_id: int, p: _Params) -> str:
    overview = views.place_overview(conn, place_id, home=app.home)
    if overview is None:
        raise _HttpError(404, NOT_FOUND)
    return ui_render.frag_place(overview)


def _frag_docs_filters(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    kind, place = p.text("kind") or None, p.number("place")
    return ui_render.frag_docs_filters(views.document_kinds(conn, place_id=place), views.list_places(conn),
                                       kind=kind, place_id=place)


def _frag_docs(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    """資料の一覧。検索語があれば、一致した資料だけを一致の順に（種類と場所で絞る）。語がなければ新しい順に
    上限（views.DOCS_LIMIT）まで出し、上限まで並んだときだけ、条件に合う数を数えて知らせに出す。"""
    kind, place, selected, text = p.text("kind") or None, p.number("place"), p.number("s"), p.text("q")
    total, capped = None, None  # type: Optional[int], Optional[int]
    terms, by_id = _terms(text), None  # type: List[str], Optional[Dict[int, DocHit]]
    if not terms:
        limit = views.DOCS_LIMIT
        items = views.documents_list(conn, kind=kind, place_id=place, limit=limit, home=app.home)
        if len(items) >= limit:
            total = views.count_documents(conn, kind=kind, place_id=place)
    else:
        query = SearchQuery(text, place_ids=None if place is None else [place], targets=frozenset({"documents"}),
                            limit=RESULT_LIMIT)
        hits = app.find(conn, query).documents
        found = views.doc_items(conn, [h.doc_id for h in hits], home=app.home)
        items = [found[h.doc_id] for h in hits if h.doc_id in found and kind in (None, found[h.doc_id].kind)]
        capped = query.limit if len(hits) >= query.limit else None
        by_id = {h.doc_id: h for h in hits}
    return ui_render.frag_docs_list(items, selected=selected, total=total, kind=kind, place_id=place, capped_at=capped,
                                    hits=by_id, terms=terms, home=app.home)


def _frag_doc(app: App, conn: sqlite3.Connection, doc_id: int, p: _Params) -> str:
    detail = views.document_detail(conn, doc_id, home=app.home, live=app.live())
    if detail is None:
        raise _HttpError(404, NOT_FOUND)
    return ui_render.frag_doc(detail, terms=_terms(p.text("q")))


def _frag_conversation(app: App, conn: sqlite3.Connection, session_id: str, p: _Params) -> str:
    """会話。turn（既定は最後のターン）の前後の窓を出す。before・after・offset があれば、読み足す分だけを返す。"""
    turn, turns, terms = p.number("turn"), p.numbers("turns"), _terms(p.text("q"))
    before, after, offset = p.number("before"), p.number("after"), p.number("offset")
    if before is not None or after is not None or offset is not None:
        return _conversation_part(conn, session_id, terms, before, after, offset)
    window = views.conversation_window(conn, session_id, around_turn=turn)
    if window is None:
        raise _HttpError(404, NOT_FOUND)
    row = views.session_rows(conn, [session_id], live={}).get(session_id)
    return ui_render.frag_conversation(window, title=row.title if row else "", terms=terms, hit_turns=turns,
                                       task_choices=taskviews.open_task_choices(conn, session_id) if row else ())


def _conversation_part(conn: sqlite3.Connection, session_id: str, terms: List[str], before: Optional[int],
                       after: Optional[int], offset: Optional[int]) -> str:
    """会話に読み足す分。before・after は読み込んである端のターンで、その前か後ろの CONVERSATION_TURNS ターンを、
    offset はヘッドレスの記録の項目の番号で、そこから TRANSCRIPT_ITEMS 項目を返す。会話の種類に合わない指定には
    何も返さない（同じ発言を2回足さないため）。"""
    size = views.CONVERSATION_TURNS
    where = {}  # type: Dict[str, Any]  conversation_window に渡す、読み足す範囲
    if before is not None:
        side, source, where = "before", "ledger", {"turns": (before - size, before - 1)}
    elif after is not None:
        side, source, where = "after", "ledger", {"turns": (after + 1, after + size)}
    else:
        side, source, where = "after", "transcript", {"item_offset": offset or 0}
    window = views.conversation_window(conn, session_id, **where)
    if window is None:
        raise _HttpError(404, NOT_FOUND)
    if window.source != source:
        return ""
    return ui_render.frag_conversation_part(window, terms=terms, side=side)


def _frag_task_filters(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    """左の状況・場所・共通の資料。場所の数は選んだ状況のタスクで数え、選んだ場所は 0件でも出す。"""
    status, place = _task_status(p), p.number("place")
    counts = taskviews.task_counts(conn)[0]
    places = _task_places(taskviews.list_tasks(conn, status=status, live={}))
    if place is not None and all((pid or ui_tasks.NO_PLACE_ID) != place for pid, _, _ in places):
        label = (taskviews.NO_PLACE if place == ui_tasks.NO_PLACE_ID
                 else views.place_labels(conn, [place]).get(place))
        if label is not None:
            places.append((None if place == ui_tasks.NO_PLACE_ID else place, label, 0))
    root, notes = app.tasks_root, []
    if root is not None:
        notes = _tasks_root_notes(app, root)
    return ui_tasks.frag_task_filters(
        counts, places, status=status, place=place, shared_place=_shared_place(app, conn),
        ticket_url=app.cfg.ticket_url, tasks_root=None if root is None else str(root), home=app.home,
        tasks_root_value=app.cfg.tasks_root, tasks_root_notes=notes)


# 左の「タスクの置き場所」の下に出す、Claude Code 側の案内（点検 tsuzuki doctor と同じ確かめ方）
_COPY_FOR_CHECK = {CHECK_ALLOW: lambda root: str(root),
                   CHECK_CLAUDE_MD: lambda root: os.path.join(str(root), "README.md")}


def _tasks_root_notes(app: App, root: Path) -> List[Tuple[str, Optional[str]]]:
    """今の置き場所で、まだ整っていない点検の文と、コピーする文字（許可するパス・README.md のパス）。"""
    notes = []  # type: List[Tuple[str, Optional[str]]]
    for check in task_checks(app.cfg, claude_dir=app.claude_dir, home=app.home):
        if check.status != "ok":
            copy = _COPY_FOR_CHECK.get(check.name)
            notes.append(("%s: %s" % (check.name, check.detail), None if copy is None else copy(root)))
    return notes


def _frag_tasks(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    """タスクの一覧。place は場所の ID で、ui_tasks.NO_PLACE_ID なら場所のないタスク。task は選んでいるタスク。"""
    status, place = _task_status(p), p.number("place")
    live = app.live()
    if place == ui_tasks.NO_PLACE_ID:
        rows = [r for r in taskviews.list_tasks(conn, status=status, live=live) if r.place_id is None]
    else:
        rows = taskviews.list_tasks(conn, status=status, place_id=place, live=live)
    return ui_tasks.frag_task_list(rows, selected=p.text("task") or None, status=status)


def _frag_task(app: App, conn: sqlite3.Connection, name: str, p: _Params) -> str:
    """タスクのカード。台帳の tasks にある名前だけ（ほかは 404）。"""
    cfg = app.cfg
    if _task_folder(conn, name) is None:
        raise _HttpError(404, NOT_FOUND)
    detail = taskviews.task_detail(conn, name, live=app.live(), home=app.home, exclude_dirs=cfg.doc_scan.exclude_dirs,
                                   ticket_template=cfg.ticket_url, default_branches=cfg.default_branches,
                                   max_depth=cfg.doc_scan.max_depth)
    if detail is None:
        raise _HttpError(404, NOT_FOUND)
    return ui_tasks.frag_task_card(detail, resume_default=cfg.resume_default, home=app.home,
                                   vscode_terminal=app.vscode_terminal().installed())


def _frag_new_task(app: App, conn: sqlite3.Connection, _: None, p: _Params) -> str:
    return ui_tasks.frag_new_task(_repo_choices(app, conn))


def _frag_task_settings(app: App, conn: sqlite3.Connection, name: str, p: _Params) -> str:
    """タスクの設定（題・チケット・リポジトリ・ワークスペース）の入力欄。task.md をその場で読んで今の値を出す
    （読めなければ 404）。"""
    folder = _task_folder(conn, name)
    if folder is None:
        raise _HttpError(404, NOT_FOUND)
    md = os.path.join(folder, TASK_MD)
    try:
        st = os.stat(md)
        with open(md, "rb") as f:
            tf = parse_task_md(f.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        raise _HttpError(404, NOT_FOUND)
    return ui_tasks.frag_task_settings(name, tf, task_title(tf, name), repos=_repo_choices(app, conn),
                                       mtime=st.st_mtime, size=st.st_size)


def _task_status(p: _Params) -> str:
    return p.choice("status", TASK_STATUSES) or "open"


def _task_places(rows: Sequence[taskviews.TaskRow]) -> List[Tuple[Optional[int], str, int]]:
    """場所ごとのタスクの数（taskviews.task_counts と同じ並び：場所なしを最後に、数の多い順、名前の順）。"""
    found = {}  # type: Dict[Optional[int], Tuple[str, int]]
    for r in rows:
        found[r.place_id] = (r.place_label, found.get(r.place_id, ("", 0))[1] + 1)
    places = [(pid, label, n) for pid, (label, n) in found.items()]
    places.sort(key=lambda item: (item[0] is None, -item[2], item[1]))
    return places


def _shared_place(app: App, conn: sqlite3.Connection) -> Optional[int]:
    """共通の資料（<タスクの置き場所>/shared）の場所の ID。台帳になければ None。"""
    if app.tasks_root is None:
        return None
    path = os.path.join(os.path.normpath(str(app.tasks_root)), SHARED_DIR)
    row = conn.execute("SELECT id FROM places WHERE path = ?", (path,)).fetchone()
    return row[0] if row else None


def _repo_choices(app: App, conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """新しいタスクのリポジトリの選択肢（パス、表示名）：場所の一覧（views.list_places の順）のうち、実在する
    フォルダーで、タスクの置き場所の外のもの。"""
    paths = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM places")}
    root = None if app.tasks_root is None else os.path.normpath(str(app.tasks_root))
    out = []  # type: List[Tuple[str, str]]
    for item in views.list_places(conn):
        path = paths.get(item.id)
        if path and os.path.isabs(path) and not (root and within(path, root)) and os.path.isdir(path):
            out.append((path, item.display))
    return out


def _task_folder(conn: sqlite3.Connection, name: str) -> Optional[str]:
    """台帳の tasks にあるタスクのフォルダー（台帳の path）。名前の形が違うか、台帳になければ None。"""
    if not _task_name_ok(name):
        return None
    row = conn.execute("SELECT path FROM tasks WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


# 経路 → (パスの中の ID の種類, 断片を作る関数)。ID の種類は None（ID なし）・"session"・"number"・"task"
# （URL 符号化したタスクのフォルダー名）
_FRAGMENTS = {
    "/frag/places": (None, _frag_places), "/frag/list": (None, _frag_list), "/frag/search": (None, _frag_search),
    "/frag/docs-filters": (None, _frag_docs_filters), "/frag/docs": (None, _frag_docs),
    "/frag/card/": ("session", _frag_card), "/frag/children/": ("session", _frag_children),
    "/frag/conversation/": ("session", _frag_conversation), "/frag/orphans/": ("number", _frag_orphans),
    "/frag/place/": ("number", _frag_place), "/frag/doc/": ("number", _frag_doc),
    "/frag/task-filters": (None, _frag_task_filters), "/frag/tasks": (None, _frag_tasks),
    "/frag/task/": ("task", _frag_task), "/frag/new-task": (None, _frag_new_task),
    "/frag/task-settings/": ("task", _frag_task_settings),
}  # type: Dict[str, Tuple[Optional[str], Callable[..., str]]]


# POST の操作（本文は JSON のオブジェクト。答えは {"ok", "needs_confirm", "message"}）

def _api_resume(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    session_id, how, confirm = _text_field(data, "session_id"), _text_field(data, "how"), data.get("confirm", False)
    if not isinstance(confirm, bool):
        raise _HttpError(400, BAD_REQUEST % "confirm")
    with app.connect() as conn:
        return _outcome(resume(conn, session_id, how, cfg=app.cfg, live=app.live(), confirm=confirm,
                               runner=app.runner, sleep=app.sleep, terminal=app.vscode_terminal()))


def _task_workspace(app: App, conn: sqlite3.Connection, task: str) -> Optional[str]:
    """タスクの task.md の workspace（手で ~ と書かれていれば home で展開）。task.md が読めなければ None。"""
    folder = _task_folder(conn, task)
    if folder is None:
        return None
    try:
        with open(os.path.join(folder, TASK_MD), encoding="utf-8") as f:
            workspace = parse_task_md(f.read()).workspace
    except (OSError, ValueError):
        return None
    if workspace and (workspace == "~" or workspace.startswith("~/")):
        workspace = str(app.home).rstrip("/") + workspace[1:]
    return workspace


def _api_open(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    doc_id, how = _id_field(data, "doc_id"), _text_field(data, "how")
    with app.connect() as conn:
        return _outcome(open_document(conn, doc_id, how, cfg=app.cfg, runner=app.runner))


def _api_open_place(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    place_id, how = _id_field(data, "place_id"), _text_field(data, "how")
    with app.connect() as conn:
        return _outcome(open_place(conn, place_id, how, cfg=app.cfg, runner=app.runner))


def _api_reindex(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    app.request_update()
    return {"ok": True}


# タスクの操作。書く操作はどれも更新と同じ鍵の中で行い、書いたら
# そのタスクだけを台帳に取り込み直して結び付けを計算し直す。書けなかったとき（TaskWriteError のほか、
# 権限やディスクの OSError、UTF-8 にできない文字）は 500 にせず、{"ok": false, ...} の文で返す

def _api_task_new(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    title, short_name = _task_text(data, "title"), _task_text(data, "short_name")
    ticket, repo = _task_text(data, "ticket", ""), _task_text(data, "repo", "")
    root = app.tasks_root
    if root is None:
        return _task_failed(NO_TASKS_ROOT)
    if not title.strip():
        return _task_failed(NO_TITLE)
    with update_lock(app.ledger_dir), app.connect() as conn:
        if repo and repo not in {path for path, _ in _repo_choices(app, conn)}:
            return _task_failed(NOT_A_PLACE)
        try:
            task_md = create_task(root, short_name, title=title, ticket=ticket or None, repo=repo or None,
                                 claude_dir=app.claude_dir)
        except TaskWriteError as e:
            return _task_failed(str(e))
        except (OSError, ValueError) as e:  # ValueError は UnicodeEncodeError を含む
            return _task_failed(CANNOT_CREATE % e)
        name = task_md.parent.name
        _reread_task(app, conn, name)
    app.forget_searches()
    return {"ok": True, "message": TASK_MADE, "task": name, "task_md": str(task_md)}


def _api_task_stage(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    task, stage = _task_text(data, "task"), _task_text(data, "stage")
    mtime, size = _task_stamp(data)
    return _set_task_fields(app, task, {"stage": stage}, mtime, size, STAGE_SET % stage_label(stage))


def _api_task_waiting(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    task, waiting = _task_text(data, "task"), _task_flag(data, "waiting")
    reason = _task_text(data, "reason") if "reason" in data else None
    mtime, size = _task_stamp(data)
    if reason is None:
        return _set_task_fields(app, task, {"waiting": "true" if waiting else "false"}, mtime, size,
                                WAITING_SET[waiting])
    return _write_task_md(app, task, lambda md, root: set_waiting(
        md, root, waiting=waiting, reason=reason, expect_mtime=mtime, expect_size=size, claude_dir=app.claude_dir),
        WAITING_SET[waiting])


def _api_task_settings(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    """題・チケット・リポジトリ・ワークスペースを task.md に書く。リポジトリは、空か、今の task.md の値か、
    場所の一覧のものだけ。"""
    task, title = _task_text(data, "task"), _task_text(data, "title")
    ticket, repo, workspace = _task_text(data, "ticket"), _task_text(data, "repo"), _task_text(data, "workspace")
    mtime, size = _task_stamp(data)
    changed = []  # type: List[bool]

    def write(md: Path, root: Path) -> None:
        wanted = " ".join(repo.split())
        if wanted:
            try:
                with open(str(md), "rb") as f:
                    current = parse_task_md(f.read().decode("utf-8")).repo
            except (OSError, UnicodeDecodeError):
                current = None  # 読めないときは update_settings が断る
            with app.connect() as conn:
                if wanted != current and wanted not in {path for path, _ in _repo_choices(app, conn)}:
                    raise TaskWriteError(NOT_A_PLACE)
        changed.append(update_settings(md, root, title=title, ticket=ticket, repo=repo, workspace=workspace,
                                       home=str(app.home), expect_mtime=mtime, expect_size=size,
                                       claude_dir=app.claude_dir))
    answer = _write_task_md(app, task, write, SETTINGS_SET)
    if answer["ok"] and not any(changed):
        answer["message"] = NO_SETTINGS_CHANGE
    return answer


def _api_ticket_url(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    """チケットのリンク先（設定 ticket_url）を config.json に書き、動いている設定（app.cfg）も変える。
    空ならリポジトリの Issue に戻す。"""
    text = _task_text(data, "url")
    with app.lock:  # 続けて押されても、読んで書く間に別の保存が入らないようにする
        try:
            value = normalize_ticket_url(text)
            write_ticket_url(app.ledger_dir, value)
        except ConfigError as e:
            return _task_failed(str(e))
        app.cfg.ticket_url = value
    return _task_done(TICKET_URL_SET if value is not None else TICKET_URL_RESET)


def _api_tasks_root(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    """タスクの置き場所（設定 tasks_root）を切り替える。フォルダーは動かさない（利用者が先に動かす）。
    今のタスクが新しい置き場所になければ、一覧から消えることを確かめてから（confirm）切り替える。"""
    text, confirm = _task_text(data, "path"), data.get("confirm", False)
    if not isinstance(confirm, bool):
        raise _HttpError(400, BAD_REQUEST % "confirm")
    try:
        text = normalize_tasks_root_input(text, home=app.home, claude_dir=app.claude_dir)
        new = resolve_tasks_root(Config(tasks_root=text), home=app.home, claude_dir=app.claude_dir)
    except ConfigError as e:
        return _task_failed(str(e))
    with update_lock(app.ledger_dir), app.lock:  # 更新が、切り替えの途中の置き場所を読まないように
        if app.tasks_root is not None and os.path.normpath(str(app.tasks_root)) == os.path.normpath(str(new)):
            return _task_done(NO_SETTINGS_CHANGE)
        with app.connect() as conn:
            names = [row[0] for row in conn.execute("SELECT name FROM tasks")]
            missing = sum(1 for name in names if not os.path.isdir(os.path.join(str(new), name)))
            if missing and not confirm:
                return {"ok": False, "needs_confirm": True, "message": TASKS_ROOT_MISSING % missing}
            try:
                write_tasks_root(app.ledger_dir, text)
            except ConfigError as e:
                return _task_failed(str(e))
            if app.tasks_root is not None:
                reset_task_index(conn, str(app.tasks_root))  # 動かしたフォルダーも、新しい path で読み直させる
                with conn:
                    set_meta(conn, ROOT_KEY, os.path.normpath(str(new)))
        app.cfg.tasks_root = text
        app.tasks_root = new
    app.request_update()
    app.forget_searches()
    return _task_done(TASKS_ROOT_SET)


def _api_task_link(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    """利用者がセッションをタスクに足す（add）・タスクから外す（remove）。記録は作り直しでも消えない（tasklinks）。"""
    task, session_id, action = _task_text(data, "task"), _task_text(data, "session_id"), _task_text(data, "action")
    root = app.tasks_root
    if root is None:
        return _task_failed(NO_TASKS_ROOT)
    if action not in LINK_SET:
        return _task_failed(BAD_LINK_ACTION)
    with update_lock(app.ledger_dir), app.connect() as conn:
        if _task_folder(conn, task) is None:
            return _task_failed(UNKNOWN_TASK)
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            return _task_failed(UNKNOWN_SESSION)
        set_override(conn, task, session_id, action, now_iso=iso_utc(time.time()))
        link_tasks(conn, tasks_root=str(root), default_branches=app.cfg.default_branches)
    return _task_done(LINK_SET[action])


def _api_task_trash(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    """タスクのフォルダーをゴミ箱へ動かし、台帳から外す。動かせなかったときは台帳をそのままにする。"""
    task = _task_text(data, "task")
    root = app.tasks_root
    if root is None:
        return _task_failed(NO_TASKS_ROOT)
    with update_lock(app.ledger_dir), app.connect() as conn:
        folder = _task_folder(conn, task)
        if folder is None:
            return _task_failed(UNKNOWN_TASK)
        try:
            trash_task(folder, root, home=app.home, claude_dir=app.claude_dir)
        except TaskWriteError as e:
            return _task_failed(str(e))
        forget_task(conn, task, folder)
    app.forget_searches()
    return _task_done(TRASHED % root)


def _api_task_copy_doc(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    """台帳にある資料を、タスクのフォルダーへコピーする（元は動かさない）。コピーしたら、タスクを読み直す。"""
    task, doc_id = _task_text(data, "task"), _id_field(data, "doc_id")
    root = app.tasks_root
    if root is None:
        return _task_failed(NO_TASKS_ROOT)
    with update_lock(app.ledger_dir), app.connect() as conn:
        folder = _task_folder(conn, task)
        if folder is None:
            return _task_failed(UNKNOWN_TASK)
        found = conn.execute("SELECT path FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if found is None:
            return _task_failed(UNKNOWN_DOC)
        try:
            copied = copy_into_task(folder, root, found[0], claude_dir=app.claude_dir)
        except TaskWriteError as e:
            return _task_failed(str(e))
        _reread_task(app, conn, task)
    app.forget_searches()
    return _task_done(COPIED % os.path.basename(copied))


def _api_task_conversation(app: App, data: Dict[str, Any]) -> Dict[str, Any]:
    """タスクから新しい会話を始める。copy なら何も起動せず、貼り付けるコマンドを command で返す。"""
    task, how = _task_text(data, "task"), _task_text(data, "how")
    with app.connect() as conn:
        out = start_conversation(conn, task, how, cfg=app.cfg, runner=app.runner, sleep=app.sleep,
                                 terminal=app.vscode_terminal(), workspace=_task_workspace(app, conn, task))
    answer = _outcome(out)
    answer["command"] = out.command
    return answer


def _set_task_fields(app: App, task: str, changes: Dict[str, str], mtime: float, size: int,
                     done: str) -> Dict[str, Any]:
    """task.md の stage か waiting の行を書き換える（書き方は _write_task_md）。"""
    return _write_task_md(app, task, lambda md, root: set_fields(
        md, root, changes, expect_mtime=mtime, expect_size=size, claude_dir=app.claude_dir), done)


def _write_task_md(app: App, task: str, write: Callable[[Path, Path], None], done: str) -> Dict[str, Any]:
    """台帳の行のフォルダーの task.md に write(task.md, 置き場所) で書く。write はカードを出したときの更新時刻と
    大きさ（mtime・size）と違えば書かない（taskwrite.CHANGED）。書けなかったときもタスクを読み直す（画面が
    読み直せば、今の時刻で送れる）。"""
    root = app.tasks_root
    if root is None:
        return _task_failed(NO_TASKS_ROOT)
    with update_lock(app.ledger_dir), app.connect() as conn:
        folder = _task_folder(conn, task)
        if folder is None:
            return _task_failed(UNKNOWN_TASK)
        try:  # 書く先は台帳の行のフォルダーの task.md。write が置き場所の直下にあることも確かめる
            write(Path(folder) / TASK_MD, root)
            answer = _task_done(done)
        except TaskWriteError as e:
            answer = _task_failed(str(e))
        except (OSError, ValueError) as e:  # ValueError は UnicodeEncodeError を含む
            answer = _task_failed(CANNOT_WRITE % e)
        _reread_task(app, conn, task)
    app.forget_searches()
    return answer


def _reread_task(app: App, conn: sqlite3.Connection, name: str) -> None:
    """書いた（書こうとした）タスクを台帳に取り込み直し、セッションとの結び付けを計算し直す。"""
    root = str(app.tasks_root)
    refresh_task(conn, app.cfg, tasks_root=root, home=app.home, name=name)
    link_tasks(conn, tasks_root=root, default_branches=app.cfg.default_branches)


def _task_done(message: str) -> Dict[str, Any]:
    return {"ok": True, "needs_confirm": False, "message": message}


def _task_failed(message: str) -> Dict[str, Any]:
    # 理由の文にファイル名などから UTF-8 にできない文字が入っても、答えを返せるようにする
    return {"ok": False, "needs_confirm": False, "message": message.encode("utf-8", "replace").decode("utf-8")}


def _task_text(data: Dict[str, Any], name: str, default: Optional[str] = None) -> str:
    """文字の項目。default があれば省いてよい。型が違うか、UTF-8 にできない文字（対のないサロゲート）を含めば 400。"""
    value = data.get(name, default)
    if not isinstance(value, str):
        raise _HttpError(400, BAD_REQUEST % name)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _HttpError(400, BAD_REQUEST % name)
    return value


def _task_flag(data: Dict[str, Any], name: str) -> bool:
    """真偽の項目。true・false のほか、画面の data-* の値の "true"・"false" も受ける。"""
    value = data.get(name)
    if isinstance(value, bool):
        return value
    if value in ("true", "false"):
        return value == "true"
    raise _HttpError(400, BAD_REQUEST % name)


def _task_stamp(data: Dict[str, Any]) -> Tuple[float, int]:
    """カードが持っていた task.md の更新時刻（数）と大きさ（0以上の整数）。どちらも欠かせない。"""
    mtime, size = data.get("mtime"), data.get("size")
    if isinstance(mtime, bool) or not isinstance(mtime, (int, float)) or not _finite(mtime):
        raise _HttpError(400, BAD_REQUEST % "mtime")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size < 2 ** 63:
        raise _HttpError(400, BAD_REQUEST % "size")
    return mtime, size


def _finite(value: float) -> bool:
    """NaN・無限大でない数か（JSON の大きすぎる整数は float にできないので、それも断る）。"""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


_ACTIONS = {"/api/resume": _api_resume, "/api/open": _api_open, "/api/open-place": _api_open_place,
            "/api/reindex": _api_reindex, "/api/task-new": _api_task_new, "/api/task-stage": _api_task_stage,
            "/api/task-waiting": _api_task_waiting, "/api/task-settings": _api_task_settings,
            "/api/task-link": _api_task_link, "/api/ticket-url": _api_ticket_url,
            "/api/tasks-root": _api_tasks_root,
            "/api/task-conversation": _api_task_conversation, "/api/task-trash": _api_task_trash,
            "/api/task-copy-doc": _api_task_copy_doc,
            }  # type: Dict[str, Callable[[App, Dict[str, Any]], Dict[str, Any]]]


def _json_object(body: bytes) -> Dict[str, Any]:
    """POST の本文。空なら {}（/api/reindex は本文がなくてよい。ほかの操作は項目がないので 400 になる）。"""
    if not body.strip():
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise _HttpError(400, BAD_REQUEST % "本文")
    if not isinstance(data, dict):
        raise _HttpError(400, BAD_REQUEST % "本文")
    return data


def _text_field(data: Dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise _HttpError(400, BAD_REQUEST % name)
    return value


def _id_field(data: Dict[str, Any], name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2 ** 63:
        raise _HttpError(400, BAD_REQUEST % name)
    return value


def _outcome(out: ActionOutcome) -> Dict[str, Any]:
    return {"ok": out.ok, "needs_confirm": out.needs_confirm, "message": out.message}


# 部品

def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _ledger_busy(error: BaseException) -> bool:
    """台帳が使用中（ほかの接続が書き込み中）のための失敗か。SQL の誤り（no such table など）は含めない。"""
    text = str(error).lower()
    return isinstance(error, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


def _log_failure(code: int, error: BaseException) -> None:
    """500・503 にした失敗を stderr に1行で書く。例外の種類と呼び出しの位置（ファイル:行:関数）だけで、
    例外の文と要求の中身（URL の検索の語など）は書かない。"""
    kind = type(error).__qualname__
    if type(error).__module__ != "builtins":
        kind = "%s.%s" % (type(error).__module__, kind)
    frames = " > ".join("%s:%d:%s" % (os.path.basename(frame.f_code.co_filename), line, frame.f_code.co_name)
                        for frame, line in traceback.walk_tb(error.__traceback__))
    print(FAILURE_LOG % (code, kind, frames), file=sys.stderr, flush=True)


def _same(given: str, secret: str) -> bool:
    """鍵の比較。かかる時間から中身を推し量られないよう hmac.compare_digest で（日本語などは bytes にして比べる）。"""
    return hmac.compare_digest(given.encode("utf-8", "replace"), secret.encode("utf-8"))


def _terms(text: str) -> List[str]:
    """検索の語（search と同じ上限）。印を付けるのと、検索の覚え書きの鍵に使う。"""
    return [term[:MAX_TERM_CHARS] for term in parse_query(text)][:MAX_TERMS]


def _refresh_seconds(value: object) -> float:
    """更新の間隔。数でない設定値は既定に、範囲の外は端に寄せる（0 で回り続けないように）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return DEFAULT_REFRESH
    return float(min(max(value, REFRESH_RANGE[0]), REFRESH_RANGE[1]))


def _load_token(ledger_dir: Path) -> str:
    """鍵（<ledger_dir>/token、権限 0600）。初回に secrets.token_hex(32) で作り、以後は同じものを使う。
    空や壊れた鍵のファイルは使わずに作り直す（空の鍵で通してしまわないため）。"""
    path = ledger_dir / TOKEN_FILE
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        token = ""
    if _TOKEN.fullmatch(token):
        os.chmod(str(path), 0o600)
        return token
    token = secrets.token_hex(32)
    tmp = ledger_dir / (TOKEN_FILE + ".tmp")
    try:
        os.unlink(str(tmp))  # 前の起動の書きかけ（権限が違うかもしれないので使わない）
    except FileNotFoundError:
        pass
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # 書く前から 0600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(str(tmp), str(path))
    return token


def _listening(port: int) -> bool:
    """ほかのプログラムがこのポートで待ち受けているか（127.0.0.1 と ::1 につないでみる）。
    bind だけでは分からない。SO_REUSEADDR（すぐに起動し直せるよう残す）があると、*:port や [::]:port で
    待ち受けている物があっても 127.0.0.1:port に bind できてしまい、::1 だけの物とは localhost を取り合う。"""
    for host in (HOST, "::1"):
        try:
            with socket.create_connection((host, port), timeout=PROBE_SECONDS):
                return True
        except OSError:  # 待ち受けていない（断られた・時間切れ）か、IPv6 が使えない
            continue
    return False


def make_server(cfg: Config, *, ledger_dir: Path, claude_dir: Path, home: str, port: int,
                runner: Callable[..., Any] = subprocess.run, sleep: Callable[[float], None] = time.sleep,
                start_indexer: bool = True, tasks_root: Optional[Path] = None) -> Tuple[ThreadingHTTPServer, App]:
    """127.0.0.1 の port で待ち受けるサーバー（port 0 なら空いている番号。app.port が実際の番号）。
    ポートが使われていれば（ほかのプログラムが 127.0.0.1 か ::1 につながる所で待ち受けていても）OSError
    （errno は EADDRINUSE）を上げる。start_indexer なら更新のスレッドを始める。tasks_root はタスクの置き場所
    （config.resolve_tasks_root で解いたもの。更新と、タスクの操作に使う）。"""
    if port and _listening(port):
        raise OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE))
    httpd = _Server((HOST, port), _Handler)
    try:
        ledger_dir = Path(ledger_dir)
        ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        open_ledger(ledger_dir / LEDGER_FILENAME).close()  # スキーマはここで作る（要求ごとの接続は書かない）
        httpd.app = App(cfg, ledger_dir=ledger_dir, claude_dir=Path(claude_dir), home=home,
                        port=httpd.server_address[1], token=_load_token(ledger_dir), runner=runner, sleep=sleep,
                        tasks_root=tasks_root)
    except BaseException:
        httpd.server_close()
        raise
    if start_indexer:
        httpd.app.start()
    return httpd, httpd.app


def _stop_on_eof(httpd: ThreadingHTTPServer, stdin: BinaryIO) -> None:
    """stdin が閉じるまで読み捨て、閉じたら httpd を止めるスレッドを立てる。"""
    def watch() -> None:
        try:
            while stdin.read(4096):
                pass
        except (OSError, ValueError):
            pass
        httpd.shutdown()
    threading.Thread(target=watch, name="stdin-eof", daemon=True).start()


def serve(cfg: Config, *, ledger_dir: Path, claude_dir: Path, home: str, port: int, open_browser: bool,
          tasks_root: Optional[Path] = None, exit_on_stdin_eof: bool = False,
          stdin: Optional[BinaryIO] = None) -> int:
    """サーバーを起動し、鍵の付いた URL を出して、Ctrl+C まで動く。ポートが使われていれば 2 を返す
    （ブックマークした URL が変わらないよう、別の番号は選ばない）。port 0 なら空いている番号を使う。
    exit_on_stdin_eof なら、標準入力が閉じたら（アプリが終わった・落ちた）止まる。"""
    try:
        httpd, app = make_server(cfg, ledger_dir=ledger_dir, claude_dir=claude_dir, home=home, port=port,
                                 tasks_root=tasks_root)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(PORT_IN_USE % port, file=sys.stderr)
            return 2
        print(START_FAILED % e, file=sys.stderr)
        return 1
    url = "http://%s:%d/?key=%s" % (HOST, app.port, app.token)
    print("tsuzuki: " + url, flush=True)
    try:
        if exit_on_stdin_eof:
            _stop_on_eof(httpd, stdin if stdin is not None else sys.stdin.buffer)
        if open_browser:
            webbrowser.open(url)
        httpd.serve_forever()
    except KeyboardInterrupt:  # Ctrl+C で終える
        pass
    finally:
        app.stop(timeout=2.0)
        httpd.server_close()
    return 0
