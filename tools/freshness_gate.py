#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据时效硬门：顶栏「PASS + LIVE」不得冒充「刚验过」（D41）。

为什么单独一个门：D40 走查抓到真缺陷 —— 上游 verdicts.json 停在 10/01 15:31
已 26 小时没重跑，驾驶舱 data.json 每轮照常重建，顶栏还是绿色 PASS、SSE 一连上
就显示 LIVE。操作员完全看不出「这是昨天验的」。本门把这条钉死。

双向断言（能同时抓「永远显示」和「永远不显示」两种空壳）：
   · 26 小时前的证据 -> 页面必须显示陈旧态 + 醒目横幅
   · 5 分钟前的证据 -> 页面必须显示新鲜态 且 横幅必须收起
   · generated_at 缺失 / 不可解析 -> 一律判 unknown（fail-closed），绝不判新鲜
   · LIVE 字样不得再出现（连接态与新鲜度必须分开表述）

变异体反向（门必须带牙）：把判定逻辑换成 4 种空壳实现，门都要真红。
产出 work/freshness.json。rc=0 全过 / 1 有红项 / 2 用法或文件错。
"""
import json
import os
import re
import sys
import time
import tempfile
from datetime import datetime, timedelta, timezone

HERE0 = os.path.dirname(os.path.abspath(__file__))
if HERE0 not in sys.path:
    sys.path.insert(0, HERE0)
from variant_pool import map_cases  # noqa: E402  （D73 并发执行器）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
DATA = os.path.join(ROOT, "data.json")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAY_RE = re.compile(
    r'(<script id="payload" type="application/json">)(.*?)(</script>)', re.S)

results = []
FV_ALL = ("新鲜", "偏旧", "陈旧", "未知")
FV_MAP = {"fresh": "新鲜", "amber": "偏旧", "stale": "陈旧",
          "unknown": "未知"}


def vtag_violations(cases):
    """顶栏 verdict 徽章的三条规则（双向），返回违规键列表。
    D86：正向断言和变异体网**必须共用这一个函数**。若变异体自己另写一份
    「怎么算违规」，就会出现「门红但网说没抓住」的二义 —— 那种网等于没有网。
    规则：
      ① 非新鲜 → 不得裸奔（须 HIST 降级 + 带时效限定词）
      ② 限定词必须与该用例自己判出的 want_f 同源（不另写映射表）
      ③ 新鲜 → 必须就是裸 verdict（别把诚实做成永远灰）
    """
    bad = {"bare": [], "qual": [], "fresh": []}
    for k, d in cases.items():
        vt = d.get("vtagText") or ""
        vc = d.get("vtagCls") or ""
        want = d["want_f"]
        if want == "fresh":
            if "HIST" in vc or vt != BASE_DATA["global_verdict"]:
                bad["fresh"].append(k)
        else:
            if vt == BASE_DATA["global_verdict"] or "HIST" not in vc:
                bad["bare"].append(k)
            elif FV_MAP[want] not in vt:
                bad["qual"].append(k)
    return bad

# --- FLAKE-RETRY（与 act_gate.py 同一语义，不另立一套） ---
# 实测依据（D41）：本门单独跑 8/8 绿，串进全链跑到第 4 个变异体时 Chrome 挂了
#   playwright._impl._errors.TargetClosedError: ... browser has been closed
# 这是**量具**故障（浏览器进程没了），不是判据红。所以只对基础设施故障重试，
# 判据红一次都不许重试 —— 否则「永远红」会被洗成绿，门就成空壳。
_INFRA_MARKS = (
    "TargetClosedError", "Browser closed", "Connection closed",
    "Target page, context or browser has been closed",
    "net::ERR_CONNECTION_REFUSED", "net::ERR_ABORTED", "Protocol error",
)
RETRY_SLEEP_S = 2.0
RETRY_N = 2  # 首次 + 2 次重试 = 最多 3 次尝试
RETRY_TRACE = []


def _is_infra(exc):
    """只认基础设施层故障：口径固定为「异常类型 + 消息」，不做更宽的模糊匹配。"""
    txt = "%s: %s" % (type(exc).__name__, exc)
    return any(m in txt for m in _INFRA_MARKS)


def run_gate(fn, ctx, name=""):
    """跑一条门；基础设施故障才重试，判据红一律原样判红。重试必须留痕。"""
    last = None
    for attempt in range(RETRY_N + 1):
        try:
            ok, detail = fn(ctx)
            if attempt:
                detail = "重试×%d 后过 | %s" % (attempt, detail)
                RETRY_TRACE.append("%s 重试×%d" % (name, attempt))
            return bool(ok), detail
        except Exception as e:
            if not _is_infra(e) or attempt == RETRY_N:
                return False, "EXC %s: %s" % (type(e).__name__, str(e)[:140])
            last = e
            RETRY_TRACE.append("%s 基建抖动 %s -> 重试×%d"
                               % (name, type(e).__name__, attempt + 1))
            time.sleep(RETRY_SLEEP_S * (attempt + 1))
    return False, ("EXC %s: %s" % (type(last).__name__, last)) if last else "EXC 未知"


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    return bool(ok)


def iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))\
        .astimezone().isoformat()


def build_html(base_html, base_data, generated_at, drop=False):
    """把真实 index.html 的内联载荷换成受控 D，其余代码一字不改。"""
    d = json.loads(json.dumps(base_data))
    if drop:
        d.pop("generated_at", None)
    else:
        d["generated_at"] = generated_at
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    return PAY_RE.sub(lambda m: m.group(1) + payload + m.group(3), base_html, count=1)


PROBE = """() => {
  const b = document.querySelector('#fresh .fresh');
  const bn = document.getElementById('sbn');
  const s1 = document.getElementById('sbn1');
  const s2 = document.getElementById('sbn2');
  const lv = document.getElementById('live');
  const ft = document.getElementById('strip');
  /* D86：顶栏 verdict 徽章本身。它曾经只读 global_verdict、完全不看时效，
     于是 3 天前的 PASS 顶栏照样纯绿，「陈旧」只在 200px 外的横幅里 ——
     视觉权重压不住。探针必须能看见它，否则门看不见这个缺陷。 */
  const vt = document.querySelector('#vtag .vtag');
  return {
    badge: !!b, badgeState: b ? b.getAttribute('data-f') : null,
    badgeText: b ? b.innerText.trim() : '',
    bannerOn: bn ? bn.classList.contains('on') : false,
    bannerVisible: bn ? (getComputedStyle(bn).display !== 'none') : false,
    banner1: s1 ? s1.textContent.trim() : '',
    banner2: s2 ? s2.innerText.trim() : '',
    liveText: lv ? lv.textContent.trim() : '',
    foot: ft ? ft.innerText.trim() : '',
    vtag: !!vt,
    vtagCls: vt ? vt.className : '',
    vtagText: vt ? vt.innerText.trim() : '',
    err: window.__err || null,
  };
}"""


CASES = [
    # (键, 多少分钟前的 generated_at（None=缺失）, 期望 data-f, 期望横幅开)
    # 阈值两侧留 1 分钟余量：边界值本身没法测 —— 造完时间戳到页面 evaluate 之间
    # 会过去几秒，正好 30min 的用例必然翻面（实测 rc=1 就是这么红的）。
    # 所以边界用 29/359/361 卡「内侧/外侧」，阈值本身仍是代码里的常量。
    # D73：时间戳**不再在 import 时定死**，而是每个用例在 set_content 前现算。
    # 否则整门跑得越久，29min/359min 这两个「内侧」用例越可能跨过 30min/360min
    # 变成合法外侧 —— 表现为「变异体命中数随运行时长漂移」（实测串行两次跑出
    # N2=3例 / N2=1例两种结果，与并发无关）。现算之后 1 分钟余量是相对
    # evaluate 时刻的，命中数与运行时长、并发度都无关。
    ("fresh_5min", 5, "fresh", False),
    ("fresh_inner_29min", 29, "fresh", False),
    ("amber_2h", 120, "amber", True),
    ("amber_inner_359min", 359, "amber", True),
    ("stale_outer_361min", 361, "stale", True),
    ("stale_26h", 26 * 60, "stale", True),
    ("stale_3d", 3 * 24 * 60, "stale", True),
    ("missing", None, "unknown", True),
]


def run_cases(page_factory, html, cases):
    out = {}
    for key, mins, want_f, want_bn in cases:
        gen = None if mins is None else iso(mins)   # 现算，别让运行时长偷走余量
        drop = gen is None
        page = page_factory()
        page.set_content(build_html(html, BASE_DATA, gen, drop=drop))
        page.wait_for_timeout(160)
        out[key] = page.evaluate(PROBE)
        out[key]["want_f"] = want_f
        out[key]["want_banner"] = want_bn
        page.close()
    return out


def garbage_case(page_factory, html):
    page = page_factory()
    page.set_content(build_html(html, BASE_DATA, "not-a-date"))
    page.wait_for_timeout(160)
    d = page.evaluate(PROBE)
    page.close()
    d["want_f"] = "unknown"
    d["want_banner"] = True
    return "garbage_ts", d


TP = """() => { const b=document.querySelector('#fresh .fresh');
  const vt=document.querySelector('#vtag .vtag');
  return {state:b&&b.getAttribute('data-f'), text:b&&b.innerText.trim(),
    banner:getComputedStyle(document.getElementById('sbn')).display,
    foot:(document.getElementById('ftfresh')||{}).innerText||'',
    vtagCls:vt?vt.className:'', vtagText:vt?vt.innerText.trim():''}; }"""

# 定时器量具：set_content() 不触发 add_init_script（实测 window.__iv 恒 undefined），
# 所以在 <head> 之后原页面脚本之前插一段同页 shim —— 记录真实 setInterval 周期。
IV_SHIM = ("<script>window.__iv=[];(function(s){window.setInterval="
          "function(f,ms){window.__iv.push(ms);return s.apply(this,arguments);};})"
          "(window.setInterval);</script>")


def with_iv_shim(html_src):
    m = re.search(r"<head[^>]*>", html_src, re.I)
    if not m:
        raise RuntimeError("index.html 里找不到 <head>，无法插定时器 shim")
    return html_src[:m.end()] + IV_SHIM + html_src[m.end():]


def tick_probe(html_src, browser=None):
    """在浏览器里跑轮询 5 用例。正向阶段和变异体阶段共用同一组探针 ——
    少这条共用，N1/N2 这类只伤轮询的变异体就会从变异体网里漏出去。"""
    out = {}
    own = browser is None
    pw = None
    if own:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(executable_path=CHROME)
    br = browser
    if True:
        try:
            def mk():
                pg = br.new_page(viewport={"width": 1440, "height": 900})
                pg.set_content(with_iv_shim(html_src))
                pg.wait_for_timeout(500)
                return pg

            pg = mk()
            pg.evaluate("()=>freshTick()")
            out["t1"] = pg.evaluate(TP)
            pg.close()
            pg = mk()
            pg.evaluate("()=>{window.__r=Date.now();Date.now=()=>window.__r+26*3600*1000;}")
            pg.evaluate("()=>freshTick()")
            out["t2"] = pg.evaluate(TP)
            pg.close()
            pg = mk()
            pg.evaluate("()=>{window.__r=Date.now();Date.now=()=>window.__r-26*3600*1000;}")
            pg.evaluate("()=>freshTick()")
            out["t3"] = pg.evaluate(TP)
            pg.close()
            pg = mk()
            out["t4"] = {"iv": pg.evaluate("()=>window.__iv||[]")}
            pg.close()
            pg = mk()
            pg.evaluate("()=>document.getElementById('t-mx').focus()")
            n0 = pg.evaluate("()=>document.getElementById('main').querySelectorAll('*').length")
            pg.evaluate("()=>freshTick()")
            n1 = pg.evaluate("()=>document.getElementById('main').querySelectorAll('*').length")
            out["t5"] = {"n0": n0, "n1": n1,
                         "active": pg.evaluate("()=>document.activeElement&&document.activeElement.id")}
            pg.close()
        finally:
            if own:
                try:
                    br.close()
                except Exception:
                    pass
                try:
                    pw.stop()
                except Exception:
                    pass
    return out


def main():
    global BASE_DATA
    for f in (HTML, DATA):
        if not os.path.exists(f):
            print("FAIL: 缺文件 %s" % f)
            return 2
    html = open(HTML, encoding="utf-8").read()
    BASE_DATA = json.load(open(DATA, encoding="utf-8"))
    if not PAY_RE.search(html):
        print("FAIL: index.html 里找不到 id=payload 的内联载荷块")
        return 2
    if "FRESH_MIN" not in html or "function freshness" not in html:
        print("FAIL: index.html 里没有 freshness()/FRESH_MIN —— 时效逻辑不在盘上")
        return 2

    report = {"cases": {}, "mutants": {}}
    # 每个浏览器阶段自成一体：浏览器进程死了就整个阶段重来（run_gate 只对
    # 基建故障重试，判据红一律原样判红）。
    def phase_live(_ctx):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = p.chromium.launch(executable_path=CHROME)
            try:
                def page_factory():
                    pg = br.new_page(viewport={"width": 1440, "height": 900})
                    pg.on("pageerror", lambda e: None)
                    return pg
                out = run_cases(page_factory, html, CASES)
                out.update(dict([garbage_case(page_factory, html)]))
                return True, out
            finally:
                try:
                    br.close()
                except Exception:
                    pass

    ok, live = run_gate(phase_live, None, "8 用例实测")
    if not ok:
        print("FAIL: 正向用例阶段不可用：%s" % live)
        with open(os.path.join(ROOT, "work", "freshness.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"summary": {"ok": 0, "total": len(results) + 1},
                       "retry_trace": RETRY_TRACE}, f, ensure_ascii=False, indent=1)
        print("FRESHNESS_GATE RED 0/%d" % (len(results) + 1))
        return 1
    report["cases"] = live

    bad_state = [k for k, d in live.items()
                 if d["badgeState"] != d["want_f"]]
    gate("时效双向判对（8 用例含阈值内外 3 例）", not bad_state,
         "5/29min->fresh 2h/359min->amber 361min/26h/3d->stale "
         "缺/坏时间戳->unknown" if not bad_state else "判错=" + ",".join(bad_state))

    bad_bn = [k for k, d in live.items() if d["bannerOn"] != d["want_banner"]
              or d["bannerVisible"] != d["want_banner"]]
    gate("横幅只在非新鲜时出现（收起也是断言）", not bad_bn,
         "新鲜 2 例横幅 display:none；其余 6 例 role=alert 可见"
         if not bad_bn else "横幅状态错=" + ",".join(bad_bn))

    s = live["stale_26h"]
    has_hist = ("历史结论" in s["banner2"]) and ("未复跑" in s["banner1"])
    gate("陈旧横幅写明「PASS 是历史结论」", has_hist,
         "b1=%r" % s["banner1"] if has_hist
         else "b1=%r b2=%r" % (s["banner1"], s["banner2"][:80]))

    u = live["missing"]
    gate("generated_at 缺失 -> fail-closed 判 unknown",
         u["badgeState"] == "unknown" and u["bannerOn"],
         "badge=%s banner=%s" % (u["badgeState"], u["bannerOn"]))

    lv_bad = [k for k, d in live.items() if d["liveText"].strip() == "LIVE"]
    gate("LIVE 字样不得再出现（连接态≠新鲜度）", not lv_bad,
         "5 例徽章均为「静态」；http 下文案=已连接" if not lv_bad
         else "仍显示 LIVE=" + ",".join(lv_bad))

    foot_bad = [k for k, d in live.items()
                if ("证据" not in d["foot"])
                or not any(s in d["foot"] for s in FV_ALL)]
    gate("底栏也带证据年龄", not foot_bad,
         "底栏含「证据 <态> <时长>」四态齐全" if not foot_bad
         else "底栏缺=" + ",".join(foot_bad))

    # ── D86：顶栏 verdict 徽章自己必须带时效限定词 ──────────────
    # 这条不能靠「横幅已经写了陈旧」来顶替。D86 的真缺陷就是：横幅正确、
    # 徽章也正确地显示 PASS，但徽章自己是纯绿无限定词，而它才是第一眼看到
    # 的东西。断言必须直接落在 #vtag 上，否则这个缺陷下次照样能溜过去。
    # 非新鲜时：徽章不得是裸 verdict，且必须带 HIST 降级 + 时效字样。
    nonfresh = [k for k, d in live.items() if d["want_f"] != "fresh"]
    vb = vtag_violations(live)
    bare = vb["bare"]
    gate("非新鲜时顶栏 PASS 不得裸奔（须降级 HIST + 带时效限定词）", not bare,
         "%d 例非新鲜顶栏均带 HIST 与时效字样，如 %r"
         % (len(nonfresh), live["stale_3d"]["vtagText"])
         if not bare else "裸 verdict/未降级=" + ",".join(bare))

    # 限定词必须与该用例自己判出的状态同源：want_f=stale 就要出现「陈旧」，
    # want_f=unknown 就要出现「未知」。直接从用例的 want_f 推，不另写一张表 ——
    # 另写一张表就会和 FRESH_MIN/FRESH_MAX 的改动悄悄对不上。
    qual_bad = vb["qual"]
    gate("顶栏徽章的时效字样与实际状态一致", not qual_bad,
         "偏旧/陈旧/未知 四类限定词与判定同源"
         if not qual_bad else "限定词错=" + ",".join(qual_bad))

    fresh_vt = [k for k, d in live.items() if d["want_f"] == "fresh"]
    fresh_bad = vb["fresh"]
    gate("新鲜时顶栏就是裸 verdict（双向：别把诚实做成永远灰）", not fresh_bad,
         "%d 例新鲜顶栏为纯 %r" % (len(fresh_vt), BASE_DATA["global_verdict"])
         if not fresh_bad else "新鲜却降级=" + ",".join(fresh_bad))

    errs = [k for k, d in live.items() if d["err"]]
    gate("8 用例 0 JS 报错", not errs,
         "0 pageerror" if not errs else "err=" + ",".join(errs))

    # ── 变异体反向：门必须带牙 ──────────────────────────────
    # 轮询类变异体必须用「新鲜」载荷喂探针：真实 payload 本来就陈旧，
    # 底栏开机即写「陈旧」，N2 这种「tick 不更新底栏」的变异就抓不到（量具自身要成立）。
    fresh_fixture = build_html(html, BASE_DATA, iso(4))
    MUTANTS = [
        ("M1 永远判新鲜（横幅永不出现）",
         "const f=ms<=FRESH_MIN*60000?'fresh':(ms<=FRESH_MAX*60000?'amber':'stale');",
         "const f='fresh';"),
        ("M2 永远判陈旧",
         "const f=ms<=FRESH_MIN*60000?'fresh':(ms<=FRESH_MAX*60000?'amber':'stale');",
         "const f='stale';"),
        ("M3 fail-open（坏时间戳当新鲜）",
         "if(!raw||isNaN(t)){",
         "if(false){"),
        ("M4 横幅永不显示（徽章活、横幅死）",
         "b.classList.add('on');",
         "b.classList.remove('on');"),
        ("N1 定时器未注册（页面开着就永远新鲜）",
         "window.__freshTimer=setInterval(freshTick,FRESH_TICK_MS);",
         "window.__freshTimer=null;"),
        ("N2 tick 不更新底栏时效",
         "function freshTick(){ freshness(); freshnessFoot(); verdictTag(); }",
         "function freshTick(){ freshness(); verdictTag(); }"),
        ("N3 tick 强关横幅（徽章陈旧但不报警）",
         "function freshTick(){ freshness(); freshnessFoot(); verdictTag(); }",
         "function freshTick(){ freshness(); freshnessFoot(); verdictTag();"
         " const _b=document.getElementById('sbn');"
         " if(_b) _b.style.display='none'; }"),
        # ── D86 两个新变异体：专打顶栏 verdict 徽章 ─────────────
        # V1 = 把 verdictTag 的时效分支删掉（退回 D86 之前那版裸彩色 PASS）。
        #     这是 D86 真缺陷的本体，必须证明新断言能抓住它。
        ("V1 verdictTag 不看时效（恒裸彩色 PASS）",
         "if(a.f==='fresh'){\n    el.innerHTML='<span class=\"vtag '+cls(gv)+'\">'+esc(gv)+'</span>';\n  }else{",
         "if(true){\n    el.innerHTML='<span class=\"vtag '+cls(gv)+'\">'+esc(gv)+'</span>';\n  }else{"),
        # V2 = verdictTag 不进 freshTick（开机对、页面开着就永远裸奔）。
        #     纯 CASES 量具抓不到它 —— 开机那一刻它是正确的，必须靠 tick 探针：
        #     用新鲜载荷起页面 → 把时钟推进 26h → 只调 freshTick() → 看 #vtag 有没有翻面。
        ("V2 verdictTag 不进 freshTick（页面开着永远裸奔）",
         "function freshTick(){ freshness(); freshnessFoot(); verdictTag(); }",
         "function freshTick(){ freshness(); freshnessFoot(); }"),
    ]
    caught, drifted = [], []

    def _run_mutant(i, mut):
        """一个变异体：自己一份浏览器、自己一份内存 HTML 副本。
        D73：这 9 个变异体互不相干（注入都在内存副本上，零落盘、零共享写目标），
        串行只是没人把它们放一起。worker 内禁止 print，只返回 (行块, 结构化结论)。"""
        mname, old, new = mut
        if old not in html:
            return (["  [FAIL] %s  变异标记漂移（源码已变，变异体没注入）" % mname],
                    {"mname": mname, "drift": True, "caught": False, "hit": []})
        from playwright.sync_api import sync_playwright
        lines = []
        with sync_playwright() as p:
            br = p.chromium.launch(executable_path=CHROME)
            try:
                def pf2():
                    return br.new_page(viewport={"width": 1440, "height": 900})

                mhtml = html.replace(old, new, 1)
                sub = run_cases(pf2, mhtml, CASES)
                sub.update(dict([garbage_case(pf2, mhtml)]))
                hit = [k for k, d in sub.items()
                       if d["badgeState"] != d["want_f"]
                       or d["bannerOn"] != d["want_banner"]]
                # D86：顶栏徽章用**与正向断言同一个** vtag_violations() 判。
                # 若这里另写一份「怎么算违规」，V1 这类变异体就会从网里漏出去，
                # 而门正文却报红 —— 门和网不一致 = 网不存在。
                for k in vtag_violations(sub):
                    hit.append("vtag:" + k)
                if mname[0] in "NV":
                    # 只伤轮询的变异体必须过 tick 探针，否则会从网里漏出去
                    tt = tick_probe(fresh_fixture.replace(old, new, 1), browser=br)
                    if "t1" not in tt:
                        hit.append("tick_probe_基建故障")
                    else:
                        if mname.startswith("N1") and \
                           30000 not in (tt["t4"]["iv"] or []):
                            hit.append("t4:定时器未注册")
                        if mname.startswith("N2") and \
                           "陈旧" not in tt["t2"]["foot"]:
                            hit.append("t2:底栏未跟陈旧翻面")
                        if mname.startswith("N3") and \
                           tt["t2"]["banner"] != "flex":
                            hit.append("t2:横幅被强关")
                        # V2：推进 26h 后只调 freshTick()，#vtag 必须自己翻成
                        # HIST 降级。若它还停在裸 PASS，就是「开机对、开着烂」。
                        if mname.startswith("V2") and \
                           ("HIST" not in (tt["t2"]["vtagCls"] or "")
                            or "陈旧" not in (tt["t2"]["vtagText"] or "")):
                            hit.append("t2:顶栏未随 tick 降级 vtag=%r"
                                       % (tt["t2"]["vtagText"],))
            finally:
                try:
                    br.close()
                except Exception:
                    pass
        if hit:
            lines.append("  [ok  ] 变异体被抓 %s  被 %d 例命中" % (mname, len(hit)))
        else:
            lines.append("  [FAIL] 变异体漏网 %s  —— 门是空壳" % mname)
        return lines, {"mname": mname, "drift": False, "caught": bool(hit), "hit": hit}

    def phase_mut(_ctx):
        caught_l, drifted_l = [], []
        vd = [None] * len(MUTANTS)
        # map_cases 按原始顺序展平，所以输出与串行逐字节可比。
        def _job(i, mut):
            blk, verdict = _run_mutant(i, mut)
            vd[i] = verdict
            return blk
        for ln in map_cases(MUTANTS, _job, header=lambda i, c: c[0][:2]):
            print(ln)
        for v in vd:
            if v is None:
                continue
            if v["drift"]:
                drifted_l.append(v["mname"])
                report["mutants"][v["mname"]] = "MARKER_DRIFT"
            else:
                report["mutants"][v["mname"]] = {"caught_by": v["hit"]}
                if v["caught"]:
                    caught_l.append(v["mname"])
        return True, (caught_l, drifted_l)

    mok, mout = run_gate(phase_mut, None, "%d 变异体反向" % len(MUTANTS))
    if not mok:
        print("FAIL: 变异体阶段不可用：%s" % mout)
        gate("%d 个变异体全部被抓（门有牙）" % len(MUTANTS), False,
             "阶段基建故障：%s" % mout)
    else:
        caught, drifted = mout

    missed = [m[0] for m in MUTANTS if m[0] not in caught]
    gate("%d 个变异体全部被抓（门有牙）" % len(MUTANTS),
         not missed and not drifted,
         "M1-M4 判定空壳 + N1-N3 轮询空壳 + V1/V2 顶栏裸奔 共 %d/%d 被真红命中"
         % (len(caught), len(MUTANTS))
         if not missed and not drifted
         else "漏网=" + ",".join(missed) + (" 漂移=" + ",".join(drifted) if drifted else ""))

    # ── D41 续：徽章轮询（「一直开着」不得永远显示新鲜） ──────────
    # 取证：fresh 数据 + 假时钟推 26h，evidenceAge() 已判 stale，
    # 而 DOM 仍是 fresh、横幅 display:none —— 徽章只在 boot() 算一次就会一直撒谎。
    fresh_html = build_html(html, BASE_DATA, iso(4))

    def phase_tick(_ctx):
        return True, tick_probe(fresh_html)

    tok, tk = run_gate(phase_tick, None, "轮询 5 用例")
    if not tok:
        gate("徽章轮询 5 用例", False, "阶段基建故障：%s" % tk)
    else:
        t2, t3 = tk["t2"], tk["t3"]
        gate("轮询把 fresh 推成 stale（缺陷回归）",
             t2["state"] == "stale" and t2["banner"] == "flex"
             and "陈旧" in t2["foot"],
             "推 26h 后 badge=%s banner=%s foot=%r"
             % (t2["state"], t2["banner"], t2["foot"]))
        gate("轮询反向把 stale 收回 fresh（双向）",
             t3["state"] == "fresh" and t3["banner"] == "none"
             and "新鲜" in t3["foot"],
             "回拨后 badge=%s banner=%s foot=%r"
             % (t3["state"], t3["banner"], t3["foot"]))
        gate("新鲜态轮询不误报", tk["t1"]["state"] == "fresh"
             and tk["t1"]["banner"] == "none",
             "fresh 保持 fresh，横幅不弹")
        t4 = tk["t4"]
        gate("轮询定时器真注册且周期=30s",
             30000 in (t4["iv"] or []),
             "setInterval 周期记录=%s" % (t4["iv"],))
        t5 = tk["t5"]
        gate("轮询不重建视图、不丢焦点",
             t5["n0"] == t5["n1"] and t5["active"] == "t-mx",
             "main 节点 %d->%d，焦点仍在 %s" % (t5["n0"], t5["n1"], t5["active"]))

    n_ok = sum(1 for r in results if r["ok"])
    report["summary"] = {"ok": n_ok, "total": len(results)}
    report["retry_trace"] = list(RETRY_TRACE)
    os.makedirs(os.path.join(ROOT, "work"), exist_ok=True)
    with open(os.path.join(ROOT, "work", "freshness.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    for t in RETRY_TRACE:
        print("  [retry] " + t)
    print("FRESHNESS_GATE %s %d/%d" % ("OK" if n_ok == len(results) else "RED",
                                       n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
