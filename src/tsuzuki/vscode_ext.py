"""VS Code の統合ターミナルで claude を起動するための、同梱の拡張（tsuzuki-local.terminal）の扱い。

画面から「VS Code のターミナルで再開」すると、Tsuzuki は依頼（作業ディレクトリと実行するコマンド）を
<台帳>/vscode-requests/<ID>.json に権限 0600 で書き、<スキーム>://tsuzuki-local.terminal/run?id=<ID> を開く。
拡張はその依頼を読んで消し、作業ディレクトリで統合ターミナルを開いてコマンドを送る。URI に載るのは乱数の
ID だけなので、Web ページなどが URI を開いても、Tsuzuki が書いていない依頼は実行されない。

拡張の本体は vscode/ にあり、tsuzuki vscode install が VSIX にまとめて入れる。そのとき依頼の置き場所を
拡張の中の tsuzuki.json に書く（拡張は台帳の場所を知らないため）。

URI のスキーム（vscode・cursor など）と拡張の置き場所（~/.vscode・~/.cursor など）は、設定 vscode_app の
アプリの product.json（urlProtocol・dataFolderName）から読む。読めなければ VS Code のものを使う。
"""
import json
import os
import pkgutil
import re
import secrets
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

PUBLISHER = "tsuzuki-local"
NAME = "terminal"
EXTENSION_ID = PUBLISHER + "." + NAME
REQUESTS_DIRNAME = "vscode-requests"
REQUEST_MAX_AGE_SECONDS = 120  # 拡張の MAX_AGE_MS と同じ。これより古い依頼は拡張が使わず、Tsuzuki が片づける
TERMINAL_NAME = "Claude Code"  # 拡張の DEFAULT_NAME と同じ
RUN_URI = "{scheme}://" + EXTENSION_ID + "/run?id={id}"
CONFIG_NAME = "tsuzuki.json"
SOURCE_FILES = ("package.json", "extension.js")
NOT_INSTALLED = "%s に拡張 Tsuzuki Terminal が入っていません。tsuzuki vscode install で入れてください"
APP_DIRS = ("/Applications", "~/Applications")  # アプリを探すフォルダー（~ はホーム）
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*")
_DATA_FOLDER = re.compile(r"\.[A-Za-z0-9._-]+")
_CLI_NAME = re.compile(r"[A-Za-z0-9._-]+")

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension=".json" ContentType="application/json"/>'
    '<Default Extension=".js" ContentType="application/javascript"/>'
    '<Default Extension=".vsixmanifest" ContentType="text/xml"/>'
    '</Types>\n')

_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}" />
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Categories>Other</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="ui" />
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
  </Assets>
</PackageManifest>
"""


def requests_dir(ledger_dir: Path) -> Path:
    return Path(ledger_dir) / REQUESTS_DIRNAME


def _source(name: str) -> bytes:
    data = pkgutil.get_data("tsuzuki", "vscode/" + name)  # zipapp の中でも読める
    if data is None:
        raise FileNotFoundError("vscode/" + name)
    return data


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_vsix(out: Path, requests: Path) -> Path:
    """拡張を VSIX（zip）にまとめて out に書く。依頼の置き場所 requests（絶対パス）を tsuzuki.json に入れる。"""
    meta = json.loads(_source("package.json").decode("utf-8"))
    manifest = _MANIFEST.format(name=_xml(meta["name"]), version=_xml(meta["version"]),
                                publisher=_xml(meta["publisher"]), display=_xml(meta["displayName"]),
                                description=_xml(meta["description"]), engine=_xml(meta["engines"]["vscode"]))
    config = json.dumps({"requests_dir": os.path.abspath(str(requests))}, ensure_ascii=False, indent=2) + "\n"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", manifest)
        for name in SOURCE_FILES:
            z.writestr("extension/" + name, _source(name))
        z.writestr("extension/" + CONFIG_NAME, config)
    return out


@dataclass(frozen=True)
class Editor:
    """設定 vscode_app のアプリ（VS Code か、その派生）の、URI のスキーム・拡張の置き場所・コマンド。"""
    app: str
    scheme: str = "vscode"
    data_folder: str = ".vscode"  # ホームからの相対。拡張は <ホーム>/<data_folder>/extensions に入る
    cli: Optional[str] = None  # アプリに入っている、拡張を入れるコマンド

    def extensions_dir(self, home: str) -> Path:
        return Path(home) / self.data_folder / "extensions"


def editor_for(app: str, home: str) -> Editor:
    """アプリの product.json を読む。アプリや項目が見つからなければ、VS Code のものを使う。"""
    for base in APP_DIRS:
        root = Path(home + base[1:] if base.startswith("~") else base) / (app + ".app") / "Contents" / "Resources" / "app"
        try:
            product = json.loads((root / "product.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(product, dict):
            continue
        scheme, folder, name = product.get("urlProtocol"), product.get("dataFolderName"), product.get("applicationName")
        cli = None
        for candidate in ([name] if isinstance(name, str) and _CLI_NAME.fullmatch(name) else []) + ["code"]:
            if (root / "bin" / candidate).is_file():
                cli = str(root / "bin" / candidate)
                break
        return Editor(app,
                      scheme if isinstance(scheme, str) and _SCHEME.fullmatch(scheme) else "vscode",
                      folder if isinstance(folder, str) and _DATA_FOLDER.fullmatch(folder) else ".vscode",
                      cli)
    return Editor(app)


def _obsolete(folder: Path) -> set:
    """外した・入れ替えた拡張のフォルダー名（エディターが次に起動するまで、フォルダーが残る）。"""
    try:
        data = json.loads((folder / ".obsolete").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {k for k, v in data.items() if v} if isinstance(data, dict) else set()


def installed_path(editor: Editor, home: str) -> Optional[Path]:
    """そのエディターに入っているこの拡張のフォルダー。extensions.json があればそれで、なければフォルダーの名前で
    探し、外した・入れ替えたもの（.obsolete）は除く。入っていなければ None。"""
    folder = editor.extensions_dir(home)
    obsolete = _obsolete(folder)
    try:
        listed = json.loads((folder / "extensions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        listed = None
    names = []  # type: List[str]
    if isinstance(listed, list):
        for entry in listed:
            try:
                if entry["identifier"]["id"].lower() == EXTENSION_ID:
                    names.append(entry.get("relativeLocation") or "")
            except (KeyError, TypeError, AttributeError):
                continue
    else:
        try:
            names = sorted(n for n in os.listdir(str(folder)) if n.startswith(EXTENSION_ID + "-"))
        except OSError:
            names = []
    for name in reversed(names):
        if name and "/" not in name and name not in obsolete and (folder / name / "package.json").is_file():
            return folder / name
    return None


def installed_requests_dir(editor: Editor, home: str) -> Optional[str]:
    """入っている拡張が覚えている、依頼の置き場所（tsuzuki.json の requests_dir）。読めなければ None。"""
    path = installed_path(editor, home)
    if path is None:
        return None
    try:
        value = json.loads((path / CONFIG_NAME).read_text(encoding="utf-8")).get("requests_dir")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def code_cli(app: str, home: str, *, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """設定 vscode_app のアプリに入っている、拡張を入れるコマンド。なければ、VS Code のときだけ PATH の code。"""
    cli = editor_for(app, home).cli
    if cli is not None:
        return cli
    return which("code") if app == "Visual Studio Code" else None


def _cleanup(directory: Path, now: float) -> None:
    """期限を過ぎた依頼を消す（拡張が受け取らなかった分。拡張が入っていない、VS Code が開かなかったなど）。"""
    try:
        names = os.listdir(str(directory))
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = directory / name
        try:
            if now - path.stat().st_mtime > REQUEST_MAX_AGE_SECONDS:
                path.unlink()
        except OSError:
            continue


class VSCodeTerminal:
    """VS Code の統合ターミナルへの依頼の書き先（<台帳>/vscode-requests）と、拡張が入っているかの確かめ。
    app は設定 vscode_app（URI のスキームと拡張の置き場所を、そのアプリから読む）。"""

    def __init__(self, requests: Path, home: str, app: str = "Visual Studio Code", *,
                 now: Callable[[], float] = time.time, token: Callable[[int], str] = secrets.token_hex,
                 editor: Optional[Editor] = None):
        self.requests, self.home, self.app, self._now, self._token = Path(requests), home, app, now, token
        self._editor = editor

    @property
    def editor(self) -> Editor:
        if self._editor is None:
            self._editor = editor_for(self.app, self.home)
        return self._editor

    def installed(self) -> bool:
        return installed_path(self.editor, self.home) is not None

    def write(self, cwd: str, command: str) -> str:
        """依頼を権限 0600 で書いて、その ID（16進の32文字）を返す。フォルダーは 0700 で作る。"""
        if "\n" in command or "\r" in command:
            raise ValueError("コマンドに改行は入れられません")
        self.requests.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(self.requests), 0o700)
        _cleanup(self.requests, self._now())
        request_id = self._token(16)
        body = json.dumps({"cwd": cwd, "command": command, "name": TERMINAL_NAME}, ensure_ascii=False)
        fd = os.open(str(self.requests / (request_id + ".json")), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body + "\n")
        return request_id

    def discard(self, request_id: str) -> None:
        """開けなかった依頼を消す（拡張が受け取らないので、残しておく理由がない）。"""
        try:
            (self.requests / (request_id + ".json")).unlink()
        except OSError:
            pass

    def steps(self, cwd: str, request_id: str) -> List[List[str]]:
        """先にそのフォルダーの窓を前に出し、そのあとで URI を開く（URI は手前の窓が受け取る）。"""
        uri = RUN_URI.format(scheme=self.editor.scheme, id=request_id)
        return [["open", "-a", self.app, cwd], ["open", uri]]
