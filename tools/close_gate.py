#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D79 收轮就绪度预检（闭环第三环）的真浏览器门。

为什么必须单独一道门（round_gate 判不到的缺陷）：
    * round_gate 到「引擎逐字告诉你下一轮该做什么」为止。闭环**第三环**
      （能不能收这一轮、缺哪一项）在它的屏级判据下完全不可见 ——
      没点「预检」按钮时，台面缺一整块功能，view_dod / a11y_walk 全绿；
    * 更要命的是**假就绪**：预检屏完全可以自己数一遍红绿、把 BLOCK 画成 PASS，
      看上去和引擎判的很像，但那是台面在替引擎下结论。所以本门核心判据是
      **屏上逐条 gate 的过没过 == 后端 close_preflight 真算的那份**，
      且就绪时必须给出**引擎原样的一行 close-round 命令**、不就绪时**绝不给命令**。

诚实负例是另一半：delta 占位 / evidence 口头断言 / 忘了填裁决 / SHIP 时还有
未收轮 / AC 越界 / 目录没 set-goal / 路径越界 —— 每一条都必须报出**为什么**，
并且不就绪时绝不递出一条注定 rc=2 的命令。

只读自证：预检跑完，夹具与活根的 work/GOAL.json 的 sha256 必须与跑前逐位一致；
三个变异体全走 route 拦 served 字节，活的 index.html 零写入。

上牙（三个变异体）：
    M1 抽掉 pdraw(j)        -> 就绪度屏上空白，门必须红；
    M2 ready 写死 true      -> 负例被画成 PASS 就绪，门必须红；
    M3 gate.ok 写死 true    -> 每条判据都显示 PASS，门必须红。

用法：python3 tools/close_gate.py   # rc=0 全过 / 1 有红项 / 2 环境不可用
"""
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVE_PY = os.path.join(HERE, "serve.py")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WORK = os.path.join(ROOT, "work")

# 夹具任务空间：R1 OPEN、3 条 AC、R2 待办（所以 SHIP 必须判红）。
FIXTURE = "work/tasks/d79fix"
# 引擎 GF_RUNS 必须在**导入 goal_loop 那一刻**就对，否则 bind/giga 判的是
# ~/.codex/gf-runs/artifacts 而不是夹具账 —— 正例会假红。serve._goal_loop() 是
# 惰性导入，所以这里在起服务前把 GF_OUT 指过去就够。
GF_FIXTURE = os.path.join(ROOT, "work", "gf-d79")

DELTA_OK = ("AC1 台面新增 #gpre 收轮就绪度：disk 族与 draft 族逐条显示过没过，"
            "缺哪条写清 why")
EV_OK = ("python3 tools/build.py rc=0 BUILD_OK；python3 tools/close_gate.py rc=0 "
         "CLOSE_GATE OK；python3 tools/gates.sh rc=0")
VD_OK = "CONTINUE"
ACM_OK = "AC1"

DELTA_BAD = "无"
EV_HEDGE_BAD = "应该已经修好了，看起来没问题"
VD_SHIP = "SHIP"
NO_DIR = "work/gf-d79"

# 变异体锚点：必须**恰好命中一次**。命中 0 次说明源码漂移、门自己失效了 ——
# 那比红更坏，所以漂移直接判 FAIL 而不是静默跳过。
ANCHOR_DRAW = "PLLAST=j; pdraw(j);"
# M2 锚在**用户真读的那枚徽章**上，不是卡片 CSS class：把 class 写死只改边框颜色，
# 屏上文字仍然写着「BLOCK 还不能收」—— 那不是用户会信的谎。锚徽章才是真上牙。
ANCHOR_BADGE = ("`<span class=\"gst ${j.ready?'pass':'block'}\">"
                "${j.ready?'PASS 就绪':'BLOCK 还不能收'}</span>`")
FAKE_BADGE = "`<span class=\"gst pass\">PASS 就绪</span>`"
ANCHOR_GST = ("`<span class=\"gst ${g.ok?'pass':'block'}\">"
              "${g.ok?'PASS':'BLOCK'}</span>`")
FAKE_GST = "`<span class=\"gst pass\">PASS</span>`"

G = {}
WHY = {}


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


def load_serve(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("d79_close_serve", path)
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


def post_chain(url, body, csrf=True):
    """直调后端（越权负例用；不经浏览器 UI）。返回 (http_code, obj)。"""
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if csrf:
        h["X-AgentOS-Action"] = "1"
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


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


def wait_sel(pg, sel, timeout=90.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pg.locator(sel).count() > 0:
            time.sleep(0.4)
            return True
        time.sleep(0.25)
    return False


def wait_txt(pg, sel, needle, timeout=60.0):
    """等某个选择器的文本出现 needle。用来判「请求真跑完了」而不是死等超时。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            txt = pg.locator(sel).text_content() or ""
        except Exception:                                     # noqa: BLE001
            txt = ""
        if needle in txt:
            return True
        time.sleep(0.25)
    return False


READ_PRE_JS = """() => {
  const box = document.querySelector('#gpre');
  const cards = [...document.querySelectorAll('#gpre .gcard')];
  return {
    hint: (document.querySelector('#gphint')||{}).textContent || '',
    nCards: cards.length,
    cardClass: cards.length ? cards[0].className : '',
    cardText: cards.length ? cards[0].textContent : '',
    heads: [...document.querySelectorAll('#gpre h4')].map(h=>h.textContent||''),
    rows: [...document.querySelectorAll('#gpre .glist li')].map(li=>({
      ok: li.getAttribute('data-ok'),
      name: (li.querySelector('.gn')||{}).textContent||'',
      tag: (li.querySelector('.gst')||{}).textContent||'',
      why: (li.querySelector('.gwhy')||{}).textContent||''
    })),
    argv: (document.querySelector('#gpargv')||{}).textContent||'',
    meta: [...document.querySelectorAll('#gpre .gmeta span')].map(s=>s.textContent||'')
  };
}"""


def read_pre(pg):
    """只读 DOM。刻意不读 window.__agentos.preflightLast 之类 JS 变量：
    抽掉 pdraw(j) 之后变量照样是满的、屏上是一片空白；读变量就会放过
    「用户什么都看不见」这个最严重的缺陷。变量与 DOM 的差距正是本门存在的理由。
    """
    return pg.evaluate(READ_PRE_JS)


def open_goal_tab(pg, url):
    pg.goto(url, wait_until="load")
    pg.click('[data-v="goal"]')
    pg.wait_for_selector("#gdmd", state="visible")


def run_pre(pg, td, n, delta, ev, vd, acm):
    """全部走页面自己的输入框与按钮 —— 不调 preflight()、不打后门。
    打后门会把「input 有没有绑上」「按钮是不是真能点」一起放过，
    而这两件恰好是用户最先撞到的（填了字点不动）。"""
    pg.fill("#gdir", td)
    pg.fill("#gpn", str(n))
    pg.fill("#gpdl", delta)
    pg.fill("#gpev", ev)
    pg.select_option("#gpvd", vd)
    pg.fill("#gpa", acm)
    pg.click("#gcpf")
    return wait_sel(pg, "#gpre .gcard")


def backend(url, td, n, delta, ev, vd, acm):
    api = url.replace("index.html", "") + "__goal/chain"
    return post_chain(api, {"op": "close-preflight", "taskdir": td, "round": n,
                            "delta": delta, "evidence": ev,
                            "verdict": vd, "ac_moved": acm})


def gates_by_id(obj):
    return {g["id"]: g for g in (obj.get("gates") or [])}


def api_of(url):
    return url.replace("index.html", "") + "__goal/chain"


# ---- 正例 ----
def run_positive(pg, url):
    print("== P. 正例：夹具 R1 OPEN + 真 delta/evidence/裁决 -> 就绪 + 给命令 ==")
    open_goal_tab(pg, url)
    code, obj = backend(url, FIXTURE, 1, DELTA_OK, EV_OK, VD_OK, ACM_OK)
    add("p_e1_backend_ok", code == 200 and obj.get("ok") and obj.get("ready"),
        "后端 close-preflight 拿到 %s ready=%r error=%r"
        % (code, obj.get("ready"), obj.get("error")))
    add("p_e2_backend_readonly", obj.get("readonly") is True,
        "后端没说自己是只读：readonly=%r" % (obj.get("readonly"),))
    add("p_e2b_engine_is_truth", (obj.get("gf_runs") or "").endswith("work/gf-d79"),
        "判据用的账目录不是夹具账：gf_runs=%r" % (obj.get("gf_runs"),))
    bg = gates_by_id(obj)
    add("p_e3_disk_family_all_green",
        all(bg.get(k, {}).get("ok") for k in
            ("goal_json", "round-file", "opened", "not-closed", "row", "bind", "giga")),
        "disk 族有红：%r" % {k: v.get("ok") for k, v in bg.items()
                            if v.get("kind") == "disk"})
    add("p_e4_draft_family_all_green",
        all(bg.get(k, {}).get("ok") for k in
            ("verdict-field", "delta", "ev-junk", "ev-result", "ev-hedge", "ac")),
        "draft 族有红：%r" % {k: v.get("ok") for k, v in bg.items()
                             if v.get("kind") == "draft"})
    add("p_e5_both_families_present",
        bool([g for g in obj.get("gates") or [] if g.get("kind") == "disk"]) and
        bool([g for g in obj.get("gates") or [] if g.get("kind") == "draft"]),
        "gates 里没有 disk/draft 两族：%r"
        % [g.get("kind") for g in obj.get("gates") or []])
    add("p_e6_backend_gives_cmd",
        isinstance(obj.get("close_argv"), list) and
        "close-round" in " ".join(obj["close_argv"]),
        "就绪却没给 close-round 命令：%r" % (obj.get("close_argv"),))
    add("p_e7_cmd_is_engine_verbatim",
        isinstance(obj.get("close_argv"), list) and
        obj["close_argv"][:2] == ["bash", os.path.expanduser(
            "~/.codex/global-background/company-delivery/company.sh")],
        "命令不是引擎 company.sh 原样：%r" % ((obj.get("close_argv") or [])[:3],))
    # ---- 屏级 ----
    add("p_e8_card_shown", run_pre(pg, FIXTURE, 1, DELTA_OK, EV_OK, VD_OK, ACM_OK),
        "点「预检」后 #gpre 连卡都没出")
    add("p_e9_wired", wait_txt(pg, "#gtrbox", "close-preflight", timeout=30.0),
        "编译现场没有 close-preflight 记录 -> 请求没真跑")
    d = read_pre(pg)
    add("p_e10_card_pass", "gpass" in (d.get("cardClass") or ""),
        "cardClass=%r" % (d.get("cardClass"),))
    add("p_e11_rows_match_backend",
        len(d.get("rows") or []) == len(obj.get("gates") or []),
        "屏上 %d 条 vs 后端 %d 条"
        % (len(d.get("rows") or []), len(obj.get("gates") or [])))
    want = {g["id"]: g["ok"] for g in obj["gates"]}
    got = {r.get("name"): (r.get("ok") == "1") for r in d.get("rows") or []}
    add("p_e12_row_by_row_identical",
        all(got.get(k) == v for k, v in want.items()),
        "屏上与后端逐条不一致：%r vs %r" % ({k: got.get(k) for k in want}, want))
    add("p_e13_why_not_empty",
        all((r.get("why") or "").strip() for r in d.get("rows") or []),
        "有条判据没写 why：%r" % [r.get("name") for r in d.get("rows") or []
                                 if not (r.get("why") or "").strip()])
    add("p_e14_two_families_drawn",
        any("盘上判据" in h for h in d.get("heads") or []) and
        any("草案" in h for h in d.get("heads") or []),
        "屏上没分 disk/draft 两族：%r" % (d.get("heads"),))
    add("p_e15_argv_shown",
        "close-round" in (d.get("argv") or "") and
        "CONTINUE" in (d.get("argv") or ""),
        "#gpargv=%r" % ((d.get("argv") or "")[:200],))
    add("p_e16_hint_ready", "就绪" in (d.get("hint") or ""),
        "hint=%r" % (d.get("hint") or "")[:160])
    add("p_e17_gf_dir_on_screen",
        any("work/gf-d79" in m for m in d.get("meta") or []),
        "屏上没摆出判的是哪个账目录：meta=%r" % (d.get("meta"),))
    return d


# ---- 诚实负例 ----
def expect_block(pg, tag, needle, backend_id=None, api=None):
    """负例的共同形状：BLOCK 卡 + 红判据 + **没有** close-round 命令。"""
    d = read_pre(pg)
    add("%s_e1_block_card" % tag, "gblock" in (d.get("cardClass") or ""),
        "cardClass=%r" % (d.get("cardClass"),))
    tags = " ".join(r.get("tag") or "" for r in d.get("rows") or [])
    add("%s_e2_has_block" % tag, "BLOCK" in tags, "tags=%r" % tags[:200])
    add("%s_e3_no_argv" % tag, not (d.get("argv") or "").strip(),
        "不就绪却递出一条注定 rc=2 的命令：%r" % ((d.get("argv") or "")[:200],))
    add("%s_e4_hint_block" % tag,
        ("不能收" in (d.get("hint") or "")) or ("BLOCK" in (d.get("hint") or "")),
        "hint=%r" % (d.get("hint") or "")[:160])
    if backend_id:
        add("%s_e5_backend_red" % tag,
            api is not None and gates_by_id(api).get(backend_id, {}).get("ok") is False,
            "后端 %s 这一条竟不是红的：%r"
            % (backend_id, gates_by_id(api).get(backend_id)))
        why = (gates_by_id(api).get(backend_id) or {}).get("why") or ""
        add("%s_e6_why_says_why" % tag, needle and needle in why,
            "没说清为什么红：want=%r got=%r" % (needle, why[:200]))
        onscreen = " ".join((r.get("why") or "") for r in d.get("rows") or [])
        add("%s_e7_why_onscreen" % tag, needle and needle in onscreen,
            "why 没上屏：want=%r" % needle)
    return d


def run_negatives(br, url):
    api = api_of(url)
    ctx, pg = new_page(br, url)
    open_goal_tab(pg, url)

    print("== N1. delta 占位 -> 不予收轮 ==")
    _, b1 = backend(url, FIXTURE, 1, DELTA_BAD, EV_OK, VD_OK, ACM_OK)
    run_pre(pg, FIXTURE, 1, DELTA_BAD, EV_OK, VD_OK, ACM_OK)
    d1 = expect_block(pg, "n1", "占位", "delta", b1)
    add("n1_e8_row_is_delta",
        any((r.get("name") or "") == "delta" and r.get("ok") == "0"
            for r in d1.get("rows") or []),
        "屏上 delta 那条不是红的：%r" % (d1.get("rows"),))
    # M2 那条上牙的**对照组**：同一个负例在活页上绝不能出现「PASS 就绪」徽章。
    # 有这一对，M2 的「变异体下出现假就绪」才真的说明判据有牙。
    add("n1_e9_no_fake_ready_badge", "PASS 就绪" not in (d1.get("cardText") or ""),
        "活页上占位 delta 竟显示 PASS 就绪：cardText=%r"
        % (d1.get("cardText") or "")[:160])

    print("== N2. evidence 口头断言 -> 不是证据 ==")
    _, b2 = backend(url, FIXTURE, 1, DELTA_OK, EV_HEDGE_BAD, VD_OK, ACM_OK)
    run_pre(pg, FIXTURE, 1, DELTA_OK, EV_HEDGE_BAD, VD_OK, ACM_OK)
    expect_block(pg, "n2", "口头断言", "ev-hedge", b2)

    print("== N3. 忘了填裁决 -> 引擎会 rc=2 ==")
    _, b3 = backend(url, FIXTURE, 1, DELTA_OK, EV_OK, "", ACM_OK)
    run_pre(pg, FIXTURE, 1, DELTA_OK, EV_OK, "", ACM_OK)
    expect_block(pg, "n3", "verdict", "verdict-field", b3)

    print("== N4. verdict=SHIP 但 R2 还待办 -> 不许提前报完成 ==")
    _, b4 = backend(url, FIXTURE, 1, DELTA_OK, EV_OK, VD_SHIP, ACM_OK)
    run_pre(pg, FIXTURE, 1, DELTA_OK, EV_OK, VD_SHIP, ACM_OK)
    expect_block(pg, "n4", "未收", "ship-pending", b4)

    print("== N5. delta 没指名动了哪条 AC -> 无法归因 ==")
    _, b5 = backend(url, FIXTURE, 1, "把一堆功能都改了", EV_OK, VD_OK, "")
    run_pre(pg, FIXTURE, 1, "把一堆功能都改了", EV_OK, VD_OK, "")
    expect_block(pg, "n5", "指名", "ac", b5)

    print("== N6. AC 指名越界（本项目只有 AC1..AC3）-> 非法 ==")
    _, b6 = backend(url, FIXTURE, 1, DELTA_OK, EV_OK, VD_OK, "AC9")
    run_pre(pg, FIXTURE, 1, DELTA_OK, EV_OK, VD_OK, "AC9")
    expect_block(pg, "n6", "AC9", "ac", b6)

    print("== N7. 活项目根：R1..R5 全 CLOSED -> 已关闭，不许重复收轮 ==")
    _, b7 = backend(url, "", 1, DELTA_OK, EV_OK, VD_OK, ACM_OK)
    run_pre(pg, "", 1, DELTA_OK, EV_OK, VD_OK, ACM_OK)
    d7 = expect_block(pg, "n7", "已关闭", "not-closed", b7)
    add("n7_e8_live_goal_named",
        "GOAL_SHIPPED" in (d7.get("cardText") or ""),
        "屏上没摆活根目标状态：cardText=%r" % (d7.get("cardText") or "")[:200])

    print("== N8. 目录没 set-goal -> 读不到判据就不给就绪度（fail-closed） ==")
    _, b8 = backend(url, NO_DIR, 1, DELTA_OK, EV_OK, VD_OK, ACM_OK)
    run_pre(pg, NO_DIR, 1, DELTA_OK, EV_OK, VD_OK, ACM_OK)
    d8 = read_pre(pg)
    add("n8_e1_block_card", "gblock" in (d8.get("cardClass") or ""),
        "cardClass=%r" % (d8.get("cardClass"),))
    add("n8_e2_backend_not_ok", b8.get("ok") is False and not b8.get("ready"),
        "后端 %r" % {k: b8.get(k) for k in ("ok", "ready", "error")})
    add("n8_e3_says_setgoal", "set-goal" in (b8.get("error") or ""),
        "error=%r" % (b8.get("error"),))
    add("n8_e4_onscreen_setgoal", "set-goal" in (d8.get("hint") or ""),
        "屏上没说清要 set-goal：hint=%r" % (d8.get("hint") or "")[:160])
    add("n8_e5_no_argv", not (d8.get("argv") or "").strip(),
        "读不到盘却递了命令：%r" % ((d8.get("argv") or "")[:200],))
    add("n8_e6_no_fake_rows", not (d8.get("rows") or []),
        "读不到盘却画出了判据表：%r" % (d8.get("rows"),))
    pg.close()
    ctx.close()

    print("== B. 后端越权负例：四道硬门必须在**服务端**就挡 ==")
    for op in ("close-round", "bind-goal", "plan-round"):
        code, obj = post_chain(api, {"op": op, "taskdir": "", "round": 1})
        add("b_%s_400" % op, code == 400 and not obj.get("ok"),
            "写操作 %s 拿到 %s %r" % (op, code, obj))
        add("b_%s_says_readonly" % op, "只读" in (obj.get("error") or ""),
            "回执没说清为什么拒：%r" % (obj.get("error"),))
    code, obj = post_chain(api, {"op": "close-preflight", "taskdir": "",
                                 "round": 1, "delta": DELTA_OK, "evidence": EV_OK,
                                 "verdict": VD_OK}, csrf=False)
    add("b_no_csrf_403", code == 403 and not obj.get("ok"),
        "无 X-AgentOS-Action 头拿到 %s %r" % (code, obj))
    code, obj = post_chain(api, {"op": "close-preflight", "taskdir": "",
                                 "round": 99, "delta": DELTA_OK, "evidence": EV_OK,
                                 "verdict": VD_OK})
    add("b_round_range_400", code == 400 and "round" in (obj.get("error") or ""),
        "round=99 拿到 %s %r" % (code, obj))
    code, obj = post_chain(api, {"op": "close-preflight", "taskdir": "/etc",
                                 "round": 1, "delta": DELTA_OK, "evidence": EV_OK,
                                 "verdict": VD_OK})
    add("b_abs_path_400", code == 400 and "越界" in (obj.get("error") or ""),
        "taskdir=/etc 拿到 %s %r" % (code, obj))
    code, obj = post_chain(api, {"op": "close-preflight",
                                 "taskdir": "../../../../etc",
                                 "round": 1, "delta": DELTA_OK, "evidence": EV_OK,
                                 "verdict": VD_OK})
    add("b_dotdot_400", code == 400 and "越界" in (obj.get("error") or ""),
        "taskdir=../../../../etc 拿到 %s %r" % (code, obj))


# ---- 上牙 ----
def wired(pg, repl, tag):
    """自证变异真生效：屏上字节里必须能看到替换后的那段。
    一个无效变异会让门看起来有牙、实际没牙，那比没有门更坏。"""
    got = pg.evaluate("() => document.documentElement.outerHTML")
    return add("%s_e0_wired" % tag, repl in (got or ""),
               "屏上找不到替换后的 %r -> 变异无效" % repl[:80])


# D51：三个变异体各自拆成独立函数 + 目标被回收时重建 context/page 重试。
# 起因：Playwright 的 TargetClosedError 在 context 被回收时抛出，原实现让整个门
# 崩掉且判据丢失；重试只在「目标被回收」这一种错误上放宽，其余异常照旧抛出，
# 判据本身一个阈值都没动（重试不是放过，是换一副干净的靶子再打同一发）。
MUTANT_ATTEMPTS = 3


def _is_target_closed(e):
    return type(e).__name__ == "TargetClosedError"


def _drop_prefix(prefix):
    for k in [k for k in G if k.startswith(prefix)]:
        G.pop(k, None)
        WHY.pop(k, None)


def mutant_m1(br, url):
    print("== M1 上牙：抽掉 pdraw(j) -> 就绪度屏上空白，门必须红 ==")
    ctx, pg = new_page(br, url, ANCHOR_DRAW, "PLLAST=j;")
    open_goal_tab(pg, url)
    wired(pg, "PLLAST=j;", "m1")
    run_pre(pg, FIXTURE, 1, DELTA_OK, EV_OK, VD_OK, ACM_OK)
    add("m1_e1_wired_run", wait_txt(pg, "#gtrbox", "close-preflight", timeout=30.0),
        "变异体下连编译现场都没进 -> 变异无效")
    add("m1_e2_nothing_painted", pg.locator("#gpre .gcard").count() == 0,
        "抽掉 pdraw 后屏上仍有卡 -> 正例判据没牙")
    pg.close()
    ctx.close()


def mutant_m2(br, url):
    print("== M2 上牙：就绪徽章写死 PASS -> 负例被说成能收，门必须红 ==")
    ctx, pg = new_page(br, url, ANCHOR_BADGE, FAKE_BADGE)
    open_goal_tab(pg, url)
    wired(pg, FAKE_BADGE, "m2")
    _, b = backend(url, FIXTURE, 1, DELTA_BAD, EV_OK, VD_OK, ACM_OK)
    run_pre(pg, FIXTURE, 1, DELTA_BAD, EV_OK, VD_OK, ACM_OK)
    d = read_pre(pg)
    add("m2_e1_fake_ready_badge_shown", "PASS 就绪" in (d.get("cardText") or ""),
        "变异体下占位 delta 的卡上没有「PASS 就绪」徽章 -> 变异无效：cardText=%r"
        % (d.get("cardText") or "")[:160])
    # 「假就绪」在屏上的签名不是卡片变色，是**自相矛盾**：同一块屏上，
    # 徽章说「PASS 就绪」、hint 说「还不能收」。用户读哪一句都会被误导。
    add("m2_e2_screen_self_contradicts",
        "PASS 就绪" in (d.get("cardText") or "") and
        "不能收" in (d.get("hint") or ""),
        "变异体下没出现自相矛盾屏：cardText=%r hint=%r"
        % ((d.get("cardText") or "")[:120], (d.get("hint") or "")[:120]))
    add("m2_e3_backend_still_red",
        gates_by_id(b).get("delta", {}).get("ok") is False,
        "后端 delta 这一条竟不是红的 -> 判据没牙")
    pg.close()
    ctx.close()


def mutant_m3(br, url):
    print("== M3 上牙：gate.ok 写死 true -> 每条判据都显示 PASS，门必须红 ==")
    ctx, pg = new_page(br, url, ANCHOR_GST, FAKE_GST)
    open_goal_tab(pg, url)
    wired(pg, FAKE_GST, "m3")
    _, b = backend(url, FIXTURE, 1, DELTA_BAD, EV_OK, VD_OK, ACM_OK)
    run_pre(pg, FIXTURE, 1, DELTA_BAD, EV_OK, VD_OK, ACM_OK)
    d = read_pre(pg)
    tags = " ".join(r.get("tag") or "" for r in d.get("rows") or [])
    add("m3_e1_all_pass_observed", "BLOCK" not in tags,
        "变异体下仍有 BLOCK 标签（tags=%r）-> 变异无效" % tags[:200])
    add("m3_e2_backend_still_red",
        gates_by_id(b).get("delta", {}).get("ok") is False,
        "后端 delta 这一条竟不是红的 -> 判据没牙")
    pg.close()
    ctx.close()


def run_mutants(br, url):
    attempts = {}
    for tag, fn in (("m1", mutant_m1), ("m2", mutant_m2), ("m3", mutant_m3)):
        _drop_prefix(tag)
        why = None
        for attempt in range(1, MUTANT_ATTEMPTS + 1):
            try:
                fn(br, url)
                attempts[tag] = attempt
                why = None
                break
            except Exception as e:
                if not _is_target_closed(e):
                    raise
                why = "%s 第%d/%d次目标被回收: %s" % (tag, attempt, MUTANT_ATTEMPTS,
                                                     str(e)[:140])
                print("  [重试] %s 目标被回收（第%d/%d次）-> 重建 context+page 重跑"
                      % (tag, attempt, MUTANT_ATTEMPTS))
                time.sleep(1.5)
        attempts.setdefault(tag, MUTANT_ATTEMPTS)
        if why is not None:
            add("%s_survived_target_closed" % tag, False, why)
        add("%s_attempts_within_budget" % tag, attempts[tag] <= MUTANT_ATTEMPTS,
            "尝试 %d 次（上限 %d）" % (attempts[tag], MUTANT_ATTEMPTS))


def main():
    if not os.path.exists(CHROME):
        print("CLOSE_GATE env unavailable: no Chrome at %s" % CHROME)
        return 2
    idx = os.path.join(ROOT, "index.html")
    if not os.path.isfile(idx):
        print("CLOSE_GATE env unavailable: missing index.html (run tools/build.py)")
        return 2
    fx = os.path.join(ROOT, FIXTURE, "work", "GOAL.json")
    if not os.path.isfile(fx):
        print("CLOSE_GATE env unavailable: missing fixture %s" % fx)
        return 2
    if not os.path.isdir(GF_FIXTURE):
        print("CLOSE_GATE env unavailable: missing fixture ledger dir %s" % GF_FIXTURE)
        return 2
    # -- c1 源码漂移自检 --
    built = open(idx, encoding="utf-8").read()
    for name, anchor in (("draw", ANCHOR_DRAW), ("badge", ANCHOR_BADGE),
                         ("gst", ANCHOR_GST)):
        n = built.count(anchor)
        if n != 1:
            print("CLOSE_GATE FAIL 源码漂移：%s 锚点 %r 命中 %d 次（应为 1）"
                  "—— 门自己失效了" % (name, anchor, n))
            return 1
    sha_idx = sha256_file(idx)
    sha_fix = sha256_file(fx)
    live_goal = os.path.join(ROOT, "work", "GOAL.json")
    sha_live = sha256_file(live_goal) if os.path.isfile(live_goal) else None
    # -- c2 起一个只读预检服务 --
    os.environ["GF_OUT"] = GF_FIXTURE
    port = free_port()
    mod = patch_serve(load_serve(SERVE_PY))
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    url = "http://127.0.0.1:%d/index.html" % port
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:                                  # noqa: BLE001
        srv.shutdown()
        print("CLOSE_GATE env unavailable: playwright missing (%r)" % e)
        return 2
    # -- c3/c4/c5 --
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(executable_path=CHROME)
            try:
                errs = []
                ctx, pg = new_page(br, url, errs=errs)
                run_positive(pg, url)
                add("p_e18_no_js_error", not errs, "; ".join(errs)[:300])
                pg.close()
                ctx.close()
                run_negatives(br, url)
                run_mutants(br, url)
            finally:
                br.close()
    finally:
        kill_current(mod)
        srv.shutdown()
        srv.server_close()
    # -- c6 只读自证 --
    add("e_live_index_untouched", sha_idx == sha256_file(idx),
        "变异体走了 route，活页面零写入")
    add("e_fixture_goal_untouched", sha_fix == sha256_file(fx),
        "预检写了夹具 work/GOAL.json：sha %r -> %r"
        % (sha_fix[:12], sha256_file(fx)[:12]))
    now_live = sha256_file(live_goal) if os.path.isfile(live_goal) else None
    add("e_live_goal_untouched", sha_live == now_live,
        "预检改了活项目 work/GOAL.json：sha %r -> %r" % (sha_live, now_live))
    red = sorted(k for k, v in G.items() if not v)
    rc = 1 if red else 0
    for k in sorted(G):
        extra = ("  <- " + WHY[k]) if (not G[k] and k in WHY) else ""
        print("  [%s] %s%s" % ("ok  " if G[k] else "FAIL", k, extra))
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, "close_gate.json"), "w", encoding="utf-8") as f:
            json.dump({"guards": G, "why": WHY, "red": red,
                       "live_index_sha256": sha_idx,
                       "fixture_goal_sha256": sha_fix,
                       "verdict": "CLOSE_GATE OK" if rc == 0 else "CLOSE_GATE FAIL"},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    print("== D79 收轮就绪度 %s ==" % ("OK" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
