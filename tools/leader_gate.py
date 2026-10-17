#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leader_gate —— W11 指挥官门：目标→里程碑→DAG→派发→回收→复盘 全链可证伪。

为什么必须有这道门（体检报告 P0-1 验收口径）：领导层此前只在仓外
（company.sh），「会不会自己定目标、拆里程碑、派发回收」在仓内**零门
覆盖** —— 没有一道门证明这是产品能力而不是想象。本门站在外面认这些
事实（纯离线、桩 worker、不连模型、不起端口）：

    D1  编译面（纯）：compile_goal → dsp.validate_plan 全过；菱形 DAG 真
        扇出扇入；每任务带里程碑归属；工具全在 KNOWN_TOOLS
    D2  落盘面：leader plan → work/plans/<sid>-plan.json + 哈希链 save 行
    D3  锁面（核心）：未审批 leader run 必拒（rc≠0 + 未审批字样 + 零派发
        + -block.md 落痕）—— 指挥官不绕计划锁
    D4  派发-回收面：approve 后 run 走桩 worker → 5/5 全绿；拓扑序
        t1→t2→t3/t4→t5；每任务证据文件落盘、计划 evidence 含 rc=0
    D5  失败传播面（乱序 DAG）：桩只让 t2 失败 → t1 done、t2 未 done、
        t3 未派发（依赖未成不抢跑）；run rc≠0 如实报未完成
    D6  监督串联面：任务边界消费 steer（送进派发书 + 跑账 + 游标只进）
    D7  复盘面：retro 落 RETRO.md，任务账逐行含 rc 证据、结论/steer 段齐
    D8  CLI 面：agentos leader --help rc=0；真 CLI plan 端到端；锚点齐
    D9  负控面：坏工具名计划必拒；有环 DAG 必拒（dsp 存时拒收在前、leader 兜底在后）
    D10 零污染：跑完 work/ 只多 --out；tools/ 零残留；夹具全清

用法: python3 tools/leader_gate.py [--leader P] [--agentos P] [--out P]
裁决：LEADER_GATE OK n/n（rc=0）/ LEADER_GATE RED（rc=2，有漏网）
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_LEADER = os.path.join(HERE, "leader.py")
DEFAULT_AGENTOS = os.path.join(HERE, "agentos.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "leader_gate.json")

LEADER = DEFAULT_LEADER
AGENTOS = DEFAULT_AGENTOS

results = []

STUB_SRC = '''#!/usr/bin/env python3
"""leader_gate 桩 worker：按派发书干活的可复现替身（零模型零网络）。"""
import os
import re
import sys

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
m = re.search(r"任务 ([A-Za-z0-9_]+)", prompt)
tid = m.group(1) if m else "?"
lg = os.environ.get("LEADER_STUB_LOG", "")
if lg:
    with open(lg, "a", encoding="utf-8") as f:
        f.write("TASK=%s\\n%s\\n--8<--\\n" % (tid, prompt))
mk = os.environ.get("LEADER_STUB_MARKS", "")
if mk:
    os.makedirs(mk, exist_ok=True)
    with open(os.path.join(mk, tid), "w", encoding="utf-8") as f:
        f.write("ok")
if os.environ.get("LEADER_STUB_FAIL") == tid:
    print("STUB FAIL %s rc=1 故意失败" % tid)
    sys.exit(1)
print("STUB OK %s rc=0" % tid)
'''


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(("PASS " if ok else "FAIL ") + name
          + ("  -- " + str(detail)[:150] if detail else ""))


def load_mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_leader(*argv, env=None, timeout=180):
    cmd = [sys.executable, LEADER, "--root", ROOT] + list(argv)
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def _read_ledger(sid):
    rows = []
    p = os.path.join(ROOT, "work", "leader", "%s.jsonl" % sid)
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
            os.path.join(ROOT, "work", "leader", "%s.jsonl" % sid),
            os.path.join(ROOT, "work", "leader", "%s-RETRO.md" % sid),
            os.path.join(ROOT, "work", "steer", "%s.jsonl" % sid),
            os.path.join(ROOT, "work", "steer", "%s.cursor" % sid),
        )
        shutil.rmtree(os.path.join(ROOT, "work", "leader", sid),
                      ignore_errors=True)
    _rmdir_if_empty(os.path.join(ROOT, "work", "leader"))
    _rmdir_if_empty(os.path.join(ROOT, "work", "steer"))


def _finish(out_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"leader": os.path.relpath(LEADER, ROOT),
                   "judgments": results, "n_ok": n_ok,
                   "n_total": len(results)}, fh,
                  ensure_ascii=False, indent=2, sort_keys=True)
    print("LEADER_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("LEADER_GATE_OK")
        return 0
    print("LEADER_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    global LEADER, AGENTOS
    ap = argparse.ArgumentParser()
    ap.add_argument("--leader", default=DEFAULT_LEADER)
    ap.add_argument("--agentos", default=DEFAULT_AGENTOS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    LEADER = os.path.abspath(args.leader)
    AGENTOS = os.path.abspath(args.agentos)
    print("== leader_gate：仓内指挥官（目标→DAG→派发→回收→复盘）")

    work = os.path.join(ROOT, "work")
    before_w = set(os.listdir(work)) if os.path.isdir(work) else set()
    tools = os.path.join(ROOT, "tools")
    before_t = set(os.listdir(tools))
    try:
        lsrc = open(LEADER, encoding="utf-8").read()
        asrc = open(AGENTOS, encoding="utf-8").read()
    except OSError as e:
        print("LEADER_GATE_RED 读不到文件：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="lgrgate_")
    pf = "lgr%d" % os.getpid()
    sid_main, sid_fail, sid_steer, sid_cli = (
        pf + "-main", pf + "-fail", pf + "-steer", pf + "-cli")
    stub = os.path.join(tmp, "stub.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(STUB_SRC)
    worker = '"%s" %s {prompt}' % (sys.executable, stub)
    marks = os.path.join(tmp, "marks")
    slog = os.path.join(tmp, "stub_log.txt")
    env = dict(os.environ)
    env["LEADER_STUB_MARKS"] = marks
    env["LEADER_STUB_LOG"] = slog

    try:
        sys.path.insert(0, HERE)
        import dsp as _dsp

        # D1 编译面（纯）
        try:
            L = load_mod(LEADER, "leader_g")
            plan = L.compile_goal("把夜间模式加进监控面板并全链验证")
            ok_s, err_s = _dsp.validate_plan(plan)
            tasks = plan.get("tasks") or []
            dep_n = {}
            for t in tasks:
                for d in (t.get("depends") or []):
                    dep_n[d] = dep_n.get(d, 0) + 1
            fan_out = any(v >= 2 for v in dep_n.values())
            fan_in = any(len(t.get("depends") or []) >= 2 for t in tasks)
            ms_ok = all(str(t.get("milestone") or "").strip()
                        for t in tasks)
            tools_ok = all(set(t.get("tools") or []) <= set(_dsp.KNOWN_TOOLS)
                           for t in tasks)
            gate("D1 编译面：schema 全过 + 菱形扇出扇入 + 里程碑 + 白名单工具",
                 ok_s and len(tasks) >= 5 and fan_out and fan_in and ms_ok
                 and tools_ok and not L.has_cycle(tasks),
                 "schema=%s n=%d fan_out=%s fan_in=%s ms=%s tools=%s"
                 % (ok_s, len(tasks), fan_out, fan_in, ms_ok, tools_ok))
        except Exception as e:                                 # noqa: BLE001
            gate("D1 编译面：schema 全过 + 菱形扇出扇入 + 里程碑 + 白名单工具",
                 False, repr(e)[:120])

        # D2 落盘面
        try:
            p = run_leader("plan", "指挥官门夹具：目标全链", "--sid", sid_main)
            ppath = os.path.join(ROOT, "work", "plans",
                                 "%s-plan.json" % sid_main)
            cpath = os.path.join(ROOT, "work", "plans",
                                 "%s-chain.jsonl" % sid_main)
            ld, lerr = _dsp.load_plan(sid_main)
            chain = []
            try:
                with open(cpath, encoding="utf-8") as f:
                    chain = [json.loads(x) for x in f if x.strip()]
            except (OSError, ValueError):
                pass
            gate("D2 落盘面：plan rc=0 + 计划可读未审批 + 哈希链 save 行",
                 p.returncode == 0 and os.path.exists(ppath)
                 and ld is not None and ld.get("approved") is False
                 and any(r.get("event") == "save" for r in chain),
                 "rc=%s plan=%s approved=%s chain_save=%s"
                 % (p.returncode, os.path.exists(ppath),
                    ld is not None and ld.get("approved"),
                    any(r.get("event") == "save" for r in chain)))
        except Exception as e:                                 # noqa: BLE001
            gate("D2 落盘面：plan rc=0 + 计划可读未审批 + 哈希链 save 行",
                 False, repr(e)[:120])

        # D3 锁面：未审批拒跑
        try:
            p = run_leader("run", sid_main, "--worker", worker, env=env)
            rows = _read_ledger(sid_main)
            bpath = os.path.join(ROOT, "work", "plans",
                                 "%s-block.md" % sid_main)
            msg = (p.stdout or "") + (p.stderr or "")
            gate("D3 锁面：未审批 run 必拒（rc≠0 + 未审批 + 零派发 + block）",
                 p.returncode != 0 and "未审批" in msg
                 and not any(r.get("event") == "dispatch" for r in rows)
                 and os.path.exists(bpath),
                 "rc=%s 未审批=%s dispatch=%d block=%s"
                 % (p.returncode, "未审批" in msg,
                    sum(1 for r in rows if r.get("event") == "dispatch"),
                    os.path.exists(bpath)))
        except Exception as e:                                 # noqa: BLE001
            gate("D3 锁面：未审批 run 必拒（rc≠0 + 未审批 + 零派发 + block）",
                 False, repr(e)[:120])

        # D4 派发-回收面
        try:
            p0 = run_leader("approve", sid_main)
            p = run_leader("run", sid_main, "--worker", worker, env=env)
            ld, _ = _dsp.load_plan(sid_main)
            tasks = (ld or {}).get("tasks") or []
            order = [r.get("task") for r in _read_ledger(sid_main)
                     if r.get("event") == "dispatch"]
            ev_ok = all(t.get("evidence")
                        and "rc=0" in str(t["evidence"][-1]) for t in tasks)
            outs = all(os.path.exists(os.path.join(
                ROOT, "work", "leader", sid_main, "%s.out" % t.get("id")))
                for t in tasks)
            marks_n = len(os.listdir(marks)) if os.path.isdir(marks) else 0
            gate("D4 派发-回收面：5/5 全绿 + 拓扑序 + 证据落盘含 rc=0",
                 p0.returncode == 0 and p.returncode == 0
                 and len(tasks) == 5
                 and all(t.get("status") == "done" for t in tasks)
                 and order == ["t1", "t2", "t3", "t4", "t5"]
                 and ev_ok and outs and marks_n == 5
                 and "5/5" in (p.stdout or ""),
                 "rc=%s order=%s ev=%s outs=%s marks=%d"
                 % (p.returncode, order, ev_ok, outs, marks_n))
        except Exception as e:                                 # noqa: BLE001
            gate("D4 派发-回收面：5/5 全绿 + 拓扑序 + 证据落盘含 rc=0",
                 False, repr(e)[:120])

        # D5 失败传播面（乱序 DAG；t2 故意失败）
        try:
            tj = os.path.join(tmp, "tasks_fail.json")
            with open(tj, "w", encoding="utf-8") as f:
                json.dump([
                    {"id": "t3", "milestone": "M4 收尾", "title": "收尾",
                     "accept": "rc=0", "tools": ["run_cmd"],
                     "depends": ["t2"]},
                    {"id": "t1", "milestone": "M1 侦察", "title": "侦察",
                     "accept": "rc=0", "tools": ["read_file"],
                     "depends": []},
                    {"id": "t2", "milestone": "M2 定案", "title": "动手",
                     "accept": "rc=0", "tools": ["run_cmd"],
                     "depends": ["t1"]},
                ], f)
            p0 = run_leader("plan", "失败传播夹具", "--sid", sid_fail,
                            "--tasks-json", tj)
            p1 = run_leader("approve", sid_fail)
            env_f = dict(env)
            env_f["LEADER_STUB_FAIL"] = "t2"
            env_f["LEADER_STUB_MARKS"] = os.path.join(tmp, "marks_f")
            p = run_leader("run", sid_fail, "--worker", worker, env=env_f)
            ld, _ = _dsp.load_plan(sid_fail)
            tmap = {t.get("id"): t for t in ((ld or {}).get("tasks") or [])}
            order = [r.get("task") for r in _read_ledger(sid_fail)
                     if r.get("event") == "dispatch"]
            mkf = os.path.join(tmp, "marks_f")
            mk_names = sorted(os.listdir(mkf)) if os.path.isdir(mkf) else []
            gate("D5 失败传播面：乱序 DAG 拓扑跑 + t2 失败阻塞 t3 + rc≠0",
                 p0.returncode == 0 and p1.returncode == 0
                 and p.returncode != 0
                 and order == ["t1", "t2"]
                 and tmap.get("t1", {}).get("status") == "done"
                 and tmap.get("t2", {}).get("status") != "done"
                 and "rc=1" in str((tmap.get("t2", {}).get("evidence")
                                    or [""])[-1])
                 and tmap.get("t3", {}).get("status") == "todo"
                 and mk_names == ["t1", "t2"]
                 and "未完成" in (p.stderr or ""),
                 "rc=%s order=%s t2=%s t3=%s marks=%s"
                 % (p.returncode, order, tmap.get("t2", {}).get("status"),
                    tmap.get("t3", {}).get("status"), mk_names))
        except Exception as e:                                 # noqa: BLE001
            gate("D5 失败传播面：乱序 DAG 拓扑跑 + t2 失败阻塞 t3 + rc≠0",
                 False, repr(e)[:120])

        # D6 监督串联面：边界消费 steer
        try:
            import steer as _steer
            p0 = run_leader("plan", "监督串联夹具", "--sid", sid_steer)
            p1 = run_leader("approve", sid_steer)
            box = os.path.join(ROOT, "work", "steer")
            ok_push, err_push = _steer.push(
                box, sid_steer, "先跑侦察，写面等我确认")
            env_s = dict(env)
            env_s["LEADER_STUB_LOG"] = os.path.join(tmp, "stub_steer.txt")
            p = run_leader("run", sid_steer, "--worker", worker, env=env_s)
            pend_now, _total = _steer.pending(box, sid_steer)
            ledger_s = _read_ledger(sid_steer)
            steer_rows = [r for r in ledger_s if r.get("event") == "steer"]
            try:
                with open(env_s["LEADER_STUB_LOG"], encoding="utf-8") as f:
                    slogtxt = f.read()
            except OSError:
                slogtxt = ""
            gate("D6 监督串联面：steer 边界消费 + 进派发书 + 游标只进",
                 ok_push and p0.returncode == 0 and p1.returncode == 0
                 and p.returncode == 0
                 and not pend_now and len(steer_rows) == 1
                 and int(steer_rows[0].get("n") or 0) == 1
                 and "老板指令" in slogtxt
                 and "先跑侦察，写面等我确认" in slogtxt
                 and os.path.exists(os.path.join(box,
                                                 "%s.cursor" % sid_steer)),
                 "push=%s rc=%s pending=%d steer_rows=%d in_prompt=%s"
                 % (ok_push, p.returncode, len(pend_now), len(steer_rows),
                    "老板指令" in slogtxt))
        except Exception as e:                                 # noqa: BLE001
            gate("D6 监督串联面：steer 边界消费 + 进派发书 + 游标只进",
                 False, repr(e)[:120])

        # D7 复盘面
        try:
            p = run_leader("retro", sid_main)
            rpath = os.path.join(ROOT, "work", "leader",
                                 "%s-RETRO.md" % sid_main)
            try:
                txt = open(rpath, encoding="utf-8").read()
            except OSError:
                txt = ""
            gate("D7 复盘面：RETRO 落盘 + 任务账 rc 证据逐行 + 结论/steer 段",
                 p.returncode == 0 and os.path.exists(rpath)
                 and "目标：" in txt and "任务账" in txt
                 and "结论" in txt and "steer" in txt
                 and txt.count("证据：rc=") >= 5,
                 "rc=%s 证据行=%d"
                 % (p.returncode, txt.count("证据：rc=")))
        except Exception as e:                                 # noqa: BLE001
            gate("D7 复盘面：RETRO 落盘 + 任务账 rc 证据逐行 + 结论/steer 段",
                 False, repr(e)[:120])

        # D8 CLI 面
        import re as _re  # 括号内可换行：格式化会把 add("leader", …) 拆多行
        try:
            ph = subprocess.run([sys.executable, AGENTOS, "leader", "--help"],
                                capture_output=True, text=True, timeout=60)
            pc = subprocess.run([sys.executable, AGENTOS, "leader", "plan",
                                 "CLI 端到端：确认 leader 子命令真转发",
                                 "--sid", sid_cli],
                                capture_output=True, text=True, timeout=60)
            cpath = os.path.join(ROOT, "work", "plans",
                                 "%s-plan.json" % sid_cli)
            try:
                gsrc = open(os.path.join(HERE, "gates.sh"),
                            encoding="utf-8").read()
            except OSError:
                gsrc = ""
            gate("D8 CLI 面：leader --help rc=0 + 真 CLI plan + 三锚点齐",
                 ph.returncode == 0 and pc.returncode == 0
                 and os.path.exists(cpath)
                 and len(_re.findall(r'add\(\s*"leader"', asrc)) == 1
                 and lsrc.count("def compile_goal(") == 1
                 and "leader_gate" in gsrc,
                 "help=%s cli=%s anchor=%s/%s chain=%s"
                 % (ph.returncode, pc.returncode,
                    len(_re.findall(r'add\(\s*"leader"', asrc)),
                    lsrc.count("def compile_goal("),
                    "leader_gate" in gsrc))
        except Exception as e:                                 # noqa: BLE001
            gate("D8 CLI 面：leader --help rc=0 + 真 CLI plan + 三锚点齐",
                 False, repr(e)[:120])

        # D9 负控面：坏工具名 / 有环 DAG 必拒
        try:
            tbad = os.path.join(tmp, "tasks_bad.json")
            with open(tbad, "w", encoding="utf-8") as f:
                json.dump([{"id": "x", "title": "坏计划", "accept": "rc=0",
                            "tools": ["rm_everything"], "depends": []}], f)
            sid_bad = pf + "-bad"
            pb = run_leader("plan", "坏工具夹具", "--sid", sid_bad,
                            "--tasks-json", tbad)
            tcyc = os.path.join(tmp, "tasks_cycle.json")
            with open(tcyc, "w", encoding="utf-8") as f:
                json.dump([
                    {"id": "c1", "title": "环1", "accept": "rc=0",
                     "tools": ["run_cmd"], "depends": ["c2"]},
                    {"id": "c2", "title": "环2", "accept": "rc=0",
                     "tools": ["run_cmd"], "depends": ["c1"]},
                ], f)
            sid_cyc = pf + "-cyc"
            pc = run_leader("plan", "环夹具", "--sid", sid_cyc,
                            "--tasks-json", tcyc)
            no_bad = not os.path.exists(os.path.join(
                ROOT, "work", "plans", "%s-plan.json" % sid_bad))
            no_cyc = not os.path.exists(os.path.join(
                ROOT, "work", "plans", "%s-plan.json" % sid_cyc))
            gate("D9 负控面：坏工具名必拒 + 有环 DAG 必拒（且零落盘）",
                 pb.returncode != 0 and no_bad
                 and pc.returncode != 0 and no_cyc
                 and "环" in (pc.stderr or ""),
                 "bad=%s/%s cyc=%s/%s"
                 % (pb.returncode, no_bad, pc.returncode, no_cyc))
        except Exception as e:                                 # noqa: BLE001
            gate("D9 负控面：坏工具名必拒 + 有环 DAG 必拒（且零落盘）",
                 False, repr(e)[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # D10 零污染：清夹具后 work/ 只多 --out、tools/ 零残留
    cleanup_fixtures([sid_main, sid_fail, sid_steer, sid_cli,
                      pf + "-bad", pf + "-cyc"])
    after_w = set(os.listdir(work)) if os.path.isdir(work) else set()
    after_t = set(os.listdir(tools))
    new_w = sorted((after_w - before_w)
                   - {os.path.basename(os.path.abspath(args.out))})
    new_t = sorted(after_t - before_t)
    gate("D10 零污染：work/ 只多 --out；tools/ 零残留；夹具全清",
         not new_w and not new_t,
         "新增work=%s 新增tools=%s" % (new_w or "无", new_t or "无"))

    return _finish(args.out)


if __name__ == "__main__":
    sys.exit(main())
