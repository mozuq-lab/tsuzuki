"""タスクの新しい会話を始めるフォルダーと、その Git リポジトリの根。標準のライブラリだけ（外のコマンドは呼ばない）。"""
import os
from typing import Callable, Optional


def start_dir(task_dir: str, repo: Optional[str], workspace: Optional[str], *,
              isdir: Callable[[str], bool] = os.path.isdir) -> str:
    """タスクの新しい会話を始めるフォルダー。ワークスペースがあればそのあるフォルダー、なければフォルダーである
    リポジトリ、それもなければタスクのフォルダー。"""
    if workspace:
        return os.path.dirname(workspace)
    if repo and isdir(repo):
        return repo
    return task_dir


def git_root(path: str) -> Optional[str]:
    """path かその親で、.git（フォルダー。ワークツリーではファイル）のある一番近いフォルダー。なければ None。"""
    current = os.path.realpath(path)  # リンクの先のリポジトリの .git も見つける
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
