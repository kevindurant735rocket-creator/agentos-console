#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真 HTTP 截图门：D97 的自证门 —— 证明「截图确实量的是用户真正看到的那一页」。

这个门为什么必须存在（本文件存在的理由，不是重写理由）：
    D97 之前的 shoot.py 与 view_dod.py 走 file:// 打开 index.html。file:// 下
    fetch('/__projects')、fetch('/__health') 全被浏览器按跨源拒掉，于是
      · 左栏「项目空间」渲染成一句「项目空间读不到：Failed to fetch」，项目按钮 0 个
      · 右栏「执行面」整栏「不可判定」，#cxexecn 写「读 /__health 失败」
    也就是说 docs/UX-SHOTS 下那 3 张总览截图和 view_dod 的 30 屏走查，量的全是
    一个**真实用户永远看不到的残废页**。实测证据（2026-10-05，w1440）：
    改真 HTTP 后同一视口 PNG 从 11KB 涨到 259KB，左栏真项目（724 文件/顶层 71）
    与真 code_sha=6244c88e1d33 出现。
    D96 已经在 ux_audit 上治过同一类病（见该文件 measure_interactive docstring），
    但 ux_audit 的口径只管交互预算，管不到「落盘的截图是不是真的」。

    关键点：**改了 shoot/view_dod 并不等于这件事被证明了**。如果没人量，
    下一个人完全可以再把 pg.goto 改回 file://，截图重新变成残废页而门照样全绿。
    所以本门反过来量「真 HTTP」这件事本身，并且自带 4 个变异体证明自己有牙：
      BASE  真 HTTP        -> 健康信号齐（项目按钮≥2、无 .pjerr、执行面真值、code_sha 在）
      M1    拦掉 /__health -> 执行面必须退成「不可判定」        -> 判红
      M2    拦掉 /__projects-> 项目空间必须退成 .pjerr 且按钮 0 个 -> 判红
      M3    退回 file://   -> 健康信号必须全部塌掉              -> 判红
      M4    端点回 500     -> 执行面必须退成「不可判定」        -> 判红
    M3 是本门最关键的一条：它让「截图走 file://」这个具体回归**当场判红**，
    而不是靠下一个人的记性。

产出 work/http_shot_gate.json，rc=0 全过 / 2 有红项。
"""
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SERVE_PY = os.path.join(HERE, "serve.py")
HTML = os.path.join(ROOT, "index.html")
OUT = os.path.join(ROOT, "work", "http_shot_gate.json")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# 第二棵树换成门自己造的 fixture：本门只管「截图是否量到真 HTTP 页面」，
# 不该依赖用户桌面那棵真树当时可不可读。ROOT 一律不动（同 ux_audit 的理由）。
TREE_FIXTURE = os.path.join(ROOT, "work", "_httpshot_tree")

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))


# 健康信号探针。判据只用**页面上真实存在的标记**，不猜：
#   · #projspace .pjnode  真实项目按钮（D96 起的左栏）
#   · #projspace .pjerr   左栏读不到时的报错节点
#   · #cxexecn            成功='读 /__health · <time>'；失败='读 /__health 失败'
#     （两处字面量见 index.template.html 的 cxHealth()，line 2855 / 2876）
#   · #cxinst             成功时含 code_sha 前 12 位；失败时整栏「不可判定」
PROBE_JS = """() => {
  const t = id => { const e = document.getElementById(id);
    return e ? (e.textContent || '').trim() : ''; };
  const inst = t('cxinst'), act = t('cxact');
  // code_sha 的真实值是 13 位十六进制。锚在标签 "code_sha" 后面取，不能写
  // /[0-9a-f]{12}/ 裸匹配 —— 那会从标签自己的尾字母 a 上匹出
  // "a6244c88e1d3" 这种把标签字符算进去的假 12 位，判据自己带 bug 比没有
  // 判据更危险。也不能加 (?<![0-9a-zA-Z]) 前瞻边界：#cxinst 的 innerText 里
  // 标签与值是**紧挨着**的（"code_sha6244c88e1d33"，无分隔符），加边界必然
  // 匹不到、把健康页判红 —— 这也是 D97 实测踩到并修掉的（两版都留了记录）。
  const shaM = inst.match(/code_sha([0-9a-f]{12,})/);
  return {
    pjnodes: document.querySelectorAll('#projspace .pjnode').length,
    pjerr: (document.querySelector('#projspace .pjerr') || {}).textContent || '',
    cxexecn: t('cxexecn'), cxact: act, cxinst: inst,
    codeSha: shaM ? shaM[1].slice(0, 12) : '',
    // 「不可判定」只数 #cxinst（常驻实例栏），**不数 #cxact**。
    // D97 实测踩到的坑：#cxact 的 当前动作/上次动作/上次 rc 在常驻实例没有
    // 正在执行的动作时**本来就该**是「不可判定」（通用 null 渲染器，line 1726），
    // 拿它当健康信号会让一个完全健康的页面判红 —— 这是量具自己的 bug，
    // 不是页面的 bug。而 #cxinst 是硬信号：读不到 /__health 时前端把整栏
    // 逐行填成「不可判定」（index.template.html line 2859-2860），健康时全是真值。
    instUndecidable: inst.split('不可判定').length - 1,
    url: location.href, scheme: location.protocol,
  };
}"""


def healthy(d):
    """一页是否同时满足：真 HTTP / 左栏真项目 / 右栏真执行面 / code_sha 在。"""
    return (d.get("scheme") == "http:"
            and d.get("pjnodes", 0) >= 2
            and not d.get("pjerr")
            and d.get("cxexecn", "").startswith("读 /__health ·")
            and bool(d.get("codeSha"))
            and d.get("instUndecidable", 99) == 0)


def why_unhealthy(d):
    bad = []
    if d.get("scheme") != "http:":
        bad.append("scheme=%s（非 http）" % d.get("scheme"))
    if d.get("pjnodes", 0) < 2:
        bad.append("项目按钮=%d（<2）" % d.get("pjnodes", 0))
    if d.get("pjerr"):
        bad.append("左栏报错=%s" % d["pjerr"][:40])
    if not d.get("cxexecn", "").startswith("读 /__health ·"):
        bad.append("#cxexecn=%r" % d.get("cxexecn", "")[:30])
    if not d.get("codeSha"):
        bad.append("#cxinst 无 code_sha")
    if d.get("instUndecidable"):
        bad.append("#cxinst 「不可判定」×%d" % d["instUndecidable"])
    return "; ".join(bad) if bad else "健康"


def start_server():
    """import 活 serve.py，起一个随机端口的真 HTTP 实例。返回 (srv, port, mod)。"""
    os.makedirs(TREE_FIXTURE, exist_ok=True)
    spec = importlib.util.spec_from_file_location("_httpshot_serve", SERVE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TREE = TREE_FIXTURE
    from http.server import ThreadingHTTPServer
    quiet = type("QuietHandler", (mod.Handler,),
                 {"log_message": lambda self, *a, **k: None})

    class QuietSrv(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = QuietSrv(("127.0.0.1", port), quiet)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    return srv, port, mod


def launch(pw):
    """优先系统 Chrome（版本与用户日常一致），没有就退回 playwright 自带的。"""
    try:
        return pw.chromium.launch(executable_path=CHROME)
    except Exception:                                        # noqa: BLE001
        return pw.chromium.launch()


def probe(pw, url, routes=None):
    """打开一页，量健康信号。routes = [(glob, handler)] 在导航前装上。

    fail-closed 返回 None：量具自己坏了时调用方必须判红，绝不许静默当「健康」。
    """
    br = launch(pw)
    try:
        pg = br.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:120]))
        for glob, handler in (routes or []):
            pg.route(glob, handler)
        pg.goto(url, wait_until="load")
        # 健康时项目按钮会异步长出来；等 4 秒够它长，不够就让判据红（fail-closed）。
        try:
            pg.wait_for_function(
                "() => document.querySelectorAll('#projspace .pjnode').length >= 2"
                " || !!document.querySelector('#projspace .pjerr')",
                timeout=4000)
        except Exception:                                     # noqa: BLE001
            pass
        pg.wait_for_timeout(700)
        d = pg.evaluate(PROBE_JS)
        d["pageerrors"] = errs
        return d
    finally:
        br.close()


def page_sha():
    try:
        with io.open(HTML, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def main():
    from playwright.sync_api import sync_playwright
    sha0 = page_sha()
    srv, port, mod = start_server()
    base = "http://127.0.0.1:%d/index.html" % port
    probes = {}
    try:
        with sync_playwright() as pw:
            # BASE：真 HTTP，应当健康
            probes["base"] = probe(pw, base)
            # M1：拦掉 /__health -> 右栏必须退成「不可判定」
            probes["m1_health_blocked"] = probe(
                pw, base, [("**/__health*", lambda r: r.abort())])
            # M2：拦掉 /__projects -> 左栏必须退成 .pjerr 且按钮 0 个
            probes["m2_projects_blocked"] = probe(
                pw, base, [("**/__projects*", lambda r: r.abort())])
            # M3：退回 file:// -> 健康信号必须全塌（这条就是「截图不许走 file://」）
            probes["m3_file_scheme"] = probe(pw, "file://" + HTML)
            # M4：端点回 500 -> 右栏必须退成「不可判定」
            def boom(route):
                route.fulfill(status=500, content_type="application/json",
                              body='{"ok":false,"error":"injected"}')
            probes["m4_health_500"] = probe(pw, base, [("**/__health*", boom)])
    finally:
        srv.shutdown()
        srv.server_close()

    b = probes["base"]
    gate("BASE 真 HTTP 页面是健康的（左栏真项目 + 右栏真执行面）", healthy(b), why_unhealthy(b))

    for key, label in (("m1_health_blocked", "拦掉 /__health"),
                       ("m2_projects_blocked", "拦掉 /__projects"),
                       ("m4_health_500", "/__health 回 500")):
        d = probes[key]
        gate("变异体有牙：%s 时本门必须判红（此刻应不健康）" % label,
             not healthy(d), why_unhealthy(d))

    m2 = probes["m2_projects_blocked"]
    gate("拦掉 /__projects 确实退化成 .pjerr（不只是按钮变少）",
         bool(m2.get("pjerr")) and m2.get("pjnodes", 99) == 0,
         "pjerr=%s pjnodes=%s" % (m2.get("pjerr", "")[:40], m2.get("pjnodes")))

    m3 = probes["m3_file_scheme"]
    gate("退回 file:// 健康信号全塌（证明 file:// 拍的截图不算数）",
         (not healthy(m3)) and m3.get("scheme") == "file:"
         and m3.get("pjnodes", 99) == 0,
         "scheme=%s pjnodes=%s %s" % (m3.get("scheme"), m3.get("pjnodes"),
                                      why_unhealthy(m3)))

    pe = [k for k, d in probes.items() if d.get("pageerrors")]
    gate("任一探针页 JS 报错", not pe,
         "5 次探测 0 pageerror" if not pe else str(pe))

    sha1 = page_sha()
    gate("活 index.html 零写入（本门只读）", sha0 == sha1 and bool(sha0),
         "sha %s -> %s" % (sha0, sha1))

    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump({"tool": "http_shot_gate.py", "base_url": base,
                   "probes": probes, "gates": results},
                  f, ensure_ascii=False, indent=1)

    nfail = sum(1 for r in results if not r["ok"])
    print("== HTTP_SHOT_GATE %s  %d/%d 门通过（真 HTTP + 4 变异体）=="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail, len(results)))
    return 0 if nfail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
