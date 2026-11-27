"""タスクの置き場所を台帳に写す。タスクのフォルダー（task.md を持つもの）ごとに
tasks の行と資料を入れ、消えたタスクの行を消す。task.md を書き換えるのは taskwrite の役目で、ここは読むだけ。"""
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from tsuzuki.config import Config
from tsuzuki.documents import upsert_document
from tsuzuki.places import place_id_for
from tsuzuki.privacy import os_error_text
from tsuzuki.store import get_meta, set_meta
from tsuzuki.tasks import (
    SHARED_DIR, TASK_MD, created_from_name, discover_tasks, is_task_dir_name, list_task_files, parse_task_md,
    ref_paths, task_title,
)

SHARED_MTIME_KEY = "tasks:shared_mtime"
ROOT_KEY = "tasks:root"  # 台帳に入れたときのタスクの置き場所（変わったら、古い置き場所の行を空にする）
LIST_ERROR = "タスクの置き場所を一覧できませんでした（この回は何も消しません）: %s: %s"


@dataclass
class TaskSync:
    changed: bool  # タスクの行が増えた・変わった・消えた
    tasks: int
    error: Optional[str] = None  # 置き場所を一覧できなかったときの文（この回は何も消さない）


def sync_tasks(conn: sqlite3.Connection, cfg: Config, *, tasks_root: str, home: str, places) -> TaskSync:
    root = os.path.normpath(tasks_root)
    stored = get_meta(conn, ROOT_KEY)
    if stored != root:
        if stored:  # 画面を通さずに置き場所が変わった（config.json を直して起動し直したなど）
            reset_task_index(conn, stored)
        with conn:
            set_meta(conn, ROOT_KEY, root)
    if not os.path.isdir(root):
        # 置き場所が見えない回は、手で直した記録（task_overrides）を消さない
        had = conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone() is not None
        had = had or conn.execute("SELECT 1 FROM task_sessions LIMIT 1").fetchone() is not None
        if had:
            with conn:
                conn.execute("DELETE FROM tasks")
                conn.execute("DELETE FROM task_sessions")
        return TaskSync(changed=had, tasks=0)
    place_id = places.id_for(root)
    try:
        found = discover_tasks(root, strict=True)
    except OSError as e:
        # 一覧できなかった回は「タスクがない」とみなさず、何も消さない（手で直した記録も、行も）
        return TaskSync(changed=False, tasks=conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                        error=LIST_ERROR % (root, os_error_text(e)))
    changed = False
    for task_dir in found:
        try:
            changed |= _sync_one(conn, cfg, root, home, task_dir, place_id, force=False)
        except OSError:
            continue  # 読めない task.md は、今ある行のままにして先へ進む
    names = {os.path.basename(p) for p in found}
    changed |= _drop_vanished(conn, names)
    _sync_shared(conn, cfg, root, home, places)
    return TaskSync(changed=changed, tasks=len(found))


def refresh_task(conn: sqlite3.Connection, cfg: Config, *, tasks_root: str, home: str, name: str) -> bool:
    """1つのタスクだけを読み直す（画面からの書き込みの後に使う）。読み直せたら True。"""
    root = os.path.normpath(tasks_root)
    task_dir = os.path.join(root, name)
    if (not is_task_dir_name(name) or os.path.islink(task_dir) or not os.path.isdir(task_dir)
            or not os.path.isfile(os.path.join(task_dir, TASK_MD))):
        return False
    row = conn.execute("SELECT id FROM places WHERE path = ?", (root,)).fetchone()
    place_id = row[0] if row else place_id_for(conn, root, home=home, strip_prefixes=cfg.display_strip_prefixes,
                                               tasks_root=root)
    try:
        _sync_one(conn, cfg, root, home, task_dir, place_id, force=True)
    except OSError:
        return False
    return True


def _scan(cfg: Config, directory: str) -> Tuple[List[str], float]:
    """フォルダーの中のファイルと、変化の印（ファイルと、ファイルを含むフォルダーと、そのフォルダー自身の
    更新時刻の最大）。フォルダーの更新時刻は、ファイルが足された・消えた・名前が変わったときに変わる。"""
    files, _cut = list_task_files(directory, exclude_dirs=cfg.doc_scan.exclude_dirs,
                                  max_depth=cfg.doc_scan.max_depth, max_files=cfg.doc_scan.max_files)
    paths = set(files) | {os.path.dirname(f) for f in files} | {directory}
    latest = 0.0
    for p in paths:
        try:
            latest = max(latest, os.stat(p).st_mtime)
        except OSError:
            continue
    return files, latest


def _index_documents(conn, cfg: Config, root: str, home: str, files: List[str], place_id: int) -> bool:
    """資料を入れる。1つでも入れられなかったら False（呼び出し側は変化の印を残さず、次の回にやり直す）。"""
    ok = True
    for path in files:
        try:
            upsert_document(conn, path, place_id=place_id, home=home, exts=cfg.doc_extensions, tasks_root=root)
        except (OSError, ValueError):
            ok = False
    return ok


def _sync_one(conn, cfg: Config, root: str, home: str, task_dir: str, place_id: int, *, force: bool) -> bool:
    """1つのタスクを入れる。tasks の行が変わったら True。task.md が読めなければ OSError を投げ、
    そのときは行にも資料にも触れない。"""
    name = os.path.basename(task_dir)
    md_path = os.path.join(task_dir, TASK_MD)
    st = os.stat(md_path)
    files, signature = _scan(cfg, task_dir)
    row = conn.execute("SELECT md_mtime, md_size, files_mtime, path FROM tasks WHERE name = ?", (name,)).fetchone()
    # フォルダーを動かしても更新時刻は変わらないので、path が違えば変わったものとして読み直す
    if not force and row is not None and tuple(row) == (st.st_mtime, st.st_size, signature, task_dir):
        return False
    with open(md_path, encoding="utf-8", errors="replace") as f:
        tf = parse_task_md(f.read())
    # 資料を先に入れ、行（変化の印）は最後に書く。途中で止まっても、次の回にやり直せる
    ok = _index_documents(conn, cfg, root, home, files, place_id)
    _drop_missing_documents(conn, task_dir, set(files))
    values = {
        "path": task_dir, "title": task_title(tf, name), "ticket": tf.ticket, "stage": tf.stage,
        "stage_raw": tf.stage_raw, "waiting": 1 if tf.waiting else 0, "repo": _abs_repo(tf.repo, home),
        "branch": tf.branch, "created": tf.created or created_from_name(name), "updated": tf.updated,
        "frontmatter_ok": 1 if tf.frontmatter_ok else 0,
        "sections": json.dumps(tf.sections, ensure_ascii=False),
        "refs": json.dumps(ref_paths(tf.sections.get("資料"), task_dir, home), ensure_ascii=False),
        "former": json.dumps(tf.former_names, ensure_ascii=False),
        "md_mtime": st.st_mtime, "md_size": st.st_size, "files_mtime": signature if ok else 0.0,
    }
    return _write_row(conn, name, values)


def _abs_repo(repo: Optional[str], home: str) -> Optional[str]:
    if not repo:
        return None
    if repo == "~" or repo.startswith("~/"):
        repo = home.rstrip("/") + repo[1:]
    return os.path.normpath(repo) if os.path.isabs(repo) else None


def _write_row(conn, name: str, values: Dict[str, object]) -> bool:
    """行を入れる。repo_resolved は結び付けが決めるので触れない。中身が変わったとき True。"""
    cols = list(values)
    old = conn.execute("SELECT %s FROM tasks WHERE name = ?" % ", ".join(cols), (name,)).fetchone()
    if old is not None and dict(zip(cols, old)) == values:
        return False
    with conn:
        if old is None:
            conn.execute("INSERT INTO tasks(name, %s) VALUES (?, %s)" % (", ".join(cols), ", ".join("?" * len(cols))),
                         [name] + [values[c] for c in cols])
        else:
            conn.execute("UPDATE tasks SET %s WHERE name = ?" % ", ".join("%s = ?" % c for c in cols),
                         [values[c] for c in cols] + [name])
    return True


def _bounds(directory: str) -> Tuple[str, str]:
    """directory の下のパスの範囲（path >= 下限 AND path < 上限）。"/" の次の文字が "0"。"""
    base = directory.rstrip("/")
    return base + "/", base + "0"


def _documents_under(conn, directory: str) -> List[Tuple[int, str]]:
    low, high = _bounds(directory)
    return conn.execute("SELECT id, path FROM documents WHERE path >= ? AND path < ?", (low, high)).fetchall()


def _drop_missing_documents(conn, directory: str, present: Set[str]) -> None:
    gone = [(i,) for i, p in _documents_under(conn, directory) if p not in present]
    if gone:
        with conn:
            conn.executemany("DELETE FROM documents WHERE id = ?", gone)


def _drop_vanished(conn, names: Set[str]) -> bool:
    """なくなったタスクの行・会話との結び付き・資料を消す。手で足した・外した記録（task_overrides）は残す
    （フォルダーが戻れば元に戻る）。消すのは、画面でゴミ箱へ動かしたとき（forget_task）だけ。"""
    gone = [(n, p) for n, p in conn.execute("SELECT name, path FROM tasks").fetchall() if n not in names]
    stale = {r[0] for r in conn.execute("SELECT task FROM task_sessions").fetchall()} - names
    stale -= {n for n, _ in gone}
    for n, p in gone:
        _drop_task(conn, n, p, keep_overrides=True)
    for n in stale:
        _drop_task(conn, n, None, keep_overrides=True)
    return bool(gone)


def reset_task_index(conn, old_root: str) -> None:
    """タスクの置き場所を切り替えるとき、古い置き場所の分の行を空にする（次の更新が、新しい置き場所の
    すべてのタスクと shared/ を読み直す）。手で足した・外した記録（task_overrides）は残す。"""
    with conn:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM task_sessions")
        low, high = _bounds(os.path.normpath(old_root))
        conn.execute("DELETE FROM documents WHERE path >= ? AND path < ?", (low, high))
        conn.execute("DELETE FROM meta WHERE key = ?", (SHARED_MTIME_KEY,))


def forget_task(conn, name: str, path: str) -> None:
    """台帳から、タスクの行・会話との結び付き・手で直した記録・そのフォルダーの資料を消す（フォルダーを動かした後）。"""
    _drop_task(conn, name, path, keep_overrides=False)


def _drop_task(conn, name: str, path: Optional[str], *, keep_overrides: bool = False) -> None:
    with conn:
        conn.execute("DELETE FROM tasks WHERE name = ?", (name,))
        conn.execute("DELETE FROM task_sessions WHERE task = ?", (name,))
        if not keep_overrides:
            conn.execute("DELETE FROM task_overrides WHERE task = ?", (name,))
        if path:
            low, high = _bounds(path)
            conn.execute("DELETE FROM documents WHERE path >= ? AND path < ?", (low, high))


def _sync_shared(conn, cfg: Config, root: str, home: str, places) -> None:
    shared = os.path.join(root, SHARED_DIR)
    if not os.path.isdir(shared):
        _drop_missing_documents(conn, shared, set())
        with conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (SHARED_MTIME_KEY,))
        return
    place_id = places.id_for(shared)
    files, signature = _scan(cfg, shared)
    if get_meta(conn, SHARED_MTIME_KEY) == repr(signature):
        return
    ok = _index_documents(conn, cfg, root, home, files, place_id)
    _drop_missing_documents(conn, shared, set(files))
    if ok:  # 入れられないものがあれば印を残さず、次の回にやり直す
        with conn:
            set_meta(conn, SHARED_MTIME_KEY, repr(signature))
