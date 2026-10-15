#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三尺寸真视口截图 + 文档级横向溢出实测。

为什么不用 `chrome --headless --window-size=375,900`：
  Chrome 154 的 headless 对 <500px 的窗口强制 innerWidth=500（实测 --headless /
  --headless=new / --headless=old 三种模式全部 innerW=500，w768 才正常），
  于是「375 宽截图」其实是 500px 布局被裁成 375px，右侧内容静默丢失 = 未验证冒充。
  本脚本走 Playwright(CDP) 的 Emulation.setDeviceMetricsOverride，innerWidth 与视口严格一致。
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
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, "index.html")
SHOTS = os.path.join(ROOT, "docs", "UX-SHOTS")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SIZES = (375, 768, 1440)
# D97：本门在**真 HTTP** 上截图。file:// 下 fetch('/\_\_projects') 被跨源拒掉，
# 左栏渲染成一句报错、**一个项目按钮都不出** —— 也就是说旧截图量的根本不是
# 用户双击启动器后真正看到的那一页（同一份文件的实测口径见 ux_audit.py 的
# measure_interactive() docstring）。SERVE_PY 是被 import 的活服务模块。
SERVE_PY = os.path.join(HERE, "serve.py")
# 第二棵树换成门自己造的 fixture：截图门不该依赖用户桌面那棵真树当时可不可读。
# ROOT 一律不动 —— 换掉 ROOT 就等于「两个假的根互相对换」，永远测不到东西。
TREE_FIXTURE = os.path.join(ROOT, "work", "_shoottree")

# 文档级横向溢出必须为 0；内部横向滚动容器（overflow-x:auto/scroll/hidden）内的溢出不算缺陷
OVERFLOW_JS = """() => {
  const de = document.documentElement;
  const offenders = [];
  for (const e of document.querySelectorAll('#app *')) {
    const r = e.getBoundingClientRect();
    if (r.width <= 1) continue;
    if (r.right > de.clientWidth + 1 || r.left < -1) {
      let scroller = false, p = e.parentElement;
      while (p) { const s = getComputedStyle(p);
        if (['auto','scroll','hidden'].indexOf(s.overflowX) >= 0 ||
            ['auto','scroll','hidden'].indexOf(s.overflow) >= 0) { scroller = true; break; }
        p = p.parentElement; }
      if (scroller) continue;
      offenders.push({tag: e.tagName,
        cls: (typeof e.className === 'string' ? e.className : '').slice(0, 40),
        left: Math.round(r.left), right: Math.round(r.right)});
    }
  }
  return {innerW: innerWidth, docScrollW: de.scrollWidth, docClientW: de.clientWidth,
          offenders: offenders.slice(0, 12)};
}"""


# D50（2026-10-03）：真浏览器基础设施故障重试，与 act_gate 共用 tools/flake_retry.py。
# flake_inject() 必须在**门体最前面**：CD_FLAKE_INJECT=always 时它让本门亚秒级假红，
# 用来证明 run_gate 包裹真的接上了（删掉包裹 → 裸 traceback + rc=1 → 自测判红）。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from flake_retry import flake_inject, run_gate  # noqa: E402,F401


def _body(_ctx):
    flake_inject()
    from playwright.sync_api import sync_playwright
    os.makedirs(SHOTS, exist_ok=True)
    # D50：记下**本次真加载的**页面指纹。原来的对账口径是 shots.json 的 mtime vs
    # index.html 的 mtime，而并发 build.py 会重写 index.html（内容可能一字未改），
    # 于是纯假红。改记内容指纹后，「截图是不是旧构建」有了唯一可信的答案。
    with open(HTML, "rb") as _fh:
        _html_sha = hashlib.sha256(_fh.read()).hexdigest()[:16]
    report = {"tool": "shoot.py", "method": "playwright-cdp-real-viewport",
              "html_sha256": _html_sha,
              "sizes": {}, "pageerrors": []}
    bad = 0
    # D97：起真服务再截图。fail-closed —— 服务器起不来 / playwright 装不上 / 页面
    # JS 报错，一律让本门判红；量具坏掉时静默继续就是本项目一路在治的假绿。
    os.makedirs(TREE_FIXTURE, exist_ok=True)
    spec = importlib.util.spec_from_file_location("_shoot_serve", SERVE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TREE = TREE_FIXTURE
    from http.server import ThreadingHTTPServer
    quiet = type("QuietHandler", (mod.Handler,),
                 {"log_message": lambda self, *a, **k: None})

    class QuietSrv(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass

    _sk = socket.socket()
    _sk.bind(("127.0.0.1", 0))
    port = _sk.getsockname()[1]
    _sk.close()
    srv = QuietSrv(("127.0.0.1", port), quiet)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever,
                     kwargs={"poll_interval": 0.2}, daemon=True).start()
    base_url = "http://127.0.0.1:%d/index.html" % port
    report["url"] = base_url
    report["served"] = True
    report["transport"] = "real-http"
    try:
      with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        for w in SIZES:
            pg = b.new_page(viewport={"width": w, "height": 900}, device_scale_factor=1)
            errs = []
            pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)[:160]))
            pg.goto(base_url, wait_until="load")
            # D97：等左栏项目空间真把按钮渲染出来再量。不等就是量 fetch 完成前的
            # 中间态 —— 那正是「量具没量到东西却照样判绿」的形状（D96 同款教训）。
            try:
                pg.wait_for_function(
                    "() => document.querySelectorAll('#projspace .pjnode').length >= 2",
                    timeout=15000)
            except Exception as e:                # noqa: BLE001
                errs.append("项目空间未渲染: " + str(e)[:80])
            pg.wait_for_timeout(900)
            d = pg.evaluate(OVERFLOW_JS)
            real_w = pg.evaluate("innerWidth")
            path = os.path.join(SHOTS, "w%d.png" % w)
            pg.screenshot(path=path)
            over = d["docScrollW"] > d["docClientW"] or bool(d["offenders"])
            if over or real_w != w or errs:
                bad += 1
            report["sizes"][str(w)] = {
                "innerW": real_w, "docScrollW": d["docScrollW"],
                "docClientW": d["docClientW"], "overflow": over,
                "offenders": d["offenders"], "pageerrors": errs,
                "png_bytes": os.path.getsize(path),
            }
            pg.close()
        b.close()
    finally:
        srv.shutdown()
        srv.server_close()
    with io.open(os.path.join(ROOT, "work", "shots.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    for w, d in sorted(report["sizes"].items(), key=lambda kv: int(kv[0])):
        extra = ""
        if d["offenders"]:
            extra = " offender=" + json.dumps(d["offenders"][:2], ensure_ascii=False)
        print("  w%-5s innerW=%-5s docScrollW=%-5s clientW=%-5s overflow=%-5s png=%dKB%s"
              % (w, d["innerW"], d["docScrollW"], d["docClientW"], d["overflow"],
                 d["png_bytes"] // 1024, extra))
        if d["pageerrors"]:
            print("       PAGEERR " + "; ".join(d["pageerrors"][:2]))
    print("== SHOOT %s  %d/%d 尺寸 innerWidth 精确 + 无文档横向溢出 + 无 JS 错误 =="
          % ("OK" if bad == 0 else "FAIL", len(SIZES) - bad, len(SIZES)))
    return bad == 0, ("%d/%d 尺寸合格" % (len(SIZES) - bad, len(SIZES)))


def main():
    """唯一入口。判据红 = 结论，一次都不重试；基础设施故障才重试（D40 铁律）。"""
    ok, detail = run_gate(_body, None)
    if detail.startswith("EXC "):
        print("  [EXC] " + detail)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
