"""場所の判定。作業ディレクトリを、たいてい git の根の「場所」に解く。"""
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from tsuzuki.tasks import within

TMP_PLACE = "(tmp)"
TASKS_LABEL = "タスク"
SHARED_LABEL = "共通の資料"

TMP_RE = re.compile(
    r"^(?:/private)?/tmp/claude-\d+/(?P<enc>[^/]+)/"
    r"(?P<sid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)")
_WT = "/.claude/worktrees/"


@dataclass
class PlaceRef:
    path: Optional[str]
    tmp_session: Optional[str] = None
    tmp_encoded: Optional[str] = None


def encode_project(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _norm(p: str) -> str:
    return os.path.normpath(os.path.expanduser(p))


def _default_read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


def _git_root_here(d: str, exists, isdir, read_text) -> Optional[str]:
    """d に .git があれば場所を返す。なければ None。"""
    g = d.rstrip("/") + "/.git"
    try:
        if not exists(g):
            return None
        if isdir(g):
            return d
        for line in read_text(g).splitlines():
            if line.startswith("gitdir:"):
                gd = line[len("gitdir:"):].strip()
                if not os.path.isabs(gd):
                    gd = os.path.normpath(os.path.join(d, gd))
                i = gd.find("/.git/worktrees/")
                return gd[:i] if i >= 0 else d
    except Exception:
        return None
    return None


def resolve_place(cwd: str, *, aliases: Dict[str, str], tasks_root: Optional[str] = None, exists=os.path.exists,
                  isdir=os.path.isdir, read_text=_default_read) -> PlaceRef:
    """空文字の cwd は PlaceRef(path="") を返す。例外は出さない。"""
    if not cwd:
        return PlaceRef(path="")
    cwd = os.path.normpath(cwd)
    best = None
    for k, v in aliases.items():
        nk = _norm(k)
        if cwd == nk or cwd.startswith(nk.rstrip("/") + "/"):
            if best is None or len(nk) > len(best[0]):
                best = (nk, _norm(v))
    if best:
        return PlaceRef(path=best[1])
    if tasks_root and within(cwd, os.path.normpath(tasks_root)):  # タスクの置き場所の中は、どこでも置き場所そのものを場所にする
        return PlaceRef(path=os.path.normpath(tasks_root))
    i = cwd.find(_WT)
    if i > 0:
        return PlaceRef(path=cwd[:i])
    m = TMP_RE.match(cwd)
    if m:
        return PlaceRef(path=None, tmp_session=m.group("sid"), tmp_encoded=m.group("enc"))
    d = cwd
    while True:
        r = _git_root_here(d, exists, isdir, read_text)
        if r:
            return PlaceRef(path=r)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return PlaceRef(path=cwd)


def display_name(root: str, *, home: str, strip_prefixes: Sequence[str], tasks_root: Optional[str] = None) -> str:
    if root == TMP_PLACE:
        return "一時"
    if tasks_root:
        tr = os.path.normpath(tasks_root)
        if root == tr:
            return TASKS_LABEL
        if root == tr.rstrip("/") + "/shared":
            return SHARED_LABEL
    for p in sorted(strip_prefixes, key=len, reverse=True):
        if not p:
            continue
        p = p if p.endswith("/") else p + "/"
        if root.startswith(p):
            return root[len(p):]
    if root == home:
        return "~"
    if root.startswith(home.rstrip("/") + "/"):
        return "~/" + root[len(home.rstrip("/")) + 1:]
    return root


def short_labels(displays: Iterable[str]) -> Dict[str, str]:
    """表示名ごとの短い名前（画面の狭い所に出す）。表示名の最後の要素を、ほかの表示名と重ならなくなるまで
    親の要素で前に延ばす（エディタのタブの名前と同じ）。親をたどり切っても重なれば、表示名そのもの。
    displays には台帳のすべての場所の表示名を渡す（同じ表示名は1つにまとまる）。"""
    parts = {d: [p for p in d.split("/") if p] for d in displays}
    counts = {}  # type: Dict[Tuple[int, str], int]  (末尾の要素の数, 末尾) → その末尾を持つ表示名の数
    for comps in parts.values():
        for k in range(1, len(comps) + 1):
            key = (k, "/".join(comps[-k:]))
            counts[key] = counts.get(key, 0) + 1
    return {d: _short_label(d, comps, counts) for d, comps in parts.items()}


def _short_label(display: str, comps: List[str], counts: Dict[Tuple[int, str], int]) -> str:
    for k in range(1, len(comps)):
        tail = "/".join(comps[-k:])
        if counts[(k, tail)] == 1:
            return tail
    return display


def place_id_for(conn, root: str, *, home: str, strip_prefixes: Sequence[str], tasks_root: Optional[str] = None) -> int:
    disp = display_name(root, home=home, strip_prefixes=strip_prefixes, tasks_root=tasks_root)
    with conn:
        row = conn.execute("SELECT id, display FROM places WHERE path=?", (root,)).fetchone()
        if row is None:
            return conn.execute("INSERT INTO places(path, display) VALUES(?, ?)", (root, disp)).lastrowid
        if row[1] != disp:
            conn.execute("UPDATE places SET display=? WHERE id=?", (disp, row[0]))
        return row[0]
