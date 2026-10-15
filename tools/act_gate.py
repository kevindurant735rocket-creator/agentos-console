#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行面门：驾驶舱从「只读仪表盘」升级成「能自己动手」之后的守门人。

为什么单独一门：a11y_walk / ux_audit / view_dod 全都走 file:// 开页面，
测不到 HTTP 行为。而执行面恰恰全部风险都在 HTTP 边界上：
  - 跨站页面能不能白嫖 POST（CSRF）
  - 动作名能不能拼出 shell（注入）
  - 两个动作会不会同时跑（单飞）
  - 前端点了按钮到底有没有真反馈（真浏览器点，真读 aria-live 区）

本门自己起一份 serve.py（--tree 指假树，绝不碰真树），
API 用 stdlib urllib 测，UI 用真浏览器点。
刻意不执行 rebuild：它会重写项目 index.html/data.json，
门不该在验收时改动被验收物。rebuild 只断言「在目录里」。

产出 work/act_gate.json，rc=0 全过 / 2 有红项。
"""
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
COMPANY = os.path.expanduser("~/.codex/global-background/company-delivery")
COMPANY_SH = os.path.join(COMPANY, "company.sh")
SAFE_ACT = "doc-drift"          # 只读、约 0.6s，适合当真实执行样本

# v118 实测：Playwright 默认导航超时 30s，而本页 load 时会并发打 /__promote
# （引擎 capability.py 真判定，冷启动实测到 9.5s）。机器一忙就 Page.goto 超时，
# 门随机红 —— 实测同一份代码连续两次跑出 18/19 与 19/19。**会随机红的门比没有门更糟**，
# 因为它训练人忽略红灯。这里放宽到 90s：放宽的是「等页面就绪」，
# 下面每条断言该红照样红，只是不再把机器慢当成执行面坏了。
NAV_TIMEOUT_MS = 90000

# v118 实测：真浏览器那 5 条门要 playwright，而 playwright 只装在项目自带的
# .venv-pw 里。于是同一个脚本有三种跑法、三种结果：
#   bash tools/gates.sh act_gate            -> 选对解释器，19/19
#   .venv-pw/bin/python tools/act_gate.py  -> 选对解释器，19/19
#   python3 tools/act_gate.py               -> 缺 playwright，5 条门假红，14/19
# 引擎的 D27「执行面接线」走的正是第三种，于是 livecheck 报 rc=2 —— 红的是
# **跑门的人选错了解释器**，不是执行面真坏了。门自己有责任让任何调用者都跑出
# 同一个结果，所以在缺依赖时自己换解释器重跑一次（os.execv，不留第二套逻辑）。
_PW_PY = os.path.join(ROOT, ".venv-pw", "bin", "python")


def _reexec_with_playwright():
    try:
        import playwright  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("AGENTOS_GATE_REEXEC") == "1":
        return                        # 已在正确解释器里仍缺依赖：真缺，如实红
    # 必须比**未解析软链的路径**：.venv-pw/bin/python 是软链到 homebrew 3.14，
    # 而 PATH 上的 python3 也正是 3.14 —— 用 realpath 比会把「venv」和「系统」
    # 判成同一个，于是 shim 自我禁用（实测踩过：门照样 14/19 假红）。
    if os.path.exists(_PW_PY) and os.path.abspath(_PW_PY) != os.path.abspath(sys.executable):
        os.environ["AGENTOS_GATE_REEXEC"] = "1"
        os.execv(_PW_PY, [_PW_PY, os.path.abspath(__file__)] + sys.argv[1:])


_reexec_with_playwright()

GATES = []


def gate(name):
    def deco(fn):
        GATES.append((name, fn))
        return fn
    return deco


# D50（2026-10-03）：重试语义抽到 tools/flake_retry.py 单一真源。
# 原来这段只活在本文件里，于是 shoot / view_dod / a11y_walk 三门真拉 Chrome
# 偶发 TargetClosedError 时**没有任何重试** → D50 偶发红。
# 四家现在共用同一份 run_gate / _is_infra，语义只有一处可改。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from flake_retry import _is_infra, run_gate  # noqa: E402,F401


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(port, body, csrf=True, timeout=300):
    """返回 (http_code, parsed_json_or_text)。"""
    hdr = {"Content-Type": "application/json"}
    if csrf:
        hdr["X-AgentOS-Action"] = "1"
    req = urllib.request.Request(
        "http://127.0.0.1:%d/__act" % port,
        data=json.dumps(body).encode(), headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw.decode("utf-8", "replace")[:200]}


def get_health(port, timeout=10):
    with urllib.request.urlopen("http://127.0.0.1:%d/__health" % port, timeout=timeout) as r:
        return json.loads(r.read())


def get_json(port, path, timeout=30):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                timeout=timeout) as r:
        return r.status, json.loads(r.read())


@gate("数字反漂移：台面标签的 livecheck 项数 == 引擎 ck 项数（不许各处写死）")
def g_label_drift(ctx):
    """驾驶舱曾在 serve.py 和 ACT_FALLBACK 各写死一份「17 项」，
    而引擎是 18 项——doc_drift 只查 SKILL/DOCTRINE，抓不到。
    这里把台面标签和引擎真值钉在一起，任一处漂移即红。
    取值刻意用 doc_drift 同口径的静态 ck 计数，**不**跑 company.sh goal livecheck：
    livecheck 反过来会跑本项目 act_gate，在门里调门 = 自递归死锁（实测 220s 超时）。"""
    src = io.open(os.path.join(HERE, "serve.py"), encoding="utf-8").read()
    html = io.open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    labels = set(re.findall(r"上线自证\s*(\d+)\s*项", src + html))
    gsrc = io.open(os.path.join(COMPANY, "goal_loop.py"), encoding="utf-8").read()
    try:
        body = gsrc[gsrc.index("def cmd_livecheck"):gsrc.index("def _render_table")]
    except ValueError:
        return False, "goal_loop.py 结构变了，取不到 cmd_livecheck 区间"
    n = len(re.findall(r"\bck\(", body)) - 1      # 与 doc_drift 同口径（减去 def ck 定义行）
    ok = labels == set([str(n)])
    detail = "引擎 ck 项数 %d（doc_drift 同口径）· 台面标签 %s" % (n, sorted(labels) or "无")
    if not ok:
        detail = "漂移：引擎 %d 项 vs 台面 %s" % (n, sorted(labels))
    # v127 实测：index.html 是 build.py 的产物，标签由 __LIVECHECK_N__ 在**构建期**注入。
    # 引擎从 35 加到 36 项后模板不用改，但**没人跑 build.py** 就会静默留下旧构建；
    # 上面那道标签比对只报「漂移」，不报「你只是忘了重建」——红灯指向错处，难查。
    # 这里补时效口径：index.html 必须不早于它的任一输入。实测 goal_loop.py 01:18 改动、
    # index.html 还停在 01:23 之前的旧构建，就是这么红的。
    feeds = [os.path.join(ROOT, "index.template.html"), os.path.join(ROOT, "data.json"),
             os.path.join(HERE, "serve.py"), os.path.join(HERE, "build.py"),
             os.path.join(COMPANY, "goal_loop.py")]
    try:
        built = os.path.getmtime(os.path.join(ROOT, "index.html"))
        newer = [os.path.relpath(p, ROOT) for p in feeds
                 if os.path.exists(p) and os.path.getmtime(p) > built]
    except OSError as exc:
        return False, "读不到 index.html 时间戳：%s" % exc
    if newer:
        return False, ("index.html 是旧构建（早于 %s）——不是漂移，跑 `python3 tools/build.py` 重建"
                       % "、".join(newer))
    return ok, detail


ENGINE_PROBE = (
    "import importlib.util;"
    "s=importlib.util.spec_from_file_location('i', %r);"
    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m);"
    "ev=m.load_events();"
    "print(m.RECURRENCE_MIN, len(ev), len(m.build_clusters(ev)))"
) % os.path.join(COMPANY, "evolution_intake.py")


@gate("学习面：/__recurrence 真取引擎复发簇，阈值与引擎同源")
def g_recurrence(ctx):
    code, j = get_json(ctx["port"], "/__recurrence")
    if code != 200 or not j.get("ok"):
        return False, "HTTP %d ok=%s %s" % (code, j.get("ok"), j.get("error"))
    p = subprocess.run([sys.executable, "-c", ENGINE_PROBE],
                       capture_output=True, text=True, timeout=150)
    if p.returncode != 0:
        return False, "引擎侧取数失败 rc=%d" % p.returncode
    thr, nev, ncl = (int(x) for x in p.stdout.split())
    nrec = sum(1 for c in j["clusters"] if c["recurring"])
    ok = (j.get("threshold") == thr and j.get("n_events") == nev
          and j.get("n_clusters") == ncl and ncl > 0
          and j.get("n_recurring") == nrec)
    return ok, "阈值 %s · 事件 %s · 簇 %s · 达阈值 %s（与引擎逐项一致）" % (
        j.get("threshold"), j.get("n_events"), j.get("n_clusters"), j.get("n_recurring"))


FAILCLOSED_PROBE = (
    "import sys; sys.path.insert(0, %r); import serve;"
    "serve.COMPANY='/nonexistent/cd'; serve._intake=None;"
    "p=serve.recurrence_payload();"
    "print('OK' if (p['ok'] is False and p['clusters']==[]) else 'BAD')"
) % os.path.join(HERE)


@gate("学习面 fail-closed：引擎取不到时报不可用，不许空簇冒充无复发")
def g_recurrence_failclosed(ctx):
    """真反证：把 COMPANY 指到空目录再 import 真实 recurrence_payload。
    若返回 ok=True + 空簇，就等于谎报「没复发」，必须判红。"""
    p = subprocess.run([sys.executable, "-c", FAILCLOSED_PROBE],
                       capture_output=True, text=True, timeout=90)
    got = p.stdout.strip()[-2:]
    return got == "OK", "坏 COMPANY 路径 -> %s（期望 OK：ok=False 且 clusters 空）" % \
        (got or p.stderr.strip()[-60:])


@gate("CSRF 闸：跨源 POST 不带自定义头必须被拒")
def g_csrf(ctx):
    code, body = post(ctx["port"], {"action": SAFE_ACT}, csrf=False, timeout=20)
    ok = code == 403 and body.get("ok") is False
    return ok, "无 X-AgentOS-Action 头 -> %d %s" % (code, body.get("error"))


@gate("注入闸：动作名不是 allowlist 成员一律 404，绝不落到 shell")
def g_inject(ctx):
    evil = ["rm -rf /", "desktop; whoami", "desktop && id", "../../bin/sh",
            "$(whoami)", "desktop|cat", "", None, 123, {"a": 1}]
    bad = []
    for e in evil:
        code, body = post(ctx["port"], {"action": e}, timeout=20)
        if code != 404 or body.get("ok") is not False:
            bad.append("%r -> %d %s" % (e, code, body))
    return not bad, ("10 个注入样本全 404" if not bad else "漏网: " + "; ".join(bad))


@gate("越权闸：非 /__act 的 POST 端点 404")
def g_noep(ctx):
    req = urllib.request.Request("http://127.0.0.1:%d/__nope" % ctx["port"],
                                 data=b"{}", method="POST",
                                 headers={"X-AgentOS-Action": "1"})
    try:
        urllib.request.urlopen(req, timeout=20)
        return False, "__nope 竟然通了"
    except urllib.error.HTTPError as e:
        return e.code == 404, "/__nope -> %d" % e.code


@gate("动作目录：__list 返回非空目录")
def g_catalog(ctx):
    code, body = post(ctx["port"], {"action": "__list"}, timeout=20)
    acts = body.get("actions") or []
    names = [a.get("name") for a in acts]
    ok = code == 200 and len(acts) >= 5 and "rebuild" in names
    ok = ok and all(a.get("desc") for a in acts)
    return ok, "%d 个动作: %s" % (len(acts), ",".join(names))


def wire_verdict(http_rc, indep_rcs):
    """判据本体：真实执行 = HTTP 报的 rc **等于**独立跑同一条命令拿到的真 rc。

    为什么不再要求 rc==0（与 g_ui_feedback 同一类错，已在 v136 修过一次）：
    这条门叫「rc 由子进程真实退出码裁决」，它要验的**性质**是「服务端没有自己编一个 rc」，
    不是「被抽样的那个动作今天恰好成功」。原来写 `body['rc'] == 0` 把两件事焊死：
    引擎文档一漂移 → sample 动作 doc-drift 返回 rc=2 → 这条门报红，
    而接线其实完好；更糟的是它把红灯递到引擎 D27（D27 跑本门要 ACT-GATE-OK），
    于是**引擎文档漂移会把台面和引擎上线门一起拽红**，责任链指错方向。

    现在改成拿「独立复现的真 rc」当基准：自己 subprocess 跑一遍 doc-drift，
    HTTP 报的必须逐位等于它。编造 rc（永远返回 0）当场被抓，且与动作成败无关。

    两次独立跑不一致 = 抽样期间世界变了（另一线程正在改引擎），判据 fail-closed，
    不去猜哪个是真的 —— 猜错就等于把「我猜的」当成「子进程说的」。
    """
    if http_rc is None or not indep_rcs:
        return False, "拿不到真 rc：http rc=%r 独立跑=%s" % (http_rc, indep_rcs)
    if http_rc not in indep_rcs:
        return False, "HTTP rc=%s 与独立跑的 %s 都不一致——rc 可能是编的" % (http_rc, indep_rcs)
    if len(set(indep_rcs)) != 1:
        return False, "独立跑两次结果不一致 %s（抽样期间世界在变），不予裁决" % (indep_rcs,)
    return True, "HTTP rc=%s == 独立复现 rc=%s" % (http_rc, indep_rcs[0])


def _indep_doc_drift():
    """独立于 HTTP 面复现 sample 动作的真实退出码（与 serve.py 同 cwd）。"""
    rcs = []
    for _ in range(2):
        try:
            r = subprocess.run(["bash", COMPANY_SH, "doc-drift"], cwd=ROOT,
                               capture_output=True, text=True, timeout=300)
            rcs.append(r.returncode)
        except Exception as exc:                      # noqa: BLE001 —— 判据要 fail-closed
            return rcs, str(exc)[:80]
        if rcs[-1] == 0:
            break                                     # 稳定了，不必再花一次
    return rcs, ""


@gate("真实执行：rc 由子进程真实退出码裁决")
def g_real(ctx):
    t0 = time.time()
    code, body = post(ctx["port"], {"action": SAFE_ACT}, timeout=300)
    dur = time.time() - t0
    tail = " ".join(body.get("tail") or [])
    basic = code == 200 and body.get("ok") is True
    if not basic:
        return False, "HTTP %d ok=%s rc=%s dur=%.1fs" % (code, body.get("ok"), body.get("rc"), dur)
    indep, err = _indep_doc_drift()
    if err:
        return False, "独立复现失败：%s" % err
    ok, why = wire_verdict(body.get("rc"), indep)
    return ok, "%s · %s · dur=%.1fs tail=%s" % (SAFE_ACT, why, dur, tail[-40:])


@gate("判据自证：rc 接线判据能抓住「编造 rc」，且与动作成败无关")
def g_wire_selftest(ctx):
    """给 wire_verdict 装牙齿：正例 + 4 条负控 + 3 个变异体，全部必须按预期。

    没有这一门，wire_verdict 随时能被改成 `return True, ""` 而没人发现——
    那正是它要防的病。变异体 M1/M2/M3 模拟三种退化实现，全都必须被用例抓住。
    """
    cases = [
        ("正例 真 rc=0",            (0, [0, 0]),            True),
        ("正例 真 rc=2（引擎漂移）", (2, [2, 2]),            True),
        ("负控 N1 编造：报 0 实为 2", (0, [2, 2]),           False),
        ("负控 N2 编造：报 0 实为 0 但独立跑两值", (0, [0, 2]), False),
        ("负控 N3 无独立基准",       (0, []),               False),
        ("负控 N4 rc 缺失",         (None, [0, 0]),         False),
    ]
    bad = []
    for name, args, want in cases:
        got, _ = wire_verdict(*args)
        if got is not want:
            bad.append("%s 期望 %s 实得 %s" % (name, want, got))
    # 变异体：三种退化实现，每个都必须至少被一条用例抓住
    mutants = {
        "M1 恒真":     lambda h, i: True,
        "M2 漏独立跑": lambda h, i: h is not None,
        "M3 漏比对":   lambda h, i: True if i else False,
    }
    for mname, fn in mutants.items():
        caught = [name for name, args, want in cases if fn(*args) is not want]
        if not caught:
            bad.append("%s 没被任何用例抓住（判据无牙）" % mname)
    ok = not bad
    return ok, ("6 用例 + 3 变异体双向全绿" if ok else "；".join(bad)[:160])


@gate("单飞：并发同动作必须有人吃 409，不许并跑")
def g_singleflight(ctx):
    codes, lock = [], threading.Lock()

    def hit():
        c, _ = post(ctx["port"], {"action": SAFE_ACT}, timeout=300)
        with lock:
            codes.append(c)

    ts = [threading.Thread(target=hit) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    busy = sum(1 for c in codes if c == 409)
    ok = busy >= 1 and all(c in (200, 409) for c in codes)
    return ok, "8 并发 -> %s（busy=%d）" % (sorted(codes), busy)


@gate("health 暴露动作目录，外部可核验执行面在位")
def g_health(ctx):
    h = get_health(ctx["port"])
    ok = h.get("ok") is True and isinstance(h.get("actions"), list)
    ok = ok and len(h["actions"]) >= 5
    return ok, "actions=%s act_running=%s" % (h.get("actions"), h.get("act_running"))


UI_INFO_JS = """() => {
  const bs = [...document.querySelectorAll('#actgrid button[data-act]')];
  return {
    n: bs.length,
    unnamed: bs.filter(x => !(x.getAttribute('aria-label')||x.innerText||'').trim()).length,
    focusable: bs.filter(x => { x.focus(); return document.activeElement === x; }).length,
    live: (document.getElementById('ares').getAttribute('aria-live')||''),
    acts: bs.map(x => x.dataset.act),
  };
}"""


# 「面板已加载完」的判据：容器存在且正文里不再有加载态文案。
SETTLED_REC = ("() => {const b=document.getElementById('recs');"
               "return !!b && !/正在读|正在推导/.test(b.textContent);}")
SETTLED_PROMO = ("() => {const b=document.getElementById('promo');"
                 "return !!b && !/正在读/.test(b.textContent);}")
SETTLED_DIST = ("() => {const b=document.getElementById('dist');"
                "return !!b && !/正在读/.test(b.textContent);}")


@gate("真浏览器：跨 run 复发面板真渲染出簇（有数据，不是空壳）")
def g_ui_recurrence(ctx):
    from playwright.sync_api import sync_playwright
    js = io.open(os.path.join(HERE, "recui.js"), encoding="utf-8").read()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(ctx["url"], timeout=NAV_TIMEOUT_MS)
        pg.wait_for_timeout(1500)
        pg.evaluate("() => document.getElementById('t-eng').click()")
        # v118 修正：固定 sleep 判「空壳」会把「还在加载」误判成缺陷。
        # 改成等**真实状态**（面板不再停在加载态），加载态真出不来才红。
        try:
            pg.wait_for_function(SETTLED_REC, timeout=30000)
        except Exception:
            pass
        info = pg.evaluate(js)
        b.close()
    ok = (info["rows"] > 0 and not info["loading"] and info["labelled"]
          and info["hasH"] and "不可用" not in info["text"])
    return ok, "簇行 %d · 标题=%s · aria-labelledby=%s%s" % (
        info["rows"], info["ct"][:56], info["labelled"],
        " · 仍停在加载态" if info["loading"] else "")


PROMO_PROBE = (
    "import importlib.util;"
    "s=importlib.util.spec_from_file_location('c', %r);"
    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m);"
    "r=m._load_ledger();"
    "print(len(r), sum(1 for v in r.values() if v.get('status')=='PROMOTED'))"
) % os.path.join(COMPANY, "capability.py")


@gate("晋升面：/__promote 的总数/hold 数 == 引擎账本直读（不许台面自算）")
def g_promote_truth(ctx):
    code, j = get_json(ctx["port"], "/__promote")
    if code != 200 or not j.get("ok"):
        return False, "HTTP %d ok=%s %s" % (code, j.get("ok"), j.get("error"))
    p = subprocess.run([sys.executable, "-c", PROMO_PROBE],
                       capture_output=True, text=True, timeout=150)
    if p.returncode != 0:
        return False, "引擎侧直读账本失败 rc=%d" % p.returncode
    total, hold = (int(x) for x in p.stdout.split())
    ok = (j.get("total") == total and j.get("n_hold") == hold
          and j.get("n_ready") == 0 and len(j.get("rows") or []) == total
          and j.get("recurrence") is not None)
    return ok, "台面 total=%s hold=%s · 引擎直读 total=%d hold=%d" % (
        j.get("total"), j.get("n_hold"), total, hold)


PROMO_FAILCLOSED_PROBE = (
    "import sys; sys.path.insert(0, %r); import serve;"
    "serve.COMPANY='/nonexistent/cd';"
    "p=serve.promote_payload();"
    "print('OK' if (p['ok'] is False and p['rows']==[] and p['n_ready']==0) else 'BAD')"
) % os.path.join(ROOT, "tools")


@gate("晋升面 fail-closed：引擎取不到时报不可用，不许空面板冒充零阻塞")
def g_promote_failclosed(ctx):
    p = subprocess.run([sys.executable, "-c", PROMO_FAILCLOSED_PROBE],
                       capture_output=True, text=True, timeout=150)
    tail = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0 and "OK" in tail, "坏 COMPANY 路径 -> %s（期望 OK）" % tail[:60]


@gate("真浏览器：能力晋升面真渲染出能力行（两尺寸，无横向溢出）")
def g_ui_promote(ctx):
    from playwright.sync_api import sync_playwright
    js = io.open(os.path.join(HERE, "promoui.js"), encoding="utf-8").read()
    res = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        for w in (1440, 375):
            pg = b.new_page(viewport={"width": w, "height": 900})
            pg.goto(ctx["url"], timeout=NAV_TIMEOUT_MS)
            pg.wait_for_timeout(1500)
            pg.evaluate("() => document.getElementById('t-eng').click()")
            try:
                pg.wait_for_function(SETTLED_PROMO, timeout=30000)
            except Exception:
                pass
            info = pg.evaluate(js)
            pg.close()
            ok = (info["rows"] > 0 and info["marks"] > 0 and not info["loading"]
                  and info["labelled"] and info["hasH"]
                  and not info["unavailable"] and not info["overflow"])
            res.append((w, ok, info))
        b.close()
    bad = [w for w, ok, _ in res if not ok]
    d1440 = res[0][2]
    return not bad, "1440 行 %d 溢出=%s · 375 行 %d 溢出=%s · 标题=%s%s" % (
        res[0][2]["rows"], res[0][2]["overflow"], res[1][2]["rows"],
        res[1][2]["overflow"], d1440["ct"][:44],
        "" if not bad else " · 红尺寸 %s" % bad)


DIST_TRUTH_PROBE = (
    "import importlib.util;"
    "s=importlib.util.spec_from_file_location('dl', %r);"
    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m);"
    "ok,d=m.verify();"
    "both=sum(1 for l in open(m.DEFAULT_OUT,encoding='utf-8') "
    "if l.startswith('|') and '---' not in l and not l.startswith('| repo') "
    "and l.rstrip().endswith('双证据 |'));"
    "print(int(ok), both, m.TARGET_BOTH)"
) % os.path.join(COMPANY, "distill_ledger.py")


@gate("蒸馏面：/__distill 的双证据数 == 引擎账本直读（不许台面自算）")
def g_distill_truth(ctx):
    code, j = get_json(ctx["port"], "/__distill")
    if code != 200 or not j.get("ok"):
        return False, "HTTP %d ok=%s %s" % (code, j.get("ok"), j.get("error"))
    p = subprocess.run([sys.executable, "-c", DIST_TRUTH_PROBE],
                       capture_output=True, text=True, timeout=150)
    if p.returncode != 0:
        return False, "引擎侧直读账本失败 rc=%d %s" % (p.returncode, p.stderr[-80:])
    vok, both, target = (int(x) for x in p.stdout.split())
    ok = (j.get("n_both") == both and j.get("target") == target
          and bool(j.get("verify_ok")) is bool(vok))
    return ok, "台面 双证据=%s 目标=%s verify=%s · 引擎直读 双证据=%d 目标=%d verify=%d" % (
        j.get("n_both"), j.get("target"), j.get("verify_ok"), both, target, vok)


DIST_FAILCLOSED_PROBE = (
    "import sys; sys.path.insert(0, %r); import serve;"
    "serve.COMPANY='/nonexistent/cd';"
    "p=serve._distill_compute();"
    "print('OK' if (p['ok'] is False and p['rows']==[] and p['n_both']==0) else 'BAD')"
) % os.path.join(ROOT, "tools")


@gate("蒸馏面 fail-closed：引擎账本取不到时报不可用，不许空面板冒充双证据齐全")
def g_distill_failclosed(ctx):
    p = subprocess.run([sys.executable, "-c", DIST_FAILCLOSED_PROBE],
                       capture_output=True, text=True, timeout=150)
    tail = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0 and "OK" in tail, "坏 COMPANY 路径 -> %s（期望 OK）" % tail[:60]


@gate("真浏览器：蒸馏证据面真渲染出账本行（两尺寸，无横向溢出）")
def g_ui_distill(ctx):
    from playwright.sync_api import sync_playwright
    js = io.open(os.path.join(HERE, "distui.js"), encoding="utf-8").read()
    res = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        for w in (1440, 375):
            pg = b.new_page(viewport={"width": w, "height": 900})
            pg.goto(ctx["url"], timeout=NAV_TIMEOUT_MS)
            pg.wait_for_timeout(1500)
            pg.evaluate("() => document.getElementById('t-eng').click()")
            try:
                pg.wait_for_function(SETTLED_DIST, timeout=30000)
            except Exception:
                pass
            info = pg.evaluate(js)
            pg.close()
            ok = (info["rows"] > 0 and not info["loading"]
                  and info["labelled"] and info["hasH"]
                  and not info["unavailable"] and not info["overflow"])
            res.append((w, ok, info))
        b.close()
    bad = [w for w, ok, _ in res if not ok]
    d = res[0][2]
    return not bad, "1440 行 %d 溢出=%s · 375 行 %d 溢出=%s · 首行=%s%s" % (
        res[0][2]["rows"], res[0][2]["overflow"], res[1][2]["rows"],
        res[1][2]["overflow"], d["first"][:44],
        "" if not bad else " · 红尺寸 %s" % bad)


@gate("真浏览器：执行台按钮可聚焦、有可访问名")
def g_ui_buttons(ctx):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(ctx["url"], timeout=NAV_TIMEOUT_MS)
        pg.wait_for_timeout(1500)
        pg.evaluate("() => document.getElementById('t-eng').click()")
        pg.wait_for_timeout(500)
        info = pg.evaluate(UI_INFO_JS)
        b.close()
    ok = info["n"] >= 5 and info["unnamed"] == 0
    ok = ok and info["focusable"] == info["n"] and info["live"] == "polite"
    return ok, "按钮 %d，无名 %d，可聚焦 %d，aria-live=%s，%s" % (
        info["n"], info["unnamed"], info["focusable"], info["live"],
        ",".join(info["acts"]))


@gate("流式：动作跑的过程中逐行推，不是跑完才一次性给")
def g_stream(ctx):
    port = ctx["port"]
    got = []
    err = []

    def reader():
        try:
            r = urllib.request.urlopen(
                "http://127.0.0.1:%d/__act/stream" % port, timeout=90)
            t0 = time.time()
            while True:
                line = r.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if s.startswith("data: "):
                    got.append((round(time.time() - t0, 2), s[6:]))
                if s.startswith("data: __RC__"):
                    break
        except Exception as e:                # 订阅断了就是失败，不是通过
            err.append("%s: %s" % (type(e).__name__, e))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(1.0)                           # 让订阅先立住
    code, j = post(port, {"action": SAFE_ACT}, timeout=120)
    th.join(timeout=30)
    data = [d for _, d in got]
    rc_tail = [d for _, d in got if d.startswith("__RC__")]
    body = [d for d in data if d != "stream" and not d.startswith("__RC__")]
    ok = code == 200 and len(body) >= 2 and rc_tail and not err
    ok = ok and rc_tail[0].split()[-1] == str(j.get("rc"))
    return ok, "HTTP=%s 实收到 %d 行内容，终局=%s，err=%s，前 3 行=%s" % (
        code, len(body), rc_tail[:1], err[:1], body[:3])


@gate("决策：推荐动作必须真在 allowlist 里，且都带证据")
def g_recommend(ctx):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ag_serve", os.path.join(HERE, "serve.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)              # 纯函数模块，import 不起服务
    with urllib.request.urlopen("http://127.0.0.1:%d/__act/recommend" % ctx["port"],
                                timeout=10) as r:
        live = json.load(r)
    recs = live.get("recommend") or []
    ok = live.get("ok") is True and len(recs) >= 1
    ok = ok and all(x["action"] in m.ACTIONS for x in recs)
    ok = ok and all(x.get("evidence") and x.get("why") for x in recs)
    clean = {"aggregate": {"FAIL": 0, "HONESTY_VIOLATION": 0, "TIMEOUT": 0,
                           "ORPHAN_SHELL": 0, "MANIFEST_INVALID": 0,
                           "SELF_REPORT_PASS_BUT_RUN_FAILED": 0},
             "divergence": [], "env_gated": [],
             "independent": {"n_mismatches": 0},
             "manifest": {"missing": []}}
    quiet = m.recommend(clean)               # 反证：全绿必须零条
    ok = ok and quiet == []
    return ok, "实测 %d 条 %s；全绿样本 %d 条（应 0）" % (
        len(recs), [x["action"] for x in recs][:4], len(quiet))


@gate("真浏览器：建议按钮可聚焦、有可访问名、且真能驱动执行")
def g_ui_recommend(ctx):
    from playwright.sync_api import sync_playwright
    js = "() => {const bs=[...document.querySelectorAll('#recs button[data-rec]')];return {n:bs.length,unnamed:bs.filter(x=>!(x.getAttribute('aria-label')||x.innerText||'').trim()).length,focusable:bs.filter(x=>{x.focus();return document.activeElement===x;}).length,acts:bs.map(x=>x.dataset.rec)};}"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(ctx["url"], timeout=NAV_TIMEOUT_MS)
        pg.wait_for_timeout(1800)
        pg.evaluate("() => document.getElementById('t-eng').click()")
        pg.wait_for_timeout(600)
        info = pg.evaluate(js)
        seen = ""
        if info["n"]:
            pg.evaluate("(a) => document.querySelector('#recs button[data-rec=\"' + a + '\"]').click()",
                        info["acts"][0])
            for _ in range(90):
                pg.wait_for_timeout(500)
                seen = pg.evaluate("() => document.getElementById('ares').innerText")
                if "rc=" in seen:
                    break
        b.close()
    ok = info["n"] >= 1 and info["unnamed"] == 0 and info["focusable"] == info["n"]
    ok = ok and "rc=" in seen
    return ok, "建议 %d 条，无名 %d，可聚焦 %d，点后结果区=%r" % (
        info["n"], info["unnamed"], info["focusable"], seen.replace("\n", " | ")[:70])


def feedback_verdict(before, after, saw_disabled):
    """判据本体：反馈为真 = 结果区内容**变了** 且 新文本里有 rc= 令牌 且 跑过禁用态。

    为什么拆掉原来的 `"rc=0" in seen`（UI 反馈判据解耦）：那把「反馈为真」和「动作成功」
    焊死在一个条件里。实测踩实：引擎任何一处回归让 sample 动作 doc-drift
    返回 rc=2 时，这条判据报的是「aria-live 区真出现 rc」红 —— 诊断指错方向。
    反馈其实完好地渲染出了 `DOC-DRIFT | rc=2 | 0.73S`，真病因是引擎回归，
    却被这条判据改写成「UI 反馈是画的」，下一轮只会去查一个不存在的 aria bug。
    动作成败不该由这条判据负责：doc-drift 的真源是 doc_drift.py / count_sync.py，
    它们红就红在那里，门不该把引擎回归翻译成 UI 缺陷。
    """
    b_txt, a_txt = (before or "").strip(), (after or "").strip()
    return bool(a_txt != b_txt and "rc=" in a_txt and saw_disabled)


@gate("判据自证：反馈判据 4 条负控都能变红 + 「旧规则误诊」反证")
def g_feedback_selftest(ctx):
    """门内嵌变异体（沿用 indep_field_gate 的做法）：证明 feedback_verdict 有牙，
    并把「旧规则把 rc=2 误判成 UI 缺陷」这件事固化成可复算的反证。"""
    cases = [
        ("BASE 本轮 rc=0", "", "DOC-DRIFT | rc=0 | 0.7S", True, True),
        ("BASE 本轮 rc=2（修复点）", "", "DOC-DRIFT | rc=2 | 0.7S", True, True),
        ("N1 结果区零变化", "rc=0", "rc=0", True, False),
        ("N2 从未出现禁用态", "", "DOC | rc=0", False, False),
        ("N3 陈旧 rc=0 冒充本轮", "DOC | rc=0", "DOC | rc=0", True, False),
        ("N4 变了但无 rc= 令牌", "", "EGATE | ok", True, False),
    ]
    bad = [n for n, bf, af, dis, want in cases
           if feedback_verdict(bf, af, dis) is not want]
    # 变异体：把判据掏空成两种典型退化，必须各自被对应负控抓到。
    muts = [
        ("M-A 漏掉 saw_disabled",
         lambda bf, af, dis: feedback_verdict(bf, af, True), "N2"),
        ("M-B 漏掉「内容变化」只看 rc=",
         lambda bf, af, dis: "rc=" in (af or ""), "N1"),
    ]
    mut_bad, mut_says = [], []
    for mname, mfn, _ in muts:
        broke = [n for n, bf, af, dis, want in cases if mfn(bf, af, dis) is not want]
        mut_says.append("%s 破 %d 条" % (mname, len(broke)))
        if not broke:
            mut_bad.append(mname)
    # 反证：旧规则在「本轮 rc=2」这一条上确实误判成 UI 缺陷。
    old_says_ok = ("rc=0" in "DOC-DRIFT | rc=2 | 0.7S") and True
    ok = not bad and not mut_bad and old_says_ok is False
    why = ("判据 %d/%d 咬合 · 变异体 %d/%d 有牙 · 旧规则(rc=0 焊死)对 rc=2 给 %s · %s"
           % (len(cases) - len(bad), len(cases), len(muts) - len(mut_bad), len(muts),
              "True(误诊为 UI 缺陷)" if old_says_ok else "False(误诊已消除)", "; ".join(mut_says)))
    if bad:
        why += " · 判据错=" + ",".join(bad)
    if mut_bad:
        why += " · 无牙变异体=" + ",".join(mut_bad)
    return ok, why


@gate("真浏览器：点一下，aria-live 区真出现 rc（反馈不是画出来的）")
def g_ui_feedback(ctx):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(ctx["url"], timeout=NAV_TIMEOUT_MS)
        pg.wait_for_timeout(1500)
        pg.evaluate("() => document.getElementById('t-eng').click()")
        pg.wait_for_timeout(400)
        # 先清空结果区取基线。旧判据只做 `in seen` 判存在，于是「跑完还留着
        # 上一轮的 rc=0」也能过 —— 那是陈旧反馈冒充本轮反馈（负控 N3）。
        before = pg.evaluate(
            "() => { var e = document.getElementById('ares');"
            " e.innerText = ''; return e.innerText; }")
        pg.evaluate("(a) => document.querySelector('#actgrid button[data-act=\"' + a + '\"]').click()",
                    SAFE_ACT)
        saw_disabled = False
        seen = ""
        for _ in range(90):
            pg.wait_for_timeout(500)
            if pg.evaluate("() => !!document.querySelector('#actgrid button:disabled')"):
                saw_disabled = True
            seen = pg.evaluate("() => document.getElementById('ares').innerText")
            if "rc=" in seen:
                break
        b.close()
    ok = feedback_verdict(before, seen, saw_disabled)
    return ok, "基线=%r 禁用态=%s 结果区=%r" % (
        before, saw_disabled, seen.replace("\n", " | ")[:100])


def main():
    port = free_port()
    fake = os.path.join(ROOT, "work", "_actgate_tree")
    os.makedirs(fake, exist_ok=True)
    srv = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "serve.py"),
         "--port", str(port), "--tree", fake, "--poll", "3600"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctx = {"port": port, "url": "http://127.0.0.1:%d/index.html" % port}
    report = {"tool": "act_gate.py", "port": port, "gates": []}
    try:
        up = False
        for _ in range(60):
            try:
                get_health(port, timeout=2)
                up = True
                break
            except Exception:
                time.sleep(0.25)
        if not up:
            print("FAIL: 服务 15s 内没起来")
            return 2
        npass = 0
        for name, fn in GATES:
            ok, detail = run_gate(fn, ctx, name)
            report["gates"].append({"name": name, "ok": bool(ok), "detail": detail})
            print("  %s %s  %s" % ("[ok  ]" if ok else "[FAIL]", name, detail))
            npass += bool(ok)
        total = len(GATES)
        report["passed"], report["total"] = npass, total
        with open(os.path.join(ROOT, "work", "act_gate.json"), "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print("== ACT_GATE %s  %d/%d 门通过（真 HTTP + 真浏览器点按）==" % (
            "OK " if npass == total else "FAIL", npass, total))
        return 0 if npass == total else 2
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    sys.exit(main())
