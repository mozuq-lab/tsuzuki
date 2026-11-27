"""タスクのフォルダーへ書き込む唯一のコード。

書くのは「新しいタスクのフォルダーと task.md」と「既存の task.md の見出し情報の行と待っていることの欄」だけ。
画面の設定から変えるのは title / ticket / repo / workspace の行と、題が変わったときの本文の見出しと、短い名前を
変えたときの former_names の行（前のフォルダー名を足す）だけ。短い名前を変えたときは、フォルダーの名前も変える（下）。
あとは、頼まれたときにタスクのフォルダーをゴミ箱へ動かす（消さずに、動かすだけ）ことと、ほかの場所の資料を
タスクのフォルダーの直下へコピーする（元は変えず、今あるファイルも上書きしない）こと、置き場所の直下に
README.md がなければ雛形から置くこと（今あるファイルは上書きしない）、頼まれたときに、置き場所の中でタスクの
フォルダーの名前を変えること（同じ置き場所の中の os.rename だけで、ほかのフォルダーは上書きしない）。
"""
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from tsuzuki.pathguard import is_inside
from tsuzuki.privacy import os_error_text
from tsuzuki.tasks import (_FENCE, _H1, _H2, STAGES, TASK_MD, created_from_name, format_former_names, is_task_dir_name,
                           parse_former_names, parse_task_md, task_title, ticket_chunks)
from tsuzuki.workspace import WorkspaceError, first_folder

CHANGED = "task.md が変わりました。読み直してください"
NO_TRASH = "このディスクのゴミ箱が見つかりません。Finder でゴミ箱に入れてください"
CANNOT_TRASH = "ゴミ箱に入れられませんでした（%s）。Finder でゴミ箱に入れてください"
NOT_A_TASK_FOLDER = "ゴミ箱に入れられるのはタスクのフォルダーだけです: %s"
NOT_A_COPY_TARGET = "コピー先はタスクのフォルダーだけです: %s"
NOT_A_FILE = "資料が見つかりません: %s"
NAME_TAKEN = "その名前のフォルダーがもうあります: %s"
NOT_A_RENAME_TARGET = "名前を変えられるのはタスクのフォルダーだけです: %s"
COMMA_IN_NAME = "フォルダーの名前に「,」は使えません（前の名前の記録で区切りに使うため）: %s"
COMMA_IN_CURRENT = "今のフォルダー名に「,」があるので、ここでは名前を変えられません。Finder で変えてください: %s"
CANNOT_RENAME = "名前を変えられませんでした（%s）"
ALREADY_IN_TASK = "この資料はもうタスクのフォルダーの中にあります"
CANNOT_COPY = "コピーできませんでした: %s"
MAX_COPY_NAMES = 999  # name-999 まで試して、なお空かなければ諦める
BAD_WORKSPACE = "ワークスペースを使えません: %s"
BAD_SECTION_LINE = "見出しやコードの囲みになる行は書けません: %s"
WAITING_SECTION = "待っていること"
NOTHING = "なし"
_BOM = "﻿"
_SECTION_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_ALLOWED_KEYS = ("stage", "waiting", "title", "ticket", "repo", "workspace", "former_names")
_TEXT_KEYS = ("title", "ticket", "repo", "workspace", "former_names")  # 1行の文字の値：改行と制御文字は断る
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")  # splitlines が行の切れ目にする U+0085・U+2028・U+2029 を含む


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


_TICKET_WORD = re.compile(r"#?[0-9]+|https?://\S+")


def normalize_ticket(ticket: Optional[str]) -> Optional[str]:
    """チケットの値。全角の数字や ＃ は半角にそろえ（NFKC）、, ・ 、 ・ ，で分け、番号と URL だけの断片は空白でも
    分け、数字だけの番号は #番号 にして、重なりを除き、", " でつなぐ（「Redmine 123」のような断片は1つのまま）。"""
    pieces = []  # type: List[str]
    for chunk in ticket_chunks(unicodedata.normalize("NFKC", ticket or "")):
        words = chunk.split()
        if words and all(_TICKET_WORD.fullmatch(w) for w in words):
            pieces += words
        elif words:
            pieces.append(" ".join(words))
    out = []  # type: List[str]
    for piece in pieces:
        piece = "#" + piece if re.fullmatch(r"[0-9]+", piece) else piece
        if piece not in out:
            out.append(piece)
    return ", ".join(out) or None


def _quoteless(ticket: Optional[str]) -> Optional[str]:
    """チケットの値から " を除く（task.md に引用符で包んで書くため）。"""
    return ticket.replace('"', "") if ticket else ticket


def _one_line(value: str) -> str:
    return " ".join(value.split())


def render_task_md(*, title: str, ticket: Optional[str], repo: Optional[str], created: str, updated: str,
                   workspace: Optional[str] = None) -> str:
    """新しい task.md の文。workspace があれば、見出し情報の repo の行の次に workspace の行を書く。"""
    title = _one_line(title)
    ticket_line = 'ticket: "%s"' % ticket.replace('"', "") if ticket else "ticket:"
    repo_line = "repo: %s" % _one_line(repo) if repo and _one_line(repo) else "repo:"
    if workspace:
        repo_line += "\nworkspace: %s" % workspace
    return (
        "---\ntitle: %s\n%s\nstage: %s\nwaiting: false\n%s\ncreated: %s\nupdated: %s\n---\n"
        "# %s\n\n## 目的\n\n## 現在地\n\n## 次の一手\n\n## 待っていること\nなし\n\n## 決めたこと\n\n## 資料\n"
    ) % (title, ticket_line, STAGES[0], repo_line, created, updated, title)


def _refuse_claude_dir(tasks_root: Path, claude_dir: Optional[Path]) -> None:
    """置き場所の実体（シンボリックリンクを解いた先）が Claude Code の設定ディレクトリの中なら断る。
    ~/.claude には書かない決まりの、書く直前の守り（config.resolve_tasks_root と同じ文言）。"""
    if claude_dir is None:
        return
    if is_inside(str(tasks_root), str(claude_dir)):
        raise TaskWriteError("タスクの置き場所に Claude Code の設定ディレクトリの中は使えません: %s" % tasks_root)


def place_tasks_readme(tasks_root: Path, text: str, *, claude_dir: Optional[Path] = None) -> Path:
    """置き場所の直下に README.md を新しく置く。今あるもの（シンボリックリンクも）は上書きせず、断る。"""
    if not os.path.isdir(str(tasks_root)):
        raise TaskWriteError("タスクの置き場所がありません: %s" % tasks_root)
    _refuse_claude_dir(tasks_root, claude_dir)
    path = Path(os.path.abspath(str(tasks_root))) / "README.md"
    data = text.encode("utf-8")  # 開く前に（符号化の失敗で空のファイルを残さない）
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise TaskWriteError("README.md はもうあります。上書きはしません: %s" % path)
    except OSError as e:
        raise TaskWriteError("README.md を置けませんでした（%s）" % os_error_text(e, writing=True))
    try:
        try:
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
    except OSError as e:
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise TaskWriteError("README.md を置けませんでした（%s）" % os_error_text(e, writing=True))
    return path


def create_task(tasks_root: Path, short_name: str, *, title: Optional[str] = None, ticket: Optional[str] = None,
                repo: Optional[str] = None, now: Optional[datetime] = None,
                claude_dir: Optional[Path] = None, workspace: Optional[str] = None,
                home: Optional[str] = None) -> Path:
    """タスクのフォルダーと task.md を作る。workspace があれば、フォルダーを作る前に設定と同じく確かめ
    （だめなら TaskWriteError で、何も作らない）、~ を home で展開した絶対パスを書く。"""
    name = normalize_short_name(short_name)
    _refuse_claude_dir(tasks_root, claude_dir)
    workspace_path = normalize_workspace(workspace, home=home or os.path.expanduser("~")) if workspace else None
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
                          created=now.strftime("%Y-%m-%d"), updated=now.strftime("%Y-%m-%dT%H:%M"),
                          workspace=workspace_path)
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
    . や _ で始まる名前を除く）であることを確かめ、その実体を返す。違えば message % task_dir で断る。"""
    real = os.path.realpath(task_dir)
    name = os.path.basename(real)
    if (os.path.dirname(real) != os.path.realpath(str(tasks_root)) or not os.path.isdir(real)
            or not is_task_dir_name(name)):
        raise TaskWriteError(message % task_dir)
    return real


def _date_prefix(name: str) -> str:
    """名前の頭の「YYYY-MM-DD-」（ハイフンまで）。なければ空。日付だけの名前には日付の頭がないとする。"""
    date = created_from_name(name)
    return date + "-" if date and name.startswith(date + "-") else ""


def short_name_of(name: str) -> str:
    """フォルダー名の短い名前。YYYY-MM-DD- で始まればその後ろ、そうでなければ名前のまま。"""
    return name[len(_date_prefix(name)):] or name


def renamed_folder_name(name: str, short_name: str) -> str:
    """name の日付を残して、短い名前だけを short_name に替えたフォルダー名。日付がなければ短い名前だけ。"""
    short = normalize_short_name(short_name)
    if "," in short:
        raise TaskWriteError(COMMA_IN_NAME % short)
    return _date_prefix(name) + short


def rename_task(task_dir: str, tasks_root: Path, new_name: str, *, claude_dir: Optional[Path] = None) -> str:
    """タスクのフォルダーを、置き場所の直下の new_name に名前を変え、新しいパスを返す。同じ置き場所の中の
    os.rename だけで、ほかのフォルダーは上書きしない（大文字小文字だけの違いは、同じフォルダーなので許す）。"""
    real = _check_task_folder(task_dir, tasks_root, NOT_A_RENAME_TARGET)
    _refuse_claude_dir(tasks_root, claude_dir)
    if "," in os.path.basename(real):
        raise TaskWriteError(COMMA_IN_CURRENT % os.path.basename(real))
    if "/" in new_name or not is_task_dir_name(new_name):
        raise TaskWriteError("フォルダーの名前に使えない文字があります: %s" % new_name)
    target = os.path.join(os.path.dirname(real), new_name)
    if os.path.lexists(target):
        try:
            same = not os.path.islink(target) and os.path.samefile(real, target)
        except OSError:
            same = False
        if not same:
            raise TaskWriteError(NAME_TAKEN % new_name)
    try:
        os.rename(real, target)
    except OSError as e:
        raise TaskWriteError(CANNOT_RENAME % (e.strerror or e))
    return target


def copy_into_task(task_dir: str, tasks_root: Path, source: str, *, claude_dir: Optional[Path] = None) -> str:
    """source（普通のファイル）をタスクのフォルダーの直下へコピーし、コピーした先のパスを返す。元は動かさず、
    変えない。同じ名前があれば name-2.ext・name-3.ext…にして、今あるファイルは上書きしない（宛先の
    シンボリックリンクもたどらない）。コピーに失敗したら、書きかけのファイルを消す。"""
    real = _check_task_folder(task_dir, tasks_root, NOT_A_COPY_TARGET)
    _refuse_claude_dir(tasks_root, claude_dir)
    src = os.path.realpath(source)
    if not os.path.isfile(src):
        raise TaskWriteError(NOT_A_FILE % source)
    if os.path.dirname(src) == real or src.startswith(real + "/"):
        raise TaskWriteError(ALREADY_IN_TASK)
    stem, ext = os.path.splitext(os.path.basename(source))  # 名前は渡されたパスのまま（コピー済みの目印と合わせる）
    for n in range(1, MAX_COPY_NAMES + 1):
        dest = os.path.join(real, stem + ext if n == 1 else "%s-%d%s" % (stem, n, ext))
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)  # O_EXCL は宛先のリンクもたどらない
        except FileExistsError:
            continue
        except OSError as e:
            raise TaskWriteError(CANNOT_COPY % (e.strerror or e))
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
                shutil.copyfileobj(f, out)
        except OSError as e:
            try:
                os.unlink(dest)
            except OSError:
                pass
            raise TaskWriteError(CANNOT_COPY % os_error_text(e))
        return dest
    raise TaskWriteError(CANNOT_COPY % "同じ名前のファイルが多すぎます")


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
            raise TaskWriteError("変えられない項目です: %s" % key)
        if key in _TEXT_KEYS and _CONTROL.search(value):
            raise TaskWriteError("%s に改行や制御文字は使えません" % key)
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


def _expand_home(value: str, home: str) -> str:
    """先頭の ~ を home で展開する（手で書かれた ~/… を絶対パスと比べるため）。"""
    value = value.strip()
    return home.rstrip("/") + value[1:] if value == "~" or value.startswith("~/") else value


def normalize_workspace(value: str, *, home: str) -> Optional[str]:
    """ワークスペースのパス。空なら None。~ は home で展開し、絶対パスの .code-workspace で、普通のファイルで、
    最初のフォルダーがあるものだけを、normpath した絶対パスで返す。それ以外は TaskWriteError。"""
    value = value.strip()
    if not value:
        return None
    if _CONTROL.search(value):
        raise TaskWriteError(BAD_WORKSPACE % "改行や制御文字は使えません")
    if value == "~" or value.startswith("~/"):
        value = home.rstrip("/") + value[1:]
    if not os.path.isabs(value):
        raise TaskWriteError(BAD_WORKSPACE % "絶対パスで指定してください")
    path = os.path.normpath(value)
    if not path.endswith(".code-workspace"):
        raise TaskWriteError(BAD_WORKSPACE % ".code-workspace のファイルを指定してください")
    if not os.path.isfile(path):
        raise TaskWriteError(BAD_WORKSPACE % "ファイルがありません")
    try:
        first_folder(path)
    except WorkspaceError as e:
        raise TaskWriteError(BAD_WORKSPACE % e)
    return path


def update_settings(task_md: Path, tasks_root: Path, *, title: str, ticket: str, repo: str, workspace: str, home: str,
                    expect_mtime: Optional[float] = None, expect_size: Optional[int] = None,
                    claude_dir: Optional[Path] = None, former: Optional[str] = None) -> bool:
    """題・チケット・リポジトリ・ワークスペースのうち、今と違う項目の行だけを書く。題が変われば、本文の
    見出し（今の題と同じときだけ）も同じ置き換えで変える。書いたら True、変わった所がなければ書かずに False。
    値のそろえと確かめは、書く前にすべて済ませる。former があれば、task.md の former_names に（なければ後ろへ）
    足し、ほかに変わる所がなくても書く（もうあれば足さない）。"""
    for value in (title, ticket, repo, workspace, former or ""):
        if _CONTROL.search(value):
            raise TaskWriteError("改行や制御文字は使えません")
    if former and parse_former_names(former) != [former]:
        raise TaskWriteError("前のフォルダー名に使えない文字があります: %s" % former)
    new_title = _one_line(title)
    if not new_title:
        raise TaskWriteError("題を入れてください")
    new_ticket = _quoteless(normalize_ticket(ticket))
    new_repo = _one_line(repo)
    folder = task_md.parent.name

    class _Unchanged(Exception):
        pass

    def transform(text: str) -> str:
        tf = parse_task_md(text)
        changes = {}  # type: Dict[str, str]
        if new_title != _one_line(task_title(tf, folder)):
            changes["title"] = new_title
        if new_ticket != _quoteless(normalize_ticket(tf.ticket)):
            changes["ticket"] = '"%s"' % new_ticket if new_ticket else ""
        if new_repo != _one_line(tf.repo or ""):
            changes["repo"] = new_repo
        current = _expand_home(tf.workspace or "", home)
        if workspace.strip() not in (tf.workspace or "", current):  # 触っていなければ確かめない
            new_workspace = normalize_workspace(workspace, home=home)
            if new_workspace != current:
                changes["workspace"] = new_workspace or ""
        if former:
            names = list(tf.former_names)
            if former not in names:
                changes["former_names"] = format_former_names(names + [former])
        if not changes:
            raise _Unchanged()
        out = _rewrite(text, changes)
        return replace_heading(out, task_title(tf, folder), new_title) if "title" in changes else out
    try:
        _edit(task_md, tasks_root, transform, expect_mtime=expect_mtime, expect_size=expect_size,
              claude_dir=claude_dir)
    except _Unchanged:
        return False
    return True


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


def replace_heading(text: str, old: str, new: str) -> str:
    """task.md の全文の、本文の最初の「# 見出し」（コードの囲みの外。parse_task_md の heading と同じ行）が old と
    同じときだけ、「# new」にする。囲みが閉じないまま見出しに着かなければ、何も変えない。"""
    bom = _BOM if text.startswith(_BOM) else ""
    lines = text[len(bom):].splitlines(True)  # type: List[str]  # parse_task_md と同じ行の切り方
    start = 0
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if close is None:
            return text
        start = close + 1
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
        m = _H1.match(line)
        if m:
            if m.group(1) == old:
                lines[i] = "# %s%s" % (new, lines[i][len(line):])
            break
    return bom + "".join(lines)


def _line(key: str, value: str) -> str:
    """見出し情報の1行（行末は含まない）。値が空なら「key:」だけにする（雛形と同じ）。"""
    return "%s: %s" % (key, value) if value else key + ":"


def _rewrite(text: str, changes: Dict[str, str]) -> str:
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    first_break = body.find("\n")
    nl = "\r\n" if first_break > 0 and body[first_break - 1] == "\r" else "\n"
    lines = re.findall(r"[^\n]*\n|[^\n]+", body)  # type: List[str]
    if not lines or lines[0].strip() != "---":
        block = "".join(_line(k, v) + nl for k, v in changes.items())
        return bom + "---" + nl + block + "---" + nl + body
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        raise TaskWriteError("task.md の見出し情報が閉じていません。直してから変えてください")
    pending = dict(changes)
    for i in range(1, close):
        key = lines[i].partition(":")[0].strip() if ":" in lines[i] else None
        if key in pending:
            ending = lines[i][len(lines[i].rstrip("\r\n")):]
            lines[i] = _line(key, pending.pop(key)) + ending
    if pending:
        if not lines[close - 1].endswith("\n") and close > 1:
            lines[close - 1] += nl
        lines[close:close] = [_line(k, v) + nl for k, v in pending.items()]
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
