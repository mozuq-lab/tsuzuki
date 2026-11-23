"""台帳の更新の全体の流れ。各段の中身は既存のモジュールに任せ、
ここは順番と受け渡しだけを持つ。"""
import fcntl
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from tsuzuki.config import Config
from tsuzuki.documents import scan_documents, should_scan_place, upsert_document
from tsuzuki.ingest import IngestContext, IngestResult, ingest_transcript
from tsuzuki.places import TMP_PLACE, PlaceRef, display_name, encode_project, place_id_for, resolve_place
from tsuzuki.relations import infer_relations
from tsuzuki.retention import DAY_SECONDS, apply_max_age, apply_missing, drop_forgotten, iso_utc
from tsuzuki.sources.history import sync_history
from tsuzuki.sources.lineage import sync_lineage
from tsuzuki.store import REREAD_KEY, get_meta, set_meta
from tsuzuki.taskindex import sync_tasks
from tsuzuki.tasklinks import RELINK_KEY, link_tasks
from tsuzuki.tasks import within as task_within

FORMAT_WARNING = "記録の形式が変わった可能性があります"
SOURCE_MISSING = "Claude Code の記録の場所が見つかりません: %s"
SOURCE_EMPTY = "Claude Code の記録が1件も見つかりません: %s（消えたとはみなしていません）"
HISTORY_ERROR = "入力履歴を読めませんでした: %s: %s"
LINEAGE_ERROR = "フックの記録を読めませんでした: %s: %s"
LOCK_FILENAME = "update.lock"
FAILED_PATHS_LIMIT = 20  # UpdateReport.failed_paths に残す、読めなかった記録のパスの数
# 行がこれ以上あるのに会話を1つも読めないファイルがあれば、形式の変化を疑う
DRIFT_MIN_RECORDS = 20
# この回に読んだ行のうち、知らない種類の割合がこれを超えたら形式の変化を疑う
DRIFT_UNKNOWN_SHARE = 0.3
# 割合で判断するのは、この回に読んだ行がこれ以上のときだけ。少ない回は、知らせを出しも消しもしない
# （セッションの終わりに管理用の行が数行だけ足された回などに、少ない行の割合で判断しないため）
DRIFT_MIN_NEW_RECORDS = 50

# 作り直し（full）で空にする表。tombstones と、meta のうち _KEEP_META は残す
_REBUILD_TABLES = ("sessions", "session_prs", "messages", "digests", "history_prompts", "documents",
                   "doc_links", "mentions", "derived_from", "lineage", "relations", "format_stats", "sources",
                   "places", "tasks", "task_sessions")
_KEEP_META = ("schema_version", "fts")


@dataclass
class UpdateReport:
    files_total: int
    files_ingested: int
    sessions_missing: int
    docs_upserted: int
    relations: int
    format_warning: Optional[str]
    seconds: float
    files_failed: int = 0
    source_warning: Optional[str] = None
    failed_paths: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def discover_transcripts(claude_dir: Path) -> List[Path]:
    """projects/<符号化した場所>/<ID>.jsonl だけを返す。<ID>/subagents/ などの中は読まない。"""
    return sorted(p for p in (Path(claude_dir) / "projects").glob("*/*.jsonl") if p.is_file())


def update(conn: sqlite3.Connection, cfg: Config, *, claude_dir: Path, ledger_dir: Path, home: str,
           now: Callable[[], float] = time.time, progress: Optional[Callable[[int, int], None]] = None,
           full: bool = False, tasks_root: Optional[Path] = None) -> UpdateReport:
    started = time.monotonic()
    tasks = None if tasks_root is None else str(tasks_root)
    # 記録のパスは台帳に残り、別の作業ディレクトリから読まれるので、絶対パスにしておく
    claude_dir, ledger_dir = Path(os.path.abspath(str(claude_dir))), Path(ledger_dir)
    with update_lock(ledger_dir):
        files = discover_transcripts(claude_dir)
        # 記録の場所が見えない回は、消えたとみなさず、作り直し（full）で台帳を空にもしない
        source_warning = _check_source(conn, claude_dir, files, full)
        if full and source_warning is None:
            _clear_ledger(conn)
        _reread_after_upgrade(conn)
        _reread_restored(conn, files)
        results, failed, failed_paths = _ingest_transcripts(conn, cfg, files, home, now, progress,
                                                                 tasks)
        missing = []  # type: List[str]
        if source_warning is None:
            missing = _sync_presence(conn, files, cfg.retention, claude_dir / "projects")
        apply_max_age(conn, cfg.max_age_days, now())
        places = _Places(conn, cfg, home, tasks)
        errors = _sync_history_and_lineage(conn, cfg, claude_dir, ledger_dir, places)
        _assign_places(conn, places)
        docs = _update_documents(conn, cfg, home, results, places, now(), tasks)
        relations = infer_relations(conn)
        if tasks is not None:
            sync = sync_tasks(conn, cfg, tasks_root=tasks, home=home, places=places)
            if sync.error:
                errors.append(sync.error)
            _relink_tasks(conn, cfg, tasks, changed=sync.changed or bool(results))
        warning = _check_format(conn, results)
        # 更新の途中で forget・purge が終わっていれば、この回が作り直したそのセッションの行を消す
        drop_forgotten(conn)
        with conn:
            set_meta(conn, "source_warning", source_warning or "")
            set_meta(conn, "last_update_at", iso_utc(now()))
    return UpdateReport(files_total=len(files), files_ingested=len(results), sessions_missing=len(missing),
                        docs_upserted=docs, relations=relations, format_warning=warning,
                        seconds=time.monotonic() - started, files_failed=failed, source_warning=source_warning,
                        failed_paths=failed_paths, errors=errors)


@contextmanager
def update_lock(ledger_dir: Path, on_wait: Optional[Callable[[], None]] = None) -> Iterator[None]:
    """更新と、台帳から消す操作（forget・purge）を1つずつ行う。tsuzuki index と serve の中の更新が同じ記録を
    同時に取り込むと、どちらも同じ位置から読んで行を二重に入れてしまう。更新の途中で消すと、更新が消した
    セッションを作り直してしまう。ほかが持っていれば on_wait を呼んでから、空くまで待つ。"""
    ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(ledger_dir / LOCK_FILENAME), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if on_wait is not None:
                on_wait()
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _clear_ledger(conn: sqlite3.Connection) -> None:
    with conn:
        for table in _REBUILD_TABLES:
            conn.execute("DELETE FROM %s" % table)
        conn.execute("DELETE FROM meta WHERE key NOT IN (?, ?)", _KEEP_META)


def _reread_after_upgrade(conn: sqlite3.Connection) -> None:
    """台帳の版を上げた後の最初の更新で、記録の残っているセッションを新しい読み方で頭から読み直させる。
    印は inode 0 で付け、読み終えていた位置（offset）は残す（取り込みは、そこまでの行を集計に数え直さない）。
    記録が消えたセッションの行には触れない（--full と違い、消えた記録の会話を失わない）。"""
    if get_meta(conn, REREAD_KEY) is None:
        return
    with conn:
        conn.execute("UPDATE sessions SET inode = 0 WHERE transcript_exists = 1")
        conn.execute("DELETE FROM meta WHERE key = ?", (REREAD_KEY,))


def _reread_restored(conn: sqlite3.Connection, files: Sequence[Path]) -> None:
    """記録が消えたとしていたセッションのファイルがまた見つかったら、頭から読み直させる。
    retention が card なら本文を消してあり、変わらないまま戻ったファイルは取り込みが読み飛ばすため。"""
    gone = {r[0] for r in conn.execute("SELECT id FROM sessions WHERE transcript_exists = 0")}
    back = [(p.stem,) for p in files if p.stem in gone]
    if back:
        with conn:
            # inode を 0 にしておくと、取り込みは作り直し（reset）として頭から読む。
            # 読めなかった回があっても、読めるまでこの印が残る
            conn.executemany("UPDATE sessions SET size = 0, inode = 0, offset = 0 WHERE id = ?", back)


def _ingest_transcripts(conn: sqlite3.Connection, cfg: Config, files: Sequence[Path], home: str,
                        now: Callable[[], float], progress: Optional[Callable[[int, int], None]],
                        tasks_root: Optional[str] = None) -> Tuple[List[IngestResult], int, List[str]]:
    """記録を1つずつ取り込み、(取り込んだ結果, 失敗したファイルの数, 失敗したパスの先頭の数件) を返す。
    1つのファイルの失敗で更新全体を止めない。"""
    ctx = IngestContext(home=home, exts=tuple(cfg.doc_extensions), tasks_root=tasks_root)
    # 期限より前に止まった記録は取り込まない（取り込んではすぐ消すのを、毎回くり返さないため）
    cutoff = None if cfg.max_age_days is None else now() - cfg.max_age_days * DAY_SECONDS
    results, failed, failed_paths = [], 0, []  # type: List[IngestResult], int, List[str]
    for done, path in enumerate(files, 1):
        try:
            if cutoff is None or path.stat().st_mtime >= cutoff:
                result = ingest_transcript(conn, path, ctx)
                if result is not None:
                    results.append(result)
        except Exception:
            failed += 1
            if len(failed_paths) < FAILED_PATHS_LIMIT:
                failed_paths.append(str(path))
        if progress is not None:
            progress(done, len(files))
    return results, failed, failed_paths


def _check_source(conn: sqlite3.Connection, claude_dir: Path, files: Sequence[Path], full: bool) -> Optional[str]:
    """記録の場所が見えないときは知らせの文を返す。そのときは記録が消えたとみなさず、作り直しもしない
    （外付けのディスクが外れているときなどに、台帳の中身を消してしまわないため）。"""
    projects = claude_dir / "projects"
    if not os.path.isdir(str(projects)):
        return SOURCE_MISSING % projects
    if not files and (full or conn.execute("SELECT 1 FROM sessions WHERE transcript_exists = 1 LIMIT 1").fetchone()):
        return SOURCE_EMPTY % projects
    return None


def _sync_presence(conn: sqlite3.Connection, files: Sequence[Path], retention: str, projects: Path) -> List[str]:
    """transcript_exists を今あるファイルに合わせ、この回に記録が消えたセッションに retention を当てる。
    消えたセッションの ID を返す。記録の有無の変更と retention の適用は1つのトランザクションにする
    （途中で止まっても、次の回に同じ変化を見つけてやり直せる）。"""
    found = {p.stem: str(p) for p in files}
    present, missing = [], []
    for sid, path, exists in conn.execute("SELECT id, transcript_path, transcript_exists FROM sessions").fetchall():
        now_path = found.get(sid)
        if now_path is not None:
            if now_path != path or not exists:
                # 変わらないまま戻った記録（取り込みは None を返す）や、移った記録
                present.append((now_path, sid))
        elif exists and _transcript_gone(path, projects):
            missing.append(sid)
    if present or missing:
        with conn:
            conn.executemany("UPDATE sessions SET transcript_path = ?, transcript_exists = 1 WHERE id = ?", present)
            conn.executemany("UPDATE sessions SET transcript_exists = 0 WHERE id = ?", [(s,) for s in missing])
            apply_missing(conn, retention, missing)
    return missing


def _transcript_gone(path: str, projects: Path) -> bool:
    """記録が消えたとみなせるか。ファイルがなく（FileNotFoundError）、記録の置き場（<claude_dir>/projects）は
    見えているときだけ消えたとみなす。場所ごとのディレクトリが消えていてもよい（Claude Code の片づけで消える）。
    読めない・ディスクの不調などのほかの失敗では消えたとみなさない。"""
    try:
        os.stat(path)
    except FileNotFoundError:
        return os.path.isdir(str(projects))
    except (OSError, ValueError):
        return False
    return False


class _Places:
    """1回の更新の中で、場所の判定（resolve_place）の結果と place_id を覚えておく。"""

    def __init__(self, conn: sqlite3.Connection, cfg: Config, home: str, tasks_root: Optional[str] = None):
        self.conn, self.home, self.tasks_root = conn, home, tasks_root
        self.aliases, self.strip = cfg.place_aliases, cfg.display_strip_prefixes
        self.refs = {}  # type: Dict[str, PlaceRef]
        self.ids = {}  # type: Dict[str, int]  台帳にある場所のパスと ID
        self._encoded = None  # type: Optional[Dict[str, int]]
        self._anchors = {}  # type: Dict[str, Optional[str]]
        renamed = []
        for pid, path, display in conn.execute("SELECT id, path, display FROM places").fetchall():
            self.ids[path] = pid
            want = display_name(path, home=home, strip_prefixes=self.strip, tasks_root=tasks_root)
            if want != display:
                renamed.append((want, pid))
        if renamed:  # 設定 display_strip_prefixes が変わったとき
            with conn:
                conn.executemany("UPDATE places SET display = ? WHERE id = ?", renamed)

    def ref(self, directory: str) -> PlaceRef:
        found = self.refs.get(directory)
        if found is None:
            found = self.refs[directory] = resolve_place(directory, aliases=self.aliases, tasks_root=self.tasks_root)
        return found

    def id_for(self, root: str) -> int:
        """場所の ID。台帳になければ作る。"""
        pid = self.ids.get(root)
        if pid is None:
            pid = self.ids[root] = place_id_for(self.conn, root, home=self.home, strip_prefixes=self.strip,
                                                tasks_root=self.tasks_root)
            self._encoded = None
        return pid

    def by_encoded(self, encoded: Optional[str]) -> Optional[int]:
        """符号化した名前（encode_project）が一致する既知の場所。重なれば先に作った場所。"""
        if self._encoded is None:
            self._encoded = {}
            for path, pid in sorted(self.ids.items(), key=lambda item: item[1]):
                if path != TMP_PLACE:
                    self._encoded.setdefault(encode_project(path), pid)
        return self._encoded.get(encoded or "")

    def for_history(self, project: Optional[str]) -> Optional[int]:
        if not project or not os.path.isabs(project):
            return None
        root = self.ref(project).path
        return self.id_for(root) if root else None  # 一時ディレクトリは場所なし

    def for_document(self, path: str, *, written: bool = False) -> Optional[int]:
        """資料の場所。触れた資料にも走査で見つけた資料にも同じ規則を使う。
        A：資料のディレクトリの、Git（.git がある）かエイリアスで決まる根。Git の外のディレクトリには根がない。
        P：資料を含む既存の場所のうち、いちばん深いもの。
        A があって P が A の中にないときは、A がすでに場所ならそこ（エイリアスの行き先や、別のディレクトリにある
        worktree の元のリポジトリのように、A が資料の上にないとき）。まだ場所でなければ、セッションが書いた資料
        （written）なら A を作ってそこに、そうでなければ場所なし。それ以外は P（なければ場所なし）。
        走査では written を渡さない。"""
        claude = os.path.join(self.home, ".claude") + "/"
        if path.startswith(claude):
            parts = path[len(claude):].split("/")
            if len(parts) > 3 and parts[0] == "projects" and parts[2] == "memory":
                return self.by_encoded(parts[1])
            return None  # 計画（plans）など
        directory = os.path.dirname(path)
        if not self.ref(directory).path:
            return None  # 一時ディレクトリ
        anchor = self._anchor(directory)
        deepest = self._deepest_place(directory)
        if anchor is not None and not (deepest is not None and _within(deepest, anchor)):
            if anchor in self.ids:
                return self.ids[anchor]
            return self.id_for(anchor) if written else None
        return None if deepest is None else self.ids[deepest]

    def _anchor(self, directory: str) -> Optional[str]:
        """Git（.git がディレクトリかファイル）かエイリアスで決まる根。Git の外のディレクトリは
        resolve_place がそのディレクトリ自身を返すが、それは根とみなさない。"""
        if directory not in self._anchors:
            root = self.ref(directory).path
            anchored = bool(root) and (self._aliased(directory) or os.path.exists(os.path.join(root, ".git")))
            self._anchors[directory] = root if anchored else None
        return self._anchors[directory]

    def _aliased(self, directory: str) -> bool:
        """設定 place_aliases のどれかに当たるか（resolve_place の規則1と同じ当て方）。"""
        d = os.path.normpath(directory)
        for key in self.aliases:
            k = os.path.normpath(os.path.expanduser(key))
            if d == k or d.startswith(k.rstrip("/") + "/"):
                return True
        return False

    def _deepest_place(self, directory: str) -> Optional[str]:
        """directory と同じか、それを含む既存の場所のうち、いちばん深いもののパス。"""
        d = directory
        while True:
            if d in self.ids:
                return d
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent


def _within(path: str, root: str) -> bool:
    """path が root と同じか、その下にあるか（パスの区切りで比べる）。"""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _sync_history_and_lineage(conn: sqlite3.Connection, cfg: Config, claude_dir: Path, ledger_dir: Path,
                              places: _Places) -> List[str]:
    """入力履歴とフックの記録を取り込み、失敗の文を返す。1つの源の失敗で更新全体を止めない
    （取り込みは源ごとに巻き戻るので、次の回に同じ位置から読み直す）。"""
    errors = []  # type: List[str]
    if cfg.index_history:
        history = claude_dir / "history.jsonl"
        try:
            tombstoned = {r[0] for r in conn.execute("SELECT session_id FROM tombstones")}
            sync_history(conn, history, tombstoned=tombstoned, place_of=places.for_history)
        except Exception as e:
            errors.append(HISTORY_ERROR % (history, e))
    else:
        _drop_history(conn)
    lineage = ledger_dir / "lineage.jsonl"
    try:
        sync_lineage(conn, lineage)
    except Exception as e:
        errors.append(LINEAGE_ERROR % (lineage, e))
    return errors


def _drop_history(conn: sqlite3.Connection) -> None:
    """設定 index_history を切ったら、入れてあった入力履歴も索引から外す。"""
    if (conn.execute("SELECT 1 FROM history_prompts LIMIT 1").fetchone()
            or conn.execute("SELECT 1 FROM sources WHERE name = 'history'").fetchone()):
        with conn:
            conn.execute("DELETE FROM history_prompts")
            conn.execute("DELETE FROM sources WHERE name = 'history'")


def _assign_places(conn: sqlite3.Connection, places: _Places) -> None:
    """場所の決まっていないセッションと、「一時」のセッションの場所を決める。
    一度決めた場所は変えない（ディレクトリが消えても前回の場所を使う）。"""
    tmp_id = places.ids.get(TMP_PLACE)
    stored = {}  # type: Dict[str, Optional[int]]
    todo = []
    for sid, cwd, pid in conn.execute("SELECT id, cwd, place_id FROM sessions").fetchall():
        stored[sid] = pid
        if (pid is None or pid == tmp_id) and cwd and os.path.isabs(cwd):
            todo.append((sid, places.ref(cwd)))
    chosen = dict(stored)
    waiting = []
    for sid, ref in todo:
        if ref.path:
            chosen[sid] = places.id_for(ref.path)
        elif ref.tmp_session:
            waiting.append((sid, ref))
    # 一時ディレクトリ：持ち主のセッションの場所 → 符号化した名前が一致する既知の場所 → 「一時」。
    # 持ち主も一時ディレクトリのセッションでありうるので、決まらなくなるまでくり返す
    while waiting:
        rest = []
        for sid, ref in waiting:
            owner = chosen.get(ref.tmp_session)
            if owner is not None and owner != tmp_id:
                chosen[sid] = owner
            else:
                rest.append((sid, ref))
        if len(rest) == len(waiting):
            break
        waiting = rest
    for sid, ref in waiting:
        pid = places.by_encoded(ref.tmp_encoded)
        chosen[sid] = pid if pid is not None else places.id_for(TMP_PLACE)
    changed = [(pid, sid) for sid, pid in chosen.items() if pid != stored[sid]]
    if changed:
        with conn:
            conn.executemany("UPDATE sessions SET place_id = ? WHERE id = ?", changed)


def _update_documents(conn: sqlite3.Connection, cfg: Config, home: str, results: Sequence[IngestResult],
                      places: _Places, now: float, tasks_root: Optional[str] = None) -> int:
    """この回に触れた資料と、走査の時期が来た場所の資料を台帳に入れ、入れた数を返す。"""
    exts = cfg.doc_extensions
    touched = set()  # type: Set[str]
    for r in results:
        touched |= r.touched_docs
    upserted = sum(_upsert_document(conn, path, places, home, exts, written=_was_written(conn, path),
                                  tasks_root=tasks_root)
                   for path in sorted(touched))
    scan = cfg.doc_scan
    if not scan.enabled:
        return upserted
    active = _places_of(conn, {r.session_id for r in results})
    for root, pid in sorted(places.ids.items(), key=lambda item: item[1]):
        if not (os.path.isabs(root) and os.path.isdir(root) and should_scan_place(root, home)):
            continue
        if tasks_root is not None and task_within(root, os.path.normpath(tasks_root)):
            continue  # タスクの置き場所の資料は sync_tasks が受け持つ
        key = "scan:%d" % pid
        last = _scanned_at(conn, key)
        # 前回の走査から間が空いたときと、その場所のセッションが動いたときに走査する
        if pid not in active and last is not None and now - last < scan.interval_seconds:
            continue
        for path in scan_documents(root, exts=exts, exclude_dirs=scan.exclude_dirs, max_depth=scan.max_depth,
                                   max_files=scan.max_files):
            upserted += _upsert_document(conn, path, places, home, exts, tasks_root=tasks_root)  # 走査では場所を作らない
        _drop_vanished_documents(conn, pid)
        with conn:
            set_meta(conn, key, str(now))
    return upserted


def _upsert_document(conn: sqlite3.Connection, path: str, places: _Places, home: str, exts: Sequence[str], *,
                     written: bool = False, tasks_root: Optional[str] = None) -> int:
    """1つの資料を台帳に入れ、入れたら1を返す。扱えないパス（NUL を含むなど）の1件で更新を止めない。"""
    try:
        place_id = places.for_document(path, written=written)
        doc_id = upsert_document(conn, path, place_id=place_id, home=home, exts=exts, tasks_root=tasks_root)
    except (OSError, ValueError):
        return 0
    return 0 if doc_id is None else 1


def _was_written(conn: sqlite3.Connection, path: str) -> bool:
    """どれかのセッションが作った・直した資料か（読んだ・名指ししただけの資料は場所を作らない）。"""
    return conn.execute("SELECT 1 FROM doc_links WHERE path = ? AND rel IN ('created', 'edited') LIMIT 1",
                        (path,)).fetchone() is not None


def _places_of(conn: sqlite3.Connection, session_ids: Set[str]) -> Set[int]:
    if not session_ids:
        return set()
    return {pid for sid, pid in conn.execute("SELECT id, place_id FROM sessions")
            if sid in session_ids and pid is not None}


def _scanned_at(conn: sqlite3.Connection, key: str) -> Optional[float]:
    raw = get_meta(conn, key)
    try:
        return None if raw is None else float(raw)
    except ValueError:
        return None


def _drop_vanished_documents(conn: sqlite3.Connection, place_id: int) -> None:
    rows = conn.execute("SELECT id, path FROM documents WHERE place_id = ?", (place_id,)).fetchall()
    gone = [(doc_id,) for doc_id, path in rows if not os.path.isfile(path)]
    if gone:
        with conn:
            conn.executemany("DELETE FROM documents WHERE id = ?", gone)


def _relink_tasks(conn: sqlite3.Connection, cfg: Config, tasks_root: str, *, changed: bool) -> None:
    """タスクかセッションが変わった回（changed）と、手で足した・外した後（meta の印）だけ、セッションとタスクの
    結び付けを計算し直す。差分の更新を軽く保つため、何も変わらない回は計算しない。"""
    if not (changed or get_meta(conn, RELINK_KEY) == "1"):
        return
    link_tasks(conn, tasks_root=tasks_root, default_branches=cfg.default_branches)
    with conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (RELINK_KEY,))


def _check_format(conn: sqlite3.Connection, results: Sequence[IngestResult]) -> Optional[str]:
    """形式の変化を調べ、知らせの文を meta.format_warning に残す。知らせがなければ None。"""
    if not results:
        # この回に読んだ記録がなければ判断の材料がないので、前の知らせをそのまま残す
        return get_meta(conn, "format_warning") or None
    unreadable = 0
    for r in results:
        row = conn.execute("SELECT n_records, n_recognized FROM sessions WHERE id = ?", (r.session_id,)).fetchone()
        if row is not None and row[0] >= DRIFT_MIN_RECORDS and row[1] == 0:
            unreadable += 1
    new = sum(r.new_records for r in results)
    share = sum(r.unknown_records for r in results) / new if new else 0.0
    if unreadable:
        warning = "%s（%d件のファイルで会話を読み取れませんでした）" % (FORMAT_WARNING, unreadable)
    elif new < DRIFT_MIN_NEW_RECORDS:
        # 読んだ行が少なく判断できないので、何も読まなかった回と同じく前の知らせを残す
        return get_meta(conn, "format_warning") or None
    elif share > DRIFT_UNKNOWN_SHARE:
        warning = "%s（知らない種類の記録が%d%%）" % (FORMAT_WARNING, round(share * 100))
    else:
        warning = ""
    with conn:
        set_meta(conn, "format_warning", warning)
    return warning or None
