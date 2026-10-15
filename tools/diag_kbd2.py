#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D51f 对照诊断：把 diag_kbd.py 观测到的 13 次 nil 拆成两种互斥原因。

diag_kbd.py（rc=0）实测：initial=AXOutline/'项目列表'，第 1 次 Tab 读到
AXRadioButton/'总览'，随后 13 次读数全 None，`tab_moves=0`。`focused()` 只在
`kAXFocusedUIElement` 为 nil 时返回 None，且每次都带 8s 重试 + 置前，所以
「读不到」不是量具抖动，是系统认定本 app 焦点元素为 nil。但 nil 有两种来源：

   (A) 焦点被别的 app 抢走  -> 本 app 自然读到 nil，产品无问题
   (B) 本 app 仍在前台、窗口仍在，焦点元素却为 nil -> 焦点困在 web 文档里
       没有可聚焦控件，是产品/桥接问题

本探针逐次同时读三件事来分辨：
   1. 屏上最前 layer-0 窗口的属主 pid（Quartz CGWindowList，正交于 AX）
   2. 本 app 的 kAXFocusedWindow（还在不在）
   3. 本 app 的 kAXFocusedUIElement 及其真实 pid

并且每次 Tab 前重新置前，把「焦点起点」钉死在自家 app，排除焦点外流。
仍然只诊断不立门：立不立门由本文件判据决定，判据写在这里可被审。
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
OUT = os.path.join(ROOT, "work", "native", "diag_kbd2.json")
TAB = 0x30
PRESSES = 8
SETTLE = 0.7
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


def real_pid(el):
    """AXUIElementGetPid 在 pyobjc 下返回形态不固定，两种都兜住。"""
    try:
        r = AS.AXUIElementGetPid(el)
    except Exception:                                           # noqa: BLE001
        return -1
    if isinstance(r, tuple):
        e, p = r
        return int(p) if e == 0 else -1
    try:
        return int(r)
    except (TypeError, ValueError):
        return -1


def frontmost_window_owner():
    """屏上最前的普通层窗口（layer 0）属主 pid；拿不到返回 -1。"""
    wl = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    best = None
    for w in wl or []:
        if w.get("kCGWindowLayer") != 0:
            continue
        pid = w.get("kCGWindowOwnerPID")
        if not pid:
            continue
        # CGWindowList 从后往前排，取最后一个 layer-0 即最前
        best = int(pid)
    return best if best is not None else -1


def nudge_frontmost(pid):
    return AS.AXUIElementSetAttributeValue(
        AS.AXUIElementCreateApplication(int(pid)), AS.kAXFrontmostAttribute, True)


def win_count(pid):
    e, wins = AS.AXUIElementCopyAttributeValue(
        AS.AXUIElementCreateApplication(int(pid)), AS.kAXWindowsAttribute, None)
    return int(e), len(wins or [])


def focused(app):
    f = ax_attr(app, AS.kAXFocusedUIElementAttribute)
    if f is None:
        return None
    label = ""
    for a in LABEL_ATTRS:
        s = ax_str(f, a).strip()
        if s:
            label = "%s=%r" % (str(a).split("Attribute")[-1], s[:60])
            break
    return {"role": ax_str(f, AS.kAXRoleAttribute), "label": label,
            "pid": real_pid(f)}


def press(code, shift=False, settle=SETTLE):
    mods = Quartz.kCGEventFlagMaskShift if shift else 0
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if mods:
            Quartz.CGEventSetFlags(ev, mods)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)
    time.sleep(settle)


def main():
    env = dict(os.environ, AGENTOS_MULTI_INSTANCE="1",
               AGENTOS_URL="http://127.0.0.1:8790", AGENTOS_AUTOCLOSE="240")
    log = open(os.path.join(ROOT, "work", "native", "diag_kbd2_A.log"), "w")
    p = subprocess.Popen([BIN], env=env, stdout=log, stderr=log, text=True)
    facts = {"pid": p.pid, "samples": []}
    try:
        time.sleep(3.0)
        facts["frontmost_rc"] = int(nudge_frontmost(p.pid))
        t0 = time.time()
        facts["windows"] = -1
        facts["windows_err"] = -1
        while time.time() - t0 < 25:
            e, n = win_count(p.pid)
            facts["windows"], facts["windows_err"] = n, int(e)
            if e == 0 and n >= 1:
                break
            nudge_frontmost(p.pid)
            time.sleep(1.2)
        app = AS.AXUIElementCreateApplication(int(p.pid))

        def sample(tag):
            fw = ax_attr(app, AS.kAXFocusedWindowAttribute)
            s = {"tag": tag, "frontmost_window_pid": frontmost_window_owner(),
                 "has_focused_window": fw is not None, "focus": focused(app)}
            facts["samples"].append(s)
            return s

        sample("before_first_tab")
        for i in range(PRESSES):
            press(TAB)
            sample("tab%d" % (i + 1))
        # Shift+Tab 回退：验证反向遍历与正向同一套判据
        for i in range(4):
            press(TAB, shift=True)
            sample("shift_tab%d" % (i + 1))
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=10)
        log.close()

    ours = facts["pid"]
    samples = facts["samples"]
    stolen = [s for s in samples
              if s["frontmost_window_pid"] not in (ours, -1)]
    inapp_nil = [s for s in samples
                 if s["frontmost_window_pid"] == ours and s["focus"] is None]
    sigs = [(s["focus"]["role"], s["focus"]["label"]) for s in samples
            if s["focus"] is not None]
    uniq = []
    for sg in sigs:
        if not uniq or uniq[-1] != sg:
            uniq.append(sg)
    facts["verdicts"] = {
        "presses": len(samples) - 1,
        "readable": len(sigs),
        "unique_stops": len(set(sigs)),
        "tab_moves": max(0, len(uniq) - 1),
        "focus_stolen_samples": len(stolen),
        "inapp_nil_samples": len(inapp_nil),
        "env_not_product": len(inapp_nil) == 0 and len(stolen) > 0,
        "focus_moved_in_app": max(0, len(uniq) - 1) > 0,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(facts, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(facts["verdicts"], ensure_ascii=False, indent=2, sort_keys=True))
    print("逐次读数:")
    for s in samples:
        f = s["focus"]
        print("  %-16s front_window_pid=%-7s has_focused_win=%-5s focus=%s"
              % (s["tag"], s["frontmost_window_pid"], s["has_focused_window"],
                 ("None" if f is None else "%s/%s pid=%s" % (f["role"], f["label"], f["pid"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
