#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部进程读交付物的 macOS AX（辅助功能）树 —— 屏幕阅读器层的**物证**。

为什么要单独一个脚本（而不是把这段塞进 native_gate.py）：
  native_gate 由**系统 python3** 跑，实测系统 python3 与 ~/.codex/capvenv
  **都没有** PyObjC（有 PyObjC 的是项目 .venv-pw）。把 `import
  ApplicationServices` 直接写进 native_gate 会让这两条判据在真实链里永久
  「读不到 → 判红」或更糟「静默跳过 → 假绿」。所以拆成本脚本，由 native_gate
  用探针解释器调起，能力可用性显式暴露在输出里。

为什么**必须从外部进程**读（D105 实测）：
  进程内自己读自己的 AX 树没有意义（拿不到系统视角的窗口注册与焦点面）。
  实测 App 启动后需 >7s 才在 kAXWindows 注册，早采样读到 n=0 是**时序**，
  不是产品缺陷 —— 所以本脚本按 --wait 轮询到「有窗且树够大」或超时，
  并把 err / 环数 / 菜单节点 / 文本数全部如实输出，不做美化。

用法：ax_probe.py --proc AgentOS [--wait 25] [--poll 1.0]
  rc=0 读到树；rc=2 没读到（stdout 仍是 JSON，含 ok=false 与原因）；
  rc=1 参数错；rc=3 PyObjC 不可用。
  注意：**不**走 ctypes 直调 AX 符号（实测 segfault rc=139）。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

AX_TREE_MAX_NODES = 3000       # hard budget; measured tree is 304 nodes
AX_TREE_MAX_SECONDS = 6.0      # hard wall clock. The app serves these AX queries
                               # on its MAIN thread, so an unbounded walk starves
                               # it: with a 20k budget, /__projects blew past the
                               # panel 6s timeout and native_gate projects-*
                               # went RED on timeout. 3000 is still ~10x the
                               # measured tree.
AX_TREE_MAX_DEPTH = 40
AX_MENU_ROLES = ("AXMenuBar", "AXMenu", "AXMenuItem")
AX_MIN_SERIES_SAMPLES = 6        # 序列采够几个样本，「多数暴露」这条判据才说话。
AX_DIAG_BUNDLE_PLIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "AgentOS.app", "Contents", "Info.plist")
                               # 样本太少时 need=ceil(N*AX_SERIES_MIN_PCT/100)
                               # 会低到毫无意义（N=1 时 need=1，一条好就 100%
                               # 通过），所以「采样条数」本身也得是一条判据。
                               # 配合下面 AX_SERIES_MIN_PCT 一起用。
AX_MIN_CONTENT_NODES = 8       # 实测最瘦的一棵（Finder）也有 144
AX_MIN_TEXTS = 3               # 实测最少的也有 35
AX_MIN_WEB_AREA = 1            # D105b 实测：被测 app 的网页内容区
                               # 加载完才出现在 AX 树里。轮询判据加上它，
                               # 就不会在 t=0.1s 抓到一棵「只有窗口外壳」的
                               # 树就宣布成功 —— 实测加载前那棵是
                               # 23 节点/7 文本且 **没有 AXWebArea**，
                               # 加载后才有（最小一棵 27 节点仍带 AXWebArea=1）。
AX_SERIES_MIN_PCT = 60         # D105b 实测：AX 暴露是**间歇**的 —— 同一份
                               # 构建、同一条命令，有时连续 18s 都是 1 个窗口，
                               # 有时 25s 全是 0（err 同样是 0），而 app 自己
                               # 报的 on-screen 计数全程是 1、mini 全程 false。
                               # 单次采样因此不能当判据：它测的是「那一刻」。
                               # 改成一条时间序列，并要求**大多数采样**都真的
                               # 暴露了内容区 —— 这比单次采样更严（一次说好不算
                               # 数），也真的稳定（不会因为撞上间歇期就翻红）。

# 每次采样的「置前 -> 重读」内层重试（D110，2026-10-11 长链实测加的）。
# 旧实现每采只补救一次：一次置前 + 一次重读，没开口就把这一采记成
# 「没暴露」。长链里窗口服务器被别的 gate 一起抢时，单次补救的命中率不够
# —— 实测同一份构建，单跑 12/12 暴露，45 门全链里只 1/12。判据一条没动
# （AX_SERIES_MIN_PCT / AX_MIN_SERIES_SAMPLES 照旧），只是把「量具没采到」
# 多给几次机会 —— 与 native_gate 的 AX_PROBE_TRIES 同一条口径：
# **只有量具自己没开口才重试；读数说「树不好」是结论，不重试**。
AX_INNER_TRIES = 4
AX_INNER_SLEEP = 0.35

# 遍历**早退**阈值（D51e 实测加的）。为什么必须早退：
#   实测（work/native/ax.json，2026-10-06 12:5x）一棵树 287 个节点要花
#   5.5—6s 走完（WKWebView 的 AX 每个节点都要过一次 Web 内容进程），
#   而 native_gate 的预算是 AX_WAIT=40s、AX_POLL=2.5s —— 于是 40s 只够
#   5 个采样，而 ax-sustained 要求 samples ≥ AX_MIN_SERIES_SAMPLES=6。
#   结果是「红」判的其实不是产品，是**抽样预算装不下判据本身**：
#   samples_exposed=4/need=3（80% > 60%）全过，只栽在 5 < 6。
#   节点数在 27↔1084 之间摆（D105b），所以这条红会随视图/面板状态
#   随机出现 —— 门自己不稳定。
# 早退让一棵树只走到**判据够用**就停：判据要的只是
#   content_nodes ≥ 8 / web_area ≥ 1 / texts ≥ 5，一条几十节点的树就够，
#   剩下的几百个节点对判据零信息量，却吃掉整轮预算。
# 阈值只往**严**的方向取（比 native_gate.judge_ax 的下限留余量），所以
# 早退后报出去的数一定还过得去门的判据，不会凭空造一条假红；反过来，
# 红路径（树一直不达标）仍会走满 3000 节点 / 6s 的硬预算，该红还是红。
# 判据本身一条没动。
# 三个数同时要满足两个方向：高于门那边的下限（不假红），又不高于实测
# 可能达到的值（否则早退形同虚设）。D105b 实测 texts 最小值 7、
# 最小树 27 节点，所以取 7 / 10 而不是拍一个更大的数。
AX_EARLY_CONTENT = 10          # 门那边 judge_ax 要 ≥ 8
AX_EARLY_TEXTS = 7             # 门那边要 ≥ 5；D105b 实测最小值就是 7
AX_EARLY_WEB_AREA = 1          # 与门那边一致


def _bundle_id():
    """当场从 AgentOS.app 的 Info.plist 读，不写死。

    写死过一次就查不到：初版以为叫 com.agentos.console，于是
    NSRunningApplication 一条都列不出来，量具看起来像「系统没注册这个
    app」，其实是 bundle id 读错了。"""
    try:
        import plistlib
        with open(AX_DIAG_BUNDLE_PLIST, "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier") or ""
    except Exception:                                      # noqa: BLE001
        return ""


def _cg_counts(pid):
    """窗口服务器视角：这个 pid 名下有几个窗、其中几个在屏上。

    为什么必须有第三方视角：AX 塌到 0 与 app 自报 on-screen=1 会同时出现，
    两边都是「自己看自己」，谁也没资格给对方作证。CGWindowList 是窗口
    服务器的账本，是本机能拿到的第三方读数。"""
    try:
        import Quartz
        lst = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
        mine = [w for w in lst if w.get("kCGWindowOwnerPID") == pid]
        on = [w for w in mine if w.get("kCGWindowIsOnscreen")]
        return len(mine), len(on)
    except Exception:                                      # noqa: BLE001
        return None, None


def _ns_state(pid, bundle):
    """LaunchServices 视角：这个 pid 是不是被当成一个正经 app 注册着。"""
    if not bundle:
        return {}
    try:
        import AppKit
        for a in AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle):
            if int(a.processIdentifier()) == pid:
                return {"ns_registered": True,
                        "ns_active": bool(a.isActive()),
                        "ns_hidden": bool(a.isHidden())}
    except Exception:                                      # noqa: BLE001
        pass
    return {"ns_registered": False}


def _pid_list(proc):
    p = subprocess.run(["pgrep", "-x", proc], capture_output=True, text=True)
    return [int(x) for x in p.stdout.split() if x.strip().isdigit()]


def _ensure_fg(pid, timeout=6.0):
    """把量具站回被测物那一侧；返回精简结果（只留判据要用的那几个键）。

    为什么必须做（D106 链内实测，不是理论）：同一次运行里，app 自报的
    window-on-screen / window-shown-at-launch 全是绿的（窗口确实在屏上），
    而**外部** AX 读到的 kAXWindows 是空列表。原因是 macOS 只把**前台**
    app 的窗口树交给别的进程读；被测 app 一旦不是前台，AX 量具就量到
    「空」，而这个「空」会被 ax-window-exposed / ax-content-tree /
    ax-sustained 三条一起当成「产品没有内容区」—— 量具的姿势问题被
    误判成产品缺陷。

    所以每次采样**之前**都复查一次前台；已经被抢走就立刻置回，并计数。
    计数会进 ax.json：`reactivations=0` 说明整轮都没被抢过，
    `reactivations>0` 说明长链里前台被抢过 —— 这两件事必须能从证据上区分，
    不能只留一个「红/绿」。

    D51e 实测（本轮根因）：这条**一直是静默空操作**。
      import fg_activate; fg_activate.ensure_foreground(pid, timeout=2.0)
      -> {"attempts": 0, "ok": false, "mechanisms": [],
          "why": "no-PyObjC No module named 'AppKit'"}
    fg_activate 走 AppKit，探针解释器（.venv-pw）有 PyObjC 但**没装
    AppKit**，于是 attempts=0、mechanisms=[] 就把「没做」当「做过」返回；
    而旧实现只读 `already_active is False`，结果 `--keep-foreground`
    （默认开）整轮零次真正置前，ax.json 里 reactivations 永远是 0 ——
    量具塌陷时没救，事后也没留下「试过没成」的痕迹。D50 那轮整条 D50
    红项就是这么来的。
    所以这里补一条**纯 AX** 兜底：AXUIElementSetAttributeValue(app,
    kAXFrontmostAttribute, True)。D51d 已实测它是唯一能把读数救回来的
    手段（work/native/diag_fm.json：塌陷后置前，下一拍 0 -> 1 窗）。
    与 minimize_check.nudge_frontmost 同一套实现。
    """
    if not pid:
        return {"ok": None, "reactivated": False, "why": "no-pid"}
    pid = int(pid)
    # D110 快速路径（2026-10-11 长链实测加的）：先问一句「它现在是不是
    # 前台」，是就零成本返回；不是就先打一枪纯 AX 置前（毫秒级）再复核。
    #
    # 为什么必须改掉「一上来就走 AppKit」：旧实现每轮先调
    # fg_activate.ensure_foreground(timeout=6.0)，这台机上前台常年被别的
    # app 占着，那条路每轮走满 6s 才轮到 AX 兜底 —— 单轮 8.6s，AX_WAIT=140
    # 只够 ~16 个样本；而且长链里别的 gate 一起抢窗口服务器时，这 6s 里读数
    # 一路是塌的，采样预算被「等」吃掉而不是被「量」吃掉。顺序改成
    # 「先 AX 快枪，再 AppKit 慢枪」：AX 快，AppKit 能切 Space，两者都留。
    try:
        if _ax_is_frontmost(pid):
            return {"ok": True, "reactivated": False,
                    "mechanism": "already-front", "attempts": 0, "why": ""}
    except Exception:                                            # noqa: BLE001
        pass
    pre = None
    try:
        pre = _ax_nudge_frontmost(pid)
        if pre == 0 and _ax_is_frontmost(pid):
            return {"ok": True, "reactivated": True,
                    "mechanism": "ax-frontmost", "attempts": 1, "why": ""}
    except Exception as e:                                       # noqa: BLE001
        pre = "exc:%r" % (e,)
    # 慢路径：AppKit activate（唯一能切 Space / 把 Dock 图标弹出来的路）。
    try:
        import fg_activate
    except Exception as e:                                       # noqa: BLE001
        r = {"ok": None, "why": "fg_activate-import-failed %r" % (e,)}
    else:
        r = fg_activate.ensure_foreground(pid, timeout=timeout, poll=0.4)
    if r.get("ok"):
        return {"ok": True, "reactivated": r.get("already_active") is False,
                "mechanism": "fg_activate", "attempts": r.get("attempts"),
                "why": str(r.get("why") or "")[:120]}
    # 两路都没把它弄到前台：再补一枪纯 AX，并把三路的 why 都带出去 ——
    # 下一轮长链再塌时，证据里能直接读出「卡在哪一路」。
    try:
        e = _ax_nudge_frontmost(pid)
    except Exception as ex:                                      # noqa: BLE001
        return {"ok": None, "reactivated": False, "mechanism": "none",
                "why": "fg=%r ax=%r pre=%r"
                       % (str(r.get("why"))[:60], ex, pre)}
    return {"ok": e == 0, "reactivated": e == 0,
            "mechanism": "ax-frontmost" if e == 0 else "none",
            "attempts": (r.get("attempts") or 0) + 1,
            "why": ("fg=%s | ax-set-frontmost=%s | pre=%s"
                    % (str(r.get("why") or "")[:60], e, pre))}


def _ax_nudge_frontmost(pid):
    """AX 客户端激活 app 的标准路径，返回 AXError（0 = 成功）。

    为什么必须真的调用它：macOS 只把**前台** app 的窗口树交给别的进程读。
    同一次运行里 app 自报 window-on-screen 全绿，而外部 AX 读到的
    kAXWindows 是 -25204（cannot complete）/ 0 窗 —— 那是量具站错了位置，
    不是产品没有内容区。详见 _ensure_fg 的 D51e 实测记录。

    注意它**不能**当产品判据的证据：置前理论上可能顺手 restore 一个
    最小化的窗口。所以只用它救**读数**，不用它替代读数 —— 置前后的两个
    读数都进 ax.json（_reading_digest / series[i]["first"|"retry"]）。
    """
    import ApplicationServices as AS
    app = AS.AXUIElementCreateApplication(int(pid))
    return AS.AXUIElementSetAttributeValue(app, AS.kAXFrontmostAttribute, True)


def _ax_is_frontmost(pid):
    """AX 视角：这个 pid 现在是不是前台。读不到就抛/返回 False（不猜）。

    与 fg_activate 的 AppKit 视角互补：AppKit 的 isActive 在窗口服务器
    被别的进程占着时可能滞后，而 AX 的 kAXFrontmost 是同一棵树的读数。
    两路都问一遍再决定要不要动慢枪，是 D110 快速路径的一部分。
    """
    import ApplicationServices as AS
    app = AS.AXUIElementCreateApplication(int(pid))
    err, val = AS.AXUIElementCopyAttributeValue(
        app, AS.kAXFrontmostAttribute, None)
    return err == 0 and bool(val)


def _reading_digest(s):
    """一次采样的关键读数 —— 两个读数按同一组字段留痕，才好比较。"""
    return {"ok": bool(s.get("ok")),
            "windows_err": s.get("windows_err"),
            "windows": s.get("windows"),
            "web_area": s.get("web_area"),
            "content_nodes": s.get("content_nodes"),
            "nodes": s.get("nodes"),
            "texts": s.get("texts"),
            "why": str(s.get("why") or "")[:80]}


def _exposed(s):
    """「这一采真的暴露了内容区」—— 与 ax-sustained 同口径。

    这里只定义「读数算不算开口」，阈值一条都没动；严格程度仍由
    AX_SERIES_MIN_PCT + AX_MIN_SERIES_SAMPLES 在 judge_ax 里说话。
    """
    return bool(s.get("ok")
                and s.get("windows_err") == 0
                and isinstance(s.get("windows"), int) and s["windows"] >= 1
                and int(s.get("web_area") or 0) >= AX_MIN_WEB_AREA)


def _title(w):
    import ApplicationServices as AS
    for k in (AS.kAXTitleAttribute, AS.kAXValueAttribute):
        try:
            e, v = AS.AXUIElementCopyAttributeValue(w, k, None)
            if e == 0 and v:
                return str(v)
        except Exception:                                  # noqa: BLE001
            pass
    return ""


def _once(proc, pid=None):
    """采一次 -> dict（ok=False 时带 why）。"""
    import ApplicationServices as AS
    out = {"proc": proc, "ok": False}
    pids = [pid] if pid else _pid_list(proc)
    out["pids"] = pids
    if not pids:
        out["why"] = "进程 %r 不在运行" % proc
        return out
    best = ""
    for pid in pids[:3]:
        try:
            el = AS.AXUIElementCreateApplication(pid)
            e0, wins = AS.AXUIElementCopyAttributeValue(
                el, AS.kAXWindowsAttribute, None)
            if e0 != 0:
                best = "pid=%d kAXWindowsAttribute err=%d" % (pid, e0)
                continue
            wins = list(wins or [])
            out["pid"] = pid
            out["windows_err"] = 0
            out["windows"] = len(wins)
            out["window_titles"] = [_title(w) for w in wins]
            roles, n, cyc, menu, texts = {}, 0, 0, 0, []
            content = 0
            stack = [(w, 0) for w in wins]
            seen = set()
            t_start = time.time()
            while stack and n < AX_TREE_MAX_NODES:
                if time.time() - t_start > AX_TREE_MAX_SECONDS:
                    out["budget_hit"] = "wall-clock %.1fs" % AX_TREE_MAX_SECONDS
                    break
                node, depth = stack.pop()
                key = hash(node)
                if key in seen:
                    cyc += 1
                    continue
                seen.add(key)
                n += 1
                e1, role = AS.AXUIElementCopyAttributeValue(
                    node, AS.kAXRoleAttribute, None)
                role = role if e1 == 0 and isinstance(role, str) else None
                if role:
                    roles[role] = roles.get(role, 0) + 1
                    if role in AX_MENU_ROLES:
                        menu += 1
                    else:
                        content += 1
                        e3, txt = AS.AXUIElementCopyAttributeValue(
                            node, AS.kAXValueAttribute, None)
                        if e3 != 0 or not txt:
                            e3, txt = AS.AXUIElementCopyAttributeValue(
                                node, AS.kAXTitleAttribute, None)
                        if e3 == 0 and txt:
                            texts.append(str(txt))
                # 菜单子树不进递归：菜单项对「界面有没有内容」零信息量，
                # 却能把节点预算吃光（D105 实测旧量具 nodes=600 全是菜单）。
                if role in AX_MENU_ROLES or depth >= AX_TREE_MAX_DEPTH:
                    continue
                e2, kids = AS.AXUIElementCopyAttributeValue(
                    node, AS.kAXChildrenAttribute, None)
                if e2 == 0 and kids is not None:
                    stack.extend((k, depth + 1) for k in list(kids))
                # 早退（见 AX_EARLY_* 的实测理由）：判据够用就停，
                # 别把剩下的节点预算和 6s 墙钟花在零信息量的节点上。
                # 停在这里时 nodes/content_nodes/texts 都是**下界**，
                # 所以显式标 early_exit，别让人把下界当成实测总数。
                if (content >= AX_EARLY_CONTENT
                        and len(texts) >= AX_EARLY_TEXTS
                        and roles.get("AXWebArea", 0) >= AX_EARLY_WEB_AREA):
                    out["early_exit"] = (
                        "第 %d 个节点就够判据（非菜单 %d / 文本 %d / AXWebArea %d）"
                        % (n, content, len(texts),
                           roles.get("AXWebArea", 0)))
                    break
            out["web_area"] = roles.get("AXWebArea", 0)
            out["nodes"] = n
            out["cycles"] = cyc
            out["menu_nodes"] = menu
            out["content_nodes"] = content
            out["texts"] = len(texts)
            out["roles"] = roles
            out["text_samples"] = texts[:8]
            if content >= AX_MIN_CONTENT_NODES and len(texts) >= AX_MIN_TEXTS:
                wa = roles.get("AXWebArea", 0)
                if wa >= AX_MIN_WEB_AREA:
                    out["ok"] = True
                    return out
                best = ("pid=%d 有 %d 节点/%d 文本但 AXWebArea=%d"
                        "（网页内容区还没进 AX 树）"
                        % (pid, n, len(texts), wa))
            else:
                best = ("pid=%d windows=%d nodes=%d 非菜单=%d 文本=%d"
                        "（不足 %d 节点 / %d 文本）"
                        % (pid, len(wins), n, content, len(texts),
                           AX_MIN_CONTENT_NODES, AX_MIN_TEXTS))
        except Exception as e:                              # noqa: BLE001
            best = "pid=%d 遍历异常 %r" % (pid, e)
    out["why"] = best or "AX 树遍历未产出任何节点"
    return out


def main(argv=None):
    """采一条**时间序列**，而不是一次采样。

    为什么必须是一条序列（D105b 实测踩实）：AX 暴露是**间歇**的。同一份
    构建、同一条命令，同一个被测 pid，有时连续 18s 每秒都读到 1 个窗口，
    有时连续 25s 全是 0（windows_err 同样是 0，没有报错），而同一轮里 app
    自报的 on-screen 计数全程是 1、miniaturized 全程 false。所以单次采样
    测的只是「那一刻」，用它当判据等于让门抽签。
    改成序列后判据问的是「**大多数时候**屏幕上有没有内容区」——比单次采样
    更严（一次说好不算数），也真的稳（撞上间歇期不会翻红）。

    输出里 ok / sustained 是两件事，别混：
      ok        = 至少读到过**一棵加载后**的树（不含外壳空树）
      sustained = 序列里够多采样真的暴露了内容区
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--proc", required=True)
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--wait", type=float, default=18.0,
                    help="序列采样总预算（秒）。必须明显小于被测 app 探针序列的寿命")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--keep-foreground", dest="keep_fg", action="store_true",
                    default=True, help="每次采样前把被测 app 置回前台（默认开）")
    ap.add_argument("--no-keep-foreground", dest="keep_fg",
                    action="store_false")
    a = ap.parse_args(argv)
    try:
        import ApplicationServices
    except ImportError as e:
        print(json.dumps({"ok": False, "why": "no-PyObjC %s" % e}))
        return 3
    t0 = time.time()
    deadline = t0 + a.wait
    series, best, last_pid = [], None, 0
    last_raw = {}
    reactivations, fg_failures = 0, 0
    nudges, retries = 0, 0
    while len(series) < a.samples and time.time() < deadline:
        fg = {"ok": None, "reactivated": False}
        if a.keep_fg and a.pid:
            fg = _ensure_fg(a.pid)
            if fg.get("reactivated"):
                reactivations += 1
            if fg.get("ok") is False:
                fg_failures += 1
        s = _once(a.proc, pid=a.pid or None)
        if s.get("pid"):
            last_pid = int(s["pid"])
        first = _reading_digest(s)
        exp_first = _exposed(s)
        # 直读读不到窗口/内容区 —— 按 D51d，这是量具站错位置，不是产品缺陷。
        # 置前后**立刻**重读一次，并且两个读数都进 ax.json：只留好的那个
        # 等于把「读数本来是塌的」这段事实抹掉，红项就没法复现了。
        retry = None
        nudge_err = None
        retry_n = 0
        if a.keep_fg and not exp_first and (a.pid or last_pid):
            # D110：一次补救不够。长链里窗口服务器被别的 gate 一起抢时，
            # 「置前 -> 立刻重读」单发的命中率会掉到 ~8%（45 门全链实测
            # 1/12）。这里改成有界多发（AX_INNER_TRIES），命中就停 ——
            # 阈值一条没动，动的只是「量具没开口时多敲几次门」。
            target = int(a.pid or last_pid)
            for _ in range(AX_INNER_TRIES):
                try:
                    if _ax_nudge_frontmost(target) == 0:
                        nudges += 1
                except Exception as e:                          # noqa: BLE001
                    nudge_err = repr(e)
                    break
                time.sleep(AX_INNER_SLEEP)
                s2 = _once(a.proc, pid=target)
                if s2.get("pid"):
                    last_pid = int(s2["pid"])
                retry = _reading_digest(s2)
                retries += 1
                retry_n += 1
                if _exposed(s2):
                    if s2.get("ok"):
                        s = s2          # 取到树的那一拍才配当 best
                    break
        last_raw = s
        exp = _exposed(s)
        series.append({"t": round(time.time() - t0, 1),
                       "exposed": bool(exp),
                       "exposed_first": bool(exp_first),
                       "windows": s.get("windows"),
                       "web_area": s.get("web_area"),
                       "nodes": s.get("nodes"),
                       "texts": s.get("texts"),
                       "why": str(s.get("why") or "")[:80],
                       "first": first, "retry": retry,
                       "retry_n": retry_n, "nudge_err": nudge_err})
        # 第三方视角：AX 读不出来时，得知道是窗口真没上屏（cg_onscreen=0）
        # 还是窗口在但系统没把它交给 AX（cg_onscreen>=1 却 windows=None）。
        # 这两列是 4.1 定位间歇性塌陷的唯一物证，所以就地采样、失败也留痕。
        if last_pid:
            cg_all, cg_on = _cg_counts(last_pid)
            series[-1].update({"cg_all": cg_all, "cg_onscreen": cg_on})
            series[-1].update(_ns_state(last_pid, _bundle_id()))
        series[-1]["fg_ok"] = fg.get("ok")
        series[-1]["fg_reactivated"] = bool(fg.get("reactivated"))
        series[-1]["fg_mechanism"] = fg.get("mechanism")
        series[-1]["fg_why"] = str(fg.get("why") or "")[:120]
        if s.get("ok") and best is None:
            best = s
        if time.time() + a.poll >= deadline:
            break
        time.sleep(a.poll)
    n = len(series)
    exposed = sum(1 for x in series if x["exposed"])
    exposed_first = sum(1 for x in series if x.get("exposed_first"))
    need = -(-n * AX_SERIES_MIN_PCT // 100)          # ceil，不用浮点
    out = dict(best) if best else {}
    # D51：一棵都没读到时（best=None），上面这行只留下循环自己加的那几个键，
    # windows / windows_err / nodes / content_nodes / window_titles **全部缺失**。
    # 门读到的是 None，于是打印出 `windows_err=None windows=None titles=None`
    # 配着 `target_pid=22698 sampled_pid=22698` —— 自相矛盾且无法定位：
    # 分不清「app 真的没窗口」和「量具这一轮什么都没量到」。
    # 这里把**最后一次采样**的诊断字段原样带上来并显式标 best_is_none，
    # 让红项能说清「最后那一刻到底量到了什么」。阈值一条没动，只补可观测性。
    if best is None and last_raw:
        for _k in ("pids", "pid", "windows_err", "windows", "window_titles",
                   "nodes", "content_nodes", "menu_nodes", "texts",
                   "web_area", "cycles", "budget_hit"):
            if _k in last_raw:
                out.setdefault(_k, last_raw[_k])
        out["best_is_none"] = True
    out["proc"] = a.proc
    out["pid"] = last_pid
    out["samples"] = n
    out["samples_exposed"] = exposed
    out["samples_need"] = need
    out["samples_pct"] = (round(100.0 * exposed / n) if n else 0)
    # 长链专属的量具账：整轮被抢过几次前台、置前失败几次。这两个数不参与
    # 过没过（阈值一条没动），但它们让「红」和「绿」都能说清是产品的问题
    # 还是量具姿势的问题 —— 上一轮就是因为缺这一列，把两者混成了一条红。
    out["reactivations"] = reactivations
    out["fg_failures"] = fg_failures
    # 补救账（D51e）：ax_nudges = 纯 AX 置前真的调成功的次数，
    # ax_retries = 「直读没开口 → 置前 → 重读」发生的次数，
    # samples_exposed_first = **不靠补救**就暴露的采样数。
    # 三者一起看才说得出是产品间歇还是量具间歇：
    #   first 高、exposed 高  -> 底子稳，量具偶尔塌
    #   first 低、exposed 高  -> 每次都得靠置前才能读（更可疑，要看 why）
    #   exposed 低            -> 置前也救不回来，这才是真塌
    out["ax_nudges"] = nudges
    out["ax_retries"] = retries
    out["samples_exposed_first"] = exposed_first
    out["series"] = series
    out["waited"] = round(time.time() - t0, 1)
    out["ok"] = best is not None
    sustained = (best is not None and n >= AX_MIN_SERIES_SAMPLES
                 and exposed >= need)
    out["sustained"] = sustained
    if not out["ok"]:
        out["why"] = str((best or {}).get("why")
                         or (series[-1]["why"] if series else "")
                         or "序列里没有一棵树")
    elif not sustained:
        out["why"] = ("序列 %d 次采样只暴露 %d 次（需 ≥%d 次 = %d%%，"
                      "且样本数需 ≥%d）：AX 暴露是间歇的，这条不够稳"
                      % (n, exposed, need, AX_SERIES_MIN_PCT,
                         AX_MIN_SERIES_SAMPLES))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if sustained else 2


if __name__ == "__main__":
    sys.exit(main())
