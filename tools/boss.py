#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""boss —— 老板模块：独立于干活 agent 之外的第二个 agent（单独进程）。

职责（人不审，机器审；但每一步留痕、可推翻）：
  * review <sid>：读计划 → schema+环校验 → 风险扫描 → 行就 approve，
    不行就拒并写 steer 指路。结论落 work/boss_reviews/<sid>.json。
  * monitor <sid>：读跑账 → 报 done/剩/blocked/steer pending；blocked
    出不来就推一条换路 steer（只建议，不直接改计划）。
  * pilot <goal>：编译目标 → review → approve → autonomy run →
    monitor → 报告。大项目一句话开跑的入口。
  * stop <sid>：撤销审批（unapprove），在跑的自主循环下一轮边界即停。

诚实边界：review 是确定性规则（schema/环/风险三件），不是模型拍脑袋；
 verdict 文件含 reasons 数组，拒了必须说清哪条、怎么改。boss 批过的计划，
 autonomy 照样要过 verify 双闸 + 证据链 —— 老板负责拍板，不管造假。
裁决：本模块不做自证；boss_gate 拿夹具证伪它。
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_MODS = {}


def _mods(root):
    key = os.path.abspath(root)
    if key not in _MODS:
        tools = os.path.join(key, "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import dsp
        import steer

        _MODS[key] = (dsp, steer)
    return _MODS[key]


def _boss_provider():
    """老板的大脑：复用本机模型配置。连不上就回 None（规则保底照走）。"""
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import agent_core as _ac

        return _ac.Provider(_ac.resolve_provider(None))
    except Exception:  # noqa: BLE001
        return None


def _chat_json(provider, system, user, timeout_note=""):
    """让模型回一个 JSON object；解析不出就回 None（不猜）。"""
    try:
        comp = provider.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
            stream=False,
        )
        txt = str((comp or {}).get("content") or "")
        i = txt.find("{")
        if i < 0:
            return None
        d, _ = json.JSONDecoder().raw_decode(txt[i:])
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _plan_brief(plan, cap=2600):
    rows = []
    for t in plan.get("tasks") or []:
        rows.append(
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "tools": t.get("tools"),
                "accept": t.get("accept"),
                "depends": t.get("depends"),
                "status": t.get("status"),
            }
        )
    s = json.dumps({"goal": plan.get("goal"), "tasks": rows}, ensure_ascii=False)
    return s if len(s) <= cap else s[:cap] + "…（截断）"


def _model_verdict(plan, provider):
    """实质审：分解对不对、工具有没有拿错、判据能不能验。
    返回 (pass_bool_or_None, reasons, suggestions)。None=模型没说明白。"""
    d = _chat_json(
        provider,
        "你是技术主管审执行计划。只回一个JSON object："
        '{"pass": true/false, "reasons": ["..."], '
        '"suggestions": ["..."]}。'
        "分解缺步骤、工具拿错、判据不可验就判false并说清哪条。"
        "可用工具只有：read_file, edit_file, write_file, run_cmd, "
        "search, list_dir, web_fetch, gh_search, gh_repo_view, "
        "subagent——不许要求表外工具，推理判断类用 read_file+accept "
        "文字判据即可。",
        "计划：" + _plan_brief(plan),
    )
    if not d or not isinstance(d.get("pass"), bool):
        return None, [], []
    reasons = [str(x)[:200] for x in (d.get("reasons") or [])][:5]
    sug = [str(x)[:200] for x in (d.get("suggestions") or [])][:5]
    return d["pass"], reasons, sug


def _model_hint(plan, tid, evidence, provider):
    """救场真招：看失败证据给一条具体换路法。给不出就回 None（用模板）。"""
    d = _chat_json(
        provider,
        '你是现场救火员。只回一个JSON object：{"hint": "一句话换路法，'
        '具体到命令或文件"}。说空话就回{"hint": ""}。',
        "任务 %s 失败证据：%s" % (tid, str(evidence)[-800:]),
    )
    h = str((d or {}).get("hint") or "").strip()
    return h[:200] if h else None


def _verdict_path(root, sid):
    return os.path.join(root, "work", "boss_reviews", "%s.json" % sid)


def _save_verdict(root, sid, verdict):
    p = _verdict_path(root, sid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=1, sort_keys=True)
    return p


def risk_scan(plan, full_access):
    """风险扫描（纯面）：返回 [reasons]，空 = 无异议。"""
    reasons = []
    tasks = plan.get("tasks") or []
    n_cmd = sum(1 for t in tasks if "run_cmd" in (t.get("tools") or []))
    if n_cmd and not full_access:
        reasons.append(
            "计划含 %d 个 run_cmd 任务，但本机 full_access 未开："
            "先开完全权限或改写只读工具" % n_cmd
        )
    for t in tasks:
        acc = str(t.get("accept") or "")
        if len(acc.strip()) < 4:
            reasons.append("任务 %r 的 accept 判据过短（无法验证）" % t.get("id"))
    if len(tasks) > 12:
        reasons.append("任务 %d 个过多（>12）：先拆阶段再跑" % len(tasks))
    return reasons


def review(sid, root="", full_access=None, use_model=False, provider=None):
    """审计划：规则三件是硬线；模型看实质（分解/工具/判据）。
    模型连不上或没说明白 → 按规则结论走，verdict 如实记。"""
    root = os.path.abspath(root or ROOT)
    dsp, steer = _mods(root)
    plan, err = dsp.load_plan(sid)
    if plan is None:
        return False, "无计划可审：%s" % (err or "missing")
    if plan.get("approved"):
        _save_verdict(
            root,
            sid,
            {
                "sid": sid,
                "pass": True,
                "reasons": ["已审批，无需重审"],
                "ts": round(time.time(), 3),
            },
        )
        return True, "已审批，无需重审"
    ok, verr = dsp.validate_plan(plan)
    if not ok:
        _save_verdict(
            root,
            sid,
            {"sid": sid, "pass": False, "reasons": [verr], "ts": round(time.time(), 3)},
        )
        return False, verr
    if full_access is None:
        try:
            import agent_core as _ac

            full_access = _ac._full_access(_ac._read_agentos_config())
        except Exception:  # noqa: BLE001
            full_access = False
    risks = risk_scan(plan, full_access)
    if risks:
        d = os.path.join(root, "work", "steer")
        steer.push(d, sid, "boss 拒审：%s" % "；".join(risks))
        _save_verdict(
            root,
            sid,
            {"sid": sid, "pass": False, "reasons": risks, "ts": round(time.time(), 3)},
        )
        return False, "；".join(risks)
    model_v = {"pass": None, "reasons": [], "suggestions": []}
    if use_model:
        prov = provider or _boss_provider()
        if prov is not None:
            mp, mr, ms = _model_verdict(plan, prov)
            model_v = {"pass": mp, "reasons": mr, "suggestions": ms}
            if mp is False:
                reasons = ["模型实质审不通过"] + mr
                d = os.path.join(root, "work", "steer")
                steer.push(d, sid, "boss 拒审：%s" % "；".join(reasons))
                _save_verdict(
                    root,
                    sid,
                    {
                        "sid": sid,
                        "pass": False,
                        "reasons": reasons,
                        "model": model_v,
                        "ts": round(time.time(), 3),
                    },
                )
                return False, "；".join(reasons)
    ok2, err2 = dsp.approve_plan(sid)
    if not ok2:
        return False, err2
    d = os.path.join(root, "work", "steer")
    steer.push(d, sid, "boss 已审批：按计划执行，证据逐条留痕")
    if model_v["suggestions"]:
        steer.push(
            d,
            sid,
            "boss 建议（顺手做，不返工）：%s" % "；".join(model_v["suggestions"]),
        )
    _save_verdict(
        root,
        sid,
        {
            "sid": sid,
            "pass": True,
            "reasons": ["schema+环+风险三件全过"]
            + (["模型实质审通过"] if model_v["pass"] else []),
            "model": model_v,
            "ts": round(time.time(), 3),
        },
    )
    return True, "boss 已审批"


def _ledger_rows(root, sid):
    rows = []
    try:
        with open(
            os.path.join(root, "work", "autonomy", "%s.jsonl" % sid),
            encoding="utf-8",
        ) as f:
            for ln in f:
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def _ledger_boss(root, sid, action, task=""):
    try:
        d = os.path.join(root, "work", "autonomy")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%s.jsonl" % sid), "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": round(time.time(), 3),
                        "event": "boss",
                        "action": action,
                        "task": task,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:  # noqa: BLE001
        pass


def supervise(
    sid,
    root="",
    interval=15,
    max_polls=0,
    use_model=False,
    provider=None,
    resume=False,
    worker="",
    full_access=None,
):
    """并行监工：干活的进程不停，老板另起循环看账动手。
    未审批→review+approve；在跑中新增blocked/stuck→重开+steer
    （每任务≤3次）；跑死还有剩→--resume则拉起resume，否则报停；
    全done→收工。返回 (ok, detail)。"""
    import time as _time

    root = os.path.abspath(root or ROOT)
    dsp, steer = _mods(root)
    prov = provider
    if use_model and prov is None:
        prov = _boss_provider()
    resets = {}
    polls = 0
    _done_at_launch = -1
    _lp = os.path.join(root, "work", "autonomy", "%s.jsonl" % sid)
    while True:
        polls += 1
        plan, err = dsp.load_plan(sid)
        if plan is None:
            return False, "计划没了：%s" % (err or "missing")
        if not plan.get("approved"):
            ok, detail = review(
                sid,
                root,
                full_access=full_access,
                use_model=use_model,
                provider=prov,
            )
            if not ok:
                return False, "审批不过：%s" % detail
        rows = _ledger_rows(root, sid)
        _starts = [r for r in rows if r.get("event") == "run_start"]
        ends = sum(1 for r in rows if r.get("event") == "run_end")
        _alive = False
        if _starts:
            _pid = _starts[-1].get("pid")
            if isinstance(_pid, int):
                try:
                    os.kill(_pid, 0)
                    _alive = True
                except OSError:  # noqa: BLE001
                    pass
        try:
            _fresh = _time.time() - os.path.getmtime(_lp) < max(60, interval * 4)
        except OSError:
            _fresh = False
        running = len(_starts) > ends and (_alive or _fresh)
        rep_tasks = plan.get("tasks") or []
        done = [t.get("id") for t in rep_tasks if t.get("status") == "done"]
        bad = [
            t
            for t in rep_tasks
            if t.get("status") in ("blocked", "stuck")
            and resets.get(t.get("id"), 0) < 3
        ]
        _fixed_this_poll = False
        if bad:
            hints = _blocked_hints({"tasks": bad})
            if use_model and prov is not None:
                for t in bad:
                    mh = _model_hint(plan, t.get("id"), t.get("evidence"), prov)
                    if mh:
                        hints[t.get("id")] = "模型换路：" + mh
            for t in bad:
                tid = t.get("id")
                t["status"] = "todo"
                (t.setdefault("evidence", [])).append(
                    "boss并行重开：%s" % hints.get(tid, "")[:90]
                )
                resets[tid] = resets.get(tid, 0) + 1
                _ledger_boss(root, sid, "reset", tid)
            ok, err = dsp.save_plan(plan, sid)
            if ok:
                _fixed_this_poll = True
                d = os.path.join(root, "work", "steer")
                for t in bad:
                    tid = t.get("id")
                    steer.push(
                        d, sid, "boss 并行自救 %s：%s" % (tid, hints.get(tid, ""))
                    )
        if not [t for t in rep_tasks if t.get("status") != "done"]:
            return True, "全done收工（盯了%d轮）" % polls
        if not running:
            remain = [
                t.get("id") for t in rep_tasks if t.get("status") not in ("done",)
            ]
            if not remain:
                return True, "全done收工（盯了%d轮）" % polls
            if resume and (_fixed_this_poll or len(done) > _done_at_launch):
                _done_at_launch = len(done)
                au = os.path.join(root, "tools", "autonomy.py")
                subprocess.Popen(
                    [sys.executable, au, "--root", root, "resume", sid]
                    + (["--worker", worker] if worker else []),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _ledger_boss(root, sid, "resume-launch")
            else:
                return False, "执行器停了还有剩%s（无进展，不空拉）" % (remain,)
        if max_polls and polls >= max_polls:
            return False, "轮数到（%d轮），剩下的下次盯" % polls
        _time.sleep(interval)


def stop(sid, root=""):
    """老板叫停：撤销审批（在跑循环下一轮边界即停）。"""
    root = os.path.abspath(root or ROOT)
    dsp, _ = _mods(root)
    return dsp.unapprove_plan(sid)


def monitor(sid, root=""):
    """读跑账出监督报告（只读，附带换路建议）。返回 dict。"""
    root = os.path.abspath(root or ROOT)
    dsp, steer = _mods(root)
    plan, err = dsp.load_plan(sid)
    if plan is None:
        return {"sid": sid, "ok": False, "error": err or "missing"}
    tasks = plan.get("tasks") or []
    done = [t.get("id") for t in tasks if t.get("status") == "done"]
    blocked = [t.get("id") for t in tasks if t.get("status") == "blocked"]
    remain = [t.get("id") for t in tasks if t.get("status") not in ("done", "blocked")]
    d = os.path.join(root, "work", "steer")
    items, total = steer.pending(d, sid)
    rep = {
        "sid": sid,
        "ok": True,
        "goal": plan.get("goal") or "",
        "approved": bool(plan.get("approved")),
        "done": done,
        "blocked": blocked,
        "remain": remain,
        "stuck": [t.get("id") for t in tasks if t.get("status") == "stuck"],
        "steer_pending": len(items),
        "steer_total": total,
        "suggest": "",
    }
    if blocked:
        rep["suggest"] = "卡住 %s：换小步重做，判据拆小，先落盘中间产物" % ",".join(
            blocked
        )
    elif remain and not plan.get("approved"):
        rep["suggest"] = "未审批：先 boss review 再跑"
    return rep


KIND_HINTS = {
    "timeout": "上一轮超时：把动作拆小，先落盘中间产物再报告，不要死等长命令",
    "notfound": "上一轮路径/命令不存在：先确认命令与路径真实存在再跑",
    "verify": "判据没过：收紧判据命令，先手工跑通判据再交活",
    "budget": "步数烧光：做最小可验证产物就收，删掉无进展的反复试探",
    "fail": "命令失败：看日志尾行，按错误信息换干法重试",
    "crash": "进程崩溃：拆小动作，加超时，留中间产物",
}


def _blocked_hints(plan):
    """纯面：blocked/stuck 任务 → {tid: hint}。kind 从证据行解析。"""
    import re

    out = {}
    for task in plan.get("tasks") or []:
        if task.get("status") not in ("blocked", "stuck"):
            continue
        kind = "fail"
        for ev in task.get("evidence") or []:
            m = re.search(r"blocked\(kind=(\w+)\)", str(ev))
            if m:
                kind = m.group(1)
                break
        else:
            if task.get("status") == "stuck":
                kind = "budget"
        out[task.get("id")] = KIND_HINTS.get(kind, KIND_HINTS["fail"])
    return out


def fix(sid, root="", worker="", use_model=False, provider=None):
    """自救环：blocked 重开为 todo + 换路 steer → resume 续跑。
    use_model 时换路法由模型看证据现想，想不出回落模板。"""
    root = os.path.abspath(root or ROOT)
    dsp, steer = _mods(root)
    plan, err = dsp.load_plan(sid)
    if plan is None:
        return False, "无计划：%s" % (err or "missing")
    hints = _blocked_hints(plan)
    if not hints:
        return False, "无卡住任务，无需自救"
    prov = None
    if use_model:
        prov = provider or _boss_provider()
    if prov is not None:
        for task in plan.get("tasks") or []:
            if task.get("id") in hints:
                mh = _model_hint(plan, task.get("id"), task.get("evidence"), prov)
                if mh:
                    hints[task.get("id")] = "模型换路：" + mh
    for task in plan.get("tasks") or []:
        if task.get("id") in hints:
            task["status"] = "todo"
            (task.setdefault("evidence", [])).append(
                "boss重开：%s" % hints[task["id"]][:90]
            )
    ok, err = dsp.save_plan(plan, sid)
    if not ok:
        return False, "重开落盘失败：" + err
    d = os.path.join(root, "work", "steer")
    for tid, hint in hints.items():
        steer.push(d, sid, "boss 自救 %s：%s" % (tid, hint))
    au = os.path.join(root, "tools", "autonomy.py")
    r = subprocess.run(
        [sys.executable, au, "--root", root, "resume", sid]
        + (["--worker", worker] if worker else []),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    rep = monitor(sid, root)
    detail = "自救派发：%s；现 done=%d blocked=%d remain=%d" % (
        ",".join(sorted(hints)),
        len(rep["done"]),
        len(rep["blocked"]),
        len(rep["remain"]),
    )
    return (not rep["blocked"] and not rep["remain"]), detail


def replan(sid, instruction, root="", provider=None):
    """改计划：模型按指示重写 tasks 数组 → 校验 → 落盘（审批清零重审）。
    返回 (ok, detail)。校验不过原计划不动。"""
    root = os.path.abspath(root or ROOT)
    dsp, _ = _mods(root)
    plan, err = dsp.load_plan(sid)
    if plan is None:
        return False, "无计划：%s" % (err or "missing")
    prov = provider or _boss_provider()
    if prov is None:
        return False, "模型连不上，改计划不动（保底）"
    d = _chat_json(
        prov,
        '你是计划编辑。只回一个JSON object：{"tasks": [{'
        '"id": ..., "title": ..., "tools": [...], '
        '"accept": ..., "depends": [...]}]}。'
        "铁律：每个任务五个键缺一不可，tools 非空数组只能从此表选："
        "read_file, edit_file, write_file, run_cmd, search, "
        "list_dir, web_fetch, gh_search, gh_repo_view, subagent。"
        "少键或表外工具 → 整个回答作废。依赖不许成环。",
        "当前计划：" + _plan_brief(plan) + "\n改动指示：" + instruction[:500],
    )
    tasks = (d or {}).get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False, "模型没给出可用 tasks，原计划不动"
    new_plan = dict(plan)
    new_plan["tasks"] = tasks
    new_plan["approved"] = False
    new_plan.pop("approved_at", None)
    ok, verr = dsp.validate_plan(new_plan)
    if not ok:
        return False, "改完校验不过，原计划不动：" + verr
    ok, err = dsp.save_plan(new_plan, sid)
    if not ok:
        return False, "落盘失败：" + err
    return True, "已改 %d 个任务，需重新 review（审批已清零）" % len(tasks)


def memory(pid, root="", mem_path=""):
    """intake 执行体（memory 类）：提案 json → 记忆账落一条，去重封顶。"""
    root = os.path.abspath(root or ROOT)
    pdir = os.path.join(root, "work", "evo_proposals")
    try:
        with open(os.path.join(pdir, "%s.json" % pid), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return False, "提案不可读：%s" % str(e)[:80]
    sys.path.insert(0, os.path.join(root, "tools"))
    try:
        import agent_core as _ac

        ok, err = _ac.validate_proposal(d)
    except Exception as e:  # noqa: BLE001
        return False, "校验器异常：%s" % str(e)[:80]
    if not ok:
        return False, "提案 schema 非法：" + err
    if d.get("kind") != "memory":
        return False, "只执行 memory 类（%s 走人工/evo_merge）" % d.get("kind")
    mem = mem_path or os.path.join(root, "work", "evo_memory.md")
    try:
        cur = open(mem, encoding="utf-8").read() if os.path.exists(mem) else ""
    except OSError as e:
        return False, "记忆账不可读：%s" % str(e)[:80]
    if d.get("sig") and d.get("sig") in cur:
        return True, "已存在（同 sig 去重），无需重复落账"
    if cur.count("\n## ") >= 100:
        return False, "记忆账已满 100 条，先人工归档再续"
    os.makedirs(os.path.dirname(mem), exist_ok=True)
    with open(mem, "a", encoding="utf-8") as f:
        f.write(
            "\n## %s（%s）\n- sig：%s ×%d\n- 建议：%s\n"
            % (
                d.get("id"),
                d.get("created") or "?",
                d.get("sig"),
                d.get("count"),
                d.get("advice"),
            )
        )
    return True, "已落记忆账"


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(prog="boss")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("review")
    p.add_argument("sid")
    p.add_argument("--root", default=ROOT)
    p.add_argument("--boss-model", action="store_true")
    p = sub.add_parser("monitor")
    p.add_argument("sid")
    p.add_argument("--root", default=ROOT)
    p = sub.add_parser("stop")
    p.add_argument("sid")
    p.add_argument("--root", default=ROOT)
    p = sub.add_parser("pilot")
    p.add_argument("goal")
    p.add_argument("--sid", default="")
    p.add_argument("--root", default=ROOT)
    p.add_argument("--worker", default="")
    p.add_argument("--max-tasks", type=int, default=12)
    p.add_argument("--task-timeout", type=int, default=300)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--no-boss-model", dest="boss_model", action="store_false")
    p.add_argument("--no-supervise", dest="supervised", action="store_false")
    p.set_defaults(boss_model=True, supervised=True)
    p = sub.add_parser("fix")
    p.add_argument("sid")
    p.add_argument("--root", default=ROOT)
    p.add_argument("--worker", default="")
    p.add_argument("--boss-model", action="store_true")
    p = sub.add_parser("replan")
    p.add_argument("sid")
    p.add_argument("instruction")
    p.add_argument("--root", default=ROOT)
    p = sub.add_parser("memory")
    p.add_argument("pid")
    p.add_argument("--root", default=ROOT)
    p.add_argument("--mem", default="")
    p = sub.add_parser("supervise")
    p.add_argument("sid")
    p.add_argument("--root", default=ROOT)
    p.add_argument("--interval", type=int, default=15)
    p.add_argument("--max-polls", type=int, default=0)
    p.add_argument("--boss-model", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--worker", default="")
    args = ap.parse_args(argv)
    if args.cmd == "review":
        ok, detail = review(args.sid, args.root, use_model=args.boss_model)
        print(("BOSS_PASS " if ok else "BOSS_REJECT ") + detail)
        return 0 if ok else 2
    if args.cmd == "monitor":
        print(
            json.dumps(
                monitor(args.sid, args.root),
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
        )
        return 0
    if args.cmd == "fix":
        ok, detail = fix(args.sid, args.root, args.worker, use_model=args.boss_model)
        print(("BOSS_FIXED " if ok else "BOSS_FIX_PART ") + detail)
        return 0 if ok else 1
    if args.cmd == "replan":
        ok, detail = replan(args.sid, args.instruction, args.root)
        print(("BOSS_REPLANNED " if ok else "BOSS_REPLAN_FAIL ") + detail)
        return 0 if ok else 2
    if args.cmd == "memory":
        ok, detail = memory(args.pid, args.root, args.mem)
        print(("BOSS_MEMORY_OK " if ok else "BOSS_MEMORY_FAIL ") + detail)
        return 0 if ok else 2
    if args.cmd == "supervise":
        ok, detail = supervise(
            args.sid,
            args.root,
            interval=args.interval,
            max_polls=args.max_polls,
            use_model=args.boss_model,
            resume=args.resume,
            worker=args.worker,
        )
        print(("BOSS_SUPERVISED " if ok else "BOSS_SUPERVISE_STOP ") + detail)
        return 0 if ok else 1
    if args.cmd == "stop":
        ok, err = stop(args.sid, args.root)
        print(("BOSS_STOPPED" if ok else "BOSS_STOP_FAIL " + str(err or "")))
        return 0 if ok else 2
    if args.cmd == "pilot":
        root = os.path.abspath(args.root)
        dsp, _ = _mods(root)
        sys.path.insert(0, os.path.join(root, "tools"))
        import leader as _leader

        if not os.path.isdir(root) or not os.access(root, os.W_OK):
            print("BOSS_PILOT_FAIL root 不可写：" + root)
            return 2
        try:
            import agent_core as _ac

            _cfg = _ac._read_agentos_config()
            _llm = _cfg.get("llm") or {}
            if not str(_llm.get("model") or "").strip():
                print("BOSS_PILOT_FAIL 模型未配置（~/.agentos/config.toml [llm]）")
                return 2
        except Exception as e:  # noqa: BLE001
            print("BOSS_PILOT_FAIL 读配置失败：%s" % str(e)[:80])
            return 2
        sid = args.sid or ("boss-%s" % time.strftime("%m%d-%H%M%S"))
        old, _ = dsp.load_plan(sid)
        if old is not None and old.get("approved"):
            print("BOSS_PILOT_RESUME 旧计划已审批，直接续跑")
        else:
            plan = _leader.compile_goal(args.goal, root)
            ok, err = dsp.save_plan(plan, sid)
            if not ok:
                print("BOSS_PILOT_FAIL save：" + err)
                return 2
            ok, detail = review(sid, root, use_model=args.boss_model)
            if not ok:
                print("BOSS_PILOT_FAIL review：" + detail)
                return 2
        au = os.path.join(root, "tools", "autonomy.py")
        _au_argv = (
            [sys.executable, au, "--root", root, "run", sid]
            + (["--worker", args.worker] if args.worker else [])
            + [
                "--max-tasks",
                str(args.max_tasks),
                "--task-timeout",
                str(args.task_timeout),
                "--retries",
                str(args.retries),
            ]
        )
        _rc = 0
        if args.supervised:
            _plog = os.path.join(root, "work", "autonomy", "%s-pilot.log" % sid)
            os.makedirs(os.path.dirname(_plog), exist_ok=True)
            with open(_plog, "a", encoding="utf-8") as _lf:
                _lf.write("=== pilot launch %s ===\n" % time.strftime("%F %T"))
                _lf.flush()
                subprocess.Popen(
                    _au_argv,
                    stdout=_lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            ok, detail = supervise(
                sid,
                root,
                use_model=args.boss_model,
                resume=True,
                worker=args.worker,
            )
            print(("BOSS_SUPERVISED " if ok else "BOSS_SUPERVISE_STOP ") + detail)
            try:
                with open(_plog, encoding="utf-8") as _lf:
                    _lf.seek(0, os.SEEK_END)
                    _size = _lf.tell()
                    _lf.seek(max(0, _size - 2000))
                    sys.stdout.write(_lf.read()[-2000:])
            except OSError:  # noqa: BLE001
                pass
            _rc = 0 if ok else 1
        else:
            r = subprocess.run(_au_argv, capture_output=True, text=True, timeout=3600)
            sys.stdout.write((r.stdout or "")[-2000:])
            _rc = r.returncode
        rep = monitor(sid, root)
        _done_n = len(rep["done"])
        _round = 0
        while rep["blocked"] or rep.get("stuck"):
            _round += 1
            print(
                "BOSS_PILOT_FIX 第%d轮：卡住=%s"
                % (_round, rep["blocked"] + (rep.get("stuck") or []))
            )
            fix(sid, root, args.worker, use_model=args.boss_model)
            rep = monitor(sid, root)
            if len(rep["done"]) <= _done_n:
                break  # 本轮零进展：停，如实报（不空转烧钱）
            _done_n = len(rep["done"])
        print(
            "BOSS_PILOT done=%d blocked=%d stuck=%d remain=%d rc=%d"
            % (
                len(rep["done"]),
                len(rep["blocked"]),
                len(rep.get("stuck") or []),
                len(rep["remain"]),
                _rc,
            )
        )
        return (
            0
            if not rep["blocked"] and not rep["remain"] and not rep.get("stuck")
            else 1
        )
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
