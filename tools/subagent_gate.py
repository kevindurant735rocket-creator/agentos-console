#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subagent_gate —— W2 子代理门：可重入、禁嵌套、步数钳、子集闸、权限继承。

为什么必须有这道门（W2 波的结构性最大件）：
  subagent-as-tool 的每一层护栏都能「看起来在工作」：禁嵌套可以只是个
  提示词、步数上限可以被参数顶穿、工具子集可以只出现在 schema 里、
  权限可以不继承外层 —— 桩模型换个说法全都能绕。本门站在外面认这些
  事实（纯离线，桩在回环；真跑 run_agent 递归一次）：
    D1  可 import + 符号齐（TOOLS 含 subagent / SUBAGENT_MAX_STEPS=12）
    D2  `--check` 末行逐字 `AGENT_CORE OK 140/140`（数漂移即红）
    D3  源码锚点四元：工具本体 / 嵌套闸 / 步数钳 / 子集闸 各恰 1
    D4  可重入（行为）：外层停→内层终答→外层继续（序贯桩恰 3 请求）
    D5  禁嵌套（行为）：depth>0 直接拒 + 零请求（fail-closed）
    D6  步数封顶（行为）：max_steps=99 钳到 12（请求数=钳值）
    D7  子集强制（行为）：子集外工具拒 + 真跑不落盘
    D8  权限继承（行为）：外层 plan 模式 → 内层写被拒、文件未落
    D9  零污染：work/ 只多 --out；夹具在系统临时目录并清理

用法: python3 tools/subagent_gate.py [--core PATH] [--out PATH]
裁决：SUBAGENT_GATE OK n/n（rc=0）/ SUBAGENT_GATE RED（rc=2，有漏网）
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
DEFAULT_CORE = os.path.join(HERE, "agent_core.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "subagent_gate.json")
EXPECT_CHECK = "AGENT_CORE OK 140/140"

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(("PASS " if ok else "FAIL ") + name
          + ("  -- " + str(detail)[:150] if detail else ""))


def load_core(path):
    spec = importlib.util.spec_from_file_location("agent_core_g", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _chat(text):
    return {"json": {"choices": [{"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant",
                                              "content": text}}]}}


def _toolcall(path, cid, name="read_file"):
    return {"json": {"choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"id": cid, "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps({"path": path})
                                    }}]}}]}}


def _finish(out_path, core_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"core": os.path.relpath(core_path, ROOT),
                   "judgments": results, "n_ok": n_ok,
                   "n_total": len(results)}, fh,
                  ensure_ascii=False, indent=2, sort_keys=True)
    print("SUBAGENT_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("SUBAGENT_GATE_OK")
        return 0
    print("SUBAGENT_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    core_path, out_path = args.core, args.out
    print("== subagent_gate：subagent-as-tool（core=%s）"
          % os.path.relpath(core_path, ROOT))

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        src = open(core_path, encoding="utf-8").read()
    except OSError as e:
        print("SUBAGENT_GATE_RED 读不到 core：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="sagate_")

    # D1 可 import + 符号齐
    K = None
    try:
        K = load_core(core_path)
        ok1 = ("subagent" in K.TOOLS and K.SUBAGENT_MAX_STEPS == 12
               and hasattr(K, "run_agent") and hasattr(K, "exec_tool")
               and hasattr(K, "Stub"))
        gate("D1 可 import + 符号齐（TOOLS 含 subagent、钳=12）", ok1,
             "subagent_in=%s cap=%s" % ("subagent" in K.TOOLS,
                                        K.SUBAGENT_MAX_STEPS))
    except Exception as e:                                     # noqa: BLE001
        gate("D1 可 import + 符号齐（TOOLS 含 subagent、钳=12）", False,
             repr(e)[:120])
    if K is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return _finish(out_path, core_path)

    # D2 --check 逐字令牌
    try:
        p = subprocess.run([sys.executable, core_path, "--check"],
                           capture_output=True, text=True, timeout=900)
        tail = [ln for ln in p.stdout.splitlines() if "AGENT_CORE OK" in ln]
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK,
             p.returncode == 0 and bool(tail)
             and tail[-1].strip() == EXPECT_CHECK,
             "rc=%s tail=%r" % (p.returncode,
                                (tail[-1] if tail else "")[:44]))
    except Exception as e:                                     # noqa: BLE001
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK, False, repr(e)[:120])

    # D3 源码锚点四元
    n_fn = src.count("def _t_subagent")
    n_depth = src.count("if depth > 0:")
    n_cap = src.count("max(1, min(SUBAGENT_MAX_STEPS, want))")
    n_sub = src.count("if isinstance(subset, list) and name not in subset:")
    gate("D3 源码锚点（工具本体/嵌套闸/步数钳/子集闸 各恰 1）",
         (n_fn, n_depth, n_cap, n_sub) == (1, 1, 1, 1),
         "fn=%d depth=%d cap=%d subset=%d"
         % (n_fn, n_depth, n_cap, n_sub))

    # D4 可重入（序贯共享桩：外层#1 → 内层#1 → 外层#2）
    try:
        sub = K.Stub([{"json": {"choices": [{"index": 0,
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": "",
                        "tool_calls": [{"id": "c0", "type": "function",
                            "function": {"name": "subagent",
                                "arguments": json.dumps({
                                    "prompt": "数数",
                                    "tools": ["list_dir"],
                                    "max_steps": 3})}}]}}]}},
            _chat("子代理结论：42"), _chat("外围结论：收到")])
        ores = K.run_agent("先把活派给子代理",
                           ctx={"root": tmp, "mode": "accept-edits"},
                           provider=sub.provider(), max_steps=4)
        tmsg = [m for m in ores.get("messages") or []
                if m.get("role") == "tool"]
        rep = {}
        try:
            rep = json.loads(tmsg[0]["content"]) if tmsg else {}
        except ValueError:
            rep = {}
        sub.close()
        gate("D4 可重入：外层停→内层终答→外层继续（3 请求）",
             bool(ores.get("ok")) and len(sub.seen) == 3
             and rep.get("summary") == "子代理结论：42"
             and rep.get("steps") == 1 and rep.get("rc") == 0,
             "seen=%d summary=%r" % (len(sub.seen),
                                     str(rep.get("summary"))[:24]))
    except Exception as e:                                     # noqa: BLE001
        gate("D4 可重入：外层停→内层终答→外层继续（3 请求）", False,
             repr(e)[:120])

    # D5 禁嵌套（直调，零请求）
    try:
        nst = K.Stub([_chat("不应被调用")])
        nres, _ = K.exec_tool("subagent", {"prompt": "再来一层"},
                              {"root": tmp, "mode": "accept-edits",
                               "subagent_depth": 1,
                               "_provider": nst.provider()})
        gate("D5 禁嵌套：depth>0 直接拒 + 零请求（fail-closed）",
             nres.get("ok") is False and nres.get("executed") is False
             and "禁止嵌套" in str(nres.get("error")) and not nst.seen,
             "err=%r seen=%d" % (str(nres.get("error"))[:30],
                                 len(nst.seen)))
        nst.close()
    except Exception as e:                                     # noqa: BLE001
        gate("D5 禁嵌套：depth>0 直接拒 + 零请求（fail-closed）", False,
             repr(e)[:120])

    # D6 步数封顶（99 → 12，请求数=钳值）
    try:
        # W11.1 夹具补齐：读**真存在**的文件 —— 否则新守卫②（同工具连续
        # 失败 ≥6 停）第 6 步就叫停，测不到「钳到 12」的本意；排列仍逐文件
        # 变化（不触 MAX_REPEAT/TEXT_REPEAT）。
        for _i in range(40):
            with open(os.path.join(tmp, "n%d.txt" % _i), "w",
                      encoding="utf-8") as _f:
                _f.write("x")
        mst = K.Stub([_toolcall("n%d.txt" % i, "c%d" % i)
                      for i in range(40)])
        mres, _ = K.exec_tool("subagent",
                              {"prompt": "无限读", "max_steps": 99},
                              {"root": tmp, "mode": "accept-edits",
                               "_provider": mst.provider()})
        gate("D6 步数封顶：99 钳到 %d（请求数=钳值）"
             % K.SUBAGENT_MAX_STEPS,
             mres.get("limit") is True
             and mres.get("steps") == K.SUBAGENT_MAX_STEPS
             and len(mst.seen) == K.SUBAGENT_MAX_STEPS,
             "steps=%s seen=%d" % (mres.get("steps"), len(mst.seen)))
        mst.close()
    except Exception as e:                                     # noqa: BLE001
        gate("D6 步数封顶：99 钳到 %d（请求数=钳值）"
             % K.SUBAGENT_MAX_STEPS, False, repr(e)[:120])

    # D7 子集强制（拒 + 不落盘）
    try:
        why_ss = K._authorize(K.TOOLS["write_file"],
                              {"path": "sx.txt", "content": "x"},
                              {"root": tmp, "mode": "full",
                               "tool_subset": ["read_file"]})
        r_ss, _ = K.exec_tool("write_file", {"path": "sx.txt",
                                             "content": "x"},
                              {"root": tmp, "mode": "full",
                               "tool_subset": ["read_file"]})
        gate("D7 子集强制：子集外工具拒 + 真跑不落盘",
             isinstance(why_ss, str) and "子集" in why_ss
             and r_ss.get("executed") is False
             and not os.path.exists(os.path.join(tmp, "sx.txt")),
             "why=%r ex=%s" % (str(why_ss)[:30], r_ss.get("executed")))
    except Exception as e:                                     # noqa: BLE001
        gate("D7 子集强制：子集外工具拒 + 真跑不落盘", False, repr(e)[:120])

    # D8 权限继承（外层 plan → 内层写拒、文件未落）
    try:
        ist = K.Stub([{"json": {"choices": [{"index": 0,
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": "",
                        "tool_calls": [{"id": "w1", "type": "function",
                            "function": {"name": "write_file",
                                "arguments": json.dumps({
                                    "path": "sp.txt",
                                    "content": "x"})}}]}}]}},
            _chat("子代理：写被拒了")])
        ires, _ = K.exec_tool("subagent",
                              {"prompt": "试着写个文件",
                               "tools": ["write_file"], "max_steps": 4},
                              {"root": tmp, "mode": "plan",
                               "_provider": ist.provider()})
        gate("D8 权限继承：外层 plan 内层写被拒、文件未落",
             ires.get("ok") is True and len(ist.seen) == 2
             and not os.path.exists(os.path.join(tmp, "sp.txt")),
             "ok=%s seen=%d" % (ires.get("ok"), len(ist.seen)))
        ist.close()
    except Exception as e:                                     # noqa: BLE001
        gate("D8 权限继承：外层 plan 内层写被拒、文件未落", False,
             repr(e)[:120])

    # D10 并行批次（barrier 确定性证伪：串行 ⇒ BrokenBarrier ⇒ 红）
    try:
        import threading as _th
        bar = _th.Barrier(3, timeout=8)

        class _BP(object):
            model = "barrier"
            base_url = "stub"
            source = "barrier"
            session_id = "bar"
            protocol = "chat"

            def chat(self, messages, tools=None, stream=False,
                     on_event=None, cancel=None):
                bar.wait()
                return {"content": "批内完成", "tool_calls": [],
                        "usage": {}, "finish_reason": "stop"}
        bres, _ = K.exec_tool("subagent",
                              {"prompts": ["甲", "乙", "丙"],
                               "max_steps": 2},
                              {"root": tmp, "mode": "plan",
                               "_provider": _BP()})
        brows = bres.get("results") or []
        gate("D10 并行批次：barrier 三线同到（串行必 BrokenBarrier）",
             bres.get("ok") is True and bres.get("parallel") is True
             and len(brows) == 3 and all(r.get("ok") for r in brows),
             "ok=%s n=%d" % (bres.get("ok"), len(brows)))
    except Exception as e:                                     # noqa: BLE001
        gate("D10 并行批次：barrier 三线同到（串行必 BrokenBarrier）",
             False, repr(e)[:120])

    # D11 批量边界（5 个/空表 fail-closed；不触网）
    try:
        bst = K.Stub([_chat("x")])
        r_lim, _ = K.exec_tool("subagent", {"prompts": ["a"] * 5},
                               {"root": tmp, "mode": "plan",
                                "_provider": bst.provider()})
        r_empty, _ = K.exec_tool("subagent", {"prompts": []},
                                 {"root": tmp, "mode": "plan",
                                  "_provider": bst.provider()})
        bst.close()
        gate("D11 批量边界：5 个/空表拒（fail-closed）",
             r_lim.get("executed") is False
             and str(K.SUBAGENT_PARALLEL_MAX) in str(r_lim.get("error"))
             and r_empty.get("executed") is False,
             "lim=%r" % (str(r_lim.get("error"))[:40],))
    except Exception as e:                                     # noqa: BLE001
        gate("D11 批量边界：5 个/空表拒（fail-closed）", False,
             repr(e)[:120])

    # D12 失败隔离（单任务炸不拖垮其余、按输入序）
    try:
        class _Picky(object):
            model = "picky"
            base_url = "stub"
            source = "picky"
            session_id = "pick"
            protocol = "chat"

            def chat(self, messages, tools=None, stream=False,
                     on_event=None, cancel=None):
                last = str((messages or [{}])[-1].get("content") or "")
                if "boom" in last:
                    raise K.AgentError("provider", "boom 任务失败")
                return {"content": "好:%s" % last[:8], "tool_calls": [],
                        "usage": {}, "finish_reason": "stop"}
        fres, _ = K.exec_tool("subagent",
                              {"prompts": ["好一", "boom 掉", "好二"],
                               "max_steps": 2},
                              {"root": tmp, "mode": "plan",
                               "_provider": _Picky()})
        fr = fres.get("results") or []
        gate("D12 失败隔离：单任务炸不拖垮其余、按输入序",
             fres.get("ok") is True and len(fr) == 3
             and fr[0].get("ok") and not fr[1].get("ok")
             and fr[2].get("ok"),
             "oks=%s" % ([r.get("ok") for r in fr],))
    except Exception as e:                                     # noqa: BLE001
        gate("D12 失败隔离：单任务炸不拖垮其余、按输入序", False,
             repr(e)[:120])

    # D13 chain 交接（行为）：上一段结论带进下一段
    try:
        cst = K.Stub([_chat("甲结论：先做 A"), _chat("乙结论：A 已带过")])
        cres, _ = K.exec_tool("subagent",
                              {"chain": ["任务甲", "任务乙"], "max_steps": 2},
                              {"root": tmp, "mode": "plan",
                               "_provider": cst.provider()})
        cseen2 = json.dumps(cst.seen[1], ensure_ascii=False)
        cst.close()
        gate("D13 chain 交接：上一段结论进下一段 prompt、按序返回",
             cres.get("ok") is True and cres.get("chain") is True
             and len(cres.get("results") or []) == 2
             and "上一阶段结论" in cseen2 and "甲结论：先做 A" in cseen2,
             "ok=%s handoff=%s" % (cres.get("ok"),
                                   "上一阶段结论" in cseen2))
    except Exception as e:                                     # noqa: BLE001
        gate("D13 chain 交接：上一段结论进下一段 prompt、按序返回", False,
             repr(e)[:120])

    # D14 chain fail-closed：中途段失败即停
    try:
        class _Boom(object):
            model = "b"
            base_url = "stub"
            source = "b"
            session_id = "b"
            protocol = "chat"

            def chat(self, messages, tools=None, stream=False,
                     on_event=None, cancel=None):
                last = str((messages or [{}])[-1].get("content") or "")
                if "boom" in last:
                    raise K.AgentError("provider", "boom 段失败")
                return {"content": "好:%s" % last[:8], "tool_calls": [],
                        "usage": {}, "finish_reason": "stop"}
        bres, _ = K.exec_tool("subagent",
                              {"chain": ["正常段", "boom 段", "第三段"],
                               "max_steps": 2},
                              {"root": tmp, "mode": "plan",
                               "_provider": _Boom()})
        brs = bres.get("results") or []
        gate("D14 chain fail-closed：中途段失败即停（stopped_at=1）",
             bres.get("ok") is False and bres.get("stopped_at") == 1
             and len(brs) == 2 and brs[0].get("ok")
             and not brs[1].get("ok") and "第 2 段" in str(bres.get("summary")),
             "stopped=%s n=%d" % (bres.get("stopped_at"), len(brs)))
    except Exception as e:                                     # noqa: BLE001
        gate("D14 chain fail-closed：中途段失败即停（stopped_at=1）", False,
             repr(e)[:120])

    # D15 merge：批量后额外一次汇总出 digest
    try:
        mst = K.Stub([_chat("子一"), _chat("子二"), _chat("汇总裁决Z")])
        mres, _ = K.exec_tool("subagent",
                              {"prompts": ["p1", "p2"], "merge": True,
                               "max_steps": 2},
                              {"root": tmp, "mode": "plan",
                               "_provider": mst.provider()})
        n_req = len(mst.seen)
        mseen3 = json.dumps(mst.seen[2], ensure_ascii=False) \
            if n_req >= 3 else ""
        mst.close()
        gate("D15 merge：批量后额外一次汇总出 digest（3 请求）",
             mres.get("ok") is True and mres.get("digest") == "汇总裁决Z"
             and n_req == 3 and "子一" in mseen3 and "子二" in mseen3,
             "digest=%r n=%d" % (str(mres.get("digest"))[:12], n_req))
    except Exception as e:                                     # noqa: BLE001
        gate("D15 merge：批量后额外一次汇总出 digest（3 请求）", False,
             repr(e)[:120])

    # D9 零污染
    shutil.rmtree(tmp, ignore_errors=True)
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    new = sorted((after - before) - {os.path.basename(out_path)})
    gate("D9 零污染（work/ 只多 --out；夹具已清）", not new,
         "新增=%s" % (new or "无"))
    return _finish(out_path, core_path)


if __name__ == "__main__":
    sys.exit(main())
