"""実行中のセッション（~/.claude/sessions/<pid>.json）を読む。読むだけで書かない。"""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict


@dataclass
class LiveSession:
    pid: int
    session_id: str
    cwd: str
    status: str


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_sessions(claude_dir: Path, *, alive: Callable[[int], bool] = pid_alive) -> Dict[str, LiveSession]:
    result = {}  # type: Dict[str, LiveSession]
    d = Path(claude_dir) / "sessions"
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return result
    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        pid, session = rec.get("pid"), rec.get("sessionId")
        if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(session, str):
            continue
        if session in result or not alive(pid):
            continue
        cwd, status = rec.get("cwd"), rec.get("status")
        result[session] = LiveSession(pid, session, cwd if isinstance(cwd, str) else "",
                                      status if isinstance(status, str) else "")
    return result
