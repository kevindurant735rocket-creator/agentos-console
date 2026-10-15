#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D78 轮次链的真浏览器门：编译成计划 → 读盘上台账 → 引擎逐字给下一轮该做什么。

为什么必须单独一道门（goal_gate 判不到的缺陷）：
    * goal_gate 到「AC 画进屏」为止。闭环**第二环**（引擎 status / next-round /
      round-goal 三个只读入口）在屏级判据下完全不可见 —— 没点那几个按钮时，
      台面缺一整块功能，view_dod / a11y_walk / ux_audit 全绿；
    * 更要命的是**假闭环**：台面完全可以自己拼一句「下一步该做 R2」，
      看上去和引擎说的很像，但没有 goal_id、没有判据、没有执行约束 ——
      用户照着它往下走，闭环就是演的。所以本门的核心判据是
      **屏上那行字 == 引擎此刻真打印的那行字（逐字）**，不是「像不像」。

诚实负例是另一半：读不到台账 / 路径越界时必须报 BLOCK 卡，
并且**不许画出一张空表** —— 画出来比不画更坏，用户会以为这个项目没有轮次。

只读自证：三个 op 跑完，任务空间 work/GOAL.json 的 sha256 必须与跑前逐位一致。
这条把「只读」从注释里的说法变成可判据的事实（写操作会改 GOAL.json）。

上牙（三个变异体，走 route 拦 served 字节，活的 index.html 零写入）：
    M1 抽掉 cdraw(j)      → 台账渲染不出来，门必须红；
    M2 台面自拼 objective → 与引擎 payload 不逐字相等，门必须红；
    M3 忽略 !j.ok         → 负例被画成假台账，门必须红。

用法：python3 tools/round_gate.py    # rc=0 全过 / 1 有红项 / 2 环境不可用
"""
import hashlib
import json
import os
import signal
import socket
import subprocess
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
COMPANY_SH = os.path.expanduser(
    "~/.codex/global-background/company-delivery/company.sh")

DEMAND = "实现一个本地库存后台，AC 全 PASS rc=0"
ACS = ["库存列表 3s 内可见", "扣减后库存数正确", "回滚可用"]
NON_GOALS = ["不做移动端"]
ROUNDS = 3
BAD_DEMAND = "做个库存后台"
BAD_ACS = ["库存列表可见"]

G = {}
WHY = {}
G = {}
WHY = {}

# 变异体锚点：必须**恰好命中一次**。命中 0 次说明源码漂移、门自己失效了 ——
# 那比红更坏，所以漂移直接判 FAIL 而不是静默跳过。
ANCHOR_DRAW = "CHLAST=j; cdraw(j);"
ANCHOR_OK = "if(!j.ok){ chfail(j); return; }"
ANCHOR_OBJ = '<pre class="gcode" id="gcobj">${esc(cg)}</pre>'
FAKE_OBJ = '<pre class="gcode" id="gcobj">台面自己拼一句：继续做下一轮</pre>'


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
    spec = importlib.util.spec_from_file_location("d78_round_serve", path)
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


def engine_truth(op, d, n=1):
    """自己 fork 一次公司引擎，拿它的原话当 ground truth。

    为什么不复用服务端已经 parse 好的 payload：门要判的是「屏上那行字是不是
    引擎说的」。若 ground truth 也走同一条 parse 路径，一处解析 bug 会同时
    污染两边、门全绿 —— 那是自己给自己出考卷。
    """
    argv = ["bash", COMPANY_SH, "goal", op, "--dir", d]
    if op == "round-goal":
        argv += ["--round", str(n)]
    p = subprocess.run(argv, cwd=ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=120)
    i = p.stdout.find("{")
    if i < 0:
        return p.returncode, None
    try:
        return p.returncode, json.loads(p.stdout[i:])
    except ValueError:
        return p.returncode, None


def norm(s):
    """屏上单元格可能带引擎原文里的换行；比较前只压空白，不动内容。"""
    return " ".join(str(s or "").split())


def disk_table(taskdir):
    """读引擎落盘的 goal_table，归一成与屏上表格同一形状的 6 元组。"""
    gd = os.path.join(taskdir, "work", "GOAL.json")
    with open(gd, encoding="utf-8") as f:
        g = json.load(f)
    rows = []
    for r in (g.get("goal_table") or []):
        rows.append(["R%s" % r.get("n"),
                     norm(r.get("state")) or "—",
                     norm(r.get("giga_run")) or "—",
                     norm(r.get("goal")) or "—",
                     norm(r.get("ac")) or "—",
                     norm(r.get("delta")) or "（未收轮）"])
    return rows, g


READ_CHAIN_JS = r"""() => {
  const q = s => document.querySelector(s);
  const txt = e => (e ? (e.textContent || '').trim() : null);
  const card = q('#gchain .gcard');
  const out = {};
  out.cardClass = card ? (card.className || '') : null;
  out.tags = [];
  document.querySelectorAll('#gchain .gcard h4 .gst').forEach(e => out.tags.push(txt(e)));
  out.goal = txt(q('#gchain .ggoal'));
  out.tblHead = [];
  document.querySelectorAll('#gchain .gtbl thead th').forEach(e => out.tblHead.push(txt(e)));
  out.tblRows = [];
  document.querySelectorAll('#gchain .gtbl tbody tr').forEach(tr => {
    const cs = [];
    tr.querySelectorAll('td').forEach(td => cs.push(txt(td)));
    out.tblRows.push(cs);
  });
  const cap = q('#gchain .gtbl caption');
  out.caption = txt(cap);
  const lis = sel => {
    const a = [];
    document.querySelectorAll(sel + ' > li').forEach(li => {
      const b = li.querySelector('.gn');
      const n = b ? txt(b) : '';
      a.push([n || '', txt(li).replace(n || '', '').trim()]);
    });
    return a;
  };
  out.rounds = lis('#gcrounds');
  out.chain = lis('#gcchain');
  out.obj = txt(q('#gcobj'));
  out.nxt = txt(q('#gcnext'));
  out.hint = txt(q('#gchint'));
  out.tr = txt(q('#gtrbox'));
  const gd = q('#gdir');
  out.dir = gd ? (gd.value || '') : '';
  const gb = q('#gchain');
  out.empty = gb ? (gb.innerHTML === '') : true;
  return out;
}"""


def read_chain_dom(pg):
    """只读 DOM。刻意不读 window.__agentos 之类 JS 变量：抽掉 cdraw(j) 之后
    变量照样是满的、屏上是一张空白；读变量就会放过「用户什么都看不见」这个
    最严重的缺陷。变量与 DOM 的差距正是本门存在的理由。
    """
    return pg.evaluate(READ_CHAIN_JS)


def open_goal_tab(pg, url):
    pg.goto(url, wait_until="load")
    pg.click('[data-v="goal"]')
    pg.wait_for_selector('#gdmd', state="visible")


def fill_and_compile(pg, demand, acs, non_goals, rounds):
    """全部走页面自己的输入框与按钮 —— 不调 goalSubmit()、不打后门。
    打后门会把「input 有没有绑上」「按钮是不是真能点」一起放过，
    而这两件恰好是用户最先撞到的（填了字点不动）。
    """
    pg.fill('#gdmd', demand)
    pg.fill('#gdac', "\n".join(acs))
    pg.fill('#gdng', "\n".join(non_goals))
    pg.fill('#gdrn', str(rounds))
    pg.click('#gdgo')


def wait_sel(pg, sel, timeout=200.0):
    """等选择器出现。判据只认屏上终态（画出来了 / BLOCK 卡出来了）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pg.locator(sel).count() > 0:
            time.sleep(0.5)
            return True
        time.sleep(0.3)
    return False


def wait_hint(pg, needle, timeout=200.0):
    """等 hint 出现 needle。变异体用它而不是等卡：抽掉 cdraw 后**根本不会有卡**，
    等卡会把每一遍变异体都拖成 200s 超时，慢到没人愿意跑这道门。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            txt = pg.locator("#gchint").text_content() or ""
        except Exception:                                     # noqa: BLE001
            txt = ""
        if needle in txt:
            return True
        time.sleep(0.3)
    return False


def wait_tr(pg, needle, timeout=200.0):
    """等编译现场（#gtrbox）出现 needle。用来判「请求真的跑完了并留了痕」。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            txt = pg.locator("#gtrbox").text_content() or ""
        except Exception:                                     # noqa: BLE001
            txt = ""
        if needle in txt:
            return True
        time.sleep(0.3)
    return False


def click_chain(pg, op):
    pg.click("#gcst" if op == "status" else
             ("#gcnx" if op == "next-round" else "#gcrg"))
    return wait_sel(pg, "#gchain .gcard")


def post_chain(url, body, csrf=True):
    """直调后端（越权负例用；不经浏览器 UI）。返回 (http_code, obj)。"""
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if csrf:
        h["X-AgentOS-Action"] = "1"
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
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


def taskdir_from_dom(pg):
    el = pg.locator("#gplan #gpath")
    if el.count() == 0:
        return None
    return (el.text_content() or "").replace("任务空间", "").strip()


# ---- main（D78 轮次链门）----
def main():
    if not os.path.exists(CHROME):
        print("ROUND_GATE env unavailable: no Chrome at %s" % CHROME)
        return 2
    idx = os.path.join(ROOT, "index.html")
    if not os.path.isfile(idx):
        print("ROUND_GATE env unavailable: missing index.html (run tools/build.py)")
        return 2
    if not os.path.isfile(COMPANY_SH):
        print("ROUND_GATE env unavailable: missing %s" % COMPANY_SH)
        return 2
    # -- c1 --
    built = open(idx, encoding="utf-8").read()
    for name, anchor in (("draw", ANCHOR_DRAW), ("ok", ANCHOR_OK),
                         ("obj", ANCHOR_OBJ)):
        n = built.count(anchor)
        if n != 1:
            print("ROUND_GATE FAIL 源码漂移：%s 锚点 %r 命中 %d 次（应为 1）"
                  "—— 门自己失效了" % (name, anchor, n))
            return 1
    sha_idx = sha256_file(idx)
    live_goal = os.path.join(ROOT, "work", "GOAL.json")
    sha_live = sha256_file(live_goal) if os.path.isfile(live_goal) else None
    # -- c2 --
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
        print("ROUND_GATE env unavailable: playwright missing (%r)" % e)
        return 2
    # -- c3 --
    st = None
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(executable_path=CHROME)
            try:
                print("== P. 正例：编译 → 读盘上台账 → 引擎逐字给下一轮 ==")
                errs = []
                ctx, pg = new_page(br, url, errs=errs)
                open_goal_tab(pg, url)
                st = run_positive(pg, "p", url)
                if st:
                    check_status_table(pg, st)
                    check_lists(pg, st)
                    check_verbatim(pg, st, "next-round")
                    check_verbatim(pg, st, "round-goal", 1)
                    check_live_root(pg, st)
                # -- c4 --
                    add("p_e15_readonly_proof", sha256_file(st["gd"]) == st["sha"],
                        "三个只读 op 跑完 GOAL.json 变了：%s -> %s"
                        % (st["sha"][:12], sha256_file(st["gd"])[:12]))
                add("p_e16_no_js_error", not errs, "; ".join(errs)[:300])
                pg.close()
                ctx.close()
                run_negatives(br, url)
                check_backend(url)
                if st:
                    run_mutants(br, url, st["td"])
            finally:
                br.close()
    finally:
        kill_current(mod)
        srv.shutdown()
        srv.server_close()
    # -- c5 --
    add("e_live_index_untouched", sha_idx == sha256_file(idx), "变异体走了 route，活页面零写入")
    now_live = sha256_file(live_goal) if os.path.isfile(live_goal) else None
    add("e_live_goal_json_untouched", sha_live == now_live,
        "活项目 work/GOAL.json 被改了 sha %r -> %r" % (sha_live, now_live))
    red = sorted(k for k, v in G.items() if not v)
    rc = 1 if red else 0
    for k in sorted(G):
        extra = ("  <- " + WHY[k]) if (not G[k] and k in WHY) else ""
        print("  [%s] %s%s" % ("ok  " if G[k] else "FAIL", k, extra))
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, "round_gate.json"), "w", encoding="utf-8") as f:
            json.dump({"guards": G, "why": WHY, "red": red,
                       "live_index_sha256": sha_idx,
                       "verdict": "ROUND_GATE OK" if rc == 0 else "ROUND_GATE FAIL"},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    print("== D78 轮次链 %s ==" % ("OK" if rc == 0 else "FAIL"))
    return rc


def run_positive(pg, tag, url):
    """正例全链：一句自然语言 → 引擎真编译 → 读盘上台账 → 引擎逐字给下一轮。"""
    open_goal_tab(pg, url)
    add("%s_e0_three_buttons" % tag,
        pg.locator("#gcst").is_visible() and pg.locator("#gcnx").is_visible()
        and pg.locator("#gcrg").is_visible(), "三个只读入口按钮没全在屏上")
    add("%s_e0b_chain_empty_at_start" % tag,
        (pg.locator("#gchain").inner_html() or "").strip() == "",
        "还没查任何东西 #gchain 就已预填台账 -> 诚实口径破了")
    fill_and_compile(pg, DEMAND, ACS, NON_GOALS, ROUNDS)
    add("%s_e1_compiled" % tag, wait_sel(pg, "#gplan .gcard"), "200s 内计划卡没出")
    td = taskdir_from_dom(pg)
    add("%s_e2_taskdir_shown" % tag, bool(td) and "work/tasks/g" in td, "任务空间=%r" % td)
    add("%s_e2b_dir_autofilled" % tag,
        (pg.locator("#gdir").input_value() or "").strip() == (td or "\x00"),
        "编译完 #gdir=%r 没指向新任务空间 %r" % (pg.locator("#gdir").input_value(), td))
    gd = os.path.join(td, "work", "GOAL.json") if td else ""
    if not add("%s_e3_goal_json_on_disk" % tag, bool(gd) and os.path.isfile(gd), "缺 %r" % gd):
        return None
    return {"td": td, "gd": gd, "sha": sha256_file(gd), "tag": tag}


def check_status_table(pg, st):
    """status 的核心判据：屏上表格逐行等于盘上 goal_table（不是等于输入框）。

    台面完全可以把 goal_table 数组原样搬上去再自己编几列 —— 那样看起来完全对。
    所以这里连**占位符**都一起比：引擎给 null 时屏上必须显示台面约定的
    「—」/「（未收轮）」，编造不出这份对应关系。
    """
    tag = st["tag"]
    td = st["td"]
    add("%s_e4_status_card" % tag, click_chain(pg, "status"), "点「读轮次台账」后没出卡")
    d = read_chain_dom(pg)
    add("%s_e5_status_pass" % tag,
        "gpass" in (d.get("cardClass") or "") and "rc=0" in " ".join(d.get("tags") or []),
        "cardClass=%r tags=%r" % (d.get("cardClass"), d.get("tags")))
    disk, g = disk_table(td)
    rows = [[norm(c) for c in r] for r in (d.get("tblRows") or [])]
    add("%s_e6_table_eq_disk" % tag, rows == disk, "屏上 %r != 盘上 %r" % (rows, disk))
    add("%s_e6b_row_count" % tag,
        len(disk) == ROUNDS and len(rows) == ROUNDS,
        "盘上 %d 行 / 屏上 %d 行 / 轮次计划 %r" % (len(disk), len(rows), g.get("rounds_plan")))
    add("%s_e6c_cols_named" % tag,
        (d.get("tblHead") or []) == ["轮次", "状态", "本轮账", "目标", "判据", "提升"],
        "表头=%r" % (d.get("tblHead"),))
    return d


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


def check_lists(pg, st):
    """gcrounds / gcchain 必须画出引擎真给的轮次，且空的时候说「为什么空」。"""
    tag = st["tag"]
    _, g = disk_table(st["td"])
    d = read_chain_dom(pg)
    nr, nc = len(g.get("rounds") or []), len(g.get("goal_chain") or [])
    add("%s_e7_rounds_list_len" % tag, len(d.get("rounds") or []) == max(nr, 1),
        "屏上开过的轮数=%d 盘上=%d" % (len(d.get("rounds") or []), nr))
    add("%s_e7b_chain_list_len" % tag, len(d.get("chain") or []) == max(nc, 1),
        "屏上对话目标链=%d 盘上=%d" % (len(d.get("chain") or []), nc))
    jt = " ".join(" ".join(x) for x in (d.get("rounds") or []))
    if nr == 0:
        add("%s_e7c_rounds_honest_why" % tag, "还没开过轮" in jt,
            "没开过轮时台账没说为什么空：%r" % (d.get("rounds"),))
    jc = " ".join(" ".join(x) for x in (d.get("chain") or []))
    if nc == 0:
        add("%s_e7d_chain_honest_why" % tag, "未绑定对话目标" in jc,
            "没绑目标时没说实话：%r" % (d.get("chain"),))
    add("%s_e7e_no_fake_goal_id" % tag,
        all(norm(x[1]) != "未绑" or nc == 0 for x in (d.get("chain") or [])),
        "台面自己编了 goal_id：%r" % (d.get("chain"),))


def check_verbatim(pg, st, op, n=1):
    """核心判据：#gcobj 的字 == 引擎此刻真打印的 create_goal_arg（逐字）。

    这是「闭环第二环真的接上引擎」的唯一硬证据。若台面自己写一句
    「下一步该做 R2」，上面所有判据照样全绿，而 AgentOS 的第二环等于演的。
    ground truth 由门自己 fork 引擎拿到，不走服务端那条 parse 路径 ——
    否则一处解析 bug 会同时污染两边、门自己给自己出考卷。
    """
    tag = st["tag"]
    rc, pay = engine_truth(op, st["td"], n)
    if not add("%s_e8_%s_engine_rc0" % (tag, op), rc == 0 and isinstance(pay, dict),
               "门自己 fork 引擎 %s rc=%r payload=%r" % (op, rc, pay)):
        return None
    cga = pay.get("create_goal_arg") or ""
    if n and op == "round-goal":
        add("%s_e8_%s_round_no" % (tag, op), str(pay.get("round")) == str(n),
            "引擎回的 round=%r != %r" % (pay.get("round"), n))
    pg.fill("#grn", str(n))
    add("%s_e9_%s_card" % (tag, op), click_chain(pg, op),
        "点「%s」后没出卡" % op)
    d = read_chain_dom(pg)
    add("%s_e10_%s_verbatim" % (tag, op), (d.get("obj") or "").strip() == cga.strip(),
        "屏上=%r\n引擎=%r" % ((d.get("obj") or "")[:160], cga[:160]))
    add("%s_e10b_%s_not_empty" % (tag, op), bool((d.get("obj") or "").strip()),
        "#gcobj 是空的")
    add("%s_e11_%s_msg_transcribed" % (tag, op),
        norm(d.get("goal")) == norm(pay.get("msg")),
        "屏上=%r 引擎 msg=%r" % (norm(d.get("goal"))[:120], norm(pay.get("msg"))[:120]))
    return d


def check_live_root(pg, st):
    """活项目根（留空 = 活项目根）的台账必须有**真收轮痕迹**。

    新编译的任务空间是 GOAL_SET，3 行全是「待办 / — / （未收轮）」——
    拿它比表格很容易比成空对空。所以这里再走一遍活根：5 轮已收，
    每行都有 giga_run 与 delta。台面若把这两列编出来，这一条立刻红。
    """
    tag = st["tag"]
    pg.fill("#gdir", "")
    add("%s_e12_live_status_card" % tag, click_chain(pg, "status"),
        "活项目根上点「读轮次台账」没出卡")
    d = read_chain_dom(pg)
    disk, g = disk_table(ROOT)
    rows = [[norm(c) for c in r] for r in (d.get("tblRows") or [])]
    # 「至少几行」随目标表的轮次计划缩放（2026-10-11）：旧锚 5 是按 v118
    # 八轮计划写死的，引擎 v138 的新对话可以合法只有 3 轮 —— 写死 5 等于
    # 门失真。牙齿不松：每一行仍必须带真 giga_run+delta（见 e13b）。
    _need = min(5, int((g or {}).get("rounds_plan") or 0) or 5)
    add("%s_e13_live_table_eq_disk" % tag, rows == disk and len(disk) >= _need,
        "活根屏上 %d 行 / 盘上 %d 行，逐行相等=%r（门槛≥%d）"
        % (len(rows), len(disk), rows == disk, _need))
    real = [r for r in rows if r[2] != "—" and r[5] != "（未收轮）"]
    add("%s_e13b_live_has_real_runs" % tag, len(real) >= _need,
        "活根 %d 行带真 giga_run+delta（≥%d，非空对空，判据没牙）"
        % (len(real), _need))
    return d


def expect_block(pg, tag, needle):
    """负例的共同形状：BLOCK 卡 + **没有**轮次表 + 没有编造的 goal_id。"""
    add("%s_e15_block_card" % tag,
        "gblock" in (pg.locator("#gchain .gcard").first.get_attribute("class") or ""),
        "cardClass=%r" % pg.locator("#gchain .gcard").first.get_attribute("class"))
    d = read_chain_dom(pg)
    tags = " ".join(d.get("tags") or [])
    add("%s_e16_block_tagged" % tag, "BLOCK" in tags, "tags=%r" % (tags,))
    add("%s_e17_no_fake_table" % tag, not (d.get("tblRows") or []) and not (d.get("rounds") or []),
        "读不到台账却画出了表/轮次：rows=%r rounds=%r" % (d.get("tblRows"), d.get("rounds")))
    add("%s_e18_no_obj" % tag, not (d.get("obj") or ""),
        "读不到台账却编了一句 objective：%r" % (d.get("obj") or "")[:120])
    add("%s_e19_hint_block" % tag, "BLOCK" in (d.get("hint") or ""),
        "hint=%r" % (d.get("hint") or "")[:120])
    add("%s_e20_why_says_why" % tag, needle in ((d.get("goal") or "") + (d.get("hint") or "")),
        "没说清为什么读不到：goal=%r hint=%r" % ((d.get("goal") or "")[:100], (d.get("hint") or "")[:100]))
    return d


def run_negatives(br, url):
    """三个诚实负例：编译判红 / 路径越界 / 目录没跑过 set-goal。"""
    print("== N1 诚实负例：编译 BLOCK → 轮次链不许预填任何台账 ==")
    ctx, pg = new_page(br, url)
    open_goal_tab(pg, url)
    fill_and_compile(pg, BAD_DEMAND, BAD_ACS, [], ROUNDS)
    add("n1_e1_compile_block_shown", wait_sel(pg, "#gplan .gblock"),
        "坏需求下连 BLOCK 卡都没出")
    add("n1_e2_chain_still_empty",
        (pg.locator("#gchain").inner_html() or "").strip() == "",
        "编译判红，#gchain 里却已经有台账：%r" % (pg.locator("#gchain").inner_text() or "")[:160])
    add("n1_e3_no_table", pg.locator("#gchain .gtbl").count() == 0,
        "编译判红却画出了轮次表")
    pg.close()
    ctx.close()


    print("== N2 诚实负例：任务空间越界 → 400 BLOCK，不出空表 ==")
    ctx, pg = new_page(br, url)
    open_goal_tab(pg, url)
    pg.fill("#gdir", "../../../../etc")
    pg.click("#gcst")
    wait_hint(pg, "BLOCK", timeout=60.0)
    expect_block(pg, "n2", "越界")
    pg.close()
    ctx.close()

    print("== N3 诚实负例：目录没跑过 set-goal → 报读不到，不出空表 ==")
    ctx, pg = new_page(br, url)
    open_goal_tab(pg, url)
    pg.fill("#gdir", "tools")
    pg.click("#gcst")
    wait_hint(pg, "BLOCK", timeout=90.0)
    expect_block(pg, "n3", "set-goal")
    pg.close()
    ctx.close()


def check_backend(url):
    """后端越权负例：写操作与缺 CSRF 必须在**服务端**就被挡。

    为什么不靠「前端不提供按钮」就够：写操作一旦漏进 allowlist，前端没有按钮
    等于没有防护 —— 任何跨站 POST 都能收轮 / 立目标。所以这里 fetch 直调。
    """
    api = url.replace("index.html", "") + "__goal/chain"
    for op in ("close-round", "bind-goal", "plan-round"):
        code, obj = post_chain(api, {"op": op, "taskdir": "", "round": 1})
        add("b_%s_400" % op, code == 400 and not obj.get("ok"),
            "写操作 %s 拿到 %s %r" % (op, code, obj))
        add("b_%s_says_readonly" % op, "只读" in (obj.get("error") or ""),
            "回执没说清为什么拒：%r" % (obj.get("error"),))
    code, obj = post_chain(api, {"op": "status", "taskdir": ""}, csrf=False)
    add("b_no_csrf_403", code == 403 and not obj.get("ok"),
        "无 X-AgentOS-Action 头拿到 %s %r" % (code, obj))
    code, obj = post_chain(api, {"op": "status", "taskdir": "", "round": 99})
    add("b_round_range_400", code == 400 and "round" in (obj.get("error") or ""),
        "round=99 拿到 %s %r" % (code, obj))
    code, obj = post_chain(api, {"op": "status", "taskdir": "/etc"})
    add("b_abs_path_400", code == 400 and "越界" in (obj.get("error") or ""),
        "taskdir=/etc 拿到 %s %r" % (code, obj))


def run_mutants(br, url, td):
    """三个上牙。全部走 route 拦 served 字节 —— 活的 index.html 零写入。

    每个变异体都要先自证「变异真的生效」（wired）：一个无效变异会让门看起来
    有牙、实际没牙，那比没有门更坏。
    """
    print("== M1 上牙：抽掉 cdraw(j) → 台账渲染不出来，门必须红 ==")
    ctx, pg = new_page(br, url, ANCHOR_DRAW, "CHLAST=j;")
    open_goal_tab(pg, url)
    pg.fill("#gdir", td)
    pg.click("#gcst")
    add("m1_e1_wired", wait_tr(pg, "◀ status rc=0", timeout=90.0),
        "变异体下连回执都没进编译现场 -> 变异无效")
    add("m1_e2_chain_never_painted", pg.locator("#gchain .gcard").count() == 0,
        "抽掉 cdraw 后屏上仍有卡 -> 正例判据没牙")
    pg.close()
    ctx.close()

    print("== M2 上牙：台面自拼 objective → 与引擎 payload 不逐字相等，门必须红 ==")
    ctx, pg = new_page(br, url, ANCHOR_OBJ, FAKE_OBJ)
    open_goal_tab(pg, url)
    pg.fill("#gdir", td)
    pg.click("#gcnx")
    add("m2_e1_card_shown", wait_sel(pg, "#gchain .gcard"), "变异体下连卡都没出")
    rc, pay = engine_truth("next-round", td)
    cga = (pay or {}).get("create_goal_arg") or ""
    got = pg.locator("#gcobj").text_content() if pg.locator("#gcobj").count() else None
    add("m2_e2_fake_observed", "台面自己拼" in (got or ""),
        "变异体下 #gcobj=%r -> 变异无效" % (got or "")[:80])
    add("m2_e3_not_verbatim", (got or "").strip() != cga.strip() and bool(cga),
        "自拼文本竟与引擎逐字相等 -> 判据没牙")
    pg.close()
    ctx.close()

    print("== M3 上牙：忽略 !j.ok → 负例被画成假台账，门必须红 ==")
    ctx, pg = new_page(br, url, ANCHOR_OK, "if(false){ chfail(j); return; }")
    open_goal_tab(pg, url)
    pg.fill("#gdir", "../../../../etc")
    pg.click("#gcst")
    wait_sel(pg, "#gchain .gcard", timeout=60.0)
    cls = ""
    if pg.locator("#gchain .gcard").count():
        cls = pg.locator("#gchain .gcard").first.get_attribute("class") or ""
    add("m3_e1_false_green_observed", "gblock" not in cls,
        "变异体下越界仍报 BLOCK（class=%r）-> 变异无效" % cls)
    add("m3_e2_fake_ledger_drawn", "gpass" in cls,
        "变异体下越界竟画成 PASS 台账（class=%r）-> 负例没牙" % cls)
    pg.close()
    ctx.close()


if __name__ == "__main__":
    sys.exit(main())
