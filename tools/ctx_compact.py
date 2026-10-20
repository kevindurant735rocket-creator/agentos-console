"""Context budget + transcript compaction, distilled from upstream agent CLIs.

References distilled (design only, no code copied):
  * anthropics/claude-code -- two-tier budget: a hard context wall plus a
    "compact before you hit it" reserve, so the agent never dies mid-turn.
  * All-Hands-AI/OpenHands -- condensation = keep head (system + first user
    intent), keep tail (most recent turns verbatim), replace the middle with a
    model-written summary. Privacy: never summarise away the user's *ask*.
  * SWE-agent -- token estimate is deliberately cheap (chars/ratio), because
    the estimate only needs to be conservative, not exact; the real count is
    only requested from the provider when the estimate is near the wall.

Why this module exists in this repo
-----------------------------------
`tools/settle_budget.py` taught the lesson that a budget which absorbs a
defect is worse than no budget at all: it was ratcheted 30->45->60s to hide a
restart flap. The same trap exists for context: an agent that "just keeps
everything" turns a token wall into a silent, unrecoverable stop. So this
module reports *why* it compacted, and refuses to compact below a floor that
would throw away the user's ask.

Contracts (all deterministic, no network, no model calls):
  estimate_tokens(text) -> int            conservative, cheap
  plan(msgs, budget)    -> Plan           pure function of the transcript
  Plan.fits             -> bool
  Plan.dropped          -> list of (index, role, tokens) that got cut
  Plan.reason           -> machine-readable string, never empty

Hard rules:
  R1 the first user message (the ask) is never dropped;
  R2 the tail is kept contiguously -- no holes in the most recent turns;
  R3 if R1+R2 alone exceed the budget, we report refusal (reason=floor_exceeded)
     and leave the transcript untouched rather than lie about fitting.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Chars-per-token ratios. CJK is ~1 char/token, latin prose ~4, code ~3.5.
# Take the *most expensive* ratio seen so the estimate is conservative.
_RATIO_LATIN = 4.0
_RATIO_CODE = 3.5
_RATIO_CJK = 1.0

# Reserve kept free for the model's own reply + tool results of the next turn.
DEFAULT_REPLY_RESERVE = 4096


def estimate_tokens(text: str) -> int:
    """Conservative token estimate. Cheap on purpose: O(n) byte scan."""
    if not text:
        return 0
    cjk = 0
    codeish = 0
    for ch in text:
        o = ord(ch)
        if o >= 0x2E80:  # CJK + fullwidth forms
            cjk += 1
        elif ch in "{}[]()<>;_=+*/\\|#":
            codeish += 1
    n = len(text)
    latin = max(0, n - cjk - codeish)
    return int(cjk * (1.0 / _RATIO_CJK)
               + codeish * (1.0 / _RATIO_CODE)
               + latin * (1.0 / _RATIO_LATIN)) + 1


def _content(msg: dict[str, Any]) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(str(p.get("text", "")) if isinstance(p, dict) else str(p)
                         for p in c)
    return "" if c is None else str(c)


def msg_tokens(msg: dict[str, Any]) -> int:
    # per-message framing overhead, same trick as upstream chat templates
    return estimate_tokens(_content(msg)) + 4


@dataclass
class Plan:
    fits: bool
    budget: int
    used_before: int
    used_after: int
    keep_idx: list[int] = field(default_factory=list)
    dropped: list[tuple[int, str, int]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fits": self.fits,
            "budget": self.budget,
            "used_before": self.used_before,
            "used_after": self.used_after,
            "keep_idx": self.keep_idx,
            "dropped": [{"idx": i, "role": r, "tokens": t} for i, r, t in self.dropped],
            "reason": self.reason,
        }


def _find_ask(msgs: Sequence[dict[str, Any]]) -> int | None:
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            return i
    return None


def plan(
    msgs: Sequence[dict[str, Any]],
    budget: int,
    reply_reserve: int = DEFAULT_REPLY_RESERVE,
    min_tail: int = 4,
) -> Plan:
    """Decide which messages survive. Pure: never mutates `msgs`."""
    if budget <= 0:
        raise ValueError("budget must be > 0")
    effective = budget - max(0, reply_reserve)
    toks = [msg_tokens(m) for m in msgs]
    before = sum(toks)

    if before <= effective:
        return Plan(True, budget, before, before, list(range(len(msgs))), [],
                    "under_budget")

    ask = _find_ask(msgs)
    must = set()
    if ask is not None:
        must.add(ask)

    # grow the tail backwards until it no longer fits, then stop (R2)
    keep = set(must)
    tail: list[int] = []
    running = sum(toks[i] for i in must)
    for i in range(len(msgs) - 1, -1, -1):
        if i in keep:
            continue
        if running + toks[i] > effective:
            break
        keep.add(i)
        tail.append(i)
        running += toks[i]

    floor = sum(toks[i] for i in keep)
    if floor > effective:
        # R3: refuse rather than pretend. Compacting down to just the ask would
        # "fit" arithmetically while leaving the model with no recent context,
        # which is the same lie as a silently truncated transcript.
        return Plan(False, budget, before, before, list(range(len(msgs))), [],
                    f"floor_exceeded:ask({floor})>effective({effective})")

    # R2 is a hard floor, not advice: fewer than min_tail recent turns kept
    # means we cannot honestly call the result a compacted *conversation*.
    want_tail = min(min_tail, len(msgs) - len(must))
    if len(tail) < want_tail:
        return Plan(False, budget, before, before, list(range(len(msgs))), [],
                    f"floor_exceeded:tail({len(tail)})<min_tail({want_tail})"
                    f";effective({effective})")

    dropped = [(i, msgs[i].get("role", "?"), toks[i])
               for i in range(len(msgs)) if i not in keep]
    dropped.sort(key=lambda x: -x[2])
    after = sum(toks[i] for i in keep)
    start = max(tail) if tail else len(msgs)
    reason = (f"over_budget:{before}>{effective};"
              f"kept_ask={ask};tail_from={start};n_tail={len(tail)}")
    return Plan(True, budget, before, after, sorted(keep), dropped, reason)


def compact(msgs: list[dict[str, Any]], p: Plan,
            summary: str | None = None) -> list[dict[str, Any]]:
    """Materialise a plan into a new transcript. Additive, never destructive.

    The hole left by dropped turns is filled with one synthetic message so the
    model is told *that* history was elided (silently vanishing history is how
    agents start hallucinating continuity).
    """
    if not p.fits or not p.dropped:
        return list(msgs)
    head = [msgs[i] for i in p.keep_idx if not _is_tail_region(msgs, p, i)]
    tail = [msgs[i] for i in sorted(p.keep_idx) if _is_tail_region(msgs, p, i)]
    if summary is None:
        n = len(p.dropped)
        tok = sum(t for _, _, t in p.dropped)
        summary = (f"[elided {n} earlier messages, ~{tok} tokens; "
                   f"original ask retained]")
    return head + [{"role": "system", "content": summary}] + tail


def _is_tail_region(msgs: Sequence[dict[str, Any]], p: Plan, i: int) -> bool:
    ask = _find_ask(msgs)
    return i != ask and i > (ask if ask is not None else -1)


def main(argv: Iterable[str]) -> int:
    args = list(argv)[1:]
    budget = int(args[0]) if args else 8000
    n = int(args[1]) if len(args) > 1 else 40
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "you are agentos, be honest."},
        {"role": "user", "content": "全面部署github的高星项目来提升你自己的能力"},
    ]
    for i in range(n):
        msgs.append({"role": "assistant" if i % 2 else "user",
                     "content": f"step {i}: " + "读源码并蒸馏出一处改动." * 20})
    p = plan(msgs, budget)
    print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))
    out = compact(msgs, p)
    print(f"transcript {len(msgs)} -> {len(out)} messages", file=sys.stderr)
    return 0 if (p.fits or p.reason.startswith("floor_exceeded")) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
