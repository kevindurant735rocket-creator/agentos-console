#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""branches —— W7 会话分支 + 计划自验收：显式 > 猜、逐条可验。

会话分支：
  fork(sid, note) → new_sid。语义：把原会话 transcript 逐行拷到新文件
  （原文件不动 = 永不断链），新文件末尾追加一条 fork note（含原 sid、
  轮数、时间）。**分支是显式动作，不是自动分叉** —— fork 只在用户输入
  `/branch [注记]` 时发生。回退 = 切回原会话 id 继续跑（历史文件都在）。
计划自验收：
  accept 结构（W0 就有）vs「验收证据行」是两回事：计划要求每个任务有
  accept 判据，但没人验过任务完成后证据行真满足 accept。verify(plan) 逐任务
  判定：done 且 evidence 满足 accept → 真完成；否则如实分为
  「未做 / 证据缺 / 证据对不上 accept」三类 —— 状态机不许提前 done。
裁决：本模块不做自证；branches_gate 拿夹具证伪它。
"""
import json
import os
import time

ACCEPT_VERBS = ("rc=", "PASS", "FAIL", "OK")


def fork_transcript(src_path, dst_dir, new_sid, note=""):
    """拷转会话 transcript → 新文件 + fork note 行。返回 (ok, err)。"""
    try:
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "%s.jsonl" % new_sid)
        n = 0
        with open(src_path, "r", encoding="utf-8") as f:
            rows = f.readlines()
        with open(dst, "w", encoding="utf-8") as f:
            for raw in rows:
                f.write(raw)
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("kind") == "user":
                    n += 1
        with open(dst, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": round(time.time(), 3), "kind": "note",
                 "text": "fork 自 %s（%d 轮）：%s"
                         % (os.path.basename(src_path)[:-len(".jsonl")],
                            n, note or "无注记")},
                ensure_ascii=False) + "\n")
        return True, ""
    except OSError as e:
        return False, "fork 落盘失败：%s" % str(e)[:120]


def verify_task(task):
    """单任务自验收 → (verdict, detail)。

    done = status 为 done 且 evidence 非空且 evidence 拼起来含 accept；
    pending = status 不是 done（不管有没有证据）；
    evidence-missing = done 但 evidence 空；
    evidence-mismatch = done 有 evidence 但对不上 accept。
    accept 缺 = 计划 schema 的锅（validate_plan 拦过），这里按 mismatch 算。
    """
    t = task if isinstance(task, dict) else {}
    accept = str(t.get("accept") or "").strip()
    ev = t.get("evidence") or []
    status = str(t.get("status") or "todo")
    if not accept:
        return "evidence-mismatch", "任务 %r 无 accept 判据" % t.get("id")
    if status != "done":
        return "pending", "任务 %r 未做（status=%s）" % (t.get("id"), status)
    if not ev:
        return "evidence-missing", "任务 %r 已 done 但 evidence 空" % t.get("id")
    blob = "\n".join(str(x) for x in ev)
    ok = (accept in blob) or any(v in blob for v in ACCEPT_VERBS
                                 if v in accept or accept in blob)
    hit = (accept in blob)
    if not hit:
        return "evidence-mismatch", "任务 %r 证据对不上 accept %r" \
            % (t.get("id"), accept[:60])
    return "done", "任务 %r 真完成（证据命中 accept）" % t.get("id")


def verify_plan(plan):
    """全计划自验收 → {"verdicts", "n_done_true", "n_total"}。

    真绿 = 全部 done；其余全部如实分类，不提前 done。
    """
    tasks = (plan or {}).get("tasks") or []
    verdicts = []
    for t in tasks:
        v, d = verify_task(t)
        verdicts.append({"id": t.get("id"), "verdict": v, "detail": d})
    n_true = sum(1 for x in verdicts if x["verdict"] == "done")
    return {"verdicts": verdicts, "n_done_true": n_true,
            "n_total": len(verdicts)}
