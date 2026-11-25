"""VS Code のワークスペース（.code-workspace）の読み取り。読むだけで、書かない。"""
import json
import os

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
