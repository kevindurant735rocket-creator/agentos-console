#!/usr/bin/env python3
"""真实最小化/恢复走查：用 AX 把窗口真的按进 Dock，再看两条路径能不能
把它拉回来。不注入、不伪造状态 —— 全部是真进程真窗口。

D105 的 `window-on-screen` 红项（mini=True）根因就在这里：`⌘M` 之后
`frameAutosaveName` 会把最小化态带进下一次启动，而
`makeKeyAndOrderFront` 对已最小化的窗口近乎空操作。判据只能抓末态，
抓不到「恢复动作压根没做」，所以要有一道真把窗口按进 Dock 的门。

排在 native_gate 之后、native_gate_selftest 之前不合适（它自己起实例、
约 80s，且与 native_gate 的单实例判据抢同一批进程）：单独跑，
或排在整条链最末尾。
"""
import json
import os
import re
import subprocess
import sys
import time

import ApplicationServices as AS

ROOT = "/Users/zhangfengrui/Documents/Codex/2026-10-01/agentos-console"
BIN = os.path.join(ROOT, "AgentOS.app/Contents/MacOS/AgentOS")
APP_BUNDLE_ID = "com.zhangfengrui.agentos-console"
APP_EXE_MATCH = "AgentOS.app/Contents/MacOS/AgentOS"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def foreground(pid, timeout=6.0):
    """把量具站回被测物那一侧。fg_activate 不可用就说不可用，不假装置前过。"""
    try:
        import fg_activate
    except Exception as e:                                  # noqa: BLE001
        return {"ok": None, "why": "fg_activate 不可用 %r" % (e,)}
    try:
        return fg_activate.ensure_foreground(int(pid), timeout=timeout, poll=0.4)
    except Exception as e:                                  # noqa: BLE001
        return {"ok": None, "why": repr(e)}


def nudge_frontmost(pid):
    """AX 自己的置前（辅助功能客户端激活 app 的标准路径）。

    D51d 实测（work/native/diag_fm.json）：A 的 AX 窗口读数塌到 0 窗之后，
    紧接着调一次 kAXFrontmost=true，下一拍就读回 1 窗（0 -> 1）。所以它是
    唯一被量过的「能把读数救回来」的手段，放在读数没开口时当补救用。

    注意它**不能**被当成产品判据的证据：置前理论上可能顺手 restore 一个
    最小化的窗口。所以只有在「直读本来就读不到」时才用，且置前后的读数
    都进 detail（见场景 2 的 ma_detail）。直读得到确定值的主路径不碰它。
    """
    app = AS.AXUIElementCreateApplication(int(pid))
    return AS.AXUIElementSetAttributeValue(app, AS.kAXFrontmostAttribute, True)


def _meta(definite, nudges, t0):
    m = {"definite": definite, "nudges": nudges}
    m["waited"] = round(time.time() - t0, 1)
    return m


def read_mini_patient(pid, timeout=30.0, poll=1.2, nudge_every=4):
    """轮询到读出**确定值**为止，读不到就每隔几拍置前一次再试。

    为什么不能靠置前一把抓：work/native/diag_fg.json 与 diag_fm.json 两次
    长跑都拍到 AX 窗口数在 0/1 之间摆（塌陷时刻 t≈18s 与 t≈3.1s 各一次，
    且 diag_fm 里它自己会恢复）。这是**量具间歇**，不是产品缺陷，所以
    判据必须等它开口，而不是把「这一秒没说话」当成「窗口不存在」。
    返回 (值, 元信息)，元信息把置前次数与确定读数次数都带出去。
    """
    t0 = time.time()
    definite = 0
    nudges = 0
    streak = 0
    while (time.time() - t0) < timeout:
        v = ax_minimized(pid)
        if v is not None:
            definite += 1
            return v, _meta(definite, nudges, t0)
        streak += 1
        if streak % nudge_every == 0:
            if nudge_frontmost(pid) == 0:
                nudges += 1
        time.sleep(poll)
    return None, _meta(definite, nudges, t0)


def wait_win_patient(pid, timeout=45.0, poll=1.2):
    """等窗口出现；读不到窗就置前一次再等。返回 (窗数, 为什么)。"""
    t0 = time.time()
    last_e, last_n, nudges = None, -1, 0
    while (time.time() - t0) < timeout:
        last_e, wins = ax_windows(pid)
        last_n = len(wins)
        if last_e == 0 and last_n >= 1:
            return last_n, "ok nudges=%d" % (nudges,)
        if last_e == 0 and last_n == 0:
            if nudge_frontmost(pid) == 0:
                nudges += 1
        time.sleep(poll)
    return 0, ("AXErr=%s 末次窗数=%d nudges=%d 进程=%r 存活=%s"
               % (last_e, last_n, nudges, ps_state(pid), alive(pid)))


def ax_windows(pid):
    e, wins = AS.AXUIElementCopyAttributeValue(
        AS.AXUIElementCreateApplication(pid), AS.kAXWindowsAttribute, None)
    if e != 0:
        return e, []
    return 0, list(wins or [])


def ax_minimized(pid, idx=0):
    e, wins = ax_windows(pid)
    if e != 0 or len(wins) <= idx:
        return None
    e2, m = AS.AXUIElementCopyAttributeValue(
        wins[idx], AS.kAXMinimizedAttribute, None)
    return None if e2 != 0 else bool(m)


def ax_minimized_settled(pid, tries=8, poll=1.5):
    """轮询到**读出确定值**为止，取最后一次确定读数。

    D105b 实测：AX 暴露是间歇的，`kAXWindows` 会在 1 窗和 0 窗之间摆。
    所以「读到 0 窗」绝不能当成「窗口没了」或「窗口最小化了」——
    那是量具这一秒没说话。单次采样判红是 D105 那轮真正的坑。
    """
    last = None
    for _ in range(tries):
        v = ax_minimized(pid)
        if v is not None:
            last = v
        time.sleep(poll)
    return last


def wait_not_minimized(pid, timeout=20.0, poll=1.5):
    """有界等到窗口**确定不再是最小化**为止；超时返回最后确定读数。

    2026-10-12 W1 链实测竞态：重启的新实例先带着继承的最小化态出现，
    deminiaturize 在其后毫秒级发生；旧读法（取最后一次确定值）在
    「早期一次确定 True + 之后 AX 转静默」时会把早读锁死判红 —— 而同一
    时刻产品的 probe 自己记的是 mini=False。判据本义是「不许**留在**
    最小化态」：读到过确定 False 即过；给足 20s 后仍确定 True 才判红
    （真缺陷——窗口一直没被拉回来——照样红）。
    """
    last = None
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = ax_minimized(pid)
        if v is not None:
            last = v
            if v is False:
                return False
        time.sleep(poll)
    return last


def set_minimized_settled(pid, want, tries=8, poll=1.5):
    """按到 Dock 里，并确认它真的处于最小化态（读不到就重试，不算数）。"""
    for _ in range(tries):
        r = set_minimized(pid, want)
        if r != "ok":
            time.sleep(poll)
            continue
        time.sleep(1.2)
        v = ax_minimized(pid)
        if v is True:
            return "ok"
        time.sleep(poll)
    return "not-confirmed"


def set_minimized(pid, want, idx=0):
    e, wins = ax_windows(pid)
    if e != 0 or len(wins) <= idx:
        return "no-window"
    e2 = AS.AXUIElementSetAttributeValue(
        wins[idx], AS.kAXMinimizedAttribute, True if want else False)
    return "ok" if e2 == 0 else "ax-err-%d" % e2


def wait_win_d(pid, timeout=30):
    """等窗口出现，顺带把「为什么没等到」说清楚。

    只回一个 0 的话，调用方分不清三种完全不同的事：进程已经退了 /
    进程活着但 AX 不给窗口 / AX 报错。它们的修法不在一处，所以这里
    把最后一次 AX 错误码、窗口数、进程状态一起带出去。"""
    t0 = time.time()
    last_e, last_n = None, -1
    while time.time() - t0 < timeout:
        last_e, wins = ax_windows(pid)
        last_n = len(wins)
        if last_e == 0 and last_n >= 1:
            return last_n, "ok"
        time.sleep(1.0)
    return 0, ("AXErr=%s 末次窗数=%d 进程=%r 存活=%s"
               % (last_e, last_n, ps_state(pid), alive(pid)))


def wait_win(pid, timeout=30):
    return wait_win_d(pid, timeout)[0]


def alive(pid):
    """进程还在不在。这条判据能把「产品把原实例弄没了」和
    「量具这一轮没说话」分开 —— 两者的修法完全不同，混在一起报红
    会把人引到错误的方向上。"""
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))],
                        capture_output=True, text=True).stdout.strip()[:4]
    # D51c 实测踩实：原来这里只有 `os.kill(pid, 0)`，而**僵尸进程
    # kill(pid,0) 是成功的** —— 于是「被测物早在 16s 前就自己退出了」
    # 会被读成「它活着，只是 AX 读不到」，把时序问题误报成量具问题。
    return bool(st) and not st.startswith("Z")


def ps_state(pid):
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))],
                        capture_output=True, text=True)
    return (r.stdout or "").strip()[:4]


def live_app_pids():
    """进程面抓**活着的**被测物实例：pgrep -f 按可执行路径匹配，再滤僵尸。

    只看 pgrep 会误报 —— 僵尸的 ps -o stat= 以 Z 开头，D51c 实测本机就躺着
    几个（含早已从 /Applications 消失的 AgentOS.app，占着 pid 不释放）。
    它们不占 AX、不参与接管仲裁，不算「另一个活实例在跟我抢窗口」。
    量具自己也要除外，否则会把自己当成环境脏而拒绝开跑。
    """
    r = subprocess.run(["pgrep", "-f", APP_EXE_MATCH],
                       capture_output=True, text=True)
    out = []
    for tok in (r.stdout or "").split():
        tok = tok.strip()
        if not tok.isdigit() or int(tok) == os.getpid():
            continue
        st = ps_state(int(tok))
        if st and not st.startswith("Z"):
            out.append((int(tok), st))
    return out


def lsappinfo_pids():
    """产品自己的仲裁口径：main.swift 的单实例仲裁按 bundleIdentifier 走
    `NSRunningApplication.runningApplications(withBundleIdentifier:)`
    （native/Sources/AgentOSShell/main.swift:33-46），所以「环境脏不脏」
    必须用同一口径问一遍系统 —— pgrep 匹配**命令行**、lsappinfo 匹配
    **注册的 app 身份**，两者不是一回事，少问一个就漏一类脏。"""
    r = subprocess.run(["lsappinfo", "info", APP_BUNDLE_ID],
                       capture_output=True, text=True)
    txt = (r.stdout or "") + (r.stderr or "")
    return [int(m) for m in re.findall(r"pid\s*=\s*(\d+)", txt)]


def env_dirty_reasons():
    """fail-closed 预检：本门要求 AgentOS.app 进程排他才能开跑。

    与 gates.sh 的 D102 门链锁同一哲学 —— 量具自己起实例判「接管/最小化」，
    若环境里已有别人的活实例，接管分支激活的就不是本门起的那个：
    2026-10-10 16:42 实测踩实，环境里仍有先前 open AgentOS.app 留下的
    pid=73461 在跑，于是整门 6/11 全红：
      · 场景 1 两个裸 env 启动的实例（不带 AGENTOS_MULTI_INSTANCE=1）被
        仲裁激活 73461 后自己退出 → 进程变 Z、AXWindows=0 AXErr=-25204
        →「窗口起来了 FAIL」「重启后不再是最小化态 FAIL」；
      · 场景 2 的 B 也走接管分支，日志实证 `takeover: unminimize=false |
        已有实例 73461 在运行，已激活并退出` → B 接管的是 73461 而不是
        本门起的 A →「B 日志报了 unminimize=true FAIL」+「A 的窗口被拉回来
        FAIL（A mini=True）」。
    四条红全部是**环境不排他**，没有一条是产品坏。判红之前先判环境，
    才是 rc=1（产品红）与 rc=3（环境红）的分界。
    """
    reasons = []
    seen = {}
    for pid, st in live_app_pids():
        seen.setdefault(pid, {"st": st, "src": []})["src"].append(
            "pgrep -f %s" % (APP_EXE_MATCH,))
    for pid in lsappinfo_pids():
        st = ps_state(pid)
        if st and not st.startswith("Z"):
            e = seen.setdefault(pid, {"st": st, "src": []})
            e["st"] = st
            e["src"].append("lsappinfo info %s" % (APP_BUNDLE_ID,))
    for pid, e in sorted(seen.items()):
        reasons.append("活实例 pid=%d ps_stat=%r 证据源=%s"
                       % (pid, e["st"], " + ".join(e["src"])))
    return reasons


def env_precheck_fail_closed():
    """环境不排他就退出 3，并且**不判产品红**：没观测到产品就不对产品下结论。

    量具不代劳 kill 用户的实例 —— 杀一个可能正在被看的窗口会连坐它的窗口
    状态，反而污染下一轮读数；也让「谁该为这轮红负责」变得说不清。
    """
    reasons = env_dirty_reasons()
    if not reasons:
        return
    print("== MINIMIZE_CHECK_ENV_DIRTY 本门要求 AgentOS.app 进程排他，"
          "实测环境已有其他活实例 ==")
    for r in reasons:
        print("  [脏] %s" % (r,))
    print("  [修法] 请把这些实例退干净后重跑本门：")
    print("         osascript -e 'tell application \"AgentOS\" to quit'"
          "（或在 Dock 里 Cmd-Q）。量具不代劳 kill。")
    print("  [口径] 退出码 3=环境红（不排他）、1=产品红、2=用法/注入。"
          "本门不把环境脏记成产品坏，也不许因此判绿。")
    print("MINIMIZE_CHECK 0/0（未开跑：环境不排他，产品未被本门观测）")
    print("MINIMIZE_CHECK ENV_DIRTY 是环境红 RED 退出码=3（非产品红）")
    print("MINIMIZE_CHECK_ENV_DIRTY")
    sys.exit(3)


def launch(probe_out, multi=False, autoclose="900", log=None, probe=True):
    env = dict(os.environ)
    env["AGENTOS_URL"] = "http://127.0.0.1:8790"
    env["AGENTOS_AUTOCLOSE"] = autoclose
    # probe=False：**不起探针序列**。原因不是省时间，是探针序列走完会
    # 自己把进程杀掉 —— MainWindowController.swift:732 序列排空 →
    # Probe.swift:122 `flush(quit:true)` → `NSApp.terminate`。
    # D51c 实测：这条序列名义 ~14s，于是任何「起实例 → 等几十秒 → 再测
    # 窗口态」的读法，都会在测量开始之前先被被测物自己退出。场景 2 的
    # 判据是「B 接管后 A 的窗口有没有被拉回来」，探针走查与它无关，
    # 带着它只会把靶子提前弄死。场景 1 的 p3 需要那份证据，保持探针。
    if probe:
        env["AGENTOS_PROBE_OUT"] = probe_out
    if multi:
        env["AGENTOS_MULTI_INSTANCE"] = "1"
    # log 不为 None 时落盘：D105b 实测 AX 会间歇性不暴露窗口，真出问题时
    # 「被测物自己的 stdout/stderr」是唯一能说清它当时在干什么的证据。
    # 早先这里一律 DEVNULL，于是上一轮只能得到一句「读不到」。
    sink = subprocess.DEVNULL if log is None else open(log, "w")
    p = subprocess.Popen([BIN], env=env, stdout=sink, stderr=sink)
    p._sink = sink
    return p


def kill(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=10)
    s = getattr(p, "_sink", None)
    if s not in (None, subprocess.DEVNULL) and hasattr(s, "close"):
        s.close()


RESULTS = []

if __name__ == "__main__":
    # 预检只在**本门当门跑**时执行。曾经它是裸的模块级调用（2026-10-14
    # W11 实测踩实，链内两次 rc=3）：native_gate 的「拉回窗口」助手
    # import 本模块复用 AX helper，环境一脏，**导入这个动作本身就
    # sys.exit(3)** —— MINIMIZE_CHECK 块输出出现在 native_gate 自己的
    # stdout 里，报红的是"别人家的预检"；且 SystemExit 不是 Exception，
    # 从 run_ax_probe 的 except Exception 头顶飞过去，整门死得没头没尾。
    # 预检语义：谁当门跑谁预检；被 import 的模块只提供助手，不做自杀式
    # 副作用。native_gate 自己的排他预检在它的 run_gate 头部显式做。
    env_precheck_fail_closed()


def step(name, ok, detail, count=True):
    # count=False = 这条判据本轮**没被判**（量具没开口），打出来是为了别让人
    # 以为它绿了。判据没判绝不算绿 —— 但也不许把「没判」说成「产品坏了」：
    # 那是一个没观测到的宣称。红项归量具那条，整体 rc 仍然是 1。
    if count:
        RESULTS.append((name, ok, detail))
    if not count:
        # 不判的判据打 FAIL/ok 都会跟上面的总账对不上（它压根没进 RESULTS），
        # 看着像门自相矛盾。打「不判」。
        tag = "不判 "
    else:
        tag = {True: "ok  ", False: "FAIL", None: "跳过"}[ok if ok in (True, False) else None]
    print("%-40s %s  %s" % (name, tag, detail), flush=True)


PB = os.path.join(ROOT, "work/native/probe_minchk_b.json")
PA = os.path.join(ROOT, "work/native/probe_minchk_a.json")

def main():
    print("=== 场景 1：正常启动路径（AppDelegate.showWindow 的 deminiaturize） ===")
    p1 = launch(PB, probe=False)
    n, why1 = wait_win_d(p1.pid)
    step("窗口起来了", n >= 1, "pid=%d AXWindows=%d %s" % (p1.pid, n, why1))

    r = set_minimized_settled(p1.pid, True)
    step("AX 把窗口按进 Dock", r == "ok" and ax_minimized_settled(p1.pid) is True,
         "set=%s now_mini=%s" % (r, ax_minimized_settled(p1.pid)))
    kill(p1)
    time.sleep(2.5)

    p2 = launch(PB, probe=False)
    n2, why2 = wait_win_d(p2.pid)
    m2 = wait_not_minimized(p2.pid)
    step("重启后不再是最小化态", m2 is False,
         "pid=%d AXWindows=%d mini=%s %s" % (p2.pid, n2, m2, why2))
    kill(p2)
    time.sleep(2.5)

    # 单独再起一个「会自己收尾落盘」的实例，只验两个新键真的写进去了。
    PC = os.path.join(ROOT, "work/native/probe_minchk_c.json")
    p3 = launch(PC, autoclose="7")
    wait_win(p3.pid)
    # 探针只在 AGENTOS_AUTOCLOSE 到点时才收尾落盘，SIGTERM 不落盘。
    # 门自己也是这么等的（AUTOCLOSE=90 然后等文件），这里照抄同一套。
    t0 = time.time()
    while time.time() - t0 < 30 and not os.path.isfile(PC):
        time.sleep(1.0)
    kill(p3)
    facts = json.load(open(PC)) if os.path.isfile(PC) else {}
    step("probe 记下了起时状态与解除动作",
         "window_minimized_before_restore" in facts
         and "window_deminaturized_on_launch" in facts,
         "before=%s restored=%s mini=%s visible=%s" % (
             facts.get("window_minimized_before_restore"),
             facts.get("window_deminaturized_on_launch"),
             facts.get("window_is_miniaturized"),
             facts.get("window_is_visible")))

    print()
    print("=== 场景 2：单实例接管路径（main.swift 的 AX 解除最小化） ===")
    LOG_A = os.path.join(ROOT, "work/native/minimize_check_A.log")
    pa = launch(PA, multi=True, log=LOG_A, probe=False)
    na, why_a = wait_win_patient(pa.pid)
    step("A 实例窗口起来了", na >= 1, "pid=%d AXWindows=%d %s" % (pa.pid, na, why_a))
    setr = set_minimized_settled(pa.pid, True)
    ma_s, meta_s = read_mini_patient(pa.pid, timeout=20.0)
    step("A 被按进 Dock", setr == "ok" and ma_s is True,
         "set=%s now_mini=%s %s" % (setr, ma_s, meta_s))

    BOUT = os.path.join(ROOT, "work/native/minimize_check_B.log")
    bf = open(BOUT, "w")
    pb = subprocess.Popen([BIN],
                          env=dict(os.environ,
                                   **{"AGENTOS_URL": "http://127.0.0.1:8790"}),
                          stdout=bf, stderr=bf, text=True)
    # 用文件而不是 PIPE：上一轮 communicate(timeout=40) 超时的原因是
    # B 退出后管道没收到 EOF（不是 B 卡住）—— 实测 B 走接管分支只花 0s。
    try:
        pb.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pb.kill()
        pb.wait(timeout=10)          # 少了这一句，returncode 会是 None
    bf.close()
    errtxt = open(BOUT).read()
    step("B 接管后自己退出", pb.returncode == 0,
         "rc=%s（None=超时后 kill 未 wait，是门的 bug 不是产品的）" % (pb.returncode,))
    # D51d 实测（work/native/diag_takeover.json）：真实用户路径连做 4 轮，
    # B 每轮都自己打出 `unminimize=true`，且 4/4 都真把 A 拉回来。所以这条
    # 比任何量具读数都硬 —— 它是**产品自己写的证据**，不是我们从外面猜的。
    step("B 日志报了 unminimize=true",
         "takeover: unminimize=true" in errtxt,
         errtxt.strip().replace("\n", " | ")[:160])
    time.sleep(2.5)
    alive_a = alive(pa.pid)
    st_a = ps_state(pa.pid)
    step("A 接管后还活着", alive_a, "ps_stat=%r" % (st_a,))
    ma0, meta0 = read_mini_patient(pa.pid, timeout=25.0) if alive_a else (None, None)
    if not alive_a:
        # 产品缺陷，不是量具问题：原实例没了，后面所有读数都没意义。
        ma = None
        ma_detail = "A 进程已死（ps_stat=%r）-> 接管把原实例搞没了" % (st_a,)
    elif meta0 and meta0.get("nudges"):
        # 读不到 ≠ 最小化。这是量具这一轮没说话（D105b/D51d 实测 AX 会在
        # 1 窗/0 窗之间自发摆，周期约 15–25s），不是产品缺陷。read_mini_patient
        # 里每 4 拍没开口就 AX 置前补一次 —— D51d 实测这是**唯一被量过的
        # 能把读数救回来的手段**（diag_fm.json：塌陷后立刻置前，下一拍读回 1 窗；
        # 同时 diag_fg2.json 证明不置前 45s 内也稳定读到 1 窗，所以置前不是必要条件）。
        # 置前**只在读不到时**用，且置前后的读数都进 detail —— 因为置前理论上
        # 可能顺手把最小化的窗口 restore 掉，那样这条判据就成了「我自己弄的绿」。
        ma = ma0
        ma_detail = ("患者采样 %s（直读始终读不到，靠置前补 %d 次才拿到值）"
                     % (meta0, meta0.get("nudges")))
        step("量具读到了 A 的窗口态", ma is not None,
             "A 存活，直读在 %s 秒里没开口，靠 %d 次置前补到了值 mini=%s"
             % (meta0.get("waited"), meta0.get("nudges"), ma))
    else:
        ma = ma0
        ma_detail = ("直读=%s %s（量具本就说话，未做任何补救）"
                     % (ma0, meta0))
        # 量具说不说话是**另一条**判据，不许借产品判据的红绿来表达：
        # 「产品没恢复」和「AX 这一轮没开口」修法完全不同。
        step("量具读到了 A 的窗口态", ma is not None,
             "直读患者采样：mini=%s %s" % (ma0, meta0))
    gauge_spoke = ma is not None
    step("A 的窗口被拉回来了", ma is False,
         "A mini=%s | %s" % (ma, ma_detail),
         count=gauge_spoke or not alive(pa.pid))
    alog = (open(LOG_A).read().strip().replace("\n", " | ")[:150]
            if os.path.isfile(LOG_A) else "没落盘 -> 没法事后追")
    step("A 进程活着（日志可查）", (not alive_a) or bool(alog), alog, count=False)
    kill(pa)

    bad = [x for x in RESULTS if not x[1]]
    print()
    print("MINIMIZE_CHECK %d/%d" % (len(RESULTS) - len(bad), len(RESULTS)))
    print("MINIMIZE_CHECK_OK" if not bad else "MINIMIZE_CHECK_RED")
    sys.exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
