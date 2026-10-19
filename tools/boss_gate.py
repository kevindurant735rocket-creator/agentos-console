#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""boss_gate —— 老板模块门：人不审、机器审，拒了一定说清哪条。

    B1 坏工具计划拒审（直写坏计划 → review 拒，理由指名工具）
    B2 环计划拒审（直写环计划 → review 拒，理由含环）
    B3 好计划放行（只读两任务 → approve + verdict 文件 schema 齐）
    B4 含 run_cmd 无全权拒审（理由指 full_access）
    B5 含 run_cmd 有全权放行
    B6 monitor 报告形状（done/blocked/remain/steer 齐）
    B7 自救环（blocked 重开 todo + 换路 steer 进箱）
    B8 记忆执行体（memory 提案落账 + 同 sig 去重）
    B10 并行监工：全 done 直接收工
    B11 并行监工：先审后批＋执行器停摆如实报
    B12 零污染（work/ 只多 --out；夹具按 sid 全清）

用法: python3 tools/boss_gate.py [--out PATH]
裁决：BOSS_GATE OK n/n（rc=0）/ BOSS_GATE RED（rc=2）
"""

import argparse
import importlib.util
import json
import os
import gate_pollution as _pollution
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "work", "boss_gate.json")

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


def _finish(out_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(
            {"judgments": results, "n_ok": n_ok, "n_total": len(results)},
            fh,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    print("BOSS_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("BOSS_GATE_OK")
        return 0
    print("BOSS_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def _direct_plan(sid, tasks):
    """绕过 save 校验直写计划（测 review 负控面）。"""
    p = os.path.join(ROOT, "work", "plans", "%s-plan.json" % sid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(
            {
                "goal": "boss 门夹具",
                "approved": False,
                "tasks": tasks,
                "bind_sha": "boss",
            },
            f,
            ensure_ascii=False,
        )
    return p


def _t(tid, tools, accept="rc=0", depends=()):
    return {
        "id": tid,
        "title": "夹具%s" % tid,
        "accept": accept,
        "tools": list(tools),
        "depends": list(depends),
        "status": "todo",
        "steps_used": 0,
        "evidence": [],
    }


def _clean(sids):
    for sid in sids:
        for sub, suf in (
            ("plans", "-plan.json"),
            ("plans", "-chain.jsonl"),
            ("plans", "-block.md"),
            ("boss_reviews", ".json"),
            ("steer", ".jsonl"),
            ("steer", ".cursor"),
        ):
            try:
                if sub == "boss_reviews":
                    os.unlink(os.path.join(ROOT, "work", sub, "%s.json" % sid))
                elif sub == "steer":
                    os.unlink(os.path.join(ROOT, "work", sub, "%s%s" % (sid, suf)))
                else:
                    os.unlink(os.path.join(ROOT, "work", sub, "%s%s" % (sid, suf)))
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    print("== boss_gate：老板模块（人不审机器审）")
    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    pf = "boss-%d" % os.getpid()
    sids = [
        pf + "-bad",
        pf + "-cyc",
        pf + "-good",
        pf + "-cmd0",
        pf + "-cmd1",
        pf + "-mon",
    ]
    try:
        BOSS = load_mod(os.path.join(HERE, "boss.py"), "boss_g")
        tools_dir = HERE
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import dsp as _dsp

        # B1 坏工具拒审
        try:
            _direct_plan(sids[0], [_t("b1", ["rm_warehouse"])])
            ok, detail = BOSS.review(sids[0], ROOT)
            gate(
                "B1 坏工具计划拒审（理由指名工具）",
                ok is False and "工具" in detail,
                detail[:60],
            )
        except Exception as e:  # noqa: BLE001
            gate("B1 坏工具计划拒审（理由指名工具）", False, repr(e)[:120])

        # B2 环计划拒审
        try:
            _direct_plan(
                sids[1],
                [
                    _t("c1", ["read_file"], depends=["c2"]),
                    _t("c2", ["read_file"], depends=["c1"]),
                ],
            )
            ok, detail = BOSS.review(sids[1], ROOT)
            gate(
                "B2 环计划拒审（理由含环）", ok is False and "环" in detail, detail[:60]
            )
        except Exception as e:  # noqa: BLE001
            gate("B2 环计划拒审（理由含环）", False, repr(e)[:120])

        # B3 好计划放行 + verdict schema
        try:
            plan = {
                "goal": "boss 门夹具",
                "approved": False,
                "tasks": [
                    _t("g1", ["read_file"]),
                    _t("g2", ["read_file"], depends=["g1"]),
                ],
                "bind_sha": "x",
            }
            oks, es = _dsp.save_plan(dict(plan), sids[2])
            ok, detail = BOSS.review(sids[2], ROOT) if oks else (False, es)
            vp = os.path.join(ROOT, "work", "boss_reviews", "%s.json" % sids[2])
            try:
                v = json.load(open(vp, encoding="utf-8"))
            except (OSError, ValueError):
                v = {}
            plan2, _ = _dsp.load_plan(sids[2])
            gate(
                "B3 好计划放行（approve + verdict schema 齐）",
                ok is True
                and plan2.get("approved") is True
                and v.get("pass") is True
                and set(("sid", "pass", "reasons", "ts")) <= set(v),
                detail[:40],
            )
        except Exception as e:  # noqa: BLE001
            gate("B3 好计划放行（approve + verdict schema 齐）", False, repr(e)[:120])

        # B4 含命令无全权拒审
        try:
            _direct_plan(sids[3], [_t("m1", ["run_cmd"])])
            # 直写计划 load 即拒（坏accept? 不，accept合法）→ 走 review 风险项
            plan = {
                "goal": "boss 门夹具",
                "approved": False,
                "tasks": [_t("m1", ["run_cmd"])],
                "bind_sha": "x",
            }
            _dsp.save_plan(dict(plan), sids[3])
            ok, detail = BOSS.review(sids[3], ROOT, full_access=False)
            gate(
                "B4 含 run_cmd 无全权拒审（理由指 full_access）",
                ok is False and "full_access" in detail,
                detail[:60],
            )
        except Exception as e:  # noqa: BLE001
            gate("B4 含 run_cmd 无全权拒审（理由指 full_access）", False, repr(e)[:120])

        # B5 含命令有全权放行
        try:
            plan = {
                "goal": "boss 门夹具",
                "approved": False,
                "tasks": [_t("m1", ["run_cmd"])],
                "bind_sha": "x",
            }
            _dsp.save_plan(dict(plan), sids[4])
            ok, detail = BOSS.review(sids[4], ROOT, full_access=True)
            plan2, _ = _dsp.load_plan(sids[4])
            gate(
                "B5 含 run_cmd 有全权放行",
                ok is True and plan2.get("approved") is True,
                detail[:40],
            )
        except Exception as e:  # noqa: BLE001
            gate("B5 含 run_cmd 有全权放行", False, repr(e)[:120])

        # B6 monitor 形状
        try:
            rep = BOSS.monitor(sids[2], ROOT)
            gate(
                "B6 monitor 报告形状（done/blocked/remain/steer 齐）",
                rep.get("ok") is True
                and rep.get("approved") is True
                and "remain" in rep
                and "steer_pending" in rep,
                "remain=%s" % (rep.get("remain"),),
            )
        except Exception as e:  # noqa: BLE001
            gate(
                "B6 monitor 报告形状（done/blocked/remain/steer 齐）",
                False,
                repr(e)[:120],
            )

        # B7 自救环（blocked 重开 todo + 换路 steer 进箱，不跑子进程）
        try:
            import steer as _ST

            fx = dict(
                {
                    "goal": "boss 门夹具",
                    "approved": True,
                    "tasks": [
                        dict(
                            _t("f1", ["run_cmd"]),
                            status="blocked",
                            evidence=["blocked(kind=verify)：判据红"],
                        )
                    ],
                    "bind_sha": "x",
                }
            )
            _dsp.save_plan(fx, sids[5])
            hints = BOSS._blocked_hints(fx)
            _d = os.path.join(ROOT, "work", "steer")
            for tid, hint in hints.items():
                _ST.push(_d, sids[5], "boss 自救 %s：%s" % (tid, hint))
            fx["tasks"][0]["status"] = "todo"
            oks, _ = _dsp.save_plan(fx, sids[5])
            pend, _ = _ST.pending(_d, sids[5])
            gate(
                "B7 自救环（blocked 重开 todo + 换路 steer 进箱）",
                hints.get("f1", "").startswith("判据没过")
                and oks
                and any("boss 自救 f1" in (r.get("text") or "") for r in pend),
                "hint=%s" % (hints.get("f1", "")[:12],),
            )
        except Exception as e:  # noqa: BLE001
            gate(
                "B7 自救环（blocked 重开 todo + 换路 steer 进箱）", False, repr(e)[:120]
            )

        # B8 记忆执行体（memory 提案落账 + 同 sig 去重，隔离记忆账）
        try:
            import tempfile as _tf

            _mem = os.path.join(_tf.mkdtemp(prefix="bossmem_"), "m.md")
            _pj = {
                "id": "w1",
                "sig": "tool-fail:read_file",
                "count": 6,
                "advice": "先读再写",
                "kind": "memory",
                "created": "2026-10-15",
                "status": "pending-human",
            }
            _pp = os.path.join(ROOT, "work", "evo_proposals", "%s.json" % pf)
            os.makedirs(os.path.dirname(_pp), exist_ok=True)
            with open(_pp, "w", encoding="utf-8") as _fh:
                json.dump(_pj, _fh, ensure_ascii=False)
            ok1, _ = BOSS.memory(pf, ROOT, _mem)
            ok2, d2 = BOSS.memory(pf, ROOT, _mem)
            _txt = open(_mem, encoding="utf-8").read()
            os.unlink(_pp)
            gate(
                "B8 记忆执行体（memory 提案落账 + 同 sig 去重）",
                ok1 and ok2 and "先读再写" in _txt and "去重" in d2,
                "dup=%s" % ("去重" in d2,),
            )
        except Exception as e:  # noqa: BLE001
            gate("B8 记忆执行体（memory 提案落账 + 同 sig 去重）", False, repr(e)[:120])

        # B10 并行监工：全 done 计划直接收工（零等待零子进程）
        try:
            _dp = {
                "goal": "boss 门夹具",
                "approved": True,
                "tasks": [
                    dict(
                        _t("q1", ["read_file"]),
                        status="done",
                        evidence=["rc=0"],
                    )
                ],
                "bind_sha": "x",
            }
            _dsp.save_plan(dict(_dp), sids[5])
            ok, detail = BOSS.supervise(
                sids[5], ROOT, interval=0, max_polls=3, full_access=True
            )
            gate(
                "B10 并行监工：全 done 直接收工",
                ok is True and "收工" in detail,
                detail[:40],
            )
        except Exception as e:  # noqa: BLE001
            gate("B10 并行监工：全 done 直接收工", False, repr(e)[:120])

        # B11 并行监工：未审批计划先审后批，再报执行器没在跑
        try:
            _up = {
                "goal": "boss 门夹具",
                "approved": False,
                "tasks": [_t("w1", ["read_file"])],
                "bind_sha": "x",
            }
            _dsp.save_plan(dict(_up), sids[4])
            ok, detail = BOSS.supervise(
                sids[4], ROOT, interval=0, max_polls=3, full_access=True
            )
            _pv, _ = _dsp.load_plan(sids[4])
            gate(
                "B11 并行监工：先审后批＋执行器停摆如实报",
                ok is False and _pv.get("approved") is True and "执行器停" in detail,
                detail[:40],
            )
        except Exception as e:  # noqa: BLE001
            gate("B11 并行监工：先审后批＋执行器停摆如实报", False, repr(e)[:120])
    finally:
        _clean(sids)
        try:
            for d in (
                os.path.join(ROOT, "work", "plans"),
                os.path.join(ROOT, "work", "boss_reviews"),
                os.path.join(ROOT, "work", "steer"),
            ):
                for nm in os.listdir(d):
                    if nm.startswith(pf):
                        os.unlink(os.path.join(d, nm))
        except OSError:
            pass
        for _d in ("boss_reviews", "steer", "evo_proposals"):
            try:
                os.rmdir(os.path.join(ROOT, "work", _d))  # 空才删得掉
            except OSError:
                pass

    # B12 零污染
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    # 零污染口径共享（tools/gate_pollution.py）：**活着的并发自检**目录不算污染
    new, _live_poll = _pollution.pollution_diff(after, before, {args.out})
    gate("B12 零污染（work/ 只多 --out；夹具全清）", not new, "新增=%s" % (new or "无"))
    return _finish(args.out)


if __name__ == "__main__":
    sys.exit(main())
