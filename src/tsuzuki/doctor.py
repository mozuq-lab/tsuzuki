"""環境の点検（tsuzuki doctor）と、会話の中身を含まない診断ファイル。"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from tsuzuki import __version__
from tsuzuki.config import Config, ConfigError, resolve_tasks_root
from tsuzuki.indexer import discover_transcripts
from tsuzuki.sources.transcript import iter_lines, record_type
from tsuzuki.store import LEDGER_FILENAME, get_meta, has_trigram, open_ledger
from tsuzuki.vscode_ext import editor_for, installed_path, installed_requests_dir, requests_dir

MIN_PYTHON = (3, 9)
MIN_SQLITE = (3, 34, 0)  # trigram に必要
CLAUDE_TIMEOUT_SECONDS = 10
HOOK_RECENT_DAYS = 7
LINEAGE_FILENAME = "lineage.jsonl"
TASK_SKILLS = ("task-start", "task-handoff")


@dataclass
class Check:
    name: str
    status: str  # ok・warn・fail
    detail: str


def hook_configured(settings_path: Path) -> bool:
    """hooks.SessionStart のどれかの command が tsuzuki の hook session-start か。読めなければ False。"""
    try:
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        groups = data["hooks"]["SessionStart"]
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command")
                if isinstance(command, str) and "tsuzuki" in command and "hook session-start" in command:
                    return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return False


def _parse_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _last_lineage_ts(path: Path) -> Optional[datetime]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        return _parse_ts(rec.get("ts")) if isinstance(rec, dict) else None
    return None


def _version_text(info: tuple) -> str:
    return ".".join(str(n) for n in info)


def _claude_command_check(runner) -> Check:
    name = "claude コマンド"
    try:
        proc = runner(["claude", "--version"], capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return Check(name, "warn", "見つかりません")
    except subprocess.TimeoutExpired:
        return Check(name, "warn", "%d秒で応答がありませんでした" % CLAUDE_TIMEOUT_SECONDS)
    except OSError as e:
        return Check(name, "warn", "実行できません: %s" % e)
    if proc.returncode != 0:
        return Check(name, "warn", "終了コード %s で終わりました" % proc.returncode)
    lines = (proc.stdout or "").strip().splitlines()
    return Check(name, "ok", lines[0] if lines else "")


def _hook_check(claude_dir: Path, ledger_dir: Path, now) -> Check:
    name = "フック"
    if not hook_configured(claude_dir / "settings.json"):
        return Check(name, "ok", "未設定（任意）")
    last = _last_lineage_ts(ledger_dir / LINEAGE_FILENAME)
    current = datetime.fromtimestamp(now(), tz=timezone.utc)
    if last is not None and current - last <= timedelta(days=HOOK_RECENT_DAYS):
        return Check(name, "ok", "設定済み・最近の記録あり")
    return Check(name, "warn", "設定されていますが、最近の記録がありません")


def vscode_terminal_check(cfg: Config, home: str, ledger_dir: Path) -> Check:
    """VS Code のターミナルで再開する拡張（設定 vscode_app のアプリに入っているもの）。使わない人のために、
    入っていなくても、既定の再開にしていなければ warn にしない。入っていれば、拡張が覚えている依頼の置き場所が
    今の台帳のものと同じかを確かめる（台帳の場所を変えて入れ直していないと、依頼が見つからない）。"""
    name = "VS Code のターミナルの拡張"
    editor = editor_for(cfg.vscode_app, home)
    if installed_path(editor, home) is not None:
        remembered = installed_requests_dir(editor, home)
        expected = os.path.realpath(str(requests_dir(ledger_dir)))
        if remembered is None or os.path.realpath(remembered) != expected:
            return Check(name, "warn", "拡張が覚えている台帳の場所（%s）が今の台帳と違います。tsuzuki vscode install "
                                       "で入れ直してください" % (remembered or "不明",))
        return Check(name, "ok", "入っています（%s）" % cfg.vscode_app)
    if cfg.resume_default == "vscode-terminal":
        return Check(name, "warn", "設定 resume_default が vscode-terminal ですが、入っていません"
                                   "（tsuzuki vscode install で入れます）")
    return Check(name, "ok", "未導入（任意）")


def _format_check(ledger_dir: Path) -> Check:
    name = "記録の形式"
    path = ledger_dir / LEDGER_FILENAME
    if not path.exists():
        return Check(name, "ok", "台帳がまだありません")
    conn = open_ledger(path)
    try:
        for key in ("format_warning", "source_warning"):
            text = get_meta(conn, key)
            if text:
                return Check(name, "warn", text)
    finally:
        conn.close()
    return Check(name, "ok", "問題は見つかっていません")


def run_checks(cfg: Config, *, ledger_dir: Path, claude_dir: Path, runner=subprocess.run,
               now=time.time, home: Optional[str] = None) -> List[Check]:
    ledger_dir, claude_dir = Path(ledger_dir), Path(claude_dir)
    checks = []  # type: List[Check]
    py = sys.version_info[:3]
    checks.append(Check("Python", "ok" if py >= MIN_PYTHON else "fail", _version_text(py)))
    lite = sqlite3.sqlite_version_info
    if lite < MIN_SQLITE:
        checks.append(Check("SQLite", "warn", "%s（trigram には 3.34 以上が必要です）" % sqlite3.sqlite_version))
    else:
        checks.append(Check("SQLite", "ok", sqlite3.sqlite_version))
    probe = sqlite3.connect(":memory:")
    try:
        trigram = has_trigram(probe)
    finally:
        probe.close()
    checks.append(Check("FTS5 trigram", "ok", "使えます") if trigram
                  else Check("FTS5 trigram", "warn", "使えません。部分一致だけで検索します"))
    projects = claude_dir / "projects"
    if projects.is_dir():
        checks.append(Check("Claude Code の記録", "ok", "記録 %d件" % len(discover_transcripts(claude_dir))))
    else:
        checks.append(Check("Claude Code の記録", "fail", "見つかりません: %s" % projects))
    checks.append(_claude_command_check(runner))
    if not ledger_dir.is_dir():
        checks.append(Check("台帳のディレクトリ", "warn", "まだありません（初回の serve か index で作られます）"))
    else:
        mode = ledger_dir.stat().st_mode & 0o777
        if mode == 0o700:
            checks.append(Check("台帳のディレクトリ", "ok", str(ledger_dir)))
        else:
            checks.append(Check("台帳のディレクトリ", "warn", "権限が %s です（0700 にしてください）" % oct(mode)))
    checks.append(_hook_check(claude_dir, ledger_dir, now))
    checks.append(_format_check(ledger_dir))
    home = home if home is not None else os.path.expanduser("~")
    checks.append(vscode_terminal_check(cfg, home, ledger_dir))
    checks.extend(task_checks(cfg, claude_dir=claude_dir, home=home))
    return checks


def _tasks_root_or_none(cfg: Config, claude_dir: Path, home: str):
    """(タスクの置き場所, 断られたときの文言)。"""
    try:
        return resolve_tasks_root(cfg, home=home, claude_dir=claude_dir), None
    except ConfigError as e:
        return None, str(e)


def _additional_dirs_ok(settings_path: Path, root: Path, home: str) -> bool:
    """permissions.additionalDirectories（パスのリスト）に、置き場所かそれを含むものがあるか。読めないか、
    リストでなければ False（文字列や辞書を1文字ずつ・キーごとに見ると、"/" がすべてを含んでしまう）。"""
    try:
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        entries = data["permissions"]["additionalDirectories"]
        if not isinstance(entries, list):
            return False
        target = os.path.normpath(str(root))
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                continue
            if entry == "~" or entry.startswith("~/"):
                entry = home + entry[1:]
            entry = os.path.normpath(os.path.join(home, entry))
            if target == entry or target.startswith(entry.rstrip("/") + "/"):
                return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return False


def _claude_md_ok(claude_md: Path, root: Path, home: str) -> bool:
    """CLAUDE.md に、置き場所の README.md のパス（~/… の形か絶対パス）があるか。"""
    from tsuzuki.export import display_path
    try:
        text = Path(claude_md).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    readme = root / "README.md"
    return display_path(readme, home) in text or str(readme) in text


def _task_facts(cfg: Config, claude_dir: Path, home: str):
    claude_dir = Path(claude_dir)
    root, error = _tasks_root_or_none(cfg, claude_dir, home)
    facts = {"root_exists": False, "root_writable": False, "readme": False, "additional_dirs": False,
             "skills": {n: (claude_dir / "skills" / n / "SKILL.md").is_file() for n in TASK_SKILLS},
             "claude_md": False}  # type: Dict[str, Any]
    if root is not None:
        facts["root_exists"] = root.is_dir()
        facts["root_writable"] = root.is_dir() and os.access(str(root), os.W_OK)
        facts["readme"] = (root / "README.md").is_file()
        facts["additional_dirs"] = _additional_dirs_ok(claude_dir / "settings.json", root, home)
        facts["claude_md"] = _claude_md_ok(claude_dir / "CLAUDE.md", root, home)
    return root, error, facts


def task_report(cfg: Config, *, claude_dir: Path, home: str) -> Dict[str, Any]:
    """タスクの導入の、あるかないかだけ（パスやファイルの中身は入れない）。"""
    return _task_facts(cfg, claude_dir, home)[2]


def task_checks(cfg: Config, *, claude_dir: Path, home: str) -> List[Check]:
    """タスクの導入の点検。使わない人のために、ok か warn だけで fail にはしない。"""
    root, error, f = _task_facts(cfg, claude_dir, home)
    names = ["タスクの置き場所", "タスクの決まり（README.md）", "Claude Code の書き込みの許可",
             "スキル（task-start・task-handoff）", "CLAUDE.md の決まり"]
    if root is None:
        return [Check(n, "warn", error or "") for n in names]
    claude_dir = Path(claude_dir)
    checks = []
    if not f["root_exists"]:
        checks.append(Check(names[0], "warn", "まだありません: %s（tsuzuki templates の案内を見てください）" % root))
    elif not f["root_writable"]:
        checks.append(Check(names[0], "warn", "書き込めません: %s" % root))
    else:
        checks.append(Check(names[0], "ok", str(root)))
    checks.append(Check(names[1], "ok", "あります") if f["readme"]
                  else Check(names[1], "warn", "%s がありません" % (root / "README.md")))
    checks.append(Check(names[2], "ok", "設定済み") if f["additional_dirs"]
                  else Check(names[2], "warn", "%s の permissions.additionalDirectories にタスクの置き場所がありません"
                             % (claude_dir / "settings.json")))
    missing = [n for n in TASK_SKILLS if not f["skills"][n]]
    checks.append(Check(names[3], "ok", "2つともあります") if not missing
                  else Check(names[3], "warn", "ありません: %s" % "、".join(missing)))
    checks.append(Check(names[4], "ok", "書かれています") if f["claude_md"]
                  else Check(names[4], "warn", "%s にタスクの README.md のパスがありません" % (claude_dir / "CLAUDE.md")))
    return checks


def _keys_by_type(claude_dir: Path, sample: int) -> Dict[str, List[str]]:
    """新しい記録 sample 件の、種類ごとの最上位の項目名（値は読み取らない）。"""
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    files = sorted(discover_transcripts(claude_dir), key=mtime, reverse=True)[:sample]
    keys = {}  # type: Dict[str, Set[str]]
    for path in files:
        try:
            for line in iter_lines(path, 0):
                if line.skipped or line.record is None:
                    continue
                keys.setdefault(record_type(line.record), set()).update(str(k) for k in line.record)
        except OSError:
            continue
    return {typ: sorted(names) for typ, names in sorted(keys.items())}


def build_report(conn: Optional[sqlite3.Connection], *, claude_dir: Path, sample: int = 50,
                 tasks: Optional[Dict[str, Any]] = None) -> dict:
    """診断ファイルの中身。会話の中身（本文・題・パス）は入れず、数と項目名だけにする。
    conn が None なら台帳がまだない場合で、台帳の統計は空にする。"""
    probe = sqlite3.connect(":memory:")
    try:
        trigram = has_trigram(probe)
    finally:
        probe.close()
    sessions = {}  # type: Dict[str, int]
    format_stats = []  # type: List[dict]
    unreadable = 0
    if conn is not None:
        sessions = {k: n for k, n in conn.execute("SELECT kind, COUNT(*) FROM sessions GROUP BY kind ORDER BY kind")}
        format_stats = [{"version": v, "record_type": r, "count": n} for v, r, n in conn.execute(
            "SELECT version, record_type, count FROM format_stats ORDER BY version, record_type")]
        try:
            unreadable = int(get_meta(conn, "unreadable_lines") or 0)
        except ValueError:
            unreadable = 0
    d = datetime.now(timezone.utc)
    report = {
        "generated_at": d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000),
        "tsuzuki_version": __version__,
        "python": _version_text(sys.version_info[:3]),
        "sqlite": sqlite3.sqlite_version,
        "trigram": trigram,
        "transcripts": len(discover_transcripts(claude_dir)),
        "sessions": sessions,
        "format_stats": format_stats,
        "keys_by_type": _keys_by_type(claude_dir, sample),
        "unreadable_lines": unreadable,
    }  # type: dict
    if tasks is not None:
        report["tasks"] = tasks
    return report


def write_report(report: dict, path: Path) -> None:
    """0600 で書く。既にあるファイルも 0600 にする。"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
