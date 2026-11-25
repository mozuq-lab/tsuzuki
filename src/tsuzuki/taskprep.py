"""タスクごとの「Claude で使う準備」：タスクの新しい会話を始めるフォルダー（taskstart.start_dir）に、決まりの文・
スキル・書き込みの許可を置く。

Tsuzuki が書く場所の例外の1つで、ボタンを押されたときだけ、次の物だけを書く（既にあるものは消さず、変えない）：
CLAUDE.local.md（末尾に足す）、.claude/skills/task-start・task-handoff の SKILL.md（なければ作る）、
.claude/settings.local.json の permissions.additionalDirectories（Git のリポジトリならその根。足す）。
Claude Code の設定ディレクトリの中には書かない。
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from tsuzuki.doctor import TASK_SKILLS, _additional_dirs_ok, _claude_md_ok
from tsuzuki.documents import should_scan_place
from tsuzuki.pathguard import is_inside
from tsuzuki.export import TEMPLATES, template_text
from tsuzuki.privacy import os_error_text
from tsuzuki.taskstart import git_root

README, RULE, SKILLS, ALLOW = "readme", "rule", "skills", "allow"
RULE_FILE = "CLAUDE.local.md"
SETTINGS_LOCAL = os.path.join(".claude", "settings.local.json")
SNIPPET_TEMPLATE = "templates/claude-md-snippet.md"


class TaskPrepError(Exception):
    """置けなかった理由（利用者に見せる日本語の文）。"""


@dataclass(frozen=True)
class Prep:
    """missing はまだ整っていない項目（README・RULE・SKILLS・ALLOW の順）、allow_file は書き込みの許可を足す
    settings.local.json、broad は会話を始めるフォルダーが広すぎて（ホーム・ディスクの根）何も置かないこと。"""
    missing: Tuple[str, ...]
    allow_file: str
    broad: bool = False


def _chain(start: str, stop: Optional[str] = None) -> List[str]:
    """start から親へ、stop まで（stop が None なら start だけ）のフォルダー。"""
    if stop is None:
        return [start]
    out, current = [], start
    while True:
        out.append(current)
        parent = os.path.dirname(current)
        if current == stop or parent == current:
            return out
        current = parent


def _all_parents(start: str) -> List[str]:
    out, current = [], start
    while True:
        out.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return out
        current = parent


BROAD_START = ("このタスクの会話はホーム（またはディスクの根）で始まるので、ここには置きません。"
               "リポジトリかワークスペースを決めてください")


def is_broad(start: str, home: str) -> bool:
    """start がホーム・ディスクの根（/Volumes の直下や /）か。そこに置くと、下のすべての会話に効いてしまう。"""
    homes = {os.path.normpath(home), os.path.realpath(home)}
    return any(not should_scan_place(p, h) for p in (os.path.abspath(start), os.path.realpath(start)) for h in homes)


def allow_file_for(start: str) -> str:
    """Claude Code が start で読む settings.local.json（Git のリポジトリなら、その根のもの）。"""
    return os.path.join(git_root(start) or start, SETTINGS_LOCAL)


def check(start: str, tasks_root: str, *, home: str, claude_dir: str) -> Prep:
    """start で始めた会話に、Claude が今すでに見えるもの。なければ missing に入れる。"""
    start = os.path.abspath(start)  # 手で書かれた /a/app/../app でも、正しい親をたどる
    if is_broad(start, home):
        return Prep((), os.path.join(start, SETTINGS_LOCAL), broad=True)
    root = Path(tasks_root)
    claude = Path(claude_dir)
    missing = []
    if not os.path.isfile(str(root / "README.md")):  # os.path は読めない場所でも False を返す（Path.is_file は上げる）
        missing.append(README)
    rule_files = [claude / "CLAUDE.md"]
    for d in _all_parents(start):
        rule_files += [Path(d) / "CLAUDE.md", Path(d) / RULE_FILE, Path(d) / ".claude" / "CLAUDE.md"]
    if not any(_claude_md_ok(f, root, home) for f in rule_files):
        missing.append(RULE)
    skill_dirs = [claude / "skills"] + [Path(d) / ".claude" / "skills" for d in _chain(os.path.realpath(start),
                                                                         git_root(start))]
    if not all(any(os.path.isfile(str(d / n / "SKILL.md")) for d in skill_dirs) for n in TASK_SKILLS):
        missing.append(SKILLS)
    allow_file = allow_file_for(start)
    inside = root == Path(start) or os.path.normpath(str(root)).startswith(os.path.normpath(start).rstrip("/") + "/")
    if not inside and not any(_additional_dirs_ok(f, root, home) for f in (
            Path(allow_file), Path(start) / ".claude" / "settings.json", claude / "settings.json")):
        missing.append(ALLOW)
    return Prep(tuple(missing), allow_file)


def _refuse(path: str, claude_dir: Optional[str]) -> None:
    """path の実体（リンクを解いた先）が Claude Code の設定ディレクトリの中なら断る。"""
    if claude_dir is None:
        return
    if is_inside(path, claude_dir):
        raise TaskPrepError("Claude Code の設定ディレクトリの中には書きません: %s" % path)


def _need_dir(start: str, claude_dir: Optional[str]) -> None:
    if not os.path.isdir(start):
        raise TaskPrepError("会話を始めるフォルダーがありません: %s" % start)
    _refuse(start, claude_dir)


def _fail(what: str, path: str, e: OSError) -> TaskPrepError:
    """e の文には一時ファイルの名前が入るので、errno と理由だけで文を作る（名前は path で言う）。"""
    bare = OSError(e.errno, e.strerror) if e.errno is not None else e
    return TaskPrepError("%s を書けませんでした: %s（%s）" % (what, path, os_error_text(bare, writing=True)))


def _confine(path: str, base: str, claude_dir: Optional[str]) -> None:
    """path の実体（途中のリンクも解いた先）が base の中で、Claude Code の設定ディレクトリの外か。違えば断る
    （.claude などがリンクで外を指していても、そこへは書かない）。"""
    _refuse(path, claude_dir)
    if not is_inside(path, base):
        raise TaskPrepError("リンクの先が会話を始めるフォルダーの外なので、書きません: %s" % path)


def _create_new(path: str, data: bytes) -> bool:
    """path を新しく（O_EXCL）作って書く。もうあれば False（何も変えない）。書けなければ作った分を消して OSError。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    try:
        try:
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return True


def _replace_existing(path: str, build) -> None:
    """あるファイル path を、build(今の中身のバイト列) の結果に、一時ファイルと os.replace で置き換える（権限は
    そのまま）。リンクは断り、書けないファイルは何も変えずに断る。build は TaskPrepError を上げてよい。"""
    if os.path.islink(path):
        raise TaskPrepError("%s はシンボリックリンクなので書きません" % path)
    try:
        with open(path, "r+b") as f:  # 書けるかの確かめ（書き込みはしない）
            old = f.read()
            mode = os.fstat(f.fileno()).st_mode & 0o7777
    except OSError as e:
        raise _fail(os.path.basename(path), path, e)
    data = build(old)
    tmp = "%s.tsuzuki-tmp-%d" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, mode)
            rest = data
            while rest:
                rest = rest[os.write(fd, rest):]
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise _fail(os.path.basename(path), path, e)


def _with_rule(old: bytes, snippet: str) -> bytes:
    """old の末尾に、空行をはさんで snippet を足す（既存の文は1バイトも変えない。改行は old のものに合わせる）。"""
    nl = b"\r\n" if b"\r\n" in old else b"\n"
    text = snippet.replace("\r\n", "\n").replace("\n", nl.decode()).encode("utf-8")
    if not old:
        return text
    head = old if old.endswith(b"\n") else old + nl
    if not head.endswith(nl + nl):
        head += nl
    return old + head[len(old):] + text


def place_rule(start: str, snippet: str, *, claude_dir: Optional[str] = None) -> str:
    """start の CLAUDE.local.md の末尾に決まりの文を足す（なければ作る）。置いたファイルのパスを返す。"""
    _need_dir(start, claude_dir)
    path = os.path.join(start, RULE_FILE)
    _confine(start, start, claude_dir)
    if not os.path.lexists(path):
        try:
            if _create_new(path, snippet.encode("utf-8")):
                return path
        except OSError as e:
            raise _fail(RULE_FILE, path, e)
    _replace_existing(path, lambda old: _with_rule(old, snippet))
    return path


def _template(rel: str, tasks_root_display: str) -> str:
    name = next(n for n, r in TEMPLATES if r == rel)
    return template_text(name, tasks_root_display=tasks_root_display)


def place_skills(start: str, *, tasks_root_display: str, claude_dir: Optional[str] = None
                 ) -> Tuple[str, List[str], List[str]]:
    """start の .claude/skills に task-start・task-handoff を置く。(skills のフォルダー, 置いた名前, 前からあって
    残した名前)。"""
    _need_dir(start, claude_dir)
    skills = os.path.join(start, ".claude", "skills")
    placed, kept = [], []  # type: List[str]
    for name in TASK_SKILLS:
        path = os.path.join(skills, name, "SKILL.md")
        data = _template("skills/%s/SKILL.md" % name, tasks_root_display).encode("utf-8")
        try:
            _confine(os.path.join(start, ".claude"), start, claude_dir)
            _confine(skills, start, claude_dir)
            _confine(os.path.dirname(path), start, claude_dir)
            _confine(path, start, claude_dir)
            if os.path.islink(path) and not os.path.isfile(path):
                raise TaskPrepError("%s はリンク先のないシンボリックリンクです。消してからもう一度押してください" % path)
            if os.path.isdir(path):
                raise TaskPrepError("%s はフォルダーです。消すか名前を変えてからもう一度押してください" % path)
            try:
                created = _create_new(path, data)
            except OSError as e:
                raise _fail("%s のスキル" % name, path, e)
        except TaskPrepError as e:
            if placed:
                raise TaskPrepError("%s は置きました。%s" % ("・".join(placed), e))
            raise
        (placed if created else kept).append(name)
    return skills, placed, kept


def _with_directory(old: Optional[bytes], path: str, directory: str) -> bytes:
    """old は今のファイルの中身（まだなければ None）。あるのに空や空白だけのときは、JSON ではないとして断る。"""
    try:
        data = {} if old is None else json.loads(old.decode("utf-8"))
    except ValueError:
        raise TaskPrepError("%s を JSON として読めないので、書きません" % path)
    if not isinstance(data, dict):
        raise TaskPrepError("%s の形が想定と違うので、書きません" % path)
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise TaskPrepError("%s の permissions の形が想定と違うので、書きません" % path)
    dirs = perms.setdefault("additionalDirectories", [])
    if not isinstance(dirs, list):
        raise TaskPrepError("%s の additionalDirectories の形が想定と違うので、書きません" % path)
    if directory not in dirs:
        dirs.append(directory)
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def place_allow(start: str, tasks_root: str, *, claude_dir: Optional[str] = None) -> str:
    """start で Claude Code が読む settings.local.json の permissions.additionalDirectories に、タスクの置き場所の
    絶対パスを足す（なければ作る）。置いたファイルのパスを返す。"""
    _need_dir(start, claude_dir)
    path = allow_file_for(start)
    base = os.path.dirname(os.path.dirname(path))  # 設定を読む Claude Code の基準のフォルダー（Git の根か start）
    _confine(base, base, claude_dir)
    _confine(os.path.dirname(path), base, claude_dir)
    _confine(path, base, claude_dir)
    directory = os.path.abspath(tasks_root)
    if not os.path.lexists(path):
        data = _with_directory(None, path, directory)
        try:
            if _create_new(path, data):
                return path
        except OSError as e:
            raise _fail("settings.local.json", path, e)
    _replace_existing(path, lambda old: _with_directory(old, path, directory))
    return path


def rule_snippet(tasks_root_display: str) -> str:
    return template_text(SNIPPET_TEMPLATE, tasks_root_display=tasks_root_display)
