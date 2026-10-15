#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autonomy_gate —— W13 自主执行门：一次审批后全流程无人干预收口的独立裁决。

为什么必须有这道门（「能全流程自己完成事情」的验收口径）：leader_gate 证明
了会拆活，但「派发之后」这一段——判据真跑没真跑、失败自救了没、封顶封没
封、断点能不能续、假成功会不会混进 done——在仓内此前零门覆盖。本门站在外
面认这些事实（纯离线、桩 worker、不连模型、不起端口）：

    D1  纯面：--help rc=0；分类表确定性四路；6 个变异体锚点各恰 1 处
    D2  负控面：有环计划拒跑（rc=2 + 零派发 + 零落账）—— 假 DAG 不派发
    D3  锁面（核心）：未审批 run 必拒（rc=2 + 未审批字样 + 零派发 +
        -block.md 落痕）—— 自主引擎不绕计划锁
    D4  全流程面：审批后一次 CLI 调用跑完 5 任务菱形 DAG：拓扑序、每任务
        verify 真跑（marker 物证）、done 全含 verify rc=0、报告落盘
    D5  自救面：t1 判据先红后绿 → 带修法重试第 2 次转绿 done；跑账留
        kind=verify 与两次尝试；修错提示真进了派发书（桩日志物证）
    D6  封顶+DAG 面：b 恒败 → 恰好 1+2=3 次派发后 blocked；下游 c 不派发；
        独立分支 d 照常 done；rc=1 如实报剩余
    D7  续跑面：t2 派发中途强杀（SIGKILL 进程组）→ resume 从断点续；
        已完成 t1 零重跑（派发行=1）；终局全绿 rc=0；run_start=2
    D8  预算面：--max-tasks 1 边界干净停（rc=1 + 剩 4 如实报）→ 再跑续完
        rc=0；同一账本两段 run_start
    D9  判据面（反假绿）：worker 自称 rc=0 但 verify 恒红 → 不 done（blocked）；
        worker rc=1 但 verify 绿 → 也不 done（双闸）；任何 done 的证据必须
        含 rc=0
    D10 监督面：任务边界消费 steer（进派发书 + 跑账 + 游标只进）
    D11 CLI 面：status rc=0（计划+跑账尾）；report rc=0（结论/下一步段齐）
    D12 零污染：夹具按 sid 精准清；work/ 只多 --out；tools/ 零残留

用法: python3 tools/autonomy_gate.py [--autonomy P] [--out P]
裁决：AUTONOMY_GATE OK n/n（rc=0）/ AUTONOMY_GATE RED（rc=2，有漏网）
"""
import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_AUTONOMY = os.path.join(HERE, "autonomy.py")
DEFAULT_LEADER = os.path.join(HERE, "leader.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "autonomy_gate.json")

AUTONOMY = DEFAULT_AUTONOMY
LEADER = DEFAULT_LEADER

results = []

STUB_SRC = '''#!/usr/bin/env python3
"""autonomy_gate 桩 worker：按派发书干活的可复现替身（零模型零网络）。"""
import os
import re
import sys
import time

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
m = re.search(r"任务 ([A-Za-z0-9_]+)", prompt)
tid = m.group(1) if m else "?"
lg = os.environ.get("AUT_STUB_LOG", "")
if lg:
    with open(lg, "a", encoding="utf-8") as f:
        f.write("TASK=%s\\n%s\\n--8<--\\n" % (tid, prompt))
mk = os.environ.get("AUT_STUB_MARKS", "")
if mk:
    os.makedirs(mk, exist_ok=True)
    with open(os.path.join(mk, "w_t_%s" % tid), "w", encoding="utf-8") as f:
        f.write("ok")
if os.environ.get("AUT_STUB_SLEEP") == tid:
    if mk:
        with open(os.path.join(mk, "sleeping_%s" % tid), "w",
                  encoding="utf-8") as f:
            f.write("zzz")
    time.sleep(30)
if os.environ.get("AUT_STUB_FAIL") == tid:
    print("STUB FAIL %s rc=1 故意失败" % tid)
    sys.exit(1)
print("STUB OK %s rc=0" % tid)
'''

VONE_SRC = '''#!/usr/bin/env python3
"""autonomy_gate 判据命令替身：确定性 rc 控制（green/red/flip/touch）。"""
import os
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "green"
arg = sys.argv[2] if len(sys.argv) > 2 else ""
if mode == "touch":
    with open(arg, "w", encoding="utf-8") as f:
        f.write("v")
    print("VERIFY touch ok rc=0")
    sys.exit(0)
if mode == "green":
    print("VERIFY GREEN rc=0")
    sys.exit(0)
if mode == "red":
    print("VERIFY RED rc=1")
    sys.exit(1)
if mode == "flip":
    n = 0
    if os.path.exists(arg):
        try:
            n = int(open(arg, encoding="utf-8").read().strip() or "0")
        except ValueError:
            n = 0
    with open(arg, "w", encoding="utf-8") as f:
        f.write(str(n + 1))
    if n == 0:
        print("VERIFY FLIP 第一次 red rc=1")
        sys.exit(1)
    print("VERIFY FLIP 转绿 rc=0")
    sys.exit(0)
print("VERIFY 未知模式 rc=1")
sys.exit(1)
'''

# 变异体锚点：autonomy_selftest 逐条改写；本门先钉「各恰 1 处」（防漂）。
ANCHORS = [
    "AUTONOMY_RETRY = 2",
    '    if not plan.get("approved"):',
    "        tid = dsp.next_task(plan)",
    "        while attempt < 1 + retries:",
    "            v_ok, v_note = _run_verify(task, root)",
    "    if stalled:",
]


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(("PASS " if ok else "FAIL ") + name
          + ("  -- " + str(detail)[:150] if detail else ""))


def load_mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_autonomy(*argv, env=None, timeout=300):
    cmd = [sys.executable, AUTONOMY, "--root", ROOT] + list(argv)
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def _read_ledger(sid):
    rows = []
    p = os.path.join(ROOT, "work", "autonomy", "%s.jsonl" % sid)
    try:
        with open(p, encoding="utf-8") as f:
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


def _unlink(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _rmdir_if_empty(p):
    try:
        os.rmdir(p)
    except OSError:
        pass


def cleanup_fixtures(sids):
    """清本门自己造的夹具（按 sid 精准清，绝不扫射）。"""
    for sid in sids:
        _unlink(
            os.path.join(ROOT, "work", "plans", "%s-plan.json" % sid),
            os.path.join(ROOT, "work", "plans", "%s-chain.jsonl" % sid),
            os.path.join(ROOT, "work", "plans", "%s-block.md" % sid),
            os.path.join(ROOT, "work", "autonomy", "%s.jsonl" % sid),
            os.path.join(ROOT, "work", "autonomy", "%s-REPORT.md" % sid),
            os.path.join(ROOT, "work", "steer", "%s.jsonl" % sid),
            os.path.join(ROOT, "work", "steer", "%s.cursor" % sid),
        )
        shutil.rmtree(os.path.join(ROOT, "work", "autonomy", sid),
                      ignore_errors=True)
    _rmdir_if_empty(os.path.join(ROOT, "work", "autonomy"))
    _rmdir_if_empty(os.path.join(ROOT, "work", "steer"))


def _finish(out_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"autonomy": os.path.relpath(AUTONOMY, ROOT),
                   "judgments": results, "n_ok": n_ok,
                   "n_total": len(results)}, fh,
                  ensure_ascii=False, indent=2, sort_keys=True)
    print("AUTONOMY_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("AUTONOMY_GATE_OK")
        return 0
    print("AUTONOMY_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def _mk_plan(sid, goal, tasks):
    import dsp as _dsp
    plan = _dsp.new_plan(goal, tasks)
    ok, err = _dsp.save_plan(plan, sid)
    return ok, err, plan


def main():
    global AUTONOMY, LEADER
    ap = argparse.ArgumentParser()
    ap.add_argument("--autonomy", default=DEFAULT_AUTONOMY)
    ap.add_argument("--leader", default=DEFAULT_LEADER)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    AUTONOMY = os.path.abspath(args.autonomy)
    LEADER = os.path.abspath(args.leader)
    print("== autonomy_gate：自主执行引擎（派发→验证→自救→续跑→收口）")

    work = os.path.join(ROOT, "work")
    before_w = set(os.listdir(work)) if os.path.isdir(work) else set()
    tools = os.path.join(ROOT, "tools")
    before_t = set(os.listdir(tools))
    try:
        asrc = open(AUTONOMY, encoding="utf-8").read()
    except OSError as e:
        print("AUTONOMY_GATE_RED 读不到文件：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="autgate_")
    pf = "au%d" % os.getpid()
    sid_main, sid_fail, sid_cap = pf + "-main", pf + "-fail", pf + "-cap"
    sid_kill, sid_bud, sid_judge = pf + "-kill", pf + "-bud", pf + "-judge"
    sid_cyc = pf + "-cyc"
    stub = os.path.join(tmp, "stub.py")
    vone = os.path.join(tmp, "vone.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(STUB_SRC)
    with open(vone, "w", encoding="utf-8") as f:
        f.write(VONE_SRC)
    worker = '"%s" %s {prompt}' % (sys.executable, stub)
    marks = os.path.join(tmp, "marks")
    slog = os.path.join(tmp, "stub_log.txt")
    env = dict(os.environ)
    env["AUT_STUB_MARKS"] = marks
    env["AUT_STUB_LOG"] = slog

    def _vcmd(mode, arg=None):
        s = '"%s" "%s" %s' % (sys.executable, vone, mode)
        if arg:
            s += ' "%s"' % arg
        return s

    try:
        sys.path.insert(0, HERE)
        import dsp as _dsp

        # D1 纯面：--help + 分类表 + 锚点齐
        try:
            ph = subprocess.run([sys.executable, AUTONOMY, "--help"],
                                capture_output=True, text=True, timeout=60)
            A = load_mod(AUTONOMY, "autonomy_g")
            c1 = A.classify_failure(124, "", None, "")
            c2 = A.classify_failure(0, "x", False, "verify rc=1")
            c3 = A.classify_failure(0, "预算用尽：达到 step 预算 24", None, "")
            c4 = A.classify_failure(127, "", None, "")
            kinds = (c1[0], c2[0], c3[0], c4[0])
            anchors_n = [asrc.count(a) for a in ANCHORS]
            gate("D1 纯面：--help rc=0 + 分类表（timeout/verify/budget/"
                 "notfound）+ 6 锚点各 1 处",
                 ph.returncode == 0
                 and kinds == ("timeout", "verify", "budget", "notfound")
                 and all(hint for _, hint in (c1, c2, c3, c4))
                 and isinstance(A.AUTONOMY_RETRY, int)
                 and A.AUTONOMY_RETRY >= 1
                 and anchors_n == [1] * len(ANCHORS),
                 "rc=%s kinds=%s anchors=%s" % (ph.returncode, kinds,
                                                anchors_n))
        except Exception as e:                                 # noqa: BLE001
            gate("D1 纯面：--help rc=0 + 分类表（timeout/verify/budget/"
                 "notfound）+ 6 锚点各 1 处", False, repr(e)[:120])

        # D2 负控面：有环计划拒跑
        try:
            okc, errc, _pc = _mk_plan(sid_cyc, "环夹具", [
                {"id": "c1", "title": "环1", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": ["c2"]},
                {"id": "c2", "title": "环2", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": ["c1"]},
            ])
            _dsp.approve_plan(sid_cyc)      # 锁面在环守卫之前，先过锁
            p = run_autonomy("run", sid_cyc, "--worker", worker, env=env)
            rows = _read_ledger(sid_cyc)
            gate("D2 负控面：有环计划 rc=2 拒跑 + 零派发 + 零落账",
                 okc and p.returncode == 2 and "环" in (p.stderr or "")
                 and not rows,
                 "rc=%s 环=%s rows=%d"
                 % (p.returncode, "环" in (p.stderr or ""), len(rows)))
        except Exception as e:                                 # noqa: BLE001
            gate("D2 负控面：有环计划 rc=2 拒跑 + 零派发 + 零落账",
                 False, repr(e)[:120])

        # D3 锁面：未审批拒跑（主夹具先建好，未审批）
        try:
            L = load_mod(LEADER, "leader_ag")
            plan = L.compile_goal("自主门夹具：把自主执行全链跑通并留证据")
            for t in plan["tasks"]:
                t["verify"] = [_vcmd("touch", "%s/v_t_%s" % (tmp, t["id"]))]
            okp, errp = _dsp.save_plan(plan, sid_main)
            p = run_autonomy("run", sid_main, "--worker", worker, env=env)
            rows = _read_ledger(sid_main)
            bpath = os.path.join(ROOT, "work", "plans",
                                 "%s-block.md" % sid_main)
            msg = (p.stdout or "") + (p.stderr or "")
            gate("D3 锁面：未审批 run 必拒（rc=2 + 未审批 + 零派发 + block）",
                 okp and p.returncode == 2 and "未审批" in msg
                 and not any(r.get("event") == "dispatch" for r in rows)
                 and os.path.exists(bpath),
                 "rc=%s 未审批=%s dispatch=%d block=%s"
                 % (p.returncode, "未审批" in msg,
                    sum(1 for r in rows if r.get("event") == "dispatch"),
                    os.path.exists(bpath)))
        except Exception as e:                                 # noqa: BLE001
            gate("D3 锁面：未审批 run 必拒（rc=2 + 未审批 + 零派发 + block）",
                 False, repr(e)[:120])

        # D4 全流程面：steer 先入队 → 审批 → 一次调用跑完 5/5
        try:
            import steer as _steer
            box = os.path.join(ROOT, "work", "steer")
            _steer.push(box, sid_main, "先跑侦察，写面等我确认")
            _pa = _dsp.approve_plan(sid_main)
            p = run_autonomy("run", sid_main, "--worker", worker, env=env)
            ld, _e = _dsp.load_plan(sid_main)
            tasks = (ld or {}).get("tasks") or []
            rows = _read_ledger(sid_main)
            order = [r.get("task") for r in rows if r.get("event") == "dispatch"]
            ev_ok = all(t.get("evidence")
                        and "verify rc=0" in str(t["evidence"][-1])
                        and "rc=0" in str(t["evidence"][-1]) for t in tasks)
            outs = all(os.path.exists(os.path.join(
                ROOT, "work", "autonomy", sid_main, "%s.a1.out" % t.get("id")))
                for t in tasks)
            vmarks = [nm for nm in (os.listdir(tmp) if os.path.isdir(tmp)
                                    else []) if nm.startswith("v_t_")]
            rpath = os.path.join(ROOT, "work", "autonomy",
                                 "%s-REPORT.md" % sid_main)
            rtxt = ""
            try:
                rtxt = open(rpath, encoding="utf-8").read()
            except OSError:
                pass
            gate("D4 全流程面：一次调用 5/5 全绿 + 拓扑序 + verify 真跑"
                 "（marker）+ 报告落盘",
                 _pa[0] and p.returncode == 0
                 and len(tasks) == 5
                 and all(t.get("status") == "done" for t in tasks)
                 and order == ["t1", "t2", "t3", "t4", "t5"]
                 and ev_ok and outs and len(vmarks) == 5
                 and "5/5" in (p.stdout or "")
                 and "全绿 5/5" in rtxt,
                 "rc=%s order=%s ev=%s vmarks=%d report=%s"
                 % (p.returncode, order, ev_ok, len(vmarks),
                    "全绿 5/5" in rtxt))
        except Exception as e:                                 # noqa: BLE001
            gate("D4 全流程面：一次调用 5/5 全绿 + 拓扑序 + verify 真跑"
                 "（marker）+ 报告落盘", False, repr(e)[:120])

        # D5 自救面：t1 判据先红后绿 → 带修法重试转绿
        try:
            plan = L.compile_goal("自救夹具：判据先红后绿")
            cnt = os.path.join(tmp, "flip_cnt")
            for t in plan["tasks"]:
                if t["id"] == "t1":
                    t["verify"] = [_vcmd("flip", cnt)]
                else:
                    t["verify"] = [_vcmd("green")]
            okp, errp = _dsp.save_plan(plan, sid_fail)
            _dsp.approve_plan(sid_fail)
            p = run_autonomy("run", sid_fail, "--worker", worker, env=env)
            ld, _e = _dsp.load_plan(sid_fail)
            tasks = (ld or {}).get("tasks") or []
            rows = [r for r in _read_ledger(sid_fail)
                    if r.get("event") == "dispatch" and r.get("task") == "t1"]
            try:
                slogtxt = open(slog, encoding="utf-8").read()
            except OSError:
                slogtxt = ""
            gate("D5 自救面：判据先红后绿 → 第 2 次尝试转绿 done + 修法真进"
                 "派发书",
                 okp and p.returncode == 0
                 and all(t.get("status") == "done" for t in tasks)
                 and len(rows) == 2
                 and "rc=1" in str(rows[0].get("verify"))
                 and "rc=0" in str(rows[1].get("verify"))
                 and rows[0].get("kind") == "verify"
                 and "上一轮失败与修法" in slogtxt
                 and "判据命令没过" in slogtxt,
                 "rc=%s t1次数=%s v0=%s kind=%s 提示=%s"
                 % (p.returncode, len(rows),
                    rows[0].get("verify") if rows else "-",
                    rows[0].get("kind") if rows else "-",
                    "上一轮失败与修法" in slogtxt))
        except Exception as e:                                 # noqa: BLE001
            gate("D5 自救面：判据先红后绿 → 第 2 次尝试转绿 done + 修法真进"
                 "派发书", False, repr(e)[:120])

        # D6 封顶+DAG 面：b 恒败 → 3 次封顶；下游 c 不派发；独立 d 照常
        try:
            okp, errp, _pc = _mk_plan(sid_cap, "封顶夹具", [
                {"id": "a", "title": "A", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": [],
                 "verify": [_vcmd("green")]},
                {"id": "b", "title": "B", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": ["a"],
                 "verify": [_vcmd("green")]},
                {"id": "c", "title": "C", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": ["b"],
                 "verify": [_vcmd("green")]},
                {"id": "d", "title": "D", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": [],
                 "verify": [_vcmd("green")]},
            ])
            _dsp.approve_plan(sid_cap)
            env_f = dict(env)
            env_f["AUT_STUB_FAIL"] = "b"
            p = run_autonomy("run", sid_cap, "--worker", worker, env=env_f)
            ld, _e = _dsp.load_plan(sid_cap)
            tmap = {t.get("id"): t for t in ((ld or {}).get("tasks") or [])}
            rows = [r for r in _read_ledger(sid_cap)
                    if r.get("event") == "dispatch"]
            n_by = {}
            for r in rows:
                n_by[r.get("task")] = n_by.get(r.get("task"), 0) + 1
            bl = [r for r in _read_ledger(sid_cap)
                  if r.get("event") == "blocked"]
            gate("D6 封顶+DAG 面：恒败恰好 1+2=3 次封顶 blocked + 下游 c 不"
                 "派发 + 独立 d 照常 done",
                 okp and p.returncode == 1
                 and n_by.get("a") == 1 and n_by.get("b") == 3
                 and n_by.get("d") == 1 and not n_by.get("c")
                 and tmap.get("a", {}).get("status") == "done"
                 and tmap.get("b", {}).get("status") == "blocked"
                 and tmap.get("c", {}).get("status") == "todo"
                 and tmap.get("d", {}).get("status") == "done"
                 and len(bl) == 1 and bl[0].get("kind") == "fail"
                 and "封顶 3 次" in (p.stdout or "")
                 and "剩 2" in (p.stderr or ""),
                 "rc=%s n_by=%s b=%s c=%s d=%s blocked=%s"
                 % (p.returncode, n_by,
                    tmap.get("b", {}).get("status"),
                    tmap.get("c", {}).get("status"),
                    tmap.get("d", {}).get("status"), len(bl)))
        except Exception as e:                                 # noqa: BLE001
            gate("D6 封顶+DAG 面：恒败恰好 1+2=3 次封顶 blocked + 下游 c 不"
                 "派发 + 独立 d 照常 done", False, repr(e)[:120])

        # D7 续跑面：t2 派发中途强杀 → resume；t1 零重跑
        try:
            plan = L.compile_goal("续跑夹具：强杀后从断点续")
            for t in plan["tasks"]:
                t["verify"] = [_vcmd("green")]
            okp, errp = _dsp.save_plan(plan, sid_kill)
            _dsp.approve_plan(sid_kill)
            env_k = dict(env)
            env_k["AUT_STUB_SLEEP"] = "t2"
            cmd = [sys.executable, AUTONOMY, "--root", ROOT, "run", sid_kill,
                   "--worker", worker, "--task-timeout", "60"]
            pp = subprocess.Popen(cmd, env=env_k, start_new_session=True,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
            marker = os.path.join(marks, "sleeping_t2")
            seen = False
            for _ in range(100):          # ≤10s 等 t2 真进派发
                if os.path.exists(marker):
                    seen = True
                    break
                if pp.poll() is not None:
                    break
                time.sleep(0.1)
            try:
                os.killpg(os.getpgid(pp.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                pp.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pp.kill()
            p2 = run_autonomy("resume", sid_kill, "--worker", worker, env=env)
            ld, _e = _dsp.load_plan(sid_kill)
            tasks = (ld or {}).get("tasks") or []
            rows = _read_ledger(sid_kill)
            n1 = sum(1 for r in rows if r.get("event") == "dispatch"
                     and r.get("task") == "t1")
            n_start = sum(1 for r in rows if r.get("event") == "run_start")
            gate("D7 续跑面：派发中途 SIGKILL 强杀 → resume 续跑 + t1 零重跑"
                 " + 终局全绿",
                 okp and seen and p2.returncode == 0
                 and all(t.get("status") == "done" for t in tasks)
                 and n1 == 1 and n_start == 2,
                 "seen=%s rc2=%s t1累计派发=%d run_start=%d"
                 % (seen, p2.returncode, n1, n_start))
        except Exception as e:                                 # noqa: BLE001
            gate("D7 续跑面：派发中途 SIGKILL 强杀 → resume 续跑 + t1 零重跑"
                 " + 终局全绿", False, repr(e)[:120])

        # D8 预算面：--max-tasks 1 边界停 + 再跑续完
        try:
            plan = L.compile_goal("预算夹具：边界停再续")
            for t in plan["tasks"]:
                t["verify"] = [_vcmd("green")]
            okp, errp = _dsp.save_plan(plan, sid_bud)
            _dsp.approve_plan(sid_bud)
            p1 = run_autonomy("run", sid_bud, "--worker", worker,
                              "--max-tasks", "1", env=env)
            ld1, _e = _dsp.load_plan(sid_bud)
            tmap1 = {t.get("id"): t.get("status")
                     for t in ((ld1 or {}).get("tasks") or [])}
            p2 = run_autonomy("run", sid_bud, "--worker", worker, env=env)
            ld2, _e = _dsp.load_plan(sid_bud)
            tasks2 = (ld2 or {}).get("tasks") or []
            n_start = sum(1 for r in _read_ledger(sid_bud)
                          if r.get("event") == "run_start")
            gate("D8 预算面：--max-tasks 1 干净停（rc=1 剩 4）→ 再跑续完"
                 "（rc=0 全绿 + 两段 run_start）",
                 okp and p1.returncode == 1 and "剩 4" in (p1.stderr or "")
                 and tmap1.get("t1") == "done"
                 and sum(1 for v in tmap1.values() if v == "done") == 1
                 and p2.returncode == 0
                 and all(t.get("status") == "done" for t in tasks2)
                 and n_start == 2,
                 "rc1=%s 剩4=%s rc2=%s run_start=%d done1=%d"
                 % (p1.returncode, "剩 4" in (p1.stderr or ""),
                    p2.returncode, n_start,
                    sum(1 for v in tmap1.values() if v == "done")))
        except Exception as e:                                 # noqa: BLE001
            gate("D8 预算面：--max-tasks 1 干净停（rc=1 剩 4）→ 再跑续完"
                 "（rc=0 全绿 + 两段 run_start）", False, repr(e)[:120])

        # D9 判据面（反假绿）：自称绿但判据红 / 判据绿但 worker 红
        try:
            okp, errp, _pj = _mk_plan(sid_judge, "判据夹具", [
                {"id": "j1", "title": "自称绿", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": [],
                 "verify": [_vcmd("red")]},
                {"id": "j2", "title": "worker 红", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": [],
                 "verify": [_vcmd("green")]},
                {"id": "j3", "title": "真绿", "accept": "rc=0",
                 "tools": ["run_cmd"], "depends": [],
                 "verify": [_vcmd("green")]},
            ])
            _dsp.approve_plan(sid_judge)
            env_j = dict(env)
            env_j["AUT_STUB_FAIL"] = "j2"
            p = run_autonomy("run", sid_judge, "--worker", worker, env=env_j)
            ld, _e = _dsp.load_plan(sid_judge)
            tmap = {t.get("id"): t for t in ((ld or {}).get("tasks") or [])}
            j1_ev = str((tmap.get("j1", {}).get("evidence") or [""])[-1])
            j2_ev = str((tmap.get("j2", {}).get("evidence") or [""])[-1])
            falsy = [t.get("id") for t in tmap.values()
                     if t.get("status") == "done"
                     and "rc=0" not in str((t.get("evidence") or [""])[-1])]
            gate("D9 判据面（反假绿）：自称 rc=0 但 verify 红 → 不 done；"
                 "worker rc=1 但 verify 绿 → 不 done；done 必含 rc=0",
                 okp and p.returncode == 1
                 and tmap.get("j1", {}).get("status") == "blocked"
                 and "verify rc=1" in j1_ev
                 and tmap.get("j2", {}).get("status") == "blocked"
                 and "rc=1" in j2_ev
                 and tmap.get("j3", {}).get("status") == "done"
                 and not falsy,
                 "j1=%s j2=%s j3=%s falsy=%s"
                 % (tmap.get("j1", {}).get("status"),
                    tmap.get("j2", {}).get("status"),
                    tmap.get("j3", {}).get("status"), falsy or "无"))
        except Exception as e:                                 # noqa: BLE001
            gate("D9 判据面（反假绿）：自称 rc=0 但 verify 红 → 不 done；"
                 "worker rc=1 但 verify 绿 → 不 done；done 必含 rc=0",
                 False, repr(e)[:120])

        # D10 监督面：steer 边界消费（读 D4 跑出来的物证）
        try:
            box = os.path.join(ROOT, "work", "steer")
            pend_now, _tot = _steer.pending(box, sid_main)
            rows = _read_ledger(sid_main)
            steer_rows = [r for r in rows if r.get("event") == "steer"]
            try:
                slogtxt = open(slog, encoding="utf-8").read()
            except OSError:
                slogtxt = ""
            gate("D10 监督面：steer 边界消费 + 进派发书 + 游标只进",
                 not pend_now and len(steer_rows) == 1
                 and int(steer_rows[0].get("n") or 0) == 1
                 and "老板指令" in slogtxt
                 and "先跑侦察，写面等我确认" in slogtxt
                 and os.path.exists(os.path.join(box,
                                                 "%s.cursor" % sid_main)),
                 "pending=%d steer_rows=%d in_prompt=%s"
                 % (len(pend_now), len(steer_rows), "老板指令" in slogtxt))
        except Exception as e:                                 # noqa: BLE001
            gate("D10 监督面：steer 边界消费 + 进派发书 + 游标只进",
                 False, repr(e)[:120])

        # D11 CLI 面：status / report
        try:
            ps = run_autonomy("status", sid_main, env=env)
            pr = run_autonomy("report", sid_main, env=env)
            rpath = os.path.join(ROOT, "work", "autonomy",
                                 "%s-REPORT.md" % sid_main)
            rtxt = ""
            try:
                rtxt = open(rpath, encoding="utf-8").read()
            except OSError:
                pass
            gate("D11 CLI 面：status rc=0（计划+跑账尾）+ report rc=0"
                 "（结论/下一步段齐）",
                 ps.returncode == 0 and "t1" in (ps.stdout or "")
                 and "跑账尾" in (ps.stdout or "")
                 and pr.returncode == 0
                 and "## 结论" in rtxt and "下一步" in rtxt
                 and "autonomy.py resume" in rtxt,
                 "status=%s report=%s 结论=%s"
                 % (ps.returncode, pr.returncode, "## 结论" in rtxt))
        except Exception as e:                                 # noqa: BLE001
            gate("D11 CLI 面：status rc=0（计划+跑账尾）+ report rc=0"
                 "（结论/下一步段齐）", False, repr(e)[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # D12 零污染：清夹具后 work/ 只多 --out、tools/ 零残留、夹具全清
    cleanup_fixtures([sid_main, sid_fail, sid_cap, sid_kill, sid_bud,
                      sid_judge, sid_cyc])
    after_w = set(os.listdir(work)) if os.path.isdir(work) else set()
    after_t = set(os.listdir(tools))
    new_w = sorted((after_w - before_w)
                   - {os.path.basename(os.path.abspath(args.out))})
    new_t = sorted(after_t - before_t)
    left = []
    for d in (os.path.join(ROOT, "work", "plans"),
              os.path.join(ROOT, "work", "autonomy")):
        try:
            left += [os.path.join(d, nm) for nm in os.listdir(d)
                     if nm.startswith(pf)]
        except OSError:
            pass
    gate("D12 零污染：work/ 只多 --out；tools/ 零残留；夹具按 sid 全清",
         not new_w and not new_t and not left,
         "新增work=%s 新增tools=%s 残留=%s"
         % (new_w or "无", new_t or "无", left or "无"))

    return _finish(args.out)


if __name__ == "__main__":
    sys.exit(main())
