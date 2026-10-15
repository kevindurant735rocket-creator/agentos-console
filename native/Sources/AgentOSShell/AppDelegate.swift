import AppKit

// D99：应用菜单 + 原生菜单栏。
//
// 这里的每一条 ⌘K / ⌘1…⌘0 / ⌘R / ⌘W 都是**真 NSMenuItem**，能被 macOS 的
// 辅助功能树读到、能进「键盘快捷键」设置面板、能显示在菜单栏里。
// 网页自己监听键盘是不够的：D68 的 a11y_human 证明的只是 DOM 层焦点与 AX 树，
// 而「按 ⌘K 有个真原生菜单项在」这件事只有原生层能证。
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: MainWindowController!
    private let probe = Probe.shared
    private var healthTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let base = Env.baseURL
        probe.put("bundle_id", Bundle.main.bundleIdentifier ?? "")
        probe.put("bundle_executable", Bundle.main.executableURL?.lastPathComponent ?? "")
        probe.put("bundle_path", Bundle.main.bundleURL.path)
        probe.put("bundle_is_app", Bundle.main.bundleURL.pathExtension == "app" ? 1 : 0)
        probe.put("target_url", base.absoluteString)
        probe.put("probe_mode", probe.enabled ? 1 : 0)
        // 单实例证据：跑起来这一刻，同一个 bundle id 下还有几个别的进程。
        // 用户自己开着 AgentOS 时门会看到 1，这正是 D107 修的那个场景；
        // 落成字段才能把「AX 间歇塌陷」和「两个实例打架」连起来。
        let me = ProcessInfo.processInfo.processIdentifier
        let bundle = Bundle.main.bundleIdentifier
        let siblings = (bundle.map {
            NSRunningApplication.runningApplications(withBundleIdentifier: $0)
        } ?? []).filter { Int($0.processIdentifier) != me && !$0.isTerminated }
        probe.put("probe_multi_instance", Env.multiInstance ? 1 : 0)
        probe.put("probe_sibling_instances", siblings.count)

        controller = MainWindowController(baseURL: base)
        buildMenu()
        showWindow()

        // 首探 + 之后每 5s 复探：服务被别人停掉时原生窗口要自己变红并给出恢复入口。
        controller.retryHealth()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.controller.retryHealth()
        }

        if probe.enabled, let secs = Env.autoCloseSeconds {
            DispatchQueue.main.asyncAfter(deadline: .now() + secs) { NSApp.terminate(nil) }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// 明确接管 reopen：这一个方法决定「上次崩过，要不要恢复窗口」那个
    /// 模态框要不要弹。
    ///
    /// D105 实测（sample 取主线程栈，runModal 出现 6 次）：返回 false 时
    /// AppKit 走 _reopenWindowsAsNecessaryIncludingRestorableState: →
    /// NSPersistentUIRestorer promptToIgnorePersistentStateWithCrashHistory:
    /// → -[NSAlert runModal]。那是**模态**事件循环，探针进程里没人点它，
    /// 于是整个 app 静默挂死：首屏截图一张都没拍出来、probe.json 永不落盘，
    /// 门只能看到一个没有解释的 timeout>180s。
    ///
    /// 危害不止于探针：只要 app 崩过一次，用户此后每次双击开都会先撞上
    /// 一个「上次没正常打开」的弹窗。返回 true 即「reopen 我自己处理」，
    /// 窗口由 applicationDidFinishLaunching 无条件重建，不依赖任何持久状态。
    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows: Bool) -> Bool { true }

    private func showWindow() {
        // 「解除最小化」必须显式做，而且必须在 orderFront **之前**。
        //
        // 为什么：`window.setFrameAutosaveName("AgentOSMainWindow")`
        // （MainWindowController.swift）+ styleMask 里的 .miniaturizable，
        // 意味着「用户按过 ⌘M」这件事会被系统持久化。而
        // `makeKeyAndOrderFront` 对一个**已最小化**的窗口近乎空操作：
        // app 上了前台、拿到了 key，窗口却还缩在 Dock 里 —— 用户看到的是
        // 「双击了但窗口没出来」。
        //
        // D105 那三份红证据正是这个自相矛盾的读数，且**每个字段都对得上**：
        //   did_became_key = 1        窗口确实拿到过焦点
        //   mini = True               但它仍是最小化态
        //   visible = False           isVisible 对最小化窗口返回 false
        //   cg_onscreen_count = 0     Dock 图标占位，不算屏上窗口
        //   frame_on_screen = True    存的 frame 仍是合法屏内矩形
        // 「起来过 + 落盘时最小化」这组矛盾，光看采样像是量具抖动，
        // 但产品侧确实少了一个 deminiaturize —— 那是缺陷，不是噪音。
        let wasMiniaturized = controller.window?.isMiniaturized ?? false
        // 落盘这两个键，门才能判「起时最小化的窗口**真的**被拉回来了」
        // 而不是只看到末态。没落盘的话一旦复发就只能靠猜。
        probe.put("window_minimized_before_restore", wasMiniaturized ? 1 : 0)
        controller.window?.deminiaturize(nil)
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        probe.put("window_deminaturized_on_launch", wasMiniaturized ? 1 : 0)
        probe.put("window_shown_at_launch", 1)
        // 「主动要过焦点」是产品行为，「真的拿到焦点」取决于运行环境给不给 ——
        // D50 用管道父进程跑门时 activate 不会生效，那不是产品缺陷。
        // 两者分开记，门只对前者无条件判红、对后者在本进程确实是前台时才判红。
        probe.put("window_requested_activation", 1)
    }

    // MARK: - 菜单

    @discardableResult
    private func item(_ menu: NSMenu, _ title: String, _ action: Selector,
                      _ key: String, _ tag: String? = nil) -> NSMenuItem {
        let it = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
        it.target = self
        it.representedObject = tag
        return it
    }

    private func buildMenu() {
        let main = NSMenu()

        // 应用菜单（第一项，AppKit 用它当应用名菜单）
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "AgentOS")
        item(appMenu, "关于 AgentOS", #selector(about), "")
        appMenu.addItem(.separator())
        item(appMenu, "检查服务", #selector(checkService), "r")
        item(appMenu, "启动本地服务", #selector(startService), "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 AgentOS",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 AgentOS",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        // 文件
        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "文件")
        item(fileMenu, "重新载入驾驶舱", #selector(reloadConsole), "r")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "关闭窗口",
                         action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        // 编辑：原生 App 必须有这条菜单，否则 ⌘C/⌘V/⌘Z 落在网页输入框里行为不对
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        // 视图：⌘K + 十个视图的 ⌘1…⌘0
        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "视图")
        item(viewMenu, "命令面板", #selector(openPalette), "k")
        viewMenu.addItem(.separator())
        // D105：⌘P 是左侧项目与任务空间的键盘入口。没有它，左栏就只有
        // 鼠标能用 —— 而「键盘用户能不能走到每一块」正是这一版要量的东西。
        item(viewMenu, "项目与任务空间", #selector(focusProjects), "p")
        viewMenu.addItem(.separator())
        for v in MainWindowController.viewTable {
            item(viewMenu, v.title, #selector(selectView(_:)), v.shortcut, v.key)
        }
        viewMenu.addItem(.separator())
        // D100：⌘T 开合原生工作区面板。与工具栏那个 item 指向同一个
        // @objc —— 两处各写一套切换逻辑，就会出现「菜单开了、按钮没开」。
        item(viewMenu, "原生工作区", #selector(togglePanel), "t")
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        // 窗口
        let winItem = NSMenuItem()
        let winMenu = NSMenu(title: "窗口")
        winMenu.addItem(withTitle: "最小化",
                        action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winItem.submenu = winMenu
        main.addItem(winItem)

        NSApp.mainMenu = main
        controller.viewMenuItems = MainWindowController.viewTable.map {
            (title: $0.title, key: $0.shortcut, view: $0.key)
        }
        // 键名必须与 MainWindowController 那份**不重名**：D99 首版两处都写
        // `native_menu_view_items`，一处放视图 id、一处放快捷键数字，
        // 谁后 flush 谁覆盖 —— 门读到的「视图条目」其实是一串 '1'..'0'。
        // 重名的两个字段在同一次落盘里只会活一个，另一个静默蒸发。
        probe.put("native_menu_view_ids", MainWindowController.viewTable.map(\.key))
        probe.put("native_menu_view_titles", MainWindowController.viewTable.map(\.title))
        probe.put("native_menu_view_shortcuts", MainWindowController.viewTable.map(\.shortcut))
        probe.put("native_menu_view_count", MainWindowController.viewTable.count)
        probe.put("native_menu_has_cmdk", 1)
        probe.put("native_menu_has_cmdt", 1)
        probe.put("native_menu_has_cmdp", 1)
        probe.put("native_menu_titles", ["AgentOS", "文件", "编辑", "视图", "窗口"])
    }

    @objc private func about() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "AgentOS",
            .init(rawValue: "Copyright"): "本地交付驾驶舱 · 服务自检自启",
        ])
    }

    @objc private func checkService() {
        controller.retryHealth()
        probe.put("native_menu_check_service_invoked", 1)
    }

    @objc private func startService() {
        controller.startService()
        probe.put("native_menu_start_service_invoked", 1)
    }

    @objc private func reloadConsole() {
        controller.reloadConsole()
    }

    @objc private func openPalette() {
        controller.openCommandPalette()
        probe.put("native_menu_cmdk_invoked", 1)
    }

    @objc private func selectView(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String else { return }
        controller.switchView(key)
        probe.put("native_menu_view_invoked", key)
    }

    @objc private func togglePanel() {
        let now = controller.toggleWorkspacePanel()
        probe.put("native_menu_cmdt_invoked", 1)
        probe.put("native_menu_cmdt_result_visible", now ? 1 : 0)
    }

    @objc private func focusProjects() {
        let ok = controller.focusProjects()
        probe.put("native_menu_cmdp_result_focused", ok ? 1 : 0)
    }
}
