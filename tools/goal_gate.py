#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D77 目标台的真浏览器门：一句自然语言 → 引擎真编译 → 计划真的渲染出来。

为什么必须单独一道门（屏级判据看不见最严重的缺陷）：
    * view_dod 判「屏里有真内容」，而目标台在没编译时是**故意空**的
      （诚实口径：不预填假计划）。空屏在 view_dod 下全绿 —— 也就是说
      「目标台根本编译不出计划」这类缺陷，屏级判据天然测不到；
    * a11y_walk 只验焦点/ARIA，不看业务链路通不通。
所以本门走真浏览器点按：填需求 → 点「编译计划」→ 断言**引擎落盘的 AC**
真的画进了 DOM，且与磁盘上 taskdir/work/GOAL.json 逐条对得上（不是前端把
输入原样回显一遍 —— 那样闭环第一环等于在演戏）。

诚实负例是另一半：需求不够具体时引擎判 rc=2，台面必须报 BLOCK，而不是
照样渲染一张「看起来像计划」的空卡（那比没有更坏：用户以为它懂了）。

上牙（两个变异体，走 route 拦 served 字节，活的 index.html 零写入）：
    M1 抽掉 gplanBox(j) 调用   → AC 渲染不出来，门必须红；
    M2 把 verdict 判定写死 true → 坏需求也画成 PASS，门必须红。

用法：python3 tools/goal_gate.py    # rc=0 全过 / 1 有红项 / 2 环境不可用
"""
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVE_PY = os.path.join(HERE, "serve.py")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WORK = os.path.join(ROOT, "work")

# 变异体锚点：必须**恰好命中一次**。命中 0 次说明源码漂移、门自己失效了 ——
# 那比红更坏，所以漂移直接判 FAIL 而不是静默跳过。
ANCHOR_RENDER = "GLAST=j; gplanBox(j);"
ANCHOR_VERDICT = "const pass=(j.verdict==='PASS');"

DEMAND = "实现一个本地库存后台，AC 全 PASS rc=0"
ACS = ["库存列表 3s 内可见", "扣减后库存数正确", "回滚可用"]
NON_GOALS = ["不做移动端"]
ROUNDS = 3

G = {}
WHY = {}


def add(name, cond, detail=""):
    try:
        v = bool(cond)
    except Exception as e:
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
    spec = importlib.util.spec_from_file_location("d77_goal_serve", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def patch_serve(mod):
    """浏览器关掉 SSE 会话时 socketserver 会甩一整页 traceback，淹掉门的判据
    输出（D50 靠输出认红门归属）。纯噪声，就地咽掉。"""
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


def read_dom(pg):
    """只读 DOM —— 刻意**不读** window.__agentos.goalPlan() 之类 JS 变量。

    为什么：门要判的是「引擎的 AC 真的画进了用户看得见的屏上」。若改成读 JS 变量，
    抽掉 gplanBox(j)（屏上什么都不画、变量照样是满的）这个变异体就会蒙混过关 ——
    门会绿，而用户看到的是一张空白。变量与 DOM 的差距正是本门唯一存在的理由。
    """
    return pg.evaluate("""() => {
      const t = s => { const e=document.querySelector(s); return e ? (e.textContent||'').trim() : null; };
      const card = document.querySelector('#gplan .gcard');
      const lis = s => Array.from(document.querySelectorAll(s+' li'))
        .map(li => { const b=li.querySelector('.gn'); return b
          ? [ (b.textContent||'').trim(), (li.textContent||'').replace(b.textContent||'','').trim() ]
          : (li.textContent||'').trim(); });
      const sts = Array.from(document.querySelectorAll('#gplan .gcard h4 .gst'))
        .map(e => (e.textContent||'').trim());
      const pathEl = document.querySelector('#gplan #gpath');
      return {
        cardClass: card ? (card.className||'') : null,
        head: card ? (card.querySelector('h4') ? (card.querySelector('h4').textContent||'').trim() : '') : '',
        tags: sts,
        goal: t('#gplan .ggoal'),
        acs: lis('#gaclist'),
        rounds: lis('#grlist'),
        nonGoals: lis('#gnlist'),
        path: pathEl ? (pathEl.textContent||'').replace('任务空间','').trim() : null,
        pathExists: pathEl ? true : false,
        hint: t('#ghint'),
        tr: t('#gtrbox')
      };
    }""")


def open_goal_tab(pg, url):
    pg.goto(url, wait_until="load")
    pg.click('[data-v="goal"]')
    pg.wait_for_selector('#gdmd', state="visible")


def fill_and_click(pg, demand, acs, non_goals, rounds):
    """全部走页面自己的输入框与按钮 —— 不调 goalSubmit()、不 page.evaluate 打后门。

    打后门会把「input 有没有正确绑上」「按钮是不是真能点」一起放过，
    而这两件恰好是用户最先撞到的（填了字点不动 / ⌘↵ 不提交）。
    """
    pg.fill('#gdmd', demand)
    pg.fill('#gdac', "\n".join(acs))
    pg.fill('#gdng', "\n".join(non_goals))
    pg.fill('#gdrn', str(rounds))
    pg.click('#gdgo')


def wait_card(pg, timeout=200.0):
    """判据只认屏上的终态：卡片画出来了（PASS 或 BLOCK 都算）才算这一遍跑完。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pg.locator("#gplan .gcard").count() > 0:
            time.sleep(0.8)
            return True
        time.sleep(0.4)
    return False


def wait_hint(pg, needle, timeout=200.0):
    """等 hint 出现 needle。M1 用它而不是 wait_card：抽掉 gplanBox 后**根本不会有卡**，
    等卡会把每一遍变异体都拖成 200s 超时，慢到没人愿意跑这道门。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        txt = ""
        try:
            txt = pg.locator("#ghint").text_content() or ""
        except Exception:
            txt = ""
        if needle in txt:
            return True
        time.sleep(0.4)
    return False


def run_positive(pg, tag, url):
    open_goal_tab(pg, url)
    add("%s_e0_goal_tab_reachable" % tag,
        pg.locator('#gdmd').is_visible() and pg.locator('#gdgo').is_visible(), "")
    add("%s_e0b_empty_is_honest" % tag,
        (pg.locator('#gplan').inner_html() or "").strip() == "",
        "未编译时目标台就预填了内容 -> 诚实口径破了")
    fill_and_click(pg, DEMAND, ACS, NON_GOALS, ROUNDS)
    done = wait_card(pg)
    add("%s_e1_card_rendered" % tag, done, "200s 内屏上没画出计划卡")
    d = read_dom(pg)
    add("%s_e2_pass_card" % tag,
        "gpass" in (d.get("cardClass") or "") and "PASS rc=0" in " ".join(d.get("tags") or []),
        "cardClass=%r tags=%r" % (d.get("cardClass"), d.get("tags")))
    acs = [x[1] if isinstance(x, list) else x for x in d.get("acs") or []]
    add("%s_e3_acs_are_engines" % tag, acs == ACS,
        "屏上 AC=%r != 输入 %r" % (acs, ACS))
    rds = [x[0] if isinstance(x, list) else x for x in d.get("rounds") or []]
    add("%s_e4_rounds_rendered" % tag, rds == ["R%d" % i for i in range(1, ROUNDS + 1)], "轮次标签=%r" % (rds,))
    ngs = [x[1] if isinstance(x, list) else x for x in d.get("nonGoals") or []]
    add("%s_e5_non_goals_rendered" % tag, ngs == NON_GOALS, "屏上非目标=%r != %r" % (ngs, NON_GOALS))
    add("%s_e6_taskdir_shown" % tag, bool(d.get("path")) and "work/tasks/g" in d.get("path"), "任务空间路径=%r" % d.get("path"))
    add("%s_e7_hint_honest" % tag, "rc=0" in (d.get("hint") or "") and "3" in (d.get("hint") or ""), "hint=%r" % (d.get("hint") or "")[:160])
    add("%s_e8_transcript_rc0" % tag, "rc=0" in (d.get("tr") or "") and "编译计划" in (d.get("tr") or ""), (d.get("tr") or "")[-160:])
    return d


def cross_check_disk(tag, dom):
    """屏上 AC 必须**逐条等于**引擎落盘的 GOAL.json，不是等于输入框。

    这条是「闭环第一环真的闭合」的唯一硬证据：若台面只是把输入原样回显成一张
    像计划的东西，上面十几条判据会全绿，而 AgentOS 仍然什么都没编译出来。
    引擎会把 demand 改写成「交付「…」：AC 全部有断言且命令退出码为 0」，
    屏上 goal 也必须是那句改写后的，不是用户原话。
    """
    path = dom.get("path") or ""
    if not add("%s_e9_taskdir_readable" % tag, os.path.isdir(path), "路径不存在 %r" % path):
        return
    gd = os.path.join(path, "work", "GOAL.json")
    if not add("%s_e9b_goal_json_on_disk" % tag, os.path.isfile(gd), "缺 %s" % gd):
        return
    with open(gd, encoding="utf-8") as f:
        g = json.load(f)
    acs = [x[1] if isinstance(x, list) else x for x in dom.get("acs") or []]
    add("%s_e10_dom_acs_eq_disk" % tag, acs == list(g.get("acs") or []), "屏上 %r vs 盘上 %r" % (acs, g.get("acs")))
    ngs = [x[1] if isinstance(x, list) else x for x in dom.get("nonGoals") or []]
    add("%s_e11_dom_non_goals_eq_disk" % tag, ngs == list(g.get("non_goals") or []), "屏上 %r vs 盘上 %r" % (ngs, g.get("non_goals")))
    add("%s_e12_rounds_eq_disk" % tag, g.get("rounds_plan") == ROUNDS, "rounds_plan=%r" % g.get("rounds_plan"))
    domg = (dom.get("goal") or "").strip()
    add("%s_e13_goal_is_engine_rewrite" % tag, bool(domg) and domg == (g.get("goal") or "").strip() and domg != DEMAND, "屏上 goal=%r 盘上=%r 原话=%r" % (domg, g.get("goal"), DEMAND))
    md = os.path.join(path, "work", "GOAL.md")
    mds = os.path.join(path, "work", "GOALS.md")
    add("%s_e14_markdown_too" % tag, os.path.isfile(md) and os.path.isfile(mds), "GOAL.md/GOALS.md 缺一")
    add("%s_e15_not_in_live_root" % tag, os.path.realpath(path).startswith(os.path.realpath(os.path.join(WORK, "tasks")) + os.sep), "草稿落到了活项目根之外：%r" % path)


BAD_DEMAND = "做个库存后台"
BAD_ACS = ["库存列表可见"]


def run_block(pg, tag, url, expect_honest=True):
    """诚实负例：需求不够具体时引擎判 rc=2，台面必须**如实报 BLOCK**。

    判红之后仍渲染一张「看起来像计划」的空卡，比没有更坏：用户会以为它懂了，
    于是照着一张空计划往下走。所以这里同时要求：卡是 gblock、AC 与轮次**为空**。
    """
    open_goal_tab(pg, url)
    fill_and_click(pg, BAD_DEMAND, BAD_ACS, [], ROUNDS)
    done = wait_card(pg)
    add("%s_e1b_card_rendered" % tag, done, "坏需求下 200s 内连 BLOCK 卡都没出")
    d = read_dom(pg)
    add("%s_e2b_block_card" % tag, "gblock" in (d.get("cardClass") or "") and "BLOCK" in " ".join(d.get("tags") or []), "cardClass=%r tags=%r" % (d.get("cardClass"), d.get("tags")))
    add("%s_e3b_no_fake_acs" % tag, not (d.get("acs") or []), "判红后仍画出了 AC 清单：%r" % (d.get("acs"),))
    add("%s_e4b_no_fake_rounds" % tag, not (d.get("rounds") or []), "判红后仍画出了轮次表：%r" % (d.get("rounds"),))
    add("%s_e5b_hint_says_block" % tag, "BLOCK" in (d.get("hint") or "") and "rc=2" in (d.get("hint") or ""), "hint=%r" % (d.get("hint") or "")[:160])
    add("%s_e6b_transcript_not_green" % tag, "判红" in (d.get("tr") or "") and "◀ rc=0" not in (d.get("tr") or ""), (d.get("tr") or "")[-160:])
    return d


def make_killer(anchor, replacement):
    """拦 served 的 index.html 字节做变异，活的 index.html 零写入。"""
    def killer(route):
        resp = route.fetch()
        body = resp.body().decode("utf-8", "replace")
        if anchor not in body:
            route.fulfill(response=resp)
            return
        route.fulfill(response=resp, body=body.replace(anchor, replacement, 1).encode("utf-8"))
    return killer


def main():
    if not os.path.exists(CHROME):
        print("GOAL_GATE 环境不可用：Chrome 不在 %s" % CHROME)
        return 2
    idx = os.path.join(ROOT, "index.html")
    if not os.path.isfile(idx):
        print("GOAL_GATE 环境不可用：缺 index.html（先跑 tools/build.py）")
        return 2
    built = open(idx, encoding="utf-8").read()
    for name, anchor in (("render", ANCHOR_RENDER), ("verdict", ANCHOR_VERDICT)):
        n = built.count(anchor)
        if n != 1:
            print("GOAL_GATE FAIL 源码漂移：%s 锚点 %r 在 index.html 命中 %d 次（应为 1）—— 门自己失效了"
                  % (name, anchor, n))
            return 1
    sha_idx = sha256_file(idx)
    live_goal_json = os.path.join(ROOT, "work", "GOAL.json")
    sha_live_goal = sha256_file(live_goal_json) if os.path.isfile(live_goal_json) else None

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
    except ImportError as e:
        print("GOAL_GATE 环境不可用：playwright 未装（%r）" % e)
        return 2
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(executable_path=CHROME)
            try:
                print("== P. 正例：填需求 → 点编译 → 引擎真编译 → AC 画进屏 ==")
                ctx = br.new_context(viewport={"width": 1440, "height": 900})
                pg = ctx.new_page()
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                dom = run_positive(pg, "p", url)
                cross_check_disk("p", dom)
                add("p_e16_no_js_error", not errs, "; ".join(errs)[:300])
                pg.close()
                ctx.close()

                print("== N. 诚实负例：需求不够具体 → 引擎 rc=2 → 台面必须报 BLOCK ==")
                ctx = br.new_context(viewport={"width": 1440, "height": 900})
                pg = ctx.new_page()
                run_block(pg, "n", url)
                pg.close()
                ctx.close()

                print("== M1 上牙：抽掉 gplanBox(j) → AC 渲染为空，门必须红 ==")
                ctx = br.new_context(viewport={"width": 1440, "height": 900})
                pg = ctx.new_page()
                pg.route("**/index.html", make_killer(ANCHOR_RENDER, "GLAST=j;"))
                open_goal_tab(pg, url)
                fill_and_click(pg, DEMAND, ACS, NON_GOALS, ROUNDS)
                wait_hint(pg, "rc=0", timeout=200.0)
                d1 = read_dom(pg)
                add("m1_wired", "rc=0" in (d1.get("hint") or ""), "变异体下连引擎回执都没进 hint -> 变异无效")
                add("m1_acs_render_is_empty", len(d1.get("acs") or []) == 0, "变异体下 AC 仍=%r -> 正例判据没牙" % (d1.get("acs"),))
                pg.close()
                ctx.close()

                print("== M2 上牙：verdict 写死 true → 坏需求也画成 PASS，门必须红 ==")
                ctx = br.new_context(viewport={"width": 1440, "height": 900})
                pg = ctx.new_page()
                pg.route("**/index.html", make_killer(ANCHOR_VERDICT, "const pass=true;"))
                open_goal_tab(pg, url)
                fill_and_click(pg, BAD_DEMAND, BAD_ACS, [], ROUNDS)
                wait_card(pg, timeout=200.0)
                d2 = read_dom(pg)
                cls2 = d2.get("cardClass") or ""
                tags2 = " ".join(d2.get("tags") or [])
                add("m2_false_green_observed", ("gpass" in cls2) or ("PASS rc=0" in tags2), "变异体下坏需求没画成假绿 cardClass=%r tags=%r -> M2 无效" % (cls2, tags2))
                pg.close()
                ctx.close()
            finally:
                br.close()
    finally:
        kill_current(mod)
        srv.shutdown()
        srv.server_close()

    add("e_live_index_untouched", sha_idx == sha256_file(idx), "变异体走了 route，活页面零写入")
    now_live = sha256_file(live_goal_json) if os.path.isfile(live_goal_json) else None
    add("e_live_goal_json_untouched", sha_live_goal == now_live, "活项目 work/GOAL.json 被改了 sha %r -> %r" % (sha_live_goal, now_live))

    red = sorted(k for k, v in G.items() if not v)
    rc = 1 if red else 0
    for k in sorted(G):
        extra = ("  <- " + WHY[k]) if (not G[k] and k in WHY) else ""
        print("  [%s] %s%s" % ("ok  " if G[k] else "FAIL", k, extra))
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, "goal_gate.json"), "w", encoding="utf-8") as f:
            json.dump({"guards": G, "why": WHY, "red": red,
                       "live_index_sha256": sha_idx,
                       "verdict": "GOAL_GATE OK" if rc == 0 else "GOAL_GATE FAIL"},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    print("== D77 目标台 %s ==" % ("OK" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
