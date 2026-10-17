#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leader_selftest —— 「关于 W11 指挥官门的那道门」：证明 leader_gate 判得红也判得绿。

做法（纯 stdlib）：BASE 真源全绿（LEADER_GATE OK + rc=0）；6 个源码级变异体
各改坏一处，用 --leader / --agentos 通道喂给同一个门，逐条点名判红 ——
其中两条正是体检报告的验收口径「无 Leader 必红」（模块面 + CLI 面各一条）。
再证生产 JSON 字节不变、--out 隔离目录真用上、临时目录清空。

  M0  无 Leader 必红（模块面）：compile_goal 被摘 → D1 必红
  M1  无 Leader 必红（CLI 面）：add("leader") 注册被摘 → D8 必红
  M2  绕锁派发：未审批闸被摘 → D3 必红
  M3  拓扑序被摘（按文件顺序派发）→ D5 必红（乱序 DAG 才咬得住）
  M4  复盘证据行被摘 → D7 必红
  M5  坏计划不拒（校验处理被摘）→ D9 必红

裁决：LEADER_GATE_SELFTEST n/n + LEADER_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LEADER = os.path.join(HERE, "leader.py")
AGENTOS = os.path.join(HERE, "agentos.py")
GATE = os.path.join(HERE, "leader_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "leader_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    ("M0 无 Leader 必红（模块面：compile_goal 被摘）", LEADER,
     'def compile_goal(goal, root=""):',
     'def compile_goal_DISABLED(goal, root=""):',
     "D1"),
    ("M1 无 Leader 必红（CLI 面：注册被摘）", AGENTOS,
     '    "leader",\n        "仓内指挥官：目标→里程碑→任务DAG→派发→回收→复盘",',
     '    "LEADER_DISABLED",\n        "仓内指挥官：目标→里程碑→任务DAG→派发→回收→复盘",',
     "D8"),
    ("M2 绕锁派发（未审批闸被摘）", LEADER,
     '    if not plan.get("approved"):',
     "    if False:  # MUT 绕锁",
     "D3"),
    ("M3 拓扑序被摘（按文件顺序派发）", LEADER,
     "    tid = dsp.next_task(plan)",
     '    tid = next((t.get("id") for t in (plan.get("tasks") or []) '
     'if t.get("id") not in attempted), "")',
     "D5"),
    ("M4 复盘证据行被摘", LEADER,
     '        ev = ((t.get("evidence") or ["-"])[-1])[:120]',
     '        ev = "-"  # MUT 证据被摘',
     "D7"),
    ("M5 坏计划不拒（校验处理被摘）", LEADER,
     "    if not ok_plan:",
     "    if False:  # MUT 不拒坏计划",
     "D9"),
]


def run_gate(leader, agentos, out, timeout=420):
    return subprocess.run([sys.executable, GATE, "--leader", leader,
                           "--agentos", agentos, "--out", out],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("LEADER_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="lgrmut_")
    try:
        base_out = os.path.join(tmp, "base.json")
        p0 = run_gate(LEADER, AGENTOS, base_out)
        case("BASE 真源全绿（LEADER_GATE OK + rc=0）",
             p0.returncode == 0 and "LEADER_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "LEADER_GATE_OK" in p0.stdout))
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
            leader_arg = mut if target == LEADER else LEADER
            agentos_arg = mut if target == AGENTOS else AGENTOS
            try:
                r = run_gate(leader_arg, agentos_arg,
                             os.path.join(tmp, "mut.json"))
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
        case("生产 work/leader_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nLEADER_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad),
                                            len(CASES)))
    print("LEADER_GATE_SELFTEST_OK" if not bad
          else "LEADER_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
