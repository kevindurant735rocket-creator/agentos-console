import AppKit

// D99：服务没起来时，原生 App 显示的是**它自己的一块原生面板**（不是白屏，
// 也不是浏览器里那句 fetch 失败）。这是「原生软件该有的样子」最基本的一条：
// 失败路径也得是产品。
final class RecoveryPanel: NSVisualEffectView {
    private let heading = NSTextField(labelWithString: "AgentOS 服务未运行")
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let startButton = NSButton(title: "启动服务", target: nil, action: nil)
    private let retryButton = NSButton(title: "重新检测", target: nil, action: nil)

    var onStart: (() -> Void)?
    var onRetry: (() -> Void)?

    init() {
        super.init(frame: .zero)
        material = .contentBackground
        blendingMode = .behindWindow
        state = .active

        let glyph = NSTextField(labelWithString: "◍")
        glyph.font = .systemFont(ofSize: 46, weight: .light)
        glyph.textColor = .secondaryLabelColor

        heading.font = .systemFont(ofSize: 20, weight: .semibold)
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor

        startButton.target = self
        startButton.action = #selector(tapStart)
        startButton.bezelStyle = .rounded
        retryButton.target = self
        retryButton.action = #selector(tapRetry)
        retryButton.bezelStyle = .rounded

        let buttons = NSStackView(views: [startButton, retryButton])
        buttons.orientation = .horizontal
        buttons.spacing = 10

        let stack = NSStackView(views: [glyph, heading, detail, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.setCustomSpacing(20, after: glyph)
        stack.setCustomSpacing(20, after: detail)
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: 460),
            detail.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("nib not supported") }

    func setDetail(_ s: String) { detail.stringValue = s }

    @objc private func tapStart() { onStart?() }
    @objc private func tapRetry() { onRetry?() }

    /// 降级面探针的出口。走真实 NSButton 的 target/action，
    /// 所以「按钮摘掉了接线」这种变异体一定会被门抓到。
    func tapStartForProbe() { startButton.performClick(nil) }

    func probeSummary() -> [String: Any] {
        ["start_title": startButton.title,
         "retry_title": retryButton.title,
         "start_wired": startButton.target != nil && startButton.action != nil,
         "retry_wired": retryButton.target != nil && retryButton.action != nil,
         "heading": heading.stringValue]
    }
}
