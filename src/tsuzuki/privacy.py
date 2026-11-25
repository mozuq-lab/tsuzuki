"""macOS のファイルの保護で読めないときの案内。tsuzuki の他のモジュールには依存しない。"""
import errno

PRIVACY_HINT = ("macOS がこのファイルの読み取りを止めているかもしれません。システム設定の「プライバシーとセキュリティ」→"
                "「ファイルとフォルダ」で Tsuzuki の「リムーバブルボリューム」を入れるか、「フルディスクアクセス」に "
                "Tsuzuki を足してください（端末から tsuzuki serve を起動しているときは、その端末のアプリに）")


def os_error_text(e: OSError) -> str:
    """OSError の文。EPERM（macOS の保護が止めたときに出る。ふつうの権限不足の EACCES ではない）なら、許可の場所を添える。"""
    if e.errno == errno.EPERM:
        return "%s。%s" % (e, PRIVACY_HINT)
    return str(e)
