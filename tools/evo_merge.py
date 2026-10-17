#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evo_merge —— 受控进化管线的合入闸：提案 → 复制式沙箱全门绿 → 合入；红则零改动回退。

为什么要有它（体检报告 P0-2 的落点）：进化面已有「信号→复发聚类→规则晋升
→提案」（work/evo_proposals/*.md 人审），但终点只是"建议改 system prompt"——
缺「安全地把自己改强」的这一跳：提案没有机器可执行形态、没有沙箱、没有
合入/回退。本模块补这一跳，纪律与全仓同源：

  * 提案形态（机器可执行）：work/evo_proposals/<id>.json =
      {"id": "...", "rationale": "...",
       "edits": [{"path": "tools/x.py", "find": "原文恰一次", "replace": "新文"},
                 {"path": "tools/new.py", "write": "整文件内容（仅限新文件）"}]}
  * 路径闸：仓内相对路径；禁 ..、绝对路径、work//native//.venv-pw/
    AgentOS.app/.zcode/.codex 禁改区；find 必须在目标文件**恰出现一次**。
  * 沙箱：本仓非 git → **复制式**（tools/ 全量复制到 work/evo_sandbox/<id>/，
    门在副本里跑，判据与真仓同源同程序）。
  * 合入：沙箱全绿 → 真树先快照（字节）→ 应用 → **真树复验**；任一环失败
    → 自动回退（从快照恢复）+ 账本记 rolled_back；沙箱红 → **不碰真树**，
    账本记 rejected（真树哈希 before==after 即回退证据）。
  * 账本：work/evo_merges.jsonl append-only（--ledger 可换，测试用）。

用法: python3 tools/evo_merge.py apply <提案.json> [--gates "a b ..."]
      [--root DIR] [--ledger P] [--no-merge]
裁决：EVO_MERGE merged（rc 0）/ rejected|rolled_back（rc 2）/ 用法环境（rc 3）
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORBIDDEN_PREFIX = ("work/", "native/", ".venv-pw", "AgentOS.app",
                    ".zcode", ".codex")

DEFAULT_GATES = ("dsp_gate", "leader_gate", "safety_gate")


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def _ledger_append(path, row):
    row = dict(row)
    row["ts"] = round(time.time(), 3)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def validate(proposal, root):
    """提案闸（纯面）：返回 (ok, err)。路径 / 锚点 / 形态逐条查。"""
    if not isinstance(proposal, dict):
        return False, "提案必须是 JSON object"
    if not str(proposal.get("id") or "").strip():
        return False, "缺 id"
    edits = proposal.get("edits")
    if not isinstance(edits, list) or not edits:
        return False, "缺 edits（非空数组）"
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return False, "edits[%d] 不是 object" % i
        p = str(e.get("path") or "")
        probs = []
        if not p or p.startswith("/") or ".." in p.split("/"):
            probs.append("路径必须是仓内相对路径：%r" % p[:60])
        if p.startswith(FORBIDDEN_PREFIX):
            probs.append("禁改区：%r" % p[:40])
        if probs:
            return False, "；".join(probs)
        fp = os.path.join(root, p)
        if "write" in e:
            if os.path.exists(fp):
                return False, ("write 只许建新文件（%s 已存在：改动用 "
                               "find/replace）" % p)
            continue
        find = e.get("find")
        if not isinstance(find, str) or not find:
            return False, "edits[%d] 缺 find（find/replace 必填）" % i
        try:
            with open(fp, encoding="utf-8") as f:
                src = f.read()
        except OSError as ex:
            return False, "目标不可读：%s（%s）" % (p, str(ex)[:60])
        n = src.count(find)
        if n != 1:  # 锚点纪律：必须恰出现一次
            return False, ("锚点必须恰出现一次：%s 里 %r 命中 %d 处"
                           % (p, find[:50], n))
    return True, ""


def apply_edits(tree, edits):
    """在 tree 里应用 edits；返回被碰文件列表；任一步失败抛 OSError。"""
    touched = []
    for e in edits:
        fp = os.path.join(tree, e["path"])
        if "write" in e:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(e["write"])
            touched.append(fp)
            continue
        with open(fp, encoding="utf-8") as f:
            src = f.read()
        n = src.count(e["find"])
        if n != 1:  # 到应用期锚点漂移（并发改动等）：拒
            raise OSError("锚点变了：%s 命中 %d 处" % (e["path"], n))
        with open(fp, "w", encoding="utf-8") as f:
            f.write(src.replace(e["find"], e["replace"], 1))
        touched.append(fp)
    return touched


def _rollback(snaps):
    for fp, data in snaps.items():
        try:
            with open(fp, "wb") as f:
                f.write(data)
        except OSError:
            pass


def build_sandbox(root, sid):
    """复制式沙箱（本仓非 git）：tools/ 全量 + 与真仓同构的 work/ 基线。返回沙箱根。

    work/plans 必须预置：真仓里它本来就存在，门（如 leader_gate）会在里面
    建/清夹具；沙箱若缺这个基线目录，门自己的「零污染」判据会把沙箱基础
    设施当新增判红（沙箱与真仓基线形状必须同构，门的判据才可判）。
    """
    sb = os.path.join(root, "work", "evo_sandbox", sid)
    if os.path.exists(sb):
        shutil.rmtree(sb, ignore_errors=True)
    os.makedirs(sb)
    shutil.copytree(os.path.join(root, "tools"),
                    os.path.join(sb, "tools"))
    os.makedirs(os.path.join(sb, "work", "plans"), exist_ok=True)
    return sb


def _gates_on(tree, gates, py, timeout=600):
    out = []
    base = os.path.join(tree, "tools")
    for g in gates:
        gp = os.path.join(base, "%s.py" % g)
        if not os.path.isfile(gp):
            out.append((g, 3, "门文件不存在"))
            continue
        try:
            p = subprocess.run([py, gp], capture_output=True, text=True,
                               timeout=timeout, cwd=tree)
            tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
            out.append((g, int(p.returncode), tail[0][:80]))
        except subprocess.TimeoutExpired:
            out.append((g, 3, "超时"))
    return out


def run_pipeline(proposal, root, gates, ledger, do_merge=True):
    """全流程：闸 → 沙箱 → （合入 + 真树复验）。返回 rc。"""
    ok, err = validate(proposal, root)
    if not ok:
        _ledger_append(ledger, {"id": proposal.get("id"), "verdict":
                                "rejected", "why": err[:160]})
        print("EVO_MERGE rejected：%s" % err[:160])
        return 2
    sid = proposal["id"]
    sb = build_sandbox(root, sid)
    try:
        apply_edits(sb, proposal["edits"])
    except (OSError, KeyError) as ex:
        _ledger_append(ledger, {"id": sid, "verdict": "rejected",
                                "why": "沙箱应用失败：" + str(ex)[:140]})
        print("EVO_MERGE rejected：沙箱应用失败（%s）" % str(ex)[:110])
        return 2
    res = _gates_on(sb, gates, sys.executable)
    all_ok = all(rc == 0 for _, rc, _ in res)
    if not all_ok:      # 沙箱有红 → 拒绝（真树零改动）
        _ledger_append(ledger, {"id": sid, "verdict": "rejected",
                                "gates": [[g, rc, t] for g, rc, t in res]})
        print("EVO_MERGE rejected：沙箱 %d 门有红（真树零改动）"
              % sum(1 for _, rc, _ in res if rc))
        for g, rc, t in res:
            print("  [%s] rc=%s %s" % (g, rc, t))
        return 2
    if not do_merge:
        print("EVO_MERGE dry-ok：沙箱全绿（--no-merge，未合入）")
        return 0
    return merge_and_verify(proposal, root, gates, ledger)


def merge_and_verify(proposal, root, gates, ledger):
    """合入真树 + 真树复验；任一环失败自动回退。返回 rc（0/2）。

    单独成函数的理由：回退是**原语**，门要在夹具树上直接驱动它
    （进程内直调）——不像 CLI 那样受"验证后目标消失"的时序窗限制。
    """
    sid = proposal["id"]
    snaps, touched = {}, []
    try:
        for e in proposal["edits"]:
            fp = os.path.join(root, e["path"])
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    snaps[fp] = f.read()
        touched = apply_edits(root, proposal["edits"])
    except (OSError, KeyError) as ex:
        _rollback(snaps)  # 合入失败：回退已落盘的半成品
        _ledger_append(ledger, {"id": sid, "verdict": "rolled_back",
                                "why": "合入失败：" + str(ex)[:140]})
        print("EVO_MERGE rolled_back：合入失败已回退（%s）" % str(ex)[:110])
        return 2
    post = _gates_on(root, gates, sys.executable)
    post_ok = all(rc == 0 for _, rc, _ in post)
    if not post_ok:  # 合入后复验红 → 自动回退
        _rollback(snaps)
        _ledger_append(ledger, {"id": sid, "verdict": "rolled_back",
                                "why": "合入后复验红",
                                "gates": [[g, rc, t] for g, rc, t in post]})
        print("EVO_MERGE rolled_back：合入后复验红，已自动回退")
        for g, rc, t in post:
            print("  [%s] rc=%s %s" % (g, rc, t))
        return 2
    _ledger_append(ledger, {"id": sid, "verdict": "merged",
                            "files": [e["path"] for e in proposal["edits"]]})
    print("EVO_MERGE merged：%s（沙箱+真树复验全绿）" % sid)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evo_merge")
    sub = ap.add_subparsers(dest="action", metavar="动作")
    pa = sub.add_parser("apply", help="应用提案（沙箱→合入→复验/回退）")
    pa.add_argument("proposal", help="提案 JSON 路径")
    pa.add_argument("--gates", default=" ".join(DEFAULT_GATES),
                    help="门清单（空格分隔）")
    pa.add_argument("--root", default="", help="仓库根（测试注入）")
    pa.add_argument("--ledger", default="",
                    help="账本路径（默认 <root>/work/evo_merges.jsonl）")
    pa.add_argument("--no-merge", action="store_true",
                    help="只跑沙箱给判词，不合入")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.action != "apply":
        ap.print_help(sys.stderr)
        return 3
    root = os.path.abspath(args.root) if args.root else ROOT
    ledger = args.ledger or os.path.join(root, "work", "evo_merges.jsonl")
    try:
        with open(args.proposal, encoding="utf-8") as f:
            proposal = json.load(f)
    except (OSError, ValueError) as ex:
        sys.stderr.write("EVO_MERGE 用法/环境错：提案不可读（%s）\n"
                         % str(ex)[:120])
        return 3
    gates = [g for g in str(args.gates).split() if g]
    return run_pipeline(proposal, root, gates, ledger,
                        do_merge=not args.no_merge)


if __name__ == "__main__":
    sys.exit(main())
