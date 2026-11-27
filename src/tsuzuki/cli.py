"""コマンドライン入口。サブコマンドは後のタスクがここに足す。"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from tsuzuki import __version__
from tsuzuki.config import (ConfigError, ledger_dir_from, load_config, read_config, resolve_claude_dir, resolve_port,
                           resolve_tasks_root)

# フックは全セッションの開始で走るので、重い module は各サブコマンドの中で読み込む
if TYPE_CHECKING:
    import sqlite3

    from tsuzuki.search import SearchQuery, SearchResult

MIN_ID_PREFIX = 6
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
FAILED_PATHS_SHOWN = 5  # tsuzuki index が1行ずつ出す、読めなかった記録のパスの数
_INDENT = "  "  # tsuzuki search の抜粋の字下げ
UPDATE_WAIT = "台帳の更新が終わるのを待っています（tsuzuki serve か tsuzuki index が更新しています）"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsuzuki", description="Claude Code の記録を引く道具")
    parser.add_argument("--claude-dir", default=None, help="Claude Code の設定ディレクトリ")
    parser.add_argument("--ledger-dir", default=None, help="台帳のディレクトリ（既定 ~/.tsuzuki）")
    parser.add_argument("--version", action="store_true", help="バージョンを表示して終わる")
    sub = parser.add_subparsers(dest="cmd")
    index = sub.add_parser("index", help="台帳を更新する")
    index.add_argument("--full", action="store_true",
                       help="台帳を作り直す（記録がもう消えたセッションは台帳からも消える）")
    index.set_defaults(func=cmd_index)
    purge = sub.add_parser("purge", help="指定した日より前に終わったセッションを台帳から消す")
    purge.add_argument("--before", required=True, metavar="YYYY-MM-DD", help="この日の0時（地方時）より前")
    purge.set_defaults(func=cmd_purge)
    fg = sub.add_parser("forget", help="セッションを台帳から消し、以後取り込まない")
    fg.add_argument("--undo", action="store_true", help="消した指定を取り消す")
    fg.add_argument("id", help="セッション ID（先頭6文字以上）")
    fg.set_defaults(func=cmd_forget)
    rs = sub.add_parser("resume", help="セッションを再開するコマンドを出す")
    rs.add_argument("--terminal", action="store_true", help="Terminal で開く")
    rs.add_argument("id", help="セッション ID（先頭6文字以上）")
    rs.set_defaults(func=cmd_resume)
    sr = sub.add_parser("search", help="会話・資料・入力履歴を検索する", allow_abbrev=False,
                        description="オプションは語の前に置く（語の後に書いたものは語として探す）")
    sr.add_argument("--days", type=_positive_days, default=None, metavar="日数",
                    help="この日数のうちの一致だけを出す（既定はすべての期間）")
    sr.add_argument("--role", choices=["user", "assistant"], default=None,
                    help="会話を発言者で絞る（user は本人、assistant は Claude。資料と入力履歴には効かない）")
    sr.add_argument("--order", choices=["recent", "count"], default="recent",
                    help="会話の並び順（recent は一致の新しい順〔既定〕、count は一致したターンの多い順）")
    # 「-」で始まる語も語として受け取る（先頭の語が知らないオプションとみなされる分は main で戻す）
    sr.add_argument("words", nargs=argparse.REMAINDER, metavar="語",
                    help='探す語（すべてを含むものを探す）。空白を含む語は、その並びのまま1語として探す')
    sr.set_defaults(func=cmd_search)
    sv = sub.add_parser("serve", help="画面のサーバーを起動する（Ctrl+C で終わる）")
    sv.add_argument("--port", type=int, default=None, help="待ち受けるポート（既定 8765。0 なら空いている番号）")
    sv.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    sv.add_argument("--exit-on-stdin-eof", action="store_true",
                    help="標準入力が閉じたら終わる（アプリから起動するため）")
    sv.set_defaults(func=cmd_serve)
    hook = sub.add_parser("hook", help="Claude Code のフックから呼ばれる（親を記録する）")
    hook.add_argument("event", choices=["session-start"], help="フックの種類")
    hook.set_defaults(func=cmd_hook)
    dr = sub.add_parser("doctor", help="環境を点検する")
    dr.add_argument("--report", metavar="FILE", default=None, help="会話の中身を含まない診断ファイルを書く")
    dr.set_defaults(func=cmd_doctor)
    tk = sub.add_parser("task", help="タスクを扱う")
    tsub = tk.add_subparsers(dest="task_cmd")
    tn = tsub.add_parser("new", help="新しいタスクのフォルダーと task.md を作る")
    tn.add_argument("name", help="フォルダー名の短い名前（YYYY-MM-DD-<短い名前> になる）")
    tn.add_argument("--title", default=None, help="タイトル（既定は短い名前）")
    tn.add_argument("--ticket", default=None, help="チケット（123 は #123 になる。URL も可）")
    tn.add_argument("--repo", default=None, help="リポジトリのパス")
    tn.set_defaults(func=cmd_task_new)
    tl = tsub.add_parser("list", help="タスクを状況・段階・題・フォルダーのパスで出す")
    tl.add_argument("--all", action="store_true", help="完了も出す")
    tl.set_defaults(func=cmd_task_list)
    tk.set_defaults(func=cmd_task, task_parser=tk)
    vs = sub.add_parser("vscode", help="VS Code のターミナルで再開するための拡張を扱う")
    vsub = vs.add_subparsers(dest="vscode_cmd")
    vi = vsub.add_parser("install", help="拡張 Tsuzuki Terminal を VS Code に入れる（入れ直しも同じ）")
    vi.add_argument("--vsix", metavar="FILE", default=None,
                    help="入れずに、VSIX のファイルを書き出すだけにする（手で入れるとき）")
    vi.set_defaults(func=cmd_vscode_install)
    vs.set_defaults(func=cmd_task, task_parser=vs)
    tp = sub.add_parser("templates", help="導入物の雛形（タスクの決まり・CLAUDE.md に足す数行・スキル2つ）を書き出す",
                        description="書き出すだけで、決まった場所には置かない。置く場所は書き出した後に出す")
    tp.add_argument("out", metavar="出力先", help="書き出すフォルダー（同じ名前のファイルがあれば何も書かない）")
    tp.set_defaults(func=cmd_templates)
    return parser


def _positive_days(raw: str) -> int:
    """tsuzuki search --days の値（1以上の整数）。"""
    if not re.fullmatch(r"[0-9]{1,6}", raw) or int(raw) < 1:
        raise argparse.ArgumentTypeError("日数は1以上の整数で指定してください: %s" % raw)
    return int(raw)


def _open(args: argparse.Namespace):
    from tsuzuki.store import LEDGER_FILENAME, open_ledger
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    return open_ledger(ledger / LEDGER_FILENAME)


def cmd_task(args: argparse.Namespace) -> int:
    args.task_parser.print_help()
    return 0


def cmd_task_new(args: argparse.Namespace) -> int:
    from tsuzuki.config import resolve_tasks_root
    from tsuzuki.tasks import at_reference
    from tsuzuki.taskwrite import TaskWriteError, create_task
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    cfg = load_config(ledger)
    claude_dir = resolve_claude_dir(args.claude_dir, os.environ, cfg)
    repo = args.repo
    if repo and repo != "~" and not repo.startswith("~/") and not os.path.isabs(repo):
        repo = os.path.abspath(repo)  # 相対パスは今のフォルダーから（task.md の repo は ~ か絶対パスで読む）
    try:
        root = resolve_tasks_root(cfg, home=os.path.expanduser("~"), claude_dir=claude_dir)
        path = create_task(root, args.name, title=args.title, ticket=args.ticket, repo=repo,
                           claude_dir=claude_dir)
    except (TaskWriteError, ConfigError) as e:
        print("tsuzuki: %s" % e, file=sys.stderr)
        return 1
    print("作りました: %s" % path)
    print(at_reference(str(path)))  # 画面のコピーと同じ @ の形（空白を含むパスは @"…"）
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    from tsuzuki.config import resolve_tasks_root
    from tsuzuki.taskviews import task_lines
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    cfg = load_config(ledger)
    claude_dir = resolve_claude_dir(args.claude_dir, os.environ, cfg)
    try:
        root = resolve_tasks_root(cfg, home=os.path.expanduser("~"), claude_dir=claude_dir)
    except ConfigError as e:
        print("tsuzuki: %s" % e, file=sys.stderr)
        return 1
    for line in task_lines(str(root), include_done=args.all):
        print(line)
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    """雛形を出力先に書き出し、利用者が置く場所を出す。台帳のディレクトリは作らず、
    Claude Code の設定ディレクトリの中には書かない（置くのは利用者）。"""
    from tsuzuki.export import TEMPLATES, display_path, export_templates, placement
    from tsuzuki.tasks import within
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    cfg = read_config(ledger)
    claude_dir = resolve_claude_dir(args.claude_dir, os.environ, cfg)
    home = os.path.expanduser("~")
    root = _tasks_root(cfg, claude_dir, home)
    if root is None:
        return 1
    out = Path(os.path.abspath(os.path.expanduser(args.out)))
    if within(os.path.realpath(str(out)), os.path.realpath(str(claude_dir))):
        print("tsuzuki: Claude Code の設定ディレクトリの中には書き出しません。ほかの場所に書き出してから、"
              "手で置いてください: %s" % out, file=sys.stderr)
        return 1
    tasks_display = display_path(root, home)
    claude_display = display_path(claude_dir, home)
    try:
        written = export_templates(out, tasks_root_display=tasks_display)
    except FileExistsError as e:
        print("tsuzuki: 同じ名前のファイルがあります。何も書き出していません: %s" % e.filename, file=sys.stderr)
        return 1
    except OSError as e:
        print("tsuzuki: 書き出せません: %s" % e, file=sys.stderr)
        return 1
    for path, (_, rel) in zip(written, TEMPLATES):
        print("書き出しました: %s → %s" % (path, placement(rel, tasks_root_display=tasks_display,
                                                          claude_dir_display=claude_display)))
    return 0


def cmd_vscode_install(args: argparse.Namespace) -> int:
    """拡張を VSIX にまとめ、設定 vscode_app のアプリの code コマンドで入れる。台帳のディレクトリは作らない
    （依頼の置き場所は、初めて依頼を書くときに作る）。"""
    import subprocess
    import tempfile
    from tsuzuki.vscode_ext import EXTENSION_ID, build_vsix, code_cli, requests_dir
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    cfg = read_config(ledger)
    requests = requests_dir(ledger.resolve())
    if args.vsix:
        out = build_vsix(Path(os.path.abspath(os.path.expanduser(args.vsix))), requests)
        print("書き出しました: %s" % out)
        print("VS Code のコマンドパレットの「Extensions: Install from VSIX...」で、このファイルを選んで入れてください")
        return 0
    code = code_cli(cfg.vscode_app, os.path.expanduser("~"))
    if code is None:
        print("tsuzuki: %s の、拡張を入れるコマンド（code など）が見つかりません。--vsix で書き出して、手で入れてください"
              % cfg.vscode_app, file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        vsix = build_vsix(Path(tmp) / "tsuzuki-terminal.vsix", requests)
        try:
            proc = subprocess.run([code, "--install-extension", str(vsix), "--force"], capture_output=True,
                                  text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            print("tsuzuki: 入れられませんでした: %s" % e, file=sys.stderr)
            return 1
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print("tsuzuki: 入れられませんでした（終了コード %s）%s" % (proc.returncode, "\n" + detail if detail else ""),
              file=sys.stderr)
        return 1
    print("入れました: %s（%s）" % (EXTENSION_ID, cfg.vscode_app))
    print("画面の状況カードに「VS Code のターミナルで再開」が出ます（tsuzuki serve を起動し直してから）。開いている窓で"
          "反応しないときは、コマンドパレットの「Developer: Reload Window」で窓を読み込み直してください")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """設定ファイルは読まない（壊れていても Claude Code に影響させない）。必ず 0 で終わる。"""
    from tsuzuki.hook import STDIN_LIMIT, run_session_start
    try:
        text = sys.stdin.read(STDIN_LIMIT)
        ledger = ledger_dir_from(args.ledger_dir, os.environ)
        claude_dir = Path(args.claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
        return run_session_start(text, claude_dir=claude_dir, ledger_dir=ledger, start_pid=os.getppid(),
                                 env=os.environ)
    except BaseException:
        return 0


_MARKS = {"ok": "✔", "warn": "⚠", "fail": "✘"}


def cmd_doctor(args: argparse.Namespace) -> int:
    """台帳のディレクトリも台帳も作らない（なければ、ないまま点検する）。"""
    from tsuzuki import doctor
    from tsuzuki.store import LEDGER_FILENAME, open_ledger
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    cfg = read_config(ledger)
    claude_dir = resolve_claude_dir(args.claude_dir, os.environ, cfg)
    home = os.path.expanduser("~")
    checks = doctor.run_checks(cfg, ledger_dir=ledger, claude_dir=claude_dir, home=home)
    for c in checks:
        print("%s %s：%s" % (_MARKS[c.status], c.name, c.detail))
    if args.report:
        ledger_file = ledger / LEDGER_FILENAME
        conn = open_ledger(ledger_file) if ledger_file.exists() else None
        try:
            report = doctor.build_report(conn, claude_dir=claude_dir,
                                         tasks=doctor.task_report(cfg, claude_dir=claude_dir, home=home))
        finally:
            if conn is not None:
                conn.close()
        doctor.write_report(report, Path(args.report))
        print("診断ファイルを書きました（会話の中身は含みません）: %s" % args.report)
    return 1 if any(c.status == "fail" for c in checks) else 0


def _after_update(args: argparse.Namespace):
    """台帳から消す操作を、動いている更新が終わってから行う（更新の途中で消すと、更新がそのセッションを
    作り直すため）。待つときは知らせを出す。"""
    from tsuzuki.indexer import update_lock
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    return update_lock(ledger, on_wait=lambda: print(UPDATE_WAIT, file=sys.stderr, flush=True))


def cmd_purge(args: argparse.Namespace) -> int:
    from tsuzuki.retention import iso_utc, purge_before
    with _after_update(args):
        conn = _open(args)
        try:
            n = purge_before(conn, args.before, now_iso=iso_utc(time.time()))
        except ValueError as e:
            print("tsuzuki: %s" % e, file=sys.stderr)
            return 1
        finally:
            conn.close()
    print("%d件を台帳から消しました（以後は取り込みません）" % n)
    return 0


def _resolve_session(conn: sqlite3.Connection, ident: str, table: str, col: str):
    """完全な UUID か先頭 MIN_ID_PREFIX 文字以上の接頭辞から、セッション ID を1つに決める。
    決まらなければ (None, 終了コード) で、理由は stderr に出す。"""
    if _UUID.match(ident):
        return ident.lower(), 0
    like = ident.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    found = [r[0] for r in conn.execute(
        "SELECT %s FROM %s WHERE %s LIKE ? ESCAPE '\\'" % (col, table, col), (like,))]
    if not found:
        print("台帳に該当するセッションがありません: %s" % ident, file=sys.stderr)
        return None, 1
    if len(found) > 1:
        print("%d件のセッションに当たります。もう少し長く指定してください: %s" % (len(found), ident), file=sys.stderr)
        return None, 1
    return found[0], 0


def cmd_forget(args: argparse.Namespace) -> int:
    from tsuzuki.retention import forget, forget_undo, iso_utc
    ident = args.id.strip()
    if not _UUID.match(ident) and len(ident) < MIN_ID_PREFIX:
        print("ID は%d文字以上で指定してください" % MIN_ID_PREFIX, file=sys.stderr)
        return 2
    with _after_update(args):
        conn = _open(args)
        try:
            table, col = ("tombstones", "session_id") if args.undo else ("sessions", "id")
            session_id, code = _resolve_session(conn, ident, table, col)
            if session_id is None:
                return code
            if args.undo:
                if not forget_undo(conn, session_id):
                    print("取り消す記録がありません: %s" % ident, file=sys.stderr)
                    return 1
                print("取り消しました。次の更新で取り込みます")
            elif forget(conn, session_id, now_iso=iso_utc(time.time())):
                print("台帳から消しました（以後は取り込みません）。Claude Code の記録は消していません")
            else:  # 完全な ID が台帳になかった。以後の取り込みは止めるが、何も消していない
                print("台帳にこのセッションはありません。この ID は以後取り込みません（ID を間違えたときは "
                      "tsuzuki forget --undo で取り消せます）。Claude Code の記録は消していません")
        finally:
            conn.close()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from tsuzuki.resume import resume, resume_command, resume_cwd
    from tsuzuki.sources.live import live_sessions
    from tsuzuki.store import LEDGER_FILENAME, open_ledger
    ident = args.id.strip()
    if not _UUID.match(ident) and len(ident) < MIN_ID_PREFIX:
        print("ID は%d文字以上で指定してください" % MIN_ID_PREFIX, file=sys.stderr)
        return 2
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    conn = open_ledger(ledger / LEDGER_FILENAME)
    try:
        session_id, code = _resolve_session(conn, ident, "sessions", "id")
        if session_id is None:
            return code
        row = conn.execute("SELECT cwd, transcript_exists FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            print("台帳にないセッションです", file=sys.stderr)
            return 1
        if not row[1]:
            print("記録は削除済みのため再開できません", file=sys.stderr)
            return 1
        # 名前を変えたフォルダーで始めた会話は、今のパスで再開する（Terminal で開くときと同じ）
        print(resume_command(resume_cwd(conn, row[0] or ""), session_id))
        if not args.terminal:
            return 0
        cfg = load_config(ledger)
        claude_dir = resolve_claude_dir(args.claude_dir, os.environ, cfg)
        out = resume(conn, session_id, "terminal", cfg=cfg, live=live_sessions(claude_dir), confirm=False)
    finally:
        conn.close()
    if out.ok:
        print(out.message)
        return 0
    print(out.message, file=sys.stderr if not out.needs_confirm else sys.stdout)
    return 1


def _tasks_root(cfg, claude_dir: Path, home: str) -> Optional[Path]:
    """設定 tasks_root を解いた置き場所。置き場所にできない所なら理由を出して None（呼び出し側は 1 で終える）。
    更新はいつもこれを渡す（渡さない更新は、場所の表示名「タスク」「共通の資料」を戻してしまう）。"""
    try:
        return resolve_tasks_root(cfg, home=home, claude_dir=claude_dir)
    except ConfigError as e:
        print("tsuzuki: %s" % e, file=sys.stderr)
        return None


def cmd_index(args: argparse.Namespace) -> int:
    from tsuzuki.indexer import update
    from tsuzuki.store import LEDGER_FILENAME, open_ledger
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    cfg = load_config(ledger)
    claude_dir = resolve_claude_dir(args.claude_dir, os.environ, cfg)
    home = os.path.expanduser("~")
    tasks_root = _tasks_root(cfg, claude_dir, home)
    if tasks_root is None:
        return 1
    conn = open_ledger(ledger / LEDGER_FILENAME)
    try:
        rep = update(conn, cfg, claude_dir=claude_dir, ledger_dir=ledger, home=home, full=args.full,
                     tasks_root=tasks_root)
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        relations = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    finally:
        conn.close()
    line = "記録 %d件（取り込み %d件）・資料 %d件・つながり %d件・%.1f秒" % (
        rep.files_total, rep.files_ingested, docs, relations, rep.seconds)
    if rep.files_failed:
        line += "（読めなかったファイル %d件）" % rep.files_failed
    print(line)
    for path in rep.failed_paths[:FAILED_PATHS_SHOWN]:
        print("読めなかったファイル: %s" % path)
    for message in rep.errors + [rep.source_warning, rep.format_warning]:
        if message:
            print(message)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from tsuzuki.search import RESULT_LIMIT, SearchQuery, search
    words = list(args.words)
    if "--" in words:
        words.remove("--")  # オプションの終わりの印（最初の1つだけ）
    if not words:
        print("探す語を指定してください", file=sys.stderr)
        return 2
    query = SearchQuery(_search_text(words), days=args.days, role=args.role, order=args.order, limit=RESULT_LIMIT)
    conn = _open(args)
    try:
        result = search(conn, query)
        lines = _search_lines(conn, result, query)
    finally:
        conn.close()
    print("\n".join(lines))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from tsuzuki.server import serve
    if args.port is not None and not 0 <= args.port <= 65535:
        print("ポートは 0〜65535 で指定してください", file=sys.stderr)
        return 2
    ledger = ledger_dir_from(args.ledger_dir, os.environ)
    cfg = load_config(ledger)
    port = resolve_port(args.port, os.environ, cfg)
    claude_dir = resolve_claude_dir(args.claude_dir, os.environ, cfg)
    home = os.path.expanduser("~")
    tasks_root = _tasks_root(cfg, claude_dir, home)
    if tasks_root is None:
        return 1
    return serve(cfg, ledger_dir=ledger, claude_dir=claude_dir, home=home, port=port,
                 open_browser=bool(cfg.open_browser) and not args.no_open, tasks_root=tasks_root,
                 exit_on_stdin_eof=args.exit_on_stdin_eof)


def _search_text(words: List[str]) -> str:
    """コマンドの引数を検索の文にする。空白を含む引数は1つの句として "…" で囲む。
    " を含む引数は、画面の検索窓と同じ書き方とみなしてそのまま使う。"""
    return " ".join('"%s"' % w if '"' not in w and any(c.isspace() for c in w) else w for w in words)


def _local_time(ts: Optional[str]) -> str:
    """台帳の時刻（UTC の ISO）を、地方時の MM/DD HH:MM にする。"""
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return when.astimezone().strftime("%m/%d %H:%M")


def _fields(*parts: Optional[str]) -> str:
    return "  ".join(p for p in parts if p)


def _section(title: str, hits: list, query: SearchQuery, order: str = "recent") -> str:
    """一致の種類の見出し。上限まで返った種類には、そう書く（黙って切らない）。"""
    if len(hits) < query.limit:
        return title
    return "%s（%s順に%d件まで）" % (title, "一致の多い" if order == "count" else "新しい", query.limit)


def _search_lines(conn: sqlite3.Connection, result: SearchResult, query: SearchQuery) -> List[str]:
    from tsuzuki.ingest import choose_title
    sections = []  # type: List[List[str]]
    if result.sessions:
        lines = [_section("会話", result.sessions, query, query.order)]
        for hit in result.sessions:
            row = conn.execute(
                "SELECT s.custom_title, s.agent_name, s.ai_title, s.first_prompt, p.display FROM sessions s "
                "LEFT JOIN places p ON p.id = s.place_id WHERE s.id = ?", (hit.session_id,)).fetchone()
            title, place = ("", None) if row is None else (choose_title(row[0], row[1], row[2], row[3]), row[4])
            lines.append(_fields(" ".join(title.split()), place, _local_time(hit.last_ts)))
            lines.append(_INDENT + hit.snippet.text)
        sections.append(lines)
    if result.documents:
        lines = [_section("資料", result.documents, query)]
        for doc in result.documents:
            row = conn.execute("SELECT path FROM documents WHERE id = ?", (doc.doc_id,)).fetchone()
            lines.append(row[0] if row else "")
            lines.append(_INDENT + doc.snippet.text)
        sections.append(lines)
    if result.history:
        lines = [_section("入力履歴", result.history, query)]
        for item in result.history:
            row = conn.execute(
                "SELECT p.display FROM history_prompts h JOIN places p ON p.id = h.place_id WHERE h.id = ?",
                (item.history_id,)).fetchone()
            lines.append(_fields(row[0] if row else item.project, _local_time(item.ts)))
            lines.append(_INDENT + item.snippet.text)
        sections.append(lines)
    if not sections:
        return ["一致するものはありませんでした"]
    out = []  # type: List[str]
    for lines in sections:
        if out:
            out.append("")
        out.extend(lines)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    args, extras = parser.parse_known_args(argv)
    if args.cmd == "search":
        # search の先頭の「-」で始まる語は、argparse が知らないオプションとして extras に回す。語に戻す。
        # 戻した語は引数の末尾と一致するはずで、そうでなければ search より前に知らないオプションがある
        args.words = extras + args.words
        if args.words != argv[len(argv) - len(args.words):]:
            parser.error("unrecognized arguments: %s" % " ".join(extras))
    elif extras:
        parser.error("unrecognized arguments: %s" % " ".join(extras))
    if args.version:
        print("tsuzuki %s" % __version__)
        return 0
    if args.cmd is None:
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ConfigError as e:
        print("tsuzuki: %s" % e, file=sys.stderr)
        return 1
    except Exception as e:
        # sqlite3 は台帳を開く経路でだけ読み込まれる。読まれていなければ OperationalError ではない
        sqlite3 = sys.modules.get("sqlite3")
        if sqlite3 is None or not isinstance(e, sqlite3.OperationalError):
            raise
        # 台帳が使用中（書き込みの待ちが長すぎた）など
        print("台帳を開けませんでした（使用中かもしれません）: %s" % e, file=sys.stderr)
        return 1
