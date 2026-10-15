#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""supervise_selftest —— 「关于 W8 监督门的那道门」：证明 supervise_gate 判得红也判得绿。

做法（纯 stdlib）：
  4 个变异体各改坏一处，双通道证伪：
    M2 注入被摘（core：步骤边界不取指令）→ 走 --core 通道 → D4 必红
    M4 /steer 命令不接线（core）→ 走 --core 通道 → D3 必红
    M1 游标不推进（steer.py）→ 纯面复核：push+commit 后 pending 必须为空
    M3 监督面丢任务行（watch.py）→ 纯面复核：render 必含任务行
  双自查：生产 work/supervise_gate.json 字节不变 + 临时目录清空（先清再断言）。

裁决：SUPERVISE_GATE_SELFTEST n/n + SUPERVISE_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(HERE, "agent_core.py")
STEER = os.path.join(HERE, "steer.py")
WATCH = os.path.join(HERE, "watch.py")
GATE = os.path.join(HERE, "supervise_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "supervise_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


# (说明, 目标文件, 锚点, 替换, 期望红判据 / 纯面复核)
MUTANTS = [
    ("M1 游标不推进（指令会重复进）", STEER,
     "        with open(c, \"w\", encoding=\"utf-8\") as f:\n"
     "            f.write(\"%d\\n\" % max(int(n), cur))",
     "        pass  # MUT 游标不推进",
     "PURE"),
    ("M2 注入被摘（步骤边界不取指令）", CORE,
     "        _stq = _steer_take(ctx)",
     "        _stq = []  # MUT 注入被摘",
     "D4"),
    ("M3 监督面丢任务行", WATCH,
     "            g = GLYPH.get(t.get(\"status\"), \"[·]\")\n"
     "            lines.append(\"  %s %-8s %s（证据 %d）\"\n"
     "                         % (g, t.get(\"id\") or \"?\","
     " t.get(\"title\") or \"\",\n"
     "                            t.get(\"evidence\") or 0))",
     "            pass  # MUT 丢任务行",
     "PURE"),
    ("M4 /steer 命令不接线", CORE,
     "    if cmd == \"/steer\":",
     "    if False:  # MUT /steer 被摘",
     "D3"),
]


def run_gate(core, out, timeout=900):
    return subprocess.run([sys.executable, GATE, "--core", core,
                           "--out", out],
                          capture_output=True, text=True, timeout=timeout)


def run_pure(target, mut_path):
    """模块级变异的纯面复核 → (caught, detail)。"""
    base = "sv_steer" if target == STEER else "sv_watch"
    spec = importlib.util.spec_from_file_location(base, mut_path)
    M = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(M)
    tmp2 = tempfile.mkdtemp(prefix="svpure_")
    try:
        if target == STEER:
            M.push(tmp2, "sA", "一")
            M.commit(tmp2, "sA", 1)
            pend = M.pending(tmp2, "sA")[0]
            return bool(pend), "pending=%d（正常应 0；有残留=变异被抓）" \
                % len(pend)
        wrep = M.render({"sid": "s", "ts": 1.0,
                         "plan": {"goal": "g", "approved": True},
                         "tasks": [{"id": "w1", "title": "t",
                                    "status": "done", "evidence": 1}],
                         "steer_n": 0, "steer_total": 0, "tail": []})
        return ("w1" not in wrep), "w1_missing=%s" % ("w1" not in wrep)
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


def main():
    print("SUPERVISE_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="svmut_")
    try:
        p0 = run_gate(CORE, os.path.join(tmp, "base.json"))
        case("BASE 真源全绿（SUPERVISE_GATE OK + rc=0）",
             p0.returncode == 0 and "SUPERVISE_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "SUPERVISE_GATE_OK" in p0.stdout))

        for name, target, anchor, repl, want in MUTANTS:
            src = open(target, encoding="utf-8").read()
            n = src.count(anchor)
            if n != 1:
                case(name, False,
                     "锚点命中 %d 处（须恰 1）——变异体无效" % n)
                continue
            mut = os.path.join(tmp, "mut_" + os.path.basename(target))
            with open(mut, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, repl, 1))
            if target == CORE:
                try:
                    r = run_gate(mut, os.path.join(tmp, "mut.json"))
                except subprocess.TimeoutExpired:
                    case(name, False, "门超时")
                    continue
                hit = ("FAIL %s" % want) in r.stdout
                case(name, r.returncode != 0 and hit,
                     "rc=%d 期望红「%s」→%s"
                     % (r.returncode, want, "命中" if hit else "未命中"))
            else:
                caught, det = run_pure(target, mut)
                case(name, caught, det)

        prod_after = b""
        if os.path.exists(PROD_JSON):
            with open(PROD_JSON, "rb") as f:
                prod_after = f.read()
        case("生产 work/supervise_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nSUPERVISE_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad),
                                               len(CASES)))
    print("SUPERVISE_GATE_SELFTEST_OK" if not bad
          else "SUPERVISE_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
