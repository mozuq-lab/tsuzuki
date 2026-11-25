"""VS Code のワークスペース（.code-workspace）の読み取りと、folders の先頭に "." を足す書き込み。"""
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Optional

from tsuzuki.pathguard import is_inside
from tsuzuki.privacy import os_error_text


class WorkspaceError(Exception):
    pass


def strip_jsonc(text: str) -> str:
    """JSONC を JSON にする：// と /* */ のコメントを空白にし、} と ] の前の末尾のカンマを除く。文字列の中は変えない。"""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':  # 文字列：\ の次の文字を飛ばし、閉じの " まで写す
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
            out.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    stripped = "".join(out)
    result = []
    i, n = 0, len(stripped)
    while i < n:
        c = stripped[i]
        if c == '"':
            j = i + 1
            while j < n and stripped[j] != '"':
                j += 2 if stripped[j] == "\\" else 1
            result.append(stripped[i:j + 1])
            i = j + 1
            continue
        if c == ",":
            j = i + 1
            while j < n and stripped[j].isspace():
                j += 1
            if j < n and stripped[j] in "}]":
                i += 1
                continue
        result.append(c)
        i += 1
    return "".join(result)


def first_folder(path: str) -> str:
    """ワークスペースの folders[0]["path"] を、相対ならファイルのあるフォルダーを基準に解いた絶対パスで返す。
    フォルダーとしてなければ WorkspaceError。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise WorkspaceError("読めません（%s）" % os_error_text(e))
    except UnicodeDecodeError as e:
        raise WorkspaceError("読めません（%s）" % e)
    try:
        data = json.loads(strip_jsonc(text))
    except ValueError:
        raise WorkspaceError("JSON の形が正しくありません")
    folders = data.get("folders") if isinstance(data, dict) else None
    first = folders[0] if isinstance(folders, list) and folders else None
    raw = first.get("path") if isinstance(first, dict) else None
    if not isinstance(raw, str) or not raw:
        raise WorkspaceError("folders がありません")
    folder = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), raw))
    if not os.path.isdir(folder):
        raise WorkspaceError("最初のフォルダーがありません: %s" % folder)
    return folder


CANNOT_ADD = "足した後の形を確かめられませんでした"
SELF_ENTRY = '{ "path": "." }'


def _is_own_dir(entry: object, directory: str) -> bool:
    """folders の1件が、ワークスペースのファイルのあるフォルダー自身を指すか。"""
    raw = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(raw, str) or not raw:
        return False
    target = os.path.join(directory, raw)
    try:  # 大文字小文字や Unicode の正規化が違っても同じフォルダーなら同じ（APFS）
        return os.path.samestat(os.stat(target), os.stat(directory))
    except OSError:
        return os.path.realpath(target) == os.path.realpath(directory)


def _skip_blank(text: str, i: int) -> int:
    """空白とコメントを飛ばした次の位置。"""
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            break
    return i


def _folders_bracket(text: str) -> int:
    """一番外のオブジェクトの "folders" 配列を開く [ の位置。なければ -1。文字列とコメントの中は見ない。"""
    depth, i, n = 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if depth == 1 and text[i + 1:j] == "folders":
                k = _skip_blank(text, j + 1)
                if k < n and text[k] == ":":
                    k = _skip_blank(text, k + 1)
                    if k < n and text[k] == "[":
                        return k
            i = j + 1
        elif text.startswith("//", i) or text.startswith("/*", i):
            i = _skip_blank(text, i)
        else:
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
            i += 1
    return -1


def _with_self_first(text: str, bracket: int) -> str:
    """bracket（folders を開く [）の直後に、{ "path": "." } の1件を足した全文。ほかは1バイトも変えない。"""
    line_start = max(text.rfind("\n", 0, bracket), text.rfind("\r", 0, bracket)) + 1
    bracket_indent = text[line_start:bracket]
    bracket_indent = bracket_indent[:len(bracket_indent) - len(bracket_indent.lstrip(" \t"))]
    end = bracket + 1
    while end < len(text) and text[end] not in "\r\n":
        end += 1
    rest = text[bracket + 1:end].strip()
    if end < len(text) and (not rest or rest.startswith("//")):  # [ のあとは行末まで空かコメントだけ
        eol = "\r\n" if text.startswith("\r\n", end) else text[end]
        body = end + len(eol)
        nxt = body
        while nxt < len(text) and text[nxt] in " \t\r\n":
            nxt += 1
        after = _skip_blank(text, body)
        closing = after < len(text) and text[after] == "]"  # コメントだけの行を挟んでも、次が ] なら最後の1件
        if (nxt < len(text) and text[nxt] == "]") or nxt >= len(text):
            indent = bracket_indent + "\t"
        else:
            row = max(text.rfind("\n", 0, nxt), text.rfind("\r", 0, nxt)) + 1
            indent = text[row:nxt]
        entry = indent + SELF_ENTRY + ("" if closing else ",") + eol
        return text[:body] + entry + text[body:]
    after = _skip_blank(text, bracket + 1)
    comma = "" if after < len(text) and text[after] == "]" else ","
    return text[:bracket + 1] + " " + SELF_ENTRY + comma + " " + text[bracket + 1:]


def ensure_self_first(path: str, *, claude_dir: Optional[Path] = None) -> str:
    """ワークスペースの folders の先頭に、ファイルのあるフォルダー自身（{ "path": "." }）がなければ足す。
    先頭にあれば "present"、先頭以外にあれば "elsewhere"（どちらも書かない）、足したら "added"。
    足すのは文字列の差し込みで、コメント・末尾のカンマ・字下げ・改行・モードはそのまま。
    足せなければ WorkspaceError（書かない）。"""
    if os.path.islink(path):
        raise WorkspaceError("シンボリックリンクのワークスペースには足せません")
    if claude_dir is not None:
        if is_inside(path, str(claude_dir)):
            raise WorkspaceError("Claude Code の設定ディレクトリの中のワークスペースには足せません")
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            text = f.read().decode("utf-8")
    except OSError as e:
        raise WorkspaceError("読めません（%s）" % os_error_text(e))
    except UnicodeDecodeError as e:
        raise WorkspaceError("読めません（%s）" % e)
    directory = os.path.dirname(os.path.abspath(path))
    try:
        data = json.loads(strip_jsonc(text))
    except ValueError:
        raise WorkspaceError("JSON の形が正しくありません")
    folders = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(folders, list):
        raise WorkspaceError("folders がありません")
    if folders and _is_own_dir(folders[0], directory):
        return "present"
    if any(_is_own_dir(entry, directory) for entry in folders[1:]):
        return "elsewhere"
    bracket = _folders_bracket(text)
    if bracket < 0:
        raise WorkspaceError(CANNOT_ADD)
    new = _with_self_first(text, bracket)
    try:
        again = json.loads(strip_jsonc(new))
        ok = _is_own_dir(again["folders"][0], directory)
    except (ValueError, KeyError, IndexError, TypeError):
        ok = False
    if not ok:
        raise WorkspaceError(CANNOT_ADD)
    tmp = None
    try:
        with open(path, "r+b"):  # 書き込みの保護（読み取り専用など）を、置き換えで抜けないよう先に確かめる
            pass
        fd, tmp = tempfile.mkstemp(prefix=".tsuzuki-", dir=directory)  # O_EXCL なので、既存のリンクをたどらない
        with os.fdopen(fd, "wb") as f:
            f.write(new.encode("utf-8"))
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise WorkspaceError("書けません（%s）" % os_error_text(e, writing=True))
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return "added"
