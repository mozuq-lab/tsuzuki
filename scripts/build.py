#!/usr/bin/env python3
"""src/tsuzuki を1つの実行ファイル（zipapp）にまとめる。

    python3 scripts/build.py

dist/tsuzuki を作る。画面の HTML・CSS・JavaScript も中に入る。
"""
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTERPRETER = "/usr/bin/env python3"
# zipapp の main= で作る __main__.py は main() の戻り値を捨て、終了コードがいつも 0 になる。そのため自分で書く
MAIN = "import sys\nfrom tsuzuki.cli import main\nsys.exit(main())\n"


def build(src: Path = Path("src"), out: Path = Path("dist/tsuzuki")) -> Path:
    """src/tsuzuki（__pycache__ と .pyc を除く）と __main__.py を out にまとめ、権限を 0755 にする。"""
    src, out = Path(src), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        shutil.copytree(str(src / "tsuzuki"), str(staging / "tsuzuki"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staging / "__main__.py").write_text(MAIN, encoding="utf-8")
        zipapp.create_archive(str(staging), str(out), interpreter=INTERPRETER, compressed=True)
    out.chmod(0o755)
    return out


def main() -> int:
    # どこから実行しても、リポジトリの src から dist/tsuzuki を作る
    out = build(ROOT / "src", ROOT / "dist" / "tsuzuki")
    print("作りました: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
