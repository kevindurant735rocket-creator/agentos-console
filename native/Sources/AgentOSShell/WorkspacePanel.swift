import AppKit
import WebKit

// D100：原生工作区面板 —— 真 AppKit 文件树 + 真 AppKit 终端。
//
// 这一刀针对的是目标的最后一句：「最终一定要做成 mac 原生软件的样子」。
// 在 D100 之前，窗口 / 菜单 / 工具栏 / 恢复面板是 AppKit，而**工作区、
// 文件、终端全在 WKWebView 里** —— 整个 App 只有一个网页。文件树一旦是
// 网页做的，它就必然是假的：没有 NSOutlineView 的展开箭头、没有原生键盘
// 导航、不在辅助功能树里、选中态也不是系统控件。终端同理。
//
// 所以这里刻意**不用** NSStackView 拼一个「像控件」的东西，而是真的：
//   - NSOutlineView（view-based，自带 disclosure triangle 与 AX 节点）
//   - NSScrollView 里的 NSTextView（真等宽字体、真选中、真 ⌘C 拷贝）
//   - NSSplitView（真分隔条、可拖）
// 数据来自 LocalAPI 打回环 /__fs 与 /__term —— 与网页面板同一个后端，
// 于是「两边看到的必须是同一份真相」这件事是可核对的。

// MARK: - 树节点

final class FSNode: NSObject {
    let name: String
    let path: String
    let isDir: Bool
    let size: Int
    /// nil = 还没展开过。刻意与「展开后确实是空目录」区分开：两者在
    /// outlineView 里长得一样（都没有子行），但对用户是天差地别的，
    /// 混成一个值就永远测不出「点了没反应」。
    var children: [FSNode]?
    var loadError: String?
    weak var parent: FSNode?

    init(name: String, path: String, isDir: Bool, size: Int) {
        self.name = name
        self.path = path
        self.isDir = isDir
        self.size = size
        super.init()
    }

    /// 数据源要的是数组；nil 给空数组而不是崩。
    var childNodes: [FSNode] { children ?? [] }
    var isExpandable: Bool { isDir }
}

final class WorkspaceCellView: NSTableCellView {
    let label = NSTextField(labelWithString: "")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        label.font = .systemFont(ofSize: 12)
        label.lineBreakMode = .byTruncatingTail
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: leadingAnchor),
            label.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        textField = label
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("nib not supported") }
}

// MARK: - 面板

// D103：五个真实环境窗格。
//
// 为什么要分段器而不是「再塞两个视图」：目标点名要「终端/浏览器/文件/代码/
// 预览等真实环境联动」，而 D100 的面板只有文件树 + 终端两个窗格，放在同一个
// NSSplitView 里彼此挤压 —— 真要按专业工作台那样同时看代码和预览，就得再切一
// 次分栏。分段器（macOS 原生 segmented control）正是工具栏那一档控件：自带
// AX 角色与选中语义，键盘 ⌘1…⌘5 可达，不自制。
enum Pane: Int, CaseIterable {
    case file = 0, term, code, browser, preview

    static let titles = ["文件", "终端", "代码", "浏览器", "预览"]

    var title: String { Pane.titles[rawValue] }
}

final class WorkspacePanel: NSView {
    private let api = LocalAPI.shared
    private let root = FSNode(name: "AgentOS", path: "", isDir: true, size: 0)
    private var project = "console"

    private let outline = NSOutlineView()
    private let termOut = NSTextView()
    private let termIn = NSTextField(string: "python3 -c \"print(6*7)\"")
    private let runBtn = NSButton(title: "运行", target: nil, action: nil)

    // ---- D103 五窗格 ----
    static let loopbackHosts: Set<String> = ["127.0.0.1", "localhost", "::1", "0.0.0.0"]
    private let seg = NSSegmentedControl()
    private let paneHost = NSView()
    private var paneViews: [Pane: NSView] = [:]
    private var activePane: Pane = .file
    private var paneSwitches = 0
    private var paneVisits: [String] = []

    private let codeHead = NSTextField(labelWithString: "未选中文件")
    private let codeOut = NSTextView()
    private var selectedFilePath = ""

    private let browserHead = NSTextField(labelWithString: "浏览器")
    private let browserURL = NSTextField(string: "")
    private let browserGo = NSButton(title: "打开", target: nil, action: nil)
    private let browserWeb = WKWebView()

    private let previewHead = NSTextField(labelWithString: "预览")
    private let previewWeb = WKWebView()

    /// 供门读的耐久事实。只记「真发生过什么」，不记「我们希望发生什么」。
    private(set) var facts: [String: Any] = [:]
    private var termRuns = 0
    private var termFailures = 0

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        build()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("nib not supported") }
    private func build() {
        // ---- 左：真 NSOutlineView ----
        let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("name"))
        col.title = "文件"
        col.width = 240
        outline.addTableColumn(col)
        outline.outlineTableColumn = col
        outline.rowSizeStyle = .default
        outline.floatsGroupRows = false
        outline.indentationPerLevel = 14
        outline.dataSource = self
        outline.delegate = self
        outline.autosaveExpandedItems = false
        outline.headerView = nil
        let treeScroll = NSScrollView()
        treeScroll.documentView = outline
        treeScroll.hasVerticalScroller = true
        treeScroll.borderType = .noBorder

        // ---- 右：真 NSTextView 终端 ----
        termOut.isEditable = false
        termOut.isSelectable = true
        termOut.font = .monospacedSystemFont(ofSize: 11.5, weight: .regular)
        termOut.textContainerInset = NSSize(width: 8, height: 6)
        let termScroll = NSScrollView()
        termScroll.documentView = termOut
        termScroll.hasVerticalScroller = true
        termScroll.borderType = .noBorder

        runBtn.target = self
        runBtn.action = #selector(runTermCommand)
        runBtn.bezelStyle = .rounded
        runBtn.font = .systemFont(ofSize: 11)
        termIn.font = .monospacedSystemFont(ofSize: 11.5, weight: .regular)
        termIn.target = self
        termIn.action = #selector(runTermCommand)
        termIn.isBezeled = true

        let inputRow = NSStackView(views: [termIn, runBtn])
        inputRow.orientation = .horizontal
        inputRow.spacing = 6
        inputRow.translatesAutoresizingMaskIntoConstraints = false

        let right = NSStackView(views: [termScroll, inputRow])
        right.orientation = .vertical
        right.spacing = 6
        right.alignment = .leading
        right.translatesAutoresizingMaskIntoConstraints = false
        termScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        inputRow.widthAnchor.constraint(equalTo: right.widthAnchor).isActive = true
        termIn.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true

        paneViews[.file] = treeScroll
        paneViews[.term] = right
        paneViews[.code] = buildCodePane()
        paneViews[.browser] = buildBrowserPane()
        paneViews[.preview] = buildPreviewPane()
        layoutPanes()
        appendLine("$ 原生终端：只读命令白名单由服务端定")
        setFactsDefaults()
    }

    /// 代码窗格：真 NSTextView（等宽、只读、可选中、可 ⌘C）。
    /// 为什么是只读：这一格的角色是「看选中文件的真实字节」，编辑权限要等
    /// 写盘链路自己有门禁的那天再开 —— 先给一个能 ⌘C 拷走的只读视图，
    /// 比给一个没有确认回路的编辑框诚实。
    private func buildCodePane() -> NSView {
        codeOut.isEditable = false
        codeOut.isSelectable = true
        codeOut.font = .monospacedSystemFont(ofSize: 11.5, weight: .regular)
        codeOut.textContainerInset = NSSize(width: 8, height: 6)
        codeOut.autoresizingMask = [.width]
        let scroll = NSScrollView()
        scroll.documentView = codeOut
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder
        codeHead.font = .systemFont(ofSize: 11, weight: .semibold)
        codeHead.lineBreakMode = .byTruncatingMiddle
        let box = NSStackView(views: [codeHead, scroll])
        box.orientation = .vertical
        box.spacing = 4
        box.alignment = .leading
        box.translatesAutoresizingMaskIntoConstraints = false
        codeHead.widthAnchor.constraint(equalTo: box.widthAnchor).isActive = true
        scroll.widthAnchor.constraint(equalTo: box.widthAnchor).isActive = true
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        return box
    }

    /// 浏览器窗格：真 WKWebView + URL 输入框。
    ///
    /// 安全边界与后端 /__web 同源，且**在客户端就先拒一道**：非 http(s)、
    /// 带凭据、非回环 host 一律不加载。后端那道不能省（任何页面都能打），
    /// 客户端这道也不能省（少一次无谓的请求，而且失败要说清是哪一条）。
    private func buildBrowserPane() -> NSView {
        browserURL.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        browserURL.isBezeled = true
        browserURL.placeholderString = "只接回环地址，例如 127.0.0.1:8790"
        browserURL.target = self
        browserURL.action = #selector(openBrowserURL)
        browserGo.target = self
        browserGo.action = #selector(openBrowserURL)
        browserGo.bezelStyle = .rounded
        browserGo.font = .systemFont(ofSize: 11)
        let row = NSStackView(views: [browserURL, browserGo])
        row.orientation = .horizontal
        row.spacing = 6
        row.translatesAutoresizingMaskIntoConstraints = false
        browserHead.font = .systemFont(ofSize: 11, weight: .semibold)
        browserWeb.navigationDelegate = self
        let box = NSStackView(views: [browserHead, row, browserWeb])
        box.orientation = .vertical
        box.spacing = 4
        box.alignment = .leading
        box.translatesAutoresizingMaskIntoConstraints = false
        for v in [browserHead, row, browserWeb] {
            v.widthAnchor.constraint(equalTo: box.widthAnchor).isActive = true
        }
        browserWeb.heightAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        browserWeb.setContentHuggingPriority(.defaultLow, for: .vertical)
        return box
    }

    /// 预览窗格：真 WKWebView，渲染选中的本地 HTML（读回环 /__fs 拿字节）。
    private func buildPreviewPane() -> NSView {
        previewHead.font = .systemFont(ofSize: 11, weight: .semibold)
        previewHead.lineBreakMode = .byTruncatingMiddle
        previewWeb.navigationDelegate = self
        let box = NSStackView(views: [previewHead, previewWeb])
        box.orientation = .vertical
        box.spacing = 4
        box.alignment = .leading
        box.translatesAutoresizingMaskIntoConstraints = false
        for v in [previewHead, previewWeb] {
            v.widthAnchor.constraint(equalTo: box.widthAnchor).isActive = true
        }
        previewWeb.heightAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        previewWeb.setContentHuggingPriority(.defaultLow, for: .vertical)
        return box
    }

    /// 把五个窗格铺进同一个宿主，一次只显示一个。
    ///
    /// 为什么用 hidden 切换而不是 add/remove：重建视图会丢掉滚动位置与
    /// WebKit 的加载状态，而「切回来还在原处」正是工作台的基本体感。
    private func layoutPanes() {
        for (_, view) in paneViews {
            view.translatesAutoresizingMaskIntoConstraints = false
            paneHost.addSubview(view)
            NSLayoutConstraint.activate([
                view.topAnchor.constraint(equalTo: paneHost.topAnchor),
                view.leadingAnchor.constraint(equalTo: paneHost.leadingAnchor),
                view.trailingAnchor.constraint(equalTo: paneHost.trailingAnchor),
                view.bottomAnchor.constraint(equalTo: paneHost.bottomAnchor),
            ])
        }
        // segmentCount 必须**先**设。NSSegmentedControl 默认是 0 段，
        // setLabel(_:forSegment:) 只写不存在的段会被丢掉 —— 于是分段器上
        // 一个标签都没有，而 selectedSegment 赋值照样让「切换」跑通。
        // 那就是一条假绿：控件活着，界面上却一个窗格入口都没有。
        seg.segmentCount = Pane.allCases.count
        for i in 0..<Pane.allCases.count {
            seg.setLabel(Pane.titles[i], forSegment: i)
        }
        seg.selectedSegment = Pane.file.rawValue
        seg.target = self
        seg.action = #selector(paneTabChanged)
        for i in 0..<Pane.allCases.count {
            seg.setToolTip(Pane.titles[i] + "窗格", forSegment: i)
        }
        seg.translatesAutoresizingMaskIntoConstraints = false
        let stack = NSStackView(views: [seg, paneHost])
        stack.orientation = .vertical
        stack.spacing = 6
        stack.alignment = .leading
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor),
            seg.widthAnchor.constraint(equalTo: stack.widthAnchor),
            paneHost.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        paneHost.setContentHuggingPriority(.defaultLow, for: .vertical)
        showPane(.file)
    }

    @objc func paneTabChanged() {
        showPane(Pane(rawValue: seg.selectedSegment) ?? .file)
    }

    // MARK: 代码窗格

    /// 把一个相对路径的真实内容读进代码窗格。
    ///
    /// 数据源是回环 `/__fs?op=read` —— 与网页面板同一后端，于是「两边看到的
    /// 必须是同一份真相」依然可核对。截断/二进制/越界一律把后端原话显示在
    /// 窗格里，不静默给一段看起来正常的假内容。
    func loadCode(path: String) {
        guard !path.isEmpty else {
            selectedFilePath = ""
            codeOut.string = ""
            codeHead.stringValue = "未选中文件"
            facts["code_path"] = ""
            facts["code_chars"] = 0
            facts["code_error"] = ""
            refreshFactCounts()
            return
        }
        api.get("__fs", ["proj": project, "path": path, "op": "read"]) { [weak self] obj, err in
            DispatchQueue.main.async {
                guard let self else { return }
                self.facts["code_loads"] = (self.facts["code_loads"] as? Int ?? 0) + 1
                self.selectedFilePath = path
                self.facts["code_path"] = path
                if let err = err {
                    self.facts["code_error"] = err
                    self.facts["code_chars"] = 0
                    self.facts["code_loaded"] = 0
                    self.codeOut.string = err
                    self.codeHead.stringValue = path + "（读取失败）"
                    self.refreshFactCounts()
                    return
                }
                let text = String(describing: obj?["text"] ?? "")
                let trunc = obj?["truncated"] as? Bool ?? false
                self.facts["code_error"] = ""
                self.facts["code_loaded"] = 1
                self.facts["code_chars"] = text.count
                self.facts["code_bytes"] = obj?["size"] as? Int ?? 0
                self.facts["code_lines"] = obj?["n_lines"] as? Int ?? 0
                self.facts["code_truncated"] = trunc
                self.facts["code_served_path"] = String(describing: obj?["path"] ?? "")
                self.codeOut.string = text
                self.codeHead.stringValue = trunc
                    ? path + "（已截断，只显示前一部分）"
                    : path + "  " + (obj?["n_lines"] as? Int ?? 0).description + " 行"
                self.refreshFactCounts()
            }
        }
    }

    /// 树里选中一个文件就把它送进代码窗格。
    private func onFilePicked(_ node: FSNode) {
        guard !node.isDir else { return }
        loadCode(path: node.path)
    }

    /// 探针用：走 NSOutlineView 自己的选中通路把代码窗格点起来。
    ///
    /// 刻意**不**直接调 loadCode。直调的话「delegate 没接上」这个变异体
    /// 量不出来 —— 代码窗格照样有内容、门照样全绿，而用户点树时其实什么都
    /// 不会发生。这里只发选中，选中之后代码窗格有没有动，全看
    /// outlineViewSelectionDidChange 有没有接上。
    func selectCodeTargetForProbe(_ done: @escaping () -> Void) {
        let prefs = [".py", ".swift", ".html", ".md", ".sh"]
        var rows: [(Int, String)] = []
        for row in 0..<outline.numberOfRows {
            guard let node = outline.item(atRow: row) as? FSNode, !node.isDir else { continue }
            rows.append((row, node.path))
        }
        var target = ""
        for want in prefs {
            if let hit = rows.first(where: { $0.1.hasSuffix(want) }) {
                target = hit.1
                break
            }
        }
        if target.isEmpty { target = rows.first?.1 ?? "" }
        facts["code_probe_file_rows"] = rows.count
        facts["code_probe_target"] = target
        guard !target.isEmpty else {
            facts["code_source"] = "no-row-fallback-direct"
            loadCode(path: "index.template.html")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { done() }
            return
        }
        if let hit = rows.first(where: { $0.1 == target }) {
            outline.selectRowIndexes(IndexSet(integer: hit.0), byExtendingSelection: false)
            outline.scrollRowToVisible(hit.0)
            facts["code_source"] = "outline-selection"
        } else {
            facts["code_source"] = "row-lookup-failed-fallback-direct"
            loadCode(path: target)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { done() }
    }

    // MARK: 浏览器窗格

    /// 只接回环。与后端 webface.url_check 同一套判据，这里是**客户端第一道**。
    ///
    /// 为什么不省掉客户端这道：省掉之后用户输错一个 host，点「打开」得到的是
    /// 一个 400 JSON —— 他看到的是「服务坏了」，而真相是「这个地址我们不接」。
    static func loopbackURL(_ raw: String) -> (URL?, String?) {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return (nil, "空地址") }
        guard let comps = URLComponents(string: s) else { return (nil, "地址解析失败") }
        guard let scheme = comps.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            return (nil, "只接 http/https（收到 " + (comps.scheme ?? "(无)") + "）")
        }
        if comps.user != nil || comps.password != nil {
            return (nil, "地址里不许带凭据")
        }
        let host = (comps.host ?? "").lowercased()
        guard loopbackHosts.contains(host) else {
            return (nil, "只接回环地址（收到 host=" + host + "）")
        }
        guard let u = comps.url else { return (nil, "地址解析失败") }
        return (u, nil)
    }

    @objc func openBrowserURL() {
        let raw = browserURL.stringValue
        facts["browser_attempts"] = (facts["browser_attempts"] as? Int ?? 0) + 1
        facts["browser_asked"] = raw
        let (u, err) = WorkspacePanel.loopbackURL(raw)
        guard let u else {
            facts["browser_rejected"] = err ?? ""
            // 刻意**不动** browser_loaded / browser_title：被拒的地址根本没
            // 发起导航，窗格里仍是上一次成功加载的那一页。把它抹成 0 才是
            // 撒谎 —— 而撒谎的方向恰好能让「拒外部地址」这道判据假绿。
            facts["browser_rejected_url"] = raw
            facts["browser_rejected_navigations"] =
                (facts["browser_rejected_navigations"] as? Int ?? 0) + 1
            browserHead.stringValue = "已拒绝：" + (err ?? "")
            browserURL.stringValue = ""
            refreshFactCounts()
            return
        }
        facts["browser_rejected"] = ""
        facts["browser_last_requested"] = u.absoluteString
        facts["browser_loaded"] = 0
        facts["browser_title"] = ""
        facts["browser_url"] = u.absoluteString
        facts["browser_base_head"] = "已加载 " + u.absoluteString
        browserHead.stringValue = "正在打开 " + u.absoluteString
        browserWeb.load(URLRequest(url: u))
        refreshFactCounts()
    }

    // MARK: 预览窗格

    /// 预览选中的本地 HTML。**读字节走回环 /__fs**，与代码窗格同一后端；
    /// 渲染走 loadHTMLString 而不是 loadFileURL —— 后者会把 file:// 权限
    /// 交给 WebKit，而我们只想渲染一段已经在沙箱里读出来的文本。
    func loadPreview(path: String) {
        let p = path.isEmpty ? "index.html" : path
        api.get("__fs", ["proj": project, "path": p, "op": "read"]) { [weak self] obj, err in
            DispatchQueue.main.async {
                guard let self else { return }
                self.facts["preview_loads"] = (self.facts["preview_loads"] as? Int ?? 0) + 1
                self.facts["preview_path"] = p
                if let err = err {
                    self.facts["preview_error"] = err
                    self.facts["preview_loaded"] = 0
                    self.previewHead.stringValue = p + "（读取失败）"
                    self.refreshFactCounts()
                    return
                }
                let text = String(describing: obj?["text"] ?? "")
                self.facts["preview_error"] = ""
                self.facts["preview_chars"] = text.count
                self.facts["preview_bytes"] = obj?["size"] as? Int ?? 0
                self.facts["preview_truncated"] = obj?["truncated"] as? Bool ?? false
                self.facts["preview_base_head"] = p + "  " + text.count.description + " 字符"
                self.previewHead.stringValue = self.facts["preview_base_head"] as? String ?? ""
                self.previewWeb.loadHTMLString(text, baseURL: nil)
                self.refreshFactCounts()
            }
        }
    }

    /// 探针用的浏览器加载：先塞地址再走真实 target-action（按钮 + 回车同一条路）。
    func loadBrowserForProbe(_ raw: String, done: @escaping () -> Void) {
        browserURL.stringValue = raw
        openBrowserURL()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) {
            self.refreshFactCounts()
            done()
        }
    }

    /// 浏览器与预览共用一个 navigation delegate，所以每条事实都要先认领
    /// 「我是哪个窗格」再落。不认领的话，浏览器加载成功会把预览的 loaded
    /// 顺手覆盖成 1 —— 而那正是两个 WKWebView 共用 delegate 时最典型的假绿：
    /// 门全绿，而预览格其实什么都没渲染。
    func recordNav(_ w: WKWebView, _ err: Error?) {
        let isBrowser = (w === browserWeb)
        let isPreview = (w === previewWeb)
        guard isBrowser || isPreview else { return }
        if isBrowser {
            if let err {
                facts["browser_loaded"] = 0
                facts["browser_error"] = err.localizedDescription
                browserHead.stringValue = "加载失败：" + err.localizedDescription
            } else {
                facts["browser_loaded"] = 1
                facts["browser_error"] = ""
                facts["browser_title"] = w.title ?? ""
                facts["browser_url"] = w.url?.absoluteString ?? ""
                readRendered(of: w, charsKey: "browser_rendered_chars", titleKey: "browser_dom_title",
                             label: { [weak self] t in
                    let base = self?.facts["browser_base_head"] as? String ?? ""
                    self?.browserHead.stringValue = base + " / " + t
                })
            }
        }
        if isPreview {
            if let err {
                facts["preview_loaded"] = 0
                facts["preview_error"] = err.localizedDescription
                previewHead.stringValue = "渲染失败：" + err.localizedDescription
            } else {
                facts["preview_loaded"] = 1
                facts["preview_error"] = ""
                facts["preview_title"] = w.title ?? ""
                readRendered(of: w, charsKey: "preview_rendered_chars", titleKey: "preview_dom_title",
                             label: { [weak self] t in
                    let base = self?.facts["preview_base_head"] as? String ?? ""
                    self?.previewHead.stringValue = base + " / " + t
                })
            }
        }
        refreshFactCounts()
    }

    /// didFinish 只说明「导航完成了」，不说明「画出了东西」—— WebKit 的失败页、
    /// 空白页也都会 didFinish。所以再问一次文档里到底有多少可见字符。
   /// 没有这一问，浏览器窗格那道判据量的就是导航事件，不是用户看到的画面。
    /// 顺带把 document.title 也读回来：WKWebView 自带的 `title` 属性在
    /// didFinish 那一刻常常还是空的（文档还没提交标题），而
    /// `document.title` 是从已解析的 DOM 里读的 —— 于是「这一页真的解析过、
    /// 标题就是它自己写的那个」有了独立于导航事件的旁证。
    private func readRendered(of w: WKWebView, charsKey: String, titleKey: String,
                              label: @escaping (String) -> Void) {
        let js = "({t: document.title || '', n: (document.body && document.body.innerText || '').length})"
        w.evaluateJavaScript(js) { [weak self] v, _ in
            let m = v as? [String: Any]
            let n = (m?["n"] as? NSNumber)?.intValue ?? -1
            let t = (m?["t"] as? String) ?? ""
            DispatchQueue.main.async {
                self?.facts[charsKey] = n
                self?.facts[titleKey] = t
                label(t.isEmpty ? "(无标题)" : t)
                self?.refreshFactCounts()
            }
        }
    }

    /// 切窗格。事实只记「真的切过几次、真的到过哪几个」，由探针走真实
    /// segmented control 的 target-action，不直接调这个方法。
    func showPane(_ pane: Pane) {
        activePane = pane
        paneSwitches += 1
        if paneVisits.last != pane.title { paneVisits.append(pane.title) }
        for (p, v) in paneViews { v.isHidden = (p != pane) }
        facts["pane_active"] = pane.title
        facts["pane_switches"] = paneSwitches
        facts["pane_visits"] = paneVisits
        facts["pane_visible_count"] = paneViews.values.filter { !$0.isHidden }.count
        refreshFactCounts()
    }

    /// 探针用：走 segmented control 的真实 target-action 依次点亮五个窗格。
    func tourPanesForProbe(_ done: @escaping () -> Void) {
        let order: [Pane] = [.code, .browser, .preview, .term]
        var i = 0
        func step() {
            guard i < order.count else {
                driveSeg(.file)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                    self.refreshFactCounts()
                    done()
                }
                return
            }
            driveSeg(order[i])
            i += 1
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35, execute: step)
        }
        step()
    }

    /// 走 segmented control 的 target-action 切窗格（探针与用户点的是同一条路）。
    func driveSeg(_ pane: Pane) {
        seg.selectedSegment = pane.rawValue
        seg.sendAction(seg.action, to: seg.target)
    }

    /// 五个窗格的内容探针：每一格都要真的被灌上真内容。
    ///
    /// 为什么不并进 tourPanesForProbe：分段器切得动只能证明「控件活着」，
    /// 证明不了「那一格里有东西」。一个空的代码窗格和一个装了真实字节的
    /// 代码窗格，用户体感完全不同，而像素也量不出来 —— NSTextView 里只有
    /// 几行字时，整格照样是满屏白，distinct/ink 两边都过。所以每一格都走
    /// 「切过去 → 真的灌内容 → 等它回来 → 再切下一格」。
    func paneContentSequenceForProbe(_ done: @escaping () -> Void) {
        let previewTarget = "index.template.html"
        let liveTarget = "http://127.0.0.1:8790/"
        let hostileTarget = "http://example.com/steal"
        driveSeg(.code)
        selectCodeTargetForProbe { [weak self] in
            guard let self else { done(); return }
            self.loadBrowserForProbe(liveTarget) {
                // 先记下成功那一次的状态，**再**去试外部地址：客户端那道
                // 回环闸必须真的挡住，而挡住之后 facts 里还要留着
                // 「刚才那一页仍然是活的」这一条，否则顺序一变就假红。
                self.facts["browser_live_loaded"] = self.facts["browser_loaded"] ?? 0
                self.facts["browser_live_title"] = self.facts["browser_title"] ?? ""
                self.facts["browser_live_url"] = self.facts["browser_url"] ?? ""
                self.browserURL.stringValue = hostileTarget
                self.openBrowserURL()
                self.facts["browser_hostile_asked"] = hostileTarget
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                    guard let self else { done(); return }
                    self.driveSeg(.preview)
                    self.loadPreview(path: previewTarget)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [weak self] in
                        guard let self else { done(); return }
                        self.driveSeg(.file)
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                            self.refreshFactCounts()
                            done()
                        }
                    }
                }
            }
        }
    }

    private func setFactsDefaults() {
        facts["panel_kind"] = "NSSplitView+NSOutlineView+NSTextView"
        facts["panel_api"] = "LocalAPI:/__fs+/__term"
        facts["term_input_wired"] = termIn.target != nil && termIn.action != nil
        facts["term_run_wired"] = runBtn.target != nil && runBtn.action != nil
        facts["term_textview_editable"] = termOut.isEditable
        facts["term_textview_selectable"] = termOut.isSelectable
        facts["term_textview_monospaced"] = termOut.font?.isFixedPitch ?? false
        facts["term_input_placeholder"] = termIn.stringValue
        facts["outline_columns"] = outline.tableColumns.count
        facts["term_argv_of_placeholder"] = WorkspacePanel.parseArgv(termIn.stringValue)
        facts["pane_count"] = Pane.allCases.count
        facts["pane_titles"] = Pane.titles
        facts["pane_active"] = activePane.title
        facts["code_textview_editable"] = codeOut.isEditable
        facts["code_error"] = ""
        facts["code_loaded"] = 0
        facts["browser_error"] = ""
        facts["browser_loaded"] = 0
        facts["browser_rejected"] = ""
        facts["preview_error"] = ""
        facts["preview_loaded"] = 0
    }

    // MARK: 终端

    private func append(_ s: String, _ color: NSColor) {
        let font = termOut.font ?? NSFont.systemFont(ofSize: 11)
        termOut.textStorage?.append(NSAttributedString(string: s + "\n",
                                                     attributes: [.font: font,
                                                                  .foregroundColor: color]))
    }

    private func appendLine(_ s: String) { append(s, .labelColor) }
    private func appendError(_ s: String) { append(s, .systemRed) }

    /// 把一行输入切成 argv。白空格分隔 + 成对引号包裹，不做 shell 语法。
    ///
    /// D100b 首跑修正：原来只按空格切，于是**输入框自己的默认值**
    /// `python3 -c "print(6*7)"` 被切成 `["python3","-c","\"print(6*7)\""]`，
    /// 后端回 rc=0、out="" —— 一次「成功但什么都没算」的命令。门若只判
    /// rc==0，就会把静默空转判成通过，这正是本项目最恨的那类假绿。
    /// 所以这里支持成对引号，让输入框的默认值真的能算出 42。
    ///
    /// 仍然**刻意不做**管道 / 重定向 / 变量展开 / glob：一个「看起来像
    /// shell」的前端会让人以为那些都能用，而后端白名单一条都不给，
    /// 那种落差比明说「不支持」更糟。
    static func parseArgv(_ line: String) -> [String] {
        var out: [String] = []
        var cur = ""
        var started = false
        var quote: Character?
        for ch in line {
            if let q = quote {
                if ch == q { quote = nil } else { cur.append(ch) }
                continue
            }
            if ch == "\"" || ch == "'" { quote = ch; started = true; continue }
            if ch == " " || ch == "\t" {
                if started { out.append(cur); cur = ""; started = false }
                continue
            }
            cur.append(ch); started = true
        }
        if started { out.append(cur) }
        return out
    }
    @objc func runTermCommand() {
        let line = termIn.stringValue
        let argv = WorkspacePanel.parseArgv(line)
        guard !argv.isEmpty else {
            appendError("空命令")
            refreshFactCounts()
            return
        }
        appendLine("$ " + line)
        termRuns += 1
        facts["term_runs"] = termRuns
        // argv 落进证据：门要能判「后端真的收到了切好的 argv」，
        // 而不是只看一个自报的 ok=1。
        facts["term_last_argv"] = argv
        api.term(argv) { [weak self] obj, err in
            DispatchQueue.main.async {
                guard let self else { return }
                if let err = err {
                    self.termFailures += 1
                    self.facts["term_failures"] = self.termFailures
                    self.facts["term_last_ok"] = 0
                    self.facts["term_last_error"] = err
                    self.appendError("[x] " + err)
                    self.refreshFactCounts()
                    return
                }
                let rc = obj?["rc"] as? Int ?? -1
                let out = String(describing: obj?["out"] ?? "")
                self.facts["term_last_ok"] = 1
                self.facts["term_last_rc"] = rc
                self.facts["term_last_out_len"] = out.count
                self.facts["term_last_out"] = out
                // 后端回传的 argv（它会把 argv[0] 解析成绝对路径）也记下来，
                // 门据此确认「真跑的就是这条命令」。
                self.facts["term_last_served_argv"] = obj?["argv"] as? [String] ?? []
                self.facts["term_last_truncated"] = obj?["truncated"] as? Bool ?? false
                self.appendLine((rc == 0 ? "[ok] rc=0" : "[!!] rc=\(rc)") + " " + out)
                self.refreshFactCounts()
            }
        }
    }

    /// 把「此刻真长什么样」重新数一遍写进 facts。为什么要现算而不是记流水：
    /// 控件随时可能被用户改，而门要判的是**落盘那一刻**的屏，不是历史。
    func refreshFactCounts() {
        let text = termOut.textStorage?.string ?? ""
        facts["outline_rows"] = outline.numberOfRows
        facts["outline_root_children"] = root.childNodes.count
        facts["outline_selected_rows"] = outline.selectedRowIndexes.count
        facts["term_lines"] = text.isEmpty ? 0 : text.split(separator: "\n").count
        facts["term_textview_len"] = text.count
        // 五个窗格的「此刻真长什么样」。为什么要现算而不记流水：窗格是
        // hidden 切换的，用户随时能点，而门要判的是**落盘那一刻**的屏。
        facts["pane_count"] = paneViews.count
        facts["pane_active"] = activePane.title
        facts["pane_switches"] = paneSwitches
        facts["pane_visits"] = paneVisits
        facts["pane_visible_count"] = paneViews.values.filter { !$0.isHidden }.count
        facts["pane_titles"] = Pane.titles
        facts["pane_segment_count"] = seg.segmentCount
        facts["pane_segment_selected"] = seg.selectedSegment
        facts["code_chars"] = codeOut.string.count
        facts["code_textview_editable"] = codeOut.isEditable
        facts["code_textview_selectable"] = codeOut.isSelectable
        facts["code_head"] = codeHead.stringValue
        facts["code_selected_path"] = selectedFilePath
        facts["browser_asked"] = facts["browser_asked"] ?? ""
        facts["browser_attempts"] = facts["browser_attempts"] ?? 0
        facts["browser_head"] = browserHead.stringValue
        facts["preview_head"] = previewHead.stringValue
    }

    // MARK: 文件树

    func reloadTree(done: @escaping () -> Void) {
        facts["tree_loads"] = (facts["tree_loads"] as? Int ?? 0) + 1
        api.get("__fs", ["proj": project, "path": "", "depth": "1"]) { [weak self] obj, err in
            DispatchQueue.main.async {
                guard let self else { return }
                if let err = err {
                    self.root.children = []
                    self.root.loadError = err
                    self.facts["tree_error"] = err
                    self.facts["tree_root_ok"] = 0
                    self.outline.reloadData()
                    self.refreshFactCounts()
                    done()
                    return
                }
                self.root.children = self.makeNodes(obj?["entries"] as? [[String: Any]] ?? [])
                self.root.loadError = nil
                self.facts["tree_error"] = ""
                self.facts["tree_root_ok"] = 1
                self.outline.reloadData()
                self.outline.expandItem(self.root)
                self.refreshFactCounts()
                // 记下前几个真实名字 —— 门要能判「树里真的有那些真文件」，
                // 而不是「有一个 outlineView 控件摆在那儿」。
                self.facts["tree_names_head"] = self.root.childNodes.prefix(6).map(\.name)
                self.facts["tree_dirs_head"] = self.root.childNodes.filter(\.isDir).prefix(4).map(\.name)
                done()
            }
        }
    }

    private func makeNodes(_ entries: [[String: Any]]) -> [FSNode] {
        entries.map { e in
            let n = FSNode(name: String(describing: e["name"] ?? ""),
                           path: String(describing: e["path"] ?? ""),
                           isDir: e["dir"] as? Bool ?? false,
                           size: e["size"] as? Int ?? 0)
            n.parent = root
            return n
        }
    }

    /// 展开一个目录时才去取它的子项（懒加载）。
    private func loadChildren(of node: FSNode) {
        if node.children != nil { return }
        api.get("__fs", ["proj": project, "path": node.path, "depth": "1"]) { [weak self] obj, err in
            DispatchQueue.main.async {
                guard let self else { return }
                if let err = err {
                    node.children = []
                    node.loadError = err
                    self.facts["tree_last_expand_error"] = err
                } else {
                    let kids = self.makeNodes(obj?["entries"] as? [[String: Any]] ?? [])
                    kids.forEach { $0.parent = node }
                    node.children = kids
                    self.facts["tree_last_expand_path"] = node.path
                    self.facts["tree_last_expand_n"] = kids.count
                }
                self.facts["tree_expands"] = (self.facts["tree_expands"] as? Int ?? 0) + 1
                self.outline.reloadData()
                self.refreshFactCounts()
            }
        }
    }

    // MARK: 探针

    /// 走真实控件路径展开一个目录（不直接调 API），这样「delegate 没接上」
    /// 这类变异体一定会被门抓到。
    ///
    /// D100b 修正：原来取的是「第一个目录」，而根目录第一个恰好是
    /// `AgentOS.app` 这个包 —— 展开它只回 1 个子项（Contents），
    /// 量到的展开深度毫无代表性。改成跳过 .app/.bundle/.framework，
    /// 让探针去展开一个用户真的会展开的普通目录。
    func expandFirstDirectoryForProbe(done: @escaping () -> Void) {
        let bundles = [".app", ".bundle", ".framework"]
        let pick = { (n: FSNode) in
            !n.isDir ? false : !bundles.contains { n.name.hasSuffix($0) }
        }
        guard let first = root.childNodes.first(where: pick)
                ?? root.childNodes.first(where: { $0.isDir }) else {
            facts["panel_expand_target"] = ""
            facts["panel_expand_skipped_bundles"] = bundles
            done()
            return
        }
        facts["panel_expand_target"] = first.path
        facts["panel_expand_skipped_bundles"] = bundles
        let idx = root.childNodes.firstIndex(of: first) ?? 0
        facts["panel_expand_index"] = idx
        // expandItem(at:ofItem:) 在 AppKit 里**不存在**（实测 swiftc 报
        // extra argument 'ofItem'）。展开一个已知的子节点用 expandItem(_:)，
        // 它同样会发 NSOutlineViewItemDidExpandNotification，所以 delegate
        // 那条懒加载路径照样被走到 —— 我们要的正是「走控件路径」。
        outline.expandItem(first)
        // expandItem 同步触发 delegate；给它一拍让网络回来。
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) {
            self.refreshFactCounts()
            done()
        }
    }

    func probeSummary() -> [String: Any] {
        refreshFactCounts()
        return facts
    }

    /// 面板里**真实存在的文本控件**清单。
    ///
    /// 为什么单独量这一份：已有 a11y 门量的是 WKWebView 里那份 DOM 的
    /// aria 覆盖，而 D100 把工作区搬到了 AppKit —— 原生控件的可见文本
    /// 不在 DOM 里，那道门量不到这里。NSOutlineView 的行是 cell 里的
    /// NSTextField，所以要自己遍历视图树才能确认「屏幕上真有这些字」。
    func axSummary() -> [String: Any] {
        var items: [String] = []
        func walk(_ v: NSView) {
            if let t = v as? NSTextField, !t.stringValue.isEmpty {
                items.append(t.stringValue)
            }
            // NSButton 的标题画在 cell 上，没有 NSTextField 子视图，所以
            // 只遍历 NSTextField 会漏掉所有按钮 —— 而「运行」这个按钮恰恰
            // 是辅助功能用户找得到、键盘用户按 ⏎ 能触发的那个。
            if let b = v as? NSButton, !b.title.isEmpty {
                items.append(b.title)
            }
            for s in v.subviews { walk(s) }
        }
        walk(self)
        let termText = termOut.string
        return [
            "panel_ax_enabled": AXIsProcessTrusted(),
            "panel_ax_texts": Array(items.prefix(12)),
            "panel_ax_texts_count": items.count,
            "panel_ax_has_tree": items.contains { $0.contains("tools") },
            "panel_ax_has_run_button": items.contains { $0.contains("运行") },
            "panel_ax_has_browser_go": items.contains { $0.contains("打开") },
            "panel_ax_term_chars": termText.count,
            "panel_ax_term_has_result": termText.contains("42"),
            // 窗格分段器那五个标题是**真控件的标签**，不进 DOM，所以网页侧
            // 那道 aria 门量不到。AX 树的可见标签要在这里单独数一遍。
            "panel_ax_pane_segment_count": seg.segmentCount,
            "panel_ax_pane_titles": Pane.titles,
            "panel_ax_pane_active": activePane.title,
            "panel_ax_pane_selected": seg.selectedSegment,
            "panel_ax_pane_has_all_five": seg.segmentCount == Pane.allCases.count,
            "panel_ax_code_editable": codeOut.isEditable,
            "panel_ax_code_chars": codeOut.string.count,
            "panel_ax_code_readonly_label": codeHead.stringValue,
        ]
    }
}

// MARK: - NSOutlineViewDataSource

extension WorkspacePanel: NSOutlineViewDataSource {
    func outlineView(_ v: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int {
        (item as? FSNode ?? root).childNodes.count
    }

    func outlineView(_ v: NSOutlineView, child index: Int, ofItem item: Any?) -> Any {
        (item as? FSNode ?? root).childNodes[index]
    }

    func outlineView(_ v: NSOutlineView, isItemExpandable item: Any) -> Bool {
        (item as? FSNode)?.isExpandable ?? false
    }
}

// MARK: - NSOutlineViewDelegate

extension WorkspacePanel: NSOutlineViewDelegate {
    func outlineView(_ v: NSOutlineView, viewFor tableColumn: NSTableColumn?,
                     item: Any) -> NSView? {
        guard let node = item as? FSNode else { return nil }
        let cell = WorkspaceCellView()
        cell.label.stringValue = node.isDir ? "\(node.name)/" : "\(node.name)  \(node.size)B"
        cell.label.textColor = node.isDir ? .labelColor : .secondaryLabelColor
        cell.label.font = node.isDir
            ? .systemFont(ofSize: 12, weight: .semibold)
            : .systemFont(ofSize: 12)
        cell.toolTip = node.path.isEmpty ? node.name : node.path
        return cell
    }

    func outlineViewItemDidExpand(_ notification: Notification) {
        guard let node = notification.userInfo?["NSObject"] as? FSNode else { return }
        loadChildren(of: node)
    }

    /// 在树里选中一行，就把它的真实字节送进代码窗格。
    ///
    /// 这一条是「文件 → 代码」联动的**唯一**通路，所以它必须挂在 delegate
    /// 上而不是塞进别的方法里。挂着它，「点得动但代码窗格不动」这个变异体
    /// 一定会被门抓到；不挂它（曾经就是那样）则代码窗格永远停在
    /// 「未选中文件」，而那个分段器看上去一切正常 —— 正是本项目最恨的假绿。
    func outlineViewSelectionDidChange(_ notification: Notification) {
        guard outline.selectedRow >= 0,
              let node = outline.item(atRow: outline.selectedRow) as? FSNode else { return }
        facts["tree_selected_path"] = node.path
        facts["tree_selected_is_dir"] = node.isDir
        onFilePicked(node)
    }
}

// MARK: - WKNavigationDelegate

extension WorkspacePanel: WKNavigationDelegate {
    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) { recordNav(w, nil) }
    func webView(_ w: WKWebView, didFail navigation: WKNavigation!, withError e: Error) { recordNav(w, e) }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { recordNav(w, e) }
}
