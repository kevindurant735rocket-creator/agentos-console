import Foundation

// D100：原生外壳与本地服务对话的**唯一**出口。
//
// 为什么原生侧要自己发 HTTP 而不是问网页要数据：网页是 WKWebView，它天生
// 能 fetch，于是「文件树 / 终端的数据」很容易又长回网页里去 —— 那样面板
// 只是把网页又抄了一遍，原生化停在皮。原生面板自己取数，才说明它是真的
// AppKit 控件驱动的，而不是网页视图的一张截图。
//
// 三条纪律与 serve.py 的硬门对齐，不许在这里放松：
//   1. 只走 Env.baseURL（回环），不接受任何外部 host —— 与 /__web 那条禁令同源。
//   2. POST 一律显式决定要不要带 X-AgentOS-Action:1。缺了它后端会 403，
//      那正是我们要的：终端能算数，但写权限的边界由后端定，不由客户端自称。
//   3. 失败一律把后端的 error 原样带出来，不做兜底成功。
final class LocalAPI {
    static let shared = LocalAPI()
    private let session: URLSession

    init() {
        let c = URLSessionConfiguration.ephemeral
        // 6s 是「回环空闲」时代定的数，撑不住真实负载：`/__projects` 每个
        // 项目都要把项目根 os.walk 一遍（tools/serve.py:1157 project_facts），
        // 实测空闲 0.10-0.24s、3 路并发（__distill+__capmap+__projects）
        // 2.78s；量具同时还在跑 AX 序列 + WebKit 渲染 + 页面抓取，这个回环
        // 请求排在它们后面，一旦越过 6s 就被 URLSession 判超时，ProjectsPanel
        // 就地报「项目未取到」（native_gate 的 projects-loaded 连带 6 项连红）。
        // 30s 是最差实测值的 10 倍以上：够装下并发，又不至于挂死。
        // timeoutIntervalForResource 故意不设 —— `/__term` 跑的是真命令，
        // 命令跑久了被硬切，会造出一个本来不会发生的失败。
        c.timeoutIntervalForRequest = 30
        c.waitsForConnectivity = false
        session = URLSession(configuration: c)
    }

    private func url(_ path: String, _ query: [String: String]) -> URL? {
        let base = Env.baseURL.appendingPathComponent(path)
        guard var c = URLComponents(url: base, resolvingAgainstBaseURL: false) else {
            return nil
        }
        if !query.isEmpty {
            c.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        return c.url
    }

    /// 解析 JSON 对象。失败一律给出一句人话，不返回「空对象冒充成功」。
    private func decode(_ data: Data?, _ resp: URLResponse?, _ err: Error?) -> ([String: Any]?, String?) {
        if let err = err { return (nil, "请求失败：\(err.localizedDescription)") }
        if let h = resp as? HTTPURLResponse, h.statusCode != 200 {
            return (nil, "HTTP \(h.statusCode)")
        }
        guard let data = data else { return (nil, "空响应") }
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (nil, "响应不是 JSON 对象")
        }
        if obj["ok"] as? Bool == false {
            return (nil, String(describing: obj["error"] ?? "ok=false"))
        }
        return (obj, nil)
    }

    func get(_ path: String, _ query: [String: String] = [:],
             done: @escaping ([String: Any]?, String?) -> Void) {
        guard let u = url(path, query) else { done(nil, "URL 构造失败"); return }
        var req = URLRequest(url: u)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        session.dataTask(with: req) { [weak self] data, resp, err in
            guard let self else { return }
            // Swift 不会把元组摊进两个闭包参数，必须显式解构 ——
            // 直接写 done(self.decode(...)) 编译期就报 arity 不匹配。
            let (obj, e) = self.decode(data, resp, err)
            done(obj, e)
        }.resume()
    }

    /// header 必须由调用点显式传 —— 让「要不要带这个头」永远是个可见选择，
    /// 而不是藏在函数里的一句默认行为。
    func post(_ path: String, _ body: [String: Any], actionHeader: Bool,
              done: @escaping ([String: Any]?, String?) -> Void) {
        guard let u = url(path, [:]) else { done(nil, "URL 构造失败"); return }
        var req = URLRequest(url: u)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if actionHeader { req.setValue("1", forHTTPHeaderField: "X-AgentOS-Action") }
        req.httpBody = (try? JSONSerialization.data(withJSONObject: body)) ?? Data()
        session.dataTask(with: req) { [weak self] data, resp, err in
            guard let self else { return }
            let (obj, e) = self.decode(data, resp, err)
            done(obj, e)
        }.resume()
    }

    /// 跑一条只读终端命令。回传后端的**真实** rc 与输出 ——
    /// 面板不替命令判定成功，这点与 serve.py 的口径一致。
    func term(_ argv: [String], done: @escaping ([String: Any]?, String?) -> Void) {
        post("__term", ["argv": argv], actionHeader: true, done: done)
    }
}
