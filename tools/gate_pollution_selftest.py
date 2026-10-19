#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_pollution_selftest —— 零污染共享口径的证伪集。

这道口径 2026-10-20 在一次全链复验里被同时踩了四道门
（repl/branches/supervise/safety 全红在同一句「work/ 多出
_st_agent_core.<pid>」）。所以它必须有自证：并发的不算污染、
真残留一定要算。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gate_pollution as gp  # noqa: E402

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    return bool(ok)


# 1) 活着的进程（自己）→ 算并发，不算污染
_self = "_st_agent_core.%d" % os.getpid()
poll, live = gp.pollution_diff({_self, "other.txt"}, set(), allowed=())
case(
    "并发：活着的自检目录不算污染",
    _self not in poll and _self in live,
    "poll=%r live=%r" % (poll, live),
)

# 2) 死掉的 pid → 真残留，要算污染
_dead = "_st_agent_core.%d" % (os.getpid() + 1000000)
poll2, live2 = gp.pollution_diff({_dead}, set(), allowed=())
case(
    "残留：pid 已死的自检目录算污染",
    _dead in poll2 and _dead not in live2,
    "poll=%r live=%r" % (poll2, live2),
)

# 3) 其它新增项一律算污染
poll3, _l3 = gp.pollution_diff({"whatever.json"}, set(), allowed=())
case("其它新增项算污染", "whatever.json" in poll3, "poll=%r" % (poll3,))

# 4) allowed 里的（--out）不算
poll4, _l4 = gp.pollution_diff({"out.json"}, set(), allowed=("out.json",))
case("allowed 里的不算污染", not poll4, "poll=%r" % (poll4,))

# 5) before 里有、after 里没有的（被清掉的）不算新增
poll5, _l5 = gp.pollution_diff(set(), {"gone.json"}, allowed=())
case("被清掉的条目不算污染", not poll5, "poll=%r" % (poll5,))

# 6) 非自检目录不会被误排除
poll6, live6 = gp.pollution_diff({"_st_other.123"}, set(), allowed=())
case("非自检目录不会被误排除", "_st_other.123" in poll6, "poll=%r" % (poll6,))

# 7) live_selftest_dirs 对垃圾输入不炸
case(
    "健壮：垃圾输入不炸",
    gp.live_selftest_dirs(["", None, "x", "_st_agent_core.abc"]) == set(),
    "",
)

bad = [c for c in CASES if not c[1]]
for name, ok, detail in CASES:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))
print("\nGATE_POLLUTION_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
if bad:
    print("GATE_POLLUTION_SELFTEST_RED")
    sys.exit(1)
print("GATE_POLLUTION_SELFTEST_OK")
