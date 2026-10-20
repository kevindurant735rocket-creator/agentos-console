#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""converge_selftest —— 收敛引擎的证伪集。

每个用例都按「先证明它能抓住坏行为，再证明它不误伤好行为」写：
判据有牙（tooth）= 用坏样本喂它，它必须报警；对照（control）= 用好样本
喂它，它必须放过。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import converge as cv  # noqa: E402

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    return bool(ok)


def tooth(name, detect, bad, good):
    """坏样本必须被抓；好样本必须被放过。"""
    b = detect(bad)
    g = detect(good)
    case(
        "%s（坏样本被抓 + 好样本放过）" % name,
        b is True and g is False,
        "bad=%s good=%s" % (b, g),
    )


# ── 1. 停滞探测：连续只读无产出 ────────────────────────────────
def _stall_detect(c):
    for i in range(c.max_stall + 1):
        c.observe("read_file", {"path": "a%d.py" % i}, ok=True)
    return c.directive() is not None and "收敛指令" in (c.directive() or "") + (
        c.notes[-1] if c.notes else ""
    )


c_bad = cv.Converge(max_stall=4)
c_bad.observe("read_file", {"path": "x"}, ok=True)
c_bad.observe("list_dir", {"path": "."}, ok=True)
c_bad.observe("search", {"pattern": "TODO"}, ok=True)
c_bad.observe("read_file", {"path": "y"}, ok=True)
c_bad.observe("gh_search", {"query": "x"}, ok=True)
d = c_bad.directive()
case(
    "停滞探测：连续 5 步只读无产出 → 注入收敛指令",
    d is not None and "收敛指令" in d and "没有产出" in d,
    repr(d)[:120],
)

c_good = cv.Converge(max_stall=4)
c_good.observe("read_file", {"path": "x"}, ok=True)
c_good.observe("write_file", {"path": "a", "content": "b"}, ok=True)
d2 = c_good.directive()
case("停滞探测：刚写完盘不算停滞（不误伤）", d2 is None, repr(d2)[:100])

# ── 2. 循环熔断：同参重复 ──────────────────────────────────────
c_loop = cv.Converge(loop_repeat=3, max_stall=99)
for _ in range(3):
    c_loop.observe("run_cmd", {"argv": ["ls", "-la"]}, ok=True, rc=0)
d3 = c_loop.directive()
case(
    "循环熔断：同参同工具重复 3 次 → 注入换路指令",
    d3 is not None and "换" in d3 and "重复" in d3,
    repr(d3)[:120],
)

c_diff = cv.Converge(loop_repeat=3, max_stall=99)
for i in range(4):
    c_diff.observe("read_file", {"path": "f%d.py" % i}, ok=True)
d4 = c_diff.directive()
case("循环熔断：换参数不算循环（不误伤）", d4 is None, repr(d4)[:100])

# ── 3. 阶段机 ───────────────────────────────────────────────────
c_ph = cv.Converge(max_explore=3)
c_ph.observe("read_file", {"path": "a"}, ok=True)
c_ph.observe("list_dir", {"path": "."}, ok=True)
c_ph.observe("search", {"pattern": "x"}, ok=True)
d5 = c_ph.directive()
case(
    "阶段机：探查 3 步后催「去动手」",
    d5 is not None and "动手" in d5,
    repr(d5)[:120],
)
c_ph2 = cv.Converge(max_explore=3)
c_ph2.observe("read_file", {"path": "a"}, ok=True)
c_ph2.observe("write_file", {"path": "b", "content": "c"}, ok=True)
case("阶段机：写盘后进 ACT 阶段", c_ph2.phase == cv.P_ACT, c_ph2.phase)

c_ph3 = cv.Converge()
c_ph3.observe("write_file", {"path": "b", "content": "c"}, ok=True)
c_ph3.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
case("阶段机：写盘+验证 → VERIFY/DELIVER", c_ph3.phase in (cv.P_VERIFY, cv.P_DELIVER), c_ph3.phase)

# ── 4. 交付闸：没有证据不许说完成 ───────────────────────────────
c_g1 = cv.Converge(task_text="部署一个新仓库到 GitHub")
c_g1.observe("read_file", {"path": "a"}, ok=True)
ok1, why1 = c_g1.gate("我已经完成部署，仓库在 GitHub 上。")
case(
    "交付闸：像实现类任务但零写盘零验证 → 退回",
    ok1 is False and "没有" in why1,
    why1,
)

c_g2 = cv.Converge(task_text="部署一个新仓库到 GitHub")
c_g2.observe("write_file", {"path": "b", "content": "c"}, ok=True)
c_g2.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
ok2, why2 = c_g2.gate("已改 tools/x.py，pytest rc=0。")
case("交付闸：有写盘+验证证据 → 放行", ok2 is True, why2)

c_g3 = cv.Converge()
c_g3.observe("read_file", {"path": "a"}, ok=True)
ok3, why3 = c_g3.gate("我只是读了一下文件，没有做任何改动。")
case("交付闸：纯问答类任务（不暗示构建）不误伤", ok3 is True, why3)

c_g4 = cv.Converge()
ok4, why4 = c_g4.gate("")
case("交付闸：空终答 → 退回", ok4 is False and "空" in why4, why4)

c_g5 = cv.Converge(task_text="写个文件")
c_g5.observe("write_file", {"path": "b", "content": "c"}, ok=True)
c_g5.observe("read_file", {"path": "b"}, ok=True)
ok5, why5 = c_g5.gate("写好了，见 tools/x.py。")
case("交付闸：写完读回即核对（放行）", ok5 is True, why5)

c_g6 = cv.Converge(task_text="写个文件并验证")
c_g6.observe("write_file", {"path": "b", "content": "c"}, ok=True)
ok6, why6 = c_g6.gate("已经写好了。")
case(
    "交付闸：需求点名要验证却没验 → 退回",
    ok6 is False and "验证" in why6,
    why6,
)

c_g6b = cv.Converge(task_text="写个文件")
c_g6b.observe("write_file", {"path": "b", "content": "c"}, ok=True)
ok6b, why6b = c_g6b.gate("已经写好了。")
case("交付闸：没点名验证的小改动不卡（不误伤）", ok6b is True, why6b)

c_g7 = cv.Converge(task_text="写个文件")
c_g7.observe("read_file", {"path": "a"}, ok=True)
c_g7.observe("read_file", {"path": "b"}, ok=True)
ok7, why7 = c_g7.gate("我看完了这些文件的情况。")
case(
    "交付闸：像要产出但只探查没写盘 → 退回",
    ok7 is False and "探查" in why7,
    why7,
)

c_g8 = cv.Converge(task_text="分析一下这个仓库的结构")
ok8, why8 = c_g8.gate("结构是 tools/ 下 100 个 py 文件。")
case("交付闸：纯分析任务（零工具）不误伤", ok8 is True, why8)

# ── 5. 指纹语义：换参数不算重复 ────────────────────────────────
f1 = cv._fingerprint("run_cmd", {"argv": ["ls"]})
f2 = cv._fingerprint("run_cmd", {"argv": ["ls"]})
f3 = cv._fingerprint("run_cmd", {"argv": ["ls", "-la"]})
f4 = cv._fingerprint("read_file", {"argv": ["ls"]})
case("指纹：同工具同参=同指纹", f1 == f2)
case("指纹：换参数=不同指纹", f1 != f3)
case("指纹：换工具=不同指纹", f1 != f4)
case(
    "指纹：参数顺序无关（dict 排序）",
    cv._fingerprint("run_cmd", {"a": 1, "b": 2}) == cv._fingerprint("run_cmd", {"b": 2, "a": 1}),
)

# ── 6. 验证命令识别 ─────────────────────────────────────────────
case("验证识别：pytest 算验证", cv._is_verify_cmd({"argv": ["pytest", "-q"]}))
case("验证识别：git diff 算验证", cv._is_verify_cmd({"argv": ["git", "diff"]}))
case(
    "验证识别：ls/cat/wc/sha256sum 算核对（文件核对也是证据）",
    cv._is_verify_cmd({"argv": ["ls", "-la"]})
    and cv._is_verify_cmd({"argv": ["cat", "x.txt"]})
    and cv._is_verify_cmd({"argv": ["wc", "-c", "x.txt"]})
    and cv._is_verify_cmd({"argv": ["sha256sum", "x.txt"]}),
)
case(
    "验证识别：latest 不含 test（整词匹配，不是子串）",
    # echo 不是核对命令；「latest」里含 "test" 也不该被误判成核对
    not cv._is_verify_cmd({"argv": ["echo", "latest"]})
    and not cv._is_verify_cmd({"argv": ["run", "fastest"]}),
)
case(
    "验证识别：shell 包装里的 pytest 仍算验证",
    cv._is_verify_cmd({"argv": ["bash", "-c", "pytest -q"]}),
)
case(
    "验证识别：shell 包装里的 curl 不算验证",
    not cv._is_verify_cmd({"argv": ["sh", "-c", "curl x"]}),
)
case("验证识别：npm test 算验证", cv._is_verify_cmd({"argv": ["npm", "test"]}))
case("验证识别：纯下载不算验证", not cv._is_verify_cmd({"argv": ["curl", "-O", "x"]}))
case("验证识别：字符串 argv 也能认", cv._is_verify_cmd({"argv": "pytest -q"}))
case("验证识别：空 argv 不算", not cv._is_verify_cmd({}))

# ── 7. 收敛画像（真实信号账） ───────────────────────────────────
rep = cv.convergence_report()
case(
    "收敛画像：能读出轮次/工具均值",
    isinstance(rep, dict) and "turns" in rep and "avg_tools" in rep,
    json.dumps(rep, ensure_ascii=False),
)

# ── 8. 快照可审计 ───────────────────────────────────────────────
c_s = cv.Converge()
c_s.observe("read_file", {"path": "a"}, ok=True)
snap = c_s.snapshot()
case(
    "快照：字段齐全（阶段/步数/写/验）",
    all(k in snap for k in ("phase", "steps", "write_seen", "verify_seen")),
    json.dumps(snap, ensure_ascii=False),
)

# ── 9. 注入不重复轰炸：注入后重置计数 ───────────────────────────
c_r = cv.Converge(max_stall=3)
for i in range(3):
    c_r.observe("read_file", {"path": "f%d" % i}, ok=True)
first = c_r.directive()
second = c_r.directive()
case(
    "停滞指令注入一次后不连发（不轰炸模型）",
    first is not None and second is None,
    "first=%s second=%s" % (bool(first), bool(second)),
)

# ── 10. 自进化旋钮：apply_policy ─────────────────────────────────
_ap1 = cv.Converge()
_a1 = _ap1.apply_policy({"max_stall": 4, "max_explore": 3, "loop_repeat": 2})
case(
    "旋钮：apply_policy 真拧紧三个阈值",
    _a1.get("max_stall") == 4
    and _a1.get("max_explore") == 3
    and _a1.get("loop_repeat") == 2
    and _ap1.max_stall == 4,
    "applied=%r" % (_a1,),
)
_ap2 = cv.Converge()
_a2 = _ap2.apply_policy({"max_stall": 99, "max_explore": 99, "loop_repeat": 99})
case(
    "旋钮：只许收紧，放宽被钳住（不能拆护栏）",
    _a2.get("max_stall") == 6
    and _a2.get("max_explore") == 6
    and _a2.get("loop_repeat") == 3,
    "applied=%r" % (_a2,),
)
_ap3 = cv.Converge()
_a3 = _ap3.apply_policy({"max_stall": 1})
case("旋钮：下限 2 钳制（不会退化成每步都催）", _a3.get("max_stall") == 2, "applied=%r" % (_a3,))
_ap4 = cv.Converge()
_a4 = _ap4.apply_policy({"max_stall": "坏值", "loop_repeat": None})
case(
    "旋钮：坏值不生效也不炸",
    _a4 == {} and _ap4.max_stall == 6,
    "applied=%r" % (_a4,),
)

# ── 11. 分类命中计数（自进化账靠它分辨病因）──────────────────────
_h1 = cv.Converge(max_stall=2, loop_repeat=99, max_explore=99)
for _i in range(3):
    _h1.observe("read_file", {"path": "f%d" % _i}, ok=True)
_h1.directive()
case("分类计数：停滞命中被单独记下", _h1.snapshot().get("stall_hits") == 1, "snap=%r" % (_h1.snapshot(),))
_h2 = cv.Converge(loop_repeat=2, max_stall=99)
for _i in range(2):
    _h2.observe("run_cmd", {"argv": ["ls"]}, ok=True, rc=0)
_h2.directive()
case("分类计数：循环命中被单独记下", _h2.snapshot().get("loop_hits") == 1, "snap=%r" % (_h2.snapshot(),))
_h3 = cv.Converge(task_text="实现一个功能")
_h3.gate("已经完成了。")
case(
    "分类计数：交付闸退回被单独记下",
    _h3.snapshot().get("gate_retries") == 1,
    "snap=%r" % (_h3.snapshot(),),
)
_h4 = cv.Converge(task_text="实现一个功能")
_h4.gate("写盘被拒，做不到。")
case(
    "分类计数：诚实报告失败不算退回",
    _h4.snapshot().get("gate_retries") == 0,
    "snap=%r" % (_h4.snapshot(),),
)

# ── 12. 验证类命令才算核对证据 ────────────────────────────────────
_v1 = cv.Converge()
_v1.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
case("命令判据：pytest 算核对证据", _v1.verify_seen is True, "snap=%r" % (_v1.snapshot(),))
_v2 = cv.Converge()
_v2.observe("run_cmd", {"argv": ["curl", "-O", "x"]}, ok=True, rc=0)
case(
    "命令判据：curl 跑了但不算核对（没查本轮改动）",
    _v2.verify_seen is False,
    "snap=%r" % (_v2.snapshot(),),
)
_v3 = cv.Converge()
_v3.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=1)
case(
    "命令判据：检查失败（rc≠0）不算核对通过",
    _v3.verify_seen is False,
    "snap=%r" % (_v3.snapshot(),),
)
_v4 = cv.Converge()
_v4.observe("run_cmd", {}, ok=True, rc=0)
case(
    "命令判据：无参数（离线评分路径）按 rc=0 兜底",
    _v4.verify_seen is True,
    "snap=%r" % (_v4.snapshot(),),
)

# ── 13. 动作边界的核对（蒸馏自 aider 的 edit→auto-lint/test）─────
_VINFO = {"cmdline": "pytest -q", "why": "pyproject.toml 存在", "cmd": ["pytest", "-q"]}
_w1 = cv.Converge(verify_info=_VINFO)
_w1.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
case(
    "核对：改完盘进入待核对态",
    _w1.verify_pending is True and _w1.snapshot().get("verify_pending") is True,
    "snap=%r" % (_w1.snapshot(),),
)
_d1 = _w1.directive()
case(
    "核对：改了还没核对 → 注入核对指令",
    _d1 is not None and "核对指令" in _d1 and "pytest -q" in _d1,
    repr(_d1)[:120],
)
case(
    "核对：催过一次不连发",
    _w1.directive() is None,
    "连发了",
)
_w2 = cv.Converge(verify_info=_VINFO)
_w2.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_w2.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
case(
    "核对：跑过验证命令 → 待核对清零",
    _w2.verify_pending is False and _w2.snapshot().get("verify_pending") is False,
    "snap=%r" % (_w2.snapshot(),),
)
_w3 = cv.Converge()
_w3.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
case(
    "核对：探测不到验证命令就不催（不猜）",
    _w3.directive() is None,
    "不该催",
)
_w4 = cv.Converge(verify_info=_VINFO)
_w4.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_w4.observe("write_file", {"path": "b.py", "content": "y"}, ok=True)
case(
    "核对：再改一次会重新武装（第二次改动仍要核）",
    _w4.verify_pending is True and _w4.verify_nagged is False,
    "snap=%r" % (_w4.snapshot(),),
)
_w5 = cv.Converge(verify_info=_VINFO)
_w5.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_w5.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=1)
case(
    "核对：检查失败不算核对通过（仍待核对）",
    _w5.verify_pending is True,
    "snap=%r" % (_w5.snapshot(),),
)

# ── 14. 每次改动的核对结论（蒸馏自 aider 的 test_outcome）────────
_o1 = cv.Converge(verify_info=_VINFO)
_o1.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
case("结论：刚改完还没验 → 结论为空", _o1.snapshot().get("verify_outcome") == "",
     "snap=%r" % (_o1.snapshot(),))
_o2 = cv.Converge(verify_info=_VINFO)
_o2.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_o2.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
case("结论：核对通过 → pass", _o2.snapshot().get("verify_outcome") == "pass",
     "snap=%r" % (_o2.snapshot(),))
_o3 = cv.Converge(verify_info=_VINFO)
_o3.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_o3.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=1)
case("结论：核对失败 → fail（且仍待核对）",
     _o3.snapshot().get("verify_outcome") == "fail" and _o3.verify_pending is True,
     "snap=%r" % (_o3.snapshot(),))
_o4 = cv.Converge(verify_info=_VINFO)
_o4.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_o4.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=0)
_o4.observe("write_file", {"path": "b.py", "content": "y"}, ok=True)
case("结论：新改动让旧结论作废", _o4.snapshot().get("verify_outcome") == "",
     "snap=%r" % (_o4.snapshot(),))
_o5 = cv.Converge(verify_info=_VINFO)
_o5.observe("write_file", {"path": "a.py", "content": "x"}, ok=True)
_o5.observe("run_cmd", {"argv": ["pytest", "-q"]}, ok=True, rc=1)
_d5 = _o5.directive()
case(
    "结论：验没过时，核对指令说的是「没通过」而不是「还没验」",
    _d5 is not None and "没通过" in _d5,
    repr(_d5)[:120],
)

bad = [c for c in CASES if not c[1]]
for name, ok, detail in CASES:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))
print("\nCONVERGE_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
if bad:
    print("CONVERGE_SELFTEST_RED")
    sys.exit(1)
print("CONVERGE_SELFTEST_OK")
