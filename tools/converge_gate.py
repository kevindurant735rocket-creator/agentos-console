#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""converge_gate —— 收敛层的门：判据有牙 + 变异体必红 + 零污染。

门存在的意义：收敛引擎是内核的新地基（2026-10-19 事故的直接解药），
它一旦被改弱（阈值放宽 / 判据删掉 / 只剩提示不剩执行），后面所有
「agent 原地打转 900 秒」都会复发。所以它必须进门链，且自带变异体。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import converge as cv  # noqa: E402

FAILS = []
PASSES = 0


def gate(name, ok, detail=""):
    global PASSES
    if ok:
        PASSES += 1
    else:
        FAILS.append((name, str(detail)[:160]))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + str(detail)[:160]) if not ok else ""))


# D1 selftest 逐字绿（子判据集自身可信）
p = subprocess.run(
    [sys.executable, os.path.join(HERE, "converge_selftest.py")],
    capture_output=True,
    text=True,
    timeout=300,
)
gate(
    "converge_selftest 逐字绿",
    p.returncode == 0 and "CONVERGE_SELFTEST_OK" in p.stdout,
    (p.stdout or p.stderr)[-160:],
)

# D2 内核真的接了收敛层（不是只有一个孤立模块）
src = open(os.path.join(HERE, "agent_core.py"), encoding="utf-8").read()
gate(
    "内核已接收敛层（Converge/directive/gate 三处都在）",
    "_cv_mod.Converge(" in src
    and "_conv.directive()" in src
    and "_conv.gate(" in src,
    "缺接线",
)

# D3 阈值不是被放开的（收敛的牙还在）
c = cv.Converge()
gate(
    "阈值仍是紧的（stall≤8 / explore≤8 / loop≤4）",
    c.max_stall <= 8 and c.max_explore <= 8 and c.loop_repeat <= 4,
    "stall=%s explore=%s loop=%s" % (c.max_stall, c.max_explore, c.loop_repeat),
)

# D4 停滞一定被抓住
c2 = cv.Converge(max_stall=3)
for i in range(4):
    c2.observe("read_file", {"path": "f%d" % i}, ok=True)
gate("停滞一定被抓（连读 4 个不同文件也判停滞）", c2.directive() is not None, "没抓到")

# D5 同参循环一定被抓住
c3 = cv.Converge(loop_repeat=3, max_stall=99)
for _ in range(3):
    c3.observe("run_cmd", {"argv": ["ls"]}, ok=True, rc=0)
gate("同参循环一定被抓", c3.directive() is not None, "没抓到")

# D6 交付闸：无证据的成功宣言被退回
c4 = cv.Converge(task_text="实现一个新功能")
c4.observe("read_file", {"path": "a"}, ok=True)
c4.observe("read_file", {"path": "b"}, ok=True)
ok6, why6 = c4.gate("功能已经实现完毕。")
gate("交付闸：无写盘证据的成功宣言被退回", ok6 is False, why6)

# D7 交付闸：有写盘+核对证据 → 放行
c5 = cv.Converge(task_text="实现一个新功能")
c5.observe("write_file", {"path": "a", "content": "x"}, ok=True)
c5.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
ok7, why7 = c5.gate("功能已经实现完毕，pytest rc=0。")
gate("交付闸：有写盘+核对证据放行", ok7 is True, why7)

# D8 诚实报告失败不被退回
c6 = cv.Converge(task_text="实现一个新功能")
c6.observe("write_file", {"path": "a", "content": "x"}, ok=True)
ok8, why8 = c6.gate("做不到：写盘被拒（审批未通过），需要你放行后再试。")
gate("交付闸：诚实报告失败不被退回", ok8 is True, why8)

# D9 变异体：把停滞判据摘掉 → 门必须判红
tmp = tempfile.mkdtemp(prefix="converge_mut_")
try:
    mut = os.path.join(tmp, "converge_mut.py")
    s = open(os.path.join(HERE, "converge.py"), encoding="utf-8").read()
    s2 = s.replace("self.since_productive >= self.max_stall", "False")
    open(mut, "w", encoding="utf-8").write(s2)
    # 用变异副本跑「停滞是否被抓」这条判据
    sys.path.insert(0, tmp)
    import importlib

    try:
        m = importlib.import_module("converge_mut")
    finally:
        sys.path.remove(tmp)
        sys.modules.pop("converge_mut", None)
    cm = m.Converge(max_stall=3)
    for i in range(4):
        cm.observe("read_file", {"path": "f%d" % i}, ok=True)
    gate(
        "变异体：摘掉停滞判据 → 停滞抓不到（门判红）",
        cm.directive() is None,
        "变异体仍抓到 = 判据摘不干净",
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# D10 零污染：门跑完 work/ 下不留本门专属残留
gate(
    "零污染：门不写生产账",
    not os.path.exists(os.path.join(ROOT, "work", "converge_gate_scratch.json")),
    "有残留",
)

print("\nCONVERGE_GATE %d/%d" % (PASSES, PASSES + len(FAILS)))
if FAILS:
    print("CONVERGE_GATE_RED")
    for n, d in FAILS:
        print("  - %s :: %s" % (n, d))
    sys.exit(1)
print("CONVERGE_GATE_OK")
