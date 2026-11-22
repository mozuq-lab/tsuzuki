"""Claude Code の記録（transcript）の形を知っている唯一のモジュール。"""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

SKIP_PREFIXES = (b'{"type":"api-request', b'{"type":"queue-operation', b'{"type":"file-history-snapshot',
                 b'{"type":"last-prompt', b'{"type":"session-owner', b'{"type":"atis-latch',
                 b'{"type":"data-flow-records')
MAX_LINE_BYTES = 8_000_000

KNOWN_TYPES = frozenset("""user assistant system attachment ai-title custom-title agent-name continued-in
pr-link relocated last-prompt mode permission-mode session-owner data-flow-records atis-latch api-request
api-request-blob api-request-shape file-history-snapshot file-history-delta tools-used summary
bridge-session agent-setting queue-operation derived-from""".split())

# 要約の末尾の案内。版によって「(disable recaps in /config)」「(turn off recaps in /config)」と言い方が違う
_RECAP_SUFFIX = re.compile(r"\([^()]*\brecaps\s+in\s+/config\b[^()]*\)$", re.IGNORECASE)
# 本人が貼り付けた内容を囲むタグ（<pasted_content id="…"> と </pasted_content id="…">）
_PASTED_TAG = re.compile(r"</?pasted_content(?:\s[^<>]*)?>")
_COMMAND_START = ("<command-name>", "<command-message>")
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_LOCAL_OUTPUT_START = ("<local-command-stdout>", "<local-command-stderr>")
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F]")
_SURROGATE = re.compile("[\ud800-\udfff]")


@dataclass
class RawLine:
    end_offset: int
    record: Optional[dict]
    skipped: bool


@dataclass
class Meta:
    session_id: Optional[str]
    ts: Optional[str]
    cwd: Optional[str]
    branch: Optional[str]
    entrypoint: Optional[str]
    version: Optional[str]


@dataclass
class UserPrompt:
    ts: Optional[str]
    text: str
    human: bool
    command: bool = False  # スラッシュコマンドだけの入力。応答が付いたときに本人の入力に数える（ingest）
    # 表示用の本文（打ち込んだ文を先、貼り付けた中身を後）。text と同じなら None
    display: Optional[str] = None


@dataclass
class LocalCommandOutput:
    """Claude Code の中だけで済んだコマンドの出力（<local-command-stdout> など）。"""
    ts: Optional[str]


@dataclass
class AssistantText:
    ts: Optional[str]
    text: str


@dataclass
class ToolUse:
    ts: Optional[str]
    name: str
    input: dict


@dataclass
class ToolResult:
    ts: Optional[str]
    text: str


@dataclass
class Title:
    kind: str  # custom / agent / ai
    text: str


@dataclass
class Recap:
    ts: Optional[str]
    text: str


@dataclass
class ContinuedIn:
    ts: Optional[str]
    session_id: str


@dataclass
class PrLink:
    ts: Optional[str]
    url: str


@dataclass
class Relocated:
    cwd: str


@dataclass
class ForkedFrom:
    session_id: str


@dataclass
class DerivedFrom:
    """記録の derived-from が名指す元のセッションと、そこからの来かた（via）。"""
    session_id: str
    via: str


def iter_lines(path: Path, start: int) -> Iterator[RawLine]:
    """start から行ごとに読む。改行で終わらない最後の行は返さない。"""
    with open(path, "rb") as f:
        f.seek(start)
        offset = start
        while True:
            chunk = f.readline(MAX_LINE_BYTES + 1)
            if not chunk:
                return
            if chunk.endswith(b"\n"):
                offset += len(chunk)
                yield _parse(chunk, offset)
            elif len(chunk) > MAX_LINE_BYTES:
                # 上限を超えた行は改行まで捨てる
                size = len(chunk)
                complete = False
                while True:
                    rest = f.readline(MAX_LINE_BYTES + 1)
                    if not rest:
                        break
                    size += len(rest)
                    if rest.endswith(b"\n"):
                        complete = True
                        break
                if not complete:
                    return
                offset += size
                yield RawLine(offset, None, True)
            else:
                return  # 書きかけの最後の行


def _parse(chunk: bytes, end_offset: int) -> RawLine:
    if chunk.startswith(SKIP_PREFIXES):
        return RawLine(end_offset, None, True)
    try:
        rec = json.loads(chunk.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return RawLine(end_offset, None, False)
    if not isinstance(rec, dict):
        return RawLine(end_offset, None, False)
    if _SURROGATE_ESCAPE.search(chunk):
        # 対になっていないサロゲート（"\ud83d" など）は UTF-8 で保存できないので、置換文字にする
        rec = json.loads(_SURROGATE.sub("\ufffd", json.dumps(rec, ensure_ascii=False)))
    return RawLine(end_offset, rec, False)


def _str(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def record_type(record: dict) -> str:
    typ = record.get("type") or ""
    if typ == "system":
        return "system:%s" % (record.get("subtype") or "")
    return typ


def is_human_prompt(record: dict) -> bool:
    if record.get("isMeta"):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "human":
        return True
    return record.get("promptSource") == "typed"


def _content_blocks(record: dict) -> list:
    content = (record.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _tool_result_text(record: dict) -> str:
    parts = []
    for b in _content_blocks(record):
        if isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return "\n".join(p for p in parts if p)


def _text_blocks(record: dict) -> Optional[list]:
    """本文の文字列か text の塊の並び。どちらでもなければ None。"""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return None


def _recap_text(text: str) -> str:
    text = text.rstrip()
    found = _RECAP_SUFFIX.search(text)
    return (text[:found.start()] if found else text).strip()


def _parts(text: str) -> List[Tuple[str, bool]]:
    """貼り付けのタグで区切った部分の並び（部分, 貼り付けた中身か）。前後の空白を除き、空の部分は除く。"""
    parts, pos, inside = [], 0, False
    for m in _PASTED_TAG.finditer(text):
        parts.append((text[pos:m.start()].strip(), inside))
        inside = not m.group(0).startswith("</")
        pos = m.end()
    parts.append((text[pos:].strip(), inside))
    return [(p, pasted) for p, pasted in parts if p]


def _unwrap_pasted(text: str) -> str:
    """貼り付けのタグを外し、タグで区切られた部分を改行でつなぐ。"""
    return "\n".join(p for p, _ in _parts(text))


def _command(text: str) -> str:
    """スラッシュコマンドの記録を「/名前 引数」にする。名前がなければ空。"""
    found = _COMMAND_NAME.search(text)
    name = found.group(1).strip() if found else ""
    if not name:
        return ""
    if not name.startswith("/"):
        name = "/" + name
    args = _COMMAND_ARGS.search(text)
    args_text = _unwrap_pasted(args.group(1)) if args else ""
    return "%s %s" % (name, args_text) if args_text else name


def _block(text: str) -> Tuple[List[Tuple[str, bool]], bool]:
    """1つの塊の (本人の入力の部分の並び, スラッシュコマンドか)。本人の入力でない塊は空の並び。"""
    head = text.lstrip()
    if head.startswith(_COMMAND_START):
        command = _command(head)
        return ([(command, False)] if command else []), True
    if head.startswith("<") and not _PASTED_TAG.match(head):
        return [], False
    return _parts(text), False


def _prompt_text(record: dict) -> Tuple[str, Optional[str], bool]:
    """本人の入力の (元の順の本文, 表示用の本文, スラッシュコマンドだけの入力か)。表示用は本文と同じなら None。"""
    parts, blocks, commands = [], 0, 0  # type: List[Tuple[str, bool]], int, int
    for tx in _text_blocks(record) or []:
        found, is_command = _block(tx)
        if found:
            parts += found
            blocks += 1
            commands += is_command
    text = "\n".join(p for p, _ in parts)
    shown = "\n".join([p for p, pasted in parts if not pasted] + [p for p, pasted in parts if pasted])
    return text, (shown if shown != text else None), bool(parts) and commands == blocks


def _is_local_output(record: dict) -> bool:
    blocks = _text_blocks(record)
    return bool(blocks) and isinstance(blocks[-1], str) and blocks[-1].lstrip().startswith(_LOCAL_OUTPUT_START)


def extract_events(record: dict) -> List[object]:
    """記録の本文の形が崩れていると例外になる（呼び出し側は読めない行として扱う）。
    文字列のはずの項目に別の型が入っていたら、その項目は無いものとして扱う。"""
    events: List[object] = []
    typ = record.get("type")
    ts = _str(record.get("timestamp"))
    session_id = _str(record.get("sessionId"))
    if session_id or ts:
        events.append(Meta(session_id, ts, _str(record.get("cwd")), _str(record.get("gitBranch")),
                           _str(record.get("entrypoint")), _str(record.get("version"))))
    if typ == "user":
        fk = record.get("forkedFrom")
        if isinstance(fk, dict) and _str(fk.get("sessionId")):
            events.append(ForkedFrom(fk["sessionId"]))
        has_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _content_blocks(record))
        if record.get("toolUseResult") is not None or has_result:
            events.append(ToolResult(ts, _tool_result_text(record)))
        elif _is_local_output(record):
            events.append(LocalCommandOutput(ts))
        elif not record.get("isMeta"):
            text, display, command = _prompt_text(record)
            if text:
                events.append(UserPrompt(ts, text, is_human_prompt(record), command, display))
    elif typ == "assistant":
        for b in _content_blocks(record):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                events.append(AssistantText(ts, b["text"]))
            elif b.get("type") == "tool_use":
                inp = b.get("input")
                events.append(ToolUse(ts, _str(b.get("name")) or "", inp if isinstance(inp, dict) else {}))
    elif typ == "system":
        if record.get("subtype") == "away_summary" and isinstance(record.get("content"), str):
            events.append(Recap(ts, _recap_text(record["content"])))
    elif typ == "ai-title" and _str(record.get("aiTitle")):
        events.append(Title("ai", record["aiTitle"]))
    elif typ == "custom-title" and _str(record.get("customTitle")):
        events.append(Title("custom", record["customTitle"]))
    elif typ == "agent-name" and _str(record.get("agentName")):
        events.append(Title("agent", record["agentName"]))
    elif typ == "continued-in" and _str(record.get("continuedInSessionId")):
        events.append(ContinuedIn(ts, record["continuedInSessionId"]))
    elif typ == "pr-link" and _str(record.get("prUrl")):
        events.append(PrLink(ts, record["prUrl"]))
    elif typ == "relocated" and _str(record.get("relocatedCwd")):
        events.append(Relocated(record["relocatedCwd"]))
    elif typ == "derived-from" and isinstance(record.get("sessions"), list):
        for item in record["sessions"]:
            if (isinstance(item, dict) and item.get("type") == "claude_code_session"
                    and _str(item.get("id")) and _str(item.get("via"))):
                events.append(DerivedFrom(item["id"], item["via"]))
    return events


def _rel(path: str, cwd: Optional[str]) -> str:
    if cwd:
        prefix = cwd.rstrip("/") + "/"
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def tool_summary(name: str, inp: dict, cwd: Optional[str]) -> str:
    inp = inp or {}
    if name == "Bash":
        d = inp.get("description") or str(inp.get("command") or "")[:60]
        s = "Bash（%s）" % d if d else "Bash"
    elif name in ("Write", "Edit", "Read") and inp.get("file_path"):
        s = "%s %s" % (name, _rel(str(inp["file_path"]), cwd))
    elif name == "Agent" and inp.get("description"):
        s = "Agent（%s）" % inp["description"]
    elif name == "Skill" and inp.get("skill"):
        s = "Skill（%s）" % inp["skill"]
    elif name in ("Grep", "Glob") and inp.get("pattern"):
        s = "%s（%s）" % (name, inp["pattern"])
    else:
        s = name
    return " ".join(s.split())[:120]
