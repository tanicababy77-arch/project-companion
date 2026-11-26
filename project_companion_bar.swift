import Cocoa

// 项目伴侣 · 菜单栏常驻小应用
// 开机自启后常驻 macOS 顶部菜单栏，点开菜单：新建项目 / 打开看板 / 退出
// 编译：swiftc -O project_companion_bar.swift -o project_companion_bar

// 工具目录跟着二进制自身位置走：整个文件夹挪到哪都能用，不再写死路径
let exe = Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])
let TOOL_DIR = exe.deletingLastPathComponent().resolvingSymlinksInPath().path
let PYTHON = "/opt/homebrew/bin/python3"

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            var img = NSImage(contentsOfFile: TOOL_DIR + "/assets/menu_bar_template.png")
            if img == nil {
                img = NSImage(systemSymbolName: "checkmark.circle", accessibilityDescription: "项目伴侣")
            }
            img?.isTemplate = true
            // 菜单栏 18pt 下绳+文档剪影偏糊，模板图按 2x 提供，让系统缩放更干净
            img?.size = NSSize(width: 18, height: 18)
            button.image = img
        }

        let menu = NSMenu()
        let newItem = NSMenuItem(title: "新建项目", action: #selector(newProject), keyEquivalent: "n")
        newItem.target = self
        menu.addItem(newItem)

        let openItem = NSMenuItem(title: "打开看板", action: #selector(openPanel), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "退出项目伴侣", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }

    @objc func newProject() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: PYTHON)
        p.arguments = [TOOL_DIR + "/create_project.py"]
        try? p.run()
    }

    @objc func openPanel() {
        if let url = URL(string: "http://127.0.0.1:8900") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
