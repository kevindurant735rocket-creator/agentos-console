import AppKit
import WebKit

// D99：原生外壳主体。
//
// 设计口径（三条，都对着「像 Codex / ZCode 那种专业工作台」这句用户要求）：
//   1. **窗口是真原生窗口**：交通灯按钮、标题栏合并（fullSizeContentView +
//      titlebarAppearsTransparent）、工具栏项是真 NSToolbarItem、窗口尺寸/位置
//      走 frameAutosaveName 持久化 —— 不是把网页塞进一个浏览器壳。
//   2. **服务生死原生自理**：/__health 探不到就显示原生恢复面板并提供真「启动服务」
//      按钮（真起 tools/serve.py），不要求用户先自己去终端敲命令。
//   3. **键盘入口原生接管**：⌘K / ⌘1…⌘0 / ⌘R / ⌘W 都在原生菜单里有条目，
//      再转发给页面里的真函数 —— 用户按的键在原生层有形，不依赖网页自己监听。
final class MainWindowController: NSWindowController, NSWindowDelegate, WKNavigationDelegate {
    private let probe = Probe.shared
    private let health: HealthProbe
    private let baseURL: URL
    private var webView: WKWebView!
    /// D100：窗口内容从「一块 webView」改成「上下分栏」。
    /// 上面仍是驾驶舱本体，下面是原生文件树 + 原生终端。默认**收起** ——
    /// 收起而不是默认展开，是因为这块面板会抢走 WebView 的高度；
    /// 首屏必须先给驾驶舱，这条不许被「顺手做成默认展开」改掉。
    private var splitView: NSSplitView!
    private var workspace: WorkspacePanel!
    /// D105：左侧项目与任务空间。外层 split 的第一格。
    private var projects: ProjectsPanel!
    private var outerSplit: NSSplitView!
    private var panelItem: NSToolbarItem?
    private var panelVisible = false
    private var panelToggleCount = 0
    private var recovery: RecoveryPanel?
    private var healthItem: NSToolbarItem?
    private var servedProcess: Process?
    private var loadedOnce = false
    private var projectsLoaded = false
    private var pendingProbeSteps: [() -> Void] = []
    /// 探针序列只能起一次。D105 实测踩实：`runProbeSequence` 的唯一调用点是
    /// `webView(_:didFinish:)`，而序列第 7 步自己会 `loadBrowserForProbe`
    /// 导航到 8790 —— 于是「导航完成」又把整个序列重新排一遍，
    /// `pendingProbeSteps` 被清空重填，永远走不到收尾那次 `flush(quit: true)`，
    /// 表现就是主线程 98% 空闲在 runloop 里、探针文件永远不落盘。
    private var probeSequenceStarted = false
    private var probeStepIndex = 0

    /// 原生菜单里的视图条目，门要靠它验「⌘1…⌘0 在原生层有形」。
    /// 由 AppDelegate 填（菜单是在 AppDelegate 里建的），故不是 private(set)。
    var viewMenuItems: [(title: String, key: String, view: String)] = []
    static let viewTable: [(key: String, title: String, shortcut: String)] = [
        ("over", "总览", "1"), ("round", "轮次台账", "2"), ("mx", "阶段矩阵", "3"),
        ("led", "证据账本", "4"), ("eng", "引擎与规则", "5"), ("work", "Agent 台", "6"),
        ("goal", "目标台", "7"), ("retro", "复盘台", "8"), ("cap", "能力地图", "9"),
        ("ws", "工作区", "0"),
        // R7 新增三面：终端 / 文件 / 设置。shortcut 是**空串**，即这三项
        // 只进「视图」菜单，不占 ⌘数字键 —— 1…0 正好十个，第 11 个起没有
        // 单位数键位可给，与 web 侧完全同一契约（web 的 a11y_walk 只给前
        // 10 个视图配数字键，第 11 起靠 tablist 方向键与点击到达）。
        // 空 keyEquivalent 在 AppKit 里是合法值（「缩放」菜单项就是这么写的），
        // 不会静默抢别人已占的键。
        ("term", "终端", ""), ("file", "文件", ""), ("set", "设置", ""),
    ]

    /// 真正有单位数快捷键的视图（⌘1…⌘0，恰十个）。键盘走查只跑这一批 ——
    /// 把空前缀的条目也塞进 keySteps，`digitKeyCode("")` 会返回 0，
    /// 等于拿一个不存在的键位去按，后面的 consumed/reached 计数全被带偏。
    static var digitViews: [(key: String, title: String, shortcut: String)] {
        viewTable.filter { !$0.shortcut.isEmpty }
    }

    init(baseURL: URL) {
        self.baseURL = baseURL
        self.health = HealthProbe(base: baseURL)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1360, height: 880),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        super.init(window: window)
        window.delegate = self
        window.title = "AgentOS"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 900, height: 600)
        window.setFrameAutosaveName("AgentOSMainWindow")
        if !window.setFrameUsingName("AgentOSMainWindow") {
            window.center()
        }
        buildContent()
        buildToolbar()
        health.onChange = { [weak self] ok, detail in self?.applyHealth(ok, detail) }
        probe.put("window_frame", NSStringFromRect(window.frame))
        probe.put("window_style_has_full_size_content",
                  window.styleMask.contains(.fullSizeContentView) ? 1 : 0)
        probe.put("window_titlebar_transparent", window.titlebarAppearsTransparent ? 1 : 0)
        probe.put("window_autosave_name", window.frameAutosaveName)
        // 落盘前重采窗口瞬时状态。`isKeyWindow` 判的是「此刻本进程是前台
        // app 吗」—— 在 D50 那种管道父进程下它合法为假，所以门不拿它当
        // 「窗口起来了」的判据（见 native_gate 的 window-on-screen 注释）。
        // 但**它必须被记录**：否则一旦门想区分「拿不到前台」与「窗口真的
        // 不在屏上」，就只剩一个没写盘的键，证据会静默缺项。
        probe.sampler = { [weak self] in
            let w = self?.window
            var out: [String: Any] = [
                "window_is_visible": w?.isVisible ?? false,
                "window_is_key": w?.isKeyWindow ?? false,
                "window_is_on_screen": w?.isOnActiveSpace ?? false,
                // 「窗口真的画在物理屏幕上」必须与前台无关地量。
                // `isOnActiveSpace` 的语义是「本 app 此刻是活跃 app，且
                // 该窗口在当前 space 上」，所以只要探针是从管道/脚本起的
                // （gates.sh 就是），它恒为 false —— 拿它判「在屏上」
                // 等于把「拿不到前台」误报成「窗口没起来」（D105 实测踩实：
                // visible=True 而 on_screen=False，截图却拍到了满屏真内容）。
                // 真正的不变式是窗口帧与某个 NSScreen 的帧有非空交集。
                "window_frame_on_screen": MainWindowController
                    .frameIntersectsScreen(w?.frame),
                "window_is_miniaturized": w?.isMiniaturized ?? false,
                "nsapp_is_active": NSApp.isActive,
                "window_cg_onscreen_count": MainWindowController.onScreenWindowCount(),
            ]
            // 面板的可见性同样是瞬时状态：用户在探针跑完到落盘之间手动按了
            // ⌘T，历史值就成了假观测。与窗口状态同一口径，落盘前重采。
            if let p = self?.workspace {
                out["panel_is_hidden_now"] = p.isHidden
                out["panel_visible_now"] = p.isHidden ? 0 : 1
                out["panel_bounds_now"] = NSStringFromRect(p.frame)
            }
            return out
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("nib not supported") }

    // MARK: - 内容

    private func buildContent() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        workspace = WorkspacePanel(frame: .zero)
        workspace.isHidden = true
        // 内层仍是「上下：驾驶舱 | 原生工作区」，口径与 D100 一字未改。
        splitView = NSSplitView(frame: .zero)
        splitView.isVertical = false
        splitView.dividerStyle = .thin
        splitView.addArrangedSubview(webView)
        splitView.addArrangedSubview(workspace)
        // D105 外层：左项目 | 右侧内层。左侧这格默认**展开**——它就是工作台
        // 的第一根骨架，收起等于没有；但宽度给窄一点，不许抢驾驶舱。
        projects = ProjectsPanel(frame: .zero)
        outerSplit = NSSplitView(frame: .zero)
        outerSplit.isVertical = true           // 左右：项目 | 右侧工作区
        outerSplit.dividerStyle = .thin
        outerSplit.addArrangedSubview(projects)
        outerSplit.addArrangedSubview(splitView)
        window?.contentView = outerSplit
        probe.put("window_content_kind", "NSSplitView:outer-horizontal")
        probe.put("window_content_subviews", ["ProjectsPanel", "NSSplitView"])
        // 内层构成单独记一份：外层加进来之后，「驾驶舱+工作区还在不在」
        // 就不能继续读 window_content_subviews 那个两元素数组了，
        // 否则这条判据会被外层的加入悄悄改成另一种含义。
        probe.put("window_content_subviews_inner", ["WKWebView", "WorkspacePanel"])
        probe.put("projects_default_visible", 1)
        probe.put("panel_default_visible", 0)
        probe.put("projects_loaded_at", "loadConsole")
        // 左栏宽度要在窗口完成布局后给，否则 frameAutosaveName 恢复出来的
        // 老尺寸会让这条被算成一个负数或一个撑爆屏幕的值。
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
            guard let self else { return }
            let w = self.window?.contentView?.bounds.width ?? 1360
            self.outerSplit.setPosition(max(190, min(280, w * 0.19)), ofDividerAt: 0)
            self.probe.put("projects_divider_x",
                           Int(self.outerSplit.arrangedSubviews.first?.frame.maxX ?? -1))
        }
    }

    // MARK: - 原生工作区面板（D100）

    @discardableResult
    @objc func toggleWorkspacePanel() -> Bool {
        setWorkspaceVisible(!panelVisible)
        return panelVisible
    }

    func setWorkspaceVisible(_ want: Bool) {
        panelVisible = want
        workspace.isHidden = !want
        panelToggleCount += 1
        if want {
            // 展开后才取数据：收起时拉树是纯浪费，还会把「收起态不发请求」
            // 这条性能承诺悄悄破掉。
            workspace.reloadTree {}
            let contentH = window?.contentView?.bounds.height ?? 880
            // 显式给 divider 一个合理位置，否则窗口尺寸被 frameAutosaveName
            // 恢复成很矮时，面板回来可能只有一条缝 —— 「展开等于没展开」。
            splitView.setPosition(max(140, contentH * 0.34), ofDividerAt: 0)
        }
        if let item = panelItem {
            item.image = NSImage(systemSymbolName: "rectangle.bottomhalf.filled",
                                 accessibilityDescription: "原生工作区")
            item.toolTip = want ? "收起原生工作区" : "展开原生工作区"
        }
        probe.merge(workspace.probeSummary())
        probe.put("panel_visible_now", panelVisible ? 1 : 0)
        probe.put("panel_toggle_invocations", panelToggleCount)
    }

    /// 探针用的原生面板序列：开面板 → 拉真文件树 → 真展开一个目录 →
    /// 真跑一条终端命令 → 逐个点亮五个窗格并灌真内容 → 截原生面板的像素。
    ///
    /// 每一步都走真实控件路径（expandItem / target-action），不直接调 API ——
    /// 否则「delegate 没接上」「按钮 target 被摘掉」这两类变异体量不出来，
    /// 而那正是原生化最容易假绿的地方。
    func runWorkspacePanelSequence(_ done: @escaping () -> Void) {
        probe.put("panel_sequence_begin", 1)
        setWorkspaceVisible(true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self else { done(); return }
            self.workspace.reloadTree { [weak self] in
                guard let self else { done(); return }
                let s1 = self.workspace.probeSummary()
                self.probe.put("panel_tree_loaded", s1["tree_root_ok"] as? Int ?? 0)
                self.workspace.expandFirstDirectoryForProbe { [weak self] in
                    guard let self else { done(); return }
                    let s2 = self.workspace.probeSummary()
                    self.probe.put("panel_expanded", s2["tree_last_expand_n"] as? Int ?? 0)
                    self.workspace.runTermCommand()
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
                        guard let self else { done(); return }
                        self.probe.put("panel_term_ran", 1)
                        self.workspace.tourPanesForProbe { [weak self] in
                            guard let self else { done(); return }
                            self.probe.put("panel_panes_toured",
                                           self.workspace.facts["pane_switches"] as? Int ?? 0)
                            self.workspace.paneContentSequenceForProbe { [weak self] in
                                guard let self else { done(); return }
                                // 代码窗格可见时先拍一张：那格里有真实字节，
                                // 是四通道像素里最容易看穿「什么都没画」的一格。
                                self.workspace.driveSeg(.code)
                                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                                    guard let self else { done(); return }
                                    self.shootPanel(tag: "code") {
                                        self.workspace.driveSeg(.file)
                                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                                            guard let self else { done(); return }
                                            self.shootPanel(tag: "panel") {
                                                self.probe.put("panel_sequence_complete", 1)
                                                done()
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    /// 原生面板的像素证据。**刻意不走 takeSnapshot**：面板里没有 WebView，
    /// 要量的正是 AppKit 控件自己画出来的那张图。
    ///
    /// 四条通道全部落盘（D100b）：D100 首跑只跑 cacheDisplay 一条，拿到
    /// 了一张 99% 纯黑的 PNG，而 black 恰好是「满墨」，于是
    /// 「ink >= 0.5 就算有内容」把全黑图判成了通过。**「哪条通道拍到了
    /// 东西」本身就是要交给门裁决的事实**，所以这里不做「挑一张好看的
    /// 报上去」，而是四条连同各自的 distinct / inkRatio 一起进证据。
    /// 汇总用的 `panel_shot_*` 取「非空白通道里 distinct 最大的那条」，
    /// 并把选中的方法名一起落盘（`panel_shot_method`），门可以据此核对
    /// 汇总值确实来自那条被点名的通道，而不是某条匿名路径。
    private func shootPanel(tag: String, then: @escaping () -> Void) {
        guard let base = Screenshot.outputPath else {
            probe.put("panel_shot_\(tag)", "skipped-no-env")
            then()
            return
        }
        let url = URL(fileURLWithPath: base)
        let dir = url.deletingLastPathComponent().path
        let stem = url.deletingPathExtension().lastPathComponent
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            let shots = Screenshot.captureViewAll(self.workspace,
                                                  to: dir, tag: "\(stem)-\(tag)")
            self.probe.put("panel_shot_channels", shots.map(\.summary))
            self.probe.put("wstate_\(tag)", self.windowStateLine())
            self.probe.put("panel_shot_channel_methods", shots.map(\.method))
            // 全黑特征：墨满（>0.9）而颜色数很少。这样的通道**不算**有内容，
            // 汇总时必须跳过，否则「什么都没画」会被当成「画得很满」。
            let blank = { (s: Screenshot.ViewShot) in s.inkRatio > 0.9 && s.distinct < 120 }
            let usable = shots.filter { $0.distinct >= 0 && !blank($0) }
            self.probe.put("panel_shot_blank_channels", shots.filter(blank).map(\.method))
            let best = usable.max { $0.distinct < $1.distinct }
            if let b = best {
                self.probe.put("panel_shot_\(tag)", b.result)
                self.probe.put("panel_shot_method_\(tag)", b.method)
                self.probe.put("panel_shot_path_\(tag)", "\(dir)/\(stem)-\(tag)-\(b.method).png")
                self.probe.put("panel_shot_distinct_colors_\(tag)", b.distinct)
                self.probe.put("panel_shot_ink_ratio_\(tag)", b.inkRatio)
            } else {
                self.probe.put("panel_shot_\(tag)", "all-channels-blank")
                self.probe.put("panel_shot_method_\(tag)", "")
                self.probe.put("panel_shot_path_\(tag)", "")
                self.probe.put("panel_shot_distinct_colors_\(tag)", -1)
                self.probe.put("panel_shot_ink_ratio_\(tag)", -1.0)
            }
            self.probe.put("panel_shot_bytes_\(tag)",
                           shots.map { s in
                           (s.result.hasPrefix("ok ") ? Int(s.result.split(separator: " ")[1]) ?? 0 : 0)
                           })
            self.probe.merge(self.workspace.axSummary())
            self.probe.put("panel_ax_summary_taken", 1)
                self.probe.merge(self.workspace.probeSummary())
                then()
        }
    }

    private func buildToolbar() {
        let toolbar = NSToolbar(identifier: NSToolbar.Identifier("AgentOSToolbar"))
        toolbar.displayMode = .iconOnly
        toolbar.allowsUserCustomization = false
        window?.toolbarStyle = .unified
        window?.toolbar = toolbar
        window?.titlebarAppearsTransparent = true

        let item = NSToolbarItem(itemIdentifier: NSToolbarItem.Identifier("health"))
        item.label = "服务状态"
        item.paletteLabel = "服务状态"
        let dot = NSImageView()
        dot.image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: "服务状态")
        dot.contentTintColor = .systemYellow
        item.view = dot
        item.toolTip = "正在探测 AgentOS 服务"
        healthItem = item
        toolbar.insertItem(withItemIdentifier: NSToolbarItem.Identifier("health"), at: 0)

        let reload = NSToolbarItem(itemIdentifier: NSToolbarItem.Identifier("reload"))
        reload.label = "重新载入"
        reload.paletteLabel = "重新载入"
        reload.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "重新载入")
        reload.target = self
        reload.action = #selector(reloadConsole)
        toolbar.insertItem(withItemIdentifier: NSToolbarItem.Identifier("reload"), at: 1)

        // D100：原生工作区面板的开关也进工具栏。⌘T 是等价的键盘入口，
        // 两个入口都指向同一个 @objc，不许各写一套切换逻辑。
        let panel = NSToolbarItem(itemIdentifier: NSToolbarItem.Identifier("panel"))
        panel.label = "原生工作区"
        panel.paletteLabel = "原生工作区"
        panel.image = NSImage(systemSymbolName: "rectangle.bottomhalf.filled",
                              accessibilityDescription: "原生工作区")
        panel.target = self
        panel.action = #selector(toggleWorkspacePanel)
        panel.toolTip = "展开原生工作区（文件 / 终端 / 代码 / 浏览器 / 预览）"
        panelItem = panel
        toolbar.insertItem(withItemIdentifier: NSToolbarItem.Identifier("panel"), at: 2)

        probe.put("native_toolbar_items", ["health", "reload", "panel"])
        probe.put("native_toolbar_style_unified", 1)
    }

    // MARK: - 健康 / 降级

    private func applyHealth(_ ok: Bool, _ detail: String) {
        if let item = healthItem, let dot = item.view as? NSImageView {
            dot.contentTintColor = ok ? .systemGreen : .systemRed
            item.toolTip = detail
        }
        probe.put("health_ok_last", ok ? 1 : 0)
        probe.put("health_detail_last", detail)
        if ok {
            if let r = recovery { r.removeFromSuperview(); recovery = nil }
            probe.put("recovery_panel_visible", 0)
            if !loadedOnce { loadConsole() }
        } else {
            showRecovery(detail)
        }
    }

    private func showRecovery(_ detail: String) {
        guard recovery == nil else {
            // 复探失败时也要保住「会自己拉起服务」那句说明，不能退化成裸 cause。
            recovery?.setDetail(self.recoveryText(detail))
            return
        }
        let panel = RecoveryPanel()
        panel.frame = webView.bounds
        panel.autoresizingMask = [.width, .height]
        // 落盘的是**用户真看到的那段话**，不是原始 cause。首版只 put 了 detail，
        // 于是「面板有没有告诉用户它会自己把服务拉起来」这件事在证据里查不到 ——
        // 而那恰恰是「失败路径也是产品」与「失败路径只是一句报错」的分界。
        let shown = recoveryText(detail)
        panel.setDetail(shown)
        panel.onStart = { [weak self] in self?.startService() }
        panel.onRetry = { [weak self] in self?.retryHealth() }
        webView.addSubview(panel)
        recovery = panel
        probe.merge(panel.probeSummary())
        probe.put("recovery_panel_visible", 1)
        probe.put("recovery_detail", shown)
        if probe.enabled && Env.degradedProbe { runDegradedFlow() }
    }

    private func recoveryText(_ detail: String) -> String {
        "\(detail)\n地址：\(baseURL.absoluteString)\n"
            + "AgentOS 会自己把本地服务拉起来，不需要你在终端敲命令。"
    }

    /// 降级面探针：量「服务没起来时这块原生面板是不是真产品」。
    /// 为什么值得单独一套流程：正常探针量的全是「页面加载成功之后」的世界，
    /// 而用户第一次双击这个 App、或者把 serve.py 误杀掉之后看到的**恰恰是
    /// 失败路径** —— 它要是白屏或一句 fetch 失败，既有 30 道门一道都不会红。
    private var degradedStage = 0
    private var degradedTimer: Timer?
    private var degradedClosed = false
    private var degradedConsoleLoaded = false
    private var degradedSamples: [[String: Any]] = []

    private func runDegradedFlow() {
        guard degradedStage == 0 else { return }
        degradedStage = 1
        // 降级序列自己收尾：正常 14s 探针序列会被这里紧随的 flush(quit:)
        // 拦腰截断，落一份半截状态，读数互相矛盾（D105 已经为这件事栽过一次）。
        probeSequenceStarted = true
        probe.put("degraded_mode", 1)
        probe.put("degraded_health_before_tap", health.lastOK ? 1 : 0)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self else { return }
            guard let panel = self.recovery else {
                self.probe.put("degraded_panel_missing", 1)
                self.probe.flush(quit: true)
                return
            }
            guard Env.tapStart else {
                self.probe.put("degraded_tapped", 0)
                self.probe.flush(quit: true)
                return
            }
            panel.tapStartForProbe()
            self.probe.put("degraded_tapped", 1)
            self.beginDegradedSequence()
        }
    }

    /// D106：原来这里是一个固定 +4.5s 的 `asyncAfter`，三个时间数字互相错开 ——
    /// `startService()` 只在 +2.5s 打一枪 health，AppDelegate 的周期轮询是 5s，
    /// 探针却在 +4.5s 读终值。于是 `degraded_health_after_tap` 量的其实是相位运气：
    /// 同一份代码，机器空载可能绿，35 门串行加 Chrome 同跑就红。
    /// 就算相位刚好对上了，+4.5s 也常常早于 webview didFinish，
    /// `degraded-console-then-loaded` 仍然是量不到的。
    ///
    /// 现在从 tap 起每 0.4s 探一次，直到健康转绿或超预算；每次探到的值和相对
    /// 时间戳都进 `degraded_health_samples`。这样下次再红，证据里能直接读出
    /// 「服务确实起不来」还是「抽样太早」，不用再猜。
    private func beginDegradedSequence() {
        let beganAt = Date()
        let budget: TimeInterval = 25
        degradedSamples.removeAll()
        degradedClosed = false
        degradedConsoleLoaded = false
        degradedTimer?.invalidate()
        let timer = Timer(timeInterval: 0.4, repeats: true) { [weak self] _ in
            guard let self else { return }
            let t = Date().timeIntervalSince(beganAt)
            self.health.poll { [weak self] ok, detail, _ in
                guard let self else { return }
                self.degradedSamples.append([
                    "t": (t * 100).rounded() / 100,
                    "ok": ok ? 1 : 0,
                    "detail": detail,
                ])
                self.probe.put("degraded_health_samples", self.degradedSamples)
                if ok || t >= budget {
                    self.closeDegradedSequence()
                }
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        degradedTimer = timer
    }

    /// 判据只在状态真的到位之后采，不在固定时钟上采。
    /// 预算耗尽也算一种到位：那时采到的是「服务确实起不来」这个事实。
    private func closeDegradedSequence() {
        guard !degradedClosed else { return }
        degradedClosed = true
        degradedTimer?.invalidate()
        degradedTimer = nil
        let recovered = health.lastOK
        probe.put("degraded_health_after_tap", recovered ? 1 : 0)
        probe.put("degraded_recovery_after_tap", recovery == nil ? 0 : 1)
        guard recovered else {
            probe.put("degraded_sequence_complete", 1)
            probe.flush(quit: true)
            return
        }
        // applyHealth 已经把面板收掉并调了 loadConsole；等控制台真的渲染出来
        // 再落盘，否则「控制台随后加载」这条永远量不到。
        waitForConsoleThenFinish()
    }

    /// 0.25s 一步、最长 10s。等到就收，等不到也收 —— 等不到本身就是证据，
    /// `degraded_console_wait_steps` 会记下到底等了多少步。
    private func waitForConsoleThenFinish(attempts: Int = 0) {
        let budget = 40
        if degradedConsoleLoaded || attempts >= budget {
            probe.put("degraded_console_wait_steps", attempts)
            probe.put("degraded_sequence_complete", 1)
            // 让这次 flush 成为终态：后面的补写不许覆盖降级序列的读数。
            probe.sequenceCompleted = true
            probe.flush(quit: true)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            self?.waitForConsoleThenFinish(attempts: attempts + 1)
        }
    }

    func retryHealth() { health.poll() }

    /// 真起服务：不是「提示用户去终端」，而是自己 fork 出 tools/serve.py。
    func startService() {
        guard servedProcess == nil else { return }
        let root = URL(fileURLWithPath: Bundle.main.bundleURL.deletingLastPathComponent().path)
        let serve = root.appendingPathComponent("tools/serve.py").path
        let py = ["/opt/miniconda3/bin/python3", "/usr/bin/python3", "/opt/homebrew/bin/python3"]
            .first(where: { FileManager.default.isExecutableFile(atPath: $0) }) ?? "python3"
        let port = baseURL.port ?? 8790
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = [serve, "--port", String(port)]
        p.currentDirectoryURL = root
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            servedProcess = p
            probe.put("service_started_by_app", 1)
            probe.put("service_pid", p.processIdentifier)
        } catch {
            probe.put("service_started_by_app", 0)
            probe.put("service_start_error", error.localizedDescription)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
            self?.retryHealth()
        }
    }

    /// 页面是否已加载完成（didFinish 到过）。
    private var webviewDidFinish = false
    /// 加载重试次数（有界：不是无限刷新）。
    private var webviewLoadRetries = 0

    /// didFinish 没到就催一次 reload（窗口被遮挡时 WebKit 会推迟加载）。
    private func scheduleLoadRetry() {
        guard !webviewDidFinish, webviewLoadRetries < 3 else { return }
        webviewLoadRetries += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 6.0) { [weak self] in
            guard let self, !self.webviewDidFinish else { return }
            self.probe.put("webview_load_retry", self.webviewLoadRetries)
            self.webView.reload()
            self.scheduleLoadRetry()
        }
    }

    private func loadConsole() {
        loadedOnce = true
        probe.put("webview_url_requested", baseURL.absoluteString)
        webView.load(URLRequest(url: baseURL))
        // 加载**重试**（2026-10-20 实测踩到）：窗口被完全遮挡时 WebKit 会
        // **推迟**这次加载 —— didFinish 迟迟不来，页面就一直空着，探针序列
        // 卡在等页面上，落下半份证据（门整片飘红，真凶只是「页面没加载」）。
        // 现在：didFinish 没到就自己 reload（有界 3 次，每次 6s 一催）。
        // 恢复可见后 WebKit 会自己继续，但**不催**它就一直不继续。
        scheduleLoadRetry()
        // 左侧项目树与驾驶舱同时起：两者都依赖同一个本地服务，分两次拉会
        // 让「面板在窗口一出现就是空的」成为可能。只拉一次。
        if !projectsLoaded {
            projectsLoaded = true
            projects.reloadProjects { [weak self] in
                guard let self else { return }
                self.probe.merge(self.projects.probeSummary())
            }
        }
    }

    /// ⌘P：把第一键盘响应者交给项目列表。这是「键盘用户够得到左栏」的
    /// 唯一入口 —— 没有它，左栏就只是鼠标专用区。
    @discardableResult
    func focusProjects() -> Bool {
        window?.makeFirstResponder(projects)
        let ok = projects.tableHasFocusForProbe()
        probe.put("projects_focus_via_cmdp", ok ? 1 : 0)
        probe.merge(projects.probeSummary())
        return ok
    }

    @objc func reloadConsole() {
        loadedOnce = false
        webView.reload()
        probe.put("native_reload_invoked", 1)
    }

    // MARK: - 原生菜单动作（转发给页面里的真函数）

    func switchView(_ key: String) {
        run("window.view && window.view('\(key)')") { [weak self] ok, _, err in
            guard let self else { return }
            self.probe.put("view_switch_\(key)", ok ? 1 : 0)
            if !ok { self.probe.put("view_switch_error_\(key)", err ?? "nil") }
            self.readState { st in
                // 每次切换写自己的键：D99 首次跑就因为两次切换共用一个键，
                // 后一次把前一次的观测覆盖掉，于是「切到阶段矩阵有没有生效」
                // 永远只能读到最后一次的结果 —— 门会拿着一个假观测判绿。
                self.probe.put("active_tab_after_\(key)", st["active_tab"] as? String ?? "")
                self.probe.put("visible_panel_after_\(key)", st["visible_panel"] as? String ?? "")
                self.probe.put("active_tab_text_after_\(key)", st["active_tab_text"] as? String ?? "")
            }
        }
    }

    func openCommandPalette() {
        run("window.cmdOpen && window.cmdOpen()") { [weak self] ok, _, err in
            guard let self else { return }
            self.probe.put("cmdk_open_called", ok ? 1 : 0)
            if !ok { self.probe.put("cmdk_open_error", err ?? "nil") }
            self.readState { st in
                self.probe.put("cmdk_on_after_open", st["cmdk_on"] as? Int ?? -1)
                self.probe.put("cmdk_focus_after_open", st["cmdk_focus"] as? String ?? "")
                self.probe.put("cmdk_options", st["cmdk_options"] as? Int ?? -1)
            }
        }
    }

    func closeCommandPalette() {
        run("window.cmdClose && window.cmdClose()") { [weak self] ok, _, _ in
            self?.readState { st in
                self?.probe.put("cmdk_on_after_close", st["cmdk_on"] as? Int ?? -1)
            }
        }
    }

    // MARK: - JS 桥

    private func run(_ js: String, _ done: @escaping (Bool, String?, String?) -> Void) {
        webView.evaluateJavaScript("String(eval(\(jsLiteral(js))))") { value, error in
            if let error = error {
                done(false, nil, error.localizedDescription)
            } else {
                done(true, value as? String ?? String(describing: value), nil)
            }
        }
    }

    /// JSON 编码一次，避免任何引号/反斜杠把 JS 字符串拼坏。
    private func jsLiteral(_ s: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [s])) ?? Data()
        guard let arr = String(data: data, encoding: .utf8), arr.count >= 2 else { return "\"\"" }
        return String(arr.dropFirst().dropLast())
    }

    /// 读页面真实状态。门判「原生动作真的作用到了屏上」靠的是这里读回的字段，
    /// 不是靠 Swift 自己声称调用成功 —— 这正是 D93 那类「进程 rc=0 却没写东西」的镜像防线。
    func readState(_ done: @escaping ([String: Any]) -> Void) {
        webView.evaluateJavaScript(Self.stateJS) { value, error in
            if let error = error {
                NSLog("[AgentOS] readState failed: %@", error.localizedDescription)
                done([:])
                return
            }
            guard let text = value as? String,
                  let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any]
            else { done([:]); return }
            done(obj)
        }
    }

    private static let stateJS = """
    (function(){
      var q=function(s){return document.querySelector(s);};
      var sel=q('[role=tab][aria-selected=true]');
      var on=q('.tabpanel.on')||q('[role=tabpanel].on');
      var cmd=q('#cmd');
      var st=q('#states.on');
      return JSON.stringify({
        title: document.title,
        active_tab: sel?sel.id:'',
        active_tab_text: sel?sel.textContent.replace(/\\s+/g,' ').trim():'',
        visible_panel: on?(on.id||''):'',
        buttons: document.querySelectorAll('button').length,
        tab_count: document.querySelectorAll('[role=tab]').length,
        has_tablist: q('[role=tablist]')?1:0,
        cmdk_on: cmd?(cmd.className.indexOf('on')>=0?1:0):-1,
        cmdk_focus: document.activeElement?(document.activeElement.id||document.activeElement.tagName):'',
        cmdk_options: document.querySelectorAll('#cmdlist button').length,
        body_len: document.body?document.body.innerText.length:0,
        state_box: st?(st.className+'|'+(st.innerText||'').replace(/\\s+/g,' ').slice(0,90)):'',
        main_present: q('main')?1:0
      });
    })()
    """

    // MARK: - 探针流程

    /// D105 真键盘走查。
    ///
    /// 走的是 `NSMenu.performKeyEquivalent(with:)` —— 与用户物理按下 ⌘1 时
    /// 系统走的是**同一条**分发路径。刻意不直接调 `switchView("mx")`：
    /// 直接调只能证明那个方法能跑，证明不了「菜单项还在、keyEquivalent
    /// 没被改、target 没被摘、action 还指着老 selector」—— 而这四件事
    /// 恰恰是原生键盘入口最容易悄悄坏掉、且既有门一道都看不见的地方。
    ///
    /// 每按一键都**回读页面真实状态**再记账：菜单被消费不等于页面切了。
    func runKeyboardSequence(_ done: @escaping () -> Void) {
        probe.put("keyeq_path", "NSMenu.performKeyEquivalent")
        // 游标放属性、不放 inout 参数：asyncAfter 与 readState 两层闭包都是
        // escaping 的，闭包里捕获 inout 参数在 Swift 里直接编译不过。
        keySteps = Self.digitViews.map {
            ($0.shortcut, Self.digitKeyCode($0.shortcut), $0.key)
        }
        keySteps.append(("k", 40, "__cmdk"))
        keySteps.append(("t", 17, "__cmdt"))
        keySteps.append(("p", 35, "__cmdp"))
        keyStepIndex = 0
        runKeyStep(done)
    }

    private var keySteps: [(String, UInt16, String)] = []
    private var keyStepIndex = 0

    // 第 11-13 视图（term/file/set）没有单位数快捷键，按不到就是按不到；
    // 走查只在有键的那一批上数「切过去了几个」，并在报告里如实记总数 13。
    // 混在一起数会得到 13 != 10 的永久假红。
    private var digitKeys: [String] { Self.digitViews.map(\.key) }

    private func runKeyStep(_ done: @escaping () -> Void) {
        guard keyStepIndex < keySteps.count else {
            projects.pressDownKeyForProbe()
            probe.merge(projects.probeSummary())
            probe.merge(projects.axSummary())
            var matched = 0
            // DOM 里 tab 的 id 带 `t-` 前缀（active_tab 回读的就是它），
            // 拿前缀后的裸 key 去比会 10 个全不匹配 —— D105 实测踩实。
            // 只数有单位数快捷键的那 10 个：term/file/set 这一轮根本没按过键，
            // 把它们算进分母就是「必然假红」。总数另外单独记（下面 total）。
            for k in digitKeys where probe.text("keyeq_tab_\(k)") == "t-" + k {
                matched += 1
            }
            probe.put("keyeq_views_total", Self.viewTable.count)
            probe.put("keyeq_digit_views_total", digitKeys.count)
            probe.put("keyeq_views_matched", matched)
            probe.put("keyeq_sequence_complete", 1)
            done()
            return
        }
        let (ch, code, tag) = keySteps[keyStepIndex]
        keyStepIndex += 1
        let consumed = pressKeyEquivalent(ch, code)
        probe.put("keyeq_consumed_\(tag)", consumed ? 1 : 0)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.65) { [weak self] in
            guard let self else { done(); return }
            self.readState { st in
                if tag == "__cmdk" {
                    self.probe.put("keyeq_cmdk_on", st["cmdk_on"] as? Int ?? -1)
                } else if tag == "__cmdt" {
                    self.probe.put("keyeq_cmdt_visible", self.panelVisible ? 1 : 0)
                    if self.panelVisible { self.setWorkspaceVisible(false) }
                } else if tag == "__cmdp" {
                    let f = self.projects.tableHasFocusForProbe() ? 1 : 0
                    self.probe.put("keyeq_projects_focused", f)
                } else {
                    self.probe.put("keyeq_tab_\(tag)", st["active_tab"] as? String ?? "")
                }
                self.runKeyStep(done)
            }
        }
    }

    /// 构造 ⌘+<ch> 的真 NSEvent 并交给主菜单分发。返回「菜单消费了它」。
    private func pressKeyEquivalent(_ ch: String, _ code: UInt16) -> Bool {
        guard let e = NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: .command,
            timestamp: 0, windowNumber: window?.windowNumber ?? 0,
            context: nil, characters: ch, charactersIgnoringModifiers: ch,
            isARepeat: false, keyCode: code)
        else { return false }
        return NSApp.mainMenu?.performKeyEquivalent(with: e) ?? false
    }

    /// 数字键的物理 keyCode：1…9 = 18…26，0 = 29。
    private static func digitKeyCode(_ ch: String) -> UInt16 {
        if ch == "0" { return 29 }
        if let v = Int(ch), v >= 1, v <= 9 { return UInt16(17 + v) }
        return 0
    }

    /// 窗口帧是否与任一物理屏幕的帧有**非空交集**。
    /// 面积门槛取 400pt²：交集窄到那个程度就只是一条边框压着屏幕边，
    /// 不可能是「用户看得见的工作台」。
    static func frameIntersectsScreen(_ frame: NSRect?) -> Bool {
        guard let f = frame, f.width > 0, f.height > 0 else { return false }
        for s in NSScreen.screens {
            let hit = f.intersection(s.frame)
            if hit.width * hit.height >= 400 { return true }
        }
        return false
    }

    /// 窗口服务器（CoreGraphics）自己的判决：本进程此刻真画在屏上的窗口数。
    /// NSWindow 那三个布尔全是瞬时量，D105 两次实测互相矛盾；只有这一路
    /// 在问 WindowServer「有没有本进程的窗口真画着」。取不到就 -1，如实取不到。
    static func onScreenWindowCount() -> Int {
        let pid = Int32(ProcessInfo.processInfo.processIdentifier)
        guard let list = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]]
        else { return -1 }
        return list.filter {
            ($0[kCGWindowOwnerPID as String] as? NSNumber)?.intValue == Int(pid)
        }.count
    }

    /// 一行窗口状态，在每张截图落地那一刻采样（与落盘时的 sampler 分开）。
    private func windowStateLine() -> String {
        let w = window
        return "visible=\(w?.isVisible ?? false) mini=\(w?.isMiniaturized ?? false) space=\(w?.isOnActiveSpace ?? false) cg=\(MainWindowController.onScreenWindowCount())"
    }

    /// 探针模式下按序做完所有可观测动作，再落盘并退出。
    func runProbeSequence() {
        guard !probeSequenceStarted else { return }
        probeSequenceStarted = true
        // 序列名义上 ~14s，留 6 倍余量；超了就是有一步的 done() 没被调用。
        probe.armWatchdog(seconds: 90)
        pendingProbeSteps = [
            { [weak self] in self?.stepReadInitialThen("over") },
            { [weak self] in self?.stepSwitchToMatrix() },
            { [weak self] in self?.stepOpenPalette() },
            { [weak self] in self?.stepClosePalette() },
            { [weak self] in self?.stepSwitchBackThen("back") },
            // D105：真键盘走查排在 5 张分状态截图之后、面板序列之前 ——
            // 它会把视图挨个按一遍并最终停在工作区，先让固定视图的截图拍完。
            { [weak self] in self?.stepKeyboardWalk() },
            // 面板序列排在最后：它会展开下面板、把 webView 压矮，
            // 所以必须排在 5 张分状态截图之后，否则那 5 张的像素判据
            // 会被这块面板连累（MIN_SHOT_H 判的是驾驶舱那张图）。
            { [weak self] in self?.stepWorkspacePanel() },
        ]
        runNextProbeStep()
    }

    private func runNextProbeStep() {
        guard !pendingProbeSteps.isEmpty else {
            Probe.trace("probe: sequence drained -> final shot + flush")
            shoot(tag: "final") { [weak self] in
                guard let self else { return }
                // D109：权威读数必须在「窗口处于产品正常呈现态」时落盘。
                //
                // 2026-10-10 全链实跑实测（41 门里 native_gate 红 187/194）：
                // probe_sequence_complete=1（序列确实收尾了）而同一份读数里
                // mini=True / visible=False / is_key=False / nsapp_is_active=True
                // —— 序列已走完，末态却落在「app 在前台、窗口缩在 Dock」。
                // 全树 rg 证明产品自身**没有任何** miniaturize 调用，所以这不是
                // 产品缺陷，是**证据挂载点**问题：把末态读数挂在一个随时会被
                // 环境改动的瞬时状态上（用户 ⌘M / 系统窗口恢复 / 别的实例抢
                // 窗口服务器），与 D105 那次同源。
                //
                // 处置：落盘前把窗口拉回产品该有的呈现态，并把这次动作落成
                // 字段，让「拉回过」在证据里看得见 —— 不静默掩盖。
                // 只对「窗口被按进 Dock」和「app 在前台却没有 key 窗口」两种
                // 自相矛盾状态动手；正常路径（mini=False 且 key 与前台一致）
                // 一个字段都不改，判据语义不变（native_gate_selftest 的
                // m_window_minimized / m_key_lies 变异体照旧要求翻红）。
                // 只在探针模式发生：runProbeSequence 的唯一调用点由
                // probe.enabled 把守，真实用户按 ⌘M 的语义不受影响。
                var reasserted = 0
                if self.window?.isMiniaturized == true {
                    self.window?.deminiaturize(nil)
                    reasserted |= 1
                }
                if NSApp.isActive, self.window?.isKeyWindow != true {
                    self.window?.makeKeyAndOrderFront(nil)
                    reasserted |= 2
                }
                self.probe.put("window_reasserted_before_flush", reasserted)
                self.probe.put("probe_sequence_complete", 1)
                self.probe.sequenceCompleted = true
                // 「落盘」与「退场」分成两步，中间按需留给外部观察者。
                //
                // D108 实测踩实（work/native/ax.json，2026-10-09 T15:43Z）：
                // 序列 drained 只花 ~39s（app-main.err：step0 15:43:48Z →
                // drained 15:44:24Z），而外部 AX 量具 tools/ax_probe.py 要采满
                // AX_SAMPLES=12 个样本要约 100s —— 每轮迭代 ~8.2s，其中 2.5s 是
                // --poll 的睡眠，剩下 ~5.7s 花在
                // `fg_activate.ensure_foreground(pid, timeout=6.0)`
                // （ax_probe.py:144 的默认超时）：这台机上前台被
                // com.wonderassembly.compositor 占着，那条 AppKit 路每轮都走满
                // 6s 后才靠纯 AX 的 kAXFrontmost 兜底救回来（ax.json 里
                // fg_mechanism="ax-frontmost"、fg_ok=True 就是这个形状）。
                // ax_probe.py / fg_activate.py 是只读量具，门这一侧改不了
                // 单轮成本，能改的只有靶子的寿命。
                //
                // 旧代码在这里 `flush(quit: true)`：app 39s 就退，而 ax_probe
                // 的预算 AX_WAIT=40s 只够 5 个样本，第 5 个采在死进程上
                // （t=39.7 exposed=False web=None nodes=None），ax-sustained 因
                // samples<12 恒红 —— AX_SAMPLES=12 / AX_SERIES_MIN_PCT=60 都是
                // 判据阈值，不许放松。所以这是**抽样预算装不下判据本身**，
                // 红的不是产品：活着时 4/4 采样全暴露、web_area=1、19-30 节点。
                //
                // 权威证据在 app 还完整活着时落盘（quit:false；落完
                // `finalFlushed=true`，退场瞬间 windowWillClose 那次
                // flush(quit:false) 会被它挡住，见 Probe.flush 的 D105 说明），
                // 之后按 AGENTOS_AX_GRACE 继续活着让外部序列采完，到点自己退。
                self.probe.flush(quit: false)
                if let g = Env.axGraceSeconds, g > 0 {
                    Probe.trace("probe: evidence flushed, holding \(g)s for external AX series")
                    DispatchQueue.main.asyncAfter(deadline: .now() + g) {
                        NSApp.terminate(nil)
                    }
                } else {
                    Probe.trace("probe: evidence flushed, exiting now")
                    NSApp.terminate(nil)
                }
            }
            return
        }
        let step = pendingProbeSteps.removeFirst()
        Probe.trace("probe: step \(probeStepIndex) begin")
        probeStepIndex += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.55, execute: step)
    }

    /// 截图必须在**状态稳定之后**拍：WebKit 的合成是异步的，
    /// 拍早了拿到的是上一帧 —— 那正是「截图比 DOM 慢一拍」的经典坑。
    private func shoot(tag: String, then: @escaping () -> Void) {
        guard let base = Screenshot.outputPath else {
            probe.put("shot_\(tag)", "skipped-no-env")
            then()
            return
        }
        let url = URL(fileURLWithPath: base)
        let dir = url.deletingLastPathComponent()
        let name = url.deletingPathExtension().lastPathComponent
        let target = dir.appendingPathComponent("\(name)-\(tag).png").path
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) {
            Screenshot.capture(self.webView, to: target) { result in
                self.probe.put("shot_\(tag)", result)
                self.probe.put("shot_path_\(tag)", target)
                if let stats = Screenshot.inkStats(target) {
                    self.probe.put("shot_distinct_colors_\(tag)", stats.distinct)
                    self.probe.put("shot_ink_ratio_\(tag)", stats.inkRatio)
                } else {
                    // 截不出来必须显式记 -1，让门能判红；不能让字段「不存在」
                    // 被读成「没这项要求」—— 那就是空门。
                    self.probe.put("shot_distinct_colors_\(tag)", -1)
                    self.probe.put("shot_ink_ratio_\(tag)", -1.0)
                }
                self.probe.put("wstate_\(tag)", self.windowStateLine())
                then()
            }
        }
    }

    private func stepReadInitialThen(_ tag: String) {
        readState { [weak self] st in
            guard let self else { return }
            self.probe.put("page_title", st["title"] as? String ?? "")
            self.probe.put("page_buttons", st["buttons"] as? Int ?? -1)
            self.probe.put("page_tab_count", st["tab_count"] as? Int ?? -1)
            self.probe.put("page_has_tablist", st["has_tablist"] as? Int ?? 0)
            self.probe.put("page_active_tab", st["active_tab"] as? String ?? "")
            self.probe.put("page_body_len", st["body_len"] as? Int ?? -1)
            self.probe.put("page_main_present", st["main_present"] as? Int ?? 0)
            self.probe.put("page_state_box", st["state_box"] as? String ?? "")
            self.shoot(tag: tag) { self.runNextProbeStep() }
        }
    }

    private func stepSwitchToMatrix() {
        // 这条是「原生菜单条目真能转发到哪些视图 id」—— 与 AppDelegate 那份
        // `native_menu_view_ids`（建菜单时登记的）互为对照：两处一致才说明
        // 菜单条目和转发目标没接错线。
        probe.put("native_menu_forward_targets", viewMenuItems.map(\.view))
        probe.put("native_menu_view_count", viewMenuItems.count)
        switchView("mx")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [weak self] in
            self?.shoot(tag: "mx") { self?.runNextProbeStep() }
        }
    }

    private func stepOpenPalette() {
        openCommandPalette()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { [weak self] in
            self?.shoot(tag: "cmdk") { self?.runNextProbeStep() }
        }
    }

    private func stepClosePalette() {
        closeCommandPalette()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { [weak self] in self?.runNextProbeStep() }
    }

    private func stepSwitchBackThen(_ tag: String) {
        switchView("over")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [weak self] in
            self?.shoot(tag: tag) { self?.runNextProbeStep() }
        }
    }

    private func stepWorkspacePanel() {
        runWorkspacePanelSequence { [weak self] in
            // 面板量完之后收起：收尾截图「final」必须是完整驾驶舱，
            // 而不是一个被面板压矮的窗口 —— 否则最后一张证据代表不了产品。
            self?.setWorkspaceVisible(false)
            self?.runNextProbeStep()
        }
    }

    /// 键盘走查的包装：走完必须回到总览。走查最后停在 ⌘0（工作区）且焦点
    /// 落在左栏，若不拉回来，后面那张 final 截图代表的就不是产品首屏 ——
    /// 「末态截图」的价值全在于它是用户默认看到的那一张。
    private func stepKeyboardWalk() {
        runKeyboardSequence { [weak self] in
            guard let self else { return }
            self.switchView("over")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) {
                self.runNextProbeStep()
            }
        }
    }

    // MARK: - WKNavigationDelegate

    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        webviewDidFinish = true
        probe.put("webview_did_finish", 1)
        // 降级序列在等这个信号：服务自己拉起来之后，控制台要真的渲染出来，
        // 「恢复面板收掉、控制台随后加载」这条才算量到，而不是猜一个固定秒数。
        if degradedStage == 1 { degradedConsoleLoaded = true }
        probe.put("webview_url", w.url?.absoluteString ?? "")
        readState { [weak self] st in
            self?.probe.put("webview_title_after_load", st["title"] as? String ?? "")
        }
        if probe.enabled {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { [weak self] in
                self?.runProbeSequence()
            }
        }
    }

    func webView(_ w: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        probe.put("webview_did_finish", 0)
        probe.put("webview_nav_error", error.localizedDescription)
    }

    func webView(_ w: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        probe.put("webview_did_finish", 0)
        probe.put("webview_nav_error", error.localizedDescription)
    }

    // MARK: - 窗口

    func windowDidBecomeKey(_ notification: Notification) {
        // 只记「**曾经**拿到过焦点」这个耐久事实，瞬时状态交给 flush 前的
        // sampler 重采（原因见 Probe.sampler 的注释）。
        probe.put("window_did_became_key", 1)
    }

    func windowWillClose(_ notification: Notification) {
        if let p = servedProcess, p.isRunning { p.terminate() }
        if probe.enabled { probe.flush(quit: false) }
    }
}
