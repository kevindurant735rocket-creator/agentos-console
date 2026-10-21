#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ctx_edit_selftest —— 上下文瘦身（fold_old_tool_results）的证伪集。

为什么要有这一层（2026-10-22 的 token 账）
------------------------------------------
长会话的成本大头不是工具面（那是固定的），而是**历史里的工具结果**：
read_file 一次 2,000 行、run_cmd 一次几百行输出，全都在**每一轮重发**。
第 20 轮的 prompt 里可能躺着 3 份同样的文件内容。

折叠（context editing 的思路）把**旧的**工具结果换成紧凑摘要。但折叠有两个
危险的失败方向：
  1. 折太狠 → 模型拿不到事实 → 开始编（假绿的危险）；
  2. 折错对象 → 把 user/assistant 原文或最近两轮也折了（上下文断裂）。

所以这个证伪集既钉「该省的省了」，也钉「不该折的没折」，并且用**变异体**
证明这些判据真的有牙（摘掉实现 → 判据必须翻红）。

裁决：CTX_EDIT_SELFTEST n/n + CTX_EDIT_SELFTEST_OK，exit 0/1
"""

import importlib.util as ilu
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + ("  -- " + str(detail)[:160] if detail else ""))


def _hist(rounds=6, size=3000, keep=2):
    """造一段像样的历史：每轮一个 assistant(tool_calls) + 一个 tool 结果。"""
    msgs = [{"role": "system", "content": "S"}]
    for r in range(rounds):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c%d" % r,
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": "c%d" % r,
                "content": json.dumps(
                    {"ok": True, "rc": 0, "out": ("行%d " % r) + "X" * size},
                    ensure_ascii=False,
                ),
            }
        )
    return msgs


def _size(msgs):
    return sum(len(m.get("content") or "") for m in msgs if isinstance(m, dict))


def main():
    import agent_session as S

    print("CTX_EDIT_SELFTEST 上下文瘦身证伪")

    # ── 1. 该省的省了 ─────────────────────────────────────────
    h = _hist()
    before = _size(h)
    folded = S.fold_old_tool_results(h)
    after = _size(folded)
    case(
        "旧工具结果被折叠：体积至少省 40%%",
        after <= before * 0.6,
        "%d → %d（省 %.0f%%）" % (before, after, (before - after) * 100.0 / max(before, 1)),
    )

    # ── 2. 不该折的没折 ───────────────────────────────────────
    case(
        "最近 keep_rounds 轮的工具结果原样保留（模型正在用的不能折）",
        all(
            "folded" not in (m.get("content") or "")
            for m in folded[-4:]
            if isinstance(m, dict) and m.get("role") == "tool"
        ),
        repr([(m.get("role"), str(m.get("content"))[:40]) for m in folded[-4:]]),
    )
    case(
        "user / assistant 原文一律不动（只折工具消息）",
        all(
            a.get("content") == b.get("content")
            for a, b in zip(h, folded)
            if isinstance(a, dict) and a.get("role") in ("user", "assistant", "system")
        ),
        "对照非工具消息",
    )
    case(
        "纯函数：不就地改传入的 messages（会话记录要完整）",
        _size(h) == before and h == _hist(),
        "原列表长度 %d" % len(h),
    )

    # ── 3. 折了要留事实（折叠≠信息消失）────────────────────────
    _st = json.loads(folded[2]["content"])
    case(
        "摘要保留事实：ok / rc / 输出首行都在",
        _st.get("folded") is True
        and "ok=True" in str(_st.get("summary"))
        and "rc=0" in str(_st.get("summary"))
        and "行0" in str(_st.get("summary")),
        repr(_st.get("summary"))[:160],
    )
    case(
        "摘要明说「需要原文请重新调用该工具」（可重取，不是消失）",
        "重新调用" in str(_st.get("hint")),
        repr(_st.get("hint")),
    )

    # ── 4. 小结果不折（折了没意义还添乱）──────────────────────
    _small = _hist(rounds=4, size=5)
    case(
        "小于阈值的工具结果不折",
        _size(S.fold_old_tool_results(_small)) == _size(_small),
        "%d vs %d" % (_size(S.fold_old_tool_results(_small)), _size(_small)),
    )

    # ── 5. 变异体自证：摘掉实现 → 判据必须翻红 ────────────────
    _mut_dir = tempfile.mkdtemp(prefix="ctxedit_mut_")
    _mut_path = os.path.join(_mut_dir, "agent_session_mut.py")
    with open(os.path.join(HERE, "agent_session.py"), encoding="utf-8") as f:
        _src = f.read()
    _anchor = (
        "    if not isinstance(messages, (list, tuple)) or not messages:\n"
        "        return list(messages or [])"
    )
    case(
        "变异体：折叠实现的锚点恰 1 处（变异体才有效）",
        _src.count(_anchor) == 1,
        "命中 %d 处" % _src.count(_anchor),
    )
    with open(_mut_path, "w", encoding="utf-8") as f:
        f.write(
            _src.replace(
                _anchor,
                "    if True:  # MUT 摘掉折叠\n        return list(messages or [])",
                1,
            )
        )
    _spec = ilu.spec_from_file_location("agent_session_mut", _mut_path)
    _mod = ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mut_folded = _mod.fold_old_tool_results(_hist())
    case(
        "变异体：摘掉折叠 → 「体积至少省 40%%」必须翻红（判据有牙）",
        _size(_mut_folded) > _size(_hist()) * 0.6,
        "变异后体积 %d（原 %d）" % (_size(_mut_folded), _size(_hist())),
    )
    # 再来一个：把「只折旧的」改成「全折」（含最近两轮）→ 判据必须翻红
    with open(_mut_path, "w", encoding="utf-8") as f:
        f.write(
            _src.replace(
                "if round_of[i] < 0 or round_of[i] >= total - keep_rounds:",
                "if False:  # MUT 连最近两轮也折",
                1,
            )
        )
    _spec2 = ilu.spec_from_file_location("agent_session_mut2", _mut_path)
    _mod2 = ilu.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)
    _mut2 = _mod2.fold_old_tool_results(_hist())
    case(
        "变异体：连最近两轮也折 → 「最近两轮保留」必须翻红（判据有牙）",
        any(
            "folded" in (m.get("content") or "")
            for m in _mut2[-4:]
            if isinstance(m, dict) and m.get("role") == "tool"
        ),
        "最近两轮被折了",
    )
    shutil.rmtree(_mut_dir, ignore_errors=True)

    bad = [c for c in CASES if not c[1]]
    print("\nCTX_EDIT_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("CTX_EDIT_SELFTEST_OK" if not bad else "CTX_EDIT_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
