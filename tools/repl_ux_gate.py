#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repl_ux_gate —— W5 终端 UX 门：Claude Code 手感的四件套真在且真干活。

为什么必须有这道门（W5 波首件交付）：
  终端 UX 的每一件都能「看起来在工作」：续行可以只认不拼、@ 补全可以路由
  丢失、⎿ 详情可以永远空、缩进可以静默不生效 —— 屏上手感全在，判据全无。
  本门站在外面认这些事实（纯离线，不碰真模型）：
    D1  纯面：续行奇偶 / 拼接 / @候选 / ⎿对账 / 缩进 五件函数的边界态
    D2  `--check` 末行逐字 `AGENT_CORE OK 152/152`（数漂移即红）
    D3  源码锚点：续行循环 / @ 路由 / 摘要集成 / 缩进两站点 / ⎿ 渲染 各就位
    D4  续行（行为）：奇=续、偶=字面、逐段拼接语义
    D5  @补全（行为）：前缀匹配 / 目录带斜杠 / 隐藏跳过 / 非 @ 不问
    D6  ⎿详情（行为）：edit_file 行级 ±对账、write_file 行/字节、失败一律空
    D7  事件集成（行为）：run_agent 真跑 edit_file → tool 事件带 summary
    D8  终答（行为）：续行缩进两格、单行不变、空答占位
    D9  零污染：work/ 只多 --out

用法: python3 tools/repl_ux_gate.py [--core PATH] [--out PATH]
裁决：REPL_UX_GATE OK n/n（rc=0）/ REPL_UX_GATE RED（rc=2，有漏网）
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
DEFAULT_OUT = os.path.join(ROOT, "work", "repl_ux_gate.json")
EXPECT_CHECK = "AGENT_CORE OK 152/152"

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


def _tc(fn, args, cid):
    return {"json": {"choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"id": cid, "type": "function",
                                    "function": {"name": fn,
                                                 "arguments":
                                                 json.dumps(args)}}]}}]}}

def _finish(out_path, core_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"core": os.path.relpath(core_path, ROOT),
                   "judgments": results, "n_ok": n_ok,
                   "n_total": len(results)}, fh,
                  ensure_ascii=False, indent=2, sort_keys=True)
    print("REPL_UX_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("REPL_UX_GATE_OK")
        return 0
    print("REPL_UX_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    print("== repl_ux_gate：终端 UX 四件套（core=%s）"
          % os.path.relpath(args.core, ROOT))

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        src = open(args.core, encoding="utf-8").read()
    except OSError as e:
        print("REPL_UX_GATE_RED 读不到 core：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="uxgate_")

    # D1 纯面（可导入 + 五件函数在场）
    K = None
    try:
        K = load_core(args.core)
        need = ("_line_wants_continue", "_compose_join", "_at_completions",
                "_tool_summary", "_format_answer")
        missing = [n for n in need if not hasattr(K, n)]
        gate("D1 可导入 + 五件纯函数齐", not missing,
             "缺=%s" % (missing or "无"))
    except Exception as e:                                     # noqa: BLE001
        gate("D1 可导入 + 五件纯函数齐", False, repr(e)[:120])
    if K is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return _finish(args.out, args.core)

    # D2 --check 逐字令牌
    try:
        p = subprocess.run([sys.executable, args.core, "--check"],
                           capture_output=True, text=True, timeout=900)
        tail = [ln for ln in p.stdout.splitlines() if "AGENT_CORE OK" in ln]
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK,
             p.returncode == 0 and bool(tail)
             and tail[-1].strip() == EXPECT_CHECK,
             "rc=%s tail=%r" % (p.returncode,
                                (tail[-1] if tail else "")[:44]))
    except Exception as e:                                     # noqa: BLE001
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK, False, repr(e)[:120])

    # D3 源码锚点
    n_loop = src.count("while _line_wants_continue(text):")
    n_at = src.count('if text.startswith("@"):')
    n_sum = src.count('"summary": _tool_summary(name, args, res)')
    n_fmt = src.count("% _format_answer(ans)")   # 调用点（def 行不算）
    n_sep = src.count('"  ⎿ %s\\n"')   # 渲染字面量（注释/文档串里的 ⎿ 不算）
    gate("D3 源码锚点（续行循环/@路由/摘要集成/缩进两站点/⎿渲染 各就位）",
         (n_loop, n_at, n_sum, n_fmt, n_sep) == (1, 1, 1, 2, 1),
         "loop=%d at=%d sum=%d fmt=%d sep=%d"
         % (n_loop, n_at, n_sum, n_fmt, n_sep))

    # D4 续行行为
    d4 = (K._line_wants_continue("跑门\\") is True
          and K._line_wants_continue("a\\\\") is False
          and K._line_wants_continue("plain") is False
          and K._compose_join("x\\", "y") == "x\ny"
          and K._compose_join(K._compose_join("a\\", "b"), "c")
          == "a\nb\nc")
    gate("D4 续行：奇=续、偶=字面、compose 拼接语义（行为）", d4,
         "odd=%s even=%s" % (K._line_wants_continue("跑门\\"),
                             K._line_wants_continue("a\\\\")))

    # D5 @补全行为
    ux = os.path.join(tmp, "ux")
    os.makedirs(os.path.join(ux, "sub"), exist_ok=True)
    for rel, body in (("a.txt", "a\n"), ("sub/b.txt", "b\n"),
                      (".h", "h\n")):
        with open(os.path.join(ux, rel), "w", encoding="utf-8") as fh:
            fh.write(body)
    d5 = (K._at_completions("@a", ux) == ["@a.txt"]
          and K._at_completions("@sub/", ux) == ["@sub/b.txt"]
          and sorted(K._at_completions("@", ux)) == ["@a.txt", "@sub/"]
          and K._at_completions("plain", ux) == [])
    gate("D5 @补全：前缀/目录斜杠/隐藏跳过/非 @ 不问（行为）", d5,
         "a=%s all=%s" % (K._at_completions("@a", ux),
                          sorted(K._at_completions("@", ux))))

    # D6 ⎿详情行为
    ts1 = K._tool_summary("edit_file",
                          {"path": "x.py", "old_string": "l1\nl2\nl3",
                           "new_string": "l1\nX\nl3"}, {"ok": True})
    ts2 = K._tool_summary("write_file", {"path": "y.txt",
                                         "content": "a\nb"}, {"ok": True})
    d6 = ("+1 −1" in ts1 and "写入 2 行" in ts2
          and K._tool_summary("edit_file", {"path": "x"}, {"ok": True}) == ""
          and K._tool_summary("read_file", {"path": "x"},
                              {"ok": True}) == ""
          and K._tool_summary("edit_file", {"path": "x", "old_string": "a",
                                            "new_string": "b"},
                              {"ok": False}) == "")
    gate("D6 ⎿详情：±对账/写入行字节/失败与缺参一律空（行为）", d6,
         "edit=%r" % (ts1[:40],))

    # D7 事件集成（run_agent 真跑 edit_file）
    try:
        with open(os.path.join(tmp, "note-e.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("l1\nl2\nl3\n")
        st = K.Stub([_tc("edit_file", {"path": "note-e.txt",
                                       "old_string": "l2",
                                       "new_string": "XX"}, "e1"),
                     _chat("改完了")])
        evs = []
        res = K.run_agent("把第二行改掉",
                          ctx={"root": tmp, "mode": "full"},
                          provider=st.provider(), max_steps=4,
                          on_event=evs.append)
        st.close()
        summ = [e.get("summary") for e in evs
                if e.get("type") == "tool" and e.get("tool") == "edit_file"]
        d7 = bool(res.get("ok")) and bool(summ) \
            and "+1 −1" in (summ[0] or "")
        gate("D7 事件集成：run_agent 真跑 edit_file → tool 事件带 summary",
             d7, "summ=%r" % (summ[:1],))
    except Exception as e:                                     # noqa: BLE001
        gate("D7 事件集成：run_agent 真跑 edit_file → tool 事件带 summary",
             False, repr(e)[:120])

    # D8 终答缩进
    d8 = (K._format_answer("l1\nl2") == "l1\n  l2"
          and K._format_answer("s") == "s"
          and K._format_answer("") == "（无终答）")
    gate("D8 终答：续行缩进两格/单行不变/空答占位（行为）", d8,
         "fmt=%r" % (K._format_answer("l1\nl2"),))

    # D9 零污染
    shutil.rmtree(tmp, ignore_errors=True)
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    new = sorted((after - before) - {os.path.basename(args.out)})
    gate("D9 零污染（work/ 只多 --out；夹具已清）", not new,
         "新增=%s" % (new or "无"))
    return _finish(args.out, args.core)


if __name__ == "__main__":
    sys.exit(main())
