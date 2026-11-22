"""合成した記録とテストの補助。本物の記録は使わない。"""
import contextlib
from unittest import mock
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from tsuzuki import cli
from tsuzuki.places import encode_project
from tsuzuki.store import open_ledger

_BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def t(minutes: float) -> str:
    d = _BASE + timedelta(minutes=minutes)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def sid(n: int) -> str:
    return "00000000-0000-4000-8000-%012d" % n


_counter = [0]


def _uuid() -> str:
    _counter[0] += 1
    return "11111111-0000-4000-8000-%012d" % _counter[0]


def _common(ts, session, cwd, entrypoint, version, branch) -> dict:
    return {"userType": "external", "entrypoint": entrypoint, "cwd": cwd, "sessionId": session,
            "version": version, "gitBranch": branch}


def user(text, ts, session, cwd, *, human=True, meta=False, entrypoint="cli", version="2.1.330",
         branch="main", forked_from=None, context_block: Optional[str] = None,
         prompt_source: Optional[str] = None) -> dict:
    if context_block is not None:
        content = [{"type": "text", "text": context_block}, {"type": "text", "text": text}]
    else:
        content = text
    rec = {"parentUuid": None, "isSidechain": False, "type": "user",
           "message": {"role": "user", "content": content},
           "uuid": _uuid(), "timestamp": ts}
    rec.update(_common(ts, session, cwd, entrypoint, version, branch))
    if human:
        rec["promptSource"] = prompt_source or "typed"
        rec["origin"] = {"kind": "human"}
    if meta:
        rec["isMeta"] = True
    if forked_from:
        rec["forkedFrom"] = {"sessionId": forked_from, "messageUuid": _uuid()}
    return rec


def tool_result(text, ts, session, cwd, *, entrypoint="cli", version="2.1.330", branch="main") -> dict:
    rec = {"parentUuid": _uuid(), "isSidechain": False, "type": "user",
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": "toolu_%024d" % _counter[0], "content": text}]},
           "uuid": _uuid(), "timestamp": ts}
    rec.update(_common(ts, session, cwd, entrypoint, version, branch))
    rec["toolUseResult"] = {"stdout": text}
    return rec


def assistant(ts, session, cwd, *, text=None, tools=(), entrypoint="cli", version="2.1.330",
              branch="main") -> dict:
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for name, inp in tools:
        content.append({"type": "tool_use", "id": "toolu_%024d" % _counter[0], "name": name, "input": inp})
    rec = {"parentUuid": _uuid(), "isSidechain": False, "type": "assistant",
           "message": {"id": "msg_%024d" % _counter[0], "type": "message", "role": "assistant",
                       "content": content},
           "uuid": _uuid(), "timestamp": ts}
    rec.update(_common(ts, session, cwd, entrypoint, version, branch))
    return rec


def recap(text, ts, session, cwd, *, entrypoint="cli", version="2.1.330", branch="main") -> dict:
    rec = {"parentUuid": _uuid(), "type": "system", "subtype": "away_summary", "content": text,
           "timestamp": ts, "cwd": cwd, "sessionId": session}
    rec.update({"entrypoint": entrypoint, "version": version, "gitBranch": branch})
    return rec


def title(kind: str, text: str, session: str) -> dict:
    if kind == "ai":
        return {"type": "ai-title", "aiTitle": text, "sessionId": session}
    if kind == "custom":
        return {"type": "custom-title", "customTitle": text, "sessionId": session}
    return {"type": "agent-name", "agentName": text, "sessionId": session}


def continued_in(session: str, next_session: str, ts: str) -> dict:
    return {"type": "continued-in", "timestamp": ts, "sessionId": session, "continuedInSessionId": next_session}


def pr_link(session: str, url: str, ts: str) -> dict:
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    return {"type": "pr-link", "sessionId": session, "prNumber": int(m.group(2)) if m else 0,
            "prUrl": url, "prRepository": m.group(1) if m else "", "timestamp": ts}


def relocated(session: str, cwd: str) -> dict:
    return {"type": "relocated", "sessionId": session, "relocatedCwd": cwd}


def api_request(session: str, size: int = 1000) -> dict:
    return {"type": "api-request", "base": "x" * size, "sessionId": session}


def pasted(text: str, paste_id: str = "1") -> str:
    """本人が貼り付けた内容（Claude Code が入力の本文に入れるタグの形）。"""
    return '<pasted_content id="%s">\n%s\n</pasted_content id="%s">' % (paste_id, text, paste_id)


def command_text(name: str, args: Optional[str] = None, *, local: bool = False) -> str:
    """スラッシュコマンドの記録の本文。local は Claude Code の中だけで済むコマンドの形（名前が先に来る）。"""
    if local:
        return ("<command-name>/%s</command-name>\n            <command-message>%s</command-message>\n"
                "            <command-args>%s</command-args>" % (name, name, args or ""))
    text = "<command-message>%s</command-message>\n<command-name>/%s</command-name>" % (name, name)
    return text + ("\n<command-args>%s</command-args>" % args if args else "")


def local_output(text: str, ts: str, session: str, cwd: str, *, stream: str = "stdout", **kw) -> dict:
    """ローカルのコマンドの出力の行。"""
    return user("<local-command-%s>%s</local-command-%s>" % (stream, text, stream), ts, session, cwd,
                human=False, **kw)


# 実データの形（2026-11-22 の確認）。対話で打ったスキルのコマンドには origin.kind と turnOrigin が付き、
# promptSource は付かない。Claude Code の中だけで済むコマンドの行には、本人の入力の印が付かない

def skill_command(name: str, ts: str, session: str, cwd: str, args: Optional[str] = None, **kw) -> dict:
    rec = user(command_text(name, args), ts, session, cwd, human=False, **kw)
    rec.update(origin={"kind": "human"}, turnOrigin="human", turnPosition={"promptIndex": 1, "turnIndex": 1})
    return rec


def local_command(name: str, ts: str, session: str, cwd: str, args: Optional[str] = None, **kw) -> dict:
    return user(command_text(name, args, local=True), ts, session, cwd, human=False, **kw)


def skill_tool_use(skill: str, ts: str, session: str, cwd: str) -> List[dict]:
    """Claude が Skill ツールでスキルを使ったときの行（コマンドの行は書かれない）。"""
    call = assistant(ts, session, cwd, tools=[("Skill", {"skill": skill})])
    result = tool_result("Launching skill: %s" % skill, ts, session, cwd)
    content = user("Base directory for this skill: /x\n\n手順", ts, session, cwd, human=False, meta=True)
    return [call, result, content]


def derived_from(session: str, sources) -> dict:
    """記録の初めに書かれる元のセッションの記録。sources は (ID, via) の並び。
    実データでは subprocess の項目に started（このセッション自身の ID）が付く。"""
    entries = []
    for s, via in sources:
        entry = {"type": "claude_code_session", "id": s, "via": via}
        if via == "subprocess":
            entry["started"] = session
        entries.append(entry)
    return {"type": "derived-from", "v": 1, "sessionId": session, "sessions": entries}


def system_local_command(content: str, ts: str, session: str, cwd: str) -> dict:
    """新しい版で、ローカルのコマンドとその出力を system の行に書いたもの（本人の入力ではない）。"""
    return {"type": "system", "subtype": "local_command", "content": content, "level": "info", "timestamp": ts,
            "uuid": _uuid(), "isMeta": False, "sessionId": session, "cwd": cwd, "version": "2.1.330"}


# 版1の台帳の sessions の表（版を上げたときの移行を確かめるのに使う）
V1_SESSIONS = """CREATE TABLE sessions (
  id TEXT PRIMARY KEY, transcript_path TEXT NOT NULL, transcript_exists INTEGER NOT NULL DEFAULT 1,
  size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0, inode INTEGER NOT NULL DEFAULT 0,
  offset INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'empty',
  entrypoint TEXT, version TEXT, cwd TEXT, relocated_cwd TEXT, place_id INTEGER, branch TEXT,
  started_at TEXT, ended_at TEXT,
  custom_title TEXT, agent_name TEXT, ai_title TEXT,
  first_prompt TEXT, last_prompt TEXT,
  n_prompts INTEGER NOT NULL DEFAULT 0, n_agents INTEGER NOT NULL DEFAULT 0,
  recap TEXT, recap_at TEXT, last_reply TEXT,
  forked_from TEXT, continued_in TEXT,
  turn INTEGER NOT NULL DEFAULT 0, next_seq INTEGER NOT NULL DEFAULT 0,
  n_records INTEGER NOT NULL DEFAULT 0, n_recognized INTEGER NOT NULL DEFAULT 0
)"""


def downgrade_to_v1(conn: sqlite3.Connection) -> None:
    """今の版の台帳を版1の形に戻す（sessions に後から足した列がなく、derived_from の表もない）。行は残す。
    読み直しの印も消す（版1の台帳にはない）。"""
    with conn:
        conn.execute("ALTER TABLE sessions RENAME TO sessions_new")
        conn.execute(V1_SESSIONS)
        cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(sessions)"))
        conn.execute("INSERT INTO sessions(%s) SELECT %s FROM sessions_new" % (cols, cols))
        conn.execute("DROP TABLE sessions_new")
        conn.execute("DROP TABLE IF EXISTS derived_from")
        conn.execute("DELETE FROM meta WHERE key = 'reread_transcripts'")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")


def downgrade_to_v2(conn: sqlite3.Connection) -> None:
    """今の版の台帳を版2の形に戻す（タスクの3つの表と索引を消す）。ほかの行は残す。"""
    with conn:
        conn.execute("DROP INDEX IF EXISTS task_sessions_session")
        for table in ("tasks", "task_sessions", "task_overrides"):
            conn.execute("DROP TABLE IF EXISTS %s" % table)
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")


def _dumps(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))


class FakeClaude:
    """偽の ~/.claude。"""

    def __init__(self, root: Path):
        self.dir = Path(root) / "claude"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, cwd: str, session: str) -> Path:
        d = self.dir / "projects" / encode_project(cwd)
        d.mkdir(parents=True, exist_ok=True)
        return d / (session + ".jsonl")

    def write(self, cwd, session, records, *, partial_tail: str = "") -> Path:
        p = self._path(cwd, session)
        with open(p, "w", encoding="utf-8") as f:
            for r in records:
                f.write(_dumps(r) + "\n")
            f.write(partial_tail)
        return p

    def append(self, cwd, session, records) -> Path:
        p = self._path(cwd, session)
        with open(p, "a", encoding="utf-8") as f:
            for r in records:
                f.write(_dumps(r) + "\n")
        return p

    def history(self, rows: List[dict]) -> Path:
        p = self.dir / "history.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(_dumps(r) + "\n")
        return p

    def live(self, pid, session, cwd, status="idle") -> Path:
        d = self.dir / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("%s.json" % pid)
        p.write_text(json.dumps({"pid": pid, "sessionId": session, "cwd": cwd, "status": status}),
                     encoding="utf-8")
        return p


def make_ledger(tmp: Path) -> sqlite3.Connection:
    return open_ledger(Path(tmp) / "ledger.db")


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def run_cli(args: List[str]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(args)
    if code != 0:
        raise AssertionError("tsuzuki %s exited %s" % (args, code))
    return buf.getvalue()


def make_git_repo(root: str, name: str) -> str:
    p = os.path.join(root, name)
    os.makedirs(os.path.join(p, ".git"), exist_ok=True)
    return p


def write_file(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@contextlib.contextmanager
def allow_system_tmp():
    """一時領域の資料を許可する（Claude の作業用 tmp だけは除外のまま）。"""
    from tsuzuki import documents
    with mock.patch.object(documents, "TMP_PREFIXES", ("/private/tmp/claude-", "/tmp/claude-")):
        yield
