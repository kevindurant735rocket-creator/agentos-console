#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subagent_inherit_selftest —— 子代理继承父任务契约与账本视图的证伪集。

为什么要有这一层（已知缺口第 2 条）
------------------------------------
`docs/ARCHITECTURE.md` 的已知缺口写着：
  「子代理的账本与契约继承：子代理现在继承 ctx，但不继承任务契约与 todo。」

后果：父任务的契约（「这类任务怎么算完成」）与账本进度子代理**看不见**——
子代理干的活与交付闸要的证据对不上，父代理还得自己猜它做了什么。

W42 的修法（学 ruflo v3 的 Agent：parent / capabilities / assignedTo）：
  · 子代理的 ctx 带上 `parent_contract`（父任务契约）与 `parent_todo_items`
    （父账本只读视图）；
  · 子代理拿**自己的** sid（<父>-sub-<i>），账本不串扰（并行安全）；
  · system prompt 里把父契约与父账本进度注入进去（子代理开工前就看见）。

判据钉四件事：
  1. 有 parent_contract → 系统提示里有「父任务契约」段；
  2. 有 parent_todo_items → 系统提示里有「父账本（只读视图）」段，带 id:state；
  3. 没有 parent_contract → 提示里**没有**父契约段（不污染）；
  4. **变异体自证**：把注入分支摘掉 → 父契约段必须消失（判据有牙）。

裁决：SUBAGENT_INHERIT_SELFTEST n/n + ..._OK，exit 0/1
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + ("  -- " + str(detail)[:160] if detail else ""))


def _contract(name="新增功能", opening="先写测试，再写实现，最后跑核对。"):
    return {"name": name, "type": "feature", "opening": opening, "hard": ["write_seen"]}


def main():
    import agent_core as ac
    import importlib.util as ilu

    print("SUBAGENT_INHERIT_SELFTEST 子代理继承证伪")

    _root = tempfile.mkdtemp(prefix="si_")
    _pc = _contract("新增功能", "先写测试，再写实现，最后跑核对。")
    _pti = [
        {"id": 1, "text": "写测试", "state": "done"},
        {"id": 2, "text": "写实现", "state": "pending"},
    ]

    # ── 1. 有 parent_contract → 系统提示里有父契约段 ────────────
    _sp1 = ac.build_system_prompt(
        {
            "root": _root,
            "mode": "full",
            "sid": "si",
            "task_text": "帮我写一个 helper",
            "task_contract": {"name": "新增功能", "opening": "写 helper 并核对", "hard": []},
            "parent_contract": _pc,
            "parent_todo_items": _pti,
        }
    )
    case(
        "继承：parent_contract → 系统提示含「父任务契约」",
        "父任务契约" in _sp1 and "先写测试，再写实现" in _sp1,
        _sp1[_sp1.find("父任务契约"):_sp1.find("父任务契约") + 90].replace("\n", " | ")
        if "父任务契约" in _sp1
        else "无",
    )
    case(
        "继承：parent_todo_items → 系统提示含「父账本（只读视图）」带 id:state",
        "父账本（只读视图，2 项）" in _sp1 and "1:done" in _sp1 and "2:pending" in _sp1,
        _sp1[_sp1.find("父账本"):_sp1.find("父账本") + 70].replace("\n", " | ")
        if "父账本" in _sp1
        else "无",
    )

    # ── 2. 没有 parent_contract → 不污染提示 ──────────────────
    _sp2 = ac.build_system_prompt(
        {
            "root": _root,
            "mode": "full",
            "sid": "si",
            "task_text": "帮我写一个 helper",
            "task_contract": {"name": "新增功能", "opening": "写 helper 并核对", "hard": []},
        }
    )
    case(
        "不污染：没有 parent_contract → 提示里没有父契约段",
        "父任务契约" not in _sp2 and "父账本" not in _sp2,
        "父任务契约=%s 父账本=%s"
        % ("父任务契约" in _sp2, "父账本" in _sp2),
    )

    # ── 3. 变异体自证：摘掉注入分支 → 父契约段必须消失 ────────
    with open(os.path.join(HERE, "agent_core.py"), encoding="utf-8") as f:
        _src = f.read()
    _anchor = '''    _pc = ctx.get("parent_contract")
    if isinstance(_pc, dict) and _pc.get("opening"):'''
    case(
        "变异体：注入分支的锚点恰 1 处（变异体才有效）",
        _src.count(_anchor) == 1,
        "命中 %d 处" % _src.count(_anchor),
    )
    _mut_path = os.path.join(tempfile.mkdtemp(prefix="si_mut_"), "agent_core_mut.py")
    with open(_mut_path, "w", encoding="utf-8") as f:
        f.write(
            _src.replace(
                _anchor,
                "    _pc = ctx.get('parent_contract')\n"
                "    if False:  # MUT 摘掉父契约注入",
                1,
            )
        )
    _spec = ilu.spec_from_file_location("agent_core_mut_si", _mut_path)
    _mod = ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _sp_mut = _mod.build_system_prompt(
        {
            "root": _root,
            "mode": "full",
            "sid": "si",
            "task_text": "帮我写一个 helper",
            "task_contract": {"name": "新增功能", "opening": "写 helper 并核对", "hard": []},
            "parent_contract": _pc,
            "parent_todo_items": _pti,
        }
    )
    case(
        "变异体：摘掉父契约注入 → 父契约段消失（判据有牙）",
        "父任务契约" not in _sp_mut,
        "仍存在=%s" % ("父任务契约" in _sp_mut),
    )

    bad = [c for c in CASES if not c[1]]
    print("\nSUBAGENT_INHERIT_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("SUBAGENT_INHERIT_SELFTEST_OK" if not bad else "SUBAGENT_INHERIT_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
