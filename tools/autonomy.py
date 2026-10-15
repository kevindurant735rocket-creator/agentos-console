#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autonomy —— 自主执行引擎：一次审批后，全流程无人干预跑到收口。

为什么要有它（「能全流程自己完成事情」的落点）：leader 证明了这个仓
会**拆**活（目标→DAG→派发→回收），但派完就散 —— 任务失败了没人自救、
跑到一半断电没人续、判据真不真没人复算、收没收口只会打一行 rc。自主
执行引擎把「派发之后」这段空白补满，让一句话目标在**一次审批**之后
自己走到终点：

  * 派发 → 复用 leader 的 worker 契约（{prompt} 单 argv），默认真
    headless agent，桩可换（离线复验）。
  * **确定性验证**：任务可带 verify（命令组）。引擎真跑 verify，
    双闸语义 —— done ⟺ worker rc=0 ∧ verify 全 rc=0（没配 verify 则
    只需 worker rc=0 且有证据行）。worker 自称 PASS 但 verify 红 =
    不 done（反假绿：判据命令是事实，worker 的话只是报告）。
  * **有界自救**：每次失败按确定性分类表（超时/命令不存在/判据没过/
    预算用尽/崩溃）生成修错提示，随下一轮派发书下发重试；1+2 次仍
    不过 → blocked，**下游按 DAG 语义不派发**，独立分支照常做完。
  * **断点检查点**：每次派发/验证后落计划 JSON + 跑账（work/autonomy/
    <sid>.jsonl）；强杀后直接重跑即从断点续，已完成任务零重跑。
  * **诚实收口**：终局落 REPORT.md（逐任务状态/尝试数/verify rc/证据/
    封顶阻断与分类）；全绿 rc=0，部分 rc=1 如实报剩余，绝不装绿。

锁面与 leader 同源：未审批拒跑（落 -block.md），自主引擎不绕计划锁。
预算（--max-tasks）到边界干净停 + 报剩余，再跑即续 —— 不烧穿、不空转。

用法（也经 REPL /goal 全流程驱动）：
  python3 tools/autonomy.py run <sid> [--worker "CMD {prompt}"] \
      [--task-timeout S] [--max-tasks N] [--retries K]
  python3 tools/autonomy.py resume <sid>     # 同 run，语义=断点续跑
  python3 tools/autonomy.py status <sid>     # 计划状态 + 跑账尾
  python3 tools/autonomy.py report <sid>     # 重写/展示终局报告

裁决：autonomy_gate 拿夹具证伪本模块（11 判据）；autonomy_selftest 用
源码级变异体证明门有牙。本模块不做自证。
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AUTONOMY_RETRY = 2          # 自救重试次数：每任务最多 1+2=3 次派发后封顶
VERIFY_TIMEOUT = 300        # 单条 verify 命令墙钟（与任务超时同量级）
TAIL_CAP = 160              # 证据尾行截断

_MODS = {}


def _mods(root):
    """按 root 懒加载 dsp/steer/leader —— 与 leader 同一套注入口径。"""
    key = os.path.abspath(root)
    if key not in _MODS:
        tools = os.path.join(key, "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import dsp
        import leader
        import steer
        _MODS[key] = (dsp, steer, leader)
    return _MODS[key]


def _dir(root):
    return os.path.join(root, "work", "autonomy")


def ledger_append(root, sid, row):
    """跑账 append-only：run_start/dispatch/steer/blocked/run_end 逐行。
    写失败静默（账是增益层）；读账失败在报告里如实标。"""
    r = dict(row)
    r["ts"] = round(time.time(), 3)
    try:
        d = _dir(root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%s.jsonl" % sid), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _read_ledger(root, sid):
    rows = []
    try:
        with open(os.path.join(_dir(root), "%s.jsonl" % sid),
                  encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    try:
                        rows.append(json.loads(raw))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def classify_failure(wrc, tail, v_ok, v_note):
    """失败 → (kind, 下一轮修错提示)。确定性分类表（零模型调用）。

    提示会随重试派发书下发（「上一轮失败与修法」段）——自救不是原地
    重跑，是带着失败事实换一次干法。"""
    t = (tail or "").lower()
    if wrc == 124 or "超时" in (tail or ""):
        return "timeout", ("上一轮超时：把动作拆小，先落盘中间产物再报告；"
                           "不要死等长命令")
    if wrc == 127 or "no such file" in t or "not found" in t:
        return "notfound", "上一轮命令/路径不存在：先确认命令与路径真实存在再跑"
    if "预算" in (tail or "") or "max-steps" in t:
        return "budget", ("上一轮把步数烧光了：做最小可验证产物就收，"
                          "删掉无进展的反复试探")
    if v_ok is False:
        vn = (v_note or "").lower()
        if "124" in vn or "timeout" in vn or "超时" in (v_note or ""):
            return "verify-timeout", ("判据命令自身超时（%s）：把判据拆小/加长单条超时，"
                                      "不许删判据" % (v_note or "verify"))
        return "verify", ("判据命令没过（%s）：先按判据修到真过再交；"
                          "不许改判据、不许只写报告" % (v_note or "verify"))
    if "permission denied" in t or "权限" in (tail or "") or wrc == 126:
        return "denied", "上一轮权限被拒：检查执行位/目录写权，用最小权限重跑"
    if "traceback" in t:
        return "crash", "上一轮崩了（Traceback）：先修异常根因再重跑"
    return "fail", "上一轮 rc=%d：换更小的验证步骤，按判据重来" % wrc


def _run_verify(task, root, timeout=VERIFY_TIMEOUT):
    """真跑任务的 verify 命令组。返回 (ok_or_None, note)。

    ok=None 表示任务没配 verify（回退 worker rc + 证据行）。
    每条命令独立跑、各自记 rc；任一条红即整组红。argv 走 shlex 拆词
    （shell=False），路径/超时/权限错误按 124/127 记，不吞。"""
    v = task.get("verify")
    if not v:
        return None, ""
    if isinstance(v, str):
        v = [v]
    rcs = []
    for cmd in v:
        try:
            argv = shlex.split(str(cmd))
        except ValueError:
            rcs.append(-2)
            continue
        if not argv:
            rcs.append(-2)
            continue
        try:
            p = subprocess.run(argv, cwd=root, capture_output=True,
                               text=True, timeout=timeout)
            rcs.append(int(p.returncode))
        except subprocess.TimeoutExpired:
            rcs.append(124)
        except OSError:
            rcs.append(127)
    return all(r == 0 for r in rcs), "verify rc=%s" % ",".join(map(str, rcs))


def _dispatch_book(plan, task, notes, hint, attempt, max_attempts):
    """派发书：目标/任务/判据/白名单/依赖/判据命令 + 失败修法（有则）。"""
    lines = [
        "[autonomy 派发] 目标：%s" % (plan.get("goal") or "")[:160],
        "任务 %s（%s）：%s" % (task.get("id"), task.get("milestone") or "-",
                              task.get("title") or ""),
        "判据：%s" % (task.get("accept") or ""),
        "允许工具：%s" % ",".join(task.get("tools") or []),
        "依赖（已完成）：%s" % (",".join(task.get("depends") or []) or "-"),
        "引擎尝试：第 %d/%d 次（仍不过将 blocked，下游不派发）"
        % (attempt, max_attempts),
    ]
    if task.get("verify"):
        vv = task["verify"]
        if isinstance(vv, str):
            vv = [vv]
        lines.append("判据命令（引擎会真跑，已过才算数）：%s"
                     % "; ".join(str(x) for x in vv))
    for n in (notes or []):
        lines.append("老板指令（只配送不裁决）：%s" % str(n)[:120])
    if hint:
        lines.append("上一轮失败与修法（必须处理）：%s" % hint)
    lines.append("完成时给出证据行（含 rc=/PASS/FAIL/OK）。")
    return "\n".join(lines)


def _consume_steer(root, sid, steer):
    """任务边界消费老板指令（只配送不裁决；游标只进）。返回 notes 列表。"""
    try:
        box = os.path.join(root, "work", "steer")
        items, total = steer.pending(box, sid)
        if items:
            notes = [str((x or {}).get("text") or "") for x in items]
            steer.commit(box, sid, total)
            ledger_append(root, sid, {"event": "steer", "n": len(notes),
                                      "texts": notes[:5]})
            return notes
    except Exception as e:                                     # noqa: BLE001
        sys.stderr.write("[autonomy] steer 读取异常（忽略）：%s\n"
                         % str(e)[:80])
    return []


def run_goal(sid, root="", worker="", task_timeout=300, max_tasks=12,
             retries=None, mode="run", log=None):
    """自主执行一个已审批计划：派发 → 验证 → 自救 → 封顶 → 检查点。

    返回 rc：0 全绿收口 / 1 诚实部分（剩任务已落账可续） /
    2 拒跑（未审批 / 无可做任务） / 3 证据目录不可建。
    """
    root = os.path.abspath(root or ROOT)
    dsp, steer, leader = _mods(root)
    say = log or (lambda s: print(s))
    if retries is None:
        retries = AUTONOMY_RETRY
    retries = max(0, int(retries))
    plan, err = dsp.load_plan(sid)
    if plan is None:
        sys.stderr.write("autonomy 拒：计划不可读（%s）\n" % (err or "missing"))
        return 2
    if not plan.get("approved"):
        dsp.write_block(sid, "计划未审批：autonomy 拒绝自主执行（未审批=写面"
                             "锁死，自主引擎不绕计划锁）", [],
                        "先 approve（agentos leader approve %s 或 REPL "
                        "/approve），再 autonomy run" % sid)
        sys.stderr.write("autonomy 拒：计划未审批（%s）—— 先 approve\n" % sid)
        return 2
    tasks = plan.get("tasks") or []
    all_done = all(t.get("status") == "done" for t in tasks)
    blocked_any = any(t.get("status") == "blocked" for t in tasks)
    stalled = not dsp.next_task(plan) and not blocked_any \
        and not all_done
    if stalled:
        sys.stderr.write("autonomy 拒：无可做任务（依赖有环或未满足）——"
                         "有环=永远无可做任务，假 DAG 不派发\n")
        return 2
    edir = os.path.join(_dir(root), sid)
    try:
        os.makedirs(edir, exist_ok=True)
    except OSError as e:
        sys.stderr.write("autonomy 拒：证据目录不可建（%s）\n" % str(e)[:80])
        return 3
    ledger_append(root, sid, {"event": "run_start", "mode": mode,
                              "goal": plan.get("goal", ""),
                              "retries": retries})
    t0 = time.time()
    say("autonomy %s 开跑：sid=%s 任务=%d worker=%s 自救=%d 次"
        % (mode, sid, len(tasks), worker or "默认 headless agent", retries))
    dispatched = 0
    while dispatched < int(max_tasks):
        tid = dsp.next_task(plan)
        if not tid:
            break
        task = next((t for t in tasks if t.get("id") == tid), {})
        notes = _consume_steer(root, sid, steer)
        attempt = 0
        passed = False
        hint = ""
        kind = ""
        wrc = 1
        while attempt < 1 + retries:
            attempt += 1
            book = _dispatch_book(plan, task, notes, hint, attempt,
                                  1 + retries)
            argv = leader.worker_argv(worker, root, sid, tid, book)
            outp = os.path.join(edir, "%s.a%d.out" % (tid, attempt))
            ts = time.time()
            wrc = 1
            try:
                with open(outp, "wb") as fh:
                    p = subprocess.run(argv, stdout=fh,
                                       stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL,
                                       timeout=task_timeout, cwd=root)
                wrc = int(p.returncode)
            except subprocess.TimeoutExpired:
                with open(outp, "a", encoding="utf-8") as fh:
                    fh.write("\n[autonomy] 超时 %ss，本轮判失败\n"
                             % task_timeout)
                wrc = 124
            except OSError as e:
                with open(outp, "a", encoding="utf-8") as fh:
                    fh.write("\n[autonomy] 派发失败：%s\n" % str(e)[:160])
                wrc = 127
            dur = round(time.time() - ts, 1)
            tail = leader._tail_line(outp)
            v_ok, v_note = _run_verify(task, root)
            ev = "rc=%d %s %s" % (wrc, v_note or "verify=-",
                                  tail[:TAIL_CAP])
            passed = (wrc == 0 and (v_ok is None or v_ok)
                      and (v_ok is not None or bool(tail)))
            if passed:
                st = dsp.task_record_step(plan, tid, True, ev)
                ledger_append(root, sid, {"event": "dispatch", "task": tid,
                                          "attempt": attempt, "rc": wrc,
                                          "verify": v_note, "status": st,
                                          "dur": dur,
                                          "out": os.path.relpath(outp,
                                                                 root)})
                dsp.save_plan(plan, sid)
                say("  [%s] rc=0 %s → %s（第 %d 次尝试，%.1fs）"
                    % (tid, v_note or "evidence", st, attempt, dur))
                break
            kind, hint = classify_failure(wrc, tail, v_ok, v_note)
            st = dsp.task_record_step(plan, tid, False, ev)
            ledger_append(root, sid, {"event": "dispatch", "task": tid,
                                      "attempt": attempt, "rc": wrc,
                                      "verify": v_note, "status": st,
                                      "kind": kind, "dur": dur,
                                      "out": os.path.relpath(outp, root)})
            dsp.save_plan(plan, sid)       # 每步检查点：强杀也不丢账
            say("  [%s] 第 %d 次未过（%s rc=%d %s）→ %s"
                % (tid, attempt, kind, wrc, v_note or "",
                   "带修法重试" if attempt <= retries else "封顶"))
        if not passed:
            task["status"] = "blocked"
            task.setdefault("evidence", []).append(
                "blocked(kind=%s)：连续 %d 次未过（rc=%d %s %s）"
                % (kind, attempt, wrc, v_note or "", hint[:90]))
            ledger_append(root, sid, {"event": "blocked", "task": tid,
                                      "kind": kind, "attempts": attempt})
            dsp.save_plan(plan, sid)
            say("  [%s] 封顶 %d 次仍未过（%s）→ blocked，下游不派发"
                % (tid, attempt, kind))
        dispatched += 1
    remain = [str(t.get("id")) for t in tasks if t.get("status") != "done"]
    dur = round(time.time() - t0, 1)
    ledger_append(root, sid, {"event": "run_end", "remain": remain,
                              "dur": dur, "mode": mode})
    rep = write_report(sid, root)
    rel = os.path.relpath(rep, root) if rep else "（报告落盘失败）"
    if not remain:
        say("autonomy run 完成：%d/%d 全绿（%.1fs · 报告 %s）"
            % (len(tasks), len(tasks), dur, rel))
        return 0
    sys.stderr.write("autonomy 未完成：剩 %d 个任务（%s）—— 已如实落账；"
                     "重跑 `autonomy resume %s` 即从断点续（已完成任务"
                     "零重跑）。报告 %s\n"
                     % (len(remain), ",".join(remain), sid, rel))
    return 1


def write_report(sid, root=""):
    """终局报告：逐任务状态/尝试/verify rc/证据 + 封顶阻断 + 下一步。
    返回报告路径（失败返回 ""）。"""
    root = os.path.abspath(root or ROOT)
    dsp, _s, _l = _mods(root)
    plan, err = dsp.load_plan(sid)
    if plan is None:
        return ""
    rows = _read_ledger(root, sid)
    disp = [r for r in rows if r.get("event") == "dispatch"]
    by_task = {}
    for r in disp:
        by_task.setdefault(r.get("task"), []).append(r)
    blocks = [r for r in rows if r.get("event") == "blocked"]
    steers = [r for r in rows if r.get("event") == "steer"]
    tasks = plan.get("tasks") or []
    done = [t for t in tasks if t.get("status") == "done"]
    lines = [
        "# autonomy 自主执行报告 · %s" % sid,
        "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "- 目标：%s" % (plan.get("goal") or ""),
        "- 计划：approved=yes bind=%s" % str(plan.get("bind_sha")
                                             or "")[:12],
        "- 汇总：全绿 %d/%d · 派发 %d 次 · steer %d 条"
        % (len(done), len(tasks), len(disp),
           sum(int(r.get("n") or 0) for r in steers)),
        "",
        "## 任务账（状态 + 尝试 + verify rc + 证据）",
    ]
    for t in tasks:
        rs = by_task.get(t.get("id")) or []
        vr = [str(r.get("verify")) for r in rs if r.get("verify")]
        lines.append("  - %s [%s] 尝试=%d 派发rc=%s 验证=%s"
                     % (t.get("id"), t.get("status"), len(rs),
                        ",".join(str(r.get("rc")) for r in rs) or "-",
                        ",".join(vr) or "-"))
        lines.append("      证据：%s"
                     % str((t.get("evidence") or ["-"])[-1])[:160])
    if blocks:
        lines += ["", "## 封顶阻断（逃不掉的事实）"]
        for r in blocks:
            lines.append("  - %s：连续 %d 次未过（kind=%s）→ 下游不派发"
                         % (r.get("task"), r.get("attempts"),
                            r.get("kind")))
    lines += ["", "## 结论",
              "- 全绿 %d/%d；未完成：%s"
              % (len(done), len(tasks),
                 ",".join(str(t.get("id")) for t in tasks
                          if t.get("status") != "done") or "无"),
              "", "## 下一步",
              "- 断点续跑：python3 tools/autonomy.py resume %s（已完成任务"
              "零重跑）；反复 blocked 的任务先看 work/autonomy/%s/<tid>."
              "a*.out 与验证输出再改" % (sid, sid)]
    p = os.path.join(_dir(root), "%s-REPORT.md" % sid)
    try:
        os.makedirs(_dir(root), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return ""
    return p


def cmd_status(args, root):
    """看状态：计划渲染 + 跑账尾（最近 12 行摘要）。"""
    dsp, _s, _l = _mods(root)
    plan, err = dsp.load_plan(args.sid)
    if plan is None:
        sys.stderr.write("autonomy status 拒：计划不可读（%s）\n"
                         % (err or "missing"))
        return 2
    print(dsp.render_plan(plan))
    rows = _read_ledger(root, args.sid)
    print("— 跑账尾（共 %d 行）—" % len(rows))
    for r in rows[-12:]:
        ev = r.get("event")
        if ev == "dispatch":
            print("  [%s] 第 %s 次 rc=%s %s%s"
                  % (r.get("task"), r.get("attempt"), r.get("rc"),
                     r.get("verify") or "",
                     "（%s）" % r.get("kind") if r.get("kind") else ""))
        elif ev == "blocked":
            print("  [%s] blocked（%s，%s 次）"
                  % (r.get("task"), r.get("kind"), r.get("attempts")))
        elif ev == "run_start":
            print("  run_start（%s）" % r.get("mode"))
        elif ev == "run_end":
            print("  run_end（剩 %s）" % (r.get("remain") or "无"))
        elif ev == "steer":
            print("  steer %s 条" % r.get("n"))
    return 0


def cmd_report(args, root):
    """重写并展示报告路径与摘要。"""
    p = write_report(args.sid, root)
    dsp, _s, _l = _mods(root)
    plan, _err = dsp.load_plan(args.sid)
    if not p:
        sys.stderr.write("autonomy report 拒：计划不可读或报告落盘失败\n")
        return 2
    tasks = (plan or {}).get("tasks") or []
    done = [t for t in tasks if t.get("status") == "done"]
    print("报告已落盘：%s" % os.path.relpath(p, root))
    print("全绿 %d/%d" % (len(done), len(tasks)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="autonomy",
        description="自主执行引擎：审批后全流程无人干预（派发/验证/自救/"
                    "断点续跑/诚实收口）")
    ap.add_argument("--root", default="",
                    help="仓库根（默认按本文件定位；测试注入）")
    sub = ap.add_subparsers(dest="action", metavar="动作")
    for act, helptext in (("run", "自主执行（未审批拒跑）"),
                          ("resume", "断点续跑（同 run，已完成零重跑）")):
        p1 = sub.add_parser(act, help=helptext)
        p1.add_argument("sid")
        p1.add_argument("--worker", default="",
                        help='派发命令模板（{prompt}/{root}/{sid}/{tid}/'
                             '{py}）；缺省 "agentos agent -p <prompt>"')
        p1.add_argument("--task-timeout", type=int, default=300)
        p1.add_argument("--max-tasks", type=int, default=12,
                        help="本轮到多少任务干净停（预算边界，可再跑续）")
        p1.add_argument("--retries", type=int, default=None,
                        help="每任务自救重试次数（默认 %d）"
                             % AUTONOMY_RETRY)
    p2 = sub.add_parser("status", help="计划状态 + 跑账尾")
    p2.add_argument("sid")
    p3 = sub.add_parser("report", help="重写终局报告")
    p3.add_argument("sid")
    args = ap.parse_args(list(argv) if argv is not None else None)
    root = os.path.abspath(args.root) if args.root else ROOT
    if args.action in ("run", "resume"):
        return run_goal(args.sid, root=root, worker=args.worker,
                        task_timeout=args.task_timeout,
                        max_tasks=args.max_tasks, retries=args.retries,
                        mode=args.action)
    if args.action == "status":
        return cmd_status(args, root)
    if args.action == "report":
        return cmd_report(args, root)
    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
