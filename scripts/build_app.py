#!/usr/bin/env python3
"""zipapp と Swift の殻をまとめて Tsuzuki.app にする。

    python3 scripts/build_app.py

dist/Tsuzuki.app を作る。要る物：swiftc（Xcode か Command Line Tools）。
署名は TSUZUKI_CODESIGN_IDENTITY、なければキーチェーンの最初の Apple Development、なければ ad-hoc。
"""
import importlib.util
import os
import plistlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Tuple

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
IDENTITY_ENV = "TSUZUKI_CODESIGN_IDENTITY"
# security find-identity の行：「  2) <40桁の16進> "Apple Development: …"」
IDENTITY_LINE = re.compile(r'\)\s+([0-9A-Fa-f]{40})\s+"([^"]*Apple Development: [^"]*)"')
# (ファイル名に付ける大きさ, 実際の画素数)
ICON_SIZES = [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64),
              ("128x128", 128), ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512),
              ("512x512", 512), ("512x512@2x", 1024)]


def load_build():
    """scripts/build.py は package ではないので、ファイルから読む。"""
    spec = importlib.util.spec_from_file_location("tsuzuki_build", str(ROOT / "scripts" / "build.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_version(src: Path) -> str:
    text = (Path(src) / "tsuzuki" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def info_plist(version: str) -> bytes:
    return plistlib.dumps({
        "CFBundleIdentifier": "local.tsuzuki.app",
        "CFBundleName": "Tsuzuki",
        "CFBundleDisplayName": "Tsuzuki",
        "CFBundleExecutable": "Tsuzuki",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSAppleEventsUsageDescription": "Terminal で Claude Code のセッションを再開するために使います。",
    })


def signing_identity(env: Mapping[str, str], runner=subprocess.run) -> Tuple[str, str]:
    """(codesign に渡す値, 表示する名前)。環境変数、Apple Development、ad-hoc の順に選ぶ。"""
    if env.get(IDENTITY_ENV):
        return env[IDENTITY_ENV], env[IDENTITY_ENV]
    try:
        found = runner(["security", "find-identity", "-v", "-p", "codesigning"],
                       check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, OSError):
        return "-", "ad-hoc"
    for line in (found.stdout or "").splitlines():
        match = IDENTITY_LINE.search(line)
        if match:
            return match.group(1), match.group(2)
    return "-", "ad-hoc"


def run(runner, cmd):
    runner([str(c) for c in cmd], check=True, capture_output=True, text=True)


def build_icon(macos: Path, resources: Path, work: Path, target: str, runner) -> None:
    tool, big = work / "make_icon", work / "icon_1024.png"
    run(runner, ["swiftc", "-swift-version", "5", "-target", target, "-framework", "AppKit",
                 "-o", tool, macos / "make_icon.swift"])
    run(runner, [tool, big])
    iconset = work / "AppIcon.iconset"
    iconset.mkdir()
    for name, px in ICON_SIZES:
        run(runner, ["sips", "-z", px, px, big, "--out", iconset / ("icon_%s.png" % name)])
    run(runner, ["iconutil", "-c", "icns", "-o", resources / "AppIcon.icns", iconset])


def build_app(src: Path, macos: Path, out: Path, *, identity: str, version: str,
              runner=subprocess.run) -> Path:
    """out（…/Tsuzuki.app）を作る。署名まで済んでから置き換え、失敗しても前の out は壊さない。"""
    src, macos, out = Path(src), Path(macos), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    target = "%s-apple-macos13.0" % platform.machine()
    work = Path(tempfile.mkdtemp(prefix=".Tsuzuki-build-", dir=str(out.parent)))
    try:
        app = work / "Tsuzuki.app"
        resources, macos_dir = app / "Contents" / "Resources", app / "Contents" / "MacOS"
        resources.mkdir(parents=True)
        macos_dir.mkdir()
        (app / "Contents" / "Info.plist").write_bytes(info_plist(version))
        load_build().build(src, resources / "tsuzuki")
        sources = sorted((macos / "Tsuzuki").glob("*.swift"), key=lambda p: p.name)
        run(runner, ["swiftc", "-O", "-swift-version", "5", "-target", target,
                     "-framework", "AppKit", "-framework", "WebKit",
                     "-o", macos_dir / "Tsuzuki"] + sources)
        icon_work = work / "icon"
        icon_work.mkdir()
        build_icon(macos, resources, icon_work, target, runner)
        run(runner, ["codesign", "--force", "--sign", identity, "--timestamp=none", app])
        run(runner, ["codesign", "--verify", "--strict", app])
        backup = work / "previous.app"
        if out.exists():
            os.rename(str(out), str(backup))
        try:
            os.rename(str(app), str(out))
        except OSError:
            if backup.exists():
                os.rename(str(backup), str(out))
            raise
        return out
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


def main() -> int:
    if shutil.which("swiftc") is None:
        print("swiftc が見つかりません。Xcode か Command Line Tools（xcode-select --install）を入れてください",
              file=sys.stderr)
        return 1
    identity, label = signing_identity(os.environ)
    try:
        out = build_app(ROOT / "src", ROOT / "macos", DIST / "Tsuzuki.app",
                        identity=identity, version=read_version(ROOT / "src"))
    except subprocess.CalledProcessError as e:
        cmd = [str(c) for c in e.cmd] if isinstance(e.cmd, (list, tuple)) else [str(e.cmd)]
        print("失敗しました（%s）:\n%s" % (" ".join(cmd[:2]), e.stderr or ""), file=sys.stderr)
        return 1
    print("作りました: %s（署名: %s）" % (out, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
