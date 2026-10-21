#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""supervise_gate —— W8 老板监督门：steer 指令真送达、每条只进一次、watch 真看得见。

为什么必须有这道门：
  监督通道的每一层都能「看起来在工作」：指令可以投了不进、可以重复进、
  可以进错会话；监督面可以永远空着。本门站在外面认这些事实（纯离线）：
    D1  steer 纯面：往返 / 游标只进不退 / 坏 sid 拒（防路径穿越）
    D2  `--check` 末行逐字 `AGENT_CORE OK 171/171`（数漂移即红）
    D3  源码锚点：步骤边界消费 / /steer / /watch / 补全表 / CLI 两子命令 +
        bash 补全字面 各就位
    D4  注入（行为）：指令进本轮消息 [老板指令…]（走真 run_agent）
    D5  幂等（行为）：已消费指令不再进第二轮（游标生效）
    D6  监督面（行为）：计划任务/会话尾进屏、无计划如实说
    D7  隔离（行为）：A 会话的指令不出现在 B 会话
    D8  CLI（行为）：`agentos watch --once` 出屏 rc=0；坏 sid 的 steer rc=2 零写入
    D9  零污染：work/ 只多 --out（真 work/steer 不被门创建）

用法: python3 tools/supervise_gate.py [--core P] [--agentos P] [--out P]
裁决：SUPERVISE_GATE OK n/n（rc=0）/ SUPERVISE_GATE RED（rc=2，有漏网）
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
DEFAULT_OUT = os.path.join(ROOT, "work", "supervise_gate.json")
EXPECT_CHECK = "AGENT_CORE OK 171/171"

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


def _chat(text):
    return {
        "json": {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ]
        }
    }


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
    print("SUPERVISE_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("SUPERVISE_GATE_OK")
        return 0
    print("SUPERVISE_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--agentos", default=DEFAULT_AGENTOS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    print("== supervise_gate：老板监督（steer/watch）")

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        asrc = open(args.core, encoding="utf-8").read()
        gsrc = open(args.agentos, encoding="utf-8").read()
    except OSError as e:
        print("SUPERVISE_GATE_RED 读不到文件：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="svgate_")

    # D1 steer 纯面
    SR = None
    try:
        SR = load_mod(os.path.join(HERE, "steer.py"), "steer_g")
        box = os.path.join(tmp, "box")
        ok1 = (
            SR.push(box, "sA", "一号")[0]
            and SR.push(box, "sA", "二号")[0]
            and [x["text"] for x in SR.pending(box, "sA")[0]] == ["一号", "二号"]
            and SR.commit(box, "sA", 1)[0]
            and [x["text"] for x in SR.pending(box, "sA")[0]] == ["二号"]
            and SR.commit(box, "sA", 0)[0]
            and len(SR.pending(box, "sA")[0]) == 1
            and SR.push(box, "../evil", "x")[0] is False
            and SR.commit(box, "..", 1)[0] is False
        )
        gate("D1 steer 纯面：往返/游标只进/坏 sid 拒", ok1, "往返=%s" % ok1)
    except Exception as e:  # noqa: BLE001
        gate("D1 steer 纯面：往返/游标只进/坏 sid 拒", False, repr(e)[:120])
    if SR is None:
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
    n_take = asrc.count("_stq = _steer_take(ctx)")
    n_steer = asrc.count('if cmd == "/steer":')
    n_watch = asrc.count('if cmd == "/watch":')
    import re as _re

    n_tbl = len(_re.findall(r'"/steer",\s*"/watch",', asrc))
    # 括号内可换行：格式化工具会把 add("steer", …) 拆成多行，
    # 锚点只认「steer/watch 两命令注册了」这个事实，不认排版。
    n_a1 = len(_re.findall(r'add\(\s*"steer"', gsrc))
    n_a2 = len(_re.findall(r'add\(\s*"watch"', gsrc))
    # bash 补全串：判「steer/watch 真在补全表里」，**不钉串尾字面**。
    # 原来钉的是 'steer watch leader"' —— 2026-10-19 给补全表加了
    # doctor/evolve/replay 之后，串尾变成 '... leader doctor evolve replay"'，
    # 锚点当场失配（与 supervise 功能本身无关）。判据的意图是
    # 「这两个命令能被 Tab 补出来」，那就按集合成员判，别按排版判。
    _ms = _re.search(r'local subs="([^"]*)"', gsrc)
    _subs_set = set((_ms.group(1) if _ms else "").split())
    n_bash = 1 if ("steer" in _subs_set and "watch" in _subs_set) else 0
    gate(
        "D3 源码锚点（边界消费//steer//watch/补全表/CLI 两命令/bash 字面）",
        (n_take, n_steer, n_watch, n_tbl, n_a1, n_a2, n_bash) == (1, 1, 1, 1, 1, 1, 1),
        "take=%d steer=%d watch=%d tbl=%d a1=%d a2=%d bash=%d"
        % (n_take, n_steer, n_watch, n_tbl, n_a1, n_a2, n_bash),
    )

    # D4/D5 注入 + 幂等（走真 run_agent）
    K = None
    try:
        K = load_mod(args.core, "agent_core_g")
        box2 = os.path.join(tmp, "box2")
        stub = K.Stub([_chat("收到")])
        K._STEER_DIR_OVERRIDE.append(box2)
        try:
            SR.push(box2, "st-sv", "只改 README")
            r1 = K.run_agent(
                "干活",
                ctx={"root": tmp, "mode": "plan", "sid": "st-sv"},
                provider=stub.provider(),
                max_steps=2,
            )
            seen1 = json.dumps(stub.seen[0], ensure_ascii=False)
            K.run_agent(
                "再干",
                ctx={"root": tmp, "mode": "plan", "sid": "st-sv"},
                provider=stub.provider(),
                max_steps=2,
            )
            seen2 = json.dumps(stub.seen[1], ensure_ascii=False)
        finally:
            K._STEER_DIR_OVERRIDE.pop()
            stub.close()
        gate(
            "D4 注入：指令进本轮消息（[老板指令…]，真 run_agent）",
            bool(r1.get("ok")) and "老板指令" in seen1 and "只改 README" in seen1,
            "seen=%s" % ("老板指令" in seen1),
        )
        gate(
            "D5 幂等：已消费指令不再进第二轮（游标生效）",
            "只改 README" not in seen2,
            "again=%s" % ("只改 README" in seen2),
        )
    except Exception as e:  # noqa: BLE001
        gate(
            "D4 注入：指令进本轮消息（[老板指令…]，真 run_agent）", False, repr(e)[:120]
        )
        gate("D5 幂等：已消费指令不再进第二轮（游标生效）", False, repr(e)[:120])

    # D6 监督面渲染
    try:
        WT = load_mod(os.path.join(HERE, "watch.py"), "watch_g")
        wdir = os.path.join(tmp, "wroot", "work", "agent_sessions")
        os.makedirs(wdir, exist_ok=True)
        with open(os.path.join(wdir, "st-sv-tail.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 1.0, "kind": "user", "text": "尾部一"}) + "\n")
            fh.write(json.dumps({"ts": 2.0, "kind": "note", "text": "尾部二"}) + "\n")
        rep = WT.render(WT.snapshot(os.path.join(tmp, "wroot"), "st-sv-tail"))
        gate(
            "D6 监督面：会话尾进屏 + 无计划如实说",
            "尾部一" in rep
            and "尾部二" in rep
            and "计划：无" in rep
            and "老板指令" in rep,
            "len=%d" % len(rep),
        )
    except Exception as e:  # noqa: BLE001
        gate("D6 监督面：会话尾进屏 + 无计划如实说", False, repr(e)[:120])

    # D7 隔离
    box3 = os.path.join(tmp, "box3")
    SR.push(box3, "sA", "给 A 的")
    gate(
        "D7 隔离：A 会话的指令不出现在 B 会话",
        len(SR.pending(box3, "sA")[0]) == 1 and len(SR.pending(box3, "sB")[0]) == 0,
        "A=%d B=%d" % (len(SR.pending(box3, "sA")[0]), len(SR.pending(box3, "sB")[0])),
    )

    # D8 CLI（watch --once 出屏 rc=0；坏 sid steer rc=2 且零写入）
    try:
        pw = subprocess.run(
            [sys.executable, args.agentos, "watch", "--once", "--sid", "st-none"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        ps = subprocess.run(
            [sys.executable, args.agentos, "steer", "../evil", "x"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        real_steer = os.path.join(ROOT, "work", "steer")
        leaked = os.path.isdir(real_steer) and any(
            "evil" in n for n in os.listdir(real_steer)
        )
        gate(
            "D8 CLI：watch --once 出屏 rc=0；坏 sid 的 steer 拒且零写入",
            pw.returncode == 0
            and "监督面" in pw.stdout
            and ps.returncode != 0
            and "形状" in (ps.stderr or "")
            and not leaked,
            "watch=%s steer=%s leaked=%s" % (pw.returncode, ps.returncode, leaked),
        )
    except Exception as e:  # noqa: BLE001
        gate(
            "D8 CLI：watch --once 出屏 rc=0；坏 sid 的 steer 拒且零写入",
            False,
            repr(e)[:120],
        )

    # D9 零污染
    shutil.rmtree(tmp, ignore_errors=True)
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    # 零污染口径共享（tools/gate_pollution.py）：**活着的并发自检**目录不算污染
    new, _live_poll = _pollution.pollution_diff(after, before, {args.out})
    gate(
        "D9 零污染（work/ 只多 --out；真 work/steer 未被门创建）",
        not new,
        "新增=%s" % (new or "无"),
    )
    return _finish(args.out, args.core)


if __name__ == "__main__":
    sys.exit(main())
