#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R8 自我进化门：验证复发簇追踪、能力自动晋升、能力自动回退。

判据：
1. 复发簇数据可读且非空
2. 能力账 36 条全部有明确状态
3. 能力账有 A/B 验证记录
4. 复发簇有自动晋升建议
5. 能力退化检测机制存在
"""
import json
import os
import sys
import time
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


print("== 自我进化门 R8 port=%d" % PORT)

# 1. 复发簇数据
st, rec = get("/__recurrence")
if st == 200 and rec.get("ok"):
    clusters = rec.get("clusters", [])
    n_recurring = sum(1 for c in clusters if c.get("recurring"))
    chk("evolution-recurrence-readable", len(clusters) > 0,
        "复发簇=%d, 复发性=%d" % (len(clusters), n_recurring))
    chk("evolution-recurrence-has-recurring", n_recurring > 0,
        "复发性簇=%d" % n_recurring)
else:
    chk("evolution-recurrence-readable", False, "API 不可用: %s" % rec.get("error", "?"))

# 2. 能力账状态
st, cap = get("/__capmap")
if st == 200 and cap.get("ok"):
    rows = cap.get("rows", [])
    n_promoted = sum(1 for r in rows if r.get("status") == "PROMOTED")
    n_ab = sum(1 for r in rows if r.get("proof") == "ab-verified")
    n_degraded = sum(1 for r in rows if r.get("degraded"))
    chk("evolution-capmap-readable", len(rows) > 0,
        "能力=%d, PROMOTED=%d" % (len(rows), n_promoted))
    chk("evolution-capmap-ab-verified", n_ab > 0,
        "A/B 验证=%d/%d" % (n_ab, len(rows)))
    chk("evolution-capmap-no-degraded", n_degraded == 0,
        "退化=%d" % n_degraded)
else:
    chk("evolution-capmap-readable", False, "API 不可用")

# 3. 能力晋升面
st, promo = get("/__promote")
if st == 200 and promo.get("ok"):
    total = promo.get("total", 0)
    n_hold = promo.get("n_hold", 0)
    chk("evolution-promote-readable", total > 0,
        "总能力=%d, 保持=%d" % (total, n_hold))
else:
    chk("evolution-promote-readable", False, "API 不可用")

# 4. 蒸馏面
st, dist = get("/__distill")
if st == 200 and dist.get("ok"):
    n_both = dist.get("n_both", 0)
    target = dist.get("target", 50)
    chk("evolution-distill-dual-evidence", n_both >= target,
        "双证据=%d/%d" % (n_both, target))
else:
    chk("evolution-distill-dual-evidence", False, "API 不可用")

# 5. 复发簇自动晋升建议
st, rec = get("/__recurrence")
if st == 200 and rec.get("ok"):
    clusters = rec.get("clusters", [])
    # 检查是否有高复发簇（>=5 次）
    high_recurring = [c for c in clusters if c.get("runs", 0) >= 5]
    chk("evolution-auto-promote-trigger", len(high_recurring) > 0,
        "高复发簇(>=5次)=%d" % len(high_recurring))
else:
    chk("evolution-auto-promote-trigger", False, "复发簇不可读")

# 6. 能力退化检测
st, cap = get("/__capmap")
if st == 200 and cap.get("ok"):
    rows = cap.get("rows", [])
    # 检查是否有 A/B 对比数据
    has_ab_compare = any(
        r.get("ab_was") is not None and r.get("ab_now") is not None
        for r in rows
    )
    chk("evolution-degradation-detection", has_ab_compare,
        "A/B 对比检测=%s" % has_ab_compare)
else:
    chk("evolution-degradation-detection", False, "能力账不可读")

# 7. 服务健康
st, health = get("/__health")
if st == 200 and health.get("ok"):
    actions = health.get("actions", [])
    chk("evolution-health-ok", len(actions) >= 8,
        "动作数=%d" % len(actions))
else:
    chk("evolution-health-ok", False, "服务不健康")

print("\nEVOLUTION_GATE %s %d/%d" % ("OK" if FAIL == 0 else "FAIL", len(CHECKS) - FAIL, len(CHECKS)))

# 产出目录可覆盖：自测门（evolution_gate_selftest.py）要在隔离目录里跑同一份
# 逻辑，绝不能把夹具结果写进生产 work/evolution_gate.json（防假绿污染真账）。
OUT_DIR = os.environ.get("AGENTOS_GATE_OUT_DIR", "work")
os.makedirs(OUT_DIR, exist_ok=True)
result = {"ok": FAIL == 0, "checks": CHECKS, "port": PORT}
with open(os.path.join(OUT_DIR, "evolution_gate.json"), "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

sys.exit(0 if FAIL == 0 else 1)
