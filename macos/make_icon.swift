import AppKit

// アプリのアイコン（1024x1024 の PNG）を、引数のパスに書く
guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("使い方: make_icon <出力する PNG のパス>\n".data(using: .utf8)!)
    exit(2)
}

let size = 1024
// 画素数をそのまま 1024 にするため、NSImage ではなくビットマップに直接描く
guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
), let context = NSGraphicsContext(bitmapImageRep: rep) else {
    FileHandle.standardError.write("描画の準備に失敗しました\n".data(using: .utf8)!)
    exit(1)
}
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context

let full = CGFloat(size)
let rect = NSRect(x: 0, y: 0, width: full, height: full)
let radius = full * 0.22
NSColor(calibratedRed: 0.17, green: 0.23, blue: 0.45, alpha: 1).setFill()
NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()

let text = NSAttributedString(string: "続", attributes: [
    .font: NSFont.systemFont(ofSize: full * 0.6, weight: .semibold),
    .foregroundColor: NSColor.white,
])
let textSize = text.size()
text.draw(at: NSPoint(x: (full - textSize.width) / 2, y: (full - textSize.height) / 2))
NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else {
    FileHandle.standardError.write("PNG にできませんでした\n".data(using: .utf8)!)
    exit(1)
}
do {
    try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
} catch {
    FileHandle.standardError.write("書けませんでした: \(error)\n".data(using: .utf8)!)
    exit(1)
}
