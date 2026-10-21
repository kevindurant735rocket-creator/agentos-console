#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""branches_gate —— W7 创新波门：会话分支显式对账 + 计划自验收不提前 done。

为什么必须有这道门：
  分支可以"拷了但断了链"（原文件被改、轮数不对），自验收可以"done 了就
  算完成"（不查证据）。本门站在外面认这些事实（纯离线）：
    D1  branches 可导入 + fork/verify 两原语齐
    D2  `--check` 末行逐字 `AGENT_CORE OK 171/171`（数漂移即红）
    D3  源码锚点：/branch 分支 / /verify 分支 / 补全表两条 各就位
    D4  fork 对账（行为）：原文件不动 + 新文件多注记 + 轮数对
    D5  verify 五态（行为）：done/pending/缺证据/对不上/无 accept 全分得开
    D6  不提前 done（行为）：跑了白名单工具也不自动 done
    D7  缺记录 fail-closed（行为）：无落盘记录不崩
    D8  分叉两路独立（行为）：fork 后两文件各自追加互不串
    D9  零污染：work/ 只多 --out

用法: python3 tools/branches_gate.py [--core PATH] [--branches PATH] [--out P]
裁决：BRANCHES_GATE OK n/n（rc=0）/ BRANCHES_GATE RED（rc=2，有漏网）
"""

import argparse
import importlib.util
import json
import os
import gate_pollution as _pollution
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CORE = os.path.join(HERE, "agent_core.py")
DEFAULT_BRANCHES = os.path.join(HERE, "branches.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "branches_gate.json")
EXPECT_CHECK = "AGENT_CORE OK 171/171"

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(
        ("PASS " if ok else "FAIL ")
        + name
        + ("  -- " + str(detail)[:150] if detail else "")
    )


def load_mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _finish(out_path, core_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(
            {
                "core": os.path.relpath(core_path, ROOT),
                "judgments": results,
                "n_ok": n_ok,
                "n_total": len(results),
            },
            fh,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    print("BRANCHES_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("BRANCHES_GATE_OK")
        return 0
    print("BRANCHES_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--branches", default=DEFAULT_BRANCHES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    print("== branches_gate：会话分支 + 计划自验收")

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        asrc = open(args.core, encoding="utf-8").read()
    except OSError as e:
        print("BRANCHES_GATE_RED 读不到 core：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="brgate_")

    # D1 可导入 + 两原语
    B = None
    try:
        B = load_mod(args.branches, "branches_g")
        gate(
            "D1 可导入 + fork/verify 两原语齐",
            hasattr(B, "fork_transcript") and hasattr(B, "verify_plan"),
            "attrs=%s"
            % ([a for a in ("fork_transcript", "verify_plan") if hasattr(B, a)],),
        )
    except Exception as e:  # noqa: BLE001
        gate("D1 可导入 + fork/verify 两原语齐", False, repr(e)[:120])
    if B is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return _finish(args.out, args.core)

    # D2 --check 逐字令牌
    try:
        p = subprocess.run(
            [sys.executable, args.core, "--check"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        # 报告**实际末行**（不是只报 OK 那行的 grep 结果）：自检失败时
        # stdout 末行是 "AGENT_CORE FAIL 154/155"，只 grep "AGENT_CORE OK"
        # 会得到空串，报告成 tail='' —— 红是红了，但看不出哪条判据红的。
        _lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        _actual = _lines[-1].strip() if _lines else ""
        gate(
            "D2 --check 末行逐字 %s" % EXPECT_CHECK,
            p.returncode == 0 and _actual == EXPECT_CHECK,
            "rc=%s actual=%r expected=%r"
            % (p.returncode, _actual[:60], EXPECT_CHECK),
        )
    except Exception as e:  # noqa: BLE001
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK, False, repr(e)[:120])

    # D3 源码锚点
    n_br = asrc.count('if cmd == "/branch":')
    n_vf = asrc.count('if cmd == "/verify":')
    import re as _re

    n_tbl = len(_re.findall(r'"/branch",\s*"/verify",', asrc))
    gate(
        "D3 源码锚点（/branch 分支//verify 分支/补全表 各就位）",
        (n_br, n_vf, n_tbl) == (1, 1, 1),
        "branch=%d verify=%d tbl=%d" % (n_br, n_vf, n_tbl),
    )

    # D4 fork 对账
    src_p = os.path.join(tmp, "agent-20261013-0001-ab12.jsonl")
    with open(src_p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "user", "text": "u1"}) + "\n")
        fh.write(json.dumps({"kind": "assistant", "text": "a1"}) + "\n")
        fh.write(json.dumps({"kind": "user", "text": "u2"}) + "\n")
    dst_d = os.path.join(tmp, "dst")
    ok4, _ = B.fork_transcript(src_p, dst_d, "agent-20261013-0002-cd34", "试分支")
    rows4, src_n = [], open(src_p, encoding="utf-8").read().count("\n")
    if ok4:
        with open(
            os.path.join(dst_d, "agent-20261013-0002-cd34.jsonl"), encoding="utf-8"
        ) as fh:
            for raw in fh:
                rows4.append(json.loads(raw))
    gate(
        "D4 fork 对账：原文件不动 + 新文件多注记 + 轮数对",
        ok4
        and len(rows4) == 4
        and rows4[-1].get("kind") == "note"
        and "fork 自" in rows4[-1].get("text", "")
        and src_n == 3,
        "ok=%s rows=%d src_lines=%d" % (ok4, len(rows4), src_n),
    )

    # D5 verify 五态
    tasks = [
        {
            "id": "t1",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": ["rc=0 全绿"],
        },
        {
            "id": "t2",
            "tools": ["read_file"],
            "depends": [],
            "status": "todo",
            "accept": "rc=0",
            "evidence": [],
        },
        {
            "id": "t3",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": [],
        },
        {
            "id": "t4",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": ["做了些事"],
        },
        {
            "id": "t5",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "",
            "evidence": ["rc=0"],
        },
    ]
    rep5 = B.verify_plan({"tasks": tasks})
    m5 = {x["id"]: x["verdict"] for x in rep5["verdicts"]}
    gate(
        "D5 verify 五态：真完成/未做/缺证据/对不上/无 accept 全分得开",
        m5
        == {
            "t1": "done",
            "t2": "pending",
            "t3": "evidence-missing",
            "t4": "evidence-mismatch",
            "t5": "evidence-mismatch",
        }
        and rep5["n_done_true"] == 1,
        "m=%s" % (m5,),
    )

    # D6 不提前 done（纯面断言：pending 任务的 status 如实保留）
    t6 = {
        "id": "x",
        "tools": ["read_file"],
        "depends": [],
        "status": "doing",
        "accept": "rc=0",
        "evidence": ["rc=0 真跑过"],
    }
    v6, _ = B.verify_task(t6)
    gate(
        "D6 不提前 done：doing + 证据齐也不算完成（只认 done）",
        v6 == "pending",
        "verdict=%s" % v6,
    )

    # D7 缺记录 fail-closed（fork 源不存在 → ok=False，不抛）
    ok7, err7 = B.fork_transcript(os.path.join(tmp, "none.jsonl"), dst_d, "agent-x", "")
    gate(
        "D7 缺记录 fail-closed：源不存在报 (False, err) 不抛",
        ok7 is False and isinstance(err7, str) and len(err7) > 0,
        "err=%r" % (err7[:60],),
    )

    # D8 分叉两路独立：fork 后两文件各自追加互不串
    dst2 = os.path.join(tmp, "dst2")
    B.fork_transcript(src_p, dst2, "agent-b1", "第一路")
    B.fork_transcript(src_p, dst2, "agent-b2", "第二路")
    with open(os.path.join(dst2, "agent-b1.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "user", "text": "b1-only"}) + "\n")
    r2 = [
        json.loads(l)
        for l in open(os.path.join(dst2, "agent-b2.jsonl"), encoding="utf-8")
    ]
    gate(
        "D8 分叉两路独立：b1 的追加不进 b2",
        not any(r.get("text") == "b1-only" for r in r2) and len(r2) == 4,
        "b2_rows=%d" % len(r2),
    )

    # D9 零污染
    shutil.rmtree(tmp, ignore_errors=True)
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    # 零污染口径共享（tools/gate_pollution.py）：**活着的并发自检**目录不算污染
    new, _live_poll = _pollution.pollution_diff(after, before, {args.out})
    gate("D9 零污染（work/ 只多 --out；夹具已清）", not new, "新增=%s" % (new or "无"))
    return _finish(args.out, args.core)


if __name__ == "__main__":
    sys.exit(main())
