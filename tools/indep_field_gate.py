#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立审计字段门（D55）：治「顶栏长期显示未给结论（null）」这类取错键的空壳修补。

实测 bug（D55，截图抓到的，不是推测）：data.json 的 independent.verdict 恒为 null，
UI 因此长期显示「独立审计 未给结论（null）」。真因是 tools/extract.py 取了 `i.get("verdict")`，
而上游 independent_audit.json 的字段名是 **independent_verdict**（顶层根本没有 verdict 键）。
注意这个 bug 的恶劣性质：它**不抛异常**、不改变任何计数，data.json 依然重建成功、
build.py 依然绿、所有既有门全绿 —— 只有一个不看代码的操作员在页面上看得见。

为什么单独一个门：修了不装牙，下一次任何人「顺手统一一下字段名」就静默退回空壳。
所以本门自带 4 个变异体（每个都必须被本门判红），再加活产物对账。

双向断言（能同时抓「取错键」和「硬写常量」两种空壳）：
  · 判据从**合成树**跑 extract.py：verdict/consistent/n_mismatches/key_used 全对才算过
  · 变异体 M1 把取键改回 `verdict`（D55 原 bug）-> 必须判红
  · 变异体 M2 改取近形键 `recomputed_global_verdict` -> 必须判红
  · 变异体 M3 丢 consistent 透传 -> 必须判红
  · 变异体 M4 把审计器分歧偷换成自报/实测分歧（两个口径混用）-> 必须判红
  · 活产物：data.json 的 independent 逐字段等于上游真值，且与 n_divergence 各算各的
  · 活产物：index.html 内联载荷的 independent 块 == data.json（构建没丢字段）
  · 模板：两个分歧口径都带口径名（合并成一个名字 -> 判红）

零浏览器、零端口、纯 stdlib + 子进程，秒级；不改活产物（合成树 + 临时产物）。
产出 work/indep_field.json。rc=0 全过 / 1 有红项 / 2 用法或文件错。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
EXTRACT = os.path.join(HERE, "extract.py")
TPL = os.path.join(ROOT, "index.template.html")
HTML = os.path.join(ROOT, "index.html")
DATA = os.path.join(ROOT, "data.json")
OUT = os.path.join(ROOT, "work", "indep_field.json")
REAL_TREE = "/Users/zhangfengrui/Desktop/项目/future agent"
UP_INDEP = os.path.join(REAL_TREE, "phase18-verify-gate/report/independent_audit.json")
PAY_RE = re.compile(
    r'(<script id="payload" type="application/json">)(.*?)(</script>)', re.S)

# 合成树的**期望值**（与 fixture 写入值逐字对应，改一处必须两处一起改）
FX_VERDICT = "PASS"
FX_CONSISTENT = True
FX_NMISMATCH = 7            # 审计器重算 vs 闸门
FX_NDIVERGENCE = 1          # 阶段自报 vs 闸门实测
FX_RECOMPUTED = "FAIL"      # 近形键，故意与 FX_VERDICT 不同 —— M2 才有牙

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    return bool(ok)


def iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


# ---------------------------------------------------------------- 合成树
def build_fixture(td):
    """造一棵最小真树副本：只保留 extract.py 真正读的 4 个文件。"""
    tree = os.path.join(td, "tree")
    rep = os.path.join(tree, "phase18-verify-gate", "report")
    os.makedirs(rep)
    os.makedirs(os.path.join(tree, "phase01"))
    os.makedirs(os.path.join(tree, "phase02"))
    os.makedirs(os.path.join(tree, "work"))
    verdicts = {
        "generated_at": iso(), "global_verdict": "PASS", "coverage_complete": True,
        "coverage": {}, "rule": "fixture", "aggregate": {}, "env_gated": [],
        "interpreter_assert": {"ok": True},
        "rows": [
            {"dir": "phase01", "declared_verdict": "PASS", "verdict": "PASS",
             "exit_code": 0, "failing_stages": [], "labels": []},
            # 自报 FAIL / 实测 PASS -> 造出 1 处「自报-实测分歧」
            {"dir": "phase02", "declared_verdict": "FAIL", "verdict": "PASS",
             "exit_code": 0, "failing_stages": [], "labels": []},
        ],
    }
    with io.open(os.path.join(rep, "verdicts.json"), "w", encoding="utf-8") as f:
        json.dump(verdicts, f, ensure_ascii=False)
    indep = {
        "independent_verdict": FX_VERDICT,
        "recomputed_global_verdict": FX_RECOMPUTED,
        "consistent": FX_CONSISTENT,
        "n_mismatches": FX_NMISMATCH,
        "audited_generated_at": iso(),
        "auditor": "fixture (no import of gate)",
    }
    with io.open(os.path.join(rep, "independent_audit.json"), "w", encoding="utf-8") as f:
        json.dump(indep, f, ensure_ascii=False)
    with io.open(os.path.join(tree, "phase01", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"measured_at": iso(), "reproducible": True,
                   "acceptance": {"verdict_counts_after": {"PASS": 1}}}, f, ensure_ascii=False)
    goals = ("- 需求: fixture\n- 轮次计划: fixture\n\n"
             "| 轮次 | 目标 | 判据 | 跑 | 状态 | AC | 差 | 证据 |\n"
             "| R1 | g | c | r | s | a | d | e |\n")
    with io.open(os.path.join(tree, "work", "GOALS.md"), "w", encoding="utf-8") as f:
        f.write(goals)
    return tree


def run_extract(script, tree, out_json):
    """在合成树上跑一份 extract.py（可以是变异体），返回 data.json 字典。"""
    env = dict(os.environ)
    env["AGENTOS_TREE_ROOT"] = tree
    env["AGENTOS_DATA_OUT"] = out_json
    r = subprocess.run([sys.executable, script], capture_output=True, text=True,
                       env=env, cwd=ROOT)
    if r.returncode != 0:
        return None, "rc=%d %s" % (r.returncode, (r.stderr or r.stdout)[-160:])
    try:
        with io.open(out_json, encoding="utf-8") as f:
            return json.load(f), ""
    except Exception as e:  # noqa: BLE001
        return None, "读产物失败 %s" % str(e)[:120]


# ---------------------------------------------------------------- 判据本体
def judge(d):
    """合成树判据：4 个字段全对才算过。返回 (ok, why)。"""
    if not isinstance(d, dict):
        return False, "无产物"
    ind = d.get("independent")
    if not isinstance(ind, dict):
        return False, "independent 块缺失"
    bad = []
    if ind.get("verdict") != FX_VERDICT:
        bad.append("verdict=%r(应 %r)" % (ind.get("verdict"), FX_VERDICT))
    if ind.get("consistent") is not FX_CONSISTENT:
        bad.append("consistent=%r" % (ind.get("consistent"),))
    if ind.get("n_mismatches") != FX_NMISMATCH:
        bad.append("n_mismatches=%r(应 %d)" % (ind.get("n_mismatches"), FX_NMISMATCH))
    if ind.get("key_used") != "independent_verdict":
        bad.append("key_used=%r" % (ind.get("key_used"),))
    # 两个分歧口径必须各算各的：审计器分歧 7 / 自报-实测分歧 1
    if d.get("n_divergence") != FX_NDIVERGENCE:
        bad.append("n_divergence=%r(应 %d)" % (d.get("n_divergence"), FX_NDIVERGENCE))
    return (not bad), ("; ".join(bad) if bad else "4 字段 + 2 口径全对")


MUTANTS = [
    ("M1 取键改回 verdict（D55 原 bug）",
     [('i.get("independent_verdict")', 'i.get("verdict")')]),
    ("M2 改取近形键 recomputed_global_verdict",
     [('i.get("independent_verdict")', 'i.get("recomputed_global_verdict")')]),
    ("M3 丢 consistent 透传",
     [('"consistent": i.get("consistent"),', '')]),
    ("M4 审计器分歧偷换成自报/实测分歧",
     [('"n_mismatches": i.get("n_mismatches"),', '"n_mismatches": len(divergence),')]),
]


def labels_ok(tpl_text):
    """两个分歧口径各自带口径名；混成一个名字 -> 判红。"""
    need = ["审计器分歧（重算 vs 闸门）", "自报-实测分歧（阶段自报 vs 闸门）"]
    miss = [n for n in need if n not in tpl_text]
    return (not miss), ("缺口径名=" + ",".join(miss) if miss else "两个口径名都在盘上")


def main():
    if not os.path.exists(EXTRACT):
        print("FAIL: 缺 tools/extract.py")
        return 2
    for f in (TPL, HTML, DATA):
        if not os.path.exists(f):
            print("FAIL: 缺 %s" % os.path.basename(f))
            return 2

    src = io.open(EXTRACT, encoding="utf-8").read()
    base_html = io.open(HTML, "rb").read()
    base_data = io.open(DATA, "rb").read()
    base_tpl = io.open(TPL, encoding="utf-8").read()

    td = tempfile.mkdtemp(prefix="indep-field-")
    report = {"tool": "indep_field_gate.py", "method": "fixture-extract+mutants+live-reconcile"}
    try:
        tree = build_fixture(td)

        # 1) BASE 正例：当前 extract.py 在合成树上必须全对
        ok, why = run_extract(EXTRACT, tree, os.path.join(td, "base.json"))
        base_d, base_err = (ok, why) if isinstance(ok, dict) else (None, why)
        j_ok, j_why = judge(base_d)
        gate("BASE：抽取键=independent_verdict 且 4 字段 + 2 口径全对", j_ok,
             j_why if j_ok else (base_err or j_why))
        report["base"] = {"ok": j_ok, "why": j_why}

        # 2) 变异体：每个都必须被同一判据判红（这才是「门有牙」）
        mut_report = []
        for name, subs in MUTANTS:
            msrc = src
            missed = []
            for a, b in subs:
                if a in msrc:
                    msrc = msrc.replace(a, b, 1)
                else:
                    missed.append(a[:40])
            mp = os.path.join(td, "mut_extract.py")
            with io.open(mp, "w", encoding="utf-8") as f:
                f.write(msrc)
            d, err = run_extract(mp, tree, os.path.join(td, "mut.json"))
            m_ok, m_why = judge(d)
            hit = (not m_ok)
            gate("变异体 %s -> 判红" % name, hit,
                 ("红因=" + m_why) if hit else ("!! 判据没抓到（空门）产物=" + str(d)[:120]))
            if missed:
                gate("变异体 %s -> 注入点命中" % name, False, "未命中=" + ",".join(missed))
            mut_report.append({"mutant": name, "caught": hit, "why": m_why})
        report["mutants"] = mut_report

        # 3) 活产物对账：逐字段等于上游真值
        try:
            with io.open(UP_INDEP, encoding="utf-8") as f:
                up = json.load(f)
            live = json.loads(base_data.decode("utf-8"))
            ind = live.get("independent") or {}
            live_bad = []
            if not up.get("independent_verdict"):
                live_bad.append("上游无 independent_verdict（真源变形，fail-closed）")
            if ind.get("verdict") != up.get("independent_verdict"):
                live_bad.append("verdict=%r != 上游 %r"
                                % (ind.get("verdict"), up.get("independent_verdict")))
            if ind.get("consistent") != up.get("consistent"):
                live_bad.append("consistent=%r != 上游 %r"
                                % (ind.get("consistent"), up.get("consistent")))
            if ind.get("n_mismatches") != up.get("n_mismatches"):
                live_bad.append("n_mismatches=%r != 上游 %r"
                                % (ind.get("n_mismatches"), up.get("n_mismatches")))
            if ind.get("key_used") != "independent_verdict":
                live_bad.append("key_used=%r" % (ind.get("key_used"),))
            if not ind.get("verdict"):
                live_bad.append("verdict 为空 -> 页面会显示「未给结论（null）」")
            gate("活产物 independent 逐字段等于上游真值", not live_bad,
                 "verdict=%s consistent=%s 分歧=%s" % (ind.get("verdict"),
                                                     ind.get("consistent"),
                                                     ind.get("n_mismatches"))
                 if not live_bad else "; ".join(live_bad))
            report["live"] = {"ok": not live_bad, "why": "; ".join(live_bad) or "对账一致"}
        except Exception as e:  # noqa: BLE001
            gate("活产物 independent 逐字段等于上游真值", False, "EXC %s" % str(e)[:140])

        # 4) 两个分歧口径在活数据里必须同时存在且不相等（合并即判红）
        try:
            live = json.loads(base_data.decode("utf-8"))
            ind = live.get("independent") or {}
            both = ("n_divergence" in live) and ("n_mismatches" in ind)
            gate("两个分歧口径并存（自报/实测 vs 审计器重算）", both,
                 "n_divergence=%s vs n_mismatches=%s"
                 % (live.get("n_divergence"), ind.get("n_mismatches")))
        except Exception as e:  # noqa: BLE001
            gate("两个分歧口径并存（自报/实测 vs 审计器重算）", False, "EXC %s" % str(e)[:140])

        # 5) 构建不丢字段：index.html 内联载荷的 independent 块 == data.json
        m = PAY_RE.search(base_html.decode("utf-8"))
        if not m:
            gate("index.html 内联载荷保留 independent 字段", False, "找不到 payload 块")
        else:
            pay = json.loads(m.group(2).replace("<\\/", "</").replace("<\\!--", "<!--"))
            same = (pay.get("independent") or {}) == (live.get("independent") or {})
            gate("index.html 内联载荷的 independent 块 == data.json", same,
                 "verdict=%s" % ((pay.get("independent") or {}).get("verdict"),))
            report["payload"] = {"ok": same}

        # 6) 模板两个口径名都在（UI 侧防「两处数字打架」复发）
        t_ok, t_why = labels_ok(base_tpl)
        gate("模板：两个分歧口径各带口径名（不合并）", t_ok, t_why)
        mut_tpl = base_tpl.replace("自报-实测分歧（阶段自报 vs 闸门）", "审计器分歧（重算 vs 闸门）", 1)
        m_ok, m_why = labels_ok(mut_tpl)
        gate("变异体 口径名合并成一个 -> 判红", (not m_ok),
             ("红因=" + m_why) if (not m_ok) else "!! 口径名合并不被判红（空门）")
    finally:
        now_html = io.open(HTML, "rb").read()
        now_data = io.open(DATA, "rb").read()
        now_tpl = io.open(TPL, encoding="utf-8").read()
        if now_html != base_html or now_data != base_data or now_tpl != base_tpl:
            gate("活产物零写入（本门不得改被验收物）", False,
                 "index.html/data.json/index.template.html 有字节变化")
        else:
            gate("活产物零写入（本门不得改被验收物）", True, "三文件字节一致")
        shutil.rmtree(td, ignore_errors=True)

    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    nfail = sum(1 for r in results if not r["ok"])
    report["gates"] = results
    report["summary"] = {"ok": len(results) - nfail, "total": len(results)}
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("== INDEP_FIELD_GATE %s  %d/%d 门通过 =="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail, len(results)))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
