"""D59b: separate "supervised restart flap" from "legitimate restart window".

The settle budget answers one question: how long may a client wait for 8787 to
come back after a *single legitimate* supervised restart? That number was
ratcheted 30 -> 45 -> 60s as the worst observed lifecycle window grew.

What broke that ratchet on 2026-10-05: the worst lifecycle window jumped to
66.2s, but reading the timestamps showed it was not one restart at all. It was
five start/shutdown cycles inside 66s (21:23:16 -> 21:24:22), six distinct proxy
pids, every shutdown reason=signal, merged into one span because the proxy never
served a single 200 in between. The supervisor's own log says why:

    ERROR: [Errno 48] error while attempting to bind on address
           ('127.0.0.1', 8787): [errno 48] address already in use

265 such bind failures against 280 start attempts in
~/.headroom/deploy/<profile>/runner.log. The runner kills the serving proxy and
then races its own replacement into the same socket, so it flaps.

Why that must NOT be absorbed by ratcheting the budget: a flap is not a slow
restart, and waiting longer cannot cure it -- the loop has to be fixed. Raising
READY_SETTLE_S to 72s would make the gate green by widening its tolerance to a
third-party defect, which is the exact inverse of what a gate is for. D59
already set the precedent for this split, for network-egress windows: waiting
does not fix those either, so they must not inflate the budget.

So a flap is measured and reported by its own check (restart-loop-no-flap) and
excluded from the budget's worst-window figure. The judgement is reported,
never swallowed: if a flap is still happening inside the recent window, that
check goes red on its own merits.
"""

# A legitimate restart means one start mark inside the window. Observed windows
# with 3+ starts inside a single span are restart loops, not restarts.
FLAP_MIN_STARTS = 3

# A flap is a live defect, but a historical one is a fact, not a fault. The
# forward check looks at this trailing window so a flap that has since stopped
# does not keep the gate red forever, while a flap that is still happening does.
FLAP_RECENT_S = 1800.0


def span_bounds(events, marks, gap_cap=10.0, adjacent=15.0):
    """Mirror of headroom_gate.unready_spans, but keeping (t0, t1).

    Duplicated rather than refactored in place on purpose: unready_spans is
    locked down by mutants M35/M36/M37 that assert its exact classification,
    and its callers want bare (duration, is_lifecycle) pairs. Re-deriving the
    same loop here keeps that contract untouched while exposing the timestamps
    the budget analysis needs.

    Lifecycle semantics (D59, refined 2026-10-09 against a real all-day
    egress outage): a span is a settle window only when the unready state
    BEGAN at a restart -- (1) a start mark within `adjacent` of the span's
    first 503 (the docstring's "adjacent to a start mark"; a mark sitting in
    the middle of a long span anchored nothing), and (2) the span's beginning
    was observed: the event before t0 is within gap_cap. A span that starts
    after an unprobed gap began at an unknown time -- the egress failure it
    is suffering may well have started during the gap, so pinning it on a
    restart mark that only appears after probing resumed would be a guess.
    Both conditions keep every mutant's fixture verdict: M35's mark sits 3s
    after a first-event t0 (observed vacuously), M36/M37's marks are minutes
    away or absent.
    """
    cap = float(gap_cap)
    mk = list(marks)
    spans, i, n = [], 0, len(events)
    while i < n:
        if not events[i][1]:
            i += 1
            continue
        t0 = events[i][0]
        j = i
        while j + 1 < n and events[j + 1][1]:
            j += 1
        gap = min(cap, (events[j + 1][0] - events[j][0]) if j + 1 < n else cap)
        t1 = events[j][0] + gap
        # (2) beginning observed: first event of the merged list counts as
        # observed (conservative -- counts against the budget, never exempts);
        # anything after an unprobed gap does not.
        observed = (i == 0) or (t0 - events[i - 1][0]) <= cap
        # (1) unready state began at the restart: start mark adjacent to t0.
        life = observed and any(
            t0 - float(adjacent) <= m[0] <= t0 + float(adjacent)
            for m in mk if m[1] == "start")
        spans.append({"dur": max(0.0, t1 - t0), "life": life,
                      "t0": t0, "t1": t1})
        i = j + 1
    return spans


def starts_in(span, marks, adjacent=15.0):
    """How many start marks fall inside this span (tolerance applied)."""
    return len([m for m in marks
                if m[1] == "start"
                and span["t0"] <= m[0] <= span["t1"] + float(adjacent)])


def is_flap(span, marks, adjacent=15.0, min_starts=FLAP_MIN_STARTS):
    """A lifecycle span with repeated restart attempts = restart loop."""
    return (bool(span.get("life"))
            and starts_in(span, marks, adjacent) >= int(min_starts))


def split_spans(spans, marks, adjacent=15.0, min_starts=FLAP_MIN_STARTS):
    """-> (life_kept, flaps, other). Flaps are reported, never silently dropped."""
    life_kept, flaps, other = [], [], []
    for s in spans:
        if is_flap(s, marks, adjacent, min_starts):
            flaps.append(s)
        elif s.get("life"):
            life_kept.append(s)
        else:
            other.append(s)
    return life_kept, flaps, other


def worst_settle_window(events, marks, gap_cap=10.0, adjacent=15.0,
                        min_starts=FLAP_MIN_STARTS):
    """Worst *legitimate* restart window, in seconds. Flaps excluded by design."""
    spans = span_bounds(events, marks, gap_cap, adjacent)
    life, flaps, other = split_spans(spans, marks, adjacent, min_starts)
    return {
        "worst": max([s["dur"] for s in life], default=0.0),
        "worst_flap": max([s["dur"] for s in flaps], default=0.0),
        "n_life": len(life),
        "n_flap": len(flaps),
        "n_other": len(other),
        "worst_other": max([s["dur"] for s in other], default=0.0),
        "spans": spans,
        "flaps": flaps,
    }


def recent_flaps(flaps, now, recent_s=FLAP_RECENT_S):
    """Flap windows still inside the trailing window = still flapping."""
    cutoff = float(now) - float(recent_s)
    return [s for s in flaps if s["t0"] >= cutoff]
