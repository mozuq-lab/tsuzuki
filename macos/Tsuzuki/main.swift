import AppKit

// サーバーの子プロセスとウィンドウをつなぐ
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var server: ServerProcess?
    private var windowController: MainWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let controller = MainWindowController()
        windowController = controller
        let zipapp = Bundle.main.resourceURL!.appendingPathComponent("tsuzuki")
        server = ServerProcess(zipapp: zipapp) { [weak self] event in
            switch event {
            case .ready(let url):
                self?.windowController?.show(url: url)
            case .failed(let message):
                self?.windowController?.showFailure(message)
            }
        }
        controller.onRetry = { [weak self] in
            self?.startServer()
        }
        controller.showWindow(nil)
        startServer()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        server?.stop()
    }

    // 表示 > 再読み込み（⌘R）
    @objc func reloadPage(_ sender: Any?) {
        windowController?.reload()
    }

    private func startServer() {
        windowController?.showStarting()
        server?.start()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
app.mainMenu = MainMenu.make()
let delegate = AppDelegate()  // NSApplication.delegate は弱参照なので、ここで持ち続ける
app.delegate = delegate
app.run()
