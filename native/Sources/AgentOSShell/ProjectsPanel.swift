import AppKit

// D105：左侧「项目与任务空间」。
//
// 为什么这一格不许长回网页里：WKWebView 天生会 fetch，所以「项目列表」
// 极易又变成网页里的一张 <ul>，那原生化就停在皮上。这里用**真 NSTableView**
// + **原生 URLSession 直取 /__projects**，数据不经过页面，于是
// 「原生侧自己拿到了项目树」这件事是可证的：探针记的是 TableView 的
// 行数与选中行，不是页面 DOM 的节点数。
//
// 三条纪律与 LocalAPI / serve.py 同源，不许在这里放松：
//   1. 只读 /__projects，不写任何路径；
//   2. 后端 error 原样透出，不做兜底成功；
//   3. 拿不到就显示「未取到」并把 reason 记进证据，绝不静默变成空列表
//      —— 空列表在视觉上与「真的没有项目」不可区分。
final class ProjectsPanel: NSView, NSTableViewDataSource, NSTableViewDelegate {
    private struct Row {
        let key: String
        let label: String
        let role: String
        let root: String
        let nFiles: Int
        let active: Bool
    }

    private let title = NSTextField(labelWithString: "项目与任务空间")
    private let table = NSTableView()
    private let scroll = NSScrollView()
    private let status = NSTextField(labelWithString: "正在读取 /__projects …")
    private let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("proj"))
    private var rows: [Row] = []
    private var loadError = ""
    private var loaded = false
    private var arrowMoves = 0
    private var lastSelectedKey = ""

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        build()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("nib not supported") }

    /// ⌘P 把第一响应者交给这一格；它自己再转交给 TableView，
    /// 于是 ↓↑ 立刻作用在行选择上，而不是停在一个不处理按键的容器上。
    override var acceptsFirstResponder: Bool { true }
    override func becomeFirstResponder() -> Bool { table.window?.makeFirstResponder(table) ?? false }

    /// ⌘P 的验收口。判「行选择器真的拿到焦点了吗」而不是判容器自己：
    /// 上面的 `becomeFirstResponder` 会把焦点**转交**给 table，所以
    /// `window.firstResponder` 是 table、永远不是本面板 —— 拿 `=== projects`
    /// 去判会恒假（D105 实测踩实，`native_menu_cmdp_result_focused` 读回 0）。
    /// 真正要保的语义是「↓↑ 立刻作用在行选择上」，那就该量 table。
    func tableHasFocusForProbe() -> Bool { window?.firstResponder === table }

    // MARK: - 构造

    private func build() {
        wantsLayer = true
        title.font = .systemFont(ofSize: 11, weight: .semibold)
        title.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 10)
        status.textColor = .tertiaryLabelColor
        status.lineBreakMode = .byTruncatingTail

        column.title = "项目"
        column.width = 200
        table.addTableColumn(column)
        table.dataSource = self
        table.delegate = self
        table.headerView = nil
        table.rowHeight = 46
        table.selectionHighlightStyle = .sourceList
        table.allowsMultipleSelection = false
        table.setAccessibilityLabel("项目列表")
        table.setAccessibilityRole(.outline)
        table.target = self
        table.action = #selector(onRowActivated(_:))

        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.setAccessibilityLabel("项目与任务空间滚动区")

        for v in [title, scroll, status] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        NSLayoutConstraint.activate([
            title.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            title.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            title.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            scroll.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 6),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            status.topAnchor.constraint(equalTo: scroll.bottomAnchor, constant: 6),
            status.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            status.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            status.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -8),
        ])
    }

    // MARK: - 取数

    func reloadProjects(_ done: @escaping () -> Void) {
        loaded = false
        loadError = ""
        LocalAPI.shared.get("__projects", [:]) { [weak self] obj, err in
            guard let self else { done(); return }
            // LocalAPI 的 done 跑在 URLSession 的 delegate queue 上，不是主线程。
            // 下面整段都在动 NSTableView（rows / reloadData / status / selectRow），
            // 离线程做这些会撞上 AppKit 的内部状态：实测直接 SIGABRT 于
            // -[NSTableRowData updateVisibleRowViews]。WorkspacePanel 每一处
            // completion 都包了 DispatchQueue.main.async，这里必须同构。
            DispatchQueue.main.async {
                self.applyProjects(obj, err: err, done: done)
            }
        }
    }

    /// 只许在主线程调用：reloadProjects 的 completion 落地处。
    private func applyProjects(_ obj: [String: Any]?, err: String?,
                              done: @escaping () -> Void) {
        defer { done() }
        guard let obj, obj["ok"] as? Bool != false else {
            loadError = err ?? "未知错误"
            status.stringValue = "项目未取到：\(loadError)"
            rows = []
            table.reloadData()
            return
        }
        let active = obj["active"] as? String ?? ""
        let arr = obj["projects"] as? [[String: Any]] ?? []
        rows = arr.map { p in
            let facts = p["facts"] as? [String: Any] ?? [:]
            return Row(
                key: p["key"] as? String ?? "",
                label: p["label"] as? String ?? "",
                role: p["role"] as? String ?? "",
                root: p["root"] as? String ?? "",
                nFiles: facts["n_files"] as? Int ?? 0,
                active: (p["key"] as? String ?? "") == active)
        }
        loaded = true
        status.stringValue = rows.isEmpty
            ? "项目未取到：后端返回 0 个项目"
            : "\(rows.count) 个项目 · 当前 \(active)"
        table.reloadData()
        // reloadData 只是标脏，行视图要等下一次 layout 才真的建出来。
        // 这里立刻强制走一次 layout，下面 probeSummary/axSummary 才敢按行
        // 去问 view(atColumn:row:) —— 否则拿到的是半成品的 row cache。
        table.layoutSubtreeIfNeeded()
        if !rows.isEmpty { selectRow(0) }
    }

    // MARK: - 键盘 / 选中

    /// 真按 ↓ 键走一次：NSResponder 链上的 moveDown 是 NSTableView 自己实现的，
    /// 这里发的是真 NSEvent，不是直接改 selectedRow —— 后者只能证明赋值能跑，
    /// 证明不了键盘路径通。
    func pressDownKeyForProbe() {
        guard !rows.isEmpty else { return }
        window?.makeFirstResponder(table)
        let e = NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: [],
            timestamp: 0, windowNumber: window?.windowNumber ?? 0,
            context: nil, characters: "\u{F703}", charactersIgnoringModifiers: "\u{F703}",
            isARepeat: false, keyCode: 125)
        if let e { table.keyDown(with: e) }
        arrowMoves += 1
        lastSelectedKey = selectedKey()
    }

    func selectRow(_ i: Int) {
        guard i >= 0, i < rows.count else { return }
        table.selectRowIndexes(IndexSet(integer: i), byExtendingSelection: false)
        table.scrollRowToVisible(i)
        lastSelectedKey = selectedKey()
    }

    func selectedKey() -> String {
        let i = table.selectedRow
        return (i >= 0 && i < rows.count) ? rows[i].key : ""
    }

    @objc private func onRowActivated(_ sender: Any?) {
        lastSelectedKey = selectedKey()
    }

    // MARK: - NSTableViewDataSource

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(_ tableView: NSTableView,
                   viewFor tableColumn: NSTableColumn?,
                   row: Int) -> NSView? {
        guard row >= 0, row < rows.count else { return nil }
        let r = rows[row]
        let cell = NSTextField(labelWithString: "")
        cell.lineBreakMode = .byTruncatingTail
        cell.font = .systemFont(ofSize: 11)
        let badge = r.active ? "● " : "○ "
        cell.stringValue = "\(badge)\(r.label)\n\(r.nFiles) 文件 · \(r.role)"
        cell.textColor = r.active ? .labelColor : .secondaryLabelColor
        cell.toolTip = r.root
        cell.setAccessibilityLabel("\(r.label)，\(r.role)，\(r.nFiles) 个文件")
        return cell
    }

    // MARK: - 证据

    func probeSummary() -> [String: Any] {
        // 按 table.numberOfRows 取，不按 rows.count：model 数组与 AppKit
        // 真正建出来的行数在 reloadData 之后的那一帧可以不一致，用后者
        // 去问 view(atColumn:row:) 就会越界。
        let n = min(rows.count, max(0, table.numberOfRows))
        return [
            "proj_loaded": loaded ? 1 : 0,
            "proj_error": loadError,
            "proj_count": rows.count,
            "proj_keys": rows.map(\.key),
            "proj_labels": rows.map(\.label),
            "proj_active_flag": rows.filter(\.active).map(\.key),
            "proj_nfiles": rows.map(\.nFiles),
            "proj_roots_absolute": rows.allSatisfy { $0.root.hasPrefix("/") },
            "proj_selected_key": selectedKey(),
            "proj_selected_is_active": rows.first { $0.key == selectedKey() }?.active ?? false,
            "proj_row_view_count": (0..<n).compactMap {
                table.view(atColumn: 0, row: $0, makeIfNecessary: true)
            }.count,
            "proj_arrow_moves": arrowMoves,
            "proj_key_after_arrow": lastSelectedKey,
            "proj_status_text": status.stringValue,
        ]
    }

    func axSummary() -> [String: Any] {
        var labels: [String] = []
        let n = min(rows.count, max(0, table.numberOfRows))
        for i in 0..<n {
            if let v = table.view(atColumn: 0, row: i, makeIfNecessary: true) as? NSTextField {
                labels.append(v.stringValue)
            }
        }
        return [
            "proj_ax_has_header": title.stringValue.contains("项目"),
            "proj_ax_header": title.stringValue,
            "proj_ax_table_label": table.accessibilityLabel() ?? "",
            "proj_ax_row_count": labels.count,
            "proj_ax_rows": Array(labels.prefix(4)),
            "proj_ax_rows_named": labels.filter { !$0.isEmpty }.count,
            "proj_ax_selected_row": table.selectedRow,
        ]
    }
}
