"""パスが別のフォルダーの中かの判定。標準のライブラリだけ。"""
import os


def is_inside(path: str, base: str) -> bool:
    """path の実体（リンクを解いた先）が base と同じか、その中か。大文字小文字の違う綴り（APFS）や、同じ場所の別名
    （/System/Volumes/Data など）でも見抜くために、path の実体の存在する祖先を1つずつ base と同じ物か
    （st_dev・st_ino）で比べる。比べられないときは、実体のパスの文字で比べる。"""
    real = os.path.realpath(path)
    try:
        b = os.stat(base)
        want = (b.st_dev, b.st_ino)
    except OSError:
        want = None
    if want is not None:
        current = real
        while True:
            try:
                st = os.stat(current)
                if (st.st_dev, st.st_ino) == want:
                    return True
            except OSError:
                pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    root = os.path.realpath(base)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)
