#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli_gate 的反向门：证明 26 道判据有牙，不是「只会返回 0」。

为什么必须有这个门（真实缺口，不是预防性焦虑）：
  cli_gate.py 是「终端可用性」（Claude Code 对标任务的核心面）唯一一道门，
  接管前它的绿只有人手敲过，没有任何反向门能证明它判得对 —— 一个从没红过的
  门，它的绿只是「它会返回 0」。本门用 4 个变异体桩把每类判据各咬一次。

判据（双向，缺一条即 rc=2）：
  BASE   生产调用（零参数）必须 rc=0 且 pre_ok=True、判据数不缩水 ——
         有牙不等于只会红；PREFAIL 提前返回时 results 为空、nfail=0、rc 反而
         是 0，所以 rc=0 alone 不够，必须同时验 pre_ok 与判据条数。
         条数下限 26 写的是「接管时已有 26 条」：判据只会随面增长
         （2026-10-10 并行员工已把它扩到 35 条），缩水才是退化的信号。
  M1-M4  每个变异体必须 rc=2 **且指定的判据真的红了**，同时**不该红的没红**。
         只断言 rc!=0 是不够的：量具自己崩掉也给非 0，那是量具死了不是判据红。

M1 桩的构造要点（踩过一次，留档）：桩必须先过 cli_gate 的 PREFAIL ——
  `-V` 输出里带「AgentOS <版本号>」、`health` 打印 "health OK" 且 rc=0。
  否则 body() 在第一段就 return，判据一条都不跑，反向门拿空 results 会**假绿**。

顺带修掉的量具 bug（2026-10-10，本门 M2 就是为它而生）：
  cli_gate.py 的 tty-q-exits-rc0 曾写成 `rc == 0`，而 rc 是上一段 open 判据
  的退出码 —— TUI 挂住不退时这条照样绿。已改用 pty_cli 真返回的 rcq。

零污染：桩落在 tempfile.mkdtemp()；变异体的判据 JSON 走 cli_gate.py 的
  --out 缝写到 work/cli_gate_mut_<名>.json，不覆盖生产 work/cli_gate.json。

用法: python3 tools/cli_gate_selftest.py   # rc=0 全过 / 2 有漏网
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GATE = os.path.join(HERE, "cli_gate.py")
WORK = os.path.join(ROOT, "work")
BASE_OUT = os.path.join(WORK, "cli_gate.json")

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name
          + (("  " + detail) if detail else ""))


# ── 四个变异体桩（落进临时目录的独立脚本，门自己不起别的进程） ──────────

STUB_SILENT0 = '''import sys
a = sys.argv[1:]
if a == ["-V"] or a == ["--version"]:
    print("AgentOS 9.9.9-silent0")
    raise SystemExit(0)
# 其它一律 rc=0 但什么都不打印：假 CLI 的典型形状。
raise SystemExit(0)
'''

# 2026-10-20：网页面下线后，STUB_HANGQ 原来的两个判据
# （TUI 首屏正常但 q 不退 / top-level -p 的自相矛盾记录）**被测对象没了**，
# 变异体无从下手 → 连同它们的判据一起下线。
# 留着一条永远红的断言不如说清「它随被测对象一起没了」。

STUB_WRITEOK = '''import sys
a = sys.argv[1:]
if a == ["-V"] or a == ["--version"]:
    print("AgentOS 9.9.9-writeok")
    raise SystemExit(0)
if a[:1] == ["gh"]:
    # 写向量也放行：gh 的写闸必须红。
    print("writeok executed: %r" % (a,))
    if a == ["gh", "whoami"]:
        print("sources: stub")
    raise SystemExit(0)
raise SystemExit(0)
'''

# 2026-10-20：STUB_LYINGJSON 证的是「top-level -p 的 headless 记录自相矛盾
# （ok=True 而 exit_code=1）」—— 顶层 -p 随网页驾驶舱一起下线，该变异体
# 连同它的判据一起下线。

# 变异体 -> (桩源, 必须红的判据, 必须绿的判据, 这一变体在证什么)
MUTANTS = [
    ("silent0", STUB_SILENT0,
     ["help-usage", "help-lists-all-subs", "completion-zsh",
      "completion-noarg-rc2", "gates-zeroarg-rc2", "gh-write-denied-rc2",
      "gh-unknown-sub-rc2", "noarg-nontty-rc2"],
     ["sessions-rc0", "sub-help-all-rc0", "src-tree-version"],
     "恒 rc=0 的假 CLI：宣称的终端面全是纸面时，判据必须集体亮红；"
     "而 sessions/sub-help 这几条结构上无法区分真假 CLI，保持绿才证明门不是见谁都红"),
    ("writeok", STUB_WRITEOK,
     ["gh-write-denied-rc2"],
     ["gh-whoami-rc0"],
     "写向量放行的 CLI：gh 的写闸必须红，合法只读命令必须仍是绿，"
     "证明红的不是 gh 存在，而是写向量没被拒"),
]


def run_gate(cmd=None, out=None, timeout=420):
    argv = [sys.executable, GATE]
    if cmd:
        argv += ["--cmd", cmd]
    if out:
        argv += ["--out", out]
    try:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, {}, ""
    data = {}
    if out and os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    return p.returncode, data, p.stdout.decode("utf-8", "replace")


def echo_summary(stdout):
    """把被检门自己那行「== CLI_GATE ... ==」原样打出来。

    反向门断言的是 JSON 里的逐条 ok，但那行总结是现场排查的第一眼证据 ——
    红的时候它直接说「33/35 判据通过」，省得回头重跑。
    """
    for ln in reversed(stdout.splitlines()):
        if ln.startswith("== CLI_GATE"):
            print("   [gate自报] %s" % ln.strip())
            return


def rows_of(data):
    return data.get("results") or []


def ask(tag, data, gname, want_ok):
    hit = [r for r in rows_of(data) if r["gate"] == gname]
    if not hit:
        gate("%s·%s 判据存在" % (tag, gname), False,
             "判据压根没跑（PREFAIL 提前返回？results=%d）" % len(rows_of(data)))
        return
    r = hit[0]
    gate("%s·%s %s" % (tag, gname, "必须绿" if want_ok else "必须红"),
         bool(r["ok"]) == want_ok,
         ("ok=%s" % r["ok"]) + ((" | " + r.get("detail", "")[:90]) if r.get("detail") else ""))


def main():
    t0 = time.time()
    os.makedirs(WORK, exist_ok=True)
    td = tempfile.mkdtemp(prefix="cli_gate_mut-")
    try:
        # BASE：生产调用（零参数），落点就是生产 work/cli_gate.json。
        # 必须显式传 out：run_gate 只在自己拿到 out 时才回读 JSON，
        # 不传就永远拿空 data，BASE 段会集体假绿（2026-10-10 踩过）。
        rc, data, sout = run_gate(out=BASE_OUT)
        echo_summary(sout)
        nrows = len(rows_of(data))
        gate("BASE 生产调用 rc=0", rc == 0, "rc=%d" % rc)
        gate("BASE pre_ok=True（判据真的跑了）", data.get("pre_ok") is True,
             "prefail=%r" % str(data.get("prefail"))[:80])
        gate("BASE 判据数不缩水（>=26）", nrows >= 26, "实到 %d" % nrows)
        gate("BASE JSON ok=True", data.get("ok") is True,
             "ok=%r" % data.get("ok"))
        # 2026-10-20：TUI 首屏/退出两条判据随网页面下线（被测对象没了），
        # 换成终端面的「无参数进 agent」这一条。
        ask("BASE", data, "tty-noarg-enters-agent", True)

        for name, src, want_red, want_green, why in MUTANTS:
            stub = os.path.join(td, name + ".py")
            with open(stub, "w", encoding="utf-8") as f:
                f.write(src)
            mut_out = os.path.join(WORK, "cli_gate_mut_%s.json" % name)
            print("== M %s ==" % name)
            mrc, mdata, msout = run_gate(cmd=stub, out=mut_out)
            echo_summary(msout)
            nred = sum(1 for r in rows_of(mdata) if not r["ok"])
            gate("M·%s 整门 rc=2（有红项）" % name, mrc == 2, "rc=%d" % mrc)
            gate("M·%s pre_ok=True（判据跑了）" % name,
                 mdata.get("pre_ok") is True, "results=%d" % len(rows_of(mdata)))
            for g in want_red:
                ask("M·%s" % name, mdata, g, False)
            for g in want_green:
                ask("M·%s" % name, mdata, g, True)
            print("   （%s）" % why)
            print("   （%s：红 %d 条）" % (name, nred))
    finally:
        shutil.rmtree(td, ignore_errors=True)

    nfail = sum(1 for r in results if not r["ok"])
    print("== CLI_GATE_SELFTEST %s  %d/%d 断言通过（1 BASE + %d 变异体）elapsed=%.1fs =="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail,
             len(results), len(MUTANTS), time.time() - t0))
    try:
        with open(os.path.join(WORK, "cli_gate_selftest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"tool": "cli_gate_selftest.py",
                       "ok": bool(nfail == 0), "elapsed": round(time.time() - t0, 2),
                       "mutants": [m[0] for m in MUTANTS],
                       "results": results}, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print("  [warn] 落盘失败 %r" % (e,))
    if nfail == 0:
        print("CLI_GATE_SELFTEST-OK %d/%d 断言" % (len(results), len(results)))
    return 0 if nfail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
