#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D82 复盘台真浏览器门：闭环最后一环「哪里做错了/为什么错/下次改什么」上屏。

为什么必须单独一道门（view_dod / a11y_walk / intent_gate 都判不到的缺陷）：
    * /__recurrence 端点在 D40 之前就已存在，已被 act_gate / resident_gate 断言过，
      后端一直绿 —— 但 index.html 里 0 命中，**交付面死端点**：有门没人看得见。
      intent_gate 当初就是这个形状（数据管线通了、只缺渲染），所以复发面会以同型复发；
    * 屏上七视图闭环只到三环。「哪里做错了/为什么错/下次改什么」在屏上零面，
      用户看不到这个系统会自我诊断，于是「越用越强」只是引擎内部的事，不是产品能力。

判据真源分层（刻意不复刻，也不问被测对象）：
    * 复发簇（阈值 / 各簇 runs / recurring 判定）-> 直接 import 引擎
      evolution_intake 的 load_events / build_clusters / RECURRENCE_MIN，
      门自己算一遍。**不复刻**聚类与阈值，也不复用 /__recurrence 的 parse 路径 ——
      若 ground truth 走同一条路径，一处解析 bug 会同时污染两边、门全绿，
      那是自己给自己出考卷；
    * self 计数与账本 -> 门自己读 COMPANY_MEMORY/postmortem.jsonl 求和
      （postmortem.py 只负责逐 run 落盘，没有聚合函数可复用，故自己聚合）。
    * 核心判据是**屏上那行字 == 引擎此刻真给的值（逐字）**，不是「像不像」。

诚实负例是另一半：postmortem.jsonl 为空时必须 fail-closed 报 BLOCK，
且**不许**画出「没有失败 / 已经在变强」—— 无数据不是零失败。

上牙（四个变异体 + 一个诚实负例，全部走 route 拦 served 字节，
活的 index.html 零写入，跑完用 sha256 自证）：
    M1 抽掉 RLAST=j; rdraw(j);   -> 屏上永远不渲染，门必须红；
    M2 把账本画成空             -> 账本面消失，门必须红；
    M3 recurring 写死 true      -> 非复发簇也被标「未修好」，与引擎判定不符，门必须红；
    M4 抹掉「人类侧全程 0」诚实注记 -> 造斜线的门被拆掉，门必须红；
    N1 postmortem.jsonl 置空    -> 必须 fail-closed BLOCK，且不得出现零失败结论。

用法：python3 tools/retro_gate.py    # rc=0 全过 / 1 有红项 / 2 环境不可用
"""
import hashlib
import io
import importlib.util
import json
import math
import os
import shutil
import signal
import socket
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVE_PY = os.path.join(HERE, "serve.py")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WORK = os.path.join(ROOT, "work")
COMPANY = os.path.expanduser("~/.codex/global-background/company-delivery")
NEG_ROOT = os.path.join(WORK, "d82neg")
FROZEN = os.path.join(WORK, "d82frozen")

G = {}
WHY = {}

# 变异体锚点：必须**恰好命中一次**。命中 0 次说明源码漂移、门自己失效了 ——
# 那比红更坏，所以漂移直接判 FAIL 而不是静默跳过。
ANCHOR_DRAW = "RLAST=j; rdraw(j);"
ANCHOR_LEDGER = "box.innerHTML=rclusters(j)+rself(j)+rledger(j);"
ANCHOR_REC = "const bad=c.recurring?1:0;"
ANCHOR_HONEST = "if(hi!==0||ht!==0) return '';"


def add(name, cond, detail=""):
    try:
        v = bool(cond)
    except Exception as e:                                  # noqa: BLE001
        v = False
        detail = "%s | EXC %r" % (detail, e)
    G[name] = v
    if not v:
        WHY[name] = str(detail)[:400]
    return v


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def norm(s):
    """屏上文本可能因换行与转义与原文差几个空格；比较前只压空白，不动内容。"""
    return " ".join(str(s or "").split())


def ledger_row(r):
    """把一条 postmortem 记录压成屏上该显示的那几段字（与 serve 同字段口径）。"""
    return {
        "ts": r.get("ts") or "",
        "verdict": r.get("verdict") or "UNKNOWN",
        "gap": r.get("capability_gap") or r.get("missing_capability") or "",
        "root_cause": r.get("root_cause") or "",
        "family": r.get("root_cause_family") or "",
        "plan": r.get("suggested_capability") or "",
        "generalizable": bool(r.get("generalizable")),
        "recoverable": r.get("recoverable"),
        "preventable": r.get("preventable"),
    }


def jsround(x):
    """复刻 JS Math.round（.5 向上），Python round 是银行家舍入，用它比会假红。"""
    return int(math.floor(float(x) + 0.5))


def jsnum(x):
    """复刻 JS 数字转字符串：0 -> '0'，0.8 -> '0.8'（不写成 '0.0'）。"""
    f = float(x)
    return str(int(f)) if f == int(f) else ("%g" % f)


def jsbool(v):
    """复刻 index.html 两处口径：tf() 对 null 给 '—'，其余一律交给 String()。

    刻意不写成「非空即 true/false」：recoverable=0 在 JS 里是 '0' 而不是
    'false'，按布尔判会把 0 误报成 false，门就假红了。
    """
    if v is None:
        return "—"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return jsnum(v)
    return str(v)


def meta_map(card):
    """把 .gmeta 里的 <span><b>k</b> v</span> 收成 {k: v}，供逐字比对。"""
    out = {}
    for k, v in card.get("meta", []):
        out[norm(k)] = norm(v)
    return out


def pick_card(dom, word):
    """按卡标题取卡；取不到返回 None（调用方必须判红，不许静默跳过）。"""
    for c in dom.get("cards", []):
        for h in c.get("h", []):
            if word in norm(h[0]):
                return c
    return None


def head_badges(card):
    h = card["h"][0] if card.get("h") else ["", []]
    return norm(h[0]), [norm(b) for b in (h[1] if len(h) > 1 else [])]


def http_get(url, timeout=90):
    def _dec(raw):
        # 静态页不是 JSON。返回 {"raw": ...} 而不是抛 —— 否则「静态页仍能供」
        # 这种探活会因为解析失败炸掉整条门，掩盖真正的红项。
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw[:200]}

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, _dec(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _dec(raw)


def engine_truth(company_root=COMPANY):
    """自己 import 一次引擎 evolution_intake，拿它的原话当 ground truth。

    刻意不复刻聚类与阈值，也不复用 /__recurrence 的 parse 路径 ——
    ground truth 若与被测对象同源，一处 bug 同时污染两边、门全绿。
    """
    path = os.path.join(company_root, "evolution_intake.py")
    spec = importlib.util.spec_from_file_location("_d82_truth_intake", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    ev = m.load_events()
    cl = m.build_clusters(ev)
    thr = int(m.RECURRENCE_MIN)
    rows = []
    for key, c in cl.items():
        n = int(c.get("run_count", 0))
        rows.append({"cluster": key, "runs": n,
                     "family": c.get("family", ""),
                     "events": int(c.get("events", 0)),
                     "last_ts": c.get("last_ts", ""),
                     "plan": c.get("plan", ""),
                     "recurring": n >= thr,
                     "ratio": round(n / float(thr), 2)})
    rows.sort(key=lambda r: (-r["runs"], r["cluster"]))
    # self 计数 / 账本：门自己读 postmortem.jsonl 聚合（postmortem.py 只逐 run
    # 落盘、没有聚合函数可复用）。同样不问 serve.py。
    pm = os.path.join(m.MEM, "postmortem.jsonl")
    raw = read_jsonl(pm)
    def s(k):
        return sum(int(r[k]) for r in raw
                   if isinstance(r.get(k), (int, float)))
    verdicts = {}
    for r in raw:
        v = r.get("verdict") or "UNKNOWN"
        verdicts[v] = verdicts.get(v, 0) + 1
    bk = {}
    for r in raw:
        d = (r.get("ts") or "")[:10]
        if len(d) != 10:
            continue
        b = bk.setdefault(d, {"day": d, "n": 0, "ver": 0, "rep": 0, "rec": 0})
        b["n"] += 1
        for src, dst in (("agent_self_verifications", "ver"),
                         ("agent_self_repairs", "rep"),
                         ("agent_self_recoveries", "rec")):
            v = r.get(src)
            b[dst] += int(v) if isinstance(v, (int, float)) else 0
    newest = None
    for r in reversed(raw):
        newest = {
            "ts": r.get("ts") or "",
            "verdict": r.get("verdict") or "UNKNOWN",
            "gap": r.get("capability_gap") or r.get("missing_capability") or "",
            "root_cause": r.get("root_cause") or "",
            "family": r.get("root_cause_family") or "",
            "plan": r.get("suggested_capability") or "",
        }
        break
    led = []
    for r in reversed(raw):
        led.append(ledger_row(r))
    return {"threshold": thr, "n_events": len(ev), "n_clusters": len(rows),
            "n_recurring": sum(1 for r in rows if r["recurring"]),
            "clusters": rows, "shown": min(8, len(rows)), "ledger": led[:14],
            "self": {"runs": len(raw),
                     "agent_self_verifications": s("agent_self_verifications"),
                     "agent_self_repairs": s("agent_self_repairs"),
                     "agent_self_recoveries": s("agent_self_recoveries"),
                     "human_interventions": s("human_interventions"),
                     "human_takeovers": s("human_takeovers"),
                     "n_generalizable": sum(1 for r in raw
                                            if r.get("generalizable")),
                     "n_with_plan": sum(1 for r in raw
                                        if r.get("suggested_capability")),
                     "n_family_known": sum(
                         1 for r in raw
                         if (r.get("root_cause_family") or "UNKNOWN_SEMANTIC")
                         != "UNKNOWN_SEMANTIC"),
                     "verdicts": verdicts},
            "trend": sorted(bk.values(), key=lambda x: x["day"]),
            "newest": newest, "postmortem": pm, "mem": m.MEM}


def load_serve(path, tag):
    spec = importlib.util.spec_from_file_location(tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def patch_serve(mod):
    """浏览器关掉 SSE 会话时 socketserver 会甩一整页 traceback，淹掉门的判据
    输出（下游靠输出认红门归属）。纯噪声，就地咽掉。"""
    _handle = mod.Handler.handle

    def _safe_handle(self):
        try:
            _handle(self)
        except (ConnectionResetError, BrokenPipeError):
            pass

    mod.Handler.handle = _safe_handle
    return mod


def kill_current(mod):
    try:
        with mod._act_proc["lock"]:
            p = mod._act_proc["p"]
        if p is not None and p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            return True
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    return False


def start_server(port, mod):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    return srv


def make_killer(anchor, replacement):
    """拦 served 的 index.html 字节做变异，活的 index.html 零写入。"""
    def killer(route):
        resp = route.fetch()
        body = resp.body().decode("utf-8", "replace")
        if anchor not in body:
            route.fulfill(response=resp)
            return
        route.fulfill(response=resp,
                      body=body.replace(anchor, replacement, 1).encode("utf-8"))
    return killer


def new_page(br, url, anchor=None, repl=None, errs=None):
    """每个场景一张干净页。需要变异体时走 route 拦 served 字节（活页零写入）。"""
    ctx = br.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    if anchor is not None:
        pg.route("**/index.html", make_killer(anchor, repl))
    if errs is not None:
        pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(url, wait_until="load")
    return ctx, pg


READ_RETRO_JS = r"""() => {
  const q = s => document.querySelector(s);
  const txt = e => (e ? (e.textContent || '').trim() : null);
  const box = q('#rtbox');
  const out = {};
  out.exists = !!box;
  out.empty = box ? ((box.innerHTML || '').trim() === '') : true;
  out.text = box ? (box.innerText || box.textContent || '') : '';
  out.nCards = document.querySelectorAll('#rtbox .gcard').length;
  out.cards = [];
  document.querySelectorAll('#rtbox .gcard').forEach(c => {
    const t = [];
    c.querySelectorAll('h4').forEach(h => {
      const tags = [];
      h.querySelectorAll('.gst').forEach(g => tags.push(txt(g)));
      const cl = h.cloneNode(true);
      cl.querySelectorAll('.gst').forEach(g => g.remove());
      t.push([txt(cl) || '', tags]);
    });
    const meta = [];
    c.querySelectorAll('.gmeta > span').forEach(s => {
      const b = s.querySelector('b');
      meta.push([b ? txt(b) : '', txt(s).replace(b ? txt(b) : '', '').trim()]);
    });
    const lis = [];
    c.querySelectorAll('.glist > li').forEach(li => {
      const gs = [];
      li.querySelectorAll('.gst').forEach(g => gs.push(txt(g)));
      const gn = txt(li.querySelector('.gn')) || '';
      const b = txt(li.querySelector('.gw > b')) || '';
      const whys = [];
      li.querySelectorAll('.gwhy').forEach(w => whys.push(txt(w)));
      lis.push({ok: li.getAttribute('data-ok'), gn: gn,
                badges: gs, head: b, whys: whys});
    });
    const rows = [];
    c.querySelectorAll('.gtbl tbody tr').forEach(tr => {
      const cs = [];
      tr.querySelectorAll('td').forEach(td => cs.push(txt(td)));
      rows.push(cs);
    });
    const heads = [];
    c.querySelectorAll('.gtbl thead th').forEach(th => heads.push(txt(th)));
    out.cards.push({cls: c.className || '', h: t, meta: meta,
            lis: lis, rows: rows, heads: heads,
      note: txt(c.querySelector('.note'))});
  });
  out.hint = txt(q('#rthint'));
  return out; }"""


# 就绪判据用「按钮 disabled 变 true 再变回 false」，而不是等某个卡出现 ——
# M1 恰恰是「卡永远不出现」，用卡做就绪判据会让变异体靠超时红，那是运气不是判据。
# D98 (2026-10-05): completion signal replaces the busy window.
# Old: wait busy (#rtgo disabled) then idle. But the busy window equals ONE
# local fetch('/__recurrence'), often single-digit ms. wait_for_function polls
# on RAF, so if the fetch finishes between click and first poll, busy is never
# observed -> 20s timeout -> false red.
# Measured 3 consecutive runs: 2 red, and both reds landed on MUTANTS (M2/M4),
# so "mutation caught" and "page hung" collapsed into the same red line. The
# gate's teeth were fake: nobody could tell which cause produced the red.
# Now: wait for COMPLETION -- #rtbox non-empty (rdraw is its only writer) and
# button re-enabled. Still pure DOM, never the page's own variables, same
# discipline as this gate's "only really-drawn DOM text counts".
# Teeth unchanged: M1 strips rdraw -> rtbox stays empty -> still red. M2/M3/M4
# do fill rtbox -> DOM criteria catch them, now red because actually caught.
READY_JS = """() => {
  const box=document.querySelector('#rtbox'), b=document.querySelector('#rtgo');
  return !!(box && (box.textContent||'').trim().length>0 && b && !b.disabled); }"""


def run_page(br, url, anchor=None, repl=None):
    """开一页 -> 切到复盘台 -> 真读一次账本 -> 只读 DOM 回来。

    刻意**不读** window.__agentos.retroLast()：那是页面自己的变量，变量在手就能
    骗人 —— 把 rdraw 抽掉后 RLAST 照样被赋值，读变量会全绿。只有 DOM 里真画出
    来的字才算数，这也是 M1 能被抓住的唯一原因。
    """
    errs = []
    ctx, pg = new_page(br, url, anchor, repl, errs)
    try:
        pg.click("#t-retro")
        pg.wait_for_selector("#rtgo", state="visible", timeout=20000)
        pg.click("#rtgo")
        pg.wait_for_function(READY_JS, timeout=20000)
        pg.wait_for_timeout(250)
        dom = pg.evaluate(READ_RETRO_JS)
        dom["errs"] = list(errs)
        return dom
    finally:
        try:
            ctx.close()
        except Exception:                                     # noqa: BLE001
            pass


def build_neg():
    """隔离副本：只 cp evolution_intake.py 并配一个空 postmortem.jsonl。

    只拷这一个文件是有依据的：evolution_intake 只 import stdlib，且
    MEM=HERE/COMPANY_MEMORY，所以副本自成一体、**不碰真引擎目录**。
    空账本 -> 屏上必须 fail-closed BLOCK。
    """
    if os.path.isdir(NEG_ROOT):
        assert NEG_ROOT.startswith(WORK + os.sep) and NEG_ROOT.endswith("d82neg")
        shutil.rmtree(NEG_ROOT)
    os.makedirs(os.path.join(NEG_ROOT, "COMPANY_MEMORY"), exist_ok=True)
    shutil.copy2(os.path.join(COMPANY, "evolution_intake.py"),
                 os.path.join(NEG_ROOT, "evolution_intake.py"))
    empty = os.path.join(NEG_ROOT, "COMPANY_MEMORY", "postmortem.jsonl")
    with open(empty, "w", encoding="utf-8") as f:
        f.write("")
    return NEG_ROOT


def check_block(dom):
    """无数据时屏上只许有 BLOCK 卡：三张结论卡（簇/计数/账本）一张都不许出现。

    这里**不能**用「屏上不许出现『已经在变强』这几个字」来判 —— 那段诚实免责
    原文就写着「不给『没有复发 / 已经在变强』这种结论」，字面禁令会把自己判红。
    所以改成结构判据：结论卡一张都不许渲染，且只准有 1 张 BLOCK 卡。
    """
    text = norm(dom.get("text"))
    if not text:
        return False, "屏上一点字都没有"
    if "BLOCK" not in text:
        return False, "无数据却没有 BLOCK 判词：%r" % text[:160]
    titles = []
    for c in dom.get("cards", []):
        for h in c.get("h", []):
            titles.append(norm(h[0]))
    if not titles:
        return False, "无数据却一张卡都没画"
    for bad in ("复发簇", "自我诊断计数", "复盘账本"):
        if bad in titles:
            return False, "无数据却仍渲染了结论卡 %r（titles=%r）" % (bad, titles)
    if not any("读不出来" in t for t in titles):
        return False, "缺「读不出来」标题（titles=%r）" % (titles,)
    if dom.get("nCards") != 1:
        return False, "无数据时卡数=%d，期望 1" % dom.get("nCards")
    if "fail-closed" not in text:
        return False, "缺 fail-closed 说明：%r" % text[:160]
    return True, "无数据 -> BLOCK 单卡，三张结论卡一张都没画"


SELF_KEYS = ("runs", "agent_self_verifications", "agent_self_repairs",
             "agent_self_recoveries", "human_interventions",
             "human_takeovers", "n_generalizable", "n_with_plan",
             "n_family_known")
FAM_SUFFIX = "（其余 UNKNOWN_SEMANTIC，如实显示，不编家族）"


def check_payload(p, truth):
    """/__recurrence 端点必须与引擎 ground truth 逐字一致 —— 这是「接线」判据。"""
    if not isinstance(p, dict):
        return False, "端点没返回 JSON 对象：%r" % (type(p),)
    if not p.get("ok"):
        return False, "端点 ok=%r error=%r" % (p.get("ok"), p.get("error"))
    bad = []
    for k in ("threshold", "n_events", "n_clusters", "n_recurring"):
        if p.get(k) != truth[k]:
            bad.append("%s %r != %r" % (k, p.get(k), truth[k]))
    ps = p.get("self") or {}
    ts = truth["self"]
    for k in SELF_KEYS:
        if ps.get(k) != ts[k]:
            bad.append("self.%s %r != %r" % (k, ps.get(k), ts[k]))
    if (ps.get("verdicts") or {}) != ts["verdicts"]:
        bad.append("verdicts %r != %r" % (ps.get("verdicts"), ts["verdicts"]))
    if norm(p.get("memory")) != norm(truth["postmortem"]):
        bad.append("memory %r != %r" % (p.get("memory"), truth["postmortem"]))
    if len(p.get("clusters") or []) != truth["shown"]:
        bad.append("clusters 长度 %d != shown %d" % (len(p.get("clusters") or []), truth["shown"]))
    if len(p.get("ledger") or []) != len(truth["ledger"]):
        bad.append("ledger 长度 %d != %d" % (len(p.get("ledger") or []), len(truth["ledger"])))
    if bad:
        return False, "; ".join(bad[:6])
    return True, "阈值/事件/簇/复发数/self 9 项/verdicts/memory/长度 逐字一致"


def exp_clusters(truth):
    return truth["clusters"][:truth["shown"]]


def check_clusters(dom, truth):
    """复发簇面：徽章、读法/总事件/总簇、以及屏上 top-N 每一行逐字对引擎。"""
    card = pick_card(dom, "复发簇")
    if card is None:
        titles = [norm(h[0]) for c in dom.get("cards", []) for h in c.get("h", [])]
        return False, "屏上没有「复发簇」卡（titles=%r）" % (titles,)
    title, badges = head_badges(card)
    bad = []
    if title != "复发簇":
        bad.append("标题 %r" % title)
    cl = exp_clusters(truth)
    thr = truth["threshold"]
    want = []
    if truth["n_recurring"]:
        want.append("%s/%s 个能力缺口还没修好" % (jsnum(truth["n_recurring"]), jsnum(truth["n_clusters"])))
    else:
        want.append("暂无复发")
    want.append("阈值 runs≥" + ("—" if thr is None else jsnum(thr)))
    want.append("取自引擎 RECURRENCE_MIN")
    if badges != want:
        bad.append("徽章 %r != %r" % (badges, want))
    mm = meta_map(card)
    rd = mm.get("读法")
    if rd is None:
        bad.append("缺「读法」")
    elif ("%s 个以上 run" % jsnum(thr)) not in rd:
        bad.append("读法没写阈值 %s：%r" % (thr, rd))
    if mm.get("总事件") != jsnum(truth["n_events"]):
        bad.append("总事件 %r != %s" % (mm.get("总事件"), jsnum(truth["n_events"])))
    if mm.get("总簇") != jsnum(truth["n_clusters"]):
        bad.append("总簇 %r != %s" % (mm.get("总簇"), jsnum(truth["n_clusters"])))
    lis = card.get("lis") or []
    if len(lis) != len(cl):
        bad.append("行数 %d != 引擎 top%d" % (len(lis), len(cl)))
    for i, c in enumerate(cl):
        if i >= len(lis):
            break
        li = lis[i]
        want_ok = "1" if c["recurring"] else "0"
        if li.get("ok") != want_ok:
            bad.append("行%d data-ok %r != %r" % (i, li.get("ok"), want_ok))
        if norm(li.get("gn")) != "%s run" % jsnum(c["runs"]):
            bad.append("行%d run 数 %r != %s" % (i, li.get("gn"), c["runs"]))
        wlab = "未修好" if c["recurring"] else "未达阈"
        if wlab not in [norm(x) for x in (li.get("badges") or [])]:
            bad.append("行%d 徽章 %r != %r" % (i, li.get("badges"), wlab))
        if norm(li.get("head")) != norm(c["cluster"]):
            bad.append("行%d 簇名 %r != %r" % (i, li.get("head"), c["cluster"]))
        w0 = "%s · events %s · 最后一次 %s" % (c["family"] or "—", jsnum(c["events"]), c["last_ts"] or "—")
        whys = [norm(w) for w in (li.get("whys") or [])]
        if not whys or whys[0] != norm(w0):
            bad.append("行%d 首条 gwhy %r != %r" % (i, whys[:1], norm(w0)))
        wp = "下次改什么：" + norm(c["plan"])
        if c["plan"]:
            if len(whys) < 2 or whys[1] != wp:
                bad.append("行%d 计划 gwhy %r != %r" % (i, whys[1:2], wp))
        elif len(whys) > 1:
            bad.append("行%d 无 plan 却多出 gwhy %r" % (i, whys[1:2]))
    if bad:
        return False, "; ".join(bad[:5])
    return True, "标题/3 徽章/读法/总事件/总簇 + top%d 逐行逐字" % len(cl)


def check_self(dom, truth):
    """自我诊断计数面：7 项 gmeta、诚实注记的有无、6 列趋势表逐格。"""
    card = pick_card(dom, "自我诊断计数")
    if card is None:
        titles = [norm(h[0]) for c in dom.get("cards", []) for h in c.get("h", [])]
        return False, "屏上没有「自我诊断计数」卡（titles=%r）" % (titles,)
    title, badges = head_badges(card)
    s = truth["self"]
    v = s["verdicts"]
    bad = []
    if title != "自我诊断计数":
        bad.append("标题 %r" % title)
    wb = ["runs " + jsnum(s["runs"])]
    wb.append("BLOCK %s · SHIP_OK %s" % (jsnum(v.get("BLOCK", 0)), jsnum(v.get("SHIP_OK", 0))))
    if badges != wb:
        bad.append("徽章 %r != %r" % (badges, wb))
    mm = meta_map(card)
    for k in ("agent_self_verifications", "agent_self_repairs", "agent_self_recoveries"):
        if mm.get(k) != jsnum(s[k]):
            bad.append("%s %r != %s" % (k, mm.get(k), s[k]))
    gp = "%s/%s" % (jsnum(s["n_generalizable"]), jsnum(s["runs"]))
    if mm.get("可泛化") != gp:
        bad.append("可泛化 %r != %r" % (mm.get("可泛化"), gp))
    wp = "%s/%s" % (jsnum(s["n_with_plan"]), jsnum(s["runs"]))
    if mm.get("有改进建议") != wp:
        bad.append("有改进建议 %r != %r" % (mm.get("有改进建议"), wp))
    fk_head = "%s/%s" % (jsnum(s["n_family_known"]), jsnum(s["runs"]))
    got_fk = mm.get("根因已分类") or ""
    if not got_fk.startswith(fk_head):
        bad.append("根因已分类 %r 不以 %r 开头" % (got_fk, fk_head))
    elif "如实显示" not in got_fk or "UNKNOWN_SEMANTIC" not in got_fk:
        bad.append("根因已分类 丢了诚实后缀：%r" % got_fk)
    hi = "%s · 人类接管 %s" % (jsnum(s["human_interventions"]), jsnum(s["human_takeovers"]))
    if mm.get("人类介入") != hi:
        bad.append("人类介入 %r != %r" % (mm.get("人类介入"), hi))
    if len(mm) != 7:
        bad.append("gmeta 项数 %d != 7：%r" % (len(mm), sorted(mm)))
    note = norm(card.get("note"))
    want_note = (s["human_interventions"] == 0 and s["human_takeovers"] == 0)
    if want_note and not note:
        bad.append("人类侧全程 0 却没画诚实注记（M4 就靠这条）")
    if want_note and note and ("全程为 0" not in note or "不画" not in note):
        bad.append("诚实注记文案不对：%r" % note[:120])
    if (not want_note) and note:
        bad.append("人类侧非 0 却仍画「全程为 0」注记：%r" % note[:80])
    heads = [norm(h) for h in (card.get("heads") or [])]
    if len(heads) != 6:
        bad.append("趋势表 %d 列 != 6：%r" % (len(heads), heads))
    tr = truth["trend"]
    rows = card.get("rows") or []
    if len(rows) != len(tr):
        bad.append("趋势行数 %d != %d" % (len(rows), len(tr)))
    for i, b in enumerate(tr):
        if i >= len(rows):
            break
        per = []
        for k in ("ver", "rep", "rec"):
            per.append(jsnum(jsround(b[k] * 10.0 / b["n"]) / 10.0) if b["n"] else "0")
        want = [b["day"], jsnum(b["n"]), jsnum(b["ver"]), jsnum(b["rep"]), jsnum(b["rec"])]
        want.append("%s / %s / %s" % (per[0], per[1], per[2]))
        got = [norm(x) for x in rows[i]]
        if got != want:
            bad.append("趋势行%d %r != %r" % (i, got, want))
    if bad:
        return False, "; ".join(bad[:5])
    return True, "标题/2 徽章/gmeta 7 项/诚实注记/6 列趋势 %d 行" % len(tr)


def check_ledger(dom, truth):
    """复盘账本面：徽章、只读声明、以及逐字取自 postmortem.jsonl 的每一行。"""
    card = pick_card(dom, "复盘账本")
    if card is None:
        titles = [norm(h[0]) for c in dom.get("cards", []) for h in c.get("h", [])]
        return False, "屏上没有「复盘账本」卡（M2 就抽这一面）（titles=%r）" % (titles,)
    title, badges = head_badges(card)
    L = truth["ledger"]
    bad = []
    if title != "复盘账本":
        bad.append("标题 %r" % title)
    wb = ["新 → 旧 %d 条" % len(L), "逐字取自引擎 postmortem.jsonl"]
    if badges != wb:
        bad.append("徽章 %r != %r" % (badges, wb))
    mm = meta_map(card)
    if norm(mm.get("账本")) != norm(truth["postmortem"]):
        bad.append("账本路径 %r != %r" % (mm.get("账本"), truth["postmortem"]))
    ro = mm.get("只读") or ""
    if "一个字节都没写" not in ro:
        bad.append("缺只读声明：%r" % ro)
    lis = card.get("lis") or []
    if len(lis) != len(L):
        bad.append("账本行数 %d != %d" % (len(lis), len(L)))
    for i, r in enumerate(L):
        if i >= len(lis):
            break
        li = lis[i]
        want_ok = "1" if r["verdict"] == "SHIP_OK" else "0"
        if li.get("ok") != want_ok:
            bad.append("行%d data-ok %r != %r" % (i, li.get("ok"), want_ok))
        if norm(li.get("gn")) != norm(r["ts"][5:16]):
            bad.append("行%d 时间 %r != %r" % (i, li.get("gn"), r["ts"][5:16]))
        got = [norm(x) for x in (li.get("badges") or [])]
        if norm(r["verdict"]) not in got:
            bad.append("行%d verdict 徽章 %r != %r" % (i, li.get("badges"), r["verdict"]))
        if norm(li.get("head")) != norm(r["gap"] or "—"):
            bad.append("行%d 能力缺口 %r != %r" % (i, li.get("head"), r["gap"] or "—"))
        whys = [norm(w) for w in (li.get("whys") or [])]
        w0 = "为什么错：%s（%s）" % (r["root_cause"] or "—", r["family"] or "UNKNOWN_SEMANTIC")
        if len(whys) < 1 or whys[0] != norm(w0):
            bad.append("行%d 根因 %r != %r" % (i, whys[:1], norm(w0)))
        w1 = "下次改什么：" + norm(r["plan"] or "—")
        if len(whys) < 2 or whys[1] != w1:
            bad.append("行%d 计划 %r != %r" % (i, whys[1:2], w1))
        g = "是" if r["generalizable"] else "否"
        w2 = "可泛化 %s · 可恢复 %s · 可预防 %s" % (g, jsbool(r["recoverable"]), jsbool(r["preventable"]))
        if len(whys) < 3 or whys[2] != norm(w2):
            bad.append("行%d 三性 %r != %r" % (i, whys[2:3], norm(w2)))
    if bad:
        return False, "; ".join(bad[:5])
    return True, "标题/2 徽章/只读声明 + 账本 %d 行逐字" % len(L)


def build_frozen():
    """冻结快照：evolution_intake.py + 两个 jsonl 整套拷进 work/d82frozen。

    浏览器那侧（正例 + 4 个变异体）全在这个副本上跑，所以完全确定 ——
    真 postmortem.jsonl 由并发 run 持续追加，边跑边变的话变异体红绿就成了
    运气。而「serve.py 真的指着真目录」由 stable_payload 在活目录上单独验；
    少了那一步，副本会把「指错目录」这个 bug 一起盖住。两步分工就是这个意思。
    """
    if os.path.isdir(FROZEN):
        assert FROZEN.startswith(WORK + os.sep) and FROZEN.endswith("d82frozen")
        shutil.rmtree(FROZEN)
    memdir = os.path.join(FROZEN, "COMPANY_MEMORY")
    os.makedirs(memdir, exist_ok=True)
    shutil.copy2(os.path.join(COMPANY, "evolution_intake.py"),
                 os.path.join(FROZEN, "evolution_intake.py"))
    for nm in ("postmortem.jsonl", "improvement_queue.jsonl"):
        src = os.path.join(COMPANY, "COMPANY_MEMORY", nm)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(memdir, nm))
    return FROZEN


def stable_payload(port, tries=4):
    """活端点取样：两次引擎签名前后一致才承认「静止」，否则重试。

    postmortem.jsonl / improvement_queue.jsonl 由并发 run 持续追加。判据只靠
    sleep 赌的话，先算 truth 再 fetch 中间落进一条记录就假红 —— 那不是被测对象
    的 bug，是取样竞态。所以用「引擎签名前后一致」当静止判据。
    """
    why = ""
    for i in range(tries):
        t0 = engine_truth()
        st, p = http_get("http://127.0.0.1:%d/__recurrence" % port)
        t1 = engine_truth()
        s0 = json.dumps(t0, sort_keys=True, ensure_ascii=False)
        s1 = json.dumps(t1, sort_keys=True, ensure_ascii=False)
        if st == 200 and isinstance(p, dict) and p.get("ok") and s0 == s1:
            return t0, p, "第 %d 次取样：HTTP=200 且引擎签名前后一致" % (i + 1)
        why = "第 %d 次：HTTP=%r ok=%r 静止=%s" % (i + 1, st, p.get("ok") if isinstance(p, dict) else None, s0 == s1)
    return None, None, why + "（试满 %d 次仍不静止）" % tries


# 四个变异体。只动 served 字节，活文件零写入。repl 必须是合法 JS ——
# 语法错会直接 pageerror，那是「崩了」而不是「判据抓到了」，两者不能混。
VCHECKS = ((check_clusters, "复发簇"), (check_self, "自我诊断计数"), (check_ledger, "复盘账本"))
MUTANTS = []
MUTANTS.append(("M1", ANCHOR_DRAW, "RLAST=j;", "抽掉 rdraw 调用：RLAST 照样赋值、屏上永不渲染"))
MUTANTS.append(("M2", ANCHOR_LEDGER, "box.innerHTML=rclusters(j)+rself(j);", "抽掉账本面：簇与计数还在、账本整面消失"))
MUTANTS.append(("M3", ANCHOR_REC, "const bad=1;", "recurring 写死 true：未达阈的簇被误标「未修好」"))
MUTANTS.append(("M4", ANCHOR_HONEST, "if(true) return '';", "抹掉「人类侧全程为 0」诚实注记"))


def run_mutants(br, base, truth):
    """每个变异体都必须被至少一组判据抓住；一个都抓不住 = 门失能。"""
    for tag, anchor, repl, why in MUTANTS:
        try:
            dom = run_page(br, base + "/index.html", anchor, repl)
        except Exception as e:                                # noqa: BLE001
            add("上牙 %s 被抓住（%s）" % (tag, why), False, "页面直接崩了：%r" % (e,))
            continue
        if dom.get("errs"):
            why2 = "变异体有 JS 报错，那是崩不是判红：%r" % (dom.get("errs"),)
            add("上牙 %s 被抓住（%s）" % (tag, why), False, why2)
            continue
        caught = []
        for fn, nm in VCHECKS:
            ok, detail = fn(dom, truth)
            if not ok:
                caught.append("%s <- %s" % (nm, detail[:110]))
        add("上牙 %s 被抓住（%s）" % (tag, why), bool(caught), "变异后三组判据全绿 = 门失能")
        for c in caught:
            print("       %s 抓住于 %s" % (tag, c))


NEED = []
NEED.append(("index.html", os.path.join(ROOT, "index.html")))
NEED.append(("serve.py", SERVE_PY))
NEED.append(("Chromium", CHROME))
NEED.append(("company.sh", os.path.join(COMPANY, "company.sh")))
NEED.append(("evolution_intake.py", os.path.join(COMPANY, "evolution_intake.py")))
ANCHORS = []
ANCHORS.append(("ANCHOR_DRAW", ANCHOR_DRAW))
ANCHORS.append(("ANCHOR_LEDGER", ANCHOR_LEDGER))
ANCHORS.append(("ANCHOR_REC", ANCHOR_REC))
ANCHORS.append(("ANCHOR_HONEST", ANCHOR_HONEST))

IDS = []
IDS.append("t-retro")
IDS.append("v-retro")
IDS.append("rtgo")
IDS.append("rthint")
IDS.append("rtbox")
IDS.append("retroLoad")
IDS.append("retroReset")


def env_check(src):
    """环境 + 锚点体检。任一项红 -> rc=2（ENV-UNAVAILABLE），不许当绿算。"""
    ok = True
    for nm, path in NEED:
        e = os.path.exists(path)
        add("环境 文件存在 %s" % nm, e, "缺 %s" % path)
        ok = ok and e
    try:
        import playwright                                      # noqa: F401
        add("环境 import playwright", True)
    except Exception as e:                                     # noqa: BLE001
        add("环境 import playwright", False, repr(e))
        ok = False
    for nm, a in ANCHORS:
        c = src.count(a)
        add("环境 锚点 %s 恰好命中 1 次" % nm, c == 1, "命中 %d 次（0=门失能，>1=锚点不唯一）" % c)
        ok = ok and (c == 1)
    for i in IDS:
        e = i in src
        add("环境 复盘台锚点 %s 在 index.html" % i, e, "找不到 %s" % i)
        ok = ok and e
    return ok


def report(rc):
    names = list(G.keys())
    red = [n for n in names if not G[n]]
    path = os.path.join(WORK, "retro_gate.json")
    out = {"gate": "retro_gate", "rc": rc, "checks": len(names),
           "green": len(names) - len(red), "red": red, "why": WHY}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)
    for n in names:
        line = "  [%s] %s" % ("绿" if G[n] else "红", n)
        if not G[n]:
            line += "  <- " + str(WHY.get(n, ""))
        print(line)
    print("  门判据 %d 项，红 %d 项 -> %s" % (len(names), len(red), path))
    tail = {0: "== D82 retro_gate OK ==",
            1: "== D82 retro_gate FAIL ==",
            2: "== D82 retro_gate ENV-UNAVAILABLE =="}[rc]
    print(tail)
    return rc


def main():
    src = io.open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    if not env_check(src):
        return report(2)
    # 禁比 postmortem.jsonl 的 sha：并发 run 一直在追加，比它必然假红。
    sha_b = {}
    for nm, path in (("index.html", os.path.join(ROOT, "index.html")),
                     ("serve.py", SERVE_PY)):
        sha_b[nm] = sha256_file(path)
    br = None
    fmod = nmod = None
    fsrv = nsrv = None
    try:
        # (1) 冻结快照：浏览器侧正例 + 4 个变异体全在这里跑，数据完全确定。
        froot = build_frozen()
        ftruth = engine_truth(froot)
        shown = exp_clusters(ftruth)
        add("正例 快照 top%d 里有非复发簇（否则 M3 会假绿）" % len(shown),
            any(not c["recurring"] for c in shown),
            "top%d 全是复发簇，M3 的 const bad=1; 抓不住" % len(shown))
        fmod = patch_serve(load_serve(SERVE_PY, "_d82_serve_frozen"))
        fmod.COMPANY = froot
        fmod._intake = None
        fport = free_port()
        fsrv = start_server(fport, fmod)
        st, fp = http_get("http://127.0.0.1:%d/__recurrence" % fport)
        add("正例 快照端点 HTTP 200 且 ok",
            st == 200 and isinstance(fp, dict) and bool(fp.get("ok")),
            "HTTP=%r ok=%r" % (st, fp.get("ok") if isinstance(fp, dict) else None))
        ok, d = check_payload(fp, ftruth)
        add("正例 快照端点与引擎 ground truth 一致", ok, d)
        # (2) 活端点接线：另起一个**不改 COMPANY** 的实例，证明 serve.py 真指着
        # 真目录。少了这一步，冻结副本会把「指错目录」这个 bug 一起盖住。
        lmod = patch_serve(load_serve(SERVE_PY, "_d82_serve_live"))
        lport = free_port()
        lsrv = start_server(lport, lmod)
        try:
            truth, p, why = stable_payload(lport)
        finally:
            lsrv.shutdown()
        add("接线 活端点取到静止样本", truth is not None, why)
        if truth is not None:
            ok, d = check_payload(p, truth)
            add("接线 活端点与真引擎 ground truth 一致", ok, d)
            mem = norm(p.get("memory"))
            add("接线 活端点 memory 指向真 COMPANY_MEMORY",
                mem == norm(truth["postmortem"]) and COMPANY in mem,
                "memory=%r 期望含 %r" % (p.get("memory"), COMPANY))
        # (3) 空账本诚实负例：端点必须 503，屏上必须 fail-closed 单卡。
        nroot = build_neg()
        nmod = patch_serve(load_serve(SERVE_PY, "_d82_serve_neg"))
        nmod.COMPANY = nroot
        nmod._intake = None
        nport = free_port()
        nsrv = start_server(nport, nmod)
        st, npay = http_get("http://127.0.0.1:%d/__recurrence" % nport)
        add("负例 空账本端点必须 HTTP 503 fail-closed", st == 503,
            "HTTP=%r（期望 503）" % st)
        st2, _raw = http_get("http://127.0.0.1:%d/index.html" % nport)
        add("负例 静态页仍能供（503 来自数据层不是服务器挂）", st2 == 200, "HTTP=%r" % st2)
        # (4) 浏览器：正例三组判据全绿 -> 四个变异体全红 -> 空账本负例。
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch(executable_path=CHROME)
            base = "http://127.0.0.1:%d" % fport
            dom = run_page(br, base + "/index.html")
            add("正例 无 pageerror", not dom.get("errs"), repr(dom.get("errs")))
            add("正例 #rtbox 读完非空", not dom.get("empty"), "读完账本 #rtbox 还是空的")
            add("正例 三张结论卡都在", dom.get("nCards") == 3,
                "nCards=%r 期望 3" % dom.get("nCards"))
            for fn, nm2 in VCHECKS:
                ok, d = fn(dom, ftruth)
                add("正例 %s 判据绿" % nm2, ok, d)
            add("正例 提示条判已读", "已读" in norm(dom.get("hint")), "hint=%r" % dom.get("hint"))
            run_mutants(br, base, ftruth)
            ndom = run_page(br, "http://127.0.0.1:%d/index.html" % nport)
            ok, d = check_block(ndom)
            add("负例 屏上 fail-closed BLOCK 单卡", ok, d)
            br.close()
            br = None
    except Exception as e:                                         # noqa: BLE001
        add("门自身跑完（无未捕获异常）", False, repr(e))
    finally:
        for m in (fmod, nmod):
            if m is not None:
                kill_current(m)
        for sv in (fsrv, nsrv):
            if sv is not None:
                try:
                    sv.shutdown()
                except Exception:                                  # noqa: BLE001
                    pass
        if br is not None:
            try:
                br.close()
            except Exception:                                      # noqa: BLE001
                pass
        for dd in (NEG_ROOT, FROZEN):
            if os.path.isdir(dd):
                assert dd.startswith(WORK + os.sep), dd
                shutil.rmtree(dd)
    for nm, path in (("index.html", os.path.join(ROOT, "index.html")),
                     ("serve.py", SERVE_PY)):
        add("零写入 %s 本轮字节未变" % nm, sha256_file(path) == sha_b[nm],
            "门自己写了活文件")
    return report(1 if [n for n in G if not G[n]] else 0)


if __name__ == "__main__":
    sys.exit(main())
