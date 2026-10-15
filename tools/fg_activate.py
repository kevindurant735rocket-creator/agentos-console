#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一个已经起来的 app 进程**置到前台**，并把「置前成功没成功」落成 JSON 物证。

为什么这道前置是必需的（D106 实测，不是猜的）：
    AX（屏幕阅读器消费的那棵树）只有在目标 app **真的是前台 app** 时才会
    稳定暴露它的网页内容区。链内实测：AgentOS 探针窗口**真的离开了屏幕**
    （CGWindowListCount 里它的 on-screen 窗口 12 次采样有 11 次是 0），
    同时 NSRunningApplication.isActive() 也是 False —— 于是一条
    ax-window-exposed / ax-content-tree / ax-sustained 一起红。
    把同一个二进制**单独跑**，12/12 次采样 ns_active=True、cg_onscreen=1、
    exposed 92% —— 门是绿的。

    也就是说：这不是 AX 抖动，是「长链跑了几分钟后，前台被别的东西抢走」
    这件事从来没被门当成**前置条件**管过。AX 是屏幕阅读器的量具，量具自己
    必须先站到被测物那一侧；否则量出来的「读不到」会被误当成「产品没有」。

为什么**不**用 `open -a AgentOS`：
    那会起第二个实例。而 AgentOS 是单实例 app，第二个实例会走 takeover 分支
    去 unminimize/steal 第一个实例的窗口 —— 于是「置前」这个动作本身把
    被测物改了。必须**按 pid** 置前同一个进程。

两段式置前，以及为什么必须有两段（D106 实测）：
    第一段 `NSRunningApplication.activateWithOptions_IgnoringOtherApps`。
    在这台机上它**返回 True 但完全无效**：实测对 pid 982 调它，返回 True、
    1s 后 isActive 仍是 False、frontmost 仍是 Finder —— 一个会说谎的
    「成功」比失败更坏，所以裁决只能看 isActive()，不能看返回值。
    第二段 `osascript` 走 System Events 的
    `set frontmost of (first process whose unix id is PID) to true`。
    同一时刻同一条形状实测 rc=0 且 frontmost 真的变成 982。它按 unix id
    点名，同样**不会**起第二个实例。
    每轮试了什么写进 JSON 的 mechanisms，让「置前到底成没成」有物证。

为什么这条独立成脚本而不是塞进 native_gate.py：
    PyObjC 只在项目 .venv-pw 里，native_gate 由系统 python3 跑。与
    ax_probe.py 同样的理由（见 ax_probe.py 的文件头）。

用法：fg_activate.py --pid 12345 [--timeout 25] [--poll 0.5]
  rc=0 置前成功；rc=1 没置上；rc=2 找不到该 pid；rc=3 PyObjC 不可用。
  stdout 永远是 JSON：ok / active / pid / attempts / waited / mechanisms / why。
"""
import argparse
import json
import subprocess
import sys
import time


def _find(appkit, pid):
    """按 pid 找 NSRunningApplication。

    走 NSWorkspace.runningApplications()，不走
    NSRunningApplication.runningApplicationsWithBundleIdentifier_：后者要先知道
    bundle id 且只返回**同 bundle** 的那些。多实例场景（本门就是）必须落到
    具体那个 pid 上，所以这里在全部运行实例里按 pid 挑。
    （实测 NSRunningApplication 上不存在 runningApplications 类方法 ——
    那是 AppKit Swift 侧的形状，PyObjC 这边没有。）
    """
    for a in appkit.NSWorkspace.sharedWorkspace().runningApplications():
        if int(a.processIdentifier()) == int(pid):
            return a
    return None


def _asa(pid):
    """System Events 按 unix id 点名置前。-> (rc 或 None, stderr 头)"""
    script = ('tell application "System Events" to set frontmost of '
              '(first process whose unix id is %d) to true' % int(pid))
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:                                     # noqa: BLE001
        return None, repr(e)[:160]
    return p.returncode, (p.stderr or "").strip()[:160]


def ensure_foreground(pid, timeout=25.0, poll=0.5):
    """置前一个已存在的 pid -> dict（不抛异常，失败也返回结构化结果）。

    单独抽出来是因为调用方有两种：命令行（自己打印 JSON 与退出码）和同进程
    复用（ax_probe 每次采样前都要复查一次，import 进来直接调）。
    """
    try:
        import AppKit
    except ImportError as e:
        return {"pid": int(pid), "attempts": 0, "ok": False,
                "active": False, "waited": 0.0,
                "why": "no-PyObjC %s" % e, "mechanisms": []}
    t0 = time.time()
    deadline = t0 + timeout
    out = {"pid": int(pid), "attempts": 0, "ok": False, "active": False}
    out["waited"] = None
    out["why"] = ""
    out["mechanisms"] = []
    app = _find(AppKit, pid)
    if app is None:
        out["why"] = "no-NSRunningApplication-for-pid"
        out["waited"] = round(time.time() - t0, 1)
        return out

    # IgnoringOtherApps 是必须的：我们主动抢前台，不是问系统「能不能」。
    # AllWindows 让它的所有窗口一起 order front —— 只让某一个上前会留下
    # 「主窗口在后、面板在前」的错位。
    opts = (AppKit.NSApplicationActivateIgnoringOtherApps
            | AppKit.NSApplicationActivateAllWindows)
    log = out["mechanisms"]
    # 「动手之前它就已经在前台了吗」—— 这一列是唯一能把「本来就是前台」
    # 和「我们把它抢回来了」分开的证据。少了它，attempts 永远是 1，
    # 读数就分不出「什么都没发生」和「发生了但被治好了」。
    out["already_active"] = bool(app.isActive())
    while time.time() < deadline:
        out["attempts"] += 1
        try:
            app.activateWithOptions_(opts)
            log.append({"via": "activateWithOptions_", "err": "",
                        "note": "返回值不可信，裁决只看 isActive"})
        except Exception as e:                                # noqa: BLE001
            log.append({"via": "activateWithOptions_", "err": repr(e)[:160]})
        if not bool(app.isActive()):
            rc, err = _asa(pid)
            log.append({"via": "system-events", "rc": rc, "err": err})
        if bool(app.isActive()):
            out["ok"] = True
            out["active"] = True
            break
        if time.time() + poll >= deadline:
            break
        time.sleep(poll)

    out["waited"] = round(time.time() - t0, 1)
    out["mechanisms"] = log[-8:]
    if not out["ok"]:
        out["active"] = bool(app.isActive())
        out["why"] = ("置前 %d 次仍未成为前台 app（isActive=%r）"
                      % (out["attempts"], out["active"]))
    try:
        out["bundle_id"] = str(app.bundleIdentifier() or "")
    except Exception:                                         # noqa: BLE001
        out["bundle_id"] = ""
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--poll", type=float, default=0.5)
    a = ap.parse_args(argv)
    try:
        import AppKit                                         # noqa: F401
    except ImportError as e:
        print(json.dumps({"ok": False, "why": "no-PyObjC %s" % e}))
        return 3
    out = ensure_foreground(a.pid, timeout=a.timeout, poll=a.poll)
    print(json.dumps(out, ensure_ascii=False))
    if out.get("why") == "no-NSRunningApplication-for-pid":
        return 2
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
