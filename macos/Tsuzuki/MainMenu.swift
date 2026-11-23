import AppKit
import WebKit

// メニュー。編集のメニューがないと、WKWebView の入力欄で ⌘C・⌘V が効かない
enum MainMenu {
    static func make() -> NSMenu {
        let main = NSMenu()

        let app = submenu(in: main, title: "Tsuzuki")
        app.addItem(item("Tsuzuki について", #selector(NSApplication.orderFrontStandardAboutPanel(_:))))
        app.addItem(.separator())
        app.addItem(item("Tsuzuki を隠す", #selector(NSApplication.hide(_:)), key: "h"))
        app.addItem(item("ほかを隠す", #selector(NSApplication.hideOtherApplications(_:)), key: "h",
                         modifiers: [.command, .option]))
        app.addItem(item("すべてを表示", #selector(NSApplication.unhideAllApplications(_:))))
        app.addItem(.separator())
        app.addItem(item("Tsuzuki を終了", #selector(NSApplication.terminate(_:)), key: "q"))

        let edit = submenu(in: main, title: "編集")
        edit.addItem(item("取り消す", Selector(("undo:")), key: "z"))
        edit.addItem(item("やり直す", Selector(("redo:")), key: "z", modifiers: [.command, .shift]))
        edit.addItem(.separator())
        edit.addItem(item("カット", #selector(NSText.cut(_:)), key: "x"))
        edit.addItem(item("コピー", #selector(NSText.copy(_:)), key: "c"))
        edit.addItem(item("ペースト", #selector(NSText.paste(_:)), key: "v"))
        edit.addItem(item("すべてを選択", #selector(NSText.selectAll(_:)), key: "a"))

        let view = submenu(in: main, title: "表示")
        view.addItem(item("再読み込み", Selector(("reloadPage:")), key: "r"))  // AppDelegate が受ける
        view.addItem(.separator())
        // target なし。ウィンドウの中身が WKWebView なので、レスポンダーチェーンで届く
        view.addItem(item("戻る", #selector(WKWebView.goBack(_:)), key: "["))
        view.addItem(item("進む", #selector(WKWebView.goForward(_:)), key: "]"))

        let window = submenu(in: main, title: "ウィンドウ")
        window.addItem(item("閉じる", #selector(NSWindow.performClose(_:)), key: "w"))
        window.addItem(item("しまう", #selector(NSWindow.performMiniaturize(_:)), key: "m"))
        window.addItem(item("拡大／縮小", #selector(NSWindow.performZoom(_:))))
        NSApp.windowsMenu = window

        return main
    }

    private static func submenu(in main: NSMenu, title: String) -> NSMenu {
        let menu = NSMenu(title: title)
        let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        holder.submenu = menu
        main.addItem(holder)
        return menu
    }

    private static func item(_ title: String, _ action: Selector, key: String = "",
                             modifiers: NSEvent.ModifierFlags = .command) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.keyEquivalentModifierMask = modifiers
        return item
    }
}
