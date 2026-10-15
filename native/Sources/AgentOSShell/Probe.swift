import AppKit

// D99：原生外壳的**结构化证据出口**。
//
// 为什么必须是文件而不是 stdout：门要判「原生 App 真的加载了真驾驶舱、真切了视图、
// 真开了命令面板」，靠 NSApplication 的 stdout 判不了 —— 那些行会经过 AppKit 自己的
// 启动横幅、dyld 噪音混在一起，grep 形状不可靠。而项目里已有的门（D50 收
// `== 名 (py=` / `RC=0`）正是靠形状裁决的。
// 所以原生进程把证据写成一份 JSON，落到 AGENTOS_PROBE_OUT 指的路径，
// 由 tools/native_gate.py 读回并逐条判。这是本仓库一贯的「非进程态物证」纪律。
// 用 final class 而不是 struct：证据要被 AppDelegate 与 MainWindowController
// 两处同时写，struct 的 mutating 语义会把它们逼成 var 互相穿透，读起来更难核对。
final class Probe {
    /// 全进程**唯一**一份证据。D99 首版这里是 `private let probe = Probe()`，
    /// 于是 AppDelegate 与 MainWindowController 各有一份 fields，
    /// 而 flush 只把持有者那一份写盘 —— AppDelegate 收的
    /// bundle_id / window_shown_at_launch / native_menu_has_cmdk /
    /// native_menu_titles / native_menu_view_shortcuts 五项**永远到不了盘上**，
    /// 门读回来全是 None。判据写对了、字段也 put 了，证据却静默丢失：
    /// 这正是「量具没量到东西却照样判红」的镜像形态。
    static let shared = Probe()

    let out: String?
    private var fields: [String: Any] = [:]

    /// 落盘前**当场重采**的补充证据。
    ///
    /// 为什么需要它：D99 首版把 `window_is_visible` / `window_is_key` /
    /// `window_became_key` 三项**只**写在 `windowDidBecomeKey(_:)` 这一个
    /// 回调里。实测踩实：那三项是「瞬时状态」，而该回调**只在 App 真的
    /// 成为前台 app 时才来**。D50 用 `capture_output=True` 跑 gates.sh
    /// （stdout 是管道、非 PTY），进程拿不到前台激活，回调不来 → 三个键
    /// 永远不写盘 → 门读回 None → D99 判红，同时 livecheck 的
    /// 「32/34 门 RC=0」把两门一起拖红。
    ///
    /// 教训与 D99 首版 `Probe.shared` 那次同源：**证据不能只挂在一条
    /// 可能不来的回调上**。改成「落盘那一刻重采一次」，于是任何 flush 路径
    /// （autoclose / 探针序列收尾 / windowWillClose）都必然带上新鲜状态。
    var sampler: (() -> [String: Any])?

    /// 序列是否已经走到收尾。看门狗靠它判断「该不该抢在前面落盘」。
    var sequenceCompleted = false

    /// 收尾那份权威证据是否已经落盘。见 flush 里的说明。
    private var finalFlushed = false

    init() {
        out = ProcessInfo.processInfo.environment["AGENTOS_PROBE_OUT"]
    }

    var enabled: Bool { out != nil }

    /// 探针模式下的进度流水，走 stderr。D105 教训：序列里某一步的 `done()`
    /// 没被调用时，进程既不崩也不退，宿主只能看到「文件永远没出现」——
    /// 没有流水就只能靠 `sample` 猜，而 sample 给出的是**那一刻**的栈，
    /// 98% 空闲在 runloop 的栈看起来和「正常等待」完全一样。
    static func trace(_ msg: String) {
        guard shared.out != nil else { return }
        let line = "[probe \(ISO8601DateFormatter().string(from: Date()))] \(msg)\n"
        FileHandle.standardError.write(Data(line.utf8))
    }

    /// 兜底落盘。宁可交一份标了 `probe_watchdog_fired=1` 的半份证据，
    /// 也不能让门读到「文件不存在」—— 后者无法区分「这步慢」和「这步死了」。
    func armWatchdog(seconds: Double) {
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self, !self.sequenceCompleted else { return }
            self.put("probe_watchdog_fired", 1)
            self.put("probe_sequence_complete", 0)
            Probe.trace("probe: WATCHDOG fired after \(seconds)s, flushing partial evidence")
            self.flush(quit: true)
        }
    }

    func put(_ key: String, _ value: Any) { fields[key] = value }

    /// 回读自己已经 put 过的字段。键盘走查要在走完之后**回头数**「有几个
    /// 视图的 ⌘数字键真的把页面切过去了」，而那批字段是几十秒前陆续写进去的
    /// —— 没有这个回读口就只能靠调用方自己拿字符串拼一遍，既容易拼错，
    /// 也让「数对了几条」这件事没法在写盘点就地完成。
    func text(_ key: String) -> String? { fields[key] as? String }

    func merge(_ other: [String: Any]) {
        for (k, v) in other { fields[k] = v }
    }

    /// 原子落盘：门可能在 App 还活着时就读这个文件，半截 JSON 会让门假红。
    func flush(quit: Bool) {
        guard let path = out else { return }
        // 序列收尾落过盘之后，任何后续 flush 一律不写。
        //
        // D105 实测踩实：`flush(quit: true)` 落盘后紧跟 `NSApp.terminate`，
        // AppKit 随即发 windowWillClose，而那里也调 `flush(quit: false)` ——
        // 于是同一份 probe.json 被写了两遍，第二遍带着**拆除瞬间**重采的
        // isVisible / isMiniaturized / isOnActiveSpace 覆盖掉第一遍。
        // 后果不是数据脏，是**读数自相矛盾**：两次运行分别读到
        // 「visible=True mini=False on_screen=False」与
        // 「visible=False mini=True on_screen=True」，而 7 张截图都是满屏真内容。
        // 判据没错、量具在落盘那一刻被拆解状态污染了。
        //
        // 守门只认「序列收尾那一次」：它是 app 还完整活着时的读数。
        // 序列没走完就关窗（autoclose 计时器 / 用户手关）时 sequenceCompleted
        // 仍是 false，半份证据照样落盘，不丢任何东西。
        if finalFlushed { return }
        if let sampler { merge(sampler()) }
        fields["probe_saved_at"] = ISO8601DateFormatter().string(from: Date())
        if sequenceCompleted { finalFlushed = true }
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard let data = try? JSONSerialization.data(
            withJSONObject: fields,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        ) else { return }
        let tmp = url.appendingPathExtension("tmp-\(ProcessInfo.processInfo.processIdentifier)")
        do {
            try data.write(to: tmp)
            _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
        } catch {
            try? data.write(to: url)   // replaceItemAt 首建/跨卷会失败，退回直写
        }
        if quit { NSApp.terminate(nil) }
    }
}

// MARK: - 环境读取（全部当场实测，不写死字面量）

enum Env {
    static var baseURL: URL {
        if let s = ProcessInfo.processInfo.environment["AGENTOS_URL"], let u = URL(string: s) {
            return u
        }
        return URL(string: "http://127.0.0.1:8790/")!
    }
    static var autoCloseSeconds: Double? {
        guard let s = ProcessInfo.processInfo.environment["AGENTOS_AUTOCLOSE"] else { return nil }
        return Double(s)
    }
    /// 探针序列 drained 之后、进程退场之前继续活着的秒数。
    /// 缺省或 <=0 = drained 即退（调用方没有外部观察者时的正确选择）。
    ///
    /// 为什么需要它：外部 AX 量具（tools/ax_probe.py）要按时间序列采
    /// AX_SAMPLES=12 个样本，每轮迭代 ~8.2s（其中 ~5.7s 是
    /// fg_activate.ensure_foreground 的 6s 超时整轮走满），要约 100s；
    /// 而探针序列本身 ~39s 就 drained。不收尾就退，量具只采到 5 个样本、
    /// 最后一个采在死进程上，ax-sustained 因 samples<12 恒红 —— 红的不是
    /// 产品，是靶子比量具先死。详见 MainWindowController.runNextProbeStep
    /// 收尾处的实测说明。
    static var axGraceSeconds: Double? {
        guard let s = ProcessInfo.processInfo.environment["AGENTOS_AX_GRACE"] else { return nil }
        return Double(s)
    }
    static var syntheticKeys: Bool {
        ProcessInfo.processInfo.environment["AGENTOS_SYNTHETIC_KEYS"] == "1"
    }
    /// 降级面探针：服务探不到时不去跑「切视图/开面板」那套序列
    /// （页面根本没加载，跑它只会量到空 DOM），而是专门量**失败路径本身**：
    /// 原生降级面板出没出现、按钮接没接线、点下去服务起没起来。
    static var degradedProbe: Bool {
        ProcessInfo.processInfo.environment["AGENTOS_DEGRADED_PROBE"] == "1"
    }
    /// 真 performClick 那个「启动服务」按钮，而不是直接调 onStart 闭包 ——
    /// 直接调闭包只能证明闭包能跑，证明不了 NSButton 的 target/action 没被摘掉。
    static var tapStart: Bool {
        ProcessInfo.processInfo.environment["AGENTOS_TAP_START"] == "1"
    }
    /// 单实例逃生阀。门要在用户自己的实例还开着的时候再起一个探针实例，
    /// 没有这条就永远撞上「已有实例在运行，已激活并退出」。
    static var multiInstance: Bool {
        ProcessInfo.processInfo.environment["AGENTOS_MULTI_INSTANCE"] == "1"
    }
}
