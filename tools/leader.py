#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leader —— 仓内指挥官：一句人话目标 → 里程碑 → 任务 DAG → 派发 → 回收 → 复盘。

为什么要有它（体检报告 P0-1 的落点）：执行层会执行、监督层会监督，但没有
「自己定目标、拆里程碑、派发回收」的层 —— 指挥官此前只存在于仓外
company.sh，不进 CLI 分发包、不受门链验证。leader 把这条链路**内化**：

  * 目标 → compile_goal()：确定性编译成带里程碑的菱形任务 DAG
    （t1 侦察 → t2 定案 →（t3 ∥ t4）→ t5 收尾），每任务带 milestone 归属
    与可验证 accept，工具全取 dsp.KNOWN_TOOLS 子集 —— 计划 JSON 自解释。
  * 落盘 → dsp.save_plan：work/plans/<sid>-plan.json + 哈希审计链。
  * 人审 → dsp.approve_plan；**未审批 leader run 拒跑**（指挥官不绕计划锁，
    拒绝时落 -block.md 留痕）。
  * 派发 → dsp.next_task 拓扑推进；每任务派一个 worker 子进程（默认
    headless agent，--worker 可换桩离线复验），任务边界消费 steer 老板
    指令（送进后续 worker prompt + 记跑账；只配送不裁决）。
  * 回收 → worker rc + 证据行经 dsp.task_record_step 写回计划、save_plan
    上链；失败任务阻塞其下游（DAG 语义，不装绿）。
  * 复盘 → work/leader/<sid>-RETRO.md（计划状态 + 派发回放 + steer + 结论）。

可见性自动成立：watch 读 work/plans + work/steer，leader 的计划与未消费
指令数直接进监督面，不需要第二套展示层。

用法（也经 `agentos leader` 原样转发）：
  python3 tools/leader.py plan "<目标>" [--sid S] [--tasks-json F]
  python3 tools/leader.py approve <sid>
  python3 tools/leader.py run <sid> [--worker "CMD {prompt}"] \
      [--task-timeout S] [--max-tasks N]
  python3 tools/leader.py retro <sid>

裁决：leader_gate 拿夹具证伪本模块（含「无 Leader 必红」变异体）；
本模块不做自证。
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MILESTONES = ("M1 侦察", "M2 定案", "M3 实施", "M4 收尾")

_MODS = {}


def _mods(root):
    """按 root 懒加载 dsp/steer（--root 注入时跟着换，变异副本可指真仓）。"""
    key = os.path.abspath(root)
    if key not in _MODS:
        tools = os.path.join(key, "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import dsp
        import steer
        _MODS[key] = (dsp, steer)
    return _MODS[key]


def compile_goal(goal, root=""):
    """人话目标 → 带里程碑的任务 DAG（确定性编译，零模型调用）。

    形状是菱形：t1 侦察 → t2 定案 →（t3 实施A ∥ t4 实施B）→ t5 收尾。
    扇出与扇入都在（t2 被两个任务依赖、t5 依赖两个任务）—— 派发层必须
    真做拓扑推进，不是把列表顺序当顺序跑。accept 全部写成可验证判据，
    经 dsp.validate_plan 全过才可能落盘。
    """
    dsp, _ = _mods(root or ROOT)
    g = (goal or "").strip()
    d = g[:80]
    tasks = [
        {"id": "t1", "milestone": MILESTONES[0],
         "title": "侦察：确认「%s」的现状与关键文件" % d,
         "accept": "关键文件已读 + 现状一句话 + rc=0",
         "tools": ["list_dir", "read_file", "search", "gh_search",
                   "gh_repo_view", "gh_file_read"],
         "depends": []},
        {"id": "t2", "milestone": MILESTONES[1],
         "title": "定案：圈改动面、每项钉成可复算判据",
         "accept": "改动面清单 + 每项判据可复算 + rc=0",
         "tools": ["read_file", "search", "run_cmd"],
         "depends": ["t1"]},
        {"id": "t3", "milestone": MILESTONES[2],
         "title": "实施A：主改动落盘",
         "accept": "改动落盘 + 验证命令 rc=0（证据行）",
         "tools": ["read_file", "edit_file", "write_file", "run_cmd",
                   "git_diff"],
         "depends": ["t2"]},
        {"id": "t4", "milestone": MILESTONES[2],
         "title": "实施B：接线/文档/回归配套",
         "accept": "配套落盘 + 相关自检 rc=0",
         "tools": ["read_file", "edit_file", "write_file", "search",
                   "git_diff"],
         "depends": ["t2"]},
        {"id": "t5", "milestone": MILESTONES[3],
         "title": "收尾：全链自检 + 回执",
         "accept": "自检 rc=0 + 回执含真实 rc",
         "tools": ["run_cmd", "git_diff", "git_status", "read_file"],
         "depends": ["t3", "t4"]},
    ]
    return dsp.new_plan(g, tasks)


def has_cycle(tasks):
    """Kahn 拓扑：dsp.validate_plan 只查 depends 指向存在、不查环 ——
    leader 在落盘前把环拒掉（有环 = 永远无可做任务 = 假 DAG）。"""
    ids = [t.get("id") for t in tasks]
    indeg = dict.fromkeys(ids, 0)
    adj = {i: [] for i in ids}
    for t in tasks:
        for dep in (t.get("depends") or []):
            if dep in indeg and t.get("id") in indeg:
                adj[dep].append(t.get("id"))
                indeg[t.get("id")] += 1
    q = [i for i in ids if indeg[i] == 0]
    seen = 0
    while q:
        n = q.pop()
        seen += 1
        for m in adj.get(n, []):
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    return seen != len(ids)


def pick_task(plan, attempted, root=""):
    """本轮下一个可派任务：dsp.next_task（拓扑序）+ 本轮已派过不再派。

    失败任务状态是 doing（未 done），next_task 还会再指向它 ——
    attempted 集合把「一次 run 里每个任务最多派一次」钉死，重试交给
    下一次 run（steps_used 继续累计，到 TASK_BUDGET 自然转 stuck）。
    """
    dsp, _ = _mods(root or ROOT)
    tid = dsp.next_task(plan)
    if not tid or tid in attempted:
        return ""
    return tid


def _leader_dir(root):
    return os.path.join(root, "work", "leader")


def ledger_append(root, sid, row):
    """跑账 append-only：run_start/dispatch/steer/run_end 逐行落盘。
    写失败静默（账是增益层），读账失败在 retro 如实标。"""
    r = dict(row)
    r["ts"] = round(time.time(), 3)
    try:
        d = _leader_dir(root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%s.jsonl" % sid), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def worker_argv(template, root, sid, tid, prompt):
    """派发命令：默认 headless agent 一次跑；{prompt} 占位=单 argv 传入。

    没写 {prompt} 时追加到末尾 —— 桩与真 worker 都按这一条契约接。
    其余 {py}/{root}/{sid}/{tid} 字面替换。
    """
    if not template:
        return [sys.executable,
                os.path.join(root, "tools", "agentos.py"),
                "agent", "-p", prompt]
    toks = shlex.split(template)
    out = []
    used = False
    for tk in toks:
        if tk == "{prompt}":
            out.append(prompt)
            used = True
        else:
            out.append(tk.replace("{py}", sys.executable)
                       .replace("{root}", root).replace("{sid}", sid)
                       .replace("{tid}", tid))
    if not used:
        out.append(prompt)
    return out


def render_dispatch(plan, task, notes):
    """派发书：目标/任务/判据/白名单/依赖 +（有则）老板指令 —— 自解释。"""
    lines = [
        "[leader 派发] 目标：%s" % (plan.get("goal") or "")[:160],
        "任务 %s（%s）：%s" % (task.get("id"), task.get("milestone") or "-",
                              task.get("title") or ""),
        "判据：%s" % (task.get("accept") or ""),
        "允许工具：%s" % ",".join(task.get("tools") or []),
        "依赖（已完成）：%s" % (",".join(task.get("depends") or []) or "-"),
    ]
    if notes:
        lines.append("老板指令（leader 送达，只配送不裁决）：")
        for n in notes:
            lines.append("  - %s" % str(n)[:120])
    lines.append("完成时给出证据行（含 rc=/PASS/FAIL/OK）。")
    return "\n".join(lines)


def _tail_line(path):
    """证据尾行：输出文件第一非空行（桩/真 worker 的 rc 自证行都在头部）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                s = raw.strip()
                if s:
                    return s[:200]
    except OSError:
        pass
    return ""


def cmd_plan(args, root):
    """建计划：compile_goal（或 --tasks-json）→ 环检查 → dsp.save_plan 上链。"""
    dsp, _ = _mods(root)
    goal = (args.goal or "").strip()
    if args.tasks_json:
        try:
            with open(args.tasks_json, encoding="utf-8") as f:
                tasks = json.load(f)
        except (OSError, ValueError) as e:
            sys.stderr.write("leader plan 拒：tasks-json 不可读（%s）\n"
                             % str(e)[:120])
            return 2
        if not isinstance(tasks, list) or not tasks:
            sys.stderr.write("leader plan 拒：tasks-json 必须是非空数组\n")
            return 2
        plan = dsp.new_plan(goal or "（--tasks-json 计划）", tasks)
        if has_cycle(plan.get("tasks") or []):
            sys.stderr.write("leader plan 拒：任务依赖有环（DAG 不成立）——"
                             "有环=永远无可做任务\n")
            return 2
    else:
        if not goal:
            sys.stderr.write("用法: leader plan \"<目标>\" [--sid S] "
                             "[--tasks-json F]\n")
            return 2
        plan = compile_goal(goal, root)
    sid = args.sid or ("leader-%d" % int(time.time()))
    ok_plan, err = dsp.save_plan(plan, sid)
    if not ok_plan:
        sys.stderr.write("leader plan 拒（%s）：%s\n" % (sid, err))
        return 2
    print("计划已落盘并上哈希链：sid=%s" % sid)
    print(dsp.render_plan(plan))
    print("下一步：agentos leader approve %s（leader run 未审批拒跑）" % sid)
    return 0


def cmd_approve(args, root):
    """人审放行：与 REPL /approve 同一把闸（dsp.approve_plan）。"""
    dsp, _ = _mods(root)
    ok_a, err = dsp.approve_plan(args.sid)
    if not ok_a:
        sys.stderr.write("leader approve 拒：%s\n" % (err or "未知"))
        return 2
    print("已审批：%s（写面/派发解锁）" % args.sid)
    return 0


def cmd_run(args, root):
    """拓扑派发：未审批拒跑 → 逐任务派 worker → 证据回收写回计划。"""
    dsp, steer = _mods(root)
    sid = args.sid
    plan, err = dsp.load_plan(sid)
    if plan is None:
        sys.stderr.write("leader run 拒：计划不可读（%s）\n"
                         % (err or "missing"))
        return 2
    if not plan.get("approved"):
        dsp.write_block(sid, "计划未审批：leader 拒绝派发（未审批=写面锁死，"
                             "指挥官不绕计划锁）", [],
                        "先 agentos leader approve %s（或 REPL /approve），"
                        "再 leader run" % sid)
        sys.stderr.write("leader run 拒：计划未审批（%s）—— 指挥官不绕"
                         "计划锁；先 approve\n" % sid)
        return 2
    evd = os.path.join(_leader_dir(root), sid)
    try:
        os.makedirs(evd, exist_ok=True)
    except OSError as e:
        sys.stderr.write("leader run 拒：证据目录不可建（%s）\n" % str(e)[:80])
        return 3
    ledger_append(root, sid, {"event": "run_start",
                              "goal": plan.get("goal", "")})
    attempted = set()
    t0 = time.time()
    print("leader run 开跑：sid=%s 任务=%d（worker=%s）"
          % (sid, len(plan.get("tasks") or []),
             args.worker or "默认 headless agent"))
    while len(attempted) < int(args.max_tasks):
        tid = pick_task(plan, attempted, root)
        if not tid:
            break
        # 任务边界：消费老板指令（只配送不裁决；送进本次派发书 + 记跑账）。
        notes = []
        try:
            items, total = steer.pending(os.path.join(root, "work", "steer"),
                                         sid)
            if items:
                notes = [str((x or {}).get("text") or "") for x in items]
                steer.commit(os.path.join(root, "work", "steer"), sid, total)
                ledger_append(root, sid, {"event": "steer", "n": len(notes),
                                          "texts": notes[:5]})
        except Exception as e:                                 # noqa: BLE001
            sys.stderr.write("[leader] steer 读取异常（忽略）：%s\n"
                             % str(e)[:80])
        task = next((t for t in (plan.get("tasks") or [])
                     if t.get("id") == tid), {})
        prompt = render_dispatch(plan, task, notes)
        argv = worker_argv(args.worker, root, sid, tid, prompt)
        outp = os.path.join(evd, "%s.out" % tid)
        ts = time.time()
        rc = 1
        try:
            with open(outp, "wb") as fh:
                p = subprocess.run(argv, stdout=fh,
                                   stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL,
                                   timeout=args.task_timeout, cwd=root)
            rc = int(p.returncode)
        except subprocess.TimeoutExpired:
            with open(outp, "a", encoding="utf-8") as fh:
                fh.write("\n[leader] 超时 %ss，任务判失败\n" % args.task_timeout)
            rc = 124
        except OSError as e:
            with open(outp, "a", encoding="utf-8") as fh:
                fh.write("\n[leader] 派发失败：%s\n" % str(e)[:160])
            rc = 127
        tail = _tail_line(outp)
        status = dsp.task_record_step(plan, tid, rc == 0,
                                      "rc=%d %s" % (rc, tail[:200]))
        dsp.save_plan(plan, sid)
        attempted.add(tid)
        dur = round(time.time() - ts, 1)
        ledger_append(root, sid, {"event": "dispatch", "task": tid, "rc": rc,
                                  "status": status, "dur": dur,
                                  "out": os.path.relpath(outp, root),
                                  "steer": len(notes)})
        print("  [%s] rc=%d → %s（%.1fs）" % (tid, rc, status, dur))
    remain = [t for t in (plan.get("tasks") or [])
              if t.get("status") != "done"]
    ledger_append(root, sid, {"event": "run_end",
                              "remain": [t.get("id") for t in remain],
                              "dur": round(time.time() - t0, 1)})
    if not remain:
        print("leader run 完成：%d/%d 任务全绿（证据 work/leader/%s/）"
              % (len(plan.get("tasks") or []), len(plan.get("tasks") or []),
                 sid))
        return 0
    sys.stderr.write("leader run 未完成：剩 %d 个任务（%s）—— 已如实落账；"
                     "证据与状态见 work/leader/%s/\n"
                     % (len(remain),
                        ",".join(str(t.get("id")) for t in remain), sid))
    return 1


def cmd_retro(args, root):
    """复盘：计划状态 + 跑账 + 证据逐行汇成一页，落 work/leader/<sid>-RETRO.md。"""
    dsp, _ = _mods(root)
    sid = args.sid
    plan, err = dsp.load_plan(sid)
    if plan is None:
        sys.stderr.write("leader retro 拒：计划不可读（%s）\n"
                         % (err or "missing"))
        return 2
    rows = []
    lp = os.path.join(_leader_dir(root), "%s.jsonl" % sid)
    try:
        with open(lp, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    try:
                        rows.append(json.loads(raw))
                    except ValueError:
                        pass
    except OSError:
        pass
    dispatches = [r for r in rows if r.get("event") == "dispatch"]
    steers = [r for r in rows if r.get("event") == "steer"]
    n_steer = sum(int(r.get("n") or 0) for r in steers)
    tasks = plan.get("tasks") or []
    done = [t for t in tasks if t.get("status") == "done"]
    rc_by = {r.get("task"): r for r in dispatches}
    lines = [
        "# leader 复盘 · %s" % sid,
        "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "- 目标：%s" % (plan.get("goal") or ""),
        "- 计划：approved=%s bind=%s"
        % ("yes" if plan.get("approved") else "no",
           str(plan.get("bind_sha") or "")[:12]),
        "",
        "## 任务账（计划状态 + 派发回放）",
    ]
    for t in tasks:
        r = rc_by.get(t.get("id")) or {}
        ev = ((t.get("evidence") or ["-"])[-1])[:120]
        lines.append("  - %s [%s] 证据：%s"
                     % (t.get("id"), t.get("status"), ev))
        lines.append("      派发 rc=%s 状态=%s 墙钟=%ss"
                     % (r.get("rc", "-"), r.get("status", "-"),
                        r.get("dur", "-")))
    lines += ["", "## steer（老板监督通道）",
              "- 累计消费：%d 条" % n_steer]
    for r in steers:
        for x in (r.get("texts") or []):
            lines.append("  - %s" % str(x)[:120])
    lines += ["", "## 结论",
              "- 全绿 %d/%d；未完成：%s"
              % (len(done), len(tasks),
                 ",".join(str(t.get("id")) for t in tasks
                          if t.get("status") != "done") or "无"),
              "", "## 下一步",
              "- 未完成任务逐个复跑：leader run %s；失败任务先看 "
              "work/leader/%s/<tid>.out 与判据再改" % (sid, sid)]
    p = os.path.join(_leader_dir(root), "%s-RETRO.md" % sid)
    try:
        os.makedirs(_leader_dir(root), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        sys.stderr.write("leader retro 落盘失败：%s\n" % str(e)[:120])
        return 3
    print("复盘已落盘：%s" % os.path.relpath(p, root))
    print("全绿 %d/%d · steer %d 条 · 派发 %d 次"
          % (len(done), len(tasks), n_steer, len(dispatches)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="leader",
        description="仓内指挥官：目标→里程碑→任务DAG→派发→回收→复盘"
                    "（计划锁/steer/门链同源纪律）")
    ap.add_argument("--root", default="",
                    help="仓库根（默认按本文件定位；测试注入）")
    sub = ap.add_subparsers(dest="action", metavar="动作")
    p1 = sub.add_parser("plan", help="人话目标 → 计划落盘上链（未审批）")
    p1.add_argument("goal", nargs="?", default="", help="一句人话目标")
    p1.add_argument("--sid", default="",
                    help="计划 id（默认 leader-<epoch>）")
    p1.add_argument("--tasks-json", default="",
                    help="自定义任务数组（JSON 文件）；缺省用内置里程碑模板")
    p2 = sub.add_parser("approve", help="人审放行（dsp.approve_plan）")
    p2.add_argument("sid")
    p3 = sub.add_parser("run", help="拓扑派发（未审批拒跑）")
    p3.add_argument("sid")
    p3.add_argument("--worker", default="",
                    help='派发命令模板（{prompt}/{root}/{sid}/{tid}/{py}）；'
                         '缺省 "agentos agent -p <prompt>"')
    p3.add_argument("--task-timeout", type=int, default=300)
    p3.add_argument("--max-tasks", type=int, default=12)
    p4 = sub.add_parser("retro", help="复盘落盘（计划+跑账+证据）")
    p4.add_argument("sid")
    args = ap.parse_args(list(argv) if argv is not None else None)
    root = os.path.abspath(args.root) if args.root else ROOT
    if args.action == "plan":
        return cmd_plan(args, root)
    if args.action == "approve":
        return cmd_approve(args, root)
    if args.action == "run":
        return cmd_run(args, root)
    if args.action == "retro":
        return cmd_retro(args, root)
    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
