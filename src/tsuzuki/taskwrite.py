"""タスクのフォルダーへ書き込む唯一のコード。

書くのは「新しいタスクのフォルダーと task.md」と「既存の task.md の stage / waiting の行」だけ。
あとは、頼まれたときにタスクのフォルダーをゴミ箱へ動かす（消さずに、動かすだけ）。
"""
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from tsuzuki.tasks import _FENCE, _H1, _H2, SHARED_DIR, STAGES, TASK_MD, parse_task_md

CHANGED = "task.md が変わりました。読み直してください"
NO_TRASH = "このディスクのゴミ箱が見つかりません。Finder でゴミ箱に入れてください"
CANNOT_TRASH = "ゴミ箱に入れられませんでした（%s）。Finder でゴミ箱に入れてください"
NOT_A_TASK_FOLDER = "ゴミ箱に入れられるのはタスクのフォルダーだけです: %s"
BAD_SECTION_LINE = "見出しやコードの囲みになる行は書けません: %s"
WAITING_SECTION = "待っていること"
NOTHING = "なし"
_BOM = "﻿"
_SECTION_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
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


def _check_task_folder(task_dir: str, tasks_root: Path, message: str) -> str:
    """task_dir の実体（シンボリックリンクを解いた先）が置き場所の直下のタスクのフォルダー（shared と
    . で始まる名前を除く）であることを確かめ、その実体を返す。違えば message % task_dir で断る。"""
    real = os.path.realpath(task_dir)
    name = os.path.basename(real)
    if (os.path.dirname(real) != os.path.realpath(str(tasks_root)) or not os.path.isdir(real)
            or name == SHARED_DIR or name.startswith(".")):
        raise TaskWriteError(message % task_dir)
    return real


def find_trash(path: str, home: str) -> Optional[str]:
    """path と同じディスクのゴミ箱。ホームと同じディスクなら <home>/.Trash、違えば path のあるディスクの根の
    .Trashes/<uid>（フォルダーで、書けるとき）。なければ None。"""
    try:
        dev = os.stat(path).st_dev
        home_trash = os.path.join(home, ".Trash")
        if os.path.isdir(home_trash) and os.stat(home_trash).st_dev == dev:
            return home_trash
        top = os.path.realpath(path)
        while True:  # st_dev が変わる手前まで上へたどる
            parent = os.path.dirname(top)
            if parent == top or os.stat(parent).st_dev != dev:
                break
            top = parent
    except OSError:
        return None
    trash = os.path.join(top, ".Trashes", str(os.getuid()))
    return trash if os.path.isdir(trash) and os.access(trash, os.W_OK) else None


def trash_task(task_dir: str, tasks_root: Path, *, home: str, claude_dir: Optional[Path] = None,
               now: Optional[datetime] = None, trash_dir: Optional[str] = None) -> str:
    """タスクのフォルダーをゴミ箱へ動かし、動かした先のパスを返す。同じ名前があれば「名前 HH.MM.SS」、
    それもあれば「名前 HH.MM.SS 2」…にする。trash_dir を渡せば、ゴミ箱を探さない（テスト用）。"""
    real = _check_task_folder(task_dir, tasks_root, NOT_A_TASK_FOLDER)
    _refuse_claude_dir(tasks_root, claude_dir)
    trash = trash_dir or find_trash(real, home)
    if not trash:
        raise TaskWriteError(NO_TRASH)
    name = os.path.basename(real)
    stamped = "%s %s" % (name, (now or datetime.now()).strftime("%H.%M.%S"))
    target, n = os.path.join(trash, name), 1
    while os.path.lexists(target):
        target = os.path.join(trash, stamped if n == 1 else "%s %d" % (stamped, n))
        n += 1
    try:
        os.rename(real, target)
    except OSError as e:
        raise TaskWriteError(CANNOT_TRASH % (e.strerror or e))
    return target


def set_fields(task_md: Path, tasks_root: Path, changes: Dict[str, str], *, expect_mtime: Optional[float] = None,
               expect_size: Optional[int] = None, claude_dir: Optional[Path] = None) -> None:
    for key, value in changes.items():
        if key not in _ALLOWED_KEYS:
            raise TaskWriteError("変えられる項目は stage と waiting だけです: %s" % key)
        if key == "stage" and value not in STAGES:
            raise TaskWriteError("段階の値が正しくありません: %s" % value)
        if key == "waiting" and value not in ("true", "false"):
            raise TaskWriteError("waiting は true か false で指定してください: %s" % value)
    _edit(task_md, tasks_root, lambda text: _rewrite(text, changes), expect_mtime=expect_mtime,
          expect_size=expect_size, claude_dir=claude_dir)


def set_waiting(task_md: Path, tasks_root: Path, *, waiting: bool, reason: Optional[str],
                expect_mtime: Optional[float] = None, expect_size: Optional[int] = None,
                claude_dir: Optional[Path] = None) -> None:
    """waiting の行を書き、reason があれば「## 待っていること」の欄も同じ置き換えで書く（reason が None なら行だけ）。"""
    content = None if reason is None else section_text(reason)

    def transform(text: str) -> str:
        new = _rewrite(text, {"waiting": "true" if waiting else "false"})
        return new if content is None else replace_section(new, WAITING_SECTION, content)
    _edit(task_md, tasks_root, transform, expect_mtime=expect_mtime, expect_size=expect_size, claude_dir=claude_dir)


def _edit(task_md: Path, tasks_root: Path, transform: Callable[[str], str], *, expect_mtime: Optional[float],
          expect_size: Optional[int], claude_dir: Optional[Path]) -> None:
    """置き場所の確かめ、読み（カードの時刻と大きさが違えば CHANGED）、transform で作った全文での置き換え。"""
    real = _check_inside(task_md, tasks_root)
    _refuse_claude_dir(tasks_root, claude_dir)
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
    _replace_file(real, transform(text), st.st_mode)


def section_text(text: str) -> str:
    """欄に書く文を整える：改行を \\n にそろえ、各行の末尾の空白と前後の空行を除く。空なら「なし」。
    見出しやコードの囲みになる行があれば断る（task.md の欄の区切りが変わってしまう）。"""
    lines = [line.rstrip() for line in text.splitlines()]  # parse_task_md と同じ行の切り方
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    for line in lines:
        if _SECTION_HEADING.match(line) or _H1.match(line) or _H2.match(line) or _FENCE.match(line):
            raise TaskWriteError(BAD_SECTION_LINE % line)
    return "\n".join(lines) or NOTHING


def _bare(line: str) -> str:
    """行末（splitlines の切れ目）を除いた行。"""
    return line.splitlines()[0] if line else ""


def _ended(line: str) -> bool:
    return _bare(line) != line


def replace_section(text: str, name: str, content: str) -> str:
    """task.md の全文の、本文の「## name」の欄の中身を content にする。コードの囲みの中は見出しとしない
    （parse_task_md と同じ）。欄がなければ「## 決めたこと」の前に、それもなければ終わりに足す。"""
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    first_break = body.find("\n")
    nl = "\r\n" if first_break > 0 and body[first_break - 1] == "\r" else "\n"
    lines = body.splitlines(True)  # type: List[str]  # parse_task_md と同じ行の切り方。行末はそのまま残す
    start = 0
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if close is None:
            raise TaskWriteError("task.md の見出し情報が閉じていません。直してから変えてください")
        start = close + 1
    headings = []  # type: List[tuple]  # 囲みの外の # と ## の行：(行の番号, ## の名前か None)
    fence = None  # type: Optional[str]
    for i in range(start, len(lines)):
        line = _bare(lines[i])
        if fence is not None:
            s = line.strip()
            if len(s) >= len(fence) and s == fence[0] * len(s):
                fence = None
            continue
        opened = _FENCE.match(line)
        if opened:
            fence = opened.group(1)
        m2, m1 = _H2.match(line), _H1.match(line)
        if m1 or m2:
            headings.append((i, m2.group(1) if m2 else None))
    if fence is not None:
        raise TaskWriteError("task.md のコードの囲みが閉じていません。直してから変えてください")
    new_lines = [part + nl for part in content.split("\n")]
    found = next((i for i, h in headings if h == name), None)
    if found is not None:
        end = next((i for i, _ in headings if i > found), len(lines))
        head = lines[found] if _ended(lines[found]) else lines[found] + nl
        tail = [nl] if end < len(lines) else []
        lines[found:end] = [head] + new_lines + tail
    else:
        block = ["## %s%s" % (name, nl)] + new_lines
        before = next((i for i, h in headings if h == "決めたこと"), None)
        if before is not None:
            lines[before:before] = block + [nl]
        else:
            if lines and not _ended(lines[-1]):
                lines[-1] += nl
            if lines[start:] and lines[-1].strip():
                lines.append(nl)
            lines += block
    return bom + "".join(lines)


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
