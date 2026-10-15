#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""晋升面门：引擎 promote 子命令的 fail-closed 与幂等性验收。

为什么单独一门：capability.py 的晋升是**唯一裁决者**，它 rc=0 才算真晋升。
但历史上这类"真门包一层"的接线有三种典型假绿：
  - 忘了接进 company.sh → 落到 usage 0，rc 被下游当成功读
  - 只信子进程 rc=0 → 空转退出 0 也算晋升（故 promote.py 另查 OK token + 事后复核）
  - 幂等没做 → 重试一次 history 就多一条，"晋升次数"变成点击次数

本门全部用例把账本重定向到 work/ 下的临时区（CD_CAPABILITY_MEMORY_DIR 缝隙），
**绝不碰生产账本**，并在收尾再核一次生产账本 sha256 未变。

产出 work/promo_gate.json，rc=0 全过 / 2 有红项。
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
COMPANY = os.path.expanduser("~/.codex/global-background/company-delivery")
COMPANY_SH = os.path.join(COMPANY, "company.sh")
PROD_LEDGER = os.path.join(COMPANY, "COMPANY_MEMORY", "capabilities", "ledger.jsonl")
OK_TOKEN = "PROMOTE-GATE-OK"

GATES = []


def gate(name):
    def deco(fn):
        GATES.append((name, fn))
        return fn
    return deco


def prod_hash():
    try:
        with open(PROD_LEDGER, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return "ABSENT"


def fresh_mem(tag):
    return tempfile.mkdtemp(prefix="promo_gate_%s_" % tag,
                            dir=os.path.join(ROOT, "work"))


def write_ledger(mem, records):
    d = os.path.join(mem, "capabilities")
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, "ledger.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def rec(cid, status, **kw):
    r = {"capability_id": cid, "status": status}
    r.update(kw)
    return r


VALID = dict(experiment={"hypothesis": "h", "bench": "36/36"},
             regression={"suite": "selflearn", "delta": 0},
             generalization={"holdout": "3 runs"},
             linked_failures=["gap-demo"])


def run_promote(args, mem):
    env = dict(os.environ)
    if mem:
        env["CD_CAPABILITY_MEMORY_DIR"] = mem
    p = subprocess.run(["bash", COMPANY_SH, "promote"] + args,
                       capture_output=True, text=True, timeout=180, env=env)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def read_ledger(mem):
    p = os.path.join(mem, "capabilities", "ledger.jsonl")
    out = {}
    try:
        with io.open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    r = json.loads(ln)
                    out[r["capability_id"]] = r
    except FileNotFoundError:
        pass
    return out


def snap(mem):
    p = os.path.join(mem, "capabilities", "ledger.jsonl")
    try:
        st = os.stat(p)
        with open(p, "rb") as f:
            b = f.read()
        return (st.st_mtime_ns, st.st_size, hashlib.sha256(b).hexdigest())
    except FileNotFoundError:
        return None


@gate("正例：VALIDATED+experiment+regression 真晋升 rc=0 且带 PROMOTED 令牌 + 事后复核")
def g_positive(ctx):
    mem = fresh_mem("pos")
    cid = "gate-pos"
    write_ledger(mem, [rec(cid, "VALIDATED", **VALID)])
    rc, out = run_promote(["--id", cid, "--commit", "deadbeef",
                           "--reason", "门内夹具"], mem)
    after = read_ledger(mem).get(cid, {})
    shutil.rmtree(mem, ignore_errors=True)
    tok = ("PROMOTED %s" % cid) in out
    ok = rc == 0 and tok and after.get("status") == "PROMOTED"
    return ok, "rc=%d token=%s 复核status=%s" % (rc, tok, after.get("status"))


@gate("幂等：重复晋升 → PROMOTE-HOLDS rc=0 且 history 不增长")
def g_idempotent(ctx):
    mem = fresh_mem("idem")
    cid = "gate-idem"
    write_ledger(mem, [rec(cid, "VALIDATED", **VALID)])
    run_promote(["--id", cid, "--commit", "aaa", "--reason", "第一次"], mem)
    before = read_ledger(mem)[cid]
    rc, out = run_promote(["--id", cid, "--commit", "aaa", "--reason", "第二次"], mem)
    after = read_ledger(mem)[cid]
    shutil.rmtree(mem, ignore_errors=True)
    h1 = len(before.get("history") or [])
    h2 = len(after.get("history") or [])
    tok = "PROMOTE-HOLDS %s" % cid in out
    ok = rc == 0 and tok and h1 == h2 == 1
    return ok, "rc=%d holds=%s history %d→%d" % (rc, tok, h1, h2)


@gate("负例·账本缺失 → rc=2（PROMOTE-FAIL，非 usage 假绿）")
def g_missing(ctx):
    mem = fresh_mem("miss")
    rc, out = run_promote(["--id", "x", "--commit", "c", "--reason", "r"], mem)
    shutil.rmtree(mem, ignore_errors=True)
    ok = rc == 2 and "PROMOTE-FAIL" in out and "ledger 缺失" in out
    return ok, "rc=%d out=%r" % (rc, out[:70])


@gate("负例·账本为空 → rc=2")
def g_empty(ctx):
    mem = fresh_mem("empty")
    write_ledger(mem, [])
    rc, out = run_promote([], mem)
    shutil.rmtree(mem, ignore_errors=True)
    ok = rc == 2 and "账本为空" in out
    return ok, "rc=%d out=%r" % (rc, out[:70])


@gate("负例·DISCOVERED 直升 PROMOTED → rc=2（真门 GATE_DENIED 冒泡）")
def g_discovered(ctx):
    mem = fresh_mem("disc")
    cid = "gate-disc"
    write_ledger(mem, [rec(cid, "DISCOVERED")])
    rc, out = run_promote(["--id", cid, "--commit", "c", "--reason", "r"], mem)
    after = read_ledger(mem).get(cid, {}).get("status")
    shutil.rmtree(mem, ignore_errors=True)
    den = "GATE_DENIED" in out
    ok = rc == 2 and den and after != "PROMOTED"
    return ok, "rc=%d denied=%s 事后status=%s" % (rc, den, after)


@gate("负例·缺 --commit/--reason → rc=2（star/口碑/单次通过都不算证据）")
def g_no_evidence(ctx):
    mem = fresh_mem("ev")
    cid = "gate-ev"
    write_ledger(mem, [rec(cid, "VALIDATED", **VALID)])
    rc, out = run_promote(["--id", cid], mem)
    after = read_ledger(mem).get(cid, {}).get("status")
    shutil.rmtree(mem, ignore_errors=True)
    ok = rc == 2 and after != "PROMOTED"
    return ok, "rc=%d 事后status=%s out=%r" % (rc, after, out[:50])


@gate("负例·--id 不存在 → rc=2 UNKNOWN_CAPABILITY")
def g_unknown(ctx):
    mem = fresh_mem("unk")
    write_ledger(mem, [rec("gate-real", "VALIDATED", **VALID)])
    rc, out = run_promote(["--id", "nope", "--commit", "c", "--reason", "r"], mem)
    shutil.rmtree(mem, ignore_errors=True)
    ok = rc == 2 and "UNKNOWN_CAPABILITY" in out
    return ok, "rc=%d out=%r" % (rc, out[:70])


@gate("只读面：report 零写账本（mtime+size+sha 三重不变）")
def g_readonly(ctx):
    mem = fresh_mem("ro")
    cid = "gate-ro"
    write_ledger(mem, [rec(cid, "VALIDATED", **VALID)])
    b = snap(mem)
    rc, out = run_promote([], mem)
    a = snap(mem)
    shutil.rmtree(mem, ignore_errors=True)
    tok = OK_TOKEN in out
    ok = rc == 0 and tok and a == b
    return ok, "rc=%d token=%s 账本不变=%s" % (rc, tok, a == b)


@gate("接线真值：production 报告 rc=0 且 OK token 在场（不是 usage 假绿）")
def g_prod_wired(ctx):
    rc, out = run_promote([], None)
    tok = OK_TOKEN in out
    head = out.splitlines()[0].strip() if out else ""
    return rc == 0 and tok, "rc=%d token=%s | %s" % (rc, tok, head[:58])


def main():
    os.makedirs(os.path.join(ROOT, "work"), exist_ok=True)
    before = prod_hash()
    report = {"tool": "promo_gate.py", "gates": [], "prod_ledger_sha_before": before}
    ctx = {}
    npass = 0
    for name, fn in GATES:
        try:
            ok, detail = fn(ctx)
        except Exception as e:
            ok, detail = False, "EXC %s: %s" % (type(e).__name__, e)
        report["gates"].append({"name": name, "ok": bool(ok), "detail": detail})
        print("  %s %s  %s" % ("[ok  ]" if ok else "[FAIL]", name, detail))
        npass += bool(ok)
    after = prod_hash()
    clean = before == after
    nm = "生产账本零污染：全程 sha256 未变"
    report["gates"].append({"name": nm, "ok": clean, "detail": "sha %s" % before[:16]})
    print("  %s %s  sha %s" % ("[ok  ]" if clean else "[FAIL]", nm, before[:16]))
    npass += bool(clean)
    total = len(report["gates"])
    report["passed"], report["total"] = npass, total
    with open(os.path.join(ROOT, "work", "promo_gate.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("== PROMO_GATE %s  %d/%d 门通过（账本全隔离夹具）==" % (
        "OK " if npass == total else "FAIL", npass, total))
    return 0 if npass == total else 2


if __name__ == "__main__":
    sys.exit(main())
