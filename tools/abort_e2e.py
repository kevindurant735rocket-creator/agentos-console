#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D76 中止能力的**真浏览器**端到端门（abort_gate 之外的第二道牙）。

abort_gate.py 证明的是「服务端语义对」：契约组 + 真进程组 + 三个变异体。
但它全程用 urllib 打 HTTP，看不见用户那一侧：
    * 停止按钮会不会真的**出现**（没有 run id 时必须不出现 —— 不盲杀）；
    * 点它会不会真的走 /__act/abort；
    * 收尾有没有把「被用户中止」如实写进 hint 与 transcript，而不是记成 rc=0 成功。
这三件事只有真浏览器点按能证，D76 起本门。

隔离纪律（与 neg_a11y / a11y_served 同款）：
    * 探针动作注入**内存**的 ACTIONS（import serve.py 后改字典），生产 allowlist 零写入；
    * recommend 也只在内存里补两条，让审批卡真的画出这两个动作 ——
      于是「授权 → 运行 → 停止」全程走的是页面自己的代码，没有 page.evaluate 走后门；
    * 活 index.html 零写入：变异体靠 Playwright 的 route 拦 served 字节，不落盘。

上牙（teeth）：用 route 把 served index.html 里那行
`$('#wkstop').addEventListener('click',wkAbort);` 删掉再跑一遍同一串点按，
必须观测到「点了停止但进程照跑、hint 不出现已中止」—— 证明正例那十几条不是恒绿。

用法：python3 tools/abort_e2e.py    # rc=0 全过 / 1 有红项 / 2 环境不可用
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

PROBE_ACT = "__d76_e2e_probe__"
FAST_ACT = "__d76_e2e_fast__"
# 父 bash 忽略 SIGTERM（逼出 3s 宽限后的 SIGKILL 整组路径），并起一个同样忽略
# SIGTERM 的孙 bash；父子各写自己的 pid，判据是「孙也死了」——整组杀才算真中止。
PROBE_SCRIPT = ('trap "" TERM; echo $$ > "$E_PIDF"; '
                'bash -c \'trap "" TERM; echo $$ > "$E_PIDF2"; sleep 60\' & '
                'sleep 60')
FAST_SCRIPT = "echo d76-e2e-fast-ok"
MUT_LINE = "$('#wkstop').addEventListener('click',wkAbort);"

G = {}
WHY = {}


def add(name, cond, detail=""):
    try:
        v = bool(cond)
    except Exception as e:                       # noqa: BLE001 门要报失败，不是崩
        v = False
        detail = "%s | EXC %r" % (detail, e)
    G[name] = v
    if not v:
        WHY[name] = str(detail)[:400]
    return v


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def wait_dead(pid, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def read_pid(path):
    try:
        return int(open(path).read().strip())
    except (OSError, ValueError):
        return None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def load_serve(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("d76_e2e_serve", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def inject(mod):
    """内存注入两个门用动作 + 让 recommend 把它们画进审批卡（生产 allowlist 零写入）。"""
    mod.ACTIONS[PROBE_ACT] = ("门用慢动作（父孙都忽略 TERM）",
                              ["bash", "-c", PROBE_SCRIPT], 120)
    mod.ACTIONS[FAST_ACT] = ("门用快动作", ["bash", "-c", FAST_SCRIPT], 30)
    # 浏览器关掉 SSE 会话时 socketserver 会甩一整页 ConnectionResetError traceback，
    # 淹掉门的判据输出（D50 靠输出认红门归属）。这是纯噪声，就地咽掉。
    _handle = mod.Handler.handle

    def _safe_handle(self):
        try:
            _handle(self)
        except (ConnectionResetError, BrokenPipeError):
            pass

    mod.Handler.handle = _safe_handle
    base = mod.recommend

    def rec(data):
        out = base(data)
        for i, a in enumerate((PROBE_ACT, FAST_ACT)):
            if not any(r.get("action") == a for r in out):
                out.insert(i, {"action": a, "severity": "P2",
                               "why": "D76 中止端到端门注入的探针动作",
                               "evidence": json.dumps({"gate": "abort_e2e"},
                                                      ensure_ascii=False)[:300]})
        return out

    mod.recommend = rec
    return mod


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    def post(self, path, obj):
        body = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-AgentOS-Action", "1")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw or "{}")
            except ValueError:
                return e.code, {"raw": raw}

    def health(self):
        return self.get("/__health")[1]


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


def auth_state(pg, action):
    """审批卡上的显式授权态：True=已授权 / False=待授权 / None=卡片还没画出来。

    授权是页面的显式状态（`index.html` 的 `WKPEND` Set，渲染成 `span.st.on`），
    只能从 DOM 真值读。靠「点了几次」猜会在重试时把已授权又点成撤销 ——
    `#wkapbox` 的点击处理器是 toggle（`if(WKPEND.has(n)) delete else add`）。
    """
    js = ("a => { const b = document.querySelector('[data-wkap=\"' + a + '\"]');"
          " if (!b) return null;"
          " const w = b.closest('div.aw');"
          " const s = w && w.querySelector('span.st');"
          " return s ? s.classList.contains('on') : null; }")
    try:
        return pg.evaluate(js, action)
    except Exception:            # noqa: BLE001 重渲染瞬间求值可能抛，不当判据
        return None


def click_and_wait(pg, action, wait_card=45.0):
    """走页面自己的授权 → 运行两下点按（页面代码，不走后门）。

    2026-10-11 定位并复现（45 门长链末端 + livecheck 内部链的同一条假红）：
    工具卡 `#wktools .a1` 由 `wkTools()` 同步画出来，审批卡 `[data-wkap]` 却要等
    `wkLoad()` 里那条 `/__act/recommend` 异步回来才画（boot → wkLoad → wkApprovals）。
    机器被前面二十多道真浏览器门压住时 recommend 慢半拍，门在审批卡还没出来就点按
    → `[data-wkap]` 不存在 → 整门假红。负向对照实测：把 recommend 拖 6s，红字
    （m_e2/m_e3/m_e5 activeElement='t-work'/m_e6/m_x_cleanup 409）与 livecheck 逐字一致。
    这里先显式等审批卡出现 —— 不是放宽判据，是把「页面还没画完」和「页面画错了」分开；
    等不到照样 return False 判红。
    """
    t0 = time.time()
    st = None
    while time.time() - t0 < wait_card:
        st = auth_state(pg, action)
        if st is not None:
            break
        time.sleep(0.25)
    if st is None:
        return False
    if not st:                       # 只有真没授权才点（toggle 不能重复点）
        pg.locator('[data-wkap="%s"]' % action).first.click()
        t1 = time.time()
        while time.time() - t1 < 5.0:
            if auth_state(pg, action) is True:
                break
            time.sleep(0.1)
    # 显式点工具卡的「运行」（#wktools 在 #wkapbox 之前，与原 `.first` 同一条路），
    # 且它不像审批卡那颗在未授权时 disabled —— 授权没生效时由 wkRun 自己拒绝并写 hint，
    # 门据此如实判红，而不是干等 enabled 抛 TimeoutError 把整门掀翻。
    pg.locator('#wktools button[data-wk="%s"]' % action).first.click()
    return True


def start_run(pg, cl, action, tries=3, wait=20.0):
    """点页面自己的「授权 → 运行」，并用服务端真值确认动作真的起来了。

    2026-10-10 踩实（44 门长链）：本门排在 abort_gate 之后，机器已被前面二十
    多道真浏览器门压得很重，偶发「点按送达了但 run 没起来」—— 表现是 #wkstop
    永远不亮，随后那次 `#wkstop`.click() 干等 30s 抛 Playwright TimeoutError，
    整门崩掉，连 work/abort_e2e.json 都没落盘（红得没有物证，只剩一条 traceback）。
    同一分钟单跑本门是 rc=0（P/M 两遍 29 条判据全过）。

    这里的重试**只**覆盖「点按送达 → run 起来」这段交付，不碰任何判据：
    三次都没起来照样如实判红（e2/e3/e7 全红），没有一条被放松。
    """
    for _ in range(tries):
        if not click_and_wait(pg, action):
            return False
        t0 = time.time()
        while time.time() - t0 < wait:
            if cl.health().get("act_running") == action:
                return True
            try:
                if pg.locator("#wkstop").is_visible():
                    return True
            except Exception:            # noqa: BLE001 重渲染瞬间查可见性可能抛，不当判据
                pass
            time.sleep(0.25)
    return False


def run_phase(pg, cl, url, mod, tag, expect_stop_works):
    """跑一遍真实点按流。expect_stop_works=False 时是被变异体阉掉的那一遍。"""
    pidf = os.environ["E_PIDF"]
    pidf2 = os.environ["E_PIDF2"]
    pg.goto(url, wait_until="domcontentloaded")
    # 台面是第 6 视图（Agent 台），默认停在总览 —— 先用页面自己的 tab 切过去，
    # 免得在隐藏面板上点按（Playwright 会一直等 visible，那不是判据是噪声）。
    pg.locator('[data-v="work"]').click()
    pg.wait_for_selector("#wktools .a1", timeout=20000)
    add("%s_e1_stop_hidden_before_run" % tag,
        pg.locator("#wkstop").is_hidden(), "无 run 时必须不给停止入口")

    add("%s_e2_card_clicked" % tag, start_run(pg, cl, PROBE_ACT), url)
    vis = True
    try:
        pg.locator("#wkstop").wait_for(state="visible", timeout=25000)
    except Exception as e:                       # noqa: BLE001
        vis = False
        WHY.setdefault("%s_e3_stop_visible" % tag, str(e)[:200])
    add("%s_e3_stop_visible" % tag, vis, "SSE __RUN__ 帧到后才亮")
    lbl = pg.locator("#wkstop").get_attribute("aria-label") or ""
    add("%s_e4_stop_named_with_run" % tag,
        lbl.startswith("中止正在运行的动作 run=r"), lbl)
    # 键盘可达：焦点能落在停止按钮上（Tab 序里有它，Enter/Space 才有意义）
    pg.evaluate("() => document.querySelector('#wkstop').focus()")
    focused = pg.evaluate("() => document.activeElement && document.activeElement.id")
    add("%s_e5_stop_focusable" % tag, focused == "wkstop", "activeElement=%r" % focused)

    h = cl.health()
    add("%s_e6_actually_running" % tag,
        h.get("act_running") == PROBE_ACT and bool(h.get("act_run")),
        "act_running=%r run=%r" % (h.get("act_running"), h.get("act_run")))

    # 2026-10-10：这里原来是无条件 click()。run 没起来时它会干等 30s 抛
    # Playwright TimeoutError 把整门打崩（连 JSON 都不落盘），且下面那个
    # 「act_running is None → stopped=True」的循环会把「按钮压根没出现」
    # 读成「点了真停」的假绿。现在先看可见性：不可见就如实记红。
    stop_clickable = pg.locator("#wkstop").is_visible()
    if stop_clickable:
        pg.locator("#wkstop").click()
    else:
        WHY.setdefault("%s_e7_stop_actually_stops" % tag,
                       "停止按钮不可见，无法点按（run 未起来）")
    t0 = time.time()
    stopped = False
    while time.time() - t0 < 25:
        if cl.health().get("act_running") is None:
            stopped = True
            break
        time.sleep(0.3)
    if expect_stop_works:
        add("%s_e7_stop_actually_stops" % tag, stop_clickable and stopped,
            "点了停止但 act_running 仍是 %r" % cl.health().get("act_running"))
        hint_ok = True
        try:
            pg.wait_for_function(
                "() => (document.querySelector('#wkhint').textContent||'')"
                ".includes('已中止 rc=130')", timeout=20000)
        except Exception as e:                   # noqa: BLE001
            hint_ok = False
            WHY.setdefault("%s_e8_hint_honest" % tag, str(e)[:200])
        hint = pg.locator("#wkhint").text_content() or ""
        add("%s_e8_hint_honest" % tag, hint_ok and "不算成功" in hint, hint[:160])
        # hint 时序：中止流与回执流是两条异步流，谁后到谁写 hint。
        # 光看「当前这一句」分不清谁赢，所以判**最后一句必须是回执那句**
        # （D76 实测踩过：livecheck 真中止时「已发中止」晚 0.1s 把 rc=130 盖掉）。
        time.sleep(3.0)
        hi = pg.evaluate("() => (window.__agentos && __agentos.hintLog)"
                         " ? __agentos.hintLog() : (window.WKHI||[]).slice()")
        last = (hi[-1]["txt"] if hi and isinstance(hi[-1], dict) else
                (hi[-1] if hi else "")) or ""
        add("%s_e8b_receipt_hint_is_last" % tag,
            "已中止 rc=130" in last and "已发中止" not in last,
            "hint 时序尾句=%r" % last[:120])
        tr = pg.locator("#wktrbox").text_content() or ""
        add("%s_e9_transcript_marks_abort" % tag,
            "被用户中止" in tr and "⏹" in tr, tr[-200:])
        add("%s_e10_not_counted_green" % tag,
            ("◀ %s rc=0" % PROBE_ACT) not in tr, "中止被记成了成功")
        add("%s_e11_stop_hidden_after" % tag,
            pg.locator("#wkstop").is_hidden(), "跑完还挂着停止按钮")
        p1, p2 = read_pid(pidf), read_pid(pidf2)
        add("%s_e12_group_reaped" % tag,
            bool(p1) and wait_dead(p1) and bool(p2) and wait_dead(p2),
            "pidf=%r pidf2=%r" % (p1, p2))
        # 中止之后台面必须还能再跑一个动作（锁释放、不卡死）
        clicked = click_and_wait(pg, FAST_ACT)
        again = True
        try:
            pg.wait_for_function(
                "a => (document.querySelector('#wkhint').textContent||'')"
                ".includes(a + ' rc=0')", arg=FAST_ACT, timeout=25000)
        except Exception as e:                   # noqa: BLE001
            again = False
            WHY.setdefault("%s_e13_can_run_again" % tag, str(e)[:200])
        add("%s_e13_can_run_again" % tag, clicked and again,
            (pg.locator("#wkhint").text_content() or "")[:160])
    else:
        # 变异体：删掉点击监听后，「点了停止」必须**什么也没发生**
        still = cl.health().get("act_running")
        add("%s_e7_stop_actually_stops" % tag, not stopped,
            "变异体下竟然停了 -> 监听可能还在，变异体无效")
        add("%s_e9_transcript_marks_abort" % tag, not stopped,
            "变异体下 transcript 不该有中止标记")
        add("%s_x_mutant_wired_through" % tag, still == PROBE_ACT,
            "act_running=%r" % still)
        # 收场：变异体这一遍是真在跑，门必须自己收尸，否则漏后台进程
        run = cl.health().get("act_run")
        c, _d = cl.post("/__act/abort", {"run": run}) if run else (0, {})
        t1 = time.time()
        while time.time() - t1 < 25 and cl.health().get("act_running") is not None:
            time.sleep(0.3)
        add("%s_x_cleanup" % tag, c == 200 and cl.health().get("act_running") is None,
            "HTTP %s act_running=%r" % (c, cl.health().get("act_running")))
        p1, p2 = read_pid(pidf), read_pid(pidf2)
        add("%s_x_group_reaped" % tag,
            bool(p1) and wait_dead(p1) and bool(p2) and wait_dead(p2),
            "pidf=%r pidf2=%r" % (p1, p2))


def main():
    if not os.path.exists(CHROME):
        print("ABORT_E2E 环境不可用：Chrome 不在 %s" % CHROME)
        return 2
    idx = os.path.join(ROOT, "index.html")
    built = open(idx, encoding="utf-8").read()
    if built.count(MUT_LINE) != 1:
        print("ABORT_E2E FAIL 源码漂移：%r 在 index.html 命中 %d 次（应为 1）"
              % (MUT_LINE, built.count(MUT_LINE)))
        return 1
    sha_before = hashlib.sha256(built.encode("utf-8")).hexdigest()

    port = free_port()
    pidf = "/tmp/_d76e_pid_%d" % os.getpid()
    pidf2 = pidf + "2"
    for p in (pidf, pidf2):
        if os.path.exists(p):
            os.unlink(p)
    os.environ["E_PIDF"] = pidf
    os.environ["E_PIDF2"] = pidf2
    mod = inject(load_serve(SERVE_PY))
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever,
                     kwargs={"poll_interval": 0.2}, daemon=True).start()
    url = "http://127.0.0.1:%d/index.html" % port
    cl = Client(port)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print("ABORT_E2E 环境不可用：playwright 未装（%r）" % e)
        return 2
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(executable_path=CHROME)
            try:
                ctx = br.new_context(viewport={"width": 1440, "height": 900})
                pg = ctx.new_page()
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                print("== P. 正例：真浏览器点「运行」→ 停止按钮出现 → 点「停止」真停 ==")
                # 2026-10-10：门要报失败，不是崩。原来 run_phase 里任何未预料的
                # 异常都会把 main 掀翻 —— 结果是「没有 JSON、只有 traceback」，
                # 排查时连哪条判据红都读不到。这里兜住，转成一条红判据。
                try:
                    run_phase(pg, cl, url, mod, "p", expect_stop_works=True)
                except Exception as e:                   # noqa: BLE001
                    add("p_phase_crashed", False,
                        "%s: %s" % (type(e).__name__, str(e)[:300]))
                add("p_e14_no_js_error", not errs, "; ".join(errs)[:300])
                pg.close()
                ctx.close()

                print("== M. 上牙：served 字节删掉停止按钮监听，同一串点按必须不再停 ==")
                ctx2 = br.new_context(viewport={"width": 1440, "height": 900})
                pg2 = ctx2.new_page()

                def killer(route):
                    resp = route.fetch()
                    body = resp.body().decode("utf-8", "replace")
                    if MUT_LINE not in body:
                        route.fulfill(response=resp)
                        return
                    route.fulfill(response=resp,
                                  body=body.replace(MUT_LINE, "", 1).encode("utf-8"))

                pg2.route("**/index.html", killer)
                try:
                    run_phase(pg2, cl, url, mod, "m", expect_stop_works=False)
                except Exception as e:                   # noqa: BLE001
                    add("m_phase_crashed", False,
                        "%s: %s" % (type(e).__name__, str(e)[:300]))
                pg2.close()
                ctx2.close()
            finally:
                br.close()
    finally:
        kill_current(mod)
        srv.shutdown()
        srv.server_close()
        for p in (pidf, pidf2):
            if os.path.exists(p):
                lp = read_pid(p)
                if lp and pid_alive(lp):
                    try:
                        os.kill(lp, signal.SIGKILL)
                    except OSError:
                        pass
                os.unlink(p)

    add("e15_live_index_untouched",
        sha_before == hashlib.sha256(
            open(idx, encoding="utf-8").read().encode("utf-8")).hexdigest(),
        "变异体走了 route，活页面零写入")

    rc = 1 if [k for k, v in G.items() if not v] else 0
    for k in sorted(G):
        extra = ("  <- " + WHY[k]) if (not G[k] and k in WHY) else ""
        print("  [%s] %s%s" % ("ok  " if G[k] else "FAIL", k, extra))
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, "abort_e2e.json"), "w", encoding="utf-8") as f:
            json.dump({"guards": G, "why": WHY,
                       "live_index_sha256": sha_before,
                       "verdict": "ABORT_E2E OK" if rc == 0 else "ABORT_E2E FAIL"},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    print("== D76 中止端到端 %s ==" % ("OK" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
