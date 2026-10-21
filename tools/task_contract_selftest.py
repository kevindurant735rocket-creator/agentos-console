#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_contract_selftest —— 任务分类与契约的证伪集。

两个方向的错都要抓住：
- **判错类型**（把调研判成重构）→ 给出不该要的证据要求，比不给更坏；
- **判太宽**（什么都判成 generic）→ 契约退化成通用规则，等于没做。
所以既有「必须判成某类」的正例，也有「必须判成 generic / 不能判成某类」的负例。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import task_contract as tc  # noqa: E402

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    return bool(ok)


def kind_of(t):
    return tc.classify(t)[0]


# ── 1. 正例：必须判对类型 ─────────────────────────────────────────
for text, want in (
    ("calc.py 里的 add 函数有 bug，请修好它", tc.T_BUGFIX),
    ("这个函数算错了，修一下", tc.T_BUGFIX),
    ("报错 traceback 了，帮我看看", tc.T_BUGFIX),
    ("重构一下 tools/ 里的重复代码", tc.T_REFACTOR),
    ("给 CLI 加一个 --json 选项", tc.T_FEATURE),
    ("实现一个缓存层", tc.T_FEATURE),
    ("写一份 README 说明", tc.T_DOCS),
    ("调研一下 ruff 的规则设计", tc.T_RESEARCH),
    ("统计 tools/ 下有多少个 py 文件", tc.T_ANALYSIS),
    ("把服务部署到 8790", tc.T_OPS),
):
    got = kind_of(text)
    case(
        "分类：%s → %s" % (text[:18], want),
        got == want,
        "got=%s" % got,
    )

# ── 2. 负例：不该硬判的类型 ───────────────────────────────────────
case(
    "分类：只跑命令不算 ops（通用）",
    kind_of("把 tools/ 下的门跑一遍并汇报真实 rc") == tc.T_GENERIC,
    kind_of("把 tools/ 下的门跑一遍并汇报真实 rc"),
)
case(
    "分类：空任务字面 → generic",
    kind_of("") == tc.T_GENERIC and kind_of("   ") == tc.T_GENERIC,
    "",
)
case(
    "分类：单字「修」不判 bugfix（避免宽泛命中）",
    kind_of("改进一下代码风格") == tc.T_GENERIC,
    kind_of("改进一下代码风格"),
)
case(
    "分类：最先出现的动作主导（部署…写说明 → ops）",
    kind_of("把这个项目部署到 GitHub 并写好说明文档") == tc.T_OPS,
    kind_of("把这个项目部署到 GitHub 并写好说明文档"),
)
case(
    "分类：分类必带理由（可复查）",
    bool(tc.classify("修个 bug")[1]) and bool(tc.classify("随便看看")[1]),
    "",
)

# ── 3. 契约：按类型给不同要求 ───────────────────────────────────
_k, _r = tc.classify("修个 bug")
_c_bug = tc.contract_for(_k, {"cmdline": "pytest -q", "why": "pyproject.toml"})
case(
    "契约：bugfix 要求「改完通过核对」",
    "verify_pass" in _c_bug["hard"],
    str(_c_bug["hard"]),
)
case(
    "契约：bugfix 把「先复现」放进开工指令",
    "复现" in _c_bug["opening"] and "pytest -q" in _c_bug["opening"],
    _c_bug["opening"][:80],
)
_c_bug_nv = tc.contract_for(tc.T_BUGFIX, None)
case(
    "契约：没有验证命令时，bugfix 改口成「自己造最小复现」",
    "最小复现" in _c_bug_nv["opening"] and "pytest" not in _c_bug_nv["opening"],
    _c_bug_nv["opening"][:80],
)
_c_docs = tc.contract_for(tc.T_DOCS)
case(
    "契约：docs 要求「写下必须读回」",
    _c_docs["hard"] == ["write_and_readback"],
    str(_c_docs["hard"]),
)
_c_ops = tc.contract_for(tc.T_OPS)
case(
    "契约：ops 要求「跑过命令」（操作要留下状态证据）",
    _c_ops["hard"] == ["command_ran"],
    str(_c_ops["hard"]),
)
_c_gen_read = tc.contract_for(tc.T_GENERIC, None, "看看这个仓库结构")
case(
    "契约：纯只读/问答类 generic 不逼模型写盘",
    _c_gen_read["hard"] == [],
    str(_c_gen_read["hard"]),
)
_c_gen_build = tc.contract_for(tc.T_GENERIC, None, "写一个工具")
case(
    "契约：要产出的 generic 仍要求落盘证据（write_seen）",
    _c_gen_build["hard"] == ["write_seen"],
    str(_c_gen_build["hard"]),
)
case(
    "契约：小改动不被过度约束（write_seen 而非 write_and_readback）",
    "write_and_readback"
    not in tc.contract_for(tc.T_FEATURE, None)["hard"],
    str(tc.contract_for(tc.T_FEATURE, None)["hard"]),
)
_c_feat = tc.contract_for(tc.T_FEATURE, {"cmdline": "make test", "why": "Makefile"})
case(
    "契约：feature 有验证命令时要求 verify_pass",
    _c_feat["hard"] == ["verify_pass"] and "make test" in _c_feat["opening"],
    str(_c_feat["hard"]),
)
case(
    "契约：改代码类任务要**自审 diff**（Aider 式：改完先看改动）",
    "diff_reviewed" in tc.contract_for(tc.T_FEATURE, None)["hard"]
    and "diff_reviewed" in tc.contract_for(tc.T_REFACTOR, None)["hard"]
    and "diff_reviewed" in tc.contract_for(tc.T_BUGFIX, None)["hard"],
    repr(
        (
            tc.contract_for(tc.T_FEATURE, None)["hard"],
            tc.contract_for(tc.T_REFACTOR, None)["hard"],
            tc.contract_for(tc.T_BUGFIX, None)["hard"],
        )
    ),
)
case(
    "契约：每份契约都有 name/why（能讲清为什么）",
    all(
        tc.contract_for(k).get("name") and tc.contract_for(k).get("why")
        for k in (
            tc.T_BUGFIX,
            tc.T_FEATURE,
            tc.T_REFACTOR,
            tc.T_DOCS,
            tc.T_RESEARCH,
            tc.T_ANALYSIS,
            tc.T_OPS,
            tc.T_GENERIC,
        )
    ),
    "",
)
case(
    "契约：summary 能讲人话",
    "硬要求" in tc.contract_summary(_c_bug),
    tc.contract_summary(_c_bug),
)

# 收尾硬要求（2026-10-22）：改代码类任务在 git 仓库里必须提交
_c_feat_git = tc.contract_for(tc.T_FEATURE, None, "加个功能", git_repo=True)
_c_feat_nogit = tc.contract_for(tc.T_FEATURE, None, "加个功能", git_repo=False)
_c_read_git = tc.contract_for(tc.T_RESEARCH, None, "查一下", git_repo=True)
case(
    "契约：git 仓库里的改代码任务 → 硬要求含 committed（改了就得提交）",
    "committed" in (_c_feat_git.get("hard") or []),
    repr(_c_feat_git.get("hard")),
)
case(
    "契约：非 git 仓库 → 不要求 committed（没 .git 提交不了）",
    "committed" not in (_c_feat_nogit.get("hard") or []),
    repr(_c_feat_nogit.get("hard")),
)
case(
    "契约：只读类任务在 git 仓库里也不要求 committed（不卡死只读任务）",
    "committed" not in (_c_read_git.get("hard") or []),
    repr(_c_read_git.get("hard")),
)
case(
    "契约：提交要求写进了任务开场白（模型开工前就知道要提交）",
    "commit 工具" in str(_c_feat_git.get("opening") or ""),
    str(_c_feat_git.get("opening") or "")[-90:],
)

bad = [c for c in CASES if not c[1]]
for name, ok, detail in CASES:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))
print("\nTASK_CONTRACT_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
if bad:
    print("TASK_CONTRACT_SELFTEST_RED")
    sys.exit(1)
print("TASK_CONTRACT_SELFTEST_OK")
