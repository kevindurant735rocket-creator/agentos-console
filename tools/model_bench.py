#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""model_bench —— **支持清单里那两个模型**的同题对比（要真端点，不进门链）。

为什么有它（2026-10-22 用户定：只加入 deepseek-v4.1-flash 与
mimo-v2.6-flash，不逐个测端点那 36 个）
------------------------------------------------
产品只支持两个模型，就得知道这两个**在真实任务上表现如何** —— 步数、
峰值 prompt、工具调用数、墙钟。这比「模型名在不在 /models 列表里」有用
得多：列表只证明端点认它，对比才证明**这个 agent 用得动它**。

三个任务各测一类能力：
  A 实现：写代码 + todo 账本 + 验证（feature 契约）
  B 阅读：读文件 + 搜索 + 总结（analysis 契约）
  C 修错：先复现红 → 再改 → 再绿（bugfix 契约，红→绿链）

用法：
    python3 tools/model_bench.py                 # 跑全表（要网络与 key）
    python3 tools/model_bench.py --task B        # 只跑一个任务
    python3 tools/model_bench.py --json work/model_bench.json

不进正确链（它打真端点、要 key、要花几分钟）—— 门链只管**离线可复跑**
的东西。账本写 work/model_bench.json，供人看/对比。
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

TASKS = {
    "A": (
        "实现",
        "在当前目录建 calc.py，实现 add(a, b) 返回 a+b；用 todo 列一条清单"
        "并在完成后标 done；最后跑 python3 -c 'import calc; print(calc.add(2,3))'"
        " 确认输出 5。",
    ),
    "B": (
        "阅读",
        "读 tools/todo.py 的前 120 行，然后用 search 找出它里 def 的条数，"
        "用不超过 60 字总结这个模块的职责。",
    ),
    "C": (
        "修错",
        "当前目录有一个 buggy.py：add(a,b) 错把加号写成减号。请先跑它看输出错在"
        "哪，再修好，并跑一次确认结果对。",
    ),
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="", help="只跑一个任务（A/B/C）")
    ap.add_argument("--json", default=os.path.join(ROOT, "work", "model_bench.json"))
    args = ap.parse_args(argv)

    import agent_core as ac
    import agent_provider as ap_mod

    models = list(ap_mod.SUPPORTED_MODELS)
    rows = []
    for key, (name, task) in TASKS.items():
        if args.task and key.upper() != args.task.upper():
            continue
        for m in models:
            work = "/tmp/model_bench_%s_%s" % (key, m.split("-")[0])
            shutil.rmtree(work, ignore_errors=True)
            os.makedirs(work)
            if key == "C":
                with open(os.path.join(work, "buggy.py"), "w", encoding="utf-8") as f:
                    f.write("def add(a, b):\n    return a - b\n")
            os.environ["AGENTOS_LLM_MODEL"] = m
            t0 = time.time()
            try:
                res = ac.run_agent(
                    task,
                    ctx={"root": work, "mode": "full", "sid": "mb"},
                    max_steps=14,
                )
                u = res.get("usage") or {}
                row = {
                    "task": "%s %s" % (key, name),
                    "model": m,
                    "ok": bool(res.get("ok")),
                    "steps": res.get("steps"),
                    "peak_prompt": u.get("prompt_tokens"),
                    "completion": u.get("completion_tokens"),
                    "tools": len(res.get("tools_used") or []),
                    "sec": round(time.time() - t0, 1),
                    "error": str(res.get("error") or "")[:100],
                }
            except Exception as e:  # noqa: BLE001
                row = {
                    "task": "%s %s" % (key, name),
                    "model": m,
                    "ok": False,
                    "steps": 0,
                    "peak_prompt": 0,
                    "completion": 0,
                    "tools": 0,
                    "sec": round(time.time() - t0, 1),
                    "error": repr(e)[:120],
                }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False))
    try:
        os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print("账本写不了（不致命）：%r" % (e,))
    print()
    print("%-8s %-22s %-5s %-4s %-11s %-4s %-6s"
          % ("任务", "模型", "ok", "步", "峰值prompt", "工具", "秒"))
    for r in rows:
        print("%-8s %-22s %-5s %-4s %-11s %-4s %-6s"
              % (r["task"], r["model"], r["ok"], r["steps"], r["peak_prompt"],
                 r["tools"], r["sec"]))
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
