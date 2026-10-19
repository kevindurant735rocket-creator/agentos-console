#!/usr/bin/env python3
"""cli7 — 最小终端 agent CLI 骨架。

蒸馏自 vstorm-co/pydantic-deepagents (★1103, Python, MIT) 的两条设计点：
  1. Live Run Forking：CoW overlay 隔离 + 测试真值加权评分选胜者
  2. 单 harness 双入口：同一 loop 既做 CLI 也做库函数；能力靠开关注入工具面
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field


# ---------- 设计点 2：能力开关 -> 工具面 ----------

@dataclass
class Capabilities:
    """能力以参数开关注入，而不是继承。schema 不变，工具面随配置变化。"""
    filesystem: bool = True
    shell: bool = False
    memory: bool = False
    forking: bool = False

    def tools(self) -> list[str]:
        name = {
            "filesystem": ["read_file", "write_file", "list_dir"],
            "shell": ["run_shell"],
            "memory": ["remember", "recall"],
            "forking": ["fork_run", "inspect_branches", "merge_or_select", "diff_branches"],
        }
        out: list[str] = []
        for key, names in name.items():
            if getattr(self, key):
                out.extend(names)
        return out


# ---------- 设计点 1：Live Run Forking ----------

@dataclass
class Branch:
    label: str
    steering: str
    budget_usd: float
    overlay: dict[str, str] = field(default_factory=dict)  # CoW 写入层
    tests_passed: int = 0
    tests_total: int = 0
    quality_spread: float = 0.0
    internal_consistency: float = 0.0

    def write(self, path: str, data: str, parent: dict[str, str]) -> None:
        """写只落本地 overlay；读时穿透到 parent。"""
        self.overlay[path] = data

    def read(self, path: str, parent: dict[str, str]) -> str | None:
        return self.overlay.get(path, parent.get(path))

    def test_pass_ratio(self) -> float:
        if self.tests_total == 0:
            return 0.0
        return self.tests_passed / self.tests_total

    def confidence(self) -> float:
        # 与上游一致：quality*0.4 + tests*0.4 + consistency*0.2
        return (
            self.quality_spread * 0.4
            + self.test_pass_ratio() * 0.4
            + self.internal_consistency * 0.2
        )


def fork_run(branches: list[Branch], parent_fs: dict[str, str],
             mode: str = "auto_with_fallback") -> Branch:
    """按 confidence 选胜者。manual 模式下退化为取第一个（真实实现里是人工 picker）。"""
    for b in branches:
        if b.budget_usd <= 0:
            raise ValueError(f"branch {b.label} 无预算，拒绝启动")
    if mode == "manual":
        return branches[0]
    if mode == "vote":
        return max(branches, key=lambda b: b.internal_consistency)
    # auto / auto_with_fallback
    return max(branches, key=lambda b: b.confidence())


# ---------- 设计点 2 的另一半：同一 harness 的库入口 ----------

def create_agent(model: str = "stub", **caps):
    """一行调用给出一个 agent；CLI 也只是它的第一个用户。"""
    capabilities = Capabilities(**caps)

    def run(prompt: str) -> dict:
        return {
            "model": model,
            "prompt": prompt,
            "tools": capabilities.tools(),
            "harness": "cli7",
        }

    return run


def _demo() -> int:
    parent_fs = {"app.py": "def handler(): ...\n"}

    branches = [
        Branch("A", "用装饰器重构", budget_usd=0.30,
               quality_spread=0.71, tests_passed=8, tests_total=8,
               internal_consistency=0.90),
        Branch("B", "用上下文管理器重构", budget_usd=0.30,
               quality_spread=0.42, tests_passed=6, tests_total=8,
               internal_consistency=0.70),
        Branch("C", "抽基类", budget_usd=0.30,
               quality_spread=0.55, tests_passed=8, tests_total=8,
               internal_consistency=0.80),
    ]
    # CoW：读穿透到 parent
    print("== 读穿透 parent ==")
    print("  A.read('app.py') ->", repr(branches[0].read("app.py", parent_fs)))
    # 写只留本地
    branches[0].write("app.py", "# 装饰器版\n", parent_fs)
    print("  A 写完 overlay, parent 未变:", parent_fs["app.py"] == "def handler(): ...\n")

    print("\n== 分支评分 ==")
    for b in branches:
        print(f"  {b.label} {b.steering:<16} tests={b.tests_passed}/{b.tests_total} "
              f"conf={b.confidence():.3f}")

    for mode in ("auto_with_fallback", "vote", "manual"):
        win = fork_run(branches, parent_fs, mode=mode)
        print(f"  mode={mode:<18} -> 胜者 {win.label}")

    print("\n== 单 harness 双入口：能力开关注入工具面 ==")
    for cap in ({}, {"shell": True, "memory": True}, {"forking": True, "shell": True}):
        agent = create_agent("stub", **cap)
        print(f"  caps={cap or 'default'} -> tools={agent('hi')['tools']}")
    return 0


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="cli7", description="最小终端 agent CLI 骨架")
    p.add_argument("prompt", nargs="?", help="单次运行（非交互）")
    p.add_argument("--model", default="stub")
    p.add_argument("--shell", action="store_true", help="注入 shell 工具")
    p.add_argument("--memory", action="store_true", help="注入 memory 工具")
    p.add_argument("--forking", action="store_true", help="注入 forking 工具组")
    p.add_argument("--demo", action="store_true", help="跑内置演示")
    a = p.parse_args(argv)

    if a.demo:
        return _demo()

    agent = create_agent(a.model, shell=a.shell, memory=a.memory, forking=a.forking)
    if a.prompt:
        out = agent(a.prompt)
        print(f"[{out['model']}] {out['prompt']}")
        print(f"tools: {', '.join(out['tools'])}")
        return 0

    caps = {"shell": a.shell, "memory": a.memory, "forking": a.forking}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        out = agent(line)
        print(f"[{out['model']}] {out['prompt']}  (tools={len(out['tools'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
