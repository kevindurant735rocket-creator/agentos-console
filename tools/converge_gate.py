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

# D1b verify_cmd 探测器自检（猜/建议跑不动的命令 → 判红）
pv = subprocess.run(
    [sys.executable, os.path.join(HERE, "verify_cmd_selftest.py")],
    capture_output=True,
    text=True,
    timeout=300,
)
gate(
    "verify_cmd_selftest 逐字绿（探测器不猜、不建议跑不动的命令）",
    pv.returncode == 0 and "VERIFY_CMD_SELFTEST_OK" in pv.stdout,
    (pv.stdout or pv.stderr)[-160:],
)

# D1c task_contract 探测器自检（判错类型 / 判太宽 都要判红）
pt = subprocess.run(
    [sys.executable, os.path.join(HERE, "task_contract_selftest.py")],
    capture_output=True,
    text=True,
    timeout=300,
)
gate(
    "task_contract_selftest 逐字绿（类型判定与契约要求不漂）",
    pt.returncode == 0 and "TASK_CONTRACT_SELFTEST_OK" in pt.stdout,
    (pt.stdout or pt.stderr)[-160:],
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

# D9b 自进化旋钮：apply_policy 能拧紧且只许收紧
c7 = cv.Converge()
ap = c7.apply_policy({"max_stall": 4, "max_explore": 3, "loop_repeat": 2})
gate(
    "自进化旋钮：apply_policy 真把阈值拧紧",
    ap.get("max_stall") == 4 and ap.get("max_explore") == 3 and ap.get("loop_repeat") == 2,
    "applied=%r" % (ap,),
)
c8 = cv.Converge()
ap2 = c8.apply_policy({"max_stall": 99, "max_explore": 99})
gate(
    "自进化旋钮：只许收紧，放宽被钳住（不能拆护栏）",
    ap2.get("max_stall") == 6 and ap2.get("max_explore") == 6,
    "applied=%r" % (ap2,),
)
c9 = cv.Converge()
ap3 = c9.apply_policy({"max_stall": 1})
gate(
    "自进化旋钮：下限 2 钳制（不会退化成每步都催）",
    ap3.get("max_stall") == 2,
    "applied=%r" % (ap3,),
)
c10 = cv.Converge()
ap4 = c10.apply_policy({"max_stall": "坏值"})
gate(
    "自进化旋钮：坏值不生效也不炸",
    ap4 == {} and c10.max_stall == 6,
    "applied=%r" % (ap4,),
)

# D9c 分类计数：停滞/循环/闸退回各自计数（自进化账靠它分辨病因）
c11 = cv.Converge(max_stall=2, loop_repeat=2, max_explore=2)
for _ in range(3):
    c11.observe("read_file", {"path": "f%d" % _}, ok=True)
c11.directive()
c12 = cv.Converge(loop_repeat=2, max_stall=99)
for _ in range(2):
    c12.observe("run_cmd", {"argv": ["ls"]}, ok=True, rc=0)
c12.directive()
c13 = cv.Converge(task_text="实现一个功能")
c13.gate("已经完成了。")
snap13 = c13.snapshot()
gate(
    "分类计数：停滞/循环/闸退回各自可分辨",
    snap13.get("gate_retries") == 1 and isinstance(snap13.get("stall_hits"), int),
    "snap=%r" % (snap13,),
)

# D9d 内核真的把旋钮接上了（规则→运行期）
src2 = open(os.path.join(HERE, "agent_core.py"), encoding="utf-8").read()
gate(
    "内核已接收敛旋钮（policy/apply_policy 都接线）",
    "_evo_converge_policy(" in src2 and "apply_policy(" in src2,
    "缺接线",
)

# D9e 变异体：拆掉「只许收紧」的钳制 → 门必须判红
tmp2 = tempfile.mkdtemp(prefix="converge_mut2_")
try:
    s3 = open(os.path.join(HERE, "converge.py"), encoding="utf-8").read()
    s3 = s3.replace(
        "newv = max(2, min(want, cur))", "newv = want"
    )
    mut2 = os.path.join(tmp2, "converge_mut2.py")
    open(mut2, "w", encoding="utf-8").write(s3)
    sys.path.insert(0, tmp2)
    import importlib

    try:
        m2 = importlib.import_module("converge_mut2")
    finally:
        sys.path.remove(tmp2)
        sys.modules.pop("converge_mut2", None)
    cm2 = m2.Converge()
    cm2.apply_policy({"max_stall": 99})
    gate(
        "变异体：拆掉收紧钳制 → 阈值被放宽（门判红）",
        cm2.max_stall == 99,
        "变异体没放宽 = 钳制摘不干净",
    )
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

# D9f 验证类命令才算核对证据（跑过命令 ≠ 核对过）
_v1 = cv.Converge()
_v1.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
_v2 = cv.Converge()
_v2.observe("run_cmd", {"argv": ["curl", "-O", "x"]}, ok=True, rc=0)
_v3 = cv.Converge()
_v3.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=1)
gate(
    "命令判据：只有验证类命令（rc=0）算核对证据",
    _v1.verify_seen is True
    and _v2.verify_seen is False
    and _v3.verify_seen is False,
    "pytest=%s curl=%s failed=%s"
    % (_v1.verify_seen, _v2.verify_seen, _v3.verify_seen),
)

# D9g 变异体：把命令判据改成恒真 → 「curl 不算核对」必须翻红
tmp3 = tempfile.mkdtemp(prefix="converge_mut3_")
try:
    s4 = open(os.path.join(HERE, "converge.py"), encoding="utf-8").read()
    s4 = s4.replace(
        "if _vc and (not args or _is_verify_cmd(args)):",
        "if _vc:",
    )
    mut3 = os.path.join(tmp3, "converge_mut3.py")
    open(mut3, "w", encoding="utf-8").write(s4)
    sys.path.insert(0, tmp3)
    import importlib

    try:
        m3 = importlib.import_module("converge_mut3")
    finally:
        sys.path.remove(tmp3)
        sys.modules.pop("converge_mut3", None)
    cm3 = m3.Converge()
    cm3.observe("run_cmd", {"argv": ["curl", "-O", "x"]}, ok=True, rc=0)
    gate(
        "变异体：命令判据恒真 → curl 也被当核对（门判红）",
        cm3.verify_seen is True,
        "变异体没放宽 = 命令判据摘不干净",
    )
finally:
    shutil.rmtree(tmp3, ignore_errors=True)

# D9h 动作边界的核对：改完必核对（蒸馏自 aider 的 edit→auto-lint/test）
_vi = {"cmdline": "pytest -q", "why": "pyproject.toml 存在", "cmd": ["pytest", "-q"]}
_k1 = cv.Converge(verify_info=_vi)
_k1.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_kd = _k1.directive()
gate(
    "核对：改完盘进待核对态并注入核对指令",
    _k1.verify_pending is True
    and _kd is not None
    and "核对指令" in _kd
    and "pytest -q" in _kd,
    "pending=%s d=%r" % (_k1.verify_pending, (_kd or "")[:80]),
)
_k2 = cv.Converge(verify_info=_vi)
_k2.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_k2.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
gate(
    "核对：跑过验证命令后待核对清零",
    _k2.verify_pending is False,
    "pending=%s" % (_k2.verify_pending,),
)
_k3 = cv.Converge()
_k3.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
gate(
    "核对：探测不到验证命令就不催（不猜）",
    _k3.directive() is None,
    "不该催",
)

# D9i 变异体：摘掉「改完进待核对」→ 门必须判红
tmp4 = tempfile.mkdtemp(prefix="converge_mut4_")
try:
    s5 = open(os.path.join(HERE, "converge.py"), encoding="utf-8").read()
    s5 = s5.replace(
        "            self.verify_pending = True\n            self.verify_nagged = False",
        "            self.verify_nagged = False",
    )
    mut4 = os.path.join(tmp4, "converge_mut4.py")
    open(mut4, "w", encoding="utf-8").write(s5)
    sys.path.insert(0, tmp4)
    import importlib

    try:
        m4 = importlib.import_module("converge_mut4")
    finally:
        sys.path.remove(tmp4)
        sys.modules.pop("converge_mut4", None)
    cm4 = m4.Converge(verify_info=_vi)
    cm4.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
    gate(
        "变异体：摘掉「改完进待核对」→ 核对指令不再出现（门判红）",
        cm4.directive() is None,
        "变异体仍出核对指令 = 判据摘不干净",
    )
finally:
    shutil.rmtree(tmp4, ignore_errors=True)

# D9j 任务契约：类型化证据 + bugfix 的「先复现」纪律
try:
    import task_contract as tcm
except Exception:  # noqa: BLE001
    tcm = None
if tcm is None:
    gate("任务契约模块可导入", False, "import task_contract 失败")
else:
    gate("任务契约模块可导入", True, "")
    _bug_c = tcm.contract_for(
        tcm.T_BUGFIX, {"cmdline": "make test", "why": "Makefile 存在"}
    )
    # 契约本身
    gate(
        "契约：bugfix 要求通过核对 + 软要求红→绿",
        "verify_pass" in _bug_c["hard"] and "repro_red_then_green" in _bug_c["soft"],
        "hard=%r soft=%r" % (_bug_c["hard"], _bug_c["soft"]),
    )
    # 盲改提醒：动手改了却没见过「红」
    _c1 = cv.Converge(contract=_bug_c)
    _c1.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
    _c1d = _c1.directive()
    gate(
        "契约：缺陷修复盲改 → 注入复现指令",
        _c1d is not None and "复现指令" in _c1d,
        repr(_c1d)[:90],
    )
    # 红→改→绿成立后不再催
    _c2 = cv.Converge(contract=_bug_c)
    _c2.observe("run_cmd", {"argv": ["make", "test"]}, ok=True, rc=1)
    _c2.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
    _c2.observe("run_cmd", {"argv": ["make", "test"]}, ok=True, rc=0)
    gate(
        "契约：红→改→绿闭环成立（repro_green）",
        _c2.repro_green is True and _c2.snapshot().get("repro_green") is True,
        "snap=%r" % (_c2.snapshot(),),
    )
    # 写被环境锁住 → 不逼模型写它写不了的
    _c3 = cv.Converge(contract=_bug_c)
    _c3.observe("write_file", {"path": "a.py", "content": "x"}, ok=False)
    _ok3, _miss3, _why3 = _c3.evidence_ok("做完了")
    gate(
        "契约：写被锁 → 撤掉写类要求（不逼模型写够不着的东西）",
        _ok3 is True,
        "missing=%r why=%r" % (_miss3, _why3),
    )

# D9k 变异体：摘掉契约检查 → 门必须判红
tmp5 = tempfile.mkdtemp(prefix="converge_mut5_")
try:
    s6 = open(os.path.join(HERE, "converge.py"), encoding="utf-8").read()
    s6 = s6.replace(
        "        _ev_ok, _ev_missing, _ev_why = self.evidence_ok(text)",
        "        _ev_ok, _ev_missing, _ev_why = (True, [], [])",
    )
    mut5 = os.path.join(tmp5, "converge_mut5.py")
    open(mut5, "w", encoding="utf-8").write(s6)
    sys.path.insert(0, tmp5)
    import importlib

    try:
        m5 = importlib.import_module("converge_mut5")
    finally:
        sys.path.remove(tmp5)
        sys.modules.pop("converge_mut5", None)
    _mc = m5.Converge(contract={"type": "bugfix", "hard": ["verify_pass"]})
    _mc.observe("read_file", {"path": "a"}, ok=True)
    _mgate = _mc.gate("已经修好了。")
    gate(
        "变异体：摘掉契约检查 → 缺证据的完成宣言被放行（门判红）",
        _mgate[0] is True,
        "变异体仍判红 = 契约检查摘不干净",
    )
finally:
    shutil.rmtree(tmp5, ignore_errors=True)

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
