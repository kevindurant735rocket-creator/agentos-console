#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subagent_selftest —— 「关于 W2 子代理门的那道门」：证明 subagent_gate 判得红也判得绿。

做法（纯 stdlib、回环桩、不碰真模型）：
  把 tools/agent_core.py 拷到临时目录、单点改坏一处（4 个变异体），跑同一道
  subagent_gate（--core 指变异副本）：
    M1 步数钳被摘 → D6 必红（99 不被钳，run_agent 自己的 24 上限兜底也不再等于 12）
    M2 嵌套闸被摘 → D5 必红（depth>0 不再拒，真发请求）
    M3 子集闸被摘 → D7 必红（子集外 write 真落盘）
    M4 摘要伪造    → D4 必红（summary 恒「已完成」）
  变异体跑完后双自查：生产 work/subagent_gate.json 字节不变 + 临时目录清空
  （先清再断言，断言不许测「还没清」的中间态）。

裁决：SUBAGENT_GATE_SELFTEST n/n + SUBAGENT_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(HERE, "agent_core.py")
GATE = os.path.join(HERE, "subagent_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "subagent_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 锚点原文, 替换, 期望点名的判据前缀)
    ("M1 步数钳被摘（99 不被钳）",
     "    cap = max(1, min(SUBAGENT_MAX_STEPS, want))",
     "    cap = max(1, want)  # MUT 钳被摘",
     "D6"),
    ("M2 嵌套闸被摘（depth>0 不拒）",
     "    if depth > 0:",
     "    if False:  # MUT 嵌套闸被摘",
     "D5"),
    ("M3 子集闸被摘（子集外放行）",
     "    if isinstance(subset, list) and name not in subset:",
     "    if False:  # MUT 子集闸被摘",
     "D7"),
    ("M4 摘要伪造（恒「已完成」）",
     "    summary = str(res.get(\"answer\") or \"\").strip()\n"
     "    if not summary and res.get(\"kind\") == \"limit\":\n"
     "        summary = \"（子代理步数预算用尽：%s）\""
     " % str(res.get(\"error\"))[:120]",
     "    summary = \"已完成\"  # MUT 摘要伪造\n"
     "    if not summary and res.get(\"kind\") == \"limit\":\n"
     "        summary = \"（子代理步数预算用尽：%s）\""
     " % str(res.get(\"error\"))[:120]",
     "D4"),
    ("M5 并行被串行化（batch 退化顺序跑）",
     "    with _cf.ThreadPoolExecutor(\n"
     "            max_workers=min(SUBAGENT_PARALLEL_MAX, len(prompts))) as _ex:\n"
     "        _futs = [_ex.submit(_one, i, pr) for i, pr in enumerate(prompts)]\n"
     "        for _f in _futs:\n"
     "            _f.result()          # 异常已在 _one 内兜住；这里只为汇合",
     "    for i, pr in enumerate(prompts):  # MUT 串行化\n"
     "        _one(i, pr)",
     "D10"),
    ("M6 chain 不交接（上一段结论不带上）",
     "        full = pr if not prev else (\"%s\\n\\n上一阶段结论：%s\""
     " % (pr, prev))",
     "        full = pr  # MUT 不交接",
     "D13"),
    ("M7 merge 被摘（不再汇总）",
     "    if merge:",
     "    if False:  # MUT merge 被摘",
     "D15"),
]


def run_gate(core_path, out_path, timeout=900):
    return subprocess.run([sys.executable, GATE, "--core", core_path,
                           "--out", out_path],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("SUBAGENT_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="samut_")
    try:
        # BASE：真源上跑同一个门，必须全绿
        p0 = run_gate(CORE, os.path.join(tmp, "base.json"))
        case("BASE 真源全绿（SUBAGENT_GATE OK + rc=0）",
             p0.returncode == 0 and "SUBAGENT_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "SUBAGENT_GATE_OK" in p0.stdout))

        src = open(CORE, encoding="utf-8").read()
        # 每个变异体：拷副本、单点替换（锚点必须恰 1 处）、跑门、要红
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

        # 双自查 1：生产 JSON 字节不变
        prod_after = b""
        if os.path.exists(PROD_JSON):
            with open(PROD_JSON, "rb") as f:
                prod_after = f.read()
        case("生产 work/subagent_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        # 双自查 2：先真清场，再断言（不许测"还没清"的中间态）
        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nSUBAGENT_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad),
                                              len(CASES)))
    print("SUBAGENT_GATE_SELFTEST_OK" if not bad
          else "SUBAGENT_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
