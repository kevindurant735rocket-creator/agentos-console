#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D51e 诊断：在原生 AgentOS.app 上走真键盘，而不是在浏览器里走。

为什么先诊断、不直接立门：
   交接第③项说「当前 aria 覆盖是机检，非实测」。a11y_walk 已经在浏览器里发
   真键盘事件（真 Tab 链 + 真 AX 树，19/19），但交付物是原生 WKWebView ——
   浏览器里绿不等于原生 app 里绿。macOS 上原生 web view 的 Tab 焦点遍历依赖
   系统「全键盘访问」开关，没开时 Tab 根本不被 WebKit 消费，那种「Tab 不动」
   是环境不是产品。所以本脚本只回答：这台机器当前设置下，真 Tab 到底动不动。

键走 Quartz CGEventPost(kCGHIDEventTap)，是 HID 层真事件（与 AppleScript
 `tell app to keystroke` 的合成路径不同，也与 Playwright 的 dispatchEvent
 不同：这三者不是同一条链路）。读数一律走 AX，读到的是**系统**认定的焦点。
"""
import json
import os
import subprocess
import sys
import time

import ApplicationServices as AS
import Quartz

ROOT = "/Users/zhangfengrui/Documents/Codex/2026-10-01/agentos-console"
BIN = os.path.join(ROOT, "AgentOS.app/Contents/MacOS/AgentOS")
OUT = os.path.join(ROOT, "work", "native", "diag_kbd.json")
TAB = 0x30          # HID usage, no modifier
STOPS = 14
SETTLE = 0.75
LABEL_ATTRS = (AS.kAXTitleAttribute, AS.kAXDescriptionAttribute, AS.kAXHelpAttribute,
               AS.kAXValueAttribute, AS.kAXPlaceholderValueAttribute)


def ax_attr(el, attr):
    e, v = AS.AXUIElementCopyAttributeValue(el, attr, None)
    return None if e != 0 else v


def ax_str(el, attr):
    v = ax_attr(el, attr)
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def nudge_frontmost(pid):
    app = AS.AXUIElementCreateApplication(int(pid))
    return AS.AXUIElementSetAttributeValue(app, AS.kAXFrontmostAttribute, True)


def win_count(pid):
    e, wins = AS.AXUIElementCopyAttributeValue(
        AS.AXUIElementCreateApplication(int(pid)), AS.kAXWindowsAttribute, None)
    return int(e), len(wins or [])


def focused(pid):
    """读系统认的焦点 -> dict；读不到返回 None（量具没开口，不等于焦点没动）。"""
    app = AS.AXUIElementCreateApplication(int(pid))
    f = ax_attr(app, AS.kAXFocusedUIElementAttribute)
    w = ax_attr(app, AS.kAXFocusedWindowAttribute)
    if f is None:
        return None
    try:
        fp = int(AS.AXUIElementGetPid(f))
    except Exception:                                          # noqa: BLE001
        fp = 0
    label = ""
    for a in LABEL_ATTRS:
        s = ax_str(f, a).strip()
        if s:
            label = "%s=%r" % (str(a).split("Attribute")[-1], s[:60])
            break
    return {"role": ax_str(f, AS.kAXRoleAttribute), "label": label,
            "pid": fp, "win": bool(w)}


def press(code, shift=False, settle=SETTLE):
    mods = Quartz.kCGEventFlagMaskShift if shift else 0
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if mods:
            Quartz.CGEventSetFlags(ev, mods)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)
    time.sleep(settle)


def patient_focus(pid, timeout=8.0, poll=1.0):
    """量具会间歇（D105/D51d 实测 AX 窗口树在 0/1 窗之间摆），读不到就置前补
    一次再等；绝不把「这一秒没说话」当成「焦点没动」。"""
    t0, n = time.time(), 0
    while time.time() - t0 < timeout:
        f = focused(pid)
        if f is not None:
            return f, n
        n += 1
        if n % 3 == 2:
            nudge_frontmost(pid)
        time.sleep(poll)
    return None, n


def main():
    env = dict(os.environ, AGENTOS_MULTI_INSTANCE="1",
               AGENTOS_URL="http://127.0.0.1:8790", AGENTOS_AUTOCLOSE="240")
    log = open(os.path.join(ROOT, "work", "native", "diag_kbd_A.log"), "w")
    p = subprocess.Popen([BIN], env=env, stdout=log, stderr=log, text=True)
    facts = {"pid": p.pid, "stops": [], "shift_stops": [], "initial": None}
    try:
        time.sleep(3.0)
        facts["frontmost_rc"] = int(nudge_frontmost(p.pid))
        t0 = time.time()
        facts["windows_err"], facts["windows"] = -1, -1
        while time.time() - t0 < 25:
            facts["windows_err"], facts["windows"] = win_count(p.pid)
            if facts["windows_err"] == 0 and facts["windows"] >= 1:
                break
            nudge_frontmost(p.pid)
            time.sleep(1.2)
        f0, _ = patient_focus(p.pid, timeout=15.0)
        facts["initial"] = f0
        seq = []
        for _ in range(STOPS):
            press(TAB)
            f, _n = patient_focus(p.pid)
            seq.append(f)
            time.sleep(0.3)
        facts["stops"] = seq
        facts["readable"] = sum(1 for x in seq if x is not None)
        sigs = [(x["role"], x["label"]) for x in seq if x is not None]
        uniq = []
        for s in sigs:
            if not uniq or uniq[-1] != s:
                uniq.append(s)
        facts["tab_moves"] = max(0, len(uniq) - 1)
        facts["unique_stops"] = len(set(sigs))
        facts["named_stops"] = sum(1 for x in seq if x and x["label"])
        facts["foreign_pids"] = sorted({x["pid"] for x in seq if x and x["pid"] not in (0, p.pid)})
        for _ in range(4):
            press(TAB, shift=True)
            f, _n = patient_focus(p.pid)
            facts["shift_stops"].append(f)
            time.sleep(0.3)
        facts["shift_readable"] = sum(1 for x in facts["shift_stops"] if x is not None)
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=10)
        log.close()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(facts, fh, ensure_ascii=False, indent=2, sort_keys=True)
    brief = dict((k, v) for k, v in facts.items() if k not in ("stops", "shift_stops"))
    print(json.dumps(brief, ensure_ascii=False, indent=2, sort_keys=True))
    print("前 6 个停靠点:")
    for x in facts["stops"][:6]:
        print("   ", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
