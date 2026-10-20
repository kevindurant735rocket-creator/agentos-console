#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rewind_gate —— W1 上下文门：compact v2 真做摘要 + /rewind 重放对齐且有界。

为什么必须有这道门（W1 波首件交付）：
  compact v2 与 /rewind 都能「看起来在工作」：摘要字段可以恒空、rewind 可以
  错位一轮、越界可以默默截断 —— 屏上都有话说。本门站在外面认这些事实，纯离线
  不碰真模型（摘要路径用回环 HTTP 桩真跑）：
    D1  agent_core 可 import + 七符号齐（v2/replay/DIGEST 前缀/补全表/run_agent/Stub）
    D2  `--check` 末行逐字 `AGENT_CORE OK 162/162`（数漂移即红）
    D3  源码锚点六元：v2/replay 各恰 1；v2 函数体内 provider.chat 恰 1、回退
        调用恰 1；/rewind 分支恰 1；补全表含 /rewind
    D4  重放对齐（行为）：3 轮夹具 n=1/2/3 → 2/4/6 条；n=0/4 越界 fail-closed
    D5  digest 真调用（行为）：桩返回摘要 → digest_used=True、前缀在头、丢 25
    D6  回退（行为）：桩 500 → 与 _compact_history 输出逐字相等（且不抛）
    D7  聚焦（行为）：focus 词真进摘要请求体
    D8  自动压缩集成（行为）：run_agent 真跑 22 步工具桩 → 收尾后 history
        头带摘要（v2 已接入主循环，不是只有 /compact 走新路）
    D9  /rewind 端到端（行为）：重放 + note 落盘 + 原记录保留 + 越界不动
    D10 零污染：work/ 只多 --out 一个文件；夹具全在系统临时目录并清理

用法: python3 tools/rewind_gate.py [--core PATH] [--out PATH]
裁决：REWIND_GATE OK n/n（rc=0）/ REWIND_GATE RED（rc=2，有漏网）
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
DEFAULT_OUT = os.path.join(ROOT, "work", "rewind_gate.json")
EXPECT_CHECK = "AGENT_CORE OK 162/162"

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


def _toolcall(path, cid):
    return {
        "json": {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": cid,
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": path}),
                                },
                            }
                        ],
                    },
                }
            ]
        }
    }


def _sec(txt, start_mark, end_mark):
    i = txt.find(start_mark)
    j = txt.find(end_mark, i + 1) if i >= 0 else -1
    return txt[i:j] if i >= 0 and j > i else ""


def _finish(out_path, ap):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(
            {
                "core": os.path.relpath(ap, ROOT) if ap else "?",
                "judgments": results,
                "n_ok": n_ok,
                "n_total": len(results),
            },
            fh,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    print("REWIND_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("REWIND_GATE_OK")
        return 0
    print("REWIND_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    core_path, out_path = args.core, args.out
    print(
        "== rewind_gate：compact v2 + /rewind（core=%s）"
        % os.path.relpath(core_path, ROOT)
    )

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        src = open(core_path, encoding="utf-8").read()
    except OSError as e:
        print("REWIND_GATE_RED 读不到 core：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="rwgate_")

    # D1 可 import + 符号齐
    K = None
    try:
        K = load_core(core_path)
        need = (
            "_compact_history_v2",
            "_rewind_replay",
            "_history_from_transcript",
            "DIGEST_PREFIX",
            "REPL_SLASH_CMDS",
            "run_agent",
            "Stub",
        )
        missing = [n for n in need if not hasattr(K, n)]
        gate("D1 可 import + 七符号齐", not missing, "缺=%s" % (missing or "无"))
    except Exception as e:  # noqa: BLE001
        gate("D1 可 import + 七符号齐", False, repr(e)[:120])
    if K is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return _finish(out_path, core_path)

    # D2 --check 逐字令牌
    try:
        p = subprocess.run(
            [sys.executable, core_path, "--check"],
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

    # D3 源码锚点六元（防漂：结构断言，不钉行为真源）
    v2_src = _sec(src, "def _compact_history_v2", "def _rewind_replay")
    n_v2 = src.count("def _compact_history_v2")
    n_rp = src.count("def _rewind_replay")
    n_chat = v2_src.count("provider.chat(")
    n_fb = v2_src.count("_compact_history(history, keep)")
    n_rw = src.count('if cmd == "/rewind":')
    import re as _re

    n_tbl = len(_re.findall(r'"/sessions",\s*"/rewind",', src))
    gate(
        "D3 源码锚点（v2/replay/chat/回退/分支/补全表 各恰 1）",
        (n_v2, n_rp, n_chat, n_fb, n_rw, n_tbl) == (1, 1, 1, 1, 1, 1),
        "v2=%d rp=%d chat=%d fb=%d rw=%d tbl=%d"
        % (n_v2, n_rp, n_chat, n_fb, n_rw, n_tbl),
    )

    # 3 轮夹具（u1/a1 .. u3/a3）
    recs = []
    for i in range(1, 4):
        recs.append({"kind": "user", "text": "u%d" % i})
        recs.append({"kind": "assistant", "text": "a%d" % i})

    # D4 重放对齐 + 越界
    ok4, det4 = True, []
    try:
        for n, want_len in ((1, 2), (2, 4), (3, 6)):
            msgs, err, turns = K._rewind_replay(recs, n)
            if err or len(msgs or []) != want_len or turns != 3:
                ok4 = False
                det4.append("n=%d len=%s err=%r" % (n, len(msgs or []), err))
        m0, e0, _ = K._rewind_replay(recs, 0)
        m4, e4, _ = K._rewind_replay(recs, 4)
        if m0 is not None or not e0 or m4 is not None or not e4:
            ok4 = False
            det4.append("越界未拒 m0=%s m4=%s" % (m0 is None, m4 is None))
    except Exception as e:  # noqa: BLE001
        ok4, det4 = False, [repr(e)[:100]]
    gate(
        "D4 重放对齐 + 越界 fail-closed（行为）",
        ok4,
        "; ".join(det4)[:150] or "n=1/2/3→2/4/6 条、n=0/4 拒",
    )

    hist = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "m%d" % i} for i in range(30)
    ]

    # D5 digest 真调用（回环桩）
    try:
        stub = K.Stub([_chat("[摘要] 关键路径 tools/dsp.py、rc=0")])
        out5, d5, used5 = K._compact_history_v2(hist, keep=5, provider=stub.provider())
        stub.close()
        gate(
            "D5 digest 真调用（桩）→ 摘要进头、丢弃数 25",
            used5
            and d5 == 25
            and len(out5) == 7
            and out5[1]["content"].startswith(K.DIGEST_PREFIX),
            "used=%s dropped=%s len=%d" % (used5, d5, len(out5)),
        )
    except Exception as e:  # noqa: BLE001
        gate("D5 digest 真调用（桩）→ 摘要进头、丢弃数 25", False, repr(e)[:120])

    # D6 回退逐字等价
    try:
        bad = K.Stub([{"status": 500, "json": {"error": "boom"}}])
        out6, d6, used6 = K._compact_history_v2(hist, keep=5, provider=bad.provider())
        bad.close()
        old6, dold = K._compact_history(hist, keep=5)
        gate(
            "D6 provider 失败 → 与旧口径逐字相等（行为）",
            out6 == old6 and d6 == dold and not used6,
            "used=%s same=%s" % (used6, out6 == old6),
        )
    except Exception as e:  # noqa: BLE001
        gate("D6 provider 失败 → 与旧口径逐字相等（行为）", False, repr(e)[:120])

    # D7 聚焦透传
    try:
        stub2 = K.Stub([_chat("DIG")])
        K._compact_history_v2(
            hist, keep=5, provider=stub2.provider(), focus="只留部署相关"
        )
        n_seen = len(stub2.seen)
        seen7 = json.dumps(stub2.seen[-1], ensure_ascii=False)
        stub2.close()
        gate("D7 聚焦词真进摘要请求体", "只留部署相关" in seen7, "seen_n=%d" % n_seen)
    except Exception as e:  # noqa: BLE001
        gate("D7 聚焦词真进摘要请求体", False, repr(e)[:120])

    # D8 自动压缩集成（run_agent 真跑 22 步工具桩）
    try:
        # W11.1 夹具补齐：读**真存在**的文件 —— 否则新守卫②（同工具连续
        # 失败 ≥6 停）第 6 步就叫停，测不到「长大到触发自动压缩」的本意。
        for _i in range(22):
            with open(
                os.path.join(tmp, "note%d.txt" % _i), "w", encoding="utf-8"
            ) as _f:
                _f.write("x")
        steps = [_toolcall("note%d.txt" % i, "c%d" % i) for i in range(22)]
        rstub = K.Stub(steps + [_chat("收尾")])
        rctx = {"root": tmp, "mode": "accept-edits", "sid": "rwgate-%d" % os.getpid()}
        res = K.run_agent(
            "长对话触发自动压缩", ctx=rctx, provider=rstub.provider(), max_steps=40
        )
        rstub.close()
        h = rctx.get("history") or []
        head1 = str(h[1].get("content", "")) if len(h) > 1 else ""
        gate(
            "D8 run_agent 自动压缩走 v2 且摘要进 history（行为）",
            bool(res.get("ok"))
            and len(h) >= 42
            and h[0].get("role") == "system"
            and head1.startswith(K.DIGEST_PREFIX),
            "ok=%s hist=%d head=%r" % (res.get("ok"), len(h), head1[:24]),
        )
    except Exception as e:  # noqa: BLE001
        gate(
            "D8 run_agent 自动压缩走 v2 且摘要进 history（行为）", False, repr(e)[:120]
        )

    # D9 /rewind 端到端
    try:
        rwp = os.path.join(tmp, "s.jsonl")
        for i in range(1, 4):
            K._transcript_append(rwp, {"kind": "user", "text": "u%d" % i})
            K._transcript_append(rwp, {"kind": "assistant", "text": "a%d" % i})
        cctx = {
            "root": tmp,
            "mode": "accept-edits",
            "history": [{"role": "user", "content": "stale"}],
        }
        K._repl_slash("/rewind 2", cctx, None, "sid", rwp)
        rr = K._transcript_load(rwp)
        ok9 = (
            cctx["history"]
            == [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
            ]
            and len(rr) == 7
            and rr[-1].get("kind") == "note"
            and rr[0].get("text") == "u1"
        )
        keep9 = cctx["history"]
        K._repl_slash("/rewind 0", cctx, None, "sid", rwp)
        K._repl_slash("/rewind 9", cctx, None, "sid", rwp)
        ok9 = ok9 and cctx["history"] is keep9 and len(K._transcript_load(rwp)) == 7
        gate(
            "D9 /rewind 端到端：重放+note+越界不动（行为）",
            ok9,
            "hist=%d recs=%d" % (len(keep9), len(rr)),
        )
    except Exception as e:  # noqa: BLE001
        gate("D9 /rewind 端到端：重放+note+越界不动（行为）", False, repr(e)[:120])

    # D10 零污染（夹具已清；work/ 只许新增 --out）
    shutil.rmtree(tmp, ignore_errors=True)
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    # 零污染口径共享（tools/gate_pollution.py）：**活着的并发自检**目录不算污染
    new, _live_poll = _pollution.pollution_diff(after, before, {out_path})
    gate("D10 零污染（work/ 只多 --out；夹具已清）", not new, "新增=%s" % (new or "无"))
    return _finish(out_path, core_path)


if __name__ == "__main__":
    sys.exit(main())
