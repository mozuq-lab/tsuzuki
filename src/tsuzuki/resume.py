"""セッションの再開。ID は UUID の形のものだけを使い、パスはシェルの規則で引用する。"""
import os
import re
import shlex
import sqlite3
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from tsuzuki.config import Config
from tsuzuki.sources.live import LiveSession
from tsuzuki.tasks import TASK_MD, at_reference

VSCODE_URI = "vscode://anthropic.claude-code/open?session={session_id}"
VSCODE_PROMPT_URI = "vscode://anthropic.claude-code/open?prompt={prompt}"
STEP_TIMEOUT = 10  # 外のコマンド1つあたりの待ち秒数
REASON_CHARS = 200  # 失敗の理由として出す、外のコマンドの stderr の1行の長さの上限

_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TASK_MD_MARK = re.compile(r"@?\{task_md\}")  # 新しい会話の依頼の雛形の、task.md の入る所


def is_session_id(s: str) -> bool:
    """小文字の16進の 8-4-4-4-12 の形か。"""
    return isinstance(s, str) and _SESSION_ID.fullmatch(s) is not None


def resume_command(cwd: str, session_id: str) -> str:
    """そのフォルダーでセッションを再開するコマンド。ID の形でなければ ValueError
    （貼り付けて実行される文字列に、台帳の外から来た文字を混ぜないため）。"""
    if not is_session_id(session_id):
        raise ValueError("セッションの ID の形ではありません: %r" % (session_id,))
    return "cd " + shlex.quote(cwd) + " && claude --resume " + session_id


def _run(*args, **kwargs):
    """subprocess.run の呼び出しを実行時に引く（テストで差し替えられるように既定の runner にする）。"""
    return subprocess.run(*args, **kwargs)


def applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def vscode_steps(cwd: str, session_id: str, app: str) -> List[List[str]]:
    """先にそのフォルダーの窓を前に出し、そのあとで URI を開く（順番が逆だと別の窓で新しい会話が始まる）。"""
    if not is_session_id(session_id):
        raise ValueError("セッションの ID の形ではありません: %r" % (session_id,))
    return [["open", "-a", app, cwd], ["open", VSCODE_URI.format(session_id=session_id)]]


def terminal_steps(cwd: str, session_id: str) -> List[List[str]]:
    return [["osascript",
             "-e", 'tell application "Terminal" to do script ' + applescript_quote(resume_command(cwd, session_id)),
             "-e", 'tell application "Terminal" to activate']]


@dataclass
class ActionOutcome:
    ok: bool
    needs_confirm: bool
    message: str
    command: Optional[str] = None  # copy のとき、貼り付けて実行するコマンド


def run_steps(steps: List[List[str]], *, runner=_run, sleep=time.sleep, delay_ms: int = 0) -> None:
    """手順を順に実行し、手順の間に delay_ms 待つ。失敗は OSError・TimeoutExpired・RuntimeError で伝える
    （RuntimeError の文は、コマンドの stderr の最初の行と終了コード）。"""
    for i, step in enumerate(steps):
        if i > 0 and delay_ms > 0:
            sleep(delay_ms / 1000)
        proc = runner(step, check=False, capture_output=True, timeout=STEP_TIMEOUT)
        if proc.returncode != 0:
            code = "終了コード %s" % proc.returncode
            reason = _first_line(getattr(proc, "stderr", None))
            raise RuntimeError("%s（%s）" % (reason, code) if reason else code)


def _first_line(output: object) -> str:
    """外のコマンドの出力の、空でない最初の行（「Unable to find application named …」など）。長ければ切る。"""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    if not isinstance(output, str):
        return ""
    for line in output.splitlines():
        if line.strip():
            return line.strip()[:REASON_CHARS]
    return ""


def _fail(e: BaseException) -> ActionOutcome:
    if isinstance(e, subprocess.TimeoutExpired):
        reason = "時間切れ"
    else:
        reason = str(e) or e.__class__.__name__
    return ActionOutcome(False, False, "開けませんでした: " + reason)


def _execute(steps: List[List[str]], ok_message: str, *, runner, sleep=time.sleep, delay_ms: int = 0) -> ActionOutcome:
    try:
        run_steps(steps, runner=runner, sleep=sleep, delay_ms=delay_ms)
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        return _fail(e)
    return ActionOutcome(True, False, ok_message)


def resume(conn: sqlite3.Connection, session_id: str, how: str, *, cfg: Config, live: Dict[str, LiveSession],
           confirm: bool, runner=_run, sleep=time.sleep, isdir=os.path.isdir) -> ActionOutcome:
    if how not in ("vscode", "terminal"):
        return ActionOutcome(False, False, "再開の方法が正しくありません")
    row = conn.execute("SELECT cwd, transcript_exists FROM sessions WHERE id = ?", (session_id,)).fetchone() \
        if is_session_id(session_id) else None
    if row is None:
        return ActionOutcome(False, False, "台帳にないセッションです")
    cwd = row[0] or ""
    if not row[1]:
        return ActionOutcome(False, False, "記録は削除済みのため再開できません")
    if not cwd or not isdir(cwd):
        return ActionOutcome(False, False, "作業ディレクトリが見つかりません: %s" % cwd)
    if session_id in live and not confirm:
        return ActionOutcome(False, True, "このセッションは今開いています。続けますか？")
    if how == "vscode":
        return _execute(vscode_steps(cwd, session_id, cfg.vscode_app), "VS Code で開きました", runner=runner,
                        sleep=sleep, delay_ms=cfg.vscode_uri_delay_ms)
    return _execute(terminal_steps(cwd, session_id), "Terminal で開きました", runner=runner, sleep=sleep)


def open_document(conn: sqlite3.Connection, doc_id: int, how: str, *, cfg: Config, runner=_run) -> ActionOutcome:
    row = conn.execute("SELECT path, ext FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return ActionOutcome(False, False, "台帳にない資料です")
    path, ext = row[0], (row[1] or "").lower().lstrip(".")
    if not os.path.isfile(path):
        return ActionOutcome(False, False, "ファイルが見つかりません: %s" % path)
    if how == "vscode":
        steps = [["open", "-a", cfg.vscode_app, path]]
    elif how == "finder":
        steps = [["open", "-R", path]]
    elif how == "browser":
        if ext not in ("html", "htm"):
            return ActionOutcome(False, False, "この資料はブラウザで開けません")
        steps = [["open", path]]
    else:
        return ActionOutcome(False, False, "開き方が正しくありません")
    return _execute(steps, "開きました", runner=runner)


def open_place(conn: sqlite3.Connection, place_id: int, how: str, *, cfg: Config, runner=_run) -> ActionOutcome:
    row = conn.execute("SELECT path FROM places WHERE id = ?", (place_id,)).fetchone()
    if row is None:
        return ActionOutcome(False, False, "台帳にない場所です")
    path = row[0]
    if not os.path.isdir(path):
        return ActionOutcome(False, False, "フォルダーが見つかりません: %s" % path)
    if how == "vscode":
        steps = [["open", "-a", cfg.vscode_app, path]]
    elif how == "finder":
        steps = [["open", path]]
    else:
        return ActionOutcome(False, False, "開き方が正しくありません")
    return _execute(steps, "開きました", runner=runner)


def conversation_prompt(template: str, task_md: str) -> str:
    """設定の雛形の @{task_md} を task.md の @ の形（at_reference。空白を含むパスは @"…"）に、@ のない {task_md} を
    絶対パスに置き換える（他の波括弧には触れない）。"""
    return _TASK_MD_MARK.sub(lambda m: at_reference(task_md) if m.group(0).startswith("@") else task_md, template)


def new_conversation_command(cwd: str, prompt: str) -> str:
    return "cd " + shlex.quote(cwd) + " && claude " + shlex.quote(prompt)


def vscode_new_steps(cwd: str, prompt: str, app: str) -> List[List[str]]:
    """先にそのフォルダーの窓を前に出し、そのあとで依頼つきの URI を開く（順番が逆だと別の窓で始まる）。"""
    return [["open", "-a", app, cwd],
            ["open", VSCODE_PROMPT_URI.format(prompt=urllib.parse.quote(prompt, safe=""))]]


def terminal_new_steps(cwd: str, prompt: str) -> List[List[str]]:
    return [["osascript",
             "-e", 'tell application "Terminal" to do script '
                   + applescript_quote(new_conversation_command(cwd, prompt)),
             "-e", 'tell application "Terminal" to activate']]


def start_conversation(conn: sqlite3.Connection, task: str, how: str, *, cfg: Config, runner=_run,
                       sleep=time.sleep, isdir=os.path.isdir) -> ActionOutcome:
    if how not in ("vscode", "terminal", "copy"):
        return ActionOutcome(False, False, "開き方が正しくありません")
    row = conn.execute("SELECT path, repo_resolved FROM tasks WHERE name = ?", (task,)).fetchone()
    if row is None:
        return ActionOutcome(False, False, "台帳にないタスクです")
    task_dir, repo = row[0] or "", row[1]
    cwd = repo if repo and isdir(repo) else task_dir
    if not cwd or not isdir(cwd):
        return ActionOutcome(False, False, "作業ディレクトリが見つかりません: %s" % cwd)
    prompt = conversation_prompt(cfg.new_conversation_prompt, os.path.join(task_dir, TASK_MD))
    if how == "copy":
        return ActionOutcome(True, False, "コマンドをコピーしました", new_conversation_command(cwd, prompt))
    if how == "vscode":
        return _execute(vscode_new_steps(cwd, prompt, cfg.vscode_app), "VS Code で新しい会話を開きました",
                        runner=runner, sleep=sleep, delay_ms=cfg.vscode_uri_delay_ms)
    return _execute(terminal_new_steps(cwd, prompt), "Terminal で新しい会話を始めました", runner=runner)
