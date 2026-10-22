#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tool_catalog —— 「工作档位」与「工具面按需披露」。

为什么要有这一层（2026-10-22 的 token 账 + 端点实测）
------------------------------------------------------
实测一：端点**不报前缀缓存** → tools 数组每轮全额付（2,720 token）。
实测二：端点**只让模型调用已声明的工具**（把 gh_search 从 tools 里去掉，
模型直接说「并没有 gh_search 这个工具」）→ **藏工具 = 藏能力**。

所以「按需披露」必须配一个**通用调度器**：没直接开的工具，用
`tool_use(name, args)` 照样能调 —— 能力不丢，schema 的 token 只在需要时付。

本模块只做三件事（纯函数，可单测）：
  1. **档位**（profile）：按任务类型/权限模式决定「这次该开哪些工具」；
  2. **暴露集合**：核心工具常开 + 情境工具按档位/已用历史开；
  3. **目录文本**：没开的工具用一行一个的紧凑目录告诉模型（它知道存在，
     需要时走 tool_use）。

设计原则：**不藏能力，只藏 schema**。模型永远能调到全部工具。
"""

import json
import os
import re

# ── 核心工具：任何档位都开（干活离不开的那一套）────────────────────
CORE_TOOLS = (
    "read_file",
    "write_file",
    "edit_file",
    "run_cmd",
    "search",
    "list_dir",
    "git_status",
    "git_diff",
    "git_log",
    "todo",
    "commit",
    "skills",
    "tool_use",
)
# 写类工具（plan 模式下不该出现在 tools 数组里：省 schema + 少一次误导）
WRITE_TOOLS = ("write_file", "edit_file", "commit")

# ── 情境工具：按档位/任务字面/已用历史开 ───────────────────────────
SITUATIONAL = {
    "gh_search": "搜 GitHub 成熟实现",
    "gh_repo_view": "看 GitHub 仓库概况",
    "gh_file_read": "读 GitHub 仓库里的文件",
    "gh_read": "跑只读 gh 命令",
    "web_fetch": "抓一个 URL 的文本",
    "health": "探回环 /health",
    "subagent": "派子代理做独立子任务",
}

# 任务字面 → 情境工具（与系统提示的纪律一致：实现类先看别人怎么做的）
_IMPL_HINT = ("实现", "新增", "加个", "加一个", "做个", "重构", "refactor",
              "feature", "实现类", "蒸馏")
# 调研词要**具体**：「看看」「搜」太泛（"看看这个文件"不是调研），
# 泛词会把普通任务误判成 research 档，白开一堆工具。
_RESEARCH_HINT = ("调研", "研究", "查一下", "对比", "research", "调查",
                  "了解一下", "有哪些成熟", "有哪些方案", "survey")
_URL_HINT = ("http://", "https://", "url", "链接", "文档", "官网", "api 文档")

PROFILES = ("implement", "research", "review", "general")


def _task_text(ctx):
    return str((ctx or {}).get("task_text") or "").lower()


def profile_for(ctx=None):
    """按任务契约类型 + 权限模式 → 档位。

    - implement：改代码类（feature/refactor/bugfix）→ 开 gh_*（系统提示
      第 5 条要求「实现类任务先看成熟实现」）
    - research：调研/分析类 → 开 gh_* + web_fetch
    - review：审查/看 diff 类 → 只读 + git，不开写类
    - general：判不出来时的通用档
    """
    ctx = ctx or {}
    _tc = ctx.get("task_contract") or {}
    _name = str(_tc.get("name") or "")
    _mode = str(ctx.get("mode") or "full")
    if _name in ("新增功能", "重构", "缺陷修复"):
        return "implement"
    if _name in ("调研", "分析", "资料查找"):
        return "research"
    _t = _task_text(ctx)
    if any(k in _t for k in _RESEARCH_HINT) and not any(
        k in _t for k in _IMPL_HINT
    ):
        return "research"
    if "审查" in _t or "review" in _t or "看看 diff" in _t:
        return "review"
    if _mode == "plan":
        return "review"
    return "general"


def situational_for(ctx=None, used=()):
    """这次该开哪些情境工具。"""
    ctx = ctx or {}
    prof = profile_for(ctx)
    _t = _task_text(ctx)
    used = set(used or ())
    out = set()
    if prof == "implement":
        # 实现类只开 **gh_search**（系统提示第 5 条只要求它）；另外两个
        # gh 工具（repo_view / file_read）用 tool_use 调，或用过 gh_search
        # 之后自动续开 —— 少开两个 = 每轮少付 ~500 字符。
        out.add("gh_search")
    if prof == "research":
        out.update(("gh_search", "gh_repo_view", "gh_file_read", "web_fetch"))
    if any(k in _t for k in _URL_HINT):
        out.add("web_fetch")
    if prof in ("implement", "research", "general"):
        # 子代理：多步/大任务才划算（单步小任务派子代理是浪费）
        if len(_t) >= 40 or prof == "research":
            out.add("subagent")
    # 已经用过的工具**永远续开**（模型知道它好用，别把它藏回去）
    out.update(t for t in used if t in SITUATIONAL)
    return out


def exposed_tools(ctx=None, used=()):
    """这次**直接开**（写进 tools 数组）的工具名集合。"""
    ctx = ctx or {}
    prof = profile_for(ctx)
    out = set(CORE_TOOLS)
    if str(ctx.get("mode") or "full") == "plan":
        out -= set(WRITE_TOOLS)          # plan 模式不摆写工具
    if prof == "review":
        out -= set(WRITE_TOOLS)          # 审查档不摆写工具
    out |= set(situational_for(ctx, used))
    return out


def hidden_tools(ctx=None, used=()):
    """没直接开、但**用 tool_use 照样能调**的工具。"""
    return sorted(set(SITUATIONAL) - exposed_tools(ctx, used))


def catalog_text(ctx=None, used=()):
    """一行一个的紧凑目录（告诉模型「这些存在，需要时用 tool_use」）。"""
    names = hidden_tools(ctx, used)
    if not names:
        return ""
    return "；".join("%s(%s)" % (n, SITUATIONAL.get(n, "")) for n in names)


def parse_args(raw):
    """tool_use 的 args：字符串或 dict → dict。解析失败如实说。"""
    if isinstance(raw, dict):
        return raw, ""
    s = str(raw or "").strip()
    if not s:
        return {}, ""
    try:
        obj = json.loads(s)
    except ValueError as e:
        return {}, "参数不是合法 JSON：%s" % str(e)[:80]
    if not isinstance(obj, dict):
        return {}, "参数必须是对象（JSON object）"
    return obj, ""
