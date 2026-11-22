"""SessionStart フック。親のセッションを lineage.jsonl に1行足す。

全セッションの開始で走るので、速く・黙って・必ず 0 で終わる。
起動を軽くするため sqlite3 を読み込む module（store・retention・sources.lineage）は import しない。
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

# tsuzuki.sources.lineage.LINEAGE_FIELDS と同じ（そちらは sqlite3 を読むので写して持ち、テストで揃いを確かめる）
LINEAGE_FIELDS = ("ts", "session_id", "parent_id", "parent_pid", "source", "cwd", "entrypoint")
LINEAGE_FILENAME = "lineage.jsonl"
STDIN_LIMIT = 1024 * 1024


def iso_utc(ts: float) -> str:
    """tsuzuki.retention.iso_utc と同じ形（UTC の ISO、ミリ秒と Z 付き）。"""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def ppid_table(runner=subprocess.run) -> Dict[int, int]:
    """ps を1回だけ呼んで {pid: ppid} の表にする。失敗したら空。"""
    try:
        out = runner(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=2).stdout
        table = {}  # type: Dict[int, int]
        for line in out.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                table[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
        return table
    except Exception:
        return {}


def _session_id_of(sessions_dir: Path, pid: int) -> Optional[str]:
    try:
        data = json.loads((sessions_dir / ("%d.json" % pid)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sid = data.get("sessionId") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) else None


def find_parent_session(start_pid: int, own_session_id: str, *, table: Dict[int, int], sessions_dir: Path,
                        max_depth: int = 12) -> Tuple[Optional[str], Optional[int]]:
    """start_pid から親プロセスをたどり、自分とは別のセッションを持つ最初の claude を探す。"""
    pid = start_pid
    seen = set()
    for _ in range(max_depth):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        found = _session_id_of(sessions_dir, pid)
        if found is not None and found != own_session_id:
            return found, pid
        parent = table.get(pid)
        if parent is None:
            break
        pid = parent
    return None, None


def run_session_start(stdin_text: str, *, claude_dir: Path, ledger_dir: Path, start_pid: int,
                      env: Mapping[str, str], table_fn=ppid_table, now=time.time) -> int:
    """常に 0 を返す。標準出力には何も書かない。"""
    try:
        data = json.loads(stdin_text)
        session_id = data.get("session_id") if isinstance(data, dict) else None
        if not isinstance(session_id, str) or not session_id:
            return 0
        try:
            parent_id, parent_pid = find_parent_session(
                start_pid, session_id, table=table_fn(), sessions_dir=claude_dir / "sessions")
        except Exception:
            parent_id, parent_pid = None, None  # 親が分からなくても、自分の行は残す
        values = {"ts": iso_utc(now()), "session_id": session_id, "parent_id": parent_id,
                  "parent_pid": parent_pid, "source": data.get("source"), "cwd": data.get("cwd"),
                  "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT")}
        line = json.dumps({k: values[k] for k in LINEAGE_FIELDS}, ensure_ascii=False) + "\n"
        ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(ledger_dir / LINEAGE_FILENAME), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except BaseException:
        pass
    return 0
