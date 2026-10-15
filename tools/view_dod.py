#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图 DoD 硬门：真视口逐屏走查（屏数 = len(VIEWS) × len(WIDTHS)，别写死）。

为什么单独一个门：UX.md §5 要求「三尺寸截图缺任一尺寸或任一页面」即 FAIL，
而 tools/shoot.py 只截默认视图（总览）—— 轮次/矩阵/账本/引擎/Agent 台
五屏从未走过。本门逐屏验五件事，任一不成立即 FAIL：
  1. 视图真的切过去了（aria-selected 唯一命中 + 对应 .v-* 可见）
  2. 屏里有真内容（渲染节点数 + 文本长度，不是空壳）
  3. 文档级横向溢出 = 0（内部滚动容器不算）
  4. 无 JS 报错
  5. 真视口：innerWidth 精确等于目标宽度（headless --window-size 会冒充）
视图口径 = 模板 index.template.html 的 tablist：13 个视图全部在册 ——
9 面数据驾驶舱（总览/轮次/矩阵/账本/引擎/Agent台/目标台/复盘台/能力地图）
+ 工作区 ws + R7 新做的终端/文件/设置（键与 tab 的 data-v 同源）。
一个 tab 都不排除：本门是 AC 点名的逐视图 DoD 门，tablist 里哪个键在册外，
它那一屏就没量过 —— 2026-10-09 就因把 ws 排除在册外被打回重做。
产出 work/view_dod.json，rc=0 全过 / 2 有红项。
"""
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
SHOTS = os.path.join(ROOT, "docs", "UX-SHOTS", "views")
OUT = os.path.join(ROOT, "work", "view_dod.json")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIDTHS = [375, 768, 1440]
# 2026-10-10 W2 views9（R7）：视图清单 9 → 13。模板 tablist 实有 13 个 tab，
# 本门逐个在册：前 9 面是**数据驾驶舱**（D77 目标台、D78/D79 轮次闭环、
# D95 能力地图），第 10 个「工作区」ws 是终端/文件/浏览器三面合一的交互工作面，
# 第 11-13 个是 R7 新做的三个真功能视图（终端 term / 文件 file / 设置 set —— 
# 三个都是真 fetch、真 rc、真文件内容，不是装饰空壳）。
#
# 为什么 13 而不是「挑 9 个凑数」：2026-10-09 把 ws 排除在册外的做法被打回重做，
# 理由是 tablist 里任何一个 tab 在册外，它的五项检查（切过去/真内容/无文档级溢出/
# 无 JS 报错/真视口）就有一屏没量过。现在一个 tab 都不排除，13 × 3 = 39 屏全量走查。
# 键 = 模板 tab 的 data-v 值 = 面板 id "v-<键>" 的后缀，还与 CL / TAB 两张 JS
# 映射表同源；tools/capmap_gate.py 的接线门逐键核对这四处，缺一即红。
VIEWS = [("over", "总览"), ("round", "轮次"), ("mx", "阶段矩阵"),
         ("led", "证据账本"), ("eng", "引擎与规则"), ("work", "Agent台"),
         ("goal", "目标台"), ("retro", "复盘台"), ("cap", "能力地图"),
         ("ws", "工作区"), ("term", "终端"), ("file", "文件"),
         ("set", "设置")]
NSCREENS = len(VIEWS) * len(WIDTHS)   # 屏数由 VIEWS/WIDTHS 推导，别写死

# 视图 -> (前置等待 or None, 加载按钮选择器 or None, 落到真数据态要等的产物选择器)。
# 与 tools/layout_gate.py 的 SETTLE 同源同口径：都是「只读地把屏推到真数据态再量」。
# 复盘台/能力地图的 view() 分支调的是 retroReset()/capReset()，进屏只渲染「未读」
# idle 态（不预填假账本的口径）—— 不点加载按钮就量，量到的是一页 idle，
# 「零重叠零溢出」是假满分（layout_gate.py 第 74-88 行记着这条实测教训）。
# 等待条件同时收 .gcard 与 .gblock：fail-closed 分支也会落一张带真实错误文案的
# .gblock，读到读不到都算落到真态，量的始终是用户真能看到的那一屏。
PRIME = {"retro": ("#rtgo", "#rtbox .gcard, #rtbox .gblock"),
         "cap": ("#capgo", "#capbuckets .capb, #capbuckets .gblock"),
         "term": ("#tquick button", "#tout .rc"),
         "file": ("#ftree button[data-p]", "#ftree button[data-d='0']",
                 "#fcode:not(.idle)"),
         "set": (None, "#setmeta .setr")}
# D97：本门在**真 HTTP** 上走查。file:// 下 fetch('/\_\_projects')、fetch('/\_\_health')
# 全被跨源拒掉，左栏渲染成「项目空间读不到」、右栏满屏「不可判定」—— 27 屏走查量的
# 是一个真实用户永远看不到的页面（同 D96 对 ux_audit 的实测结论，见该文件
# measure_interactive() docstring）。现在量的是用户双击启动器后真正看到的那一页。
SERVE_PY = os.path.join(HERE, "serve.py")


def html_fingerprint():
    """读 work/build.json 的 html_sha256，与当前 index.html 的真实 sha256 比。
    返回 (ok, why)：一致 = 内容没变 = 截图**不算**陈旧。

    为什么要从 mtime 换成内容 sha（D50 实测根因）：
    mtime 判据把「内容一个字没改、只是被并发 build.py 重写了一遍」当成陈旧。
    实测 gates.sh 无锁，shoot/ux_audit/view_dod 并发跑时 build.py 正好重写
    index.html，于是三张截图的 mtime 集体早于 html 的 mtime → 纯假红。
    内容指纹不受重写影响，这正是区分「真陈旧」与「被重写」的唯一可用信号。"""
    live = os.path.abspath(HTML) == os.path.join(ROOT, "index.html")
    if not live:
        # 隔离副本（--html）：没有属于它的 build.json 基线，硬对账等于**恒红**。
        # 恒红的判据比没有判据更糟：它会让反向门 selftest 的每个变异体都假红，
        # 于是「rc!=0」不再说明「注入的缺陷被抓到」。故显式跳过并说明理由。
        return True, "隔离副本模式：无本页基线，内容指纹对账不适用"
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


PROBE_JS = """(key) => {
  const de = document.documentElement;
  const panel = document.getElementById('v-' + key);
  const offenders = [];
  for (const e of panel.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;
    if (r.right > de.clientWidth + 1 || r.left < -1) {
      let scroller = false, p = e.parentElement;
      while (p) {
        const s = getComputedStyle(p);
        const okx = ['auto','scroll','hidden'].indexOf(s.overflowX) >= 0;
        const ok = ['auto','scroll','hidden'].indexOf(s.overflow) >= 0;
        if (okx || ok) { scroller = true; break; }
        p = p.parentElement;
      }
      if (scroller) continue;
      offenders.push({tag: e.tagName,
        cls: (typeof e.className === 'string' ? e.className : '').slice(0, 30),
        left: Math.round(r.left), right: Math.round(r.right)});
    }
  }
  const sel = [...document.querySelectorAll('[role=tab]')]
    .filter(t => t.getAttribute('aria-selected') === 'true').map(t => t.getAttribute('data-v'));
  const cs = getComputedStyle(panel);
  return {
    innerW: innerWidth,
    docScrollW: de.scrollWidth, docClientW: de.clientWidth,
    visible: cs.display !== 'none' && cs.visibility !== 'hidden' && panel.offsetHeight > 0,
    nodes: panel.querySelectorAll('*').length,
    textLen: (panel.innerText || '').trim().length,
    tabsSelected: sel, offenders: offenders.slice(0, 8),
  };
}"""

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))


# D50（2026-10-03）：真浏览器基础设施故障重试，与 act_gate 共用 tools/flake_retry.py。
# flake_inject() 必须在**门体最前面**：CD_FLAKE_INJECT=always 时它让本门亚秒级假红，
# 用来证明 run_gate 包裹真的接上了（删掉包裹 → 裸 traceback + rc=1 → 自测判红）。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from flake_retry import flake_inject, run_gate  # noqa: E402,F401


def _page_sha():
    try:
        with open(HTML, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def _body(_ctx):
    flake_inject()
    del results[:]  # 重试时别把上一轮的判据累加进来
    # 隔离缝（D47）：--html / --shots / --out。默认值一字不变，老调用方零影响。
    # 为什么必须有：反向门要在**隔离副本**上注入缺陷再验，不能就地改活的 index.html。
    # 不就地改的理由与 tools/neg_a11y.py 同一份事故记录（D39）：并发窗口内常驻 8790
    # 实例、真浏览器门、act_gate 的真点按读的是同一份文件，注入期它们会读到缺陷版，
    # 表现为「门随机红」。没有 --html 就只能就地改，于是这个门**结构上无法自证有牙**。
    global HTML, SHOTS, OUT
    argv = sys.argv[1:]
    for flag, var in (("--html", "HTML"), ("--shots", "SHOTS"), ("--out", "OUT")):
        if flag in argv:
            globals()[var] = argv[argv.index(flag) + 1]
    # D103（2026-10-11）：_sha_start 必须在**隔离缝解析之后**取，顺序不许再调。
    # 改前的顺序（_sha_start 在解析前）在隔离模式下是拿**两个文件**对账：start 读
    # 活的 index.html，end 读副本。副本内容与活页逐字节相同时它偶然成立，于是只有
    # M6（唯一逐字节副本的变异体）真有牙；M1–M5 的副本被注入过脚本、恒与活页不等，
    # 这条判据对它们**恒红** —— 「rc!=0 ⇒ 注入被抓到」这个结论里 5/6 来自假来源。
    # 现在 start/end 同源，判据说的就是它做的事（活页/副本都成立的真牙）。
    _sha_start = _page_sha()
    from playwright.sync_api import sync_playwright
    if not os.path.exists(HTML):
        print("FAIL: index.html 不存在")
        return 2
    os.makedirs(SHOTS, exist_ok=True)
    screen_report, bad_screens, errors = {}, [], []

    # D97：起真服务再走查。D105（2026-10-11 实测）：隔离副本**只换 index.html
    # 这一页的字节**，服务根一律是活产品根。D97 原实现把整个根换成「symlink 版
    # 临时根」，结果 /__fs 的 realpath 前缀沙箱把 symlink 的目标判成越界 —— 实测
    # fs_read('README.md', 临时根) 直接返回「路径越界」；于是 file 视图在隔离模式下
    # **永远**落不到真数据态，「任一视图 JS 报错」和「屏截图齐」两条判据在副本上恒红：
    # M1/M5 变异体就算什么都不注入也会「红」—— 变异体被无关的红洗白，是空门。
    # 现在副本模式与生产模式只差这一页字节（/__fs、/__projects、/__health、其余
    # 静态资源全走活根），两条判据在副本上也必须是绿的，变异体的红只可能来自注入。
    # fail-closed 不变：服务器起不来就判红。
    _live = os.path.abspath(HTML) == os.path.join(ROOT, "index.html")
    iso_index = None if _live else os.path.abspath(HTML)
    spec = importlib.util.spec_from_file_location("_viewdod_serve", SERVE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = ROOT
    from http.server import ThreadingHTTPServer

    class QuietHandler(mod.Handler):
        """只换这一页：/__fs、/__projects、/__health、其余静态资源全走活根。"""

        def log_message(self, *a, **k):
            pass

        def translate_path(self, path):
            if iso_index and urllib.parse.urlparse(path).path == "/index.html":
                return iso_index
            return mod.Handler.translate_path(self, path)

    quiet = QuietHandler

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
    print("  [D97] 真 HTTP 走查 %s（serve root=%s）"
          % (base_url, "活产品根" if _live else "隔离副本"))
    try:
      with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        for w in WIDTHS:
            pg = b.new_page(viewport={"width": w, "height": 900}, device_scale_factor=1)
            errs = []
            pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)[:160]))
            pg.goto(base_url, wait_until="load")
            # 等左栏项目空间真把按钮渲染出来再逐屏走查：不等就是量 fetch 完成前的
            # 中间态，那正是「量具没量到东西却照样判绿」的形状（D96 同款教训）。
            # 驾驶舱的项目空间按钮是 #projspace .pjnode（projInit() 拉 /__projects
            # 真清单后渲染）；本门自己起的 HTTP 服务能喂到真数据，超时即判红。
            try:
                pg.wait_for_function(
                    "() => document.querySelectorAll('#projspace .pjnode').length >= 1",
                    timeout=15000)
            except Exception as e:                # noqa: BLE001
                errs.append("项目空间未渲染: " + str(e)[:80])
            pg.wait_for_timeout(700)
            for key, label in VIEWS:
                pg.evaluate("k => view(k)", key)
                # 进屏后把这一面推到真数据态再量（见 PRIME 注释）：retro/cap 的
                # view() 落的是「未读」idle 态，不点加载按钮量的就是一页空台面。
                _prime = PRIME.get(key)
                if _prime:
                    if len(_prime) == 3:
                        # (前置等待, 要点按钮, 真数据态判据)。file 视图的文件树是
                        # view('file') 里异步拉的：不等第一批条目就点第一个文件，
                        # 点中的是空气，然后 20s 超时把真页面判成假红。
                        _pre, _btn, _wait = _prime
                        if _pre:
                            try:
                                pg.wait_for_function(
                                    "s => document.querySelectorAll(s).length >= 1",
                                    arg=_pre, timeout=20000)
                            except Exception as e:            # noqa: BLE001
                                errs.append("%s 前置数据未就绪: %s" % (key, str(e)[:80]))
                    else:
                        _btn, _wait = _prime
                    if _btn:
                        pg.evaluate("s => { const b = document.querySelector(s);"
                                    " if (b) b.click(); }", _btn)
                    try:
                        pg.wait_for_function(
                            "s => document.querySelectorAll(s).length >= 1",
                            arg=_wait, timeout=20000)
                    except Exception as e:                # noqa: BLE001
                        # 落不到真数据态 = fail-closed 判红，不许拿 idle 态充数。
                        errs.append("%s 未落到真数据态: %s" % (key, str(e)[:80]))
                pg.wait_for_timeout(320)
                d = pg.evaluate(PROBE_JS, key)
                d["pageerrors"] = list(errs)
                errs[:] = []
                path = os.path.join(SHOTS, "%s-w%d.png" % (key, w))
                # 截图**原子落盘**（2026-10-03 实测加的根因修复，不是预防性焦虑）。
                # 实测链：同一页连跑两次 27 屏 sha 逐字节全等 ⇒ 渲染是**确定的**
                #（实测 /tmp/fix_verif/summary.txt STAGE1）。既然内容确定，
                # 并发跑 view_dod 时对方读到不同 sha 就只剩一种可能：
                # **读到了我正写到一半的文件** —— playwright 的
                # screenshot(path=) 是就地 truncate+覆写，没有原子性。
                # 那个窗口是真实的：引擎 D50 的 desktop_gates 与本机链跑都含
                # view_dod/view_dod_selftest，两边同时写 docs/UX-SHOTS/views。
                # 症状是间歇性的 view_dod_selftest rc=2（旧版把共享目录当承重
                # 判据时），实测台账 /tmp/chain_runs/130550/rc.txt。
                # os.replace 在同一文件系统内是原子的：读者只会看到完整的旧版
                # 或完整的新版，永远看不到半个 PNG。
                # 临时名**必须仍以 .png 结尾**：playwright 的 screenshot(path=)
                # 靠扩展名推断 mime type，路径不以已知图片扩展名收尾就抛
                # `path: unsupported mime type ".part-5113"`。
                # 2026-10-03 13:47 实测：上一版写成 `<name>-w375.png.part-<pid>`，
                # 于是 27 屏一张都存不下，view_dod rc=2、view_dod_selftest 量具
                # 自身异常（「无门汇总行」）—— 两个红其实是同一个根因，
                # 不是并发假红。pid 插在扩展名**之前**，扩展名留在末尾。
                tmp = "%s.part-%d%s" % (path[:-4], os.getpid(), path[-4:])
                try:
                    pg.screenshot(path=tmp)
                    os.replace(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                d["png_bytes"] = os.path.getsize(path)
                d["viewport_faked"] = (d["innerW"] != w)
                d["overflow"] = d["docScrollW"] > d["docClientW"] or bool(d["offenders"])
                screen_report["%s@%d" % (key, w)] = d
                if (not d["visible"] or d["nodes"] < 3 or d["textLen"] < 20
                        or d["tabsSelected"] != [key]
                        or d["overflow"] or d["viewport_faked"] or d["pageerrors"]):
                    bad_screens.append("%s@%d" % (key, w))
                if d["pageerrors"]:
                    errors.append("%s@%d: %s" % (key, w, d["pageerrors"][0]))
            pg.close()
        b.close()
    finally:
        srv.shutdown()
        srv.server_close()

    missing = [k for k, _ in VIEWS for w in WIDTHS
               if not os.path.exists(os.path.join(SHOTS, "%s-w%d.png" % (k, w)))]
    gate("%d 视图 × %d 尺寸 = %d 屏截图齐" % (len(VIEWS), len(WIDTHS), NSCREENS),
         not missing and not bad_screens,
         "截图=%d/%d 逐屏实测过=%d/%d" % (NSCREENS - len(missing), NSCREENS,
                                        NSCREENS - len(bad_screens), NSCREENS)
         + ("  红屏=" + ",".join(bad_screens[:6]) if bad_screens else ""))

    fake = [k for k, d in screen_report.items() if d["viewport_faked"]]
    gate("真视口未被冒充（innerWidth 精确）", not fake,
         "innerW 精确=375/768/1440 × %d 视图" % len(VIEWS) if not fake else "冒充=" + ",".join(fake))

    ovf = [k for k, d in screen_report.items() if d["overflow"]]
    gate("任一视图存在文档级横向溢出", not ovf,
         "%d 屏 docScrollW==clientW 且无越界元素" % NSCREENS if not ovf else "溢出=" + ",".join(ovf[:6]))

    thin = ["%s(nodes=%d,text=%d)" % (k, d["nodes"], d["textLen"])
            for k, d in screen_report.items() if d["nodes"] < 3 or d["textLen"] < 20]
    gate("任一视图是空壳（无真内容）", not thin,
         "每屏节点≥3 且文本≥20 字符" if not thin else "; ".join(thin[:6]))

    gate("任一视图 JS 报错", not errors,
         "%d 屏 0 pageerror" % NSCREENS if not errors else "; ".join(errors[:4]))

    # D50：mtime 口径 → 内容指纹口径（并发重写 index.html 会打出纯假红）
    stale = []
    _fp_ok, _fp_why = html_fingerprint()
    if not _fp_ok:
        stale.append(_fp_why)
    # 走查期间页面被改（并发 build.py / 手工编辑）→ 27 屏来自两个不同版本，
    # 互相不自洽。这一条对隔离副本同样成立，所以它是活页/副本都有的真牙。
    _sha_end = _page_sha()
    if _sha_start and _sha_end and _sha_start != _sha_end:
        stale.append("页面在走查期间被改（%s→%s），%d 屏非同一版本"
                     % (_sha_start, _sha_end, NSCREENS))
    miss = [("%s-w%d" % (k, w)) for k, _ in VIEWS for w in WIDTHS
            if not os.path.exists(os.path.join(SHOTS, "%s-w%d.png" % (k, w)))]
    if miss:
        stale.append("缺截图=" + ",".join(miss[:6]))
    gate("截图为旧版本冒充本轮（内容指纹对不上构建）", not stale,
         _fp_why if not stale else "不成立=" + ",".join(stale[:6]))

    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"tool": "view_dod.py", "method": "playwright-cdp-real-viewport",
                   "url": base_url, "transport": "real-http",
                   "served_root": "live" if _live else "isolated-copy",
                   "views": [v[0] for v in VIEWS], "widths": WIDTHS,
                   "screens": screen_report, "gates": results},
                  f, ensure_ascii=False, indent=1)

    nfail = sum(1 for r in results if not r["ok"])
    print("== VIEW_DOD %s  %d/%d 门通过（%d 视图 × %d 尺寸 = %d 屏真视口走查）=="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail, len(results),
             len(VIEWS), len(WIDTHS), NSCREENS))
    return nfail == 0, ("%d/%d 门通过" % (len(results) - nfail, len(results)))


def main():
    """唯一入口。判据红 = 结论，一次都不重试；基础设施故障才重试（D40 铁律）。"""
    ok, detail = run_gate(_body, None)
    if detail.startswith("EXC "):
        print("  [EXC] " + detail)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
