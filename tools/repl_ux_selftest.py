#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repl_ux_selftest —— 「关于 W5 终端 UX 门的那道门」：证明 repl_ux_gate 判得红也判得绿。

做法（纯 stdlib，不碰真模型）：
  把 tools/agent_core.py 拷到临时目录、单点改坏一处（4 个变异体），跑同一道
  repl_ux_gate（--core 指变异副本）：
    M1 续行被摘（REPL 循环不再收多行）→ D3 必红
    M2 @补全路由被摘（Tab 不再走 @ 候选）→ D3 必红
    M3 摘要集成被摘（tool 事件不再带 summary）→ D7 必红
    M4 缩进被摘（_format_answer 原样返回）→ D8 必红
  双自查：生产 work/repl_ux_gate.json 字节不变 + 临时目录清空（先清再断言）。

裁决：REPL_UX_GATE_SELFTEST n/n + REPL_UX_GATE_SELFTEST_OK / _RED，exit 0/1
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(HERE, "agent_core.py")
GATE = os.path.join(HERE, "repl_ux_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "repl_ux_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 锚点原文, 替换, 期望点名的判据前缀)
    (
        "M1 续行被摘（REPL 不再收多行）",
        "        while _line_wants_continue(text):",
        "        while False:  # MUT 续行被摘",
        "D3",
    ),
    (
        "M2 @补全路由被摘（Tab 不走 @ 候选）",
        '        if text.startswith("@"):',
        "        if False:  # MUT @补全被摘",
        "D3",
    ),
    (
        "M3 摘要集成被摘（tool 事件无 summary）",
        '"summary": _tool_summary(name, args, res),',
        '"summary": "",  # MUT 摘要被摘',
        "D7",
    ),
    (
        "M4 缩进被摘（_format_answer 原样返回）",
        '    return s.replace("\\n", "\\n  ")',
        "    return s  # MUT 不缩进",
        "D8",
    ),
]


def run_gate(core_path, out_path, timeout=900):
    return subprocess.run(
        [sys.executable, GATE, "--core", core_path, "--out", out_path],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main():
    print("REPL_UX_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="uxmut_")
    try:
        p0 = run_gate(CORE, os.path.join(tmp, "base.json"))
        case(
            "BASE 真源全绿（REPL_UX_GATE OK + rc=0）",
            p0.returncode == 0 and "REPL_UX_GATE_OK" in p0.stdout,
            "rc=%s ok=%s" % (p0.returncode, "REPL_UX_GATE_OK" in p0.stdout),
        )

        src = open(CORE, encoding="utf-8").read()
        for name, anchor, repl, want in MUTANTS:
            mut_path = os.path.join(tmp, "agent_core_mut.py")
            n = src.count(anchor)
            if n != 1:
                case(name, False, "锚点命中 %d 处（须恰 1）——变异体无效" % n)
                continue
            with open(mut_path, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, repl, 1))
            try:
                r = run_gate(mut_path, os.path.join(tmp, "mut.json"))
            except subprocess.TimeoutExpired:
                case(name, False, "门超时")
                continue
            hit = ("FAIL %s" % want) in r.stdout
            case(
                name,
                r.returncode != 0 and hit,
                "rc=%d 期望红「%s」→%s"
                % (r.returncode, want, "命中" if hit else "未命中"),
            )

        prod_after = b""
        if os.path.exists(PROD_JSON):
            with open(PROD_JSON, "rb") as f:
                prod_after = f.read()
        case(
            "生产 work/repl_ux_gate.json 字节不变",
            prod_before == prod_after,
            "before=%dB after=%dB" % (len(prod_before), len(prod_after)),
        )

        shutil.rmtree(tmp, ignore_errors=True)
        case(
            "临时目录已清理（变异副本不残留）",
            not os.path.exists(tmp),
            "仍存在=%s" % os.path.exists(tmp),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nREPL_UX_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("REPL_UX_GATE_SELFTEST_OK" if not bad else "REPL_UX_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
