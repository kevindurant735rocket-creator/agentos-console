#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_cmd —— 探测「这个仓库跑什么命令能证明改动是对的」。

来源（**本地蒸馏**，不向 GitHub 上传任何自有内容）
------------------------------------------------
- **aider**（★30k+）：改完文件自动跑 lint/test，失败就把错误喂回模型。
  它的关键洞察是——**验证要贴着动作发生**，不是等到任务末尾才验一次：
  连改十个文件、只验最后一个，前九个坏了也不知道。
- **SWE-agent**（★25k+）：ACI（Agent-Computer Interface）把工具失败转成
  「可行动的观察」而不是原始报错，让模型知道下一步该做什么。

本模块只做前者的**前置**：找出该跑哪条命令；真正的执行仍由 agent 通过
run_cmd 走白名单与留痕（内核不替模型跑命令——那会绕过审计）。

判据刻意保守：探测不到就返回 None（不猜）。猜错命令会让 agent 跑一条
不存在的测试，比不提示更坏 —— 它会把「跑失败」误当成「改动坏了」。
"""

import json
import os

# 探测顺序：从「最明确的声明」到「最通用的约定」。
# 每项都要求仓库里**真的有那个文件**，否则不猜。
_RULES = (
    ("make", ("Makefile", "makefile", "GNUmakefile"), "make test", "Makefile 存在"),
    ("npm", ("package.json",), "npm test", "package.json 存在"),
    ("cargo", ("Cargo.toml",), "cargo test", "Cargo.toml 存在"),
    ("go", ("go.mod",), "go test ./...", "go.mod 存在"),
    ("pytest", ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"), "pytest -q",
     "Python 项目配置文件存在"),
)


def _has_make_test(root):
    """Makefile 里真的有 test 目标才建议 make test。"""
    for name in ("Makefile", "makefile", "GNUmakefile"):
        p = os.path.join(root, name)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if ln.startswith("test:") or ln.startswith("test :"):
                        return True
        except OSError:
            continue
    return False


def _has_npm_test(root):
    p = os.path.join(root, "package.json")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    return bool((d.get("scripts") or {}).get("test"))


def detect_verify_cmd(root):
    """返回 {"cmd": [...], "why": "..."} 或 None（探测不到就 honest 地说没有）。

    为什么不猜「pytest -q」作为兜底：仓库里没有 Python 项目时猜它，
    agent 会去跑一条必然失败的命令，而 rc≠0 会被读成「改动坏了」——
    把「没找到验证方式」伪装成「验证失败」，是最坏的一种误导。
    """
    if not root:
        return None
    root = os.path.realpath(str(root))
    for kind, names, cmd, why in _RULES:
        for name in names:
            if not os.path.exists(os.path.join(root, name)):
                continue
            if kind == "make" and not _has_make_test(root):
                continue
            if kind == "npm" and not _has_npm_test(root):
                continue
            return {
                "cmd": cmd.split(),
                "cmdline": cmd,
                "why": "%s（%s）" % (why, name),
                "kind": kind,
            }
    return None


def describe(info):
    """把探测结果说成人话（注入给模型时用）。"""
    if not info:
        return ""
    return "%s（依据：%s）" % (info.get("cmdline") or "", info.get("why") or "")


if __name__ == "__main__":  # 手动看一眼当前仓库
    here = os.path.dirname(os.path.abspath(__file__))
    print(json.dumps(detect_verify_cmd(os.path.dirname(here)), ensure_ascii=False))
