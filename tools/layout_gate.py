#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D82 布局硬门：真视口逐屏抓两类「AI 味」缺陷 —— 元素互相压字 / 文字被裁。

为什么单独一个门（真实缺口，不是预防性焦虑）：
  view_dod 已做 24 屏真视口逐屏走查，但 D82 真机目视 375px 截图时抓到两处
  **24 门全绿时就在**的缺陷：
    A. 顶栏自相撞：.hdinfo 被 flex 压到 93px、内容 243px（overflow:visible），
       溢出的 #fresh/#vtag 直接压在 nav tab 上（真重叠 3 处 42/27/46x21px）。
    B. 复盘台长簇名被裁：CAP_substrate+SPEC_coverage_complete+… 这种无空格长
       token 在 .glist .gw b 里无法换行，clientWidth 232 / scrollWidth 305。
  也就是说「截图齐 + 无文档级横向溢出 + 无 JS 报错」这三条**看不见**这两类：
  A 是 flex 内部溢出（文档 scrollWidth 不变），B 是换行点缺失。本门补这两条。

判据口径（排除项都写明理由 —— 不排除就是把设计当缺陷）：
  重叠：只查**有直接文字的叶子节点**两两相交（父子不查）；交叠区取
        「元素矩形 ∩ 全部 overflow!=visible 祖先的矩形」，滚动容器里被裁掉的
        部分用户看不见；祖先链含 position:fixed|sticky 的排除（.live 热重载
        徽章是设计上的浮层，本来就盖在滚过的正文上）。
        行内元素按 getClientRects() **逐片段**比，不用 getBoundingClientRect()：
        后者是所有行片段的并集盒子，实测 .ln 段 nRects=8、并集 322x86，相邻
        两段的并集必然相交 —— 那是量具的并集重叠，不是「字被压住」。
  裁字：scrollWidth > clientWidth+2 且**自身** overflowX=visible 且无
        text-overflow:ellipsis，且溢出部分没被祖先裁掉（=用户真看不见那截字）。
  不可见：checkVisibility() 说 false 的一律不算叶子（闭合 <details> 的子树
        浏览器不绘制，但 rect 仍能量到 content-visibility 的残留幻盒 —— 实测
        .rdb h392 与 summary h60 相交，那是幻盒不是缺陷）。
        **但排除不等于验过**，所以另跑一轮 details 全展开再量（见 EXPANDED 门）。
  真视口：playwright CDP 设 viewport，innerWidth 精确等于目标宽度
        （chrome --window-size<500 会冒充 500，见 tools/shoot.py 头注）。

D83 定性实录（CTRL 一度抓到 89 项，逐类实测后 4 类里只有 1 类是真缺陷）：
  ① round 72 项  P/DIV.lbl × SPAN.tag/g —— 闭合 <details class="rd"> 内，
     checkVisibility=false（幻盒）。已由 checkVisibility 门排除 + 展开轮覆盖。
  ② eng 11 项 / over 2 项  SPAN.ln、B、B —— display:inline 的并集盒子，
     nRects 分别 8/2。已改为逐片段比对。
  ③ mx 2 项  LI「declared_leg:isolated_copy_pre_reclaim|declared_snapshot=PASS」
     cw=301 / sw=382 / checkVisibility=true / 祖先全 visible / 无 ellipsis
     —— **真缺陷**，与已修的 .glist .gw b 同类（长 token 无处可断），
     修法：.ro ul 加 overflow-wrap:anywhere。

自带反向门（这是本门能证明自己有牙的唯一办法）：
  CTRL  不注入任何东西，必须 clean —— 只会红的门同样没用。
  MA    把 #cmdk 从 flex 流里拽出来做 absolute 覆盖 nav tab → 必被「压字」抓住。
  MB    给 .glist .gw b 去掉 overflow-wrap → 长 token 不换行 → 必被「裁字」抓住。
  变异体只拦 **served 字节**（playwright route.fulfill），活的 index.html 零写入，
  理由与 tools/retro_gate.py / tools/neg_a11y.py 的 D39 事故记录一致。

rc=0 全过 / 2 有红项。产出 work/layout_gate.json。
"""
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
OUT = os.path.join(ROOT, "work", "layout_gate.json")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIDTHS = [375, 768, 1440]
VIEWS = ["over", "round", "mx", "led", "eng", "work", "goal", "retro", "cap", "ws",
         "term", "file", "set"]
# 视图真数据态的**只读**加载动作：视图 -> (点击目标, 等到的真实产物选择器)。
# 只收只读动作：目标台的「编译计划」会 POST 写 work/GOAL.json，那是执行不是走查，
# 走查门不许替用户改状态，所以目标台按诚实表单态验。
SETTLE = {"retro": ("#rtgo", "#rtbox .gcard"),
          # D95：工作区补进 VIEWS 的同时必须给它 settle。view('ws') 里
          # `if(key==='ws'&&!wsPath) wsList('')` 才去拉 /__fs —— 不等它回来就量，
          # 量到的是一个连文件条目都没有的空壳，量出「零重叠零溢出」的假满分。
          # 等 #wstree 里真出现 li 才算落到真数据态。
          "ws": (None, "#wstree li"),
          # R7 新三面（终端/文件/设置）同口径：都先把屏推到真数据态再量，否则
          # 量到的是一页 idle，「零重叠零溢出」是假满分。终端要点一个常用命令
          # 并等 .rc 真的落下来 —— #tout 是 pre，有占位文案「终端待命」，
          # 只等 #tout 的文本永远秒过，量的却是没跑过命令的那一屏。
          # 文件树是惰性的，要等 li>button[data-p] 真出现（真拉过 /__fs）。
          "term": ("#tquick button[data-cmd='ls -1 tools']", "#tout .rc"),
          "file": (None, "#ftree li button[data-p]"),
          # 设置面无点击目标：setInit() 一进屏就 setRefresh('all') 真拉三路
          # 只读源，等 .setr 行渲染出来即可。
          "set": (None, "#setmeta .setr"),
          # 能力地图与复盘台同口径：不点按钮就是「未读」，量到的是一页 idle，
          # 量出「零重叠零溢出」的假满分。所以点「读能力地图」并等四桶卡片
          # （#capbuckets .capb）真的渲染出来，才算落到真数据态。
          "cap": ("#capgo", "#capbuckets .capb")}
NSCREENS = len(VIEWS) * len(WIDTHS)

PROBE_JS = r"""() => {
  const CLIPS = ['hidden', 'auto', 'scroll', 'clip'];
  const shown = e => { const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
           s.visibility !== 'hidden' && +s.opacity > 0 &&
           (!e.checkVisibility || e.checkVisibility({opacityProperty: true,
             visibilityProperty: true, contentVisibilityAuto: false})); };
  const clipBox = e => {
    let L = null;
    for (let p = e.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (CLIPS.indexOf(s.overflowX) >= 0 || CLIPS.indexOf(s.overflowY) >= 0) {
        const pr = p.getBoundingClientRect();
        L = L === null
          ? {l: pr.left, t: pr.top, r: pr.right, b: pr.bottom}
          : {l: Math.max(L.l, pr.left), r: Math.min(L.r, pr.right),
             t: Math.max(L.t, pr.top), b: Math.min(L.b, pr.bottom)};
      }
    }
    return L;
  };
  // 行内元素的 getBoundingClientRect() 是它所有行片段的**并集盒子**。实测
  // .ln 段 nRects=8、并集 322x86，于是相邻两段的并集必然相交 —— 那是并集的
  // 重叠，不是「字被压住」。判重叠必须逐片段比：inline 取 getClientRects()。
  const visRects = e => {
    const L = clipBox(e);
    const s = getComputedStyle(e);
    const src = (s.display === 'inline' || s.display === 'inline flow-root')
      ? e.getClientRects() : [e.getBoundingClientRect()];
    const out = [];
    for (const r of src) {
      let x = {l: r.left, t: r.top, r: r.right, b: r.bottom};
      if (L) { x.l = Math.max(x.l, L.l); x.r = Math.min(x.r, L.r);
               x.t = Math.max(x.t, L.t); x.b = Math.min(x.b, L.b); }
      if (x.r - x.l > 0.5 && x.b - x.t > 0.5) out.push(x);
    }
    return out;
  };
  const floated = e => {
    for (let p = e; p && p !== document.documentElement; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.position === 'fixed' || s.position === 'sticky') return true;
    }
    return false;
  };
  const leaves = [...document.querySelectorAll('body *')].filter(e => {
    if (e.closest('#cmd')) return false;
    if (!shown(e)) return false;
    return [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
  });
  const ov = [], cl = [];
  const cache = new Map();
  const rects = e => { let v = cache.get(e); if (!v) { v = visRects(e); cache.set(e, v); }
                      return v; };
  for (let i = 0; i < leaves.length; i++) for (let j = i + 1; j < leaves.length; j++) {
    const a = leaves[i], b = leaves[j];
    if (a.contains(b) || b.contains(a)) continue;
    if (floated(a) || floated(b)) continue;
    let hit = null;
    for (const A of rects(a)) for (const B of rects(b)) {
      const x = Math.min(A.r, B.r) - Math.max(A.l, B.l);
      const y = Math.min(A.b, B.b) - Math.max(A.t, B.t);
      if (x > 1 && y > 1) { hit = [Math.round(x), Math.round(y)]; break; }
    }
    if (hit) ov.push([a.tagName + '.' + (a.className || '') + '#' + a.id,
      b.tagName + '.' + (b.className || '') + '#' + b.id, hit[0], hit[1],
      (a.textContent || '').trim().slice(0, 16),
      (b.textContent || '').trim().slice(0, 16)]);
  }
  for (const e of leaves) {
    if (floated(e)) continue;
    if (e.clientWidth <= 0 || e.scrollWidth <= e.clientWidth + 2) continue;
    const s = getComputedStyle(e);
    if (s.overflowX !== 'visible' || s.textOverflow === 'ellipsis') continue;
    const rs = rects(e);
    const wide = rs.reduce((m, x) => Math.max(m, x.r - x.l), 0);
    if (wide + 2 >= e.scrollWidth) continue;
    cl.push([e.tagName + '.' + (e.className || '') + '#' + e.id,
      e.clientWidth, e.scrollWidth, (e.textContent || '').trim().slice(0, 30)]);
  }
  const de = document.documentElement;
  return {innerW: innerWidth, textLen: (document.body.innerText || '').trim().length,
          nodes: document.querySelectorAll('#app *').length,
          docScrollW: de.scrollWidth, docClientW: de.clientWidth,
          overlap: ov.slice(0, 12), clip: cl.slice(0, 12),
          overlapN: ov.length, clipN: cl.length};
}"""

G, WHY = {}, {}


def add(name, cond, detail=""):
    try:
        v = bool(cond)
    except Exception as e:                                   # noqa: BLE001
        v, detail = False, "%s | EXC %r" % (detail, e)
    G[name] = v
    if not v:
        WHY[name] = str(detail)[:500]
    print(("  [ok  ] " if v else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    return v


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def sha_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_serve(tag="layout_gate_serve"):
    """借 tools/serve.py 的 Handler 起**一次性**真服务。
    为什么不用静态 server：复盘台要 fetch('/__recurrence') 拿真数据，file:// 与
    静态 server 都拿不到 —— 那样 B 类缺陷（真数据里的长 token）根本不会进 DOM，
    判据就成了永远绿的摆设。"""
    path = os.path.join(HERE, "serve.py")
    spec = importlib.util.spec_from_file_location(tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _h = mod.Handler.handle

    def _safe(self):
        try:
            _h(self)
        except (ConnectionResetError, BrokenPipeError):
            pass
    mod.Handler.handle = _safe
    return mod


def start_server(port, mod):
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    return srv


def killer(anchor, repl):
    def _k(route):
        resp = route.fetch()
        body = resp.body().decode("utf-8", "replace")
        if body.count(anchor) != 1:
            route.fulfill(response=resp)
            return
        route.fulfill(response=resp,
                      body=body.replace(anchor, repl, 1).encode("utf-8"))
    return _k


def walk(br, base, views, widths, anchor=None, repl=None, label="", expand=False):
    """逐（宽度 × 视图 × 上下滚动两位）跑探针，返回 findings。"""
    out = []
    for w in widths:
        ctx = br.new_context(viewport={"width": w, "height": 900}, device_scale_factor=1)
        pg = ctx.new_page()
        if anchor is not None:
            pg.route("**/index.html", killer(anchor, repl))
        pg.goto(base + "/index.html", wait_until="load")
        pg.wait_for_timeout(900)
        for key in views:
            pg.evaluate("k => view(k)", key)
            pg.wait_for_timeout(260)
            if expand:
                # 闭合 <details> 的子树浏览器不绘制，探针的 checkVisibility 会
                # 如实把它排除（否则量到 content-visibility 的残留幻盒）。但
                # 「排除」不等于「验过」：轮次视图的判据/增量正文全在 details
                # 里，所以另开一轮把 details 全展开再量一遍，覆盖不丢。
                pg.evaluate("() => document.querySelectorAll('details')"
                            ".forEach(d => { d.open = true; })")
                pg.wait_for_timeout(260)
            if key in SETTLE:
                btn, want = SETTLE[key]
                try:
                    # btn=None = 这一面是**进屏即自动加载**（工作区的 wsList('')），
                    # 没有按钮可点；直接 pg.click(None) 会抛 TypeError 被下面的
                    # except 吞成一条假红 finding。所以点与等要分开。
                    if btn:
                        pg.click(btn)
                    pg.wait_for_selector(want, timeout=30000)
                    pg.wait_for_timeout(320)
                except Exception as e:                       # noqa: BLE001
                    out.append([w, key, "-", "load", str(e)[:100]])
                    continue
            for pos, at_top in (("top", True), ("bottom", False)):
                pg.evaluate("t => { const m = document.getElementById('main');"
                            " if (m) m.scrollTop = t ? 0 : m.scrollHeight; }", at_top)
                pg.wait_for_timeout(160)
                d = pg.evaluate(PROBE_JS)
                if d.get("innerW") != w:
                    out.append([w, key, pos, "viewport",
                               "innerW=%s 不等于 %d" % (d.get("innerW"), w)])
                for x in d.get("overlap", []):
                    out.append([w, key, pos, "overlap", x])
                for x in d.get("clip", []):
                    out.append([w, key, pos, "clip", x])
        ctx.close()
    if out:
        print("  [%s] 抓到 %d 项，首项=%s" % (label, len(out), out[0]))
    else:
        print("  [%s] 0 项" % label)
    return out


def main():
    if not os.path.exists(HTML):
        print("FAIL: index.html 不存在")
        return 2
    from playwright.sync_api import sync_playwright
    live_sha = sha_file(HTML)
    mod = load_serve()
    port = free_port()
    srv = start_server(port, mod)
    base = "http://127.0.0.1:%d" % port
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/index.html", timeout=5).read()
            break
        except Exception:                                    # noqa: BLE001
            time.sleep(0.2)
    print("== LAYOUT_GATE 真视口 %d 屏（%d 视图 × %d 尺寸 × 上下两位）=="
          % (NSCREENS * 2, len(VIEWS), len(WIDTHS)))
    ctrl = []
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(executable_path=CHROME)
            ctrl = walk(br, base, VIEWS, WIDTHS, label="CTRL")
            ov_bad = [x for x in ctrl if x[3] == "overlap"]
            cl_bad = [x for x in ctrl if x[3] == "clip"]
            vp_bad = [x for x in ctrl if x[3] == "viewport"]
            ld_bad = [x for x in ctrl if x[3] == "load"]
            add("正例无元素压字（滚动容器/浮层/父子已排除）", not ov_bad,
                "%d 屏 0 真重叠" % NSCREENS if not ov_bad
                else "; ".join(str(x) for x in ov_bad)[:400])
            add("正例无文字被裁（无 ellipsis 且溢出真可见）", not cl_bad,
                "%d 屏 0 裁字" % NSCREENS if not cl_bad
                else "; ".join(str(x) for x in cl_bad)[:400])
            add("真视口未被冒充（innerWidth 精确）", not vp_bad,
                "375/768/1440 精确" if not vp_bad
                else "; ".join(str(x) for x in vp_bad)[:200])
            add("每屏都走到真数据态（只读加载成功）", not ld_bad,
                "复盘台读到引擎真账本" if not ld_bad
                else "; ".join(str(x) for x in ld_bad)[:200])
            ma = walk(br, base, ["over"], [1440], anchor="</body>",
                      repl="<style>#cmdk{position:absolute;right:8px;top:6px}"
                           "</style></body>",
                      label="MA 按钮脱离文档流压住 tab")
            add("上牙 MA：脱离文档流的按钮压住 nav tab 会被抓住",
                any(x[3] == "overlap" for x in ma),
                "MA findings=%d 首项=%s" % (len(ma), ma[0] if ma else None))
            mb = walk(br, base, ["retro"], [375], anchor="</body>",
                      repl="<style>.glist .gw b{overflow-wrap:normal}</style></body>",
                      label="MB 长 token 不换行")
            add("上牙 MB：长 token 不换行被裁会被抓住",
                any(x[3] == "clip" for x in mb),
                "MB findings=%d 首项=%s" % (len(mb), mb[0] if mb else None))
            ex = walk(br, base, VIEWS, WIDTHS, label="EXPANDED details 全展开",
                      expand=True)
            ex_ov = [x for x in ex if x[3] == "overlap"]
            ex_cl = [x for x in ex if x[3] == "clip"]
            ex_vp = [x for x in ex if x[3] == "viewport"]
            ex_ld = [x for x in ex if x[3] == "load"]
            add("details 全展开后同样无压字/无裁字（不靠排除蒙混过关）",
                not (ex_ov or ex_cl or ex_vp or ex_ld),
                "%d 屏 clean" % NSCREENS if not (ex_ov or ex_cl)
                else "; ".join(str(x) for x in (ex_ov + ex_cl))[:400])
            br.close()
    finally:
        try:
            srv.shutdown()
        except Exception:                                    # noqa: BLE001
            pass
    add("反向门零污染（活 index.html sha 未变）", sha_file(HTML) == live_sha,
        "sha=%s" % live_sha[:16])
    nfail = sum(1 for v in G.values() if not v)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    rep = {"tool": "layout_gate.py", "html_sha256": live_sha[:16],
           "views": VIEWS, "widths": WIDTHS, "screens": NSCREENS * 2,
           "gates": [{"gate": k, "ok": v, "why": WHY.get(k, "")} for k, v in G.items()],
           "ctrl_findings": ctrl}
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("== LAYOUT_GATE %s  %d/%d 门通过 ==" % ("OK" if nfail == 0 else "FAIL",
                                                   len(G) - nfail, len(G)))
    return 0 if nfail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
