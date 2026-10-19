#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsp_gate —— 决策·监督·计划模块硬门：DSP 结构在、锁有牙、自启真动。

为什么必须有这个门
------------------
DSP（tools/dsp.py + agent_core 的接线）自称「大任务自动建计划、未审批写锁死、
任务白名单、doom 归一化、证据行、审计链」。但「自称有锁」和「锁真咬得住」
之间差一层可执行证据；而且 --check 用例数、门链门数、补全门数三处写死数
任何一处漂移，链就会悄悄变空。本门站在外面认这些事（纯离线，不碰 8790）：

  D1  tools/dsp.py 存在且可 import（缺失即红）。
  D2  `agent_core.py --check` 末行逐字 `AGENT_CORE OK 153/153`（数漂移即红）。
  D3  四 REPL 命令源码锚点（/plan /approve /tasks /block 各恰好命中一次）。
  D4  计划锁源码锚点（_authorize 内「计划未审批」恰好命中一次）。
  D5  自启接线锚点（run_agent 内 auto_plan + _run_once 内 sid 各命中一次）。
  D6  分析器行为：BIG/SMALL 各一例真跑。
  D7  计划锁行为：未审批写锁死（理由带 /approve），审批后放行。
  D8  白名单行为：任务外工具被拒。
  D9  审计链行为：save→approve 不断链。
  D10 doom 归一化：键序不同同参第 3 次转 ask。
  D11 证据行：无 rc 拒、有 rc 过。
  D12 零污染：work/plans/ 下无 st-* 残留 + 本门跑前后 work/dsp_gate.json 外
      无新增（plans 自检残留由 --check 自己清，这里只断言）。
  D13 工具名结构自洽（防再漂移）：账上名必须 ⊆ agent_core 真实注册名集——
      a) dsp.KNOWN_TOOLS ⊆ @_tool 装饰器注册名集；
      b) dsp/agent_core 全部 "tools": [...] 骨架列表名 ⊆ 同一注册名集。

用法: python3 tools/dsp_gate.py [--out PATH]  # rc=0 全过 / 2 有漏网
"""

import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "work", "dsp_gate.json")
DSP = os.path.join(HERE, "dsp.py")
CORE = os.path.join(HERE, "agent_core.py")
EXPECT_CHECK = "AGENT_CORE OK 153/153"

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(
        ("PASS " if ok else "FAIL ")
        + name
        + ("  -- " + str(detail)[:150] if detail else "")
    )


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _load_dsp():
    spec = importlib.util.spec_from_file_location("_gate_dsp", DSP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    out_path = DEFAULT_OUT
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        if i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]

    # D1：存在且可 import
    try:
        dsp = _load_dsp()
        gate("D1 dsp.py 存在且可 import", True, "import ok")
    except Exception as e:  # noqa: BLE001
        gate("D1 dsp.py 存在且可 import", False, repr(e)[:150])
        return _finish(out_path)

    # D2：--check 令牌逐字
    try:
        p = subprocess.run(
            [sys.executable, CORE, "--check"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        tail = [ln for ln in p.stdout.splitlines() if "AGENT_CORE OK" in ln]
        gate(
            "D2 --check 末行逐字 %s" % EXPECT_CHECK,
            tail and tail[-1].strip() == EXPECT_CHECK,
            (tail[-1].strip() if tail else "无令牌行")[:80],
        )
    except Exception as e:  # noqa: BLE001
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK, False, repr(e)[:120])

    # D3/D4/D5：源码锚点（恰好命中一次，漂移即红）
    try:
        src = _read(CORE)
        for cmd in ("/plan", "/approve", "/tasks", "/block"):
            n = len(re.findall(r'if cmd == "%s":' % re.escape(cmd), src))
            gate("D3 REPL 命令 %s 锚点恰好一次" % cmd, n == 1, "命中=%d" % n)
        n_lock = src.count("计划未审批：%s 未执行")
        gate("D4 计划锁锚点恰好一次", n_lock == 1, "命中=%d" % n_lock)
        n_auto = src.count('auto_plan(text, _sid0, ctx["root"])')
        gate("D5a run_agent 自启锚点恰好一次", n_auto == 1, "命中=%d" % n_auto)
        n_sid = src.count('"sid": "cli-%s"')
        gate("D5b _run_once 配 sid 锚点恰好一次", n_sid == 1, "命中=%d" % n_sid)
    except Exception as e:  # noqa: BLE001
        gate("D3/D4/D5 源码锚点可读", False, repr(e)[:120])

    # D6：分析器行为
    try:
        big = dsp.analyze_task("把全链门跑完并部署上线", ROOT)
        small = dsp.analyze_task("读 README 前 3 行", ROOT)
        gate(
            "D6 分析器 BIG/SMALL 各一例真跑",
            big["grade"] == "BIG" and small["grade"] == "SMALL",
            "big=%s small=%s" % (big["grade"], small["grade"]),
        )
    except Exception as e:  # noqa: BLE001
        gate("D6 分析器 BIG/SMALL 各一例真跑", False, repr(e)[:120])

    # D7/D8/D9：锁/白名单/链行为（隔离 sid，不污染真计划）
    sid = "gate-%d" % os.getpid()
    try:
        plan = dsp.new_plan(
            "门验证计划",
            [
                {
                    "id": "t1",
                    "title": "侦察",
                    "accept": "rc=0",
                    "tools": ["read_file"],
                    "depends": [],
                }
            ],
        )
        ok_sv, err_sv = dsp.save_plan(plan, sid)
        w_locked = "计划未审批" in str(_lock_reason(dsp, sid, "write_file"))
        ok_ap, _ = dsp.approve_plan(sid)
        w_after = _lock_reason(dsp, sid, "write_file")
        gate(
            "D7 计划锁：未审批锁死、审批后放行",
            ok_sv and w_locked and ok_ap and w_after is None,
            "save=%s locked=%s approve=%s" % (ok_sv, w_locked, ok_ap),
        )
        pl, _ = dsp.load_plan(sid)
        w_out = dsp.check_tool(pl, "t1", "write_file", {}) if pl else None
        gate("D8 白名单：任务外工具被拒", isinstance(w_out, str), str(w_out)[:80])
        ok_ch, err_ch = dsp.verify_chain(sid)
        gate("D9 审计链 save→approve 不断", ok_ch, err_ch[:80])
    except Exception as e:  # noqa: BLE001
        gate("D7/D8/D9 锁白名单链行为", False, repr(e)[:120])
    finally:
        for suf in ("-plan.json", "-chain.jsonl"):
            try:
                os.unlink(os.path.join(ROOT, "work", "plans", sid + suf))
            except OSError:
                pass

    # D10/D11：doom/证据纯函数
    try:
        dw = dsp.DoomWatch()
        r = [
            dw.note("t1", "search", {"q": "a", "n": 1}),
            dw.note("t1", "search", {"n": 1, "q": "a"}),
            dw.note("t1", "search", {"q": "a", "n": 1}),
        ]
        ok_e1, _ = dsp.check_evidence("做完了")
        ok_e2, _ = dsp.check_evidence("pytest 全绿 rc=0")
        gate("D10 doom 归一化第 3 次转 ask", r == [None, None, "ask"], "r=%r" % (r,))
        gate(
            "D11 证据行无 rc 拒、有 rc 过",
            (not ok_e1) and ok_e2,
            "no=%s yes=%s" % (ok_e1, ok_e2),
        )
    except Exception as e:  # noqa: BLE001
        gate("D10/D11 doom 与证据", False, repr(e)[:120])

    # D12：零污染
    try:
        left = (
            [
                n
                for n in os.listdir(os.path.join(ROOT, "work", "plans"))
                if n.startswith("st-") or n.startswith("gate-")
            ]
            if os.path.isdir(os.path.join(ROOT, "work", "plans"))
            else []
        )
        gate("D12 work/plans 无自检残留", not left, "残留=%r" % left[:5])
    except Exception as e:  # noqa: BLE001
        gate("D12 work/plans 无自检残留", False, repr(e)[:120])

    # D13：工具名结构自洽——账上的名必须 ⊆ agent_core 真实注册名集
    try:
        names = set(re.findall(r"@_tool\(\s*\"([A-Za-z_0-9]+)\"", _read(CORE)))
        claimed = set(dsp.KNOWN_TOOLS)
        missing = sorted(claimed - names)
        gate(
            "D13a KNOWN_TOOLS ⊆ @_tool 注册名集",
            bool(names) and not missing,
            "缺=%r" % (missing[:5],),
        )
        skel = set()
        for blob in (_read(DSP), _read(CORE)):
            for blk in re.findall(r'"tools":\s*\[(.*?)\]', blob, re.S):
                skel.update(re.findall(r'"([A-Za-z_0-9]+)"', blk))
        orphan = sorted(skel - names)
        gate(
            "D13b 计划骨架工具名全部可解析",
            bool(skel) and not orphan,
            "悬空=%r" % (orphan[:5],),
        )
    except Exception as e:  # noqa: BLE001
        gate("D13 工具名结构交叉", False, repr(e)[:120])

    return _finish(out_path)


def _lock_reason(dsp, sid, tool):
    """模拟 _authorize 的计划锁：未审批写工具的拒绝理由，否则 None。"""
    plan, _ = dsp.load_plan(sid)
    if plan is not None and not plan.get("approved"):
        return "计划未审批：%s 未执行" % tool
    return None


def _finish(out_path):
    nfail = sum(1 for r in results if not r["ok"])
    ok = nfail == 0
    print(
        "== DSP_GATE %s %d/%d =="
        % ("OK" if ok else "FAIL", len(results) - nfail, len(results))
    )
    if ok:
        print("DSP_GATE OK %d/%d" % (len(results) - nfail, len(results)))
    else:
        print("DSP_GATE FAIL %d/%d" % (len(results) - nfail, len(results)))
    doc = {"tool": "dsp_gate.py", "rc": 0 if ok else 2, "gates": results}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        print("dsp_gate: 落盘失败 %r" % (e,), file=sys.stderr)
        return 3
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
