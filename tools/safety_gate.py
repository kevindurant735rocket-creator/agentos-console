#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safety_gate —— W10 自由面门：默认不摆闸、显式 true 才上锁、硬阻断不可关。

为什么必须有这道门：
  「改开关默认值」最经典的翻车是方向写反——本该自由的反而默认锁死，或者
  上锁路径整个失效、连硬阻断一起放掉。本门站在外面认这些事实（纯离线；
  CLI 侧用隔离 HOME 真跑）：
    D1  _safety_flags 纯面：缺键=自由 / 字面 True 才上锁 / 字符串不认
    D2  `--check` 末行逐字 `AGENT_CORE OK 158/158`（数漂移即红）
    D3  源码锚点：入口两处载入 / 三键站点齐 / CLI 白名单扩到 agent 三键
    D4  默认自由（行为）：入口派生旗标下未审批写放行；裸 ctx（无旗标）
        保守仍锁 —— 闸的钥匙只在旗标上，没旗标不许自个儿松
    D5  模式开关（行为）：显式 mode_gate 下 plan 拒写与写命令；**硬阻断
        仍拒**（git push / 路径形态 / bash -c 载荷三探针）
    D6  计划锁开关（行为）：显式上锁未审批写拦、白名单拦；自由面同跳
    D7  CLI（行为，隔离 HOME）：set true/false 落布尔 rc=0；yes/越权键 rc=2
    D9  命令面开关（行为）：自由面非白名单命令放行且真跑（mkdir 落盘）；
        显式 cmd_gate 恢复只读白名单拒绝
    D8  零污染：work/ 只多 --out；真 ~/.agentos/config.toml 未被门碰

用法: python3 tools/safety_gate.py [--core P] [--agentos P] [--out P]
裁决：SAFETY_GATE OK n/n（rc=0）/ SAFETY_GATE RED（rc=2，有漏网）
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
DEFAULT_AGENTOS = os.path.join(HERE, "agentos.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "safety_gate.json")
EXPECT_CHECK = "AGENT_CORE OK 158/158"

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(
        ("PASS " if ok else "FAIL ")
        + name
        + ("  -- " + str(detail)[:150] if detail else "")
    )


def load_core(path):
    spec = importlib.util.spec_from_file_location("agent_core_g", path)
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
    print("SAFETY_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("SAFETY_GATE_OK")
        return 0
    print("SAFETY_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--agentos", default=DEFAULT_AGENTOS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    print("== safety_gate：自由面契约（默认不摆闸；显式 true 才上锁）")

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        asrc = open(args.core, encoding="utf-8").read()
        gsrc = open(args.agentos, encoding="utf-8").read()
    except OSError as e:
        print("SAFETY_GATE_RED 读不到文件：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="safegate_")

    # D1 纯面
    K = None
    try:
        K = load_core(args.core)
        f1 = K._safety_flags({})
        f2 = K._safety_flags(
            {"agent": {"plan_lock": True, "mode_gate": True, "cmd_gate": True}}
        )
        f3 = K._safety_flags(
            {"agent": {"plan_lock": "true", "mode_gate": 1, "cmd_gate": "yes"}}
        )
        gate(
            "D1 _safety_flags：缺键=自由 / 字面 True 才上锁 / 字符串不认",
            f1 == {"plan_lock_off": True, "mode_gate_off": True, "cmd_gate_off": True}
            and f2
            == {"plan_lock_off": False, "mode_gate_off": False, "cmd_gate_off": False}
            and f3 == f1,
            "free=%s lock=%s str=%s" % (f1, f2, f3),
        )
    except Exception as e:  # noqa: BLE001
        gate(
            "D1 _safety_flags：缺键=自由 / 字面 True 才上锁 / 字符串不认",
            False,
            repr(e)[:120],
        )
    if K is None:
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
    n_load = asrc.count("_sf9 = _safety_flags(_read_agentos_config())")
    n_mode = asrc.count('ctx.get("mode_gate_off")')
    n_lock = asrc.count('ctx.get("plan_lock_off")')
    n_cmd = asrc.count('ctx.get("cmd_gate_off")')
    n_tbl = gsrc.count('table not in ("llm", "gh", "agent")')
    import re as _re

    # 括号可选：格式化工具会把 `(val == "true")` 压成 `val == "true"，
    # 锚点只认「agent 表落布尔」这个事实，不认括号。
    n_bool = len(_re.findall(r'cfg\[table\]\[field\] = \(?val == "true"\)?', gsrc))
    n_keys = gsrc.count('("plan_lock", "mode_gate", "cmd_gate")')
    gate(
        "D3 源码锚点（入口两处载入 / 三键站点齐 / CLI agent 三键落布尔）",
        n_load == 2
        and n_mode >= 3
        and n_lock >= 2
        and n_cmd >= 3
        and n_tbl == 1
        and n_bool == 1
        and n_keys == 1,
        "load=%d mode=%d lock=%d cmd=%d tbl=%d bool=%d keys=%d"
        % (n_load, n_mode, n_lock, n_cmd, n_tbl, n_bool, n_keys),
    )

    tmp_plans = "st-safetygate-%d" % os.getpid()
    tmp2 = "st-safetygate-%d-b" % os.getpid()
    try:
        dsp = K._dsp()
        dsp.save_plan(
            dsp.new_plan(
                "自由面夹具",
                [
                    {
                        "id": "s1",
                        "title": "写",
                        "tools": ["read_file"],
                        "depends": [],
                        "accept": "rc=0",
                    }
                ],
            ),
            tmp_plans,
        )
        wargs = {"path": "x.txt", "content": "x"}
        free = K._safety_flags({})  # 生产入口缺键派生的默认旗标

        # D4 默认自由：入口派生旗标下未审批写放行；裸 ctx（无旗标）保守仍锁
        d4_free = K._authorize(
            K.TOOLS["write_file"],
            wargs,
            dict({"root": tmp, "mode": "full", "sid": tmp_plans}, **free),
        )
        d4_bare = K._authorize(
            K.TOOLS["write_file"],
            wargs,
            {"root": tmp, "mode": "full", "sid": tmp_plans},
        )
        gate(
            "D4 默认自由：入口旗标下未审批写放行；裸 ctx 保守仍锁",
            d4_free is None and isinstance(d4_bare, str) and "未审批" in d4_bare,
            "free=%s bare=%r" % (d4_free is None, str(d4_bare)[:20]),
        )

        # D5 模式开关：显式 mode_gate 下 plan 拒写命令；自由面放行；
        #    硬阻断仍拒（git push / 路径形态 / shell -c 载荷 三探针）
        gated = dict(free, mode_gate_off=False)
        d5_gate = K._authorize(
            K.TOOLS["run_cmd"],
            {"argv": ["python3", "-c", "pass"]},
            dict(gated, root=tmp, mode="plan"),
        )
        d5_free = K._authorize(
            K.TOOLS["run_cmd"],
            {"argv": ["python3", "-c", "pass"]},
            {"root": tmp, "mode": "plan", **free},
        )
        d5_h1 = K._authorize(
            K.TOOLS["run_cmd"],
            {"argv": ["git", "push", "origin", "main"]},
            {"root": tmp, "mode": "full", **free},
        )
        d5_h2 = K._authorize(
            K.TOOLS["run_cmd"],
            {"argv": ["/bin/rm", "-rf", "x"]},
            {"root": tmp, "mode": "full", **free},
        )
        d5_h3 = K._authorize(
            K.TOOLS["run_cmd"],
            {"argv": ["bash", "-c", "rm -rf /tmp/x"]},
            {"root": tmp, "mode": "full", **free},
        )
        gate(
            "D5 模式开关：显式 mode_gate 下 plan 拒写命令；自由面放行；"
            "硬阻断三形态仍拒",
            isinstance(d5_gate, str)
            and d5_free is None
            and isinstance(d5_h1, str)
            and isinstance(d5_h2, str)
            and isinstance(d5_h3, str),
            "gate=%s free=%s h=%s/%s/%s"
            % (
                isinstance(d5_gate, str),
                d5_free is None,
                isinstance(d5_h1, str),
                isinstance(d5_h2, str),
                isinstance(d5_h3, str),
            ),
        )

        # D6 计划锁：显式上锁未审批写拦 + 已审批白名单拦；自由面同跳
        dsp.approve_plan(tmp_plans)
        locked = dict(free, plan_lock_off=False)
        dsp.save_plan(
            dsp.new_plan(
                "自由面夹具二",
                [
                    {
                        "id": "t1",
                        "title": "写",
                        "tools": ["read_file"],
                        "depends": [],
                        "accept": "rc=0",
                    }
                ],
            ),
            tmp2,
        )
        d6_lock = K._authorize(
            K.TOOLS["write_file"], wargs, dict(locked, root=tmp, mode="full", sid=tmp2)
        )
        d6_wl = K._authorize(
            K.TOOLS["list_dir"],
            {"path": "."},
            dict(locked, root=tmp, mode="full", sid=tmp_plans, dsp_task="s1"),
        )
        d6_free = K._authorize(
            K.TOOLS["write_file"],
            wargs,
            {"root": tmp, "mode": "full", "sid": tmp2, **free},
        )
        gate(
            "D6 计划锁：显式上锁未审批写拦 + 已审批白名单拦；自由面同跳",
            isinstance(d6_lock, str)
            and "未审批" in d6_lock
            and isinstance(d6_wl, str)
            and d6_free is None,
            "lock=%r wl=%r free=%s"
            % (str(d6_lock)[:18], str(d6_wl)[:18], d6_free is None),
        )

        # D9 命令面开关：自由面非白名单命令放行且真跑（mkdir 落盘）；
        #    显式 cmd_gate 恢复只读白名单拒绝
        d9_free = K._authorize(
            K.TOOLS["run_cmd"], {"argv": ["mkdir", "probe"]}, {"root": tmp, **free}
        )
        d9_gate = K._authorize(
            K.TOOLS["run_cmd"],
            {"argv": ["mkdir", "probe"]},
            {"root": tmp, **dict(free, cmd_gate_off=False)},
        )
        d9_res, _ = K.exec_tool(
            "run_cmd", {"argv": ["mkdir", "sa-dir"]}, {"root": tmp, **free}
        )
        gate(
            "D9 命令面开关：自由面非白名单命令放行且真跑（mkdir）；"
            "显式 cmd_gate 恢复白名单拒",
            d9_free is None
            and isinstance(d9_gate, str)
            and "白名单" in d9_gate
            and d9_res.get("ok") is True
            and d9_res.get("rc") == 0
            and os.path.isdir(os.path.join(tmp, "sa-dir")),
            "free=%s gate=%r rc=%s"
            % (d9_free is None, str(d9_gate)[:16], d9_res.get("rc")),
        )
    except Exception as e:  # noqa: BLE001
        for nm in (
            "D4 默认自由：入口旗标下未审批写放行；裸 ctx 保守仍锁",
            "D5 模式开关：显式 mode_gate 下 plan 拒写命令；自由面放行；"
            "硬阻断三形态仍拒",
            "D6 计划锁：显式上锁未审批写拦 + 已审批白名单拦；自由面同跳",
            "D9 命令面开关：自由面非白名单命令放行且真跑（mkdir）；"
            "显式 cmd_gate 恢复白名单拒",
        ):
            gate(nm, False, repr(e)[:120])
    finally:
        for sidx in (tmp_plans, tmp2):
            for suf in ("-plan.json", "-chain.jsonl"):
                try:
                    os.unlink(os.path.join(ROOT, "work", "plans", sidx + suf))
                except OSError:
                    pass

    # D7 CLI（隔离 HOME）
    try:
        iso = os.path.join(tmp, "home")
        os.makedirs(iso, exist_ok=True)
        env = dict(os.environ, HOME=iso)
        p1 = subprocess.run(
            [sys.executable, args.agentos, "config", "set", "agent.plan_lock", "true"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        p2 = subprocess.run(
            [sys.executable, args.agentos, "config", "set", "agent.cmd_gate", "false"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        p3 = subprocess.run(
            [sys.executable, args.agentos, "config", "set", "agent.plan_lock", "yes"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        p4 = subprocess.run(
            [sys.executable, args.agentos, "config", "set", "agent.bogus", "false"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        cfgtxt = ""
        try:
            with open(
                os.path.join(iso, ".agentos", "config.toml"), encoding="utf-8"
            ) as f:
                cfgtxt = f.read()
        except OSError:
            pass
        gate(
            "D7 CLI（隔离 HOME）：set true/false 落布尔 rc=0；yes/越权键 rc=2",
            p1.returncode == 0
            and p2.returncode == 0
            and "plan_lock = true" in cfgtxt
            and "cmd_gate = false" in cfgtxt
            and p3.returncode == 2
            and p4.returncode == 2,
            "r1=%s r2=%s r3=%s r4=%s true=%s false=%s"
            % (
                p1.returncode,
                p2.returncode,
                p3.returncode,
                p4.returncode,
                "plan_lock = true" in cfgtxt,
                "cmd_gate = false" in cfgtxt,
            ),
        )
    except Exception as e:  # noqa: BLE001
        gate(
            "D7 CLI（隔离 HOME）：set true/false 落布尔 rc=0；yes/越权键 rc=2",
            False,
            repr(e)[:120],
        )

    # D8 零污染
    shutil.rmtree(tmp, ignore_errors=True)
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    # 零污染口径共享（tools/gate_pollution.py）：**活着的并发自检**目录不算污染
    new, _live_poll = _pollution.pollution_diff(after, before, {args.out})
    gate(
        "D8 零污染（work/ 只多 --out；真 ~/.agentos 未被门碰）",
        not new,
        "新增=%s" % (new or "无"),
    )
    return _finish(args.out, args.core)


if __name__ == "__main__":
    sys.exit(main())
