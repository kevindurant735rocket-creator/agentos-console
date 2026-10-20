"""Selftest for tools/ctx_compact.py -- exit 0 only if every contract holds."""

from __future__ import annotations

import json
import sys

from ctx_compact import compact, estimate_tokens, plan

fails: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{name}: {detail}")


# 1 estimate is conservative and monotone
ck("est_empty", estimate_tokens("") == 0)
ck("est_cjk_ge_chars_ratio",
   estimate_tokens("中" * 100) >= 100,
   f"got {estimate_tokens('中' * 100)}")
a = estimate_tokens("x" * 400)
ck("est_latin_ratio", 90 <= a <= 130, f"got {a}")
ck("est_monotone", estimate_tokens("x" * 800) > a)

# 2 under budget -> nothing dropped
small = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
p = plan(small, 100_000)
ck("under_budget_fits", p.fits and not p.dropped and p.reason == "under_budget",
   p.reason)

# 3 over budget -> ask survives, tail contiguous, tokens shrink
msgs = [{"role": "system", "content": "sys"}]
msgs += [{"role": "user", "content": "THE ASK: 蒸馏上下文压缩"}]
msgs += [{"role": "assistant", "content": "blob " * 400} for _ in range(30)]
p = plan(msgs, 6000, reply_reserve=1000)
ck("over_budget_no_fit_raw", p.used_before > 0)
ck("over_budget_compacts", p.fits and bool(p.dropped), p.reason)
ck("ask_kept", 1 in p.keep_idx, f"keep={p.keep_idx[:5]}...")
ck("tokens_shrink", p.used_after < p.used_before, f"{p.used_before}->{p.used_after}")
tail = [i for i in p.keep_idx if i >= 2]
ck("tail_contiguous", tail == list(range(min(tail), max(tail) + 1)) if tail else True,
   f"tail={tail}")

# 4 materialised transcript keeps the ask and announces the elision
out = compact(msgs, p)
ck("compact_shorter", len(out) < len(msgs), f"{len(msgs)}->{len(out)}")
ck("compact_keeps_ask",
   any(m.get("content", "").startswith("THE ASK") for m in out))
ck("compact_announces",
   any("elided" in (m.get("content") or "") for m in out if m.get("role") == "system"))

# 5 R3: absurd-tight budget refuses instead of lying
p2 = plan(msgs, 3000, reply_reserve=2900)
ck("floor_refuses", (not p2.fits) and p2.reason.startswith("floor_exceeded"),
   p2.reason)
ck("floor_leaves_transcript",
   compact(msgs, p2) == list(msgs))

# 6 purity: plan never mutates input
snapshot = json.dumps(msgs, sort_keys=True, ensure_ascii=False)
plan(msgs, 6000)
ck("plan_pure", snapshot == json.dumps(msgs, sort_keys=True, ensure_ascii=False))

if fails:
    print("FAIL " + json.dumps(fails, ensure_ascii=False, indent=2))
    sys.exit(1)
print("OK ctx_compact selftest")
