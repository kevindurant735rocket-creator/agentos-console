#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tool_selftest —— 「工具面按需披露 + tool_use 调度器」的证伪集。

为什么要有这一层（2026-10-22 的端点实测）
------------------------------------------
实测一：端点**不报前缀缓存** → tools 数组每轮全额付（2,720 token）。
实测二：端点**只让模型调用已声明**的工具（把 gh_search 从 tools 数组里
去掉，模型直接说「并没有 gh_search 这个工具」）→ **藏工具 = 藏能力**。

所以「按需披露」必须配一个**通用调度器**（tool_use）。这个证伪集钉住
三件事，一件都不能塌：

  1. **裁得动**：核心工具常开，情境工具按档位开 → 声明的工具面确实小了；
  2. **能力不丢**：没开的工具用 tool_use 照样调得动（参数/名字错了要
     **结构化拒绝**，不许瞎执行）；
  3. **裁剪不是空转**：隐藏集合非空（裁了东西），且档位判得对。

裁决：TOOL_SELFTEST n/n + TOOL_SELFTEST_OK，exit 0/1
"""

import json
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


def _ctx(**kw):
    c = {
        "root": tempfile.mkdtemp(prefix="tool_ctx_"),
        "mode": "full",
        "sid": "ts",
        "task_text": "",
    }
    c.update(kw)
    return c


def main():
    import tool_catalog as tc
    import agent_core as ac

    print("TOOL_SELFTEST 工具面按需披露证伪")

    # ── 1. 档位判得对 ─────────────────────────────────────────
    _impl = _ctx(task_contract={"name": "新增功能"}, task_text="实现一个命令行工具")
    _res = _ctx(task_contract={"name": "调研"}, task_text="调研一下成熟的 agent 框架")
    _gen = _ctx(task_text="看看这个文件")
    case(
        "档位：实现类 → implement（开 gh_*：系统提示要求先看成熟实现）",
        tc.profile_for(_impl) == "implement"
        and "gh_search" in tc.exposed_tools(_impl),
        "profile=%s" % tc.profile_for(_impl),
    )
    case(
        "档位：调研类 → research（开 gh_* + web_fetch）",
        tc.profile_for(_res) == "research"
        and "web_fetch" in tc.exposed_tools(_res)
        and "gh_search" in tc.exposed_tools(_res),
        "profile=%s" % tc.profile_for(_res),
    )
    case(
        "档位：判不出来 → general（不硬套）",
        tc.profile_for(_gen) == "general",
        "profile=%s" % tc.profile_for(_gen),
    )
    case(
        "plan 模式 → 不摆写类工具（省 schema + 少一次误导）",
        not (set(tc.WRITE_TOOLS) & set(tc.exposed_tools(_ctx(mode="plan")))),
        repr(sorted(tc.exposed_tools(_ctx(mode="plan")))),
    )

    # ── 2. 裁得动（工具面真的小了）＋ 裁剪不是空转 ────────────
    _all = set(ac.TOOLS)
    _exp = set(tc.exposed_tools(_impl))
    _chars_all = len(json.dumps(ac.tool_defs(), ensure_ascii=False))
    _chars_exp = len(json.dumps(ac.tool_defs(names=sorted(_exp)), ensure_ascii=False))
    case(
        "裁得动：声明的工具面比全量小 ≥15%%（%d → %d 字符）" % (_chars_all, _chars_exp),
        _chars_exp <= _chars_all * 0.85,
        "%d → %d" % (_chars_all, _chars_exp),
    )
    case(
        "裁剪不是空转：隐藏集合非空（真裁掉了东西）",
        len(tc.hidden_tools(_impl)) > 0,
        repr(tc.hidden_tools(_impl)),
    )
    case(
        "核心工具永远在（read_file/write_file/run_cmd/todo/commit/tool_use）",
        all(
            t in _exp
            for t in ("read_file", "write_file", "run_cmd", "todo", "commit", "tool_use")
        ),
        repr(sorted(_exp)),
    )
    case(
        "用过的工具**续开**（模型用过就别藏回去）",
        "web_fetch" in tc.exposed_tools(_impl, ("web_fetch",)),
        repr(tc.hidden_tools(_impl, ("web_fetch",))),
    )

    # ── 3. 能力不丢：tool_use 调得动没开的工具 ────────────────
    _root = tempfile.mkdtemp(prefix="tool_use_")
    with open(os.path.join(_root, "u.txt"), "w", encoding="utf-8") as f:
        f.write("hi\n")
    _c = {"root": _root, "mode": "full", "sid": "tu"}
    r = ac._t_tool_use(
        {"name": "list_dir", "args": json.dumps({"path": "."})}, _c
    )
    case(
        "tool_use：能调到没直接开的工具（能力不丢）",
        r.get("ok") is True and "u.txt" in json.dumps(r, ensure_ascii=False),
        repr(r)[:140],
    )
    r2 = ac._t_tool_use({"name": "没有这个工具", "args": "{}"}, _c)
    case(
        "tool_use：工具名不存在 → 结构化拒绝（不瞎执行）",
        r2.get("ok") is False and "没有这个工具" in str(r2.get("error") or ""),
        repr(r2.get("error"))[:120],
    )
    r3 = ac._t_tool_use({"name": "list_dir", "args": "{不是 JSON"}, _c)
    case(
        "tool_use：参数不是合法 JSON → 结构化拒绝",
        r3.get("ok") is False and "JSON" in str(r3.get("error") or ""),
        repr(r3.get("error"))[:120],
    )
    r4 = ac._t_tool_use({"name": "list_dir", "args": "[1,2]"}, _c)
    case(
        "tool_use：参数不是对象 → 结构化拒绝",
        r4.get("ok") is False and "对象" in str(r4.get("error") or ""),
        repr(r4.get("error"))[:120],
    )
    # 权限闸不许被 tool_use 绕过：越界路径照样被拒
    r5 = ac._t_tool_use(
        {"name": "read_file", "args": json.dumps({"path": "../../etc/passwd"})}, _c
    )
    case(
        "tool_use 不绕过权限闸（越界路径照样拒）",
        r5.get("ok") is False,
        repr(r5.get("error"))[:120],
    )

    # ── 4. 目录文本：模型得知道那些工具存在 ───────────────────
    _cat = tc.catalog_text(_impl, ())
    case(
        "目录文本：没开的工具一行一个告诉模型（它要知道存在）",
        bool(_cat) and "tool_use" not in _cat,
        _cat[:120],
    )

    bad = [c for c in CASES if not c[1]]
    print("\nTOOL_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("TOOL_SELFTEST_OK" if not bad else "TOOL_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
