// Grab.app - native macOS shell around the grab GUI.
// Starts `.venv/bin/grab-gui` from the project folder (path stored in Info.plist as GrabProjectDir)
// and shows its page in a WKWebView window, with a native folder picker and a Dock badge.

import AppKit
import UserNotifications
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate,
                         NSMenuDelegate, NSWindowDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var serverURL: URL?
    var outputBuffer = ""
    var activeDownloads = 0
    var pageReady = false
    var pendingJS: [String] = []          // calls waiting for the page to finish loading
    var lastPasteboardChange = NSPasteboard.general.changeCount
    var askedForNotifications = false
    // menu bar
    var statusItem: NSStatusItem!
    var lastJobs: [[String: Any]] = []
    var lastStatus: [Int: String] = [:]
    var pollTimer: Timer?
    var flash: (state: MenuIcon.State, text: String, until: Date)?  // brief "Done" / "Failed" after a download ends

    var projectDir: String {
        Bundle.main.object(forInfoDictionaryKey: "GrabProjectDir") as? String ?? ""
    }

    /// The engine the app runs: its own copy in ~/Library/Application Support/Grab/runtime, so it never needs
    /// access to the (privacy-protected) Documents folder the project lives in.
    var runtimeDir: String {
        Bundle.main.object(forInfoDictionaryKey: "GrabRuntime") as? String ?? (projectDir as NSString).appendingPathComponent(".venv")
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildWindow()
        buildStatusItem()
        startServer()
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: window

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "grab")
        if #available(macOS 14.0, *) {
            // By default WebKit freezes a web view whose window is in the background, which left Grab's
            // window blank. Throttling keeps it alive (just slower) so it's ready when you look at it.
            config.preferences.inactiveSchedulingPolicy = .throttle
        }
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        webView.loadHTMLString(
            "<body style='font:15px -apple-system;color:#888;display:grid;place-items:center;height:90vh;background:transparent'>Starting grab…</body>",
            baseURL: nil)

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Grab"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor(srgbRed: 0x11 / 255.0, green: 0x13 / 255.0, blue: 0x17 / 255.0, alpha: 1)
                : NSColor(srgbRed: 0xf5 / 255.0, green: 0xf6 / 255.0, blue: 0xf8 / 255.0, alpha: 1)
        }
        window.minSize = NSSize(width: 460, height: 520)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("GrabMainWindow")
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Grab", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Grab", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Grab", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Open Download Folder", action: #selector(openDownloads), keyEquivalent: "o")
        fileMenu.addItem(withTitle: "Paste Links and Download", action: #selector(pasteAndGo), keyEquivalent: "V")
        fileMenu.addItem(withTitle: "Pause All", action: #selector(pauseAll), keyEquivalent: "")
        fileMenu.addItem(withTitle: "Resume All", action: #selector(resumeAll), keyEquivalent: "")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        // Without an Edit menu, ⌘C/⌘V/⌘A don't work inside the web view.
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Find Download", action: #selector(findDownload), keyEquivalent: "f")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let winItem = NSMenuItem()
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Show Grab", action: #selector(showWindow), keyEquivalent: "1")
        winItem.submenu = winMenu
        main.addItem(winItem)
        NSApp.windowsMenu = winMenu

        NSApp.mainMenu = main
    }

    @objc func openSettings() { showWindow(); runJS("openSettings()") }
    @objc func findDownload() { runJS("document.querySelector('#q').focus()") }
    @objc func pauseAll() { runJS("document.querySelector('#pauseAll').click()") }
    @objc func resumeAll() { runJS("document.querySelector('#resumeAll').click()") }

    /// Run JavaScript now, or as soon as the page has loaded.
    func runJS(_ js: String) {
        if pageReady { webView.evaluateJavaScript(js) } else { pendingJS.append(js) }
    }

    func jsString(_ s: String) -> String {
        String(data: try! JSONSerialization.data(withJSONObject: [s]), encoding: .utf8)! + "[0]"
    }

    @objc func reload() {
        pageReady = false
        if let u = serverURL { webView.load(URLRequest(url: u)) }
    }

    @objc func showWindow() {
        // Links that launch the app arrive before applicationDidFinishLaunching has built the window.
        guard let window else { return }
        NSApp.setActivationPolicy(.regular)  // back in the Dock while the window is open
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func openDownloads() {
        webView.evaluateJavaScript("document.querySelector('#openDir')?.click()")
    }

    @objc func pasteAndGo() {
        guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else { return }
        showWindow()
        lastPasteboardChange = NSPasteboard.general.changeCount
        runJS("window.grabPasteAndGo(\(jsString(text)))")
    }

    // MARK: server process

    func startServer() {
        let bin = (runtimeDir as NSString).appendingPathComponent("bin/grab-gui")
        guard FileManager.default.isExecutableFile(atPath: bin) else {
            fail("Couldn't find grab at\n\(bin)\n\nIn the project folder run ./build_app.sh --install to set it up again.")
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bin)
        p.arguments = ["--no-browser", "--port", "8765", "--exit-with-parent"]
        p.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory())
        var env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(home)/.local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env

        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let s = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.handleServerOutput(s) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self, self.serverURL == nil else { return }
                self.fail("grab stopped (exit \(proc.terminationStatus)).\n\n\(self.outputBuffer.suffix(1500))")
            }
        }
        do {
            try p.run()
            server = p
        } catch {
            fail("Couldn't start grab: \(error.localizedDescription)")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            guard let self, self.serverURL == nil, self.server?.isRunning == true else { return }
            self.fail("grab didn't start within 20 seconds.\n\n\(self.outputBuffer.suffix(1500))")
        }
    }

    func handleServerOutput(_ s: String) {
        outputBuffer += s
        guard serverURL == nil,
              let r = outputBuffer.range(of: #"http://127\.0\.0\.1:\d+/"#, options: .regularExpression),
              let u = URL(string: String(outputBuffer[r])) else { return }
        serverURL = u
        webView.load(URLRequest(url: u))
    }

    func fail(_ msg: String) {
        let a = NSAlert()
        a.messageText = "Grab couldn't start"
        a.informativeText = msg
        a.alertStyle = .critical
        a.runModal()
        NSApp.terminate(nil)
    }

    // MARK: messages from the page

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "pickFolder":
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = true
            panel.prompt = "Save Here"
            if let cur = body["current"] as? String, !cur.isEmpty {
                panel.directoryURL = URL(fileURLWithPath: (cur as NSString).expandingTildeInPath)
            }
            panel.beginSheetModal(for: window) { [weak self] resp in
                guard resp == .OK, let path = panel.url?.path else { return }
                guard let self else { return }
                self.webView.evaluateJavaScript("window.grabSetFolder(\(self.jsString(path)))")
            }
        case "readClipboard":
            let text = NSPasteboard.general.string(forType: .string) ?? ""
            let then = body["then"] as? String ?? "add"
            runJS("window.grabClipboardResult(\(jsString(String(text.prefix(50000)))), \(jsString(then)))")
        case "ready":
            pageReady = true
            let queued = pendingJS
            pendingJS = []
            queued.forEach { webView.evaluateJavaScript($0) }
            checkClipboard(force: true)
        case "status":
            break  // the menu-bar poller below reads status straight from the engine (works with the window closed)
        default:
            break
        }
    }

    // Open target=_blank / external links in the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url { NSWorkspace.shared.open(u) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = action.request.url, let host = u.host, host != "127.0.0.1", action.navigationType == .linkActivated {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // MARK: clipboard, grab:// links, notifications

    /// Offer links the user copied elsewhere, once per clipboard change. Only *detects* that the
    /// clipboard looks like a link (no privacy prompt); the text is read when the user clicks the banner.
    func checkClipboard(force: Bool = false) {
        let pb = NSPasteboard.general
        guard force || pb.changeCount != lastPasteboardChange else { return }
        lastPasteboardChange = pb.changeCount
        pb.__detectPatterns(forPatterns: [.__probableWebURL]) { [weak self] found, _ in
            guard found?.contains(.__probableWebURL) == true else { return }
            DispatchQueue.main.async { self?.runJS("window.grabClipboardHint()") }
        }
    }

    func applicationDidBecomeActive(_ note: Notification) {
        ensurePageAlive()
        if pageReady { checkClipboard() }
    }

    // MARK: keeping the window's page alive

    /// macOS may stop a background window's web page; if the page is gone or blank, load it again.
    func ensurePageAlive() {
        guard serverURL != nil, pageReady else { return }
        var answered = false
        webView.evaluateJavaScript("document.querySelector('#urls') ? 1 : 0") { [weak self] result, error in
            answered = true
            if error != nil || (result as? Int) != 1 { self?.reload() }
        }
        // a frozen or wedged page doesn't answer at all: reload it
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            if !answered { self?.reload() }
        }
    }

    func windowDidBecomeKey(_ notification: Notification) {
        ensurePageAlive()
    }

    func windowDidChangeOcclusionState(_ notification: Notification) {
        if window.occlusionState.contains(.visible) { ensurePageAlive() }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        reload()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    func retryLoad() {
        guard serverURL != nil, server?.isRunning == true else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.reload() }
    }

    // grab://add?url=…  (from the "Send to Grab" bookmarklet)
    func application(_ application: NSApplication, open urls: [URL]) {
        for u in urls where u.scheme == "grab" {
            let items = URLComponents(url: u, resolvingAgainstBaseURL: false)?.queryItems ?? []
            func value(_ name: String) -> String { items.first(where: { $0.name == name })?.value ?? "" }
            let link = value("url")
            guard link.hasPrefix("http://") || link.hasPrefix("https://") else { continue }
            // streams sent by the browser extension also carry the page they played on and its title
            let referer = value("referer")
            let extra: [String: String] = [
                "referer": referer.hasPrefix("http://") || referer.hasPrefix("https://") ? referer : "",
                "title": String(value("title").prefix(200)),
                "key": String(value("key").prefix(64)),  // checked by the page against Grab's private key
            ]
            let extraJSON = String(data: try! JSONSerialization.data(withJSONObject: extra), encoding: .utf8)!
            showWindow()
            runJS("window.grabIncoming(\(jsString(link)), \(extraJSON))")
        }
    }

    func notify(_ finished: [[String: Any]]) {
        if NSApp.isActive && window.isKeyWindow { return }
        let center = UNUserNotificationCenter.current()
        if !askedForNotifications {
            askedForNotifications = true
            center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        }
        let ok = finished.filter { ($0["status"] as? String) == "done" }
        let failed = finished.count - ok.count
        let content = UNMutableNotificationContent()
        if finished.count == 1 {
            content.title = ok.isEmpty ? "Download failed" : "Downloaded"
            content.body = finished[0]["title"] as? String ?? ""
        } else {
            content.title = "\(ok.count) download\(ok.count == 1 ? "" : "s") finished" + (failed > 0 ? ", \(failed) failed" : "")
            content.body = ok.prefix(3).compactMap { $0["title"] as? String }.joined(separator: "\n")
        }
        content.sound = .default
        center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        NSApp.requestUserAttention(.informationalRequest)
    }

    // MARK: lifecycle

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    // Closing the window keeps downloads going; quitting stops them.
    // MARK: menu bar

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.autosaveName = "GrabStatusItem"  // keeps the position you drag it to
        if let b = statusItem.button {
            b.image = MenuIcon.image(.idle)
            b.imagePosition = .imageLeading
            b.toolTip = "Grab"
        }
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.poll() }
        RunLoop.main.add(pollTimer!, forMode: .common)
    }

    /// Read job status from the engine every 2 s: menu-bar title, Dock badge, finish notifications.
    func poll() {
        guard let base = serverURL, let url = URL(string: "api/jobs", relativeTo: base) else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let jobs = obj["jobs"] as? [[String: Any]] else { return }
            DispatchQueue.main.async { self?.update(jobs) }
        }.resume()
    }

    func update(_ jobs: [[String: Any]]) {
        lastJobs = jobs
        let running = jobs.filter { ($0["status"] as? String) == "running" }
        let queued = jobs.filter { ($0["status"] as? String) == "queued" }.count
        activeDownloads = running.count + queued
        NSApp.dockTile.badgeLabel = activeDownloads > 0 ? String(activeDownloads) : nil
        let pcts = running.compactMap { $0["progress"] as? Double }
        let speed = running.compactMap { $0["speed"] as? Double }.reduce(0, +)
        let overall: Double? = pcts.isEmpty ? nil : pcts.reduce(0, +) / Double(pcts.count)
        // notifications for downloads that just finished
        var finished: [[String: Any]] = []
        for j in jobs {
            guard let id = j["id"] as? Int, let st = j["status"] as? String else { continue }
            if let was = lastStatus[id], was != st, was == "running" || was == "queued",
               st == "done" || st == "error", (j["children"] as? Int ?? 0) == 0 {
                finished.append(["title": j["title"] ?? "", "status": st])
            }
            lastStatus[id] = st
        }
        if !finished.isEmpty {
            notify(finished)
            let failed = finished.filter { ($0["status"] as? String) == "error" }.count
            flash = failed > 0 ? (.failed, failed == 1 ? "Failed" : "\(failed) failed", Date().addingTimeInterval(8))
                               : (.done, finished.count == 1 ? "Done" : "\(finished.count) done", Date().addingTimeInterval(8))
        }
        // the menu-bar icon + text
        guard let b = statusItem.button else { return }
        var state = MenuIcon.State.idle
        var text = ""
        var color = NSColor.labelColor
        if !running.isEmpty {
            state = .active(overall ?? 0, indeterminate: overall == nil)
            let pct = overall.map { "\(Int($0))%" } ?? "…"
            text = (running.count > 1 ? "\(running.count)× " : "") + pct
            if speed > 0 { text += " · " + ByteCountFormatter.string(fromByteCount: Int64(speed), countStyle: .file) + "/s" }
        } else if let f = flash, f.until > Date() {
            state = f.state
            text = f.text
            color = f.state == .failed ? .systemRed : .systemGreen
        } else if queued > 0 {
            state = .waiting
            text = "\(queued) waiting"
            color = .secondaryLabelColor
        }
        if case .active = state, flash != nil { flash = nil }
        b.image = MenuIcon.image(state)
        let font = NSFont.monospacedDigitSystemFont(ofSize: 12.5, weight: .semibold)
        b.attributedTitle = NSAttributedString(string: text.isEmpty ? "" : " " + text,
                                               attributes: [.font: font, .foregroundColor: color, .baselineOffset: 0.5])
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let running = lastJobs.filter { ($0["status"] as? String) == "running" }
        let queued = lastJobs.filter { ($0["status"] as? String) == "queued" }.count
        let paused = lastJobs.filter { ($0["status"] as? String) == "paused" }.count
        let speed = running.compactMap { $0["speed"] as? Double }.reduce(0, +)
        let head = running.isEmpty ? (queued > 0 ? "\(queued) waiting" : "Nothing downloading")
            : "\(running.count) downloading · \(ByteCountFormatter.string(fromByteCount: Int64(speed), countStyle: .file))/s"
            + (queued > 0 ? " · \(queued) queued" : "")
        let h = NSMenuItem(title: head, action: nil, keyEquivalent: "")
        h.isEnabled = false
        menu.addItem(h)
        for j in running.prefix(6) {
            var title = (j["title"] as? String) ?? ""
            if title.count > 46 { title = String(title.prefix(45)) + "…" }
            var detail = ""
            if let p = j["progress"] as? Double { detail += "\(Int(p))%" }
            if let eta = j["eta"] as? Int { detail += " · \(eta >= 3600 ? "\(eta / 3600)h \(eta % 3600 / 60)m" : "\(eta / 60):\(String(format: "%02d", eta % 60))") left" }
            let item = NSMenuItem(title: "\(title)   \(detail)", action: #selector(showWindow), keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let paste = NSMenuItem(title: "Paste Link and Download", action: #selector(menuPaste), keyEquivalent: "")
        paste.target = self
        menu.addItem(paste)
        let open = NSMenuItem(title: "Open Grab", action: #selector(showWindow), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        if !running.isEmpty || queued > 0 {
            let p = NSMenuItem(title: "Pause All", action: #selector(menuPauseAll), keyEquivalent: "")
            p.target = self
            menu.addItem(p)
        }
        if paused > 0 {
            let r = NSMenuItem(title: "Resume All (\(paused))", action: #selector(menuResumeAll), keyEquivalent: "")
            r.target = self
            menu.addItem(r)
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Grab", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
    }

    func post(_ path: String, _ body: [String: Any] = [:]) {
        guard let base = serverURL, let url = URL(string: path, relativeTo: base) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in DispatchQueue.main.async { self?.poll() } }.resume()
    }

    @objc func menuPauseAll() { post("api/pause") }

    /// From the menu bar: download the copied link without opening the window.
    @objc func menuPaste() {
        guard let text = NSPasteboard.general.string(forType: .string), text.contains("http") else {
            NSSound.beep()
            return
        }
        lastPasteboardChange = NSPasteboard.general.changeCount
        post("api/quick_add", ["urls": text])  // straight to the engine: works even while the window is closed
    }
    @objc func menuResumeAll() { post("api/resume") }

    // Closing the window turns Grab into a menu-bar app (no Dock icon) that keeps downloading.
    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }


    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard activeDownloads > 0 else { return .terminateNow }
        let a = NSAlert()
        a.messageText = "\(activeDownloads) download\(activeDownloads == 1 ? " is" : "s are") still running"
        a.informativeText = "Quitting cancels them. Unfinished direct-file downloads resume next time."
        a.addButton(withTitle: "Keep Downloading")
        a.addButton(withTitle: "Quit")
        return a.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ note: Notification) {
        guard let p = server, p.isRunning else { return }
        p.interrupt()  // SIGINT: grab cancels jobs and stops aria2c / gallery-dl children
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { p.terminate() }
    }
}

// MARK: - menu-bar icon drawing

enum MenuIcon {
    enum State: Equatable {
        case idle, waiting, done, failed
        case active(Double, indeterminate: Bool)
    }

    static let blue = NSColor(srgbRed: 0.27, green: 0.45, blue: 1.0, alpha: 1)

    /// 18 pt icons drawn in colour (not a grey template) so Grab stands out in the menu bar.
    static func image(_ state: State) -> NSImage {
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { r in
            let c = NSPoint(x: r.midX, y: r.midY)
            switch state {
            case .idle, .waiting, .done, .failed:
                let fill: NSColor = state == .done ? .systemGreen : state == .failed ? .systemRed
                    : state == .waiting ? NSColor.systemGray : blue
                fill.setFill()
                NSBezierPath(ovalIn: r.insetBy(dx: 1, dy: 1)).fill()
                NSColor.white.setStroke()
                NSColor.white.setFill()
                if state == .done {
                    stroke([NSPoint(x: 5.2, y: 9.2), NSPoint(x: 7.8, y: 6.6), NSPoint(x: 12.8, y: 11.8)], width: 2)
                } else if state == .failed {
                    stroke([NSPoint(x: 9, y: 13), NSPoint(x: 9, y: 8)], width: 2.2)
                    NSBezierPath(ovalIn: NSRect(x: 7.9, y: 4.1, width: 2.2, height: 2.2)).fill()
                } else {
                    arrow(in: c, color: .white, size: 1)
                }
            case let .active(p, indeterminate):
                let track = NSBezierPath()
                track.appendArc(withCenter: c, radius: 7.6, startAngle: 0, endAngle: 360)
                track.lineWidth = 2.2
                NSColor.tertiaryLabelColor.setStroke()
                track.stroke()
                let frac = indeterminate ? 0.3 : max(0.04, min(1, p / 100))
                let start: CGFloat = indeterminate ? CGFloat(Date().timeIntervalSince1970.truncatingRemainder(dividingBy: 1) * -360) : 90
                let arc = NSBezierPath()
                arc.appendArc(withCenter: c, radius: 7.6, startAngle: start, endAngle: start - CGFloat(frac * 360), clockwise: true)
                arc.lineWidth = 2.2
                arc.lineCapStyle = .round
                blue.setStroke()
                arc.stroke()
                arrow(in: c, color: blue, size: 0.78)
            }
            return true
        }
        img.isTemplate = false
        img.accessibilityDescription = "Grab"
        return img
    }

    static func stroke(_ pts: [NSPoint], width: CGFloat) {
        let p = NSBezierPath()
        p.move(to: pts[0])
        pts.dropFirst().forEach { p.line(to: $0) }
        p.lineWidth = width
        p.lineCapStyle = .round
        p.lineJoinStyle = .round
        p.stroke()
    }

    static func arrow(in c: NSPoint, color: NSColor, size s: CGFloat) {
        color.setStroke()
        stroke([NSPoint(x: c.x, y: c.y + 4.6 * s), NSPoint(x: c.x, y: c.y - 4.0 * s)], width: 2 * max(s, 0.85))
        stroke([NSPoint(x: c.x - 3.4 * s, y: c.y - 0.6 * s), NSPoint(x: c.x, y: c.y - 4.2 * s), NSPoint(x: c.x + 3.4 * s, y: c.y - 0.6 * s)],
               width: 2 * max(s, 0.85))
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
