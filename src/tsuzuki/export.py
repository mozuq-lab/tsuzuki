"""導入物の雛形を書き出す（tsuzuki templates）。

雛形はパッケージの中に持ち、pkgutil で読む（1つの実行ファイル〔zipapp〕の中からも読める）。
Tsuzuki は書き出すだけで、決まった場所に置くのは利用者である。
"""
import errno
import os
import pkgutil
from pathlib import Path
from typing import List, Optional, Tuple

# （パッケージの中の雛形、出力先での相対パス）。書き出しも、置く場所の案内も、この順に行う
TEMPLATES: Tuple[Tuple[str, str], ...] = (
    ("templates/tasks-README.md", "tasks/README.md"),
    ("templates/claude-md-snippet.md", "CLAUDE.md-snippet.md"),
    ("templates/skills/task-start/SKILL.md", "skills/task-start/SKILL.md"),
    ("templates/skills/task-handoff/SKILL.md", "skills/task-handoff/SKILL.md"),
)
TASKS_ROOT_IN_TEMPLATES = "~/tasks"  # 雛形の中のタスクの置き場所。書き出すときに設定の置き場所に置き換える
_SNIPPET = "CLAUDE.md-snippet.md"
_TASKS_DIR = "tasks/"


def display_path(path: Path, home: str) -> str:
    """ホームの下なら ~/… の形、ほかは絶対パス（シンボリックリンクは解かない）。"""
    p = os.path.normpath(os.path.abspath(str(path)))
    h = os.path.normpath(home).rstrip("/")
    if p.startswith(h + "/"):
        return "~/" + p[len(h) + 1:]
    return p


def template_text(name: str, *, tasks_root_display: str) -> str:
    data = pkgutil.get_data("tsuzuki", name)
    if data is None:  # get_data を持たない読み込み方。Tsuzuki の配り方（ファイルと zipapp）では起きない
        raise FileNotFoundError("雛形を読めません: %s" % name)
    return data.decode("utf-8").replace(TASKS_ROOT_IN_TEMPLATES, tasks_root_display)


def placement(rel: str, *, tasks_root_display: str, claude_dir_display: str) -> str:
    """出力先での相対パス rel のファイルを、利用者がどこに置くか。"""
    if rel == _SNIPPET:
        return "中身を %s/CLAUDE.md に足す" % claude_dir_display
    if rel.startswith(_TASKS_DIR):
        return "%s/%s に置く" % (tasks_root_display, rel[len(_TASKS_DIR):])
    return "%s/%s に置く" % (claude_dir_display, rel)


def _in_the_way(out_dir: Path, rel: str) -> Optional[Path]:
    """出力先での相対パス rel に書くのを妨げる物。rel そのもの（壊れたシンボリックリンクも含む）か、
    out_dir から rel までの途中にある、フォルダーでない物。なければ None。"""
    path = out_dir / rel
    if os.path.lexists(str(path)):
        return path
    folder = out_dir
    for part in ("",) + Path(rel).parent.parts:
        folder = folder / part
        if os.path.lexists(str(folder)) and not folder.is_dir():
            return folder
    return None


def export_templates(out_dir: Path, *, tasks_root_display: str) -> List[Path]:
    """雛形を out_dir の下に書き出し、書いたパスを TEMPLATES の順に返す。1つでも同じ名前の物があれば、
    何も書かずに FileExistsError。途中で書けなくなったときは、書いた分を消してから例外を返す。"""
    out_dir = Path(out_dir)
    for _, rel in TEMPLATES:
        blocker = _in_the_way(out_dir, rel)
        if blocker is not None:
            raise FileExistsError(errno.EEXIST, "同じ名前のファイルがあります", str(blocker))
    texts = [template_text(name, tasks_root_display=tasks_root_display) for name, _ in TEMPLATES]
    written = []  # type: List[Path]
    try:
        for (_, rel), text in zip(TEMPLATES, texts):
            path = out_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(path), "x", encoding="utf-8", newline="") as f:  # "x"：あれば書かない
                written.append(path)
                f.write(text)
    except BaseException:
        for path in written:
            try:
                os.unlink(str(path))
            except OSError:
                pass
        raise
    return written
