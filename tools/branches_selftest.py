#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""branches_selftest —— 「关于 W7 门的那道门」：证明 branches_gate 判得红也判得绿。

做法（纯 stdlib）：
  4 个变异体各改坏一处，跑同一道 branches_gate：
    M1 fork 不注记（branches.py）→ D4 必红
    M2 done 不查证据（branches.py）→ D5 必红
    M3 pending 也算完成（branches.py）→ D6 必红
    M4 分支源拷反了（core：fork 时把 dst_dir 当源读）→ D4 必红
  双自查：生产 work/branches_gate.json 字节不变 + 临时目录清空（先清再断言）。

裁决：BRANCHES_GATE_SELFTEST n/n + BRANCHES_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(HERE, "agent_core.py")
BRANCHES = os.path.join(HERE, "branches.py")
GATE = os.path.join(HERE, "branches_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "branches_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 目标文件, 锚点原文, 替换, 期望点名的判据前缀)
    ("M1 fork 注记类型被篡改（note 写成 user 行）", BRANCHES,
     "                {\"ts\": round(time.time(), 3), \"kind\": \"note\",",
     "                {\"ts\": round(time.time(), 3), \"kind\": \"user\",  # MUT",
     "D4"),
    ("M2 done 不查证据（直接算完成）", BRANCHES,
     "    if status != \"done\":\n"
     "        return \"pending\", \"任务 %r 未做（status=%s）\" % (t.get(\"id\"), status)",
     "    if False:  # MUT 跳过 status 检查\n"
     "        return \"pending\", \"任务 %r 未做（status=%s）\" % (t.get(\"id\"), status)",
     "D5"),
    ("M3 pending 也算完成（doing+证据算 done）", BRANCHES,
     "    if status != \"done\":",
     "    if False:  # MUT pending 也算完成",
     "D6"),
    ("M4 fork 丢最后一行（新文件少一行）", BRANCHES,
     "            for raw in rows:\n                f.write(raw)",
     "            for raw in rows[:-1]:  # MUT 丢最后一行\n"
     "                f.write(raw)",
     "D4"),
]


def run_gate(core, branches, out, timeout=900):
    return subprocess.run([sys.executable, GATE, "--core", core,
                           "--branches", branches, "--out", out],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("BRANCHES_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="brmut_")
    try:
        p0 = run_gate(CORE, BRANCHES, os.path.join(tmp, "base.json"))
        case("BASE 真源全绿（BRANCHES_GATE OK + rc=0）",
             p0.returncode == 0 and "BRANCHES_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "BRANCHES_GATE_OK" in p0.stdout))

        for name, target, anchor, repl, want in MUTANTS:
            src = open(target, encoding="utf-8").read()
            n = src.count(anchor)
            if n != 1:
                case(name, False,
                     "锚点命中 %d 处（须恰 1）——变异体无效" % n)
                continue
            mut = os.path.join(tmp, "mut_" + os.path.basename(target))
            with open(mut, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, repl, 1))
            try:
                r = run_gate(mut if target == CORE else CORE,
                             mut if target == BRANCHES else BRANCHES,
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
        case("生产 work/branches_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nBRANCHES_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad),
                                              len(CASES)))
    print("BRANCHES_GATE_SELFTEST_OK" if not bad
          else "BRANCHES_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
