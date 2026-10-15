#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rewind_selftest —— 「关于 W1 上下文门的那道门」：证明 rewind_gate 判得红也判得绿。

做法（纯 stdlib、回环桩、不碰真模型）：
  把 tools/agent_core.py 拷到临时目录、单点改坏一处（4 个变异体），跑同一道
  rewind_gate（--core 指变异副本）：
    M1 跳 digest（v2 永不调 provider）→ D5 必红
    M2 回退被绕过（失败即抛不改口径）→ D6 必红
    M3 越界不拒（bounds 检查被摘）→ D4 必红
    M4 重放错位（少放一轮）→ D4 必红
  变异体跑完后双自查：生产 work/rewind_gate.json 字节不变 + 临时目录清空。

裁决：REWIND_GATE_SELFTEST n/n + REWIND_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(HERE, "agent_core.py")
GATE = os.path.join(HERE, "rewind_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "rewind_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 锚点原文, 替换, 期望点名的判据前缀)
    ("M1 跳 digest（v2 永不调 provider）",
     "    if provider is not None:\n        try:",
     "    if False:  # MUT 跳 digest\n        try:",
     "D5"),
    ("M2 回退被绕过（失败即抛不改口径）",
     "        new_hist, dropped = _compact_history(history, keep)",
     "        raise RuntimeError('MUT 回退被绕过')  # noqa",
     "D6"),
    ("M3 越界不拒（bounds 被摘）",
     "    if n < 1 or n > len(turns):",
     "    if False:  # MUT 越界不拒",
     "D4"),
    ("M4 重放错位（少放一轮）",
     "    end = turns[n] if n < len(turns) else len(recs)",
     "    end = turns[n] - 1 if n < len(turns) else len(recs)  # MUT",
     "D4"),
]


def run_gate(core_path, out_path, timeout=900):
    return subprocess.run([sys.executable, GATE, "--core", core_path,
                           "--out", out_path],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("REWIND_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="rwmut_")
    try:
        # BASE：真源上跑同一个门，必须全绿
        p0 = run_gate(CORE, os.path.join(tmp, "base.json"))
        case("BASE 真源全绿（REWIND_GATE OK + rc=0）",
             p0.returncode == 0 and "REWIND_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "REWIND_GATE_OK" in p0.stdout))

        src = open(CORE, encoding="utf-8").read()
        # 每个变异体：拷副本、单点替换（锚点必须恰 1 处）、跑门、要红
        for name, anchor, repl, want in MUTANTS:
            mut_path = os.path.join(tmp, "agent_core_mut.py")
            n = src.count(anchor)
            if n != 1:
                case(name, False, "锚点命中 %d 处（须恰 1）——变异体无效"
                     % n)
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

        # 双自查 1：生产 JSON 字节不变
        prod_after = b""
        if os.path.exists(PROD_JSON):
            with open(PROD_JSON, "rb") as f:
                prod_after = f.read()
        case("生产 work/rewind_gate.json 字节不变", prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        # 双自查 2：清场后自证（先真清，再断言 —— 断言必须在清理之后，
        # 否则测的是"还没清"的中间态）
        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nREWIND_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad),
                                            len(CASES)))
    print("REWIND_GATE_SELFTEST_OK" if not bad else "REWIND_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
