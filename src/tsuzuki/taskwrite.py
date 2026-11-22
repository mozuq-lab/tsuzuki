"""タスクのフォルダーへ書き込む唯一のコード。

書くのは「新しいタスクのフォルダーと task.md」と「既存の task.md の stage / waiting の行」だけ。
"""
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from tsuzuki.tasks import STAGES, TASK_MD, parse_task_md

CHANGED = "task.md が変わりました。読み直してください"
_BOM = "﻿"
_ALLOWED_KEYS = ("stage", "waiting")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class TaskWriteError(Exception):
    pass


def normalize_short_name(name: str) -> str:
    name = (name or "").strip()
    if _CONTROL.search(name):
        raise TaskWriteError("タスクの短い名前に制御文字は使えません")
    name = "-".join(name.split())
    if not name or "/" in name or name.startswith("."):
        raise TaskWriteError("タスクの短い名前に使えない文字があります（空、/、先頭の . は使えません）")
    return name


def normalize_ticket(ticket: Optional[str]) -> Optional[str]:
    """チケットの値。全角の数字や ＃ は半角にそろえ（NFKC）、数字だけなら #番号 にする。"""
    ticket = " ".join(unicodedata.normalize("NFKC", ticket or "").split())
    if not ticket:
        return None
    return "#" + ticket if re.fullmatch(r"[0-9]+", ticket) else ticket


def _one_line(value: str) -> str:
    return " ".join(value.split())


def render_task_md(*, title: str, ticket: Optional[str], repo: Optional[str], created: str, updated: str) -> str:
    title = _one_line(title)
    ticket_line = 'ticket: "%s"' % ticket.replace('"', "") if ticket else "ticket:"
    repo_line = "repo: %s" % _one_line(repo) if repo and _one_line(repo) else "repo:"
    return (
        "---\ntitle: %s\n%s\nstage: %s\nwaiting: false\n%s\ncreated: %s\nupdated: %s\n---\n"
        "# %s\n\n## 目的\n\n## 現在地\n\n## 次の一手\n\n## 待っていること\nなし\n\n## 決めたこと\n\n## 資料\n"
    ) % (title, ticket_line, STAGES[0], repo_line, created, updated, title)


def _refuse_claude_dir(tasks_root: Path, claude_dir: Optional[Path]) -> None:
    """置き場所の実体（シンボリックリンクを解いた先）が Claude Code の設定ディレクトリの中なら断る。
    ~/.claude には書かない決まりの、書く直前の守り（config.resolve_tasks_root と同じ文言）。"""
    if claude_dir is None:
        return
    real, claude = os.path.realpath(str(tasks_root)), os.path.realpath(str(claude_dir))
    if real == claude or real.startswith(claude.rstrip("/") + "/"):
        raise TaskWriteError("タスクの置き場所に Claude Code の設定ディレクトリの中は使えません: %s" % tasks_root)


def create_task(tasks_root: Path, short_name: str, *, title: Optional[str] = None, ticket: Optional[str] = None,
                repo: Optional[str] = None, now: Optional[datetime] = None,
                claude_dir: Optional[Path] = None) -> Path:
    name = normalize_short_name(short_name)
    _refuse_claude_dir(tasks_root, claude_dir)
    now = now or datetime.now()
    title = _one_line(title or "") or name
    root = Path(os.path.abspath(str(tasks_root)))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TaskWriteError("タスクの置き場所を作れません: %s（%s）" % (root, e))
    folder = root / ("%s-%s" % (now.strftime("%Y-%m-%d"), name))
    try:
        os.mkdir(str(folder))
    except FileExistsError:
        raise TaskWriteError("同じ名前のフォルダーがあります: %s" % folder)
    except OSError as e:
        raise TaskWriteError("フォルダーを作れません: %s（%s）" % (folder, e))
    text = render_task_md(title=title, ticket=normalize_ticket(ticket), repo=repo,
                          created=now.strftime("%Y-%m-%d"), updated=now.strftime("%Y-%m-%dT%H:%M"))
    md = folder / TASK_MD
    try:
        with open(str(md), "x", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as e:
        raise TaskWriteError("task.md を書けません: %s（%s）" % (md, e))
    return md


def _check_inside(task_md: Path, tasks_root: Path) -> str:
    real = os.path.realpath(str(task_md))
    folder = os.path.dirname(real)
    root = os.path.realpath(str(tasks_root))
    if os.path.basename(real) != TASK_MD or os.path.dirname(folder) != root or folder == root:
        raise TaskWriteError("タスクの置き場所の外には書けません: %s" % task_md)
    return real


def set_fields(task_md: Path, tasks_root: Path, changes: Dict[str, str], *, expect_mtime: Optional[float] = None,
               expect_size: Optional[int] = None, claude_dir: Optional[Path] = None) -> None:
    real = _check_inside(task_md, tasks_root)
    _refuse_claude_dir(tasks_root, claude_dir)
    for key, value in changes.items():
        if key not in _ALLOWED_KEYS:
            raise TaskWriteError("変えられる項目は stage と waiting だけです: %s" % key)
        if key == "stage" and value not in STAGES:
            raise TaskWriteError("段階の値が正しくありません: %s" % value)
        if key == "waiting" and value not in ("true", "false"):
            raise TaskWriteError("waiting は true か false で指定してください: %s" % value)
    try:
        st = os.stat(real)
        if (expect_mtime is not None and st.st_mtime != expect_mtime) or \
                (expect_size is not None and st.st_size != expect_size):
            raise TaskWriteError(CHANGED)
        with open(real, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise TaskWriteError("task.md を読めません: %s（%s）" % (task_md, e))
    if expect_size is not None and len(raw) != expect_size:
        raise TaskWriteError(CHANGED)
    new_text = _rewrite(text, changes)
    _replace_file(real, new_text, st.st_mode)


def _rewrite(text: str, changes: Dict[str, str]) -> str:
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    first_break = body.find("\n")
    nl = "\r\n" if first_break > 0 and body[first_break - 1] == "\r" else "\n"
    lines = re.findall(r"[^\n]*\n|[^\n]+", body)  # type: List[str]
    if not lines or lines[0].strip() != "---":
        block = "".join("%s: %s%s" % (k, v, nl) for k, v in changes.items())
        return bom + "---" + nl + block + "---" + nl + body
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        raise TaskWriteError("task.md の見出し情報が閉じていません。直してから変えてください")
    pending = dict(changes)
    for i in range(1, close):
        key = lines[i].partition(":")[0].strip() if ":" in lines[i] else None
        if key in pending:
            ending = lines[i][len(lines[i].rstrip("\r\n")):]
            lines[i] = "%s: %s%s" % (key, pending.pop(key), ending)
    if pending:
        if not lines[close - 1].endswith("\n") and close > 1:
            lines[close - 1] += nl
        lines[close:close] = ["%s: %s%s" % (k, v, nl) for k, v in pending.items()]
    return bom + "".join(lines)


def _replace_file(real: str, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".task-", suffix=".tmp", dir=os.path.dirname(real))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, real)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise TaskWriteError("task.md を書けません: %s（%s）" % (real, e))
