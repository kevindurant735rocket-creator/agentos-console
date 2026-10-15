import AppKit
import WebKit

// D99：原生窗口的**像素证据**。
//
// 为什么必须截图而不是只报 window.isVisible：D68 那道 a11y_human 已经吃过一次
// 「窗口在、内容是空」的亏，而本项目的历史里也有一整类门（view_dod 判「屏里有真
// 内容」）是靠 DOM 判据，DOM 有内容不等于**用户眼睛看到的画面**有内容。
// 原生 WebView 一旦踩到 ATS / 证书 / 渲染进程崩溃，DOM 与 isVisible 可以同时为真，
// 而截图会立刻变成一张空白或错误页 —— 这正是门该抓的差异。
enum Screenshot {
    static var outputPath: String? {
        ProcessInfo.processInfo.environment["AGENTOS_SHOT_OUT"]
    }

    /// WKWebView.takeSnapshot 拿的是 WebKit 自己的合成结果（含 GPU 层），
    /// cacheDisplay 只能拿到 AppKit 视图树 —— 前者才等于用户看到的画面。
    /// path 必须由调用方逐状态传入 —— D99 首跑就是这里只写 outputPath，
    /// 于是 5 张分状态截图全部覆盖成同一张，而探针照样报 "ok N bytes"：
    /// 「说自己截了」与「真的留下 5 张不同的图」之间没有任何校验。
    static func capture(_ webView: WKWebView, to path: String,
                        completion: @escaping (String) -> Void) {
        webView.takeSnapshot(with: nil) { image, err in
            if let err = err {
                completion("error: \(err.localizedDescription)")
                return
            }
            guard let image = image,
                  let tiff = image.tiffRepresentation,
                  let rep = NSBitmapImageRep(data: tiff),
                  let png = rep.representation(using: .png, properties: [:])
            else {
                completion("error: cannot-encode")
                return
            }
            let url = URL(fileURLWithPath: path)
            try? FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            do {
                try png.write(to: url)
                completion("ok \(png.count) bytes \(rep.pixelsWide)x\(rep.pixelsHigh)")
            } catch {
                completion("error: \(error.localizedDescription)")
            }
        }
    }

    /// 空白/纯色页自检：把图缩成一张小位图，数**不同颜色数**与**非背景像素占比**。
    /// 判据不设「必须含某个品牌色」—— 那会把门绑死在美术改版上；
    /// 只判「这张图有结构」，这才是「用户眼睛看到东西」的最小充分条件。
    static func inkStats(_ path: String) -> (distinct: Int, inkRatio: Double)? {
        guard let img = NSImage(contentsOfFile: path),
              let tiff = img.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff) else { return nil }
        let w = rep.pixelsWide, h = rep.pixelsHigh
        guard w > 8, h > 8 else { return nil }
        var seen = Set<UInt32>()
        var ink = 0
        var total = 0
        let stepX = max(1, w / 160), stepH = max(1, h / 160)
        var y = 0
        while y < h {
            var x = 0
            while x < w {
                if let c = rep.colorAt(x: x, y: y) {
                    let r = Int(c.redComponent * 255), g = Int(c.greenComponent * 255)
                    let b = Int(c.blueComponent * 255)
                    seen.insert(UInt32(r << 16 | g << 8 | b))
                    total += 1
                    // 「墨」= 与最亮像素差得开的像素。纯色页 / 纯白错误页 distinct 会≈1
                    if r + g + b < 690 { ink += 1 }
                }
                x += stepX
            }
            y += stepH
        }
        guard total > 0 else { return nil }
        return (seen.count, Double(ink) / Double(total))
    }

    /// 任意 NSView 的像素证据（原生面板用它）。
    ///
    /// 为什么不复用上面的 takeSnapshot：那条路只吃 WKWebView，而原生面板里
    /// 根本没有 WebView —— D100 要量的恰恰是「AppKit 控件真的画出来了」。
    /// 用 cacheDisplay 抓 AppKit 自己的视图树，并显式把 rep.size 设成 bounds
    /// （points），于是 pixelsWide 就是逻辑像素宽，2x 屏上门拿到的尺寸判据
    /// 仍然等于控件的实际 point 尺寸，不会被 Retina 放大搞成假红。
    static func captureView(_ view: NSView, to path: String,
                            completion: @escaping (String) -> Void) {
        let bounds = view.bounds
        guard bounds.width >= 8, bounds.height >= 8 else {
            completion("error: bounds-too-small")
            return
        }
        guard let rep = view.bitmapImageRepForCachingDisplay(in: bounds) else {
            completion("error: no-rep")
            return
        }
        rep.size = bounds.size
        view.cacheDisplay(in: bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            completion("error: cannot-encode")
            return
        }
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        do {
            try png.write(to: url)
            completion("ok \(png.count) bytes \(rep.pixelsWide)x\(rep.pixelsHigh)")
        } catch {
            completion("error: \(error.localizedDescription)")
        }
    }

    // MARK: - 原生面板像素证据（D100b：四条通道）

    /// 一条通道的结果。**每条通道的数字都要落进探针**，见 captureViewAll
    /// 的说明：只留「最好看的那张」等于自己给自己打分。
    struct ViewShot {
        let method: String
        let result: String
        let distinct: Int
        let inkRatio: Double

        var summary: String {
            "\(method):\(result.prefix(24)) distinct=\(distinct) ink=\(String(format: "%.3f", inkRatio))"
        }
    }

    /// 铺好不透明白底的 2x 位图 + 它的 CGContext。
    ///
    /// 为什么强制白底：透明像素被读成黑色，于是「空白页」和「全黑页」
    /// 在统计上会长得一模一样 —— D100 首跑正是这样把一张全黑图判成了通过。
    private static func whiteRep(for view: NSView, scale: CGFloat = 2)
        -> (NSBitmapImageRep, CGContext)? {
        let bounds = view.bounds
        let w = Int(bounds.width * scale), h = Int(bounds.height * scale)
        guard w > 0, h > 0,
              let rep = NSBitmapImageRep(
                  bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h,
                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                  isPlanar: false, colorSpaceName: .deviceRGB,
                  bytesPerRow: 0, bitsPerPixel: 0),
              let nsctx = NSGraphicsContext(bitmapImageRep: rep) else { return nil }
        let cg = nsctx.cgContext
        rep.size = bounds.size
        cg.setFillColor(NSColor.white.cgColor)
        cg.fill(CGRect(x: 0, y: 0, width: CGFloat(w), height: CGFloat(h)))
        cg.scaleBy(x: scale, y: scale)
        return (rep, cg)
    }

    /// 任意 NSView 的像素证据，**跑四条通道**。
    ///
    /// D100 首跑实测踩实的坑：只写 cacheDisplay 一条，产出一张 99% 纯黑的
    /// PNG（distinct=17），而 black 恰好是「满墨」—— ink_ratio≈0.99，
    /// 于是「ink >= 0.5 就算有内容」这条判据把一张全黑图判成了通过。
    /// 判据和被测物串通，一起骗了门。病根是 cacheDisplay 走 AppKit 的
    /// draw(_:) 老路，而 NSOutlineView / NSTextView / NSScrollView 全是
    /// layer-backed，窗口没真正上屏时它们一个像素都不画。
    ///
    /// 四条通道各修一个不同的缺口：layer 合成拿 CALayer.contents、
    /// cachewhite 拿 draw(_:) 且有白底可辨、PDF 回放强制走完整 AppKit
    /// 绘制（唯一不依赖窗口上屏的）、cachedefault 留作对照以便看出
    /// 「换了哪条通道才有内容」。全黑的特征是 inkRatio > 0.9 且 distinct
    /// 很小，门据此判「空白」而不是「有内容」。
    static func captureViewAll(_ view: NSView, to dir: String,
                               tag: String) -> [ViewShot] {
        let bounds = view.bounds
        guard bounds.width >= 8, bounds.height >= 8 else {
            return [ViewShot(method: "guard", result: "error: bounds-too-small",
                             distinct: -1, inkRatio: -1)]
        }
        // layer-backed 子控件的内容要先落进 layer.contents，否则
        // layer.render(in:) 拿到的是空 layer。
        view.displayIfNeeded()
        view.layoutSubtreeIfNeeded()

        var shots: [ViewShot] = []

        // 通道 1：CALayer 合成
        if let (rep, cg) = whiteRep(for: view) {
            view.layer?.render(in: cg)
            shots.append(write(rep, method: "layer", dir: dir, tag: tag))
        } else {
            shots.append(ViewShot(method: "layer", result: "error: no-bitmap",
                                  distinct: -1, inkRatio: -1))
        }

        // 通道 2：cacheDisplay 铺白底
        if let (rep, _) = whiteRep(for: view) {
            view.cacheDisplay(in: bounds, to: rep)
            shots.append(write(rep, method: "cachewhite", dir: dir, tag: tag))
        } else {
            shots.append(ViewShot(method: "cachewhite", result: "error: no-bitmap",
                                  distinct: -1, inkRatio: -1))
        }

        // 通道 3：dataWithPDF 回放（不依赖窗口上屏）
        if let (rep, cg) = whiteRep(for: view) {
            let pdf = view.dataWithPDF(inside: bounds)
            if let provider = CGDataProvider(data: pdf as CFData),
               let doc = CGPDFDocument(provider), let page = doc.page(at: 1) {
                cg.saveGState()
                cg.drawPDFPage(page)
                cg.restoreGState()
                shots.append(write(rep, method: "pdf", dir: dir, tag: tag))
            } else {
                shots.append(ViewShot(method: "pdf", result: "error: no-pdf-page",
                                      distinct: -1, inkRatio: -1))
            }
        } else {
            shots.append(ViewShot(method: "pdf", result: "error: no-bitmap",
                                  distinct: -1, inkRatio: -1))
        }

        // 通道 4：D100 首跑那条，留作对照
        if let rep = view.bitmapImageRepForCachingDisplay(in: bounds) {
            rep.size = bounds.size
            view.cacheDisplay(in: bounds, to: rep)
            shots.append(write(rep, method: "cachedefault", dir: dir, tag: tag))
        } else {
            shots.append(ViewShot(method: "cachedefault", result: "error: no-rep",
                                  distinct: -1, inkRatio: -1))
        }

        return shots
    }

    private static func write(_ rep: NSBitmapImageRep, method: String,
                              dir: String, tag: String) -> ViewShot {
        guard let png = rep.representation(using: .png, properties: [:]) else {
            return ViewShot(method: method, result: "error: cannot-encode",
                            distinct: -1, inkRatio: -1)
        }
        let path = "\(dir)/\(tag)-\(method).png"
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        do {
            try png.write(to: url)
        } catch {
            return ViewShot(method: method, result: "error: \(error.localizedDescription)",
                            distinct: -1, inkRatio: -1)
        }
        let st = inkStats(path) ?? (-1, -1.0)
        return ViewShot(method: method,
                        result: "ok \(png.count) bytes \(rep.pixelsWide)x\(rep.pixelsHigh)",
                        distinct: st.0, inkRatio: st.1)
    }
}
