#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自我进化门的「关于门的那道门」：证明 evolution_gate / capability_gate
**判得红也判得绿**，而不只是「它们会返回 0」。

为什么必须有这一道（2026-10-10）：
  `tools/evolution_gate.py` 与 `tools/capability_gate.py` 在此之前只有正向
  判据 —— 对着活的 8790 服务读 /__recurrence /__capmap /__promote /__distill
  /__health，全 PASS 就 rc=0。它们没有变异体，所以「门有没有牙」这件事**从
  来没有任何物证**。按本仓 doctrine，这正是不许裸加进链的形态：量具没量到
  东西也可能照样判绿。

做法（纯 stdlib、零浏览器、零真端口争用）：
  起一个**夹具 HTTP 服务**在 127.0.0.1 的临时空闲端口上，按五个端点的真
  schema 返回一份自洽的绿夹具；再用 `AGENTOS_RESIDENT_PORT` 把两道门指过去。
    · BASE   ：夹具原样 → 两道门都必须 rc=0 且 stdout 零 FAIL 行。
    · 变异体 ：每个变异体只改夹具的一处 → 点名的那条判据必须变 FAIL 且 rc≠0。
  产出目录用 `AGENTOS_GATE_OUT_DIR` 指到隔离目录，跑完双自查：生产
  `work/evolution_gate.json` / `work/capability_gate.json` 字节不变。
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELFTEST_OUT = os.path.join(ROOT, "work", "evolution_gate_selftest.json")
ISOLATED_OUT = os.path.join(ROOT, "work", "_evogate_selftest_out")

CASES = []
FAIL = 0


def case(name, ok, detail):
    global FAIL
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    if not ok:
        FAIL += 1
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


# ── 夹具：五端点的真 schema（字段名逐个对着活服务核过）────────────────
def build_fixture():
    rows = []
    for i in range(36):
        rows.append({
            "id": "rollback-safety" if i == 0 else "cap-%02d" % i,
            "source": "stage2",
            "status": "PROMOTED" if i < 26 else "DISCOVERED",
            "proof": "ab-verified" if i < 20 else "none",
            "ab_was": 4 if i < 20 else None,
            "ab_now": 4 if i < 20 else None,
            "degraded": False,
            "verdict": "HOLD-AB" if i < 20 else "",
        })
    runs = [21, 11, 7, 6, 5, 3, 2, 1]
    clusters = [{
        "cluster": "V_fixture_%d" % i,
        "family": "UNKNOWN_SEMANTIC",
        "runs": r,
        "events": r + 1,
        "last_ts": "2026-10-10 00:00",
        "plan": "fixture",
        "recurring": i < 7,
        "ratio": float(r) / 3.0,
    } for i, r in enumerate(runs)]
    return {
        "/__recurrence": {
            "ok": True, "error": None, "threshold": 3, "n_events": 56,
            "n_clusters": len(clusters), "n_recurring": 7, "clusters": clusters,
        },
        "/__capmap": {
            "ok": True, "error": None, "total": len(rows), "rows": rows,
            "n_ledger": len(rows), "n_verified": 20, "n_unverifiable": 0,
            "n_no_verdict": 16, "n_anonymous": 0, "n_degraded": 0,
        },
        "/__promote": {
            "ok": True, "error": None, "total": len(rows), "n_ready": 0,
            "n_hold": 26, "rows": [], "age_s": 1, "cached": False, "stale": False,
        },
        "/__distill": {
            "ok": True, "error": None, "rows": [], "n_rows": 77, "n_both": 61,
            "target": 50, "verify_ok": True, "detail": "fixture",
        },
        "/__health": {
            "ok": True, "root": ROOT, "tree": "fixture", "index": "abc",
            "data": "def", "clients": 0, "stats": {}, "code_sha": "deadbeef",
            "actions": [{"name": "a%d" % i} for i in range(9)],
        },
    }


FIX = {}
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):        # 静音：门链日志里不要混 HTTP 访问行
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        with LOCK:
            payload = FIX.get(path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def reset_fixture(mutate=None):
    global FIX
    with LOCK:
        FIX = build_fixture()
        if mutate is not None:
            mutate(FIX)


def run_gate(script, port):
    env = dict(os.environ)
    env["AGENTOS_RESIDENT_PORT"] = str(port)
    env["AGENTOS_GATE_OUT_DIR"] = ISOLATED_OUT
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", script)],
                       cwd=ROOT, env=env, capture_output=True, timeout=120)
    out = (p.stdout or b"").decode("utf-8", "replace")
    return p.returncode, out


def fail_lines(out):
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL ")]


def criterion_red(out, crit):
    return any(ln.startswith("FAIL " + crit + " ") or ln == "FAIL " + crit
               for ln in fail_lines(out))


# ── 变异体定义：(用例名, 目标门, 目标判据, 改夹具的函数)─────────────────
def m_empty_recurrence(f):
    f["/__recurrence"]["clusters"] = []
    f["/__recurrence"]["n_clusters"] = 0
    f["/__recurrence"]["n_recurring"] = 0


def m_degraded_row(f):
    f["/__capmap"]["rows"][3]["degraded"] = True


def m_row_count(f):
    f["/__capmap"]["rows"] = f["/__capmap"]["rows"][:35]


def m_engine_mismatch(f):
    f["/__promote"]["total"] = 37


def m_distill_below_target(f):
    f["/__distill"]["n_both"] = 49


def m_health_actions(f):
    f["/__health"]["actions"] = f["/__health"]["actions"][:7]


def m_no_ab_verified(f):
    for r in f["/__capmap"]["rows"]:
        r["proof"] = "none"


def m_no_high_recurrence(f):
    for c in f["/__recurrence"]["clusters"]:
        c["runs"] = 2


def m_no_verdict_hold(f):
    for r in f["/__capmap"]["rows"]:
        r["verdict"] = ""


def m_no_rollback(f):
    for r in f["/__capmap"]["rows"]:
        r["id"] = (r["id"] or "").replace("rollback", "generic")


def m_service_dead(f):
    for k in list(f.keys()):
        f[k] = {"ok": False, "error": "fixture-dead"}


MUTANTS = [
    ("M01", "evolution_gate", "evolution-recurrence-readable", m_empty_recurrence),
    ("M02", "evolution_gate", "evolution-capmap-no-degraded", m_degraded_row),
    ("M03", "evolution_gate", "evolution-distill-dual-evidence", m_distill_below_target),
    ("M04", "evolution_gate", "evolution-auto-promote-trigger", m_no_high_recurrence),
    ("M05", "evolution_gate", "evolution-health-ok", m_health_actions),
    ("M06", "capability_gate", "capability-total-36", m_row_count),
    ("M07", "capability_gate", "capability-engine-consistent", m_engine_mismatch),
    ("M08", "capability_gate", "capability-ab-verified", m_no_ab_verified),
    ("M09", "capability_gate", "capability-verdict-hold", m_no_verdict_hold),
    ("M10", "capability_gate", "capability-rollback-exists", m_no_rollback),
    ("M11", "both", "service-dead", m_service_dead),
]


def main():
    print("== 自我进化门自测 evolution_gate_selftest")
    port = free_port()
    reset_fixture()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    prod = {}
    for p in (os.path.join(ROOT, "work", "evolution_gate.json"),
              os.path.join(ROOT, "work", "capability_gate.json")):
        try:
            with open(p, "rb") as fh:
                prod[p] = fh.read()
        except OSError:
            prod[p] = None

    os.makedirs(ISOLATED_OUT, exist_ok=True)

    try:
        # ── BASE：绿侧对照。夹具原样时两道门都必须绿且零 FAIL 行。
        reset_fixture()
        ev_rc, ev_out = run_gate("evolution_gate.py", port)
        cp_rc, cp_out = run_gate("capability_gate.py", port)
        case("BASE-evolution_gate-green",
             ev_rc == 0 and not fail_lines(ev_out),
             "rc=%d FAIL行=%d" % (ev_rc, len(fail_lines(ev_out))))
        case("BASE-capability_gate-green",
             cp_rc == 0 and not fail_lines(cp_out),
             "rc=%d FAIL行=%d" % (cp_rc, len(fail_lines(cp_out))))

        # ── 变异体：红侧。每例点名判据必须变 FAIL 且 rc≠0。
        for tag, which, crit, mutate in MUTANTS:
            reset_fixture(mutate)
            if which == "both":
                rcs, outs = [], []
                for script in ("evolution_gate.py", "capability_gate.py"):
                    r, o = run_gate(script, port)
                    rcs.append(r)
                    outs.append(o)
                ok = all(r != 0 for r in rcs)
                detail = "rc=%s" % rcs
            else:
                r, o = run_gate(which + ".py", port)
                ok = r != 0 and criterion_red(o, crit)
                detail = "rc=%d 命中%s=%s" % (r, crit, criterion_red(o, crit))
            case("%s-%s-%s" % (tag, which, crit), ok, detail)
    finally:
        srv.shutdown()
        srv.server_close()

    # ── 零污染双自查：隔离目录写了东西，生产两份 JSON 字节不变。
    dirty = []
    for p, before in prod.items():
        try:
            with open(p, "rb") as fh:
                after = fh.read()
        except OSError:
            after = None
        if after != before:
            dirty.append(os.path.basename(p))
    case("zero-pollution-prod-json-untouched", not dirty,
         "被改写=%s" % (dirty or "无"))
    isolated = sorted(os.listdir(ISOLATED_OUT)) if os.path.isdir(ISOLATED_OUT) else []
    case("isolated-out-dir-really-used",
         "evolution_gate.json" in isolated and "capability_gate.json" in isolated,
         "隔离目录=%s" % isolated)

    total = len(CASES)
    print("\nEVOLUTION_GATE_SELFTEST %s %d/%d" %
          ("OK" if FAIL == 0 else "FAIL", total - FAIL, total))
    with open(SELFTEST_OUT, "w", encoding="utf-8") as fh:
        json.dump({"ok": FAIL == 0, "cases": CASES, "port": port},
                  fh, ensure_ascii=False, indent=1)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
