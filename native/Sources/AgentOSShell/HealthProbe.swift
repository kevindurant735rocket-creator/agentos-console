import Foundation

// D99：原生外壳自己判服务死活，不依赖「用户先在浏览器里开过一次」。
// 取不到 /__health 一律按**服务未起**处理（fail-closed）：
// 原生 App 宁可显示「服务未运行 + 启动按钮」，也不许给用户一块空白的驾驶舱。
struct HealthPayload: Decodable {
    let ok: Bool
    let root: String?
    let index: String?
    let clients: Int?
}

final class HealthProbe {
    private let url: URL
    private(set) var lastOK = false
    private(set) var lastDetail = "未探测"
    private(set) var lastFingerprint: String?
    var onChange: ((Bool, String) -> Void)?
    private var inFlight = false     // 单飞：连点「启动服务」不叠探针线程

    init(base: URL) {
        url = base.appendingPathComponent("__health")
    }

    func poll(_ done: ((Bool, String, [String: Any]) -> Void)? = nil) {
        guard !inFlight else { return }
        inFlight = true
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { data, resp, err in
            var fields: [String: Any] = [:]
            var ok = false
            var detail = ""
            if let err = err {
                detail = "连接失败：\(err.localizedDescription)"
            } else if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                detail = "服务返回 HTTP \(http.statusCode)"
            } else if let data = data,
                      let p = try? JSONDecoder().decode(HealthPayload.self, from: data) {
                ok = p.ok
                detail = "已连接 · \(p.clients ?? 0) 客户端 · 指纹 \(p.index ?? "?")"
                fields["health_root"] = p.root ?? ""
                fields["health_index_fingerprint"] = p.index ?? ""
                fields["health_clients"] = p.clients ?? -1
            } else {
                detail = "响应无法解析"
            }
            DispatchQueue.main.async {
                self.inFlight = false
                let changed = (ok != self.lastOK) || (detail != self.lastDetail)
                self.lastOK = ok
                self.lastDetail = detail
                if ok, let data = data,
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let idx = obj["index"] as? String {
                    self.lastFingerprint = idx
                }
                if changed { self.onChange?(ok, detail) }
                done?(ok, detail, fields)
            }
        }.resume()
    }
}
