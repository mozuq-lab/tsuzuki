"""task.md の読み取りと、タスクのフォルダー・資料の見分け。

tsuzuki パッケージのほかのモジュールを import しない（循環を避け、SessionStart の hook を軽く保つため）。
"""
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

STAGES: Tuple[str, ...] = ("チケット", "調査", "調整", "実装", "PR", "修正", "完了")
SECTION_NAMES: Tuple[str, ...] = ("目的", "現在地", "次の一手", "待っていること", "決めたこと", "資料")
TASK_MD = "task.md"
SHARED_DIR = "shared"

_STAGE_LABELS = {"実装": "実装・テスト"}
_KIND_BY_WORD = {"research": "research", "design": "design", "plan": "plan",
                 "review": "response", "notes": "notes"}

_H2 = re.compile(r"^##\s+(.+?)\s*$")
_H1 = re.compile(r"^#\s+(.+?)\s*$")
# コードの囲みの開き（画面の render._FENCE と同じ決まり：3つ以上の ` か ~。` の囲みの後には ` を書けない）。
# 閉じは、同じ文字だけの、開きと同じ数以上の行
_FENCE = re.compile(r"^[ \t]*(`{3,}(?=[^`]*$)|~{3,})")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_AT = re.compile(r"@(\S+)")
_BULLET = re.compile(r"^\s*[-*+]\s+(\S+)")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-|$)")
_KIND_WORD = re.compile(r"^([A-Za-z]+)[-_]")


@dataclass
class TaskFile:
    title: Optional[str] = None
    ticket: Optional[str] = None
    stage: str = STAGES[0]
    stage_raw: Optional[str] = None
    waiting: bool = False
    repo: Optional[str] = None
    branch: Optional[str] = None
    workspace: Optional[str] = None
    created: Optional[str] = None
    updated: Optional[str] = None
    heading: Optional[str] = None
    sections: Dict[str, str] = field(default_factory=dict)
    frontmatter_ok: bool = False


def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _split_frontmatter(lines: List[str]) -> Tuple[Optional[Dict[str, str]], List[str]]:
    """(見出し情報, 本文の行)。見出し情報がないか閉じていなければ None と全体。"""
    if not lines or lines[0].strip() != "---":
        return None, lines
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta: Dict[str, str] = {}
            for line in lines[1:i]:
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                meta.setdefault(key.strip(), _unquote(value))
            return meta, lines[i + 1:]
    return None, lines


def parse_task_md(text: str) -> TaskFile:
    if text.startswith("﻿"):
        text = text[1:]
    meta, body = _split_frontmatter(text.splitlines())
    tf = TaskFile()
    if meta is not None:
        tf.frontmatter_ok = True
        tf.title = meta.get("title") or None
        tf.ticket = meta.get("ticket") or None
        tf.repo = meta.get("repo") or None
        tf.branch = meta.get("branch") or None
        tf.workspace = meta.get("workspace") or None
        tf.created = meta.get("created") or None
        tf.updated = meta.get("updated") or None
        tf.waiting = meta.get("waiting", "").lower() == "true"
        raw = meta.get("stage")
        if raw is not None:
            tf.stage_raw = raw
            if raw in STAGES:
                tf.stage = raw
    current: Optional[str] = None
    buf: List[str] = []
    fence: Optional[str] = None  # 開いているコードの囲みの印。中の # の行は見出しではない

    def flush() -> None:
        if current is not None and current not in tf.sections:
            tf.sections[current] = "\n".join(buf).strip()

    for line in body:
        if fence is not None:  # 囲みの中（閉じの行も）は、見出しを探さずに欄へ入れる
            s = line.strip()
            if len(s) >= len(fence) and s == fence[0] * len(s):
                fence = None
            if current is not None:
                buf.append(line)
            continue
        opened = _FENCE.match(line)
        if opened:
            fence = opened.group(1)
        m2 = _H2.match(line)
        m1 = _H1.match(line)
        if m1 and tf.heading is None:
            tf.heading = m1.group(1)
        if m1 or m2:
            flush()
            current, buf = None, []
            if m2 and m2.group(1) in SECTION_NAMES:
                current = m2.group(1)
            continue
        if current is not None:
            buf.append(line)
    flush()
    return tf


def at_reference(path: str) -> str:
    """Claude に渡す @ の形。空白を含み " を含まないパスは @"<パス>"、ほかは @<パス>。
    tsuzuki が @ を出す所（画面のコピー・新しい会話の依頼・tsuzuki task new）は、すべてこれを使う。"""
    if '"' not in path and any(c.isspace() for c in path):
        return '@"%s"' % path
    return "@" + path


def task_title(tf: TaskFile, folder: str) -> str:
    return tf.title or tf.heading or folder


def is_task_dir_name(name: str) -> bool:
    return name != SHARED_DIR and not name.startswith((".", "_"))


def discover_tasks(tasks_root: str, *, strict: bool = False) -> List[str]:
    """task.md を持つタスクのフォルダー。strict なら、置き場所を一覧できないとき OSError を投げる
    （一覧できなかった回と、空だった回を区別するため）。既定は空の一覧を返す。"""
    try:
        entries = sorted(os.scandir(tasks_root), key=lambda e: e.name)
    except OSError:
        if strict:
            raise
        return []
    found = []
    for e in entries:
        try:
            if (is_task_dir_name(e.name) and e.is_dir(follow_symlinks=False)
                    and os.path.isfile(os.path.join(e.path, TASK_MD))):
                found.append(os.path.abspath(e.path))
        except OSError:
            continue
    return found


def created_from_name(name: str) -> Optional[str]:
    m = _DATE_PREFIX.match(name)
    return m.group(1) if m else None


def no_waiting(text: Optional[str]) -> bool:
    return text is None or text.strip() in ("", "なし")


def ticket_numbers(ticket: Optional[str]) -> List[str]:
    out: List[str] = []
    for part in (ticket or "").split(","):
        part = part.strip()
        m = re.fullmatch(r"#?(\d+)", part) or re.search(r"://.*?/(\d+)/?(?:[?#].*)?$", part)
        if m:
            out.append(m.group(1))
    return out


def ref_paths(section: Optional[str], task_dir: str, home: str) -> List[str]:
    """資料の欄が名指すパス。1行ごとに、リンク、@、箇条書きの先頭の語の順で探す。"""
    out: List[str] = []

    def add(raw: str) -> None:
        if "://" in raw or raw.startswith(("#", "mailto:")):
            return
        if raw == "~" or raw.startswith("~/"):
            path = home.rstrip("/") + raw[1:]
        elif raw.startswith("/"):
            path = raw
        else:
            path = os.path.join(task_dir, raw)
        path = os.path.normpath(path)
        if path not in out:
            out.append(path)

    for line in (section or "").splitlines():
        links = _LINK.findall(line)
        if links:
            for raw in links:
                add(raw)
            continue
        m = _AT.search(line)
        if m:
            add(m.group(1))
            continue
        m = _BULLET.match(line)
        if m and m.group(1).startswith(("/", "~/", "./", "../")):
            add(m.group(1))
    return out


def within(path: str, root: str) -> bool:
    """path が root と同じか、その下にあるか（パスの区切りで比べる。indexer._within と同じ）。"""
    return path == root or path.startswith(root.rstrip("/") + "/")


def task_doc_kind(path: str, tasks_root: str) -> Optional[str]:
    if path == tasks_root or not within(path, tasks_root):
        return None
    rel = path[len(tasks_root.rstrip("/")) + 1:]
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2:
        return None
    first = parts[0]
    if first != SHARED_DIR and not is_task_dir_name(first):
        return None
    name = parts[-1]
    if name == TASK_MD:  # 表紙は、タスクのフォルダーの直下の task.md だけ
        return "cover" if len(parts) == 2 and first != SHARED_DIR else None
    m = _KIND_WORD.match(name)
    return _KIND_BY_WORD.get(m.group(1).lower()) if m else None


def list_task_files(task_dir: str, *, exclude_dirs: Sequence[str], max_depth: int,
                    max_files: int) -> Tuple[List[str], bool]:
    """隠しファイル・除外のフォルダー・シンボリックリンクを除いた全ファイルと、上限で切ったか。"""
    files: List[str] = []
    excluded = set(exclude_dirs)

    def walk(directory: str, depth: int) -> bool:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return False
        for e in entries:
            if e.name.startswith(".") or e.is_symlink():
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in excluded or depth >= max_depth:
                        continue
                    if walk(e.path, depth + 1):
                        return True
                elif e.is_file(follow_symlinks=False):
                    if len(files) >= max_files:
                        return True
                    files.append(os.path.abspath(e.path))
            except OSError:
                continue
        return False

    cut = walk(task_dir, 1)
    return files, cut
