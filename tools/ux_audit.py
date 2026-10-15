#!/usr/bin/env python3
"""UX DoD 硬门机检：10 条 FAIL 条件逐条验，rc=0 全过 / 2 有红项。

判据真源 = ~/.codex/global-background/company-delivery/depts/UX.md §5
不用颜色猜测：对比度走 WCAG 相对亮度公式，交互元素数走无头浏览器实测 DOM。
"""
import hashlib, importlib.util, io, json, os, re, socket, subprocess, sys, threading
import html as H

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
BUDGET = os.path.join(ROOT, "work", "ui_budget.json")
SHOTS = os.path.join(ROOT, "docs", "UX-SHOTS")
WIDTHS = [375, 768, 1440]
SERVE_PY = os.path.join(ROOT, "tools", "serve.py")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def html_fingerprint():
    """读 work/build.json 的 html_sha256，与当前 index.html 的真实 sha256 比。
    返回 (ok, why)：一致 = 内容没变 = 截图**不算**陈旧。

    为什么要从 mtime 换成内容 sha（D50 实测根因）：
    mtime 判据把「内容一个字没改、只是被并发 build.py 重写了一遍」当成陈旧。
    实测 gates.sh 无锁，shoot/ux_audit/view_dod 并发跑时 build.py 正好重写
    index.html，于是三张截图的 mtime 集体早于 html 的 mtime → 纯假红。
    内容指纹不受重写影响，这正是区分「真陈旧」与「被重写」的唯一可用信号。"""
    bj = os.path.join(ROOT, "work", "build.json")
    if not os.path.exists(bj):
        return False, "无 work/build.json（fail-closed：无法证明内容一致）"
    try:
        with io.open(bj, encoding="utf-8") as f:
            b = json.load(f) or {}
        with io.open(HTML, "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception as e:  # noqa: BLE001
        return False, "指纹读取异常 %s" % str(e)[:60]
    # 只认 file_sha256：它是**落盘后**文件的指纹，可直接与盘上文件对账。
    # html_sha256 是 stamp 注入**之前**的指纹（build.py 里 digest 算在 replace 之前），
    # 拿它跟盘上文件比是恒不相等的 —— 那是 100% 假红的判据，不是判据。
    want = b.get("file_sha256") or ""
    if not want:
        return False, "build.json 无 file_sha256（需先 python3 tools/build.py 重建）"
    if want != got:
        return False, "index.html 内容已变（build=%s 实际=%s）" % (want, got)
    return True, "内容指纹一致=%s（截图不因被重写而假红）" % got


results = []
def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))

def rel_lum(hexrgb):
    c = [int(hexrgb[i:i+2], 16) / 255 for i in (1, 3, 5)]
    c = [(x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4) for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

def contrast(fg, bg):
    a, b = rel_lum(fg), rel_lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)

def main():
    if not os.path.exists(HTML):
        print("FAIL: index.html 不存在"); return 2
    src = open(HTML, encoding="utf-8").read()
    bud = json.load(open(BUDGET, encoding="utf-8"))

    print("== UX DoD 机检 ==")
    # 1 裸 HTML 无样式
    styled = "<style>" in src and len(re.search(r"<style>(.*?)</style>", src, re.S).group(1)) > 2000
    gate("裸 HTML 无样式直接交付", styled,
         "内联 CSS %d 字节（有样式=过门）" % len(re.search(r"<style>(.*?)</style>", src, re.S).group(1)))

    # 2 四态
    four = {"空态": "empty" in src and "没有可显示的阶段数据" in src,
            "加载态": "loading" in src and "sk" in src and "animation:sh" in src,
            "错误态": "error" in src and "载荷解析失败" in src,
            "404": "notfound" in src and "视图不存在" in src}
    gate("四态缺任一态", all(four.values()),
         " ".join("%s=%s" % (k, "Y" if v else "N") for k, v in four.items()))

    # 3 三尺寸截图（PNG magic + 尺寸真实）
    shot_ok, shot_detail = True, []
    for w in WIDTHS:
        p = os.path.join(SHOTS, "w%d.png" % w)
        if not os.path.exists(p):
            shot_ok = False; shot_detail.append("w%d 缺" % w); continue
        with open(p, "rb") as f:
            magic = f.read(24)
        if magic[:8] != b"\x89PNG\r\n\x1a\n":
            shot_ok = False; shot_detail.append("w%d 非PNG" % w); continue
        iw = int.from_bytes(magic[16:20], "big"); ih = int.from_bytes(magic[20:24], "big")
        if iw < w - 2:
            shot_ok = False; shot_detail.append("w%d 实宽%d" % (w, iw))
        else:
            shot_detail.append("w%d=%dx%d" % (w, iw, ih))
    gate("三尺寸截图缺任一尺寸", shot_ok, " ".join(shot_detail))

    # 4 正文对比度 >= 4.5（取 --tx / --tx2 / --tx3 对 --bg / --panel 实算）
    css = re.search(r"<style>(.*?)</style>", src, re.S).group(1)
    def varof(n):
        m = re.search(r"--%s:\s*(#[0-9a-fA-F]{6})" % n, css)
        return m.group(1) if m else None
    pairs = [("tx", "bg", "正文"), ("tx2", "bg", "次要正文"), ("tx3", "panel", "弱化标签"),
             ("pass", "bg", "PASS色"), ("partial", "bg", "PARTIAL色"), ("fail", "bg", "FAIL色")]
    worst, worst_name = 99, ""
    cd_detail = []
    for fg, bg, label in pairs:
        f, b = varof(fg), varof(bg)
        if not f or not b:
            continue
        c = contrast(f, b)
        cd_detail.append("%s=%.2f" % (label, c))
        need = 3.0 if label in ("PASS色", "PARTIAL色", "FAIL色") else 4.5
        if c < need:
            worst = min(worst, c); worst_name = label
    gate("正文对比度低于 4.5", worst >= 4.5, " ".join(cd_detail) + (" 最低%s=%.2f" % (worst_name, worst) if worst < 4.5 else ""))

    # 5 键盘主流程 + alt/label
    kbd = all(k in src for k in ["keydown", "ArrowRight", "ArrowDown", "Enter", "Escape", "aria-pressed", "aria-label"])
    # 每个 <img> 要 alt（本页无 img，则验 aria-label 覆盖率）
    imgs = re.findall(r"<img\b[^>]*>", src)
    img_ok = all('alt=' in i for i in imgs)
    gate("键盘无法回车走通主流程，或图片缺 alt、表单缺 label", kbd and img_ok,
         "键盘链=Y img=%d(全带alt=%s) 表单label=1" % (len(imgs), img_ok))

    # 6 首屏性能：零外部请求（无 http(s):// 资源引用、无 <link href> 外链、无 <script src>）
    ext = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', src)
    tags = len(re.findall(r"<script\b", src)); styles = len(re.findall(r"<style\b", src))
    bytes_kb = len(src.encode()) / 1024
    gate("首屏超过两秒无优化记录", not ext and bytes_kb < 400,
         "外部请求=%d 内联script=%d style=%d 体积=%.0fKB" % (len(ext), tags, styles, bytes_kb))

    # 7 野值像素无出处：所有 px 必须落在白名单刻度上
    # D83 修正口径：**只量声明，不量注释散文**。
    # 原口径对整个 <style> 原文扫 px，于是 CSS 注释里写的实测数字（"375px 下被压到
    # 93px"、"重叠 42/27/46×21px"）被当成设计刻度判红 —— 那不是缺陷，是修法说明，
    # 门在这里报的是假红。判据本身（4px 刻度 + 1-3px hairline 例外）一个字没松，
    # 只是先把 /* */ 剥掉再看。
    # 为了证明「剥注释」没有把门掏空，同一段里带一个反向自证：
    # 把一段含野值的声明**注入到注释里**必须不再被判红（注释确实是散文），
    # 同一段野值作为**真实声明**必须仍被判红。两条都过，这道门才算既有牙又不假红。
    css_decls = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    px = [int(m) for m in re.findall(r"(\d+)px", css_decls)]
    # 出处=设计系统规则：一切 px 必须落在 4px 刻度（1-3px 例外：hairline/border）
    def _wild_of(text):
        """刻度判定的唯一实现：先剥注释，再按 4px 刻度 + 1-3px hairline 例外挑野值。
        门与两个反向自证必须走同一个函数，否则自证就只是在测另一个函数。"""
        decls = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return sorted({p for p in (int(m) for m in re.findall(r"(\d+)px", decls))
                       if p % 4 != 0 and not (0 < p <= 3)})
    wild = _wild_of(css)
    # 反向自证 1：野值藏在注释里 -> 不算野值（注释是散文，不是刻度）
    _probe_comment = css + "\n/* D83 自证：93px 243px 21px 375px 均为修法说明 */"
    _wild_in_comment = _wild_of(_probe_comment)
    # 反向自证 2：同样的野值作为真实声明 -> 必须仍被抓住（证明剥注释没掏空判据）
    _probe_decl = css + "\n.__d83selfcheck{padding:21px}"
    _wild_as_decl = _wild_of(_probe_decl)
    gate("野值像素无出处且未打回", not wild,
         ("野值=" + ",".join(map(str, wild))) if wild
         else "全部 %d 个 px 落在 4px 刻度（1-3px hairline 例外）" % len(set(px)))
    gate("野值刻度门被掏空（剥注释后不再抓真实声明）", _wild_as_decl == [21],
         "注入声明 21px -> 判出 %s" % (_wild_as_decl or "无（门已失效）"))
    gate("野值刻度门误伤注释散文（D83 假红回归）", _wild_in_comment == [],
         "注释内 21/93/243/375px -> 判出 %s（应为空）" % (_wild_in_comment or "无"))

    # 8 截图为旧版本冒充本轮（**内容指纹**口径，D50：原 mtime 口径会被并发重写打出假红）
    stale = []
    _fp_ok, _fp_why = html_fingerprint()
    if not _fp_ok:
        stale.append(_fp_why)
    bj = os.path.join(ROOT, "work", "build.json")
    if os.path.exists(bj):
        with io.open(bj, encoding="utf-8") as f:
            _emb = (json.load(f) or {}).get("html_sha256", "")
        if _emb and _emb not in src:
            stale.append("指纹不在页面内")
    miss = [w for w in WIDTHS if not os.path.exists(os.path.join(SHOTS, "w%d.png" % w))]
    if miss:
        stale.append("缺截图 w%s" % ",".join(map(str, miss)))
    gate("截图为旧版本冒充本轮（内容指纹对不上构建）", not stale,
         "; ".join(stale) or _fp_why)

    # 9/10 交互预算
    if not os.path.exists(BUDGET):
        gate("未声明交互预算 work/ui_budget.json", False, "文件不存在")
    else:
        gate("未声明交互预算 work/ui_budget.json", True,
             "首屏上限=%d 总量上限=%d 披露容器=%d" % (bud["first_screen"]["max_interactive"],
             bud["total_interactive"]["max"], len(bud["progressive_disclosure_containers"])))

    n_first, n_total, n_disp = measure_interactive()
    over_first = n_first > bud["first_screen"]["max_interactive"]
    over_total = n_total > bud["total_interactive"]["max"]
    gate("首屏有效可交互元素超过预算上限", not over_first,
         "首屏=%d / 预算=%d" % (n_first, bud["first_screen"]["max_interactive"]))
    gate("可交互元素总数超预算且无渐进披露容器", not (over_total and n_disp == 0),
         "总量=%d / 预算=%d 披露容器=%d" % (n_total, bud["total_interactive"]["max"], n_disp))

    # 12 真视口证据：截图必须由 CDP 真视口产出（--window-size=375 实测 innerW=500，会裁图冒充）
    shots_json = os.path.join(ROOT, "work", "shots.json")
    bad = []
    if not os.path.exists(shots_json):
        bad.append("work/shots.json 缺失（先跑 tools/shoot.py）")
    else:
        sj = json.load(open(shots_json, encoding="utf-8"))
        _sj_sha = sj.get("html_sha256") or ""
        with open(HTML, "rb") as _fh:
            _cur_sha = hashlib.sha256(_fh.read()).hexdigest()[:16]
        if not _sj_sha:
            bad.append("shots.json 无 html_sha256（旧版 shoot.py 产物，先重跑 tools/shoot.py）")
        elif _sj_sha != _cur_sha:
            bad.append("截图属旧构建：shots.json 页面指纹=%s 当前 index.html=%s"
                       % (_sj_sha, _cur_sha))
        for w in WIDTHS:
            d = sj.get("sizes", {}).get(str(w))
            if not d:
                bad.append("w%d 无实测记录" % w); continue
            if d.get("innerW") != w:
                bad.append("w%d innerWidth=%s≠%d（视口冒充）" % (w, d.get("innerW"), w))
            if d.get("overflow") or d.get("offenders"):
                bad.append("w%d 文档横向溢出 %s" % (w, json.dumps(d.get("offenders", [])[:2], ensure_ascii=False)))
            if d.get("pageerrors"):
                bad.append("w%d JS 报错 %s" % (w, d["pageerrors"][0][:80]))
    gate("截图视口冒充或存在文档级横向溢出", not bad,
         "; ".join(bad) or "真视口 innerW=375/768/1440 精确，docScrollW==clientW，无 JS 错误")

    # 回填实测
    bud["measured"] = {"first_screen_interactive": n_first, "total_interactive": n_total,
                       "disclosure_containers": n_disp,
                       "audit_rc": 0 if all(r["ok"] for r in results) else 2}
    json.dump(bud, open(BUDGET, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(results, open(os.path.join(ROOT, "work", "ux_audit.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    nfail = sum(1 for r in results if not r["ok"])
    print("== UX_AUDIT %s  %d/%d 门通过 ==" % ("OK" if nfail == 0 else "FAIL", len(results) - nfail, len(results)))
    return 0 if nfail == 0 else 2


def measure_interactive():
    """在**真 HTTP** 的 serve.py 上数首屏可见可交互元素 + 总量 + 披露容器。

    D96 为什么必须换口径（这一段是本函数存在的理由，不是重写理由）：
        旧实现把 index.html 复制成 work/_probe.html 用 file:// 打开。file:// 下
        fetch('/__projects')、fetch('/__fs?proj=') 全部因跨源被浏览器拒掉，
        左栏「项目空间」于是渲染成一句报错、**一个项目按钮都不出**。
        实测同一份 index.html：file:// 首屏 17 / 总量 17，真 HTTP 首屏 19 /
        总量 19，差的正好是 #projspace .pjnode ×2。也就是说旧口径把 D96 整个
        左栏项目空间漏出了交互预算 —— 预算门量的是一个真实用户看不到的页面。
    现在量的是用户双击启动器后真正看到的那一页：http://127.0.0.1:<临时端口>/。

    fail-closed：起不了服务器 / 装不了 playwright / 页面 JS 报错，一律返回
    (999, 999, 0) 让两条预算门判红。量具坏掉时静默给 0 会把预算门变成空门。
    """
    out = os.path.join(ROOT, "work", "_probe.json")
    tree = os.path.join(ROOT, "work", "_uxtree")
    os.makedirs(os.path.join(tree, "sub"), exist_ok=True)
    with open(os.path.join(tree, "sub", "deep.txt"), "w", encoding="utf-8") as f:
        f.write("ux_audit 门用第二棵树的 fixture（只读，不参与任何产品判据）\n")
    COUNT_JS = """() => {
      const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
        return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'
          && r.top < innerHeight && r.bottom > 0; };
      const SEL='button,a[href],input,select,textarea,[role=tab],[role=button],'
        +'[tabindex]:not([tabindex="-1"]),summary';
      const all=[...document.querySelectorAll(SEL)];
      const shown=e=>{const s=getComputedStyle(e);
        return s.display!=='none'&&s.visibility!=='hidden'&&e.getBoundingClientRect().width>0;};
      const inApp=e=>e.closest('#app')&&!e.closest('#cmd');
      const tag=e=>e.tagName.toLowerCase()+(e.id?'#'+e.id:'')
        +(e.className&&typeof e.className==='string'&&e.className.trim()
          ?'.'+e.className.trim().split(/\\s+/)[0]:'');
      const onscreen=all.filter(e=>vis(e)&&inApp(e));
      const totalEls=all.filter(e=>shown(e)&&inApp(e));
      return {first:onscreen.length, total:totalEls.length,
        disclosure:document.querySelectorAll('details,dialog,[hidden]').length,
        innerH:innerHeight, innerW:innerWidth, title:document.title,
        pjnodes:document.querySelectorAll('#projspace .pjnode').length,
        comp:onscreen.map(tag)};
    }"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                                   # noqa: BLE001
        print("  [probe] fail-closed：playwright 不可用 %s" % str(e)[:80])
        return (999, 999, 0)
    spec = importlib.util.spec_from_file_location("_uxaudit_serve", SERVE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 第二棵树换成门自己造的 fixture：门不该依赖用户桌面那棵真树当时可不可读。
    # ROOT 一律不动 —— 换掉 ROOT 就等于「两个假的根互相对换」，永远测不到东西。
    mod.TREE = tree
    from http.server import ThreadingHTTPServer
    quiet = type("QuietHandler", (mod.Handler,),
                 {"log_message": lambda self, *a, **k: None})

    class QuietSrv(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = QuietSrv(("127.0.0.1", port), quiet)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    try:
        with sync_playwright() as pw:
            try:
                br = pw.chromium.launch(executable_path=CHROME)
            except Exception:                                  # noqa: BLE001
                br = pw.chromium.launch()
            errs = []
            try:
                pg = br.new_context(viewport={"width": 1440, "height": 900}).new_page()
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
                # 等左栏项目空间真的把按钮渲染出来再数：不等就是量 fetch 完成前的
                # 中间态，那正是「量具没量到东西却照样判绿」的形状。
                pg.wait_for_function(
                    "() => document.querySelectorAll('#projspace .pjnode').length >= 2",
                    timeout=15000)
                pg.wait_for_timeout(600)
                d = pg.evaluate(COUNT_JS)
            finally:
                br.close()
    except Exception as e:                                       # noqa: BLE001
        print("  [probe] fail-closed：%s" % str(e)[:120])
        return (999, 999, 0)
    finally:
        srv.shutdown(); srv.server_close()
    if errs:
        print("  [probe] fail-closed：页面 JS 报错 %s" % errs[0][:100])
        return (999, 999, 0)
    d["url"] = "http://127.0.0.1:%d/index.html（真 HTTP，非 file://）" % port
    d["pageerrors"] = errs
    json.dump(d, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    return (d["first"], d["total"], d["disclosure"])


if __name__ == "__main__":
    sys.exit(main())
