#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hooks_selftest —— 「关于 W4 门的那道门」：证明 hooks_gate 判得红也判得绿。

做法（纯 stdlib、夹具=本地 sh 钩子，自起自杀）：
  三个变异体各改坏一处，跑同一道 hooks_gate：
    M1 钩子崩溃放行（hooks_mw：超时/崩溃不再拦）→ D5 必红
    M2 description 超预算不截（skills_load）→ D7 必红
    M3 正文注入错位（agent_core /skill 注了 description）→ D8 必红
  双自查：生产 work/hooks_gate.json 字节不变 + 临时目录清空（先清再断言）。

裁决：HOOKS_GATE_SELFTEST n/n + HOOKS_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(HERE, "agent_core.py")
HOOKS = os.path.join(HERE, "hooks_mw.py")
SKILLS = os.path.join(HERE, "skills_load.py")
GATE = os.path.join(HERE, "hooks_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "hooks_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 目标文件, 锚点原文, 替换, 期望点名的判据前缀)
    ("M1 钩子崩溃放行（超时/崩溃不再拦）", HOOKS,
     "        except (subprocess.TimeoutExpired, OSError) as e:\n"
     "            if event == \"PreToolUse\":",
     "        except (subprocess.TimeoutExpired, OSError) as e:\n"
     "            if False:  # MUT 崩溃放行",
     "D5"),
    ("M2 description 超预算不截", SKILLS,
     "        if len(d) > per:",
     "        if False:  # MUT 不截",
     "D7"),
    ("M3 正文注入错位（注了 description）", CORE,
     "            {\"role\": \"user\", \"content\": \"[skill %s]\\n%s\""
     " % (it[\"name\"], body)})",
     "            {\"role\": \"user\", \"content\": \"[skill %s]\\n%s\""
     " % (it[\"name\"], str(it.get(\"description\") or \"\"))})",
     "D8"),
    ("M4 skills 自动触发被摘（run_agent 不注入）", CORE,
     "    _sauto = _skills_auto(text)",
     "    _sauto = \"\"  # MUT 自动触发被摘",
     "D11"),
]


def run_gate(core, hooks, skills, out, timeout=900):
    return subprocess.run([sys.executable, GATE, "--core", core,
                           "--hooks", hooks, "--skills", skills,
                           "--out", out],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("HOOKS_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="hkmut_")
    try:
        p0 = run_gate(CORE, HOOKS, SKILLS, os.path.join(tmp, "base.json"))
        case("BASE 真源全绿（HOOKS_GATE OK + rc=0）",
             p0.returncode == 0 and "HOOKS_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "HOOKS_GATE_OK" in p0.stdout))

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
            core = mut if target == CORE else CORE
            hooks = mut if target == HOOKS else HOOKS
            skills = mut if target == SKILLS else SKILLS
            try:
                r = run_gate(core, hooks, skills,
                             os.path.join(tmp, "mut.json"))
            except subprocess.TimeoutExpired:
                case(name, False, "门超时")
                continue
            hit = ("FAIL %s" % want) in r.stdout
            case(name, r.returncode != 0 and hit,
                 "rc=%d 期望红「%s」→%s"
                 % (r.returncode, want, "命中" if hit else "未命中"))

        prod_after = b""
        if os.path.exists(PROD_JSON):
            with open(PROD_JSON, "rb") as f:
                prod_after = f.read()
        case("生产 work/hooks_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nHOOKS_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("HOOKS_GATE_SELFTEST_OK" if not bad else "HOOKS_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
