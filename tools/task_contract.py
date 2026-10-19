#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_contract —— 任务类型 → 完成契约。

为什么需要它
------------
收敛引擎解决的是「**别绕路、别空口交付**」；但它认的证据是通用的
（任意一条 rc=0）。而「完成各种任务」真正难的地方在于：**不同任务类型的
「完成证据」根本不一样**：

- 缺陷修复：不先看到原 bug 复现，就无法证明你修的是那个 bug → 需要
  **红→绿**（改之前失败一次，改之后通过一次）
- 加功能：需要**新代码被测到**（跑一次测试/构建）
- 重构：需要**行为没变**（同一套测试改前改后都过）
- 写文档：需要**落盘并读回**（文件在、内容对）
- 调研/分析：需要**结论可复算**（跑过脚本/命令，或给出可核对的来源）
- 运维操作：需要**操作后状态可见**（跑一次确认命令）

一份通用证据要求（"跑过一条命令"）对上面每一种都太松或太紧。
所以这里给每种任务类型配一份**契约**：要什么证据、为什么、以及
「这类任务该怎么做」的开工指令。

设计取舍
--------
1. **分类只用任务字面 + 结构化线索，不猜**：分类结果连同**理由**一起
   记进账本，事后能查「为什么这轮被判成 bugfix」。
2. **硬要求与软要求分开**：
   - 硬要求（缺了就退回）：没有它就不算完成（比如 bugfix 改完没有通过验证）。
   - 软要求（缺了只记账 + 提前提醒）：比如红→绿复现。**不硬拦** ——
     有些 bug 本来就复现不了（需要人工步骤、依赖外部系统），硬拦会逼模型
     编造一个假复现，那比缺证据更坏。
3. **诚实报告失败仍然算交付**：契约管的是「做成了要拿什么证据」，
    不是「不许承认做不到」。
"""

import re

# ── 任务类型 ────────────────────────────────────────────────────────
T_BUGFIX = "bugfix"
T_FEATURE = "feature"
T_REFACTOR = "refactor"
T_DOCS = "docs"
T_RESEARCH = "research"
T_ANALYSIS = "analysis"
T_OPS = "ops"
T_GENERIC = "generic"

# 分类词表：**宁可判 generic，也不要判错**。判错类型的代价是给出一套
# 不该要的证据要求（比不给更坏）。所以词表用具体词，不用宽泛词。
# 「修」不单列（「修改/改进」会命中）；只认「修复/修好/修个/bug」等。
_SIGNALS = (
    (
        T_BUGFIX,
        (
            "bug",
            "bugfix",
            "缺陷",
            "缺陷修复",
            "修复",
            "修好",
            "修个",
            "修一下",
            "算错",
            "有误",
            "不对",
            "报错",
            "报错了",
            "崩溃",
            "崩了",
            "不生效",
            "失效",
            "异常",
            "回归",
            "复现",
            "debug",
            "traceback",
            "fix the",
            "fix a",
            "broken",
            "incorrect",
            "wrong result",
        ),
    ),
    (
        T_REFACTOR,
        (
            "重构",
            "refactor",
            "重写",
            "整理代码",
            "代码整洁",
            "拆分",
            "抽出",
            "解耦",
            "clean up",
            "cleanup",
            "tidy",
        ),
    ),
    (
        T_FEATURE,
        (
            "加一个",
            "加个",
            "新增",
            "实现",
            "做一个",
            "做个",
            "支持",
            "加功能",
            "feature",
            "implement",
            "add support",
            "build a",
        ),
    ),
    (
        T_DOCS,
        (
            "文档",
            "readme",
            "说明书",
            "注释",
            "docstring",
            "写一份说明",
            "写个说明",
            "documentation",
            "changelog",
        ),
    ),
    (
        T_RESEARCH,
        (
            "调研",
            "研究",
            "调查",
            "了解一下",
            "搜索",
            "research",
            "investigate",
            "find out",
            "look up",
            "search for",
        ),
    ),
    (
        T_ANALYSIS,
        (
            "分析",
            "统计",
            "盘点",
            "梳理",
            "对比",
            "analyz",
            "analyse",
            "summar",
            "compare",
            "count",
            "report on",
        ),
    ),
    (
        T_OPS,
        (
            "部署",
            "上线",
            "发布",
            "安装",
            "配置",
            "启动",
            "运行起来",
            "deploy",
            "install",
            "publish",
            "release",
            "configure",
            "run the",
        ),
    ),
)


def classify(task_text):
    """返回 (类型, 理由列表)。理由必填 —— 分类要能被人复查。"""
    t = str(task_text or "").lower()
    if not t.strip():
        return T_GENERIC, ["空任务字面"]
    hits = []
    for order, (kind, words) in enumerate(_SIGNALS):
        matched = []
        first_pos = None
        for w in words:
            i = t.find(w)
            if i < 0:
                continue
            matched.append(w)
            if first_pos is None or i < first_pos:
                first_pos = i
        if matched:
            hits.append((len(matched), first_pos, kind, matched, order))
    if not hits:
        return T_GENERIC, ["任务字面没命中任何类型特征词"]
    # 判定顺序：**最先出现的动作**主导（「把服务部署到 8790 并写好说明文档」
    # 是 ops，不是 docs）→ 命中数多的优先 → 声明顺序兜底。
    hits.sort(key=lambda h: (h[1], -h[0], h[4]))
    best = hits[0]
    reasons = ["命中特征词：%s（出现在第 %d 字处）" % ("、".join(best[3]), best[1])]
    if len(hits) > 1:
        others = ["%s(%d)" % (k, len(m)) for _c, _p, k, m, _o in hits[1:3]]
        reasons.append("次要信号：" + "、".join(others))
    return best[2], reasons
    best = hits[0]
    reasons = ["命中特征词：%s" % ("、".join(best[2]))]
    if len(hits) > 1:
        others = ["%s(%d)" % (k, len(m)) for _c, k, m in hits[1:3]]
        reasons.append("次要信号：" + "、".join(others))
    return best[1], reasons


# ── 契约 ────────────────────────────────────────────────────────────
def _cmdline(verify_info):
    if not verify_info:
        return ""
    return verify_info.get("cmdline") or ""


def contract_for(task_type, verify_info=None, task_text=""):
    """给任务类型配一份契约。

    返回 dict：
      type / name / why            —— 是什么、为什么这么要求
      opening                      —— 开工时该给模型的话（一次性）
      hard                         —— 硬要求（缺了就退回交付）
      soft                         —— 软要求（缺了只记账 + 提醒）
      verify_cmd                   —— 该跑哪条核对命令（探测不到就是空）
    """
    cmd = _cmdline(verify_info)
    c = {
        "type": task_type,
        "name": "",
        "why": "",
        "opening": "",
        "hard": [],
        "soft": [],
        "verify_cmd": cmd,
    }

    def _cited_or_read():
        return [("cited", "any_read")]

    if task_type == T_BUGFIX:
        c["name"] = "缺陷修复"
        c["why"] = "不先看到原 bug 复现，就无法证明修的是那个 bug"
        c["hard"] = (
            ["verify_pass"] if cmd else [("verify_pass", "write_and_readback")]
        )
        c["soft"] = ["repro_red_then_green"]
        if cmd:
            c["opening"] = (
                "这是**缺陷修复**任务，按这个顺序做：\n"
                "1. 先复现：跑 `%s`，看到它**失败**（这就是原 bug 的证据）；\n"
                "2. 再改：只改跟这个失败有关的地方；\n"
                "3. 再验：重跑 `%s`，看到它**通过**。\n"
                "没看到过失败就没法证明你修对了——顺序反了。" % (cmd, cmd)
            )
        else:
            c["opening"] = (
                "这是**缺陷修复**任务，按这个顺序做：\n"
                "1. 先造一个**能失败的最小复现**（一个能重现该 bug 的例子/命令），"
                "把它的失败输出留好；\n"
                "2. 再改；\n"
                "3. 再跑一遍那个复现，让它通过。\n"
                "没看到过失败就没法证明你修对了——顺序反了。"
            )

    elif task_type == T_FEATURE:
        c["name"] = "新增功能"
        c["why"] = "新代码得被真的执行到才算数"
        c["hard"] = (
            ["verify_pass"] if cmd else [("verify_pass", "write_and_readback")]
        )
        c["soft"] = []
        c["opening"] = (
            (
                "这是**新增功能**任务：写完必须跑 `%s` 证明新代码被执行到、"
                "且没弄坏别的。\n" % cmd
            )
            if cmd
            else "这是**新增功能**任务：写完要**读回**新文件确认内容对，"
            "并说明新代码怎么被走通。\n"
        )

    elif task_type == T_REFACTOR:
        c["name"] = "重构"
        c["why"] = "重构的验收标准是行为不变，不是「看起来更整齐」"
        c["hard"] = (
            ["verify_pass"] if cmd else [("verify_pass", "write_and_readback")]
        )
        c["soft"] = ["no_behavior_change"]
        c["opening"] = (
            (
                "这是**重构**任务：验收标准是**行为不变**。改之前跑一次 `%s` "
                "记下结果（这是基线），改完再跑一次，两次都必须通过。\n" % cmd
            )
            if cmd
            else "这是**重构**任务：验收标准是**行为不变**。改之前先跑一遍"
            "相关测试/命令记下基线，改完再跑一遍对比。\n"
        )

    elif task_type == T_DOCS:
        c["name"] = "文档"
        c["why"] = "文档的完成证据是「文件在、内容对、别人读得懂」"
        c["hard"] = ["write_and_readback"]
        c["soft"] = []
        c["opening"] = (
            "这是**文档**任务：写完要**读回**一遍确认落盘且内容对；"
            "命令、路径、链接要能被核对，不许写「大概是这样」。\n"
        )

    elif task_type == T_RESEARCH:
        c["name"] = "调研"
        c["why"] = "调研的完成证据是「结论可核对」——来源要能点开"
        c["hard"] = _cited_or_read()
        c["soft"] = []
        c["opening"] = (
            "这是**调研**任务：每条结论都要有**能核对的来源**（文件路径+行号，"
            "或 URL）；核对不上的别写进结论。\n"
        )

    elif task_type == T_ANALYSIS:
        c["name"] = "分析/盘点"
        c["why"] = "分析的完成证据是「结论可复算」——同样的输入要能再跑一遍"
        c["hard"] = _cited_or_read()
        c["soft"] = []
        c["opening"] = (
            "这是**分析**任务：结论必须来自**跑出来的数**（命令 + rc + 关键输出），"
            "不许凭印象报数。\n"
        )

    elif task_type == T_OPS:
        c["name"] = "操作/部署"
        c["why"] = "操作的完成证据是「操作后状态可见」"
        c["hard"] = ["command_ran"]
        c["soft"] = []
        c["opening"] = (
            "这是**操作**任务：做完后要跑一次**确认命令**把结果状态打出来"
            "（列表/健康检查/rc），别只说「做完了」。\n"
        )

    else:  # generic
        c["name"] = "通用任务"
        c["why"] = "没识别出具体类型，用通用契约（改了要有核对证据）"
        # 纯只读/问答类任务**不该**被要求「必须写盘并读回」——
        # 它的交付物就是结论本身，硬要求写盘只会逼模型为了交差而写文件。
        _imply = any(
            k in str(task_text or "").lower()
            for k in ("实现", "写", "改", "部署", "创建", "修", "新增", "生成", "加")
        )
        c["hard"] = ["verify_pass"] if cmd else (["write_and_readback"] if _imply else [])
        c["soft"] = []
        c["opening"] = (
            (
                "本次任务契约：改了东西就要跑 `%s` 核对（rc 决定真假），"
                "并用真实 rc/文件行号交付。\n" % cmd
            )
            if cmd
            else "本次任务契约：改了东西要**读回**确认，并用真实 rc/文件行号交付。\n"
        )
    return c


# 需要「真的写盘」才能满足的证据要求
_WRITE_SCOPED = ("write_and_readback", "verify_pass", "repro_red_then_green")


def relax_for_writes(contract, blocked, why=""):
    """写操作被环境锁住时，把「必须写盘」的要求撤掉。

    为什么必须撤：契约要是要求写盘证据，而环境（plan 只读 / DSP 计划
    未审批）**根本不让写**，那这个任务就永远交不了付 —— 闸门会一直退、
    一直退，直到预算耗尽。**契约必须服从环境现实**，不能反过来逼模型
    去写它写不了的东西（更不能逼它假装写过了）。
    """
    if not contract or not blocked:
        return contract
    c = dict(contract)
    kept = []
    for h in c.get("hard") or []:
        if isinstance(h, (tuple, list)):
            # any-of 元组：把「写盘类」成员摘掉；摘空了整条要求就没了
            rest = [k for k in h if k not in _WRITE_SCOPED]
            if rest:
                kept.append(tuple(rest) if len(rest) > 1 else rest[0])
            continue
        if h in _WRITE_SCOPED:
            continue
        kept.append(h)
    c["hard"] = kept
    c["writes_blocked"] = True
    c["relaxed_why"] = why or "写操作被环境锁住"
    return c


def contract_summary(contract):
    """把契约讲成人话（写进账本 / 门报告用）。"""
    if not contract:
        return ""
    return "%s（%s）：硬要求=%s 软要求=%s" % (
        contract.get("name") or contract.get("type"),
        contract.get("why") or "",
        ",".join(contract.get("hard") or []),
        ",".join(contract.get("soft") or []),
    )


if __name__ == "__main__":  # 手动看看分类
    import json
    import sys

    for t in sys.argv[1:] or (
        "calc.py 里的 add 函数有 bug，请修好它",
        "给 CLI 加一个 --json 选项",
        "重构一下 tools/ 里的重复代码",
        "写一份 README 说明",
        "调研一下 ruff 的规则设计",
        "统计 tools/ 下有多少个 py 文件",
        "把服务部署到 8790",
    ):
        kind, reasons = classify(t)
        print("%-28s -> %-10s %s" % (t[:28], kind, reasons))
