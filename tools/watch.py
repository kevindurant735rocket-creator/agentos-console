#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch —— 老板监督面：一屏看清计划 / 指令 / 会话尾（时时刻刻看得见）。

只读聚合，全部现取现算、不缓存、不估算：
  * 计划：work/plans/<sid>-plan.json —— 经 dsp.load_plan 校验读取；坏计划
    如实显示「计划不可读：<原因>」，不装没坏。
  * 指令：work/steer/<sid>.jsonl 未消费条数（老板的插手在不在、进了没有）。
  * 会话尾：work/agent_sessions/<sid>.jsonl 最后 N 行（user/assistant/note）。

渲染是纯函数 render(snapshot) —— supervise_gate 拿夹具证伪；`--once` 打一份
快照（管道/CI 用），TTY 下循环重画（默认 1s）。
"""
import json
import os
import sys
import time

_TAIL = 8


def snapshot(root, sid, tail=_TAIL):
    """聚合一屏监督面。任何一块取不到都如实标原因，绝不抛穿。"""
    sid = str(sid or "").strip()
    snap = {"sid": sid, "ts": time.time(), "plan": None, "plan_err": "",
            "tasks": [], "steer_n": 0, "steer_total": 0, "tail": []}
    tools_dir = os.path.join(root, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import dsp as _dsp
        plan, err = _dsp.load_plan(sid) if sid else (None, "无 sid")
        if plan is None:
            snap["plan_err"] = err or "missing"
        else:
            snap["plan"] = {"goal": plan.get("goal") or "",
                            "approved": bool(plan.get("approved"))}
            for t in plan.get("tasks") or []:
                snap["tasks"].append({
                    "id": t.get("id"), "title": str(t.get("title") or "")[:40],
                    "status": t.get("status") or "todo",
                    "evidence": len(t.get("evidence") or [])})
    except Exception as e:                        # noqa: BLE001
        snap["plan_err"] = "读计划异常：%s" % str(e)[:80]
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import steer as _steer
        d = os.path.join(root, "work", "steer")
        items, total = _steer.pending(d, sid)
        snap["steer_n"] = len(items)
        snap["steer_total"] = total
    except Exception:                             # noqa: BLE001
        pass
    tp = os.path.join(root, "work", "agent_sessions", "%s.jsonl" % sid)
    try:
        with open(tp, encoding="utf-8") as f:
            rows = f.readlines()
        for raw in rows[-tail:]:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if isinstance(r, dict):
                snap["tail"].append({"kind": r.get("kind") or "?",
                                     "ts": r.get("ts") or 0,
                                     "text": str(r.get("text") or "")[:64]})
    except OSError:
        pass
    return snap


GLYPH = {"done": "[✓]", "doing": "[▶]", "stuck": "[✗]", "blocked": "[■]"}


def render(snap):
    """一屏监督面（纯函数）。空就是空，坏就是坏，绝不画假进度。"""
    s = snap or {}
    ts = time.strftime("%H:%M:%S", time.localtime(s.get("ts") or 0))
    lines = ["== 监督面 sid=%s @ %s ==" % (s.get("sid") or "?", ts)]
    pl = s.get("plan")
    if pl:
        lines.append("计划：%s（%s）"
                     % (str(pl.get("goal"))[:60],
                        "已批准" if pl.get("approved") else "未批准"))
    else:
        lines.append("计划：无（%s）" % (s.get("plan_err") or "missing"))
    tasks = s.get("tasks") or []
    if tasks:
        n_done = sum(1 for t in tasks if t.get("status") == "done")
        lines.append("任务 %d/%d：" % (n_done, len(tasks)))
        for t in tasks:
            g = GLYPH.get(t.get("status"), "[·]")
            lines.append("  %s %-8s %s（证据 %d）"
                         % (g, t.get("id") or "?", t.get("title") or "",
                            t.get("evidence") or 0))
    lines.append("老板指令：未消费 %s 条（累计 %s 条）"
                 % (s.get("steer_n") or 0, s.get("steer_total") or 0))
    tail = s.get("tail") or []
    if tail:
        lines.append("会话尾：")
        for r in tail:
            hh = time.strftime("%H:%M", time.localtime(r.get("ts") or 0))
            lines.append("  [%s] %-8s %s" % (hh, r.get("kind") or "?",
                                             r.get("text") or ""))
    else:
        lines.append("会话尾：（无记录）")
    lines.append("提示：agentos steer <sid> \"指令\"；/branch 分支；/verify 对账")
    return "\n".join(lines)


def main(argv=None):
    """`python3 tools/watch.py [--sid S] [--root R] [--once] [--interval N]`"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", default="")
    ap.add_argument("--root", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args(argv)
    if args.once or not sys.stdout.isatty():
        sys.stdout.write(render(snapshot(args.root, args.sid)) + "\n")
        return 0
    try:
        while True:
            sys.stdout.write("\x1b[H\x1b[2J" if not args.once else "")
            sys.stdout.write(render(snapshot(args.root, args.sid)) + "\n")
            sys.stdout.flush()
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
