#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D51f 变异体自证：证明 kbd_gate 的 9 条判据每条都能变红。

为什么必须写这道门（本仓库既有规矩，view_dod/native_gate 同理）：
   一道只会返回 0 的门，和一道没有判据的门，在 rc 上长得一模一样。
   「KBD_GATE_OK」如果是从未被证伪过的字符串，它证明不了任何东西。

怎么保证测的是判据本体而不是本文件的复述：
   直接 import kbd_gate，把**原始逐拍读数**（不是汇总结果）喂进
   kbd_gate.summarize() + kbd_gate.judge()。判据只有一份，就在 kbd_gate 里；
   本文件只负责造坏的原始证据。

BASE 优先从 work/native/kbd_gate.json 读上一次真跑（真硬件读数）；
   读不到就退回结构同形的合成正例，并**明确打印降级提示** —— 不用合成数据
   冒充真跑正例，也不因为没有真跑就跳过自证。

W0-3 加装一条护栏回归：本机 pyobjc 不可用时，真门本体必须 exit 2 + ENV 令牌
   且不崩栈（子进程跑的就是 tools/kbd_gate.py 本体）—— 缺量具 ≠ 产品红，也 ≠ 假绿。

裁决：KBD_GATE_SELFTEST n/n + KBD_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import copy
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kbd_gate as K                                       # noqa: E402

BASE_PATH = K.OUT
SIDECAR = os.path.join(K.ROOT, "work", "native", "kbd_gate_selftest.json")
RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %-40s %s" % ("ok " if ok else "FAIL", name, detail))


def beat(tag, key, role, label, pid=None):
    return {"tag": tag, "key": key,
            "sig_direct": None if role is None
            else {"role": role, "label": label, "pid": pid},
            "sig_after_nudge": None}


def synthetic_base():
    """兜底正例：结构与真跑同形，4 拍对照 + 8 拍处理 + 4 拍反向。"""
    pid = 424242
    beats = [beat("control%d" % (i + 1), "none", "AXOutline",
                  "AXDescription='项目列表'", pid) for i in range(4)]
    cycle = [("AXRadioButton", "AXTitle='总览'"),
             ("AXGroup", "AXTitle='总览'"),
             ("AXDisclosureTriangle", "AXTitle='口径分歧 ▾'")]
    for i in range(8):
        role, label = cycle[i % len(cycle)]
        beats.append(beat("treat%d" % (i + 1), "tab", role, label, pid))
    for i in range(4):
        beats.append(beat("shift%d" % (i + 1), "shift_tab", "AXOutline",
                          "AXDescription='项目列表'", pid))
    return {"pid": pid, "frontmost_rc": 0, "windows": 1, "windows_err": 0,
            "beats": beats}


def reds_of(facts):
    return {name for name, ok, _
            in K.judge(K.summarize(copy.deepcopy(facts)), fka="off") if not ok}


def verdict_of(facts, fka):
    """走判据本体拿裁决词（fka 显式传，不读系统）。"""
    return K.decide(K.judge(K.summarize(copy.deepcopy(facts)), fka=fka), fka)


def main():
    print("KBD_GATE 变异体自证")
    if os.path.isfile(BASE_PATH):
        with open(BASE_PATH) as fh:
            base = json.load(fh)
        real_reds = reds_of(base)
        first_direct = (base.get("beats") or [{}])[0].get("sig_direct")
        if first_direct is None:
            # 变异体 6/7 直接改 beats[0]["sig_direct"] 的字段：首拍直读为
            # None 的真跑（靠补读救回）当 BASE 会让自证自己 crash。这不是判据
            # 的红，是 BASE 形态不满足变异体前置条件 —— 同样明确降级。
            print("  降级：work/native/kbd_gate.json 首拍直读为空"
                  "（靠补读救回，不满足变异体 6/7 前置条件，已降级）")
            base = synthetic_base()
            src = "合成同形正例（真跑落盘首拍直读为空，已降级）"
            base_is_real = False
        elif real_reds:
            # 真跑落盘自带红判据（典型：本机 FKA=off，真 HID Tab 压根不动，
            # 于是 ENV_GATED 判红）。拿它当"正例"会让自证自相矛盾 ——
            # BASE 变红 -> 变异体"真键没生效"造不出差异 -> 假失败。
            # 这种情况明确降级到合成正例，并打印降级原因，不蒙混。
            print("  降级：work/native/kbd_gate.json 自带红判据 %s（非可作正例的真跑读数）"
                  % sorted(real_reds))
            base = synthetic_base()
            src = "合成同形正例（真跑落盘带红判据，已降级）"
            base_is_real = False
        else:
            src = "work/native/kbd_gate.json（上次真跑，%d 拍）" % len(base.get("beats", []))
            base_is_real = True
    else:
        base = synthetic_base()
        src = "合成同形正例（无真跑落盘，已标注）"
        base_is_real = False
    print("  BASE = %s" % src)

    base_reds = reds_of(base)
    check("BASE 正例全绿", not base_reds, "红判据=%s" % (sorted(base_reds) or "无"))

    mutants = []

    def mut(name, expect, why, mm):
        mutants.append((name, expect, why, mm))

    # 1. 真键没生效：处理臂读数被换成对照臂读数（= Tab 不动）
    m = copy.deepcopy(base)
    ctrl = next(b for b in m["beats"] if b["key"] == "none")
    for b in m["beats"]:
        if b["key"] in ("tab", "shift_tab"):
            b["sig_direct"] = copy.deepcopy(ctrl["sig_direct"])
    mut("真键没生效（处理臂==对照臂）", "真 HID Tab 多停到新控件",
        "处理臂读数替换成对照臂读数，模拟按了键但焦点不动", m)

    # 2. 原生窗口没起来
    m = copy.deepcopy(base)
    m["windows"] = 0
    mut("原生窗口没起来", "原生窗口起来了", "windows=0", m)

    # 3. 置前调用失败
    m = copy.deepcopy(base)
    m["frontmost_rc"] = -25202
    mut("置前调用失败", "置前调用成功", "kAXFrontmostAttribute set 返回非 0", m)

    # 4. 量具全程不开口
    m = copy.deepcopy(base)
    for b in m["beats"]:
        b["sig_direct"] = None
        b["sig_after_nudge"] = None
    mut("量具全程不开口", "量具会说话（对照臂有读数）", "所有拍直读与补读都为空", m)

    # 5. 控件全无名（可访问名被抹掉）
    m = copy.deepcopy(base)
    for b in m["beats"]:
        for k in ("sig_direct", "sig_after_nudge"):
            if b[k]:
                b[k]["label"] = ""
    mut("控件全无名", "对照臂读到有名产品控件", "所有 label 清空", m)

    # 6. 焦点落在非法 role 上
    m = copy.deepcopy(base)
    m["beats"][0]["sig_direct"]["role"] = "AXWindow"
    mut("焦点落在 AXWindow 上", "读到的都是合法控件 role", "首拍 role=AXWindow", m)

    # 7. 焦点跑到别的 app
    m = copy.deepcopy(base)
    m["beats"][0]["sig_direct"]["pid"] = 99999
    mut("焦点跑到别的 app", "焦点没跑到别的 app", "首拍 pid=99999 != 本 app", m)

    # 8. 反向臂空
    m = copy.deepcopy(base)
    m["beats"] = [b for b in m["beats"] if b["key"] != "shift_tab"]
    mut("Shift+Tab 臂空", "Shift+Tab 反向也停到有名控件", "删掉全部 shift 拍", m)

    # 9. 移动只靠补读才看得到（直读全空 = 纯补读假象）
    m = copy.deepcopy(base)
    for b in m["beats"]:
        if b["key"] == "tab":
            b["sig_direct"] = None
    mut("移动只靠补读才看得到", "strict 口径也看得出移动（非纯补读假象）",
        "处理臂直读全空，只剩补读有值", m)

    detail = []
    for name, expect, why, mm in mutants:
        reds = reds_of(mm)
        hit = expect in reds
        check(name, hit, "期望红「%s」→ 实际红 %d 条 %s"
              % (expect, len(reds), sorted(reds)))
        detail.append({"mutant": name, "expect": expect, "caught": bool(hit),
                       "reds": sorted(reds), "why": why})

    # ---- 裁决分支：证明 FKA 档位既不假绿，也不掩盖真实产品红 ----
    # no_move = 把处理臂读数换成对照臂读数，等价于「按了键但焦点不动」。
    no_move = copy.deepcopy(base)
    ctrl = next(b for b in no_move["beats"] if b["key"] == "none")
    for b in no_move["beats"]:
        if b["key"] in ("tab", "shift_tab"):
            b["sig_direct"] = copy.deepcopy(ctrl["sig_direct"])

    always_red = copy.deepcopy(base)
    always_red["windows"] = 0            # ALWAYS 红：原生窗没起来

    branches = [
        ("FKA=on 且 Tab 动了 -> ok", base, "on", "ok", "环境就绪且 Tab 真搬了焦点"),
        ("FKA=on 但 Tab 不动 -> red", no_move, "on", "red",
         "环境就绪仍不动 = 真缺陷，不许被 env 档吞掉"),
        ("FKA=off 且 ALWAYS 全过 -> env", no_move, "off", "env",
         "Tab 不可判定 -> ENV_UNVERIFIED 且退出码非 0"),
        ("FKA=unknown 同样 -> env", no_move, "unknown", "env",
         "读不到开关不许默认当绿"),
        ("FKA=off 但 ALWAYS 有红 -> red", always_red, "off", "red",
         "FKA 关也压不住产品自己的义务红"),
    ]
    for b, facts_, fka, want, why in branches:
        got = verdict_of(facts_, fka)
        check(b, got == want, "裁决=%s（期望 %s）：%s" % (got, want, why))
        detail.append({"branch": b, "fka": fka, "want": want, "got": got,
                       "why": why})

    # W0-3 护栏回归：缺 pyobjc 时真门本体不许崩栈、不许假绿（rc=2 + ENV 令牌）。
    if not K.PYOBJC:
        gate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kbd_gate.py")
        r = subprocess.run([sys.executable, gate], capture_output=True, text=True,
                           timeout=120)
        ok = (r.returncode == 2 and K.FKA_OFF_TOKEN in r.stdout
              and "Traceback" not in r.stderr)
        check("缺 pyobjc 时真门 exit2+ENV 令牌、不崩栈", ok,
              "rc=%d 令牌在=%s 崩栈=%s"
              % (r.returncode, K.FKA_OFF_TOKEN in r.stdout,
                 "Traceback" in r.stderr))
        detail.append({"guard": "no-pyobjc", "rc": r.returncode,
                       "token": K.FKA_OFF_TOKEN in r.stdout,
                       "traceback": "Traceback" in r.stderr})
    else:
        print("  -  pyobjc 可用：跳过「缺 pyobjc 护栏」检查（真跑路径由门本体覆盖）")

    bad = [r for r in RESULTS if not r[1]]
    os.makedirs(os.path.dirname(SIDECAR), exist_ok=True)
    with open(SIDECAR, "w") as fh:
        json.dump({"base_source": src, "base_is_real": base_is_real,
                   "base_reds": sorted(base_reds), "mutants": detail},
                  fh, ensure_ascii=False, indent=2, sort_keys=True)
    print("\nKBD_GATE_SELFTEST %d/%d" % (len(RESULTS) - len(bad), len(RESULTS)))
    print("KBD_GATE_SELFTEST_OK" if not bad else "KBD_GATE_SELFTEST_RED")
    if not base_is_real:
        print("  ! BASE 非真跑落盘：判据自证成立，但正例代表性打折")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
