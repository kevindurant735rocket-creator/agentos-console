import AppKit
import ApplicationServices

// D99 入口。手写 NSApplicationMain 而不是 @main + SwiftUI：
// 标题栏合并 / 交通灯 / NSToolbar / WKWebView 都要 AppKit 全权控制，
// SwiftUI 那层只会挡在中间。
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)

// D107：单实例。第二次双击 AgentOS 不该开出第二个窗口。
//
// 为什么这是产品缺陷而不只是量具噪音：macOS 上一个 .app 被双击两次，
// 用户的直觉是「它已经在跑了，把那个窗口叫到前面来」。而实际发生的是
// 两个独立进程各开一个窗口，Dock 图标跳来跳去，两份 WKWebView 各自轮询
// 同一个 8790 服务，谁都不知道对方存在。D105 实测的 AX 间歇塌陷
// （同一份构建，有时连续 18s 每秒读到 1 窗，有时连续 25s 全 0 窗）
// 正是两个实例在窗口服务器上互相打架时的典型症状。
//
// 所以正确顺序是：**先探测再决定要不要跑**。已有实例在就把它的窗口叫到
// 前面，自己安静退出，用户看到的是窗口被唤起而不是多了一个窗口。
//
// 「叫到前面」是两件事，不是一件：`activate` 切前台/切 Space，
// `AXMinimized=false` 把缩在 Dock 里的窗口拉回来。D105 实测踩过前者的
// 不足：只 activate 时，若对方窗口正处于最小化态，用户看到的是
// Dock 图标跳了一下、窗口依然没出来。跨进程碰不了别人的 NSWindow，
// 只能走 AX（也正因如此需要辅助功能权限；没有权限时如实退回 activate，
// 并把结果打到 stderr，不假装成功）。
//
// 逃生阀 AGENTOS_MULTI_INSTANCE=1：门必须能在用户自己的实例还开着的时候
// 再起一个探针实例。没有这个阀，单实例逻辑会把门自己杀掉（门起的第二个
// 进程发现第一个在，转身就退，probe.json 永不落盘）。
func takeOverExistingInstance() -> NSRunningApplication? {
    if ProcessInfo.processInfo.environment["AGENTOS_MULTI_INSTANCE"] == "1" { return nil }
    guard let bid = Bundle.main.bundleIdentifier, !bid.isEmpty else { return nil }
    let me = ProcessInfo.processInfo.processIdentifier
    let others = NSRunningApplication.runningApplications(withBundleIdentifier: bid)
        .filter { Int($0.processIdentifier) != me && !$0.isTerminated }
    guard let other = others.first else { return nil }
    // activate 之前先把 Dock 图标弹到前面，否则 macOS 可能只切 Space
    // 而不把窗口叫出来（用户会看到「什么都没发生」）。
    other.activate(options: [])
    let unminiaturized = unminimizeWindows(of: other)
    FileHandle.standardError.write(Data(
        "[agentos] takeover: unminimize=\(unminiaturized)\n".utf8))
    return other
}

/// 把另一个进程里处于最小化态的窗口拉回来。返回**是否真的动过手**，
/// 不返回「大概成功了」—— 拿不到辅助功能权限时 AX 会直接报错，
/// 那种情况下 false 是唯一诚实的答案。
///
/// 为什么逐窗口做而不是给 app 设 AXMinimized：`kAXMinimized` 是
/// 窗口级属性，只挂在 `kAXWindows` 里的元素上。
private func unminimizeWindowsOnce(_ axApp: AXUIElement) -> Bool? {
    // nil = 这一拍读数没开口（不是「没有最小化窗口」这个确定答案）。
    // 只有拿到窗口清单之后的结果才配叫确定值，理由见下面的调用方注释。
    var raw: CFTypeRef?
    guard AXUIElementCopyAttributeValue(
            axApp, kAXWindowsAttribute as CFString, &raw) == .success,
        let windows = raw as? [AXUIElement], !windows.isEmpty else { return nil }
    var acted = false
    for w in windows {
        var mini: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
                w, kAXMinimizedAttribute as CFString, &mini) == .success,
            (mini as? Bool) == true else { continue }
        if AXUIElementSetAttributeValue(
            w, kAXMinimizedAttribute as CFString,
            kCFBooleanFalse as CFBoolean) == .success { acted = true }
    }
    return acted
}

func unminimizeWindows(of app: NSRunningApplication) -> Bool {
    // D105/D107 实测：AX 窗口读数会间歇塌陷 —— 同一份构建，有时连续 18s
    // 每秒读到 1 窗，有时连续 25s 全 0 窗，且它自己会恢复。这是**量具间歇**，
    // 不是「对方没有窗口」。原来只读一次，读不到就 return false：takeover
    // 路径只剩 activate，窗口留在 Dock，用户看到「Dock 图标跳了一下」。
    // 口径与 tools/minimize_check.py 的 read_mini_patient 对齐：读数没开口时
    // 置前一次（kAXFrontmost=true 是被量过唯一能救回读数的手段）再试；
    // 有界重试，预算用完仍返回 false，不猜、不谎报成功。
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    let deadline = Date(timeIntervalSinceNow: 6.0)
    var streak = 0
    var nudges = 0
    while true {
        if let acted = unminimizeWindowsOnce(axApp) { return acted }
        streak += 1
        if streak % 2 == 0 {
            if AXUIElementSetAttributeValue(
                axApp, kAXFrontmostAttribute as CFString,
                kCFBooleanTrue as CFBoolean) == .success { nudges += 1 }
        }
        if Date() >= deadline {
            FileHandle.standardError.write(Data(
                "[agentos] takeover: AX window reading stayed silent for 6s (nudges=\(nudges)), reporting not-unminimized honestly\n".utf8))
            return false
        }
        usleep(300_000)
    }
}

if let existing = takeOverExistingInstance() {
    // 这一支不打 probe.json：门看的每一份 probe.json 都必须属于「真的
    // 跑起来了的那个实例」。这里要落的是另一份语义 —— 我退出了。
    FileHandle.standardError.write(Data("[agentos] 已有实例 \(existing.processIdentifier) 在运行，已激活并退出\n".utf8))
    exit(0)
}

app.run()
