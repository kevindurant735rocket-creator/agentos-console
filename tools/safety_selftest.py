#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safety_selftest —— 「关于 W10 自由面门的那道门」：证明 safety_gate 判得红也判得绿。

做法（纯 stdlib）：
  5 个变异体各改坏一处，跑同一道 safety_gate（--core 指变异副本）：
    M1 计划锁整段被摘（显式上锁也不拦）→ D6 必红
    M2 模式开关站点被摘（写命令不认 mode_gate）→ D5 必红
    M3 硬阻断被摘（_hard_deny 恒 None）→ D5 必红
    M4 flags 恒自由（转换被写死，显式 true 也上不了锁）→ D1 必红
    M5 命令白名单闸被摘（显式 cmd_gate 也自由）→ D9 必红
  双自查：生产 work/safety_gate.json 字节不变 + 临时目录清空（先清再断言）。

裁决：SAFETY_GATE_SELFTEST n/n + SAFETY_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(HERE, "agent_core.py")
GATE = os.path.join(HERE, "safety_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "safety_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 锚点原文, 替换, 期望点名的判据前缀)
    ("M1 计划锁整段被摘（显式上锁也不拦）",
     "        if not ctx.get(\"plan_lock_off\"):\n            try:",
     "        if False:  # MUT 计划锁被摘\n            try:",
     "D6"),
    ("M2 模式开关站点被摘（写命令不认 mode_gate）",
     "        # 显式 mode_gate 时问审批（headless 无审批者=拒），默认自由面放行。\n"
     "        if ctx.get(\"mode_gate_off\"):\n"
     "            return None",
     "        # 显式 mode_gate 时问审批（headless 无审批者=拒），默认自由面放行。\n"
     "        if False:  # MUT 模式开关被摘\n"
     "            return None",
     "D5"),
    ("M3 硬阻断被摘（_hard_deny 恒 None）",
     "        why = _hard_deny(argv)",
     "        why = None  # MUT 硬阻断被摘",
     "D5"),
    ("M4 flags 恒自由（显式 true 也上不了锁）",
     "    return {\"plan_lock_off\": a.get(\"plan_lock\") is not True,\n"
     "            \"mode_gate_off\": a.get(\"mode_gate\") is not True,\n"
     "            \"cmd_gate_off\": a.get(\"cmd_gate\") is not True}",
     "    return {\"plan_lock_off\": True,  # MUT 恒自由\n"
     "            \"mode_gate_off\": True,\n"
     "            \"cmd_gate_off\": True}",
     "D1"),
    ("M5 命令白名单闸被摘（显式 cmd_gate 也自由）",
     "        if not ctx.get(\"cmd_gate_off\"):",
     "        if False:  # MUT 命令白名单被摘",
     "D9"),
]


def run_gate(core_path, out_path, timeout=900):
    return subprocess.run([sys.executable, GATE, "--core", core_path,
                           "--out", out_path],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("SAFETY_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="safemut_")
    try:
        p0 = run_gate(CORE, os.path.join(tmp, "base.json"))
        case("BASE 真源全绿（SAFETY_GATE OK + rc=0）",
             p0.returncode == 0 and "SAFETY_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "SAFETY_GATE_OK" in p0.stdout))

        src = open(CORE, encoding="utf-8").read()
        for name, anchor, repl, want in MUTANTS:
            mut_path = os.path.join(tmp, "agent_core_mut.py")
            n = src.count(anchor)
            if n != 1:
                case(name, False,
                     "锚点命中 %d 处（须恰 1）——变异体无效" % n)
                continue
            with open(mut_path, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, repl, 1))
            try:
                r = run_gate(mut_path, os.path.join(tmp, "mut.json"))
            except subprocess.TimeoutExpired:
                case(name, False, "门超时")
                continue
            hit = ("FAIL %s" % want) in r.stdout
            case(name, r.returncode != 0 and hit,
                 "rc=%d 期望红「%s」→%s"
                 % (r.returncode, want, "命中" if hit else "未命中"))

        prod_after = b""
        if os.path.exists(PROD_JSON):
            with open(PROD_JSON, "rb") as f:
                prod_after = f.read()
        case("生产 work/safety_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nSAFETY_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad),
                                            len(CASES)))
    print("SAFETY_GATE_SELFTEST_OK" if not bad
          else "SAFETY_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
