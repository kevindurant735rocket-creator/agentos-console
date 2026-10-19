#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""replay_gate —— 真实会话回放基准的门。

自进化闭环此前只能证明「规则合入了、门是绿的」，**证明不了行为真的变好了**。
回放基准补上这一层：把真实跑过的会话语料用桩原样重放，引擎开/关做 A/B，
用**交付闸自己的判据**量「空口完成」（说完成了但全程零证据）。

门判据（5 条 + 1 个变异体）
---------------------------
1. 语料存在、≥3 条、每条带 note（可追溯到真实会话，不许造假语料）
2. 引擎开着时**零空口完成**（硬指标：事故形状必须被压住）
3. 引擎关时**必须出现空口完成**——否则语料分辨不出两侧，基准就是空门
4. 确定性：同一份语料两次回放结果完全一致
5. 变异体：把两侧都强制成「引擎关」→ 判据必须判红（证明它有牙）

离线、纯 stdlib、无模型调用、不抢端口。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import replay_bench as rb  # noqa: E402

FAILS = []
PASSES = 0


def gate(name, ok, detail=""):
    global PASSES
    if ok:
        PASSES += 1
    else:
        FAILS.append((name, str(detail)[:200]))
    print(
        "%s %s%s"
        % ("PASS" if ok else "FAIL", name, ("  -- " + str(detail)[:200]) if not ok else "")
    )


# D1 语料
corpus = rb.load_corpus()
gate(
    "语料：≥3 条且每条带 note（可追溯真实会话）",
    len(corpus) >= 3 and all(str(r.get("note") or "").strip() for r in corpus),
    "n=%d notes=%r" % (len(corpus), [bool(r.get("note")) for r in corpus]),
)
gate(
    "语料：每条都有 prompt 与 script",
    all(str(r.get("prompt") or "").strip() and r.get("script") for r in corpus),
    "n=%d" % len(corpus),
)

# D2/D3 A/B：引擎开必须更安全，引擎关必须出现空口完成
on, off, diff = rb.compare(corpus)
gate(
    "A/B：引擎开时零空口完成（事故形状被压住）",
    on["false_claims"] == 0,
    "on=%r" % (on,),
)
gate(
    "A/B：引擎关时必须出现空口完成（语料有分辨力）",
    off["false_claims"] >= 1,
    "off=%r" % (off,),
)
gate(
    "A/B：差值 = 避免的空口完成数 ≥1",
    diff["false_claims_avoided"] >= 1,
    "diff=%r" % (diff,),
)

# D4 确定性
again = rb.summarize(rb.run_all(corpus, engine_on=True))
gate("确定性：两次回放结果一致", again == on, "%r vs %r" % (on, again))

# D5 变异体：两侧都强制引擎关 → 判据必须判红
_orig_run_all = rb.run_all


def _mut_run_all(c=None, engine_on=True):
    return _orig_run_all(c, engine_on=False)


rb.run_all = _mut_run_all
try:
    _mon, _moff, _mdiff = rb.compare(corpus)
    gate(
        "变异体：两侧都关掉引擎 → 分辨力归零（门判红）",
        _mdiff["false_claims_avoided"] == 0,
        "变异体下 diff=%r" % (_mdiff,),
    )
finally:
    rb.run_all = _orig_run_all

# D6 --check 模式本身要绿（它会钉基线）
rc = rb.main(["--check"])
gate("基准 --check 模式自绿", rc == 0, "rc=%s" % rc)

print("\nREPLAY_GATE %d/%d" % (PASSES, PASSES + len(FAILS)))
if FAILS:
    print("REPLAY_GATE_RED")
    for n, d in FAILS:
        print("  - %s :: %s" % (n, d))
    sys.exit(1)
print("REPLAY_GATE_OK")
