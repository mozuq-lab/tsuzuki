"""設定ファイル（config.json）の読み書きと、設定値の優先順位の解決。"""
import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

DEFAULT_DOC_EXTENSIONS = [
    ".md", ".markdown", ".mdx", ".txt", ".html", ".htm",
    ".csv", ".pdf", ".docx", ".pptx", ".xlsx",
]

DEFAULT_EXCLUDE_DIRS = [
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".ruff_cache", ".pytest_cache", ".next", ".wrangler", ".dart_tool",
    "build", "Pods", "DerivedData", ".playwright-mcp",
]

RETENTION_VALUES = ("all", "card", "none")
RESUME_VALUES = ("vscode", "vscode-terminal", "terminal", "copy")


class ConfigError(Exception):
    pass


@dataclass
class DocScan:
    enabled: bool = True
    max_depth: int = 6
    max_files: int = 3000
    interval_seconds: int = 600
    exclude_dirs: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))


@dataclass
class Config:
    port: int = 8765
    open_browser: bool = True
    refresh_seconds: int = 30
    claude_dir: str = "~/.claude"
    retention: str = "all"
    max_age_days: Optional[int] = None
    index_history: bool = True
    resume_default: str = "vscode"
    vscode_app: str = "Visual Studio Code"
    vscode_uri_delay_ms: int = 1500
    display_strip_prefixes: List[str] = field(default_factory=list)
    place_aliases: Dict[str, str] = field(default_factory=dict)
    doc_extensions: List[str] = field(default_factory=lambda: list(DEFAULT_DOC_EXTENSIONS))
    doc_scan: DocScan = field(default_factory=DocScan)
    headless_in_list: bool = False
    tasks_root: str = "~/tasks"
    ticket_url: Optional[str] = None
    new_conversation_prompt: str = "@{task_md} を読んで、次の一手から進めてください。"
    default_branches: List[str] = field(default_factory=lambda: ["main", "master", "develop"])


def ledger_dir_from(cli_value: Optional[str], env: Mapping[str, str]) -> Path:
    raw = cli_value or env.get("TSUZUKI_HOME") or "~/.tsuzuki"
    return Path(raw).expanduser()


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _validate(cfg: Config) -> None:
    if cfg.retention not in RETENTION_VALUES:
        raise ConfigError("設定 retention は all・card・none のどれかにしてください: %r" % (cfg.retention,))
    if cfg.resume_default not in RESUME_VALUES:
        raise ConfigError("設定 resume_default は vscode・vscode-terminal・terminal・copy のどれかにしてください: %r" % (cfg.resume_default,))
    if not _is_int(cfg.port) or not 1 <= cfg.port <= 65535:
        raise ConfigError("設定 port は 1 から 65535 の整数にしてください: %r" % (cfg.port,))
    if cfg.max_age_days is not None and (not _is_int(cfg.max_age_days) or cfg.max_age_days <= 0):
        raise ConfigError("設定 max_age_days は null か正の整数にしてください: %r" % (cfg.max_age_days,))
    if not isinstance(cfg.tasks_root, str) or not cfg.tasks_root.strip():
        raise ConfigError("設定 tasks_root は空でない文字列にしてください: %r" % (cfg.tasks_root,))
    if cfg.ticket_url is not None and (not isinstance(cfg.ticket_url, str) or "{n}" not in cfg.ticket_url):
        raise ConfigError("設定 ticket_url は null か、{n} を含む文字列にしてください: %r" % (cfg.ticket_url,))
    if not isinstance(cfg.new_conversation_prompt, str) or "{task_md}" not in cfg.new_conversation_prompt:
        raise ConfigError(
            "設定 new_conversation_prompt は {task_md} を含む文字列にしてください: %r" % (cfg.new_conversation_prompt,))
    if not isinstance(cfg.default_branches, list) or not all(isinstance(b, str) for b in cfg.default_branches):
        raise ConfigError("設定 default_branches は文字列の配列にしてください: %r" % (cfg.default_branches,))


def _from_dict(data: Dict[str, Any]) -> Config:
    cfg = Config()
    for f in fields(Config):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name == "doc_scan":
            if not isinstance(value, dict):
                raise ConfigError("設定 doc_scan はオブジェクトにしてください")
            scan = DocScan()
            for sf in fields(DocScan):
                if sf.name in value:
                    setattr(scan, sf.name, value[sf.name])
            value = scan
        setattr(cfg, f.name, value)
    return cfg


def _to_dict(cfg: Config) -> Dict[str, Any]:
    out = {}
    for f in fields(Config):
        v = getattr(cfg, f.name)
        if isinstance(v, DocScan):
            v = {sf.name: getattr(v, sf.name) for sf in fields(DocScan)}
        out[f.name] = v
    return out


def read_config(ledger: Path) -> Config:
    """config.json を読んで検証する。ディレクトリもファイルも作らず、権限も変えない。
    ファイルがなければ既定の設定を返す。"""
    path = ledger / "config.json"
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError("config.json を読めません: %s" % e)
    if not isinstance(data, dict):
        raise ConfigError("config.json の最上位はオブジェクトにしてください")
    cfg = _from_dict(data)
    _validate(cfg)
    return cfg


BAD_TICKET_URL = "チケットのリンク先は http:// か https:// で始まる URL にしてください"


def normalize_ticket_url(value: str) -> Optional[str]:
    """画面で入れたチケットのリンク先を、設定 ticket_url の値にする。空なら None（リポジトリの Issue に戻す）。
    {n} がなければ、末尾に /{n} を足す。"""
    value = value.strip()
    if not value:
        return None
    if any(ch.isspace() or not ch.isprintable() for ch in value) or not value.startswith(("http://", "https://")):
        raise ConfigError(BAD_TICKET_URL)
    return value if "{n}" in value else value.rstrip("/") + "/{n}"


def write_ticket_url(ledger: Path, value: Optional[str]) -> None:
    """config.json の ticket_url だけを書き換える（ほかの項目と知らない項目はそのまま）。読めない・正しくない
    ときは ConfigError で、書かない。同じフォルダーの一時ファイルに書いてから置き換え、元の権限を保つ。"""
    path = ledger / "config.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError("config.json を読めません: %s" % e)
        if not isinstance(data, dict):
            raise ConfigError("config.json の最上位はオブジェクトにしてください")
        mode = os.stat(str(path)).st_mode & 0o777
    else:
        data, mode = _to_dict(Config()), 0o600
    data["ticket_url"] = value
    _validate(_from_dict(dict(data)))
    tmp = None  # type: Optional[str]
    try:
        fd, tmp = tempfile.mkstemp(dir=str(ledger), prefix=".config-", suffix=".tmp")  # 名前は毎回新しく、リンクはたどらない
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except OSError as e:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise ConfigError("config.json に書けません: %s" % e)


def load_config(ledger: Path) -> Config:
    ledger.mkdir(parents=True, exist_ok=True)
    os.chmod(str(ledger), 0o700)
    path = ledger / "config.json"
    if not path.exists():
        cfg = Config()
        path.write_text(
            json.dumps(_to_dict(cfg), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return cfg
    return read_config(ledger)


def resolve_port(cli_port: Optional[int], env: Mapping[str, str], cfg: Config) -> int:
    if cli_port is not None:
        return cli_port
    raw = env.get("TSUZUKI_PORT")
    if raw:
        try:
            port = int(raw)
        except ValueError:
            raise ConfigError("環境変数 TSUZUKI_PORT は整数にしてください: %r" % raw)
        if not 1 <= port <= 65535:
            raise ConfigError("環境変数 TSUZUKI_PORT は 1 から 65535 にしてください: %r" % raw)
        return port
    return cfg.port


def resolve_claude_dir(cli_value: Optional[str], env: Mapping[str, str], cfg: Config) -> Path:
    raw = cli_value or env.get("CLAUDE_CONFIG_DIR") or cfg.claude_dir
    return Path(raw).expanduser()


def resolve_tasks_root(cfg: Config, *, home: str, claude_dir: Path) -> Path:
    """tasks_root を絶対パスにする。~ は引数 home で展開し、シンボリックリンクは解かない。
    ホームやボリュームの直下、Claude Code の設定ディレクトリの中は断る。"""
    from .documents import should_scan_place  # documents は sqlite3 を読むので、使う時まで読み込まない

    raw = cfg.tasks_root
    if raw == "~" or raw.startswith("~/"):
        raw = home + raw[1:]
    path = os.path.normpath(os.path.join(home, raw))  # 相対パスはホームからの相対として扱う
    if not should_scan_place(path, home):
        raise ConfigError("設定 tasks_root にホームやボリュームの直下は指定できません: %s" % path)
    claude = os.path.normpath(os.path.join(home, str(claude_dir)))
    real, real_claude = os.path.realpath(path), os.path.realpath(claude)
    # シンボリックリンクを解いた先でも比べる（tasks_root か ~/.claude がリンクで、実体が重なる場合）
    if any(p == c or p.startswith(c.rstrip("/") + "/") for p, c in ((path, claude), (real, real_claude))):
        raise ConfigError("設定 tasks_root に Claude Code の設定ディレクトリの中は指定できません: %s" % path)
    return Path(path)
