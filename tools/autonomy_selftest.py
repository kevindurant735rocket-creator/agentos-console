#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autonomy_selftest —— 「关于 W13 自主执行门的那道门」：证明门判得红也判得绿。

做法（纯 stdlib）：BASE 真源全绿（AUTONOMY_GATE OK + rc=0）；6 个源码级
变异体各改坏一处（绕锁/无自救/封顶差一/续跑重跑/假成功/环守卫被摘），用
--autonomy 通道喂给同一个门，逐条点名判红。再证生产 JSON 字节不变、
--out 隔离目录真用上、临时目录清空。

  M1  绕锁派发（未审批闸被摘）→ D3 必红
  M2  无自救（重试次数归零）→ D5 必红
  M3  封顶差一（多派一次）→ D6 必红
  M4  续跑重跑已完成（拓扑选择被摘）→ D7 必红
  M5  verify 被跳过（假成功）→ D9 必红
  M6  环守卫被摘 → D2 必红

裁决：AUTONOMY_GATE_SELFTEST n/n + _OK / _RED，exit 0/1
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AUTONOMY = os.path.join(HERE, "autonomy.py")
GATE = os.path.join(HERE, "autonomy_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "autonomy_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    ("M1 绕锁派发（未审批闸被摘）", AUTONOMY,
     '    if not plan.get("approved"):',
     "    if False:  # MUT 绕锁",
     "D3"),
    ("M2 无自救（重试次数归零）", AUTONOMY,
     "AUTONOMY_RETRY = 2",
     "AUTONOMY_RETRY = 0",
     "D5"),
    ("M3 封顶差一（多派一次）", AUTONOMY,
     "        while attempt < 1 + retries:",
     "        while attempt < 1 + retries + 1:",
     "D6"),
    ("M4 续跑重跑已完成（拓扑选择被摘）", AUTONOMY,
     "        tid = dsp.next_task(plan)",
     '        tid = next((t.get("id") for t in (plan.get("tasks") or []) '
     'if t.get("status") in ("todo", "doing", "done")), "")',
     "D7"),
    ("M5 verify 被跳过（假成功）", AUTONOMY,
     "            v_ok, v_note = _run_verify(task, root)",
     '            v_ok, v_note = None, ""  # MUT verify 被跳过',
     "D9"),
    ("M6 环守卫被摘", AUTONOMY,
     "    if stalled:",
     "    if stalled and False:  # MUT 环守卫被摘",
     "D2"),
]


def run_gate(autonomy, out, timeout=480):
    return subprocess.run([sys.executable, GATE, "--autonomy", autonomy,
                           "--out", out],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("AUTONOMY_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="aumut_")
    try:
        base_out = os.path.join(tmp, "base.json")
        p0 = run_gate(AUTONOMY, base_out)
        case("BASE 真源全绿（AUTONOMY_GATE OK + rc=0）",
             p0.returncode == 0 and "AUTONOMY_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "AUTONOMY_GATE_OK" in p0.stdout))
        case("隔离目录真用上（--out 落临时目录）",
             os.path.exists(base_out) and os.path.getsize(base_out) > 0,
             "exists=%s" % os.path.exists(base_out))

        for name, target, anchor, repl, want in MUTANTS:
            try:
                src = open(target, encoding="utf-8").read()
            except OSError as e:
                case(name, False, "读不到 %s：%s" % (target, e))
                continue
            n = src.count(anchor)
            if n != 1:
                case(name, False,
                     "锚点命中 %d 处（须恰 1）——变异体无效" % n)
                continue
            mut = os.path.join(tmp, "mut_" + os.path.basename(target))
            with open(mut, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, repl, 1))
            try:
                r = run_gate(mut, os.path.join(tmp, "mut.json"))
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
        case("生产 work/autonomy_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nAUTONOMY_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad),
                                              len(CASES)))
    print("AUTONOMY_GATE_SELFTEST_OK" if not bad
          else "AUTONOMY_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
