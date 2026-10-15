#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D51f 原生真键盘门：在真 .app 上发真 HID Tab，量焦点是否在产品控件间移动。

为什么单独立这道门（交接第 3 项）：
   a11y_walk 是浏览器里的机检（真键盘事件 + 真 AX 树，19/19），a11y_human 是渲染层
   判据；但交付物是原生 WKWebView。交接原话「当前 aria 覆盖是机检，非实测」。
   ax_probe 量的是 AX 树暴不暴露，不是焦点动不动 —— 暴露 != 可用键盘走通。

为什么必须有对照臂（本门最关键的设计）：
   AXUIElementSetAttributeValue(app, kAXFrontmostAttribute, True)（置前）本身
   就可能改变系统认定的焦点。若只跑「置前 + Tab」一条臂，把观测到的焦点变化
   记在 Tab 头上，就是把量具副作用当成产品能力。一次实测就踩到：
   diag_kbd.py（rc=0）报 tab_moves=0 / readable=1，看着像「Tab 在原生 app 里
   不动」；但 D51f 三跑里，每次 Tab 前重新置前后读到了两个真实产品控件：
   AXOutline/'项目列表' 与 AXRadioButton/'总览'。
   所以本门跑两条量具策略完全相同、只差按不按 Tab 的臂：
       对照臂 control = 置前 -> 读
       处理臂 treat   = 置前 -> 真 HID Tab -> 读
   只有 treat 臂有名控件集合严格包含 control 臂，才算 Tab 自己的功劳。

读数间歇（D105/D51d/D51e 实测 AX 会自发摆）不许被藏起来：
   每一步先直读；直读为 nil 才置前补一次并两条读数都留痕
   （sig_direct / sig_after_nudge）。判据用「先直读、nil 才补」那条，
   补读只填空位、不许替换直读；两种口径（strict=只用直读 / filled=允许补读）
   都算都报，若只有 filled 口径才动，如实写成「焦点读数间歇，移动靠补读确认」。

落盘：work/native/kbd_gate.json（含逐拍读数，供事后追）
裁决：KBD_GATE n/N + KBD_GATE_OK / KBD_GATE_RED / KBD_GATE_ENV_UNVERIFIED，exit 0/1/2。
pyobjc（ApplicationServices/Quartz）缺失时**不崩栈也不假绿**：直接报 ENV_UNVERIFIED、
exit 2 —— 判据本体（summarize/judge/decide）刻意保持纯 stdlib，供 selftest 无 pyobjc 也走得通。
"""
import json
import os
import subprocess
import subprocess as _sp
import sys
import time

try:
    import ApplicationServices as AS
    import Quartz
    PYOBJC = True
    PYOBJC_ERR = ""
except ImportError as _e:      # 实测 2026-10-12：三个系统级解释器全无 pyobjc，裸 import 会让真门崩栈
    AS = None                  # noqa: F811
    Quartz = None
    PYOBJC = False
    PYOBJC_ERR = str(_e)

ROOT = "/Users/zhangfengrui/Documents/Codex/2026-10-01/agentos-console"
BIN = os.path.join(ROOT, "AgentOS.app/Contents/MacOS/AgentOS")
OUT = os.path.join(ROOT, "work", "native", "kbd_gate.json")
ALOG = os.path.join(ROOT, "work", "native", "kbd_gate_A.log")
TAB = 0x30
N_CONTROL = 6
N_TREAT = 8
N_SHIFT = 4
SETTLE = 0.7
RETRY_POLL = 0.5
# pyobjc 里这些 kAX* 常量的值就是这些 CFString 字面量；写成字面量后，
# 无 pyobjc 的解释器也能导入本模块（判据本体 + selftest 不再绑 pyobjc）。
ATTR_ROLE = "AXRole"
ATTR_FOCUSED = "AXFocusedUIElement"
ATTR_FRONTMOST = "AXFrontmost"
ATTR_WINDOWS = "AXWindows"
LABEL_ATTRS = ("AXTitle", "AXDescription", "AXHelp", "AXValue", "AXPlaceholderValue")
LEGAL_ROLES = {
    "AXButton", "AXRadioButton", "AXCheckBox", "AXMenuItem", "AXLink",
    "AXTextField", "AXTextArea", "AXSearchField", "AXOutline", "AXRow",
    "AXCell", "AXTabGroup", "AXGroup", "AXList", "AXComboBox",
    "AXPopUpButton", "AXDisclosureTriangle", "AXIncrementor", "AXSlider",
}
RESULTS = []

# 判据分两类：
#   ALWAYS    —— 与 Tab 遍历无关，任何环境都必须成立（窗起来了 / 量具开口 / 有名
#               控件 / role 合法 / 焦点没外流）。这些是产品自己的义务。
#   ENV_GATED —— 「真 HID Tab 真的把焦点搬到新控件」。这条在 macOS 上依赖系统
#               「全键盘访问」(FKA) 开关：FKA 关时 WebKit 不消费 Tab，任何原生 app
#               的 web content 都收不到焦点遍历。
# D51f 本机实测证据（是实测，不是推断）：
#   1) ~/Library/Preferences/com.apple.universalaccess.plist 有 282 行、含
#      keyboardAccessCommandMap 等键盘键，但**没有** fullKeyboardAccessEnabled
#      —— 该 plist 确实被读到了，所以「键不存在 = FKA 关」这个读法成立；
#   2) 纯原生靶对照：osascript `display dialog`（两个已知按钮，与本项目零关系）
#      真 HID Tab 7/7 拍全停在 AXWindow/'FKA'，unique_stops=1 —— 纯原生控件都
#      不动，说明「Tab 不动」是机器级开关，不是本产品的缺陷。
#
# 所以 FKA 关时门必须**不产出绿**：判据不成立就报 ENV_UNVERIFIED 并以独立退出码
# 2 退出，绝不并进 35 门链把环境红算成产品红，也绝不 exit 0 假装量到了。
FKA_OFF_TOKEN = "KBD_GATE_ENV_UNVERIFIED"
ENV_GATED_NAMES = ("真 HID Tab 多停到新控件",
                   "strict 口径也看得出移动（非纯补读假象）")


def fka_state():
    """读系统「全键盘访问」开关。返回 on / off / unknown。

    唯一权威处是 com.apple.universalaccess 的 fullKeyboardAccessEnabled：
    开时写成 _CFBool 1，关时该键**不存在**（macOS 只在偏离默认值时落盘）。
    AX 侧读不到（AXFullKeyboardAccessEnabled -> -25205 attributeUnsupported，
    系统级 kAXFocusedApplication -> -25204 cannotComplete），所以走 CFPreferences。
    """
    try:
        out = _sp.run(["defaults", "read", "com.apple.universalaccess",
                       "fullKeyboardAccessEnabled"],
                      capture_output=True, text=True, timeout=10)
    except Exception:                                           # noqa: BLE001
        return "unknown"
    if out.returncode != 0:
        return "off"
    return "on" if out.stdout.strip().endswith("1") else "off"


def step(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %-32s %s" % ("ok " if ok else "FAIL", name, detail))


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
    """pyobjc 下签名是 AXUIElementGetPid(el, out)，返回 (err, pid)；不是单参。"""
    try:
        e, p = AS.AXUIElementGetPid(el, None)
    except Exception:                                           # noqa: BLE001
        return -1
    return int(p) if e == 0 else -1


def describe(f):
    if f is None:
        return None
    label = ""
    for a in LABEL_ATTRS:
        s = ax_str(f, a).strip()
        if s:
            label = "%s=%r" % (str(a).split("Attribute")[-1], s[:60])
            break
    return {"role": ax_str(f, ATTR_ROLE), "label": label, "pid": real_pid(f)}


def read_direct(app):
    return describe(ax_attr(app, ATTR_FOCUSED))


def sig(beat_rec, filled):
    """控件签名。strict 只认直读；filled 允许补读填空（不替换直读，只填空位）。"""
    r = beat_rec["sig_direct"]
    if r is None and filled:
        r = beat_rec["sig_after_nudge"]
    if not r:
        return None
    return (r["role"], r["label"])


def named(s):
    """有名 = 有可访问名。空 label 不算停靠点。"""
    return bool(s and s[1])


def press(code, shift=False, settle=SETTLE):
    mods = Quartz.kCGEventFlagMaskShift if shift else 0
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if mods:
            Quartz.CGEventSetFlags(ev, mods)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)
    time.sleep(settle)


def nudge(pid):
    return int(AS.AXUIElementSetAttributeValue(
        AS.AXUIElementCreateApplication(int(pid)), ATTR_FRONTMOST, True))


def win_count(pid):
    e, wins = AS.AXUIElementCopyAttributeValue(
        AS.AXUIElementCreateApplication(int(pid)), ATTR_WINDOWS, None)
    return int(e), len(wins or [])


def beat(app, pid, tag, do_key, shift=False):
    """一拍：置前 ->（可选）真键 -> 直读 ->（nil 才）置前补读一次。两条读数都留痕。"""
    nudge(pid)
    time.sleep(0.4)
    if do_key:
        press(TAB, shift=shift)
    direct = read_direct(app)
    after = None
    if direct is None:
        time.sleep(RETRY_POLL)
        nudge(pid)
        time.sleep(RETRY_POLL)
        after = read_direct(app)
    return {"tag": tag,
            "key": ("shift_tab" if shift else "tab") if do_key else "none",
            "sig_direct": direct, "sig_after_nudge": after}


def gather():
    """真跑一遍：起 app、发真 HID Tab、逐拍留痕。返回原始 facts（未汇总）。"""
    env = dict(os.environ, AGENTOS_MULTI_INSTANCE="1",
               AGENTOS_URL="http://127.0.0.1:8790", AGENTOS_AUTOCLOSE="300")
    log = open(ALOG, "w")
    p = subprocess.Popen([BIN], env=env, stdout=log, stderr=log, text=True)
    facts = {"pid": p.pid, "beats": []}
    try:
        time.sleep(3.0)
        facts["frontmost_rc"] = nudge(p.pid)
        t0, wins, werr = time.time(), -1, -1
        while time.time() - t0 < 25:
            werr, wins = win_count(p.pid)
            if werr == 0 and wins >= 1:
                break
            nudge(p.pid)
            time.sleep(1.2)
        facts["windows"], facts["windows_err"] = wins, werr
        app = AS.AXUIElementCreateApplication(int(p.pid))
        for i in range(N_CONTROL):
            facts["beats"].append(beat(app, p.pid, "control%d" % (i + 1), False))
        for i in range(N_TREAT):
            facts["beats"].append(beat(app, p.pid, "treat%d" % (i + 1), True))
        for i in range(N_SHIFT):
            facts["beats"].append(beat(app, p.pid, "shift%d" % (i + 1), True, shift=True))
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=10)
        log.close()
    return facts


def summarize(facts):
    """从原始逐拍读数算两套口径的结论。纯函数：只吃 facts，不碰硬件。

    抽出来是为了让 kbd_gate_selftest.py 能拿变异体喂进**同一条**汇总路径，
    而不是重写一份判据 —— 后者等于变异体测的是 selftest 自己。
    """
    def arm(keys):
        return [b for b in facts["beats"] if b["key"] in keys]

    def sigs(beats, filled):
        return [s for s in (sig(b, filled) for b in beats) if s]

    ctrl_f, trt_f, shf_f = arm(("none",)), arm(("tab",)), arm(("shift_tab",))
    facts["arms"] = {"control_beats": len(ctrl_f), "treat_beats": len(trt_f),
                     "shift_beats": len(shf_f)}
    for filled in (True, False):
        k = "filled" if filled else "strict"
        c_all = sigs(ctrl_f, filled)
        t_all = sigs(trt_f, filled)
        s_all = sigs(shf_f, filled)
        facts["arms"][k] = {
            "control_named": sorted({str(s) for s in c_all if named(s)}),
            "treat_named": sorted({str(s) for s in t_all if named(s)}),
            "shift_named": sorted({str(s) for s in s_all if named(s)}),
            "control_readable": len(c_all), "treat_readable": len(t_all),
            "shift_readable": len(s_all),
        }

    F, S = facts["arms"]["filled"], facts["arms"]["strict"]
    cset = set(F["control_named"])
    tset = set(F["treat_named"])
    sset = set(F["shift_named"])
    only_treat = sorted(tset - cset)
    beats_all = facts["beats"]
    recoveries = sum(1 for b in beats_all if b["sig_after_nudge"] is not None)
    readable = sum(1 for b in beats_all if sig(b, True))
    roles_bad = sorted({s["role"] for b in beats_all
                        for s in (b["sig_direct"], b["sig_after_nudge"])
                        if s and s["role"] not in LEGAL_ROLES})
    foreign = sorted({s["pid"] for b in beats_all
                      for s in (b["sig_direct"], b["sig_after_nudge"])
                      if s and s["pid"] not in (facts["pid"], -1, 0)})
    facts["summary"] = {
        "beats": len(beats_all), "readable": readable,
        "readable_pct": round(100.0 * readable / max(1, len(beats_all)), 1),
        "gauge_recoveries": recoveries, "only_in_treat": only_treat,
        "illegal_roles": roles_bad, "foreign_focus_pids": foreign,
        "movement_needs_recovery": not S["treat_named"] and bool(F["treat_named"]),
    }
    return facts


def judge(facts, fka=None):
    """9 条判据。纯函数：吃 summarize 过的 facts，吐 [(名, 是否过, 依据)]。

    同样抽出来给 selftest：变异体走的是**判据本体**，不是 selftest 的复述。
    `fka` 形参让 selftest 能在不真改系统开关的前提下走遍三条分支。
    """
    F, S = facts["arms"]["filled"], facts["arms"]["strict"]
    cset = set(F["control_named"])
    tset = set(F["treat_named"])
    sset = set(F["shift_named"])
    only_treat = sorted(tset - cset)
    recoveries = facts["summary"]["gauge_recoveries"]
    beats_all = facts["beats"]
    out = []

    def j(name, ok, detail):
        out.append((name, bool(ok), detail))

    j("原生窗口起来了", facts["windows"] >= 1,
      "windows=%s windows_err=%s" % (facts["windows"], facts["windows_err"]))
    j("置前调用成功", facts["frontmost_rc"] == 0,
      "kAXFrontmostAttribute set rc=%s" % facts["frontmost_rc"])
    j("量具会说话（对照臂有读数）", F["control_readable"] >= 1,
      "对照臂 %d/%d 拍开口，补读救回 %d 拍"
      % (F["control_readable"], len(ctrl_beats_of(facts)), recoveries))
    j("对照臂读到有名产品控件", len(cset) >= 1,
      "control_named=%d 个 %s" % (len(cset), sorted(cset)))
    j("真 HID Tab 多停到新控件", len(only_treat) >= 1,
      "treat_named=%d 个，比对照臂多 %d 个：%s"
      % (len(tset), len(only_treat), only_treat or "无"))
    j("读到的都是合法控件 role", not facts["summary"]["illegal_roles"],
      "非法 role=%s" % (facts["summary"]["illegal_roles"] or "无"))
    j("Shift+Tab 反向也停到有名控件", len(sset) >= 1,
      "shift_named=%d 个 %s" % (len(sset), sorted(sset)))
    j("焦点没跑到别的 app", not facts["summary"]["foreign_focus_pids"],
      "foreign_focus_pids=%s" % (facts["summary"]["foreign_focus_pids"] or "无"))
    j("strict 口径也看得出移动（非纯补读假象）", bool(S["treat_named"]),
      "strict treat_named=%d 个；仅 filled 才动=%s"
      % (len(S["treat_named"]), facts["summary"]["movement_needs_recovery"]))
    fka = fka_state() if fka is None else fka
    facts["fka"] = fka
    facts["verdict"] = decide(out, fka)
    return out


def decide(results, fka):
    """把 9 条判据 + FKA 状态收敛成一个裁决词。纯函数，selftest 直接打它。

    ok        全部成立，且 Tab 真的动了
    red       ALWAYS 里有红的 —— 产品自己的义务没做到，与环境无关
    env       ALWAYS 全过但 FKA 不在 on —— Tab 那两条无法判定，不许报绿
    """
    bad = [name for name, ok, _ in results if not ok]
    bad_always = [n for n in bad if n not in ENV_GATED_NAMES]
    if bad_always:
        return "red"
    if fka == "on":
        return "ok" if not bad else "red"
    return "env"


def ctrl_beats_of(facts):
    return [b for b in facts["beats"] if b["key"] == "none"]


def report(facts, results):
    """打印判据 + 逐拍明细 + 裁决，并落盘。"""

    verdict = facts.get("verdict") or "red"
    print("\nKBD_GATE 判据：")
    for name, ok, detail in results:
        step(name, ok, detail)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(facts, fh, ensure_ascii=False, indent=2, sort_keys=True)
    bad = [r for r in results if not r[1] and r[0] not in ENV_GATED_NAMES]
    beats_all = facts["beats"]
    readable = facts["summary"]["readable"]
    recoveries = facts["summary"]["gauge_recoveries"]
    print("\n可读率 %d/%d 拍（%s%%），补读救回 %d 拍"
          % (readable, len(beats_all), facts["summary"]["readable_pct"], recoveries))
    print("逐拍读数：")
    for b in beats_all:
        print("  %-10s key=%-9s direct=%-40s after_nudge=%s"
              % (b["tag"], b["key"],
                 json.dumps(sig(b, False), ensure_ascii=False),
                 json.dumps(sig(b, True), ensure_ascii=False)))
    print("\nKBD_GATE %d/%d（其中 ENV_GATED %d 条在 FKA=%s 下不可判定）"
          % (len(results) - len(bad), len(results),
             sum(1 for r in results if r[0] in ENV_GATED_NAMES), facts.get("fka")))
    if verdict == "ok":
        print("KBD_GATE_OK")
        return 0
    if verdict == "env":
        # 退出码 2：不并进 35 门链（那是产品红的清单），也不 exit 0 冒充量到了。
        print(FKA_OFF_TOKEN)
        print("  依据：FKA（系统全键盘访问）=%s；Tab 遍历类判据不可判定，"
              "AT-UNVERIFIED-ENV" % facts.get("fka"))
        print("  这不是产品红，也不是产品绿 —— 要变成绿需先开 FKA 再复跑本门。")
        return 2
    print("KBD_GATE_RED")
    print("  红判据=%s" % sorted(r[0] for r in results
                                 if not r[1] and r[0] not in ENV_GATED_NAMES))
    return 1


def main():
    if not PYOBJC:
        print("KBD_GATE 判据：未跑（量具缺 pyobjc，一条都没量）")
        print(FKA_OFF_TOKEN)
        print("  依据：import ApplicationServices/Quartz 失败（%s）——" % PYOBJC_ERR)
        print("  量具不可用；这不是产品红也不是产品绿，装上 pyobjc（或换带 pyobjc")
        print("  的解释器）后复跑本门。")
        print("AT-UNVERIFIED-ENV")
        return 2
    facts = summarize(gather())
    results = judge(facts)
    RESULTS[:] = results
    return report(facts, results)


if __name__ == "__main__":
    sys.exit(main())
