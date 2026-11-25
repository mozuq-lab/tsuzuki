"""macOS のファイルの保護で読めないときの案内。tsuzuki の他のモジュールには依存しない。"""
import errno

PRIVACY_HINT = ("macOS がこのファイルの読み取りを止めているかもしれません。システム設定の「プライバシーとセキュリティ」→"
                "「ファイルとフォルダ」で Tsuzuki の「リムーバブルボリューム」を入れるか、「フルディスクアクセス」に "
                "Tsuzuki を足してください（端末から tsuzuki serve を起動しているときは、その端末のアプリに）")


WRITE_HINT = ("macOS がこのファイルへの書き込みを止めているかもしれません。Finder の「情報を見る」で「ロック」が外れているか、"
              "システム設定の「プライバシーとセキュリティ」→「ファイルとフォルダ」で Tsuzuki の「リムーバブルボリューム」が"
              "入っているかを確かめてください")


def os_error_text(e: OSError, *, writing: bool = False) -> str:
    """OSError の文。EPERM（macOS の保護が止めたときに出る。ふつうの権限不足の EACCES ではない）なら、許可の場所を添える。
    書くときの EPERM は、Finder のロック（uchg）のこともあるので、その確かめ方を添える。"""
    if e.errno == errno.EPERM:
        return "%s。%s" % (e, WRITE_HINT if writing else PRIVACY_HINT)
    return str(e)
