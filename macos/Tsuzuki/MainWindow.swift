import AppKit
import WebKit

// WKUserContentController は受け手を強く持つ。そのままだとウィンドウが自分を離さなくなるので、弱参照で中継する
private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?

    init(_ target: WKScriptMessageHandler) {
        self.target = target
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(userContentController, didReceive: message)
    }
}

// ウィンドウは1つ。WKWebView に、起動中・失敗の表示と、サーバーの画面を出す
final class MainWindowController: NSWindowController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var onRetry: (() -> Void)?

    private static let themeDefaultsKey = "theme"
    private static let themeStorageKey = "tsuzuki-theme"
    private static let themeMessage = "tsuzukiTheme"
    private static let retryMessage = "tsuzukiRetry"

    private let webView: WKWebView
    private var serverURL: URL?
    private var showingFailure = false

    init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        let frame = NSRect(x: 0, y: 0, width: 1280, height: 800)
        webView = WKWebView(frame: frame, configuration: configuration)
        webView.allowsBackForwardNavigationGestures = true

        let window = NSWindow(contentRect: frame,
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered,
                              defer: false)
        window.title = "Tsuzuki"
        window.contentMinSize = NSSize(width: 800, height: 500)
        window.isReleasedWhenClosed = false  // 持ち主はこのコントローラー
        window.contentView = webView
        window.center()
        super.init(window: window)
        // コントローラーに渡した後に付ける（先に付けると、コントローラーの空の名前で消される）。
        // 覚えた大きさと位置があれば、それで出す。ずらして出す（cascade）と覚えた位置からずれるので、しない
        shouldCascadeWindows = false
        window.setFrameAutosaveName("TsuzukiMainWindow")

        let relay = WeakScriptMessageHandler(self)
        let contents = webView.configuration.userContentController
        contents.add(relay, name: Self.themeMessage)
        contents.add(relay, name: Self.retryMessage)
        webView.navigationDelegate = self
        webView.uiDelegate = self
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not used")
    }

    func showStarting() {
        showingFailure = false
        serverURL = nil
        webView.loadHTMLString(Self.page(body: "<p class=\"starting\">起動しています…</p>"), baseURL: nil)
    }

    func show(url: URL) {
        showingFailure = false
        serverURL = url
        installThemeScript()
        webView.load(URLRequest(url: url))
    }

    func showFailure(_ message: String) {
        showingFailure = true
        serverURL = nil
        let body = """
            <h1>起動できませんでした</h1>
            <pre>\(Self.escapeHTML(message))</pre>
            <button type="button" onclick="window.webkit.messageHandlers.tsuzukiRetry.postMessage('')">もう一度</button>
            """
        webView.loadHTMLString(Self.page(body: body), baseURL: nil)
    }

    func reload() {
        if showingFailure {
            onRetry?()
        } else {
            webView.reload()
        }
    }

    // MARK: - WKScriptMessageHandler

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame else { return }  // 資料の枠の中からは受けない
        switch message.name {
        case Self.themeMessage:
            UserDefaults.standard.set(Self.validTheme(message.body as? String), forKey: Self.themeDefaultsKey)
            installThemeScript()  // 入れ直さないと、読み込み直したときに前の値へ戻る
        case Self.retryMessage:
            if showingFailure {
                onRetry?()
            }
        default:
            break
        }
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url, let scheme = url.scheme?.lowercased() else {
            decisionHandler(.cancel)
            return
        }
        // 戻る・進むでサーバーの外（起動中・失敗の表示の about:blank や、前のポート）へは出さない
        if navigationAction.navigationType == .backForward {
            decisionHandler(isServer(url) ? .allow : .cancel)
            return
        }
        if isServer(url) || scheme == "about" {
            decisionHandler(.allow)
            return
        }
        // 資料の HTML は信頼できないので、押したリンクだけを開く（スクリプトによる移動は取り消すだけ）
        if navigationAction.navigationType == .linkActivated,
           scheme == "http" || scheme == "https" || scheme == "mailto" {
            NSWorkspace.shared.open(url)  // 外は既定のブラウザ（メールのアプリ）で開く
        }
        decisionHandler(.cancel)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        reload()
    }

    // MARK: - WKUIDelegate

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        // 資料の HTML は信頼できないので、押したリンクだけを開く（スクリプトの window.open は開かない）
        if navigationAction.navigationType == .linkActivated,
           let url = navigationAction.request.url, let scheme = url.scheme?.lowercased(),
           scheme == "http" || scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        return nil  // 新しいウィンドウは作らない
    }

    // MARK: - 中身

    // 今のサーバーと同じ scheme・host・port か
    private func isServer(_ url: URL) -> Bool {
        guard let server = serverURL else { return false }
        return url.scheme?.lowercased() == server.scheme?.lowercased()
            && url.host == server.host
            && url.port == server.port
    }

    // "light"・"dark" のほかは自動（空）とみなす
    private static func validTheme(_ value: String?) -> String {
        guard let value = value, value == "light" || value == "dark" else { return "" }
        return value
    }

    private static func savedTheme() -> String {
        validTheme(UserDefaults.standard.string(forKey: themeDefaultsKey))
    }

    // 覚えた表示を、読み込みの最初に localStorage へ入れる（ポートが毎回変わり、localStorage が引き継がれないため）
    private func installThemeScript() {
        let contents = webView.configuration.userContentController
        contents.removeAllUserScripts()
        contents.addUserScript(WKUserScript(source: Self.themeScript(Self.savedTheme()),
                                            injectionTime: .atDocumentStart,
                                            forMainFrameOnly: true))
    }

    private static func themeScript(_ theme: String) -> String {
        let key = jsonString(themeStorageKey)
        let statement = theme.isEmpty
            ? "localStorage.removeItem(\(key));"
            : "localStorage.setItem(\(key), \(jsonString(theme)));"
        return "try { \(statement) } catch (e) {}"
    }

    // 文字列を JSON（そのまま JavaScript の文字列）の書き方にする
    private static func jsonString(_ text: String) -> String {
        guard let data = try? JSONEncoder().encode(text) else { return "\"\"" }
        return String(decoding: data, as: UTF8.self)
    }

    private static func escapeHTML(_ text: String) -> String {
        text.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }

    private static func page(body: String) -> String {
        """
        <!doctype html>
        <html lang="ja">
        <head>
        <meta charset="utf-8">
        <meta name="color-scheme" content="light dark">
        <style>
        :root { --bg: #ffffff; --fg: #1d1d1f; --muted: #6e6e73; --box: #f2f2f4; --line: #d2d2d7; }
        @media (prefers-color-scheme: dark) {
          :root { --bg: #1e1e1e; --fg: #f2f2f2; --muted: #a1a1a6; --box: #2a2a2c; --line: #3a3a3c; }
        }
        body { margin: 0; padding: 48px; background: var(--bg); color: var(--fg);
               font: 14px -apple-system, "Hiragino Sans", sans-serif; }
        .starting { color: var(--muted); text-align: center; margin-top: 30vh; }
        h1 { font-size: 20px; margin: 0 0 16px; }
        pre { white-space: pre-wrap; word-break: break-word; background: var(--box); border: 1px solid var(--line);
              border-radius: 6px; padding: 12px; max-height: 50vh; overflow: auto; font: 12px ui-monospace, Menlo, monospace; }
        button { margin-top: 16px; font: inherit; padding: 4px 16px; }
        </style>
        </head>
        <body>
        \(body)
        </body>
        </html>
        """
    }
}
