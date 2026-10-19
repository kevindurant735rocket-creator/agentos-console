#!/usr/bin/env python3
"""agentos-study-cli3 — 最小终端 agent CLI 骨架。

derived from gptme/gptme (MIT) 的三条设计点：
  1. 工具注册表 + entry-point 自动发现（声明式、可启停）
  2. 单一 core，多前端共享；非交互运行有明确 exit taxonomy
  3. extras 分层 + 沙箱档位显式声明（默认非沙箱）

刻意零第三方依赖，只用标准库，方便任意 headless / ARM 环境跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------
# 借鉴点 2：退出码分类（gptme 的 non-interactive exit taxonomy）
# --------------------------------------------------------------------------


class ExitCode(IntEnum):
    OK = 0
    USAGE = 2
    MODEL_ERR = 3
    TOOL_ERR = 4
    BUDGET = 5
    INTERRUPT = 130


# --------------------------------------------------------------------------
# 借鉴点 1：工具以「类 + schema 声明」注册，而不是散落的 if-else
#           第三方工具通过 entry-point group "agentos3.tools" 注入
# --------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str          # 给模型看的能力说明
    parameters: Dict[str, Any]  # JSON-Schema 风格参数声明
    risk: str                 # 借鉴点 3：none|read|write|exec —— 能力风险档位
    func: Callable[..., str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
        }


class ToolRegistry:
    """全局工具注册表；gptme 对应 `gptme/tools/*` + plugins entry-points。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, func: Callable[..., str], *, name: str = "", description: str = "",
                 parameters: Optional[Dict[str, Any]] = None, risk: str = "read") -> Tool:
        tool = Tool(
            name=name or func.__name__,
            description=description or (func.__doc__ or "").strip().split("\n")[0],
            parameters=parameters or {"type": "object", "properties": {}, "required": []},
            risk=risk,
            func=func,
        )
        self._tools[tool.name] = tool
        return tool

    def tool(self, **kwargs: Any) -> Callable[[Callable[..., str]], Callable[..., str]]:
        def deco(func: Callable[..., str]) -> Callable[..., str]:
            self.register(func, **kwargs)
            return func
        return deco

    def discover(self) -> List[str]:
        """从 entry-points 发现第三方工具（缺失时静默返回空，保证 minimal 可跑）。"""
        found: List[str] = []
        try:
            from importlib.metadata import entry_points
        except ImportError:  # pragma: no cover
            return found
        try:
            eps = entry_points(group="agentos3.tools")
        except TypeError:  # Python 3.9 老签名
            eps = entry_points().get("agentos3.tools", [])  # type: ignore[attr-defined]
        for ep in eps:
            try:
                loaded = ep.load()
                if isinstance(loaded, Tool):
                    self._tools[loaded.name] = loaded
                elif callable(loaded):
                    self.register(loaded, name=ep.name)
                found.append(ep.name)
            except Exception as exc:  # 插件的锅不能炸掉主程序
                print(f"[warn] 插件 {ep.name} 加载失败: {exc}", file=sys.stderr)
        return found

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def names(self) -> List[str]:
        return [t.name for t in self.all()]


# --------------------------------------------------------------------------
# 内置工具：对应 AgentOS 的 read_file / run_cmd / search
# --------------------------------------------------------------------------

REGISTRY = ToolRegistry()


@REGISTRY.tool(
    name="read_file",
    description="读取一个本地文本文件的内容",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "仓库内相对路径"}},
        "required": ["path"],
    },
    risk="read",
)
def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()[:4000]


@REGISTRY.tool(
    name="echo_tool",
    description="把输入原样回显（用于验证调用链的最小工具）",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    risk="none",
)
def echo_tool(text: str) -> str:
    return text


# --------------------------------------------------------------------------
# 借鉴点 3：沙箱档位（默认 none，README/横幅显式声明风险）
# --------------------------------------------------------------------------


@dataclass
class Sandbox:
    mode: str = "none"   # none | docker | wasm
    declared_only: bool = True  # 骨架阶段只声明，不真正隔离

    def banner(self) -> str:
        if self.mode == "none":
            return "sandbox=none  ⚠ 未沙箱：exec 类工具在本机直接执行，仅本地学习用途"
        return f"sandbox={self.mode}（骨架阶段仅声明，未实现真实隔离）"


# --------------------------------------------------------------------------
# 借鉴点 2：单一 core —— REPL 与非交互 run 共用同一执行路径
# --------------------------------------------------------------------------


@dataclass
class Result:
    code: ExitCode
    output: str
    steps: List[Dict[str, Any]] = field(default_factory=list)


class DemoPlanner:
    """替身 planner：不接模型，按关键词选工具，只为演示注册表驱动调度。"""

    def plan(self, prompt: str, registry: ToolRegistry) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        if "读" in prompt or "read" in prompt.lower():
            calls.append({"tool": "read_file", "args": {"path": "README.md"}})
        calls.append({"tool": "echo_tool", "args": {"text": prompt}})
        known = set(registry.names())
        return [c for c in calls if c["tool"] in known]


def run_once(prompt: str, registry: ToolRegistry, sandbox: Sandbox,
             max_steps: int = 4) -> Result:
    """单一执行路径：CLI run / REPL / stdin 都走这里。"""
    steps: List[Dict[str, Any]] = []
    planner = DemoPlanner()
    plan = planner.plan(prompt, registry)
    if len(plan) > max_steps:
        plan = plan[:max_steps]
    out: List[str] = []
    for step in plan:
        tool = registry.get(step["tool"])
        if tool is None:
            return Result(ExitCode.TOOL_ERR, f"未知工具 {step['tool']}", steps)
        steps.append({"tool": tool.name, "risk": tool.risk, "args": step["args"]})
        if tool.risk == "exec" and sandbox.mode == "none":
            print(f"[warn] 工具 {tool.name} 属 exec 档且 sandbox=none", file=sys.stderr)
        try:
            out.append(f"[{tool.name}] " + str(tool.func(**step["args"])))
        except Exception as exc:
            return Result(ExitCode.TOOL_ERR, f"工具 {tool.name} 执行失败: {exc}", steps)
    if not out:
        return Result(ExitCode.MODEL_ERR, "planner 未产出任何可执行步骤", steps)
    return Result(ExitCode.OK, "\n".join(out), steps)


# --------------------------------------------------------------------------
# CLI 前端（薄壳：只做解析与退出码映射）
# --------------------------------------------------------------------------


def cmd_tools(args: argparse.Namespace) -> int:
    registry = ToolRegistry()
    # 复用内置工具定义，再补发现
    for tool in REGISTRY.all():
        registry._tools[tool.name] = tool  # noqa: SLF001 - 骨架内直接复用
    added = registry.discover()
    if args.json:
        print(json.dumps([t.to_dict() for t in registry.all()], ensure_ascii=False, indent=2))
    else:
        print(f"已注册工具 {len(registry.all())} 个（插件发现 {len(added)} 个）:")
        for t in registry.all():
            print(f"  - {t.name:<12} risk={t.risk:<5} {t.description}")
    return int(ExitCode.OK)


def cmd_run(args: argparse.Namespace) -> int:
    prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    sandbox = Sandbox(mode=args.sandbox)
    print(sandbox.banner(), file=sys.stderr)
    result = run_once(prompt, REGISTRY, sandbox)
    print(result.output)
    if args.json:
        print(json.dumps({"code": int(result.code), "steps": result.steps}, ensure_ascii=False))
    return int(result.code)


def cmd_chat(args: argparse.Namespace) -> int:
    sandbox = Sandbox(mode=args.sandbox)
    print(f"agentos-study-cli3 REPL · {sandbox.banner()}")
    print("输入内容回车执行，Ctrl-D / exit 退出")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return int(ExitCode.INTERRUPT if sys.exc_info()[0] else ExitCode.OK)
        if line.strip() in {"exit", "quit"}:
            return int(ExitCode.OK)
        if not line.strip():
            continue
        result = run_once(line, REGISTRY, sandbox)
        print(result.output)
        if result.code != ExitCode.OK:
            print(f"[exit {int(result.code)}]", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentos3",
        description="最小终端 agent CLI 骨架（distilled from gptme/gptme, MIT）",
    )
    p.add_argument("--sandbox", choices=["none", "docker", "wasm"], default="none",
                   help="执行风险档位（骨架阶段仅声明，默认 none=未沙箱）")
    sub = p.add_subparsers(dest="cmd")

    p_tools = sub.add_parser("tools", help="列出工具注册表（含风险档位）")
    p_tools.add_argument("--json", action="store_true")
    p_tools.set_defaults(func=cmd_tools)

    p_run = sub.add_parser("run", help="非交互执行一条指令，返回分类退出码")
    p_run.add_argument("prompt", help="指令文本，'-' 表示从 stdin 读")
    p_run.add_argument("--json", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="交互 REPL（与 run 共用同一 core）")
    p_chat.set_defaults(func=cmd_chat)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return int(ExitCode.USAGE)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return int(ExitCode.INTERRUPT)


if __name__ == "__main__":
    raise SystemExit(main())
