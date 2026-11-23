import Foundation

// アプリの中の tsuzuki を子プロセスの serve として動かし、URL の行を待つ。
// 子の標準入力はこのアプリが持つパイプにつなぐ。アプリが終われば（落ちても）パイプが閉じ、子も終わる
final class ServerProcess {
    enum Event {
        case ready(URL)
        case failed(String)
    }

    private static let python = "/usr/bin/python3"
    private static let urlLinePrefix = "tsuzuki: http://127.0.0.1:"
    private static let removedEnvironment = ["TSUZUKI_HOME", "CLAUDE_CONFIG_DIR", "TSUZUKI_PORT", "PYTHONPATH"]
    private static let stderrLimit = 4000
    private static let startTimeout: TimeInterval = 15
    private static let stopTimeout: TimeInterval = 3

    private let zipapp: URL
    private let onEvent: (Event) -> Void

    // ここから下はメインスレッドだけで触る
    private var process: Process?
    private var stdinPipe: Pipe?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    // start と stop のたびに増やす。前の子から遅れて届いた知らせは、番号が違うので捨てる
    private var generation = 0
    private var isReady = false
    private var hasFailed = false
    private var stdoutBuffer = Data()
    private var stderrTail = Data()
    private var startTimer: DispatchWorkItem?

    init(zipapp: URL, onEvent: @escaping (Event) -> Void) {
        self.zipapp = zipapp
        self.onEvent = onEvent
    }

    func start() {
        if process != nil {
            stop()
        }
        generation += 1
        let current = generation
        isReady = false
        hasFailed = false
        stdoutBuffer = Data()
        stderrTail = Data()

        guard FileManager.default.isExecutableFile(atPath: Self.python) else {
            fail("/usr/bin/python3 が見つかりません。ターミナルで xcode-select --install を実行して、Command Line Tools を入れてください",
                 generation: current)
            return
        }

        let child = Process()
        child.executableURL = URL(fileURLWithPath: Self.python)
        child.arguments = [zipapp.path, "serve", "--port", "0", "--no-open", "--exit-on-stdin-eof"]
        var environment = ProcessInfo.processInfo.environment
        for name in Self.removedEnvironment {
            environment.removeValue(forKey: name)
        }
        child.environment = environment

        let input = Pipe()
        let output = Pipe()
        let errors = Pipe()
        child.standardInput = input
        child.standardOutput = output
        child.standardError = errors

        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil  // EOF。外さないと空読みが続く
                return
            }
            DispatchQueue.main.async { self?.receiveStdout(data, generation: current) }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            DispatchQueue.main.async { self?.receiveStderr(data, generation: current) }
        }
        child.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            let signaled = finished.terminationReason == .uncaughtSignal
            // 標準エラーの残りが先に届くよう、少し待ってから知らせる
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                self?.childExited(status: status, signaled: signaled, generation: current)
            }
        }

        do {
            try child.run()
        } catch {
            output.fileHandleForReading.readabilityHandler = nil
            errors.fileHandleForReading.readabilityHandler = nil
            fail(error.localizedDescription, generation: current)
            return
        }
        process = child
        stdinPipe = input
        stdoutPipe = output
        stderrPipe = errors

        let timer = DispatchWorkItem { [weak self] in
            self?.startTimedOut(generation: current)
        }
        startTimer = timer
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.startTimeout, execute: timer)
    }

    // 同期で止める。パイプを閉じて最大3秒待ち、まだ動いていれば SIGTERM を送る
    func stop() {
        generation += 1  // 止める印。これより後に届く今の子の知らせは捨てる
        startTimer?.cancel()
        startTimer = nil
        let child = process
        process = nil
        try? stdinPipe?.fileHandleForWriting.close()
        stdinPipe = nil
        if let child = child {
            let deadline = Date().addingTimeInterval(Self.stopTimeout)
            while child.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
            if child.isRunning {
                child.terminate()
            }
        }
        // 読み手は待った後に外す。先に外すと、終わり際に64KB以上書く子が詰まる
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        stdoutPipe = nil
        stderrPipe = nil
    }

    private func receiveStdout(_ data: Data, generation current: Int) {
        guard current == generation, !isReady else { return }
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of: UInt8(ascii: "\n")) {
            let line = String(decoding: stdoutBuffer[stdoutBuffer.startIndex..<newline], as: UTF8.self)
            stdoutBuffer.removeSubrange(stdoutBuffer.startIndex...newline)
            let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
            guard text.hasPrefix(Self.urlLinePrefix),
                  let url = URL(string: String(text.dropFirst("tsuzuki: ".count))) else { continue }
            isReady = true
            stdoutBuffer = Data()
            startTimer?.cancel()
            startTimer = nil
            onEvent(.ready(url))
            return
        }
    }

    private func receiveStderr(_ data: Data, generation current: Int) {
        guard current == generation else { return }
        stderrTail.append(data)
        // 字の数は最後に数える。ここではバイトで多めに残す（UTF-8 は1字が最大4バイト）
        if stderrTail.count > Self.stderrLimit * 4 {
            stderrTail.removeFirst(stderrTail.count - Self.stderrLimit * 4)
        }
    }

    private func startTimedOut(generation current: Int) {
        guard current == generation, !isReady else { return }
        let message = withStderr("15秒待っても起動しませんでした")
        stop()
        fail(message, generation: generation)
    }

    private func childExited(status: Int32, signaled: Bool, generation current: Int) {
        guard current == generation else { return }  // stop() を呼んだ後なら知らせない
        startTimer?.cancel()
        startTimer = nil
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        stdoutPipe = nil
        stderrPipe = nil
        try? stdinPipe?.fileHandleForWriting.close()
        stdinPipe = nil
        process = nil
        let reason = signaled ? "シグナル \(status)" : "終了コード \(status)"
        fail(withStderr("Tsuzuki のサーバーが終わりました（\(reason)）"), generation: current)
    }

    private func withStderr(_ message: String) -> String {
        let tail = String(String(decoding: stderrTail, as: UTF8.self).suffix(Self.stderrLimit))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return tail.isEmpty ? message : message + "\n\n" + tail
    }

    // 1回の start で1回だけ知らせる
    private func fail(_ message: String, generation current: Int) {
        guard current == generation, !hasFailed else { return }
        hasFailed = true
        onEvent(.failed(message))
    }
}
