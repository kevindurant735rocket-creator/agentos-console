#!/usr/bin/env python3
"""hello CLI —— study8 蒸馏后的最小终端 agent 骨架入口。

参考 gptme/gptme 的两条设计点：
1) provider 无关：模型/提供方由环境变量注入，主循环只依赖统一接口；
2) 能力=工具：工具注册表 + 分发，CLI 本身保持极薄。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable

DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "ollama/llama3.1")
DEFAULT_PROVIDER = os.environ.get("AGENT_PROVIDER", "ollama")

# 工具注册表：name -> 处理器（设计点 2）
TOOLS: dict[str, Callable[[str], str]] = {}


def tool(name: str) -> Callable[[Callable[[str], str]], Callable[[str], str]]:
    def deco(fn: Callable[[str], str]) -> Callable[[str], str]:
        TOOLS[name] = fn
        return fn

    return deco


@tool("echo")
def _echo(text: str) -> str:
    return text


@tool("upper")
def _upper(text: str) -> str:
    return text.upper()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli8", description="hello CLI (gptme-inspired)")
    p.add_argument("prompt", nargs="?", default="hello", help="要处理的文本，默认 hello")
    p.add_argument("-t", "--tool", default="echo", choices=sorted(TOOLS), help="用哪个工具")
    p.add_argument("--model", default=DEFAULT_MODEL, help="模型标识（可换 provider）")
    p.add_argument("--provider", default=DEFAULT_PROVIDER, help="provider 名")
    p.add_argument("-V", "--version", action="version", version="cli8 0.1.0")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 设计点 1：provider/模型只影响配置，不影响主循环
    sys.stderr.write(f"[cli8] provider={args.provider} model={args.model}\n")
    try:
        out = TOOLS[args.tool](args.prompt)
    except Exception as exc:  # 明确的失败退出码，便于脚本化
        sys.stderr.write(f"[cli8] error: {exc}\n")
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
