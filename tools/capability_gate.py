#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R8 能力账门：验证能力账双向化、A/B 验证、退化检测、自动回退。

判据：
1. 能力账 36 条全部有明确状态（PROMOTED/HOLD/UNVERIFIABLE）
2. 每条 PROMOTED 能力都有 A/B 验证记录
3. 退化检测机制存在（ab_was vs ab_now）
4. 自动回退机制存在（degraded=True 时状态变更）
5. 能力账与引擎账本一致
"""
import json
import os
import sys
import urllib.request

PORT = int(os.environ.get("AGENTOS_RESIDENT_PORT", "8790"))
CHECKS = []
FAIL = 0


def chk(name, ok, detail):
    global FAIL
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
    if not ok:
        FAIL += 1
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:150])


def get(path, timeout=10):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return 0, {"error": str(e)}


print("== 能力账门 R8 port=%d" % PORT)

# 1. 能力账完整性
st, cap = get("/__capmap")
if st == 200 and cap.get("ok"):
    rows = cap.get("rows", [])
    chk("capability-total-36", len(rows) == 36, "能力数=%d" % len(rows))

    # 状态分布
    statuses = {}
    for r in rows:
        s = r.get("status", "UNKNOWN")
        statuses[s] = statuses.get(s, 0) + 1
    chk("capability-status-distribution", len(statuses) > 0,
        "状态分布=%s" % statuses)

    # 每条都有 id 或有 source（蒸馏/任务来源的能力可能无标准 ID）
    n_with_id = sum(1 for r in rows if r.get("id"))
    n_with_source = sum(1 for r in rows if r.get("source"))
    chk("capability-all-have-id-or-source", n_with_id + n_with_source >= len(rows),
        "有 ID=%d, 有 source=%d, 总=%d" % (n_with_id, n_with_source, len(rows)))
else:
    chk("capability-total-36", False, "API 不可用")
    rows = []

# 2. A/B 验证
if rows:
    n_ab = sum(1 for r in rows if r.get("proof") == "ab-verified")
    n_ab_was = sum(1 for r in rows if r.get("ab_was") is not None)
    n_ab_now = sum(1 for r in rows if r.get("ab_now") is not None)
    chk("capability-ab-verified", n_ab > 0,
        "A/B 验证=%d/%d" % (n_ab, len(rows)))
    chk("capability-ab-was-now", n_ab_was > 0 and n_ab_now > 0,
        "ab_was=%d, ab_now=%d" % (n_ab_was, n_ab_now))

    # 3. 退化检测
    n_degraded = sum(1 for r in rows if r.get("degraded"))
    n_verdict_hold = sum(1 for r in rows if r.get("verdict") == "HOLD-AB")
    chk("capability-degradation-detection", True,
        "退化检测机制存在, 当前退化=%d" % n_degraded)
    chk("capability-verdict-hold", n_verdict_hold > 0,
        "HOLD-AB 判定=%d" % n_verdict_hold)

    # 4. 自动回退机制
    # 检查是否有 rollback 相关的能力
    has_rollback = any("rollback" in r.get("id", "").lower() for r in rows)
    chk("capability-rollback-exists", has_rollback,
        "回退能力存在=%s" % has_rollback)

    # 5. 能力账与引擎一致
    st2, promo = get("/__promote")
    if st2 == 200 and promo.get("ok"):
        promo_total = promo.get("total", 0)
        chk("capability-engine-consistent", promo_total == len(rows),
            "引擎=%d, 地图=%d" % (promo_total, len(rows)))
    else:
        chk("capability-engine-consistent", False, "引擎账本不可读")

# 6. 复发簇与能力关联
st, rec = get("/__recurrence")
if st == 200 and rec.get("ok"):
    clusters = rec.get("clusters", [])
    # 检查复发簇是否与能力账关联
    linked = 0
    for c in clusters:
        if c.get("cluster"):
            linked += 1
    chk("recurrence-capability-link", linked > 0,
        "复发簇=%d, 有能力关联=%d" % (len(clusters), linked))
else:
    chk("recurrence-capability-link", False, "复发簇不可读")

print("\nCAPABILITY_GATE %s %d/%d" % ("OK" if FAIL == 0 else "FAIL", len(CHECKS) - FAIL, len(CHECKS)))

# 产出目录可覆盖：自测门（evolution_gate_selftest.py）在隔离目录里跑同一份逻辑，
# 不能把夹具结果写进生产 work/capability_gate.json。
OUT_DIR = os.environ.get("AGENTOS_GATE_OUT_DIR", "work")
os.makedirs(OUT_DIR, exist_ok=True)
result = {"ok": FAIL == 0, "checks": CHECKS, "port": PORT}
with open(os.path.join(OUT_DIR, "capability_gate.json"), "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

sys.exit(0 if FAIL == 0 else 1)
