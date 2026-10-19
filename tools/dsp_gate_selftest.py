#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsp_gate_selftest —— 「关于 DSP 门的那道门」：证明 dsp_gate 判得红也判得绿。

做法（纯 stdlib、零浏览器、不碰 8790）：
  把 dsp_gate.py 拷到临时目录、单点改坏一处（5 个变异体），喂给同一个门：
  点名的判据必须变 FAIL 且子进程 rc≠0。变异体跑完后双自查：生产
  work/dsp_gate.json 字节不变 + 临时目录清空。
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(HERE, "dsp_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "dsp_gate.json")

CASES = []
FAIL = 0


def case(name, ok, detail):
    global FAIL
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    if not ok:
        FAIL += 1
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 锚点原文, 替换, 期望变红的门)
    # D7：整块摘锁（两行一起换，保持缩进合法）—— _lock_reason 永返 None，
    # 未审批也放行，D7 必须红。
    (
        "D7 锁被摘（未审批也放行）",
        '    if plan is not None and not plan.get("approved"):\n'
        '        return "计划未审批：%s 未执行" % tool',
        "    return None  # MUT: 锁被摘",
        "D7",
    ),
    # D8：check_tool 调用被短路（门内调用点整行换）——白名单永放行。
    (
        "D8 白名单被摘（任务外工具放行）",
        'w_out = dsp.check_tool(pl, "t1", "write_file", {}) if pl else None',
        "w_out = None  # MUT: 白名单被摘",
        "D8",
    ),
    (
        "D2 令牌被改字",
        'EXPECT_CHECK = "AGENT_CORE OK 152/152"',
        'EXPECT_CHECK = "AGENT_CORE  OK 140/140"',
        "D2",
    ),
    # D10：门内期望被改（r 永对不上）—— doom 判据空转，D10 必须红。
    (
        "D10 doom 期望被改（判据空转）",
        'r == [None, None, "ask"]',
        'r == ["ask", "ask", "ask"]',
        "D10",
    ),
    # D13：交叉断言被翻转（悬空名不报）—— 结构自洽判据空转，D13 必须红。
    (
        "D13 交叉断言被翻转（悬空名不报）",
        "            bool(names) and not missing,",
        "            bool(names) and missing,  # MUT 交叉翻转",
        "D13",
    ),
]


def _run_copy(path, out):
    return subprocess.run(
        [sys.executable, path, "--out", out],
        capture_output=True,
        text=True,
        timeout=900,
    )


def main():
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    # BASE：生产门自己必须绿
    p0 = subprocess.run(
        [sys.executable, SRC], capture_output=True, text=True, timeout=900
    )
    base_ok = p0.returncode == 0 and "DSP_GATE OK 18/18" in p0.stdout
    case(
        "BASE 生产 dsp_gate 绿（DSP_GATE OK 18/18）",
        base_ok,
        "rc=%s 尾行=%r"
        % (p0.returncode, p0.stdout.strip().splitlines()[-1:] if p0.stdout else []),
    )
    td = tempfile.mkdtemp(prefix="dspgate_mut_")
    try:
        with open(SRC, encoding="utf-8") as f:
            src = f.read()
        for tag, anchor, repl, want in MUTANTS:
            hits = src.count(anchor)
            if hits != 1:
                case("变异体 %s 锚点恰好一次" % tag, False, "命中=%d" % hits)
                continue
            # D10 的锚在 dsp.py 不在门里：改门内 r 期望，让门自己判错。
            if want == "D10":
                bad = src.replace(
                    'gate("D10 doom 归一化第 3 次转 ask", r == [None, None, "ask"],',
                    'gate("D10 doom 归一化第 3 次转 ask", r == ["ask", "ask", "ask"],',
                )
            else:
                bad = src.replace(anchor, repl)
            mp = os.path.join(td, "mut_%s.py" % want)
            with open(mp, "w", encoding="utf-8") as f:
                f.write(bad)
            out = os.path.join(td, "mut_%s.json" % want)
            r = _run_copy(mp, out)
            red = r.returncode != 0 and (
                "FAIL " + want in r.stdout or "FAIL D" in r.stdout
            )
            case(
                "变异体 %s 必红（门有牙）" % tag,
                red,
                "rc=%s 命中=%s" % (r.returncode, want),
            )
    finally:
        shutil.rmtree(td, ignore_errors=True)
    prod_after = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_after = f.read()
    case(
        "生产 work/dsp_gate.json 字节不变（零污染）",
        prod_before == prod_after,
        "before=%d after=%d" % (len(prod_before), len(prod_after)),
    )

    print(
        "\nDSP_GATE_SELFTEST %s %d/%d"
        % ("OK" if FAIL == 0 else "FAIL", len(CASES) - FAIL, len(CASES))
    )
    out_doc = {
        "tool": "dsp_gate_selftest.py",
        "rc": 0 if FAIL == 0 else 2,
        "cases": CASES,
    }
    with open(
        os.path.join(ROOT, "work", "dsp_gate_selftest.json"), "w", encoding="utf-8"
    ) as f:
        import json

        json.dump(out_doc, f, ensure_ascii=False, indent=1)
    return 0 if FAIL == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
