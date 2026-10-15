#!/usr/bin/env python3
"""从 future agent 真树抽驾驶舱数据 -> data.json。

纪律（v2，2026-10-01）：
1. 不造数。每个字段都来自源文件，UI 端可显示出处。
2. 两个口径并列，不合并、不藏分歧：
   - declared = 阶段自报 (rows[].declared_verdict)
   - gate     = 闸门实测 (rows[].verdict)
   两者不等 -> 落进 divergence[]，UI 必须显式标注「需人工裁决」。
3. failing_stages 不是缺陷计数：含对抗性负控(fake-inject 应当失败)，
   所以拆成 fail_total / fail_negctrl / fail_real，只把 fail_real 当缺陷。
4. manifest.json 是每个阶段目录各一份（真树实测 57/57 都有）；
   旧版去找 report/manifest.json 是错路径 -> 改为按目录统计覆盖率。
"""
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime

# D55：真树根与落盘路径改为**可覆盖**（环境变量），默认值与原来逐字一致。
# 为什么必须可覆盖：给「独立审计字段」加牙时，只能在隔离的合成树上跑抽取逻辑并对拍产物
# —— 不能为了造用例去改真树，也不能去改活的 data.json（改了就是「门改被验收物」，
# 并发读者会在注入窗口里看到假值，症状与真 bug 无法区分）。
R = os.environ.get("AGENTOS_TREE_ROOT", "/Users/zhangfengrui/Desktop/项目/future agent")
DATA_OUT = os.environ.get("AGENTOS_DATA_OUT", "")
REPORT = os.path.join(R, "phase18-verify-gate/report")
GATE = os.path.join(REPORT, "verdicts.json")
INDEP = os.path.join(REPORT, "independent_audit.json")
GOALS = os.path.join(R, "work/GOALS.md")

NEGCTRL = ("fake__", "__fake", "noinject", "[mut]", "mutation", "故障注入", "负控")


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def classify_failures(stages):
    total = len(stages)
    neg = sum(1 for s in stages if any(k in s for k in NEGCTRL))
    return total, neg, total - neg


def read_manifest_of(dirname):
    p = os.path.join(R, dirname, "manifest.json")
    if not os.path.exists(p):
        return {"present": False}
    try:
        m = jload(p)
    except Exception as e:
        return {"present": True, "error": str(e)}
    acc = m.get("acceptance")
    if not isinstance(acc, dict):
        acc = {}   # 部分阶段 acceptance 是布尔值，不能当 dict 取键
    return {
        "present": True,
        "measured_at": m.get("measured_at") or m.get("date"),
        "reproducible": m.get("reproducible"),
        "verdict_counts": acc.get("verdict_counts_after") or {},
        "reference_pass": acc.get("reference_pass"),
    }


def main():
    d = jload(GATE)
    rows = d.get("rows", [])
    dirs = []
    divergence = []
    for r in rows:
        name = r.get("dir")
        declared = r.get("declared_verdict")
        gate = r.get("verdict")
        stages = r.get("failing_stages") or []
        total, neg, real_red = classify_failures(stages)
        item = {
            "dir": name,
            "declared": declared,
            "gate": gate,
            "agree": declared == gate,
            "exit": r.get("exit_code"),
            "dur": r.get("duration_sec"),
            "budget": r.get("budget_sec"),
            "reasons": (r.get("reasons") or [])[:3],
            "fail_total": total,
            "fail_negctrl": neg,
            "fail_real": real_red,
            "labels": r.get("labels") or [],
            "manifest": read_manifest_of(name),
        }
        dirs.append(item)
        if not item["agree"]:
            divergence.append({
                "dir": name,
                "declared": declared,
                "gate": gate,
                "real_red": real_red,
                "sample": stages[:3],
                "reasons": (r.get("reasons") or [])[:3],
                "adjudication": "需人工裁决" if real_red > 0 else "自报严于实测",
            })

    man_present = sum(1 for x in dirs if x["manifest"].get("present"))
    out = {
        "generated_at": d.get("generated_at"),
        # D40：把「证据产生时刻」和「驾驶舱抽取时刻」**分成两个字段**。
        # 教训（D40 实测）：原来只有 generated_at，而它是上游 verdicts.json 的
        # 时间戳、原样透传。实测抓到真故障：上游 verdicts.json 停在 10/01 15:31
        # 已 26 小时没重跑，驾驶舱 data.json 却每轮照常重建，头顶还顶着绿色
        # PASS + LIVE 徽章 —— 操作员完全看不出「这是昨天验的」。
        # 两个时间戳必须都在：generated_at 说「证据多老」（该红就红），
        # extracted_at 说「我什么时候读的」（判断常驻实例有没有跟上）。
        "extracted_at": datetime.now().astimezone().isoformat(),
        "evidence_source": GATE,
        "global_verdict": d.get("global_verdict"),
        "coverage_complete": d.get("coverage_complete"),
        "coverage": d.get("coverage", {}),
        "rule": d.get("rule"),
        "aggregate": d.get("aggregate", {}),
        "declared_counts": dict(Counter(x["declared"] for x in dirs)),
        "gate_counts": dict(Counter(x["gate"] for x in dirs)),
        "divergence": divergence,
        "n_divergence": len(divergence),
        "env_gated": d.get("env_gated", []),
        "interpreter_assert": d.get("interpreter_assert", {}),
        "manifest": {
            "present": man_present,
            "missing": len(dirs) - man_present,
            "n_total": len(dirs),
            "note": "manifest.json 按阶段目录各一份，非 report/ 级单文件",
        },
        "dirs": dirs,
    }

    try:
        i = jload(INDEP)
        # D55 真 bug（截图实测抓到的，不是推测）：这里原来取 i.get("verdict")，
        # 而真源 independent_audit.json 的字段名是 **independent_verdict**（顶层无 verdict 键），
        # 于是 out["independent"]["verdict"] **恒为 None** —— 顶栏与「需求与总闸」表长期显示
        # 「独立审计 未给结论（null）」，而真源明明写着 PASS 且 n_mismatches=0。
        # fail-closed 纪律：取不到就**显式**记 None 并把真源里所有 verdict 形近键列出来，
        # 绝不静默兜底成 PASS —— 兜底会把「上游改名/改结构」伪装成绿灯。
        out["independent"] = {
            "verdict": i.get("independent_verdict"),
            "consistent": i.get("consistent"),
            "n_mismatches": i.get("n_mismatches"),
            "audited_generated_at": i.get("audited_generated_at"),
            "auditor": i.get("auditor"),
            "source_keys": sorted(k for k in i if "verdict" in k),
            "key_used": "independent_verdict",
        }
    except Exception as e:
        out["independent"] = {"error": str(e), "verdict": None, "key_used": None}

    txt = open(GOALS, encoding="utf-8").read()
    meta = {}
    for line in txt.splitlines():
        if line.startswith("- 需求:"):
            meta["demand"] = line.split(":", 1)[1].strip()
        if line.startswith("- 轮次计划:"):
            meta["rounds_plan"] = line.split(":", 1)[1].strip()
    rounds = []
    for line in txt.splitlines():
        if not re.match(r"^\|\s*R\d+\s*\|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8:
            continue
        rounds.append({
            "round": cells[0], "goal": cells[1], "criteria": cells[2],
            "run": cells[3], "status": cells[4], "ac": cells[5],
            "delta": cells[6], "evidence": cells[7],
        })
    out["rounds"] = rounds
    out["meta"] = meta

    dest = os.path.abspath(DATA_OUT or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data.json"))
    # 所有权分界（D65）：extract 只拥有「树派生」的那些键，live_snapshot 归
    # tools/snap_live.py。这里把上一轮的 live_snapshot 原样带走，
    # 否则每跑一次 rebuild，快照就被整块丢掉 —— 直开 index.html 时闭环三面
    # 又塌回「需服务在线」，而且**悄无声息**：rebuild 是绿的，只是交付物烂了。
    # 带走陈旧快照是安全的：前端按 age 打「非实时 / 已过期」，不会冒充实时。
    try:
        with open(dest, encoding="utf-8") as f:
            _prev = json.load(f)
        if isinstance(_prev.get("live_snapshot"), dict):
            out["live_snapshot"] = _prev["live_snapshot"]
    except Exception:
        pass  # 首跑没有上一轮 data.json，正常
    # 原子写：build.py 每次上线都读 data.json，非原子写会在 build 读到半截 JSON 时
    # 直接炸掉，看起来像「并发时门随机红」。临时文件 + os.replace。
    _fd, _tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix='.tmp-', suffix='.swap')
    try:
        with os.fdopen(_fd, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, dest)
    except BaseException:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise

    print("dirs=%d rounds=%d global=%s divergence=%d manifest=%d/%d declared=%s gate=%s" % (
        len(dirs), len(rounds), out["global_verdict"], out["n_divergence"],
        man_present, len(dirs), out["declared_counts"], out["gate_counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
