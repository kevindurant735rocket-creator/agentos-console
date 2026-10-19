# GitHub 成熟实现研究 #3：vstorm-co/pydantic-deepagents

- 检索方式：`gh_search(kind=repos)`，再按 `stars:>1000` 过滤
- 命中（3 个 ≥1k）：`OpenOSINT/OpenOSINT` ★1777 · `massgen/MassGen` ★1141 · `vstorm-co/pydantic-deepagents` ★1103
- 选中：`vstorm-co/pydantic-deepagents` ★1103 · Python · MIT · "Open-source, self-hosted Claude Code — a terminal AI assistant and the Python framework behind it."
- 同类性最高：它同时是「终端 agent」和「驱动它的 Python 框架」，与 AgentOS console（终端 agent + tools/ 工具面 + 闸门）形态一致；另外两个是领域 agent（OSINT）与多 agent 编排，偏离本仓主线。
- 读取：`gh_file_read(repo="vstorm-co/pydantic-deepagents", path="README.md")` 前 60 行

## 从 README 观察到的事实

1. 一个仓库两个面：终端助手（TUI）与 Python 框架，**同一套 harness**；`create_deep_agent()` 一次调用装配文件系统、shell、规划、记忆、子代理、沙箱、MCP。
2. 主打特性 **Live Run Forking**：一次运行遇岔路口就分叉成 N 条分支，每条分支**完全隔离** —— 写操作落在 copy-on-write 覆盖层（读穿透到父层，写只留本地），各自有独立 steering 消息和独立 `budget_usd`。
3. 分叉收敛由协调器按**可插拔接受模式**裁决：`manual` / `auto` / `auto_with_fallback`（默认）/ `vote`；获胜分支的历史被采纳为父运行的续跑。
4. 打分是**加权可复算**的：`confidence = quality_spread·0.4 + test_pass_ratio·0.4 + internal_consistency·0.2`；其中 test_pass_ratio 由**真实测试命令的 exit code** 决定（`LiveForkCapability(test_command="pytest -q", test_timeout_s=120)`），不是让模型自评。
5. 能力是**显式 opt-in 的 Capability 对象**（`forking=True` / `forking=LiveForkCapability(...)`），不默认全开；类型 100% 由 pydantic 约束；安装走 `install.sh` 引导器（自举与日常使用解耦）。

## 蒸馏 3 条可借鉴设计点（映射到 AgentOS）

### D1. 候选方案必须隔离试跑，且各自带预算
pydantic-deep 的每条分支跑在 copy-on-write 覆盖层里，读穿透、写本地，另配 `budget_usd` 与 `test_timeout_s`，所以「试错」不会污染父运行，也不会无限烧钱。
**映射**：本仓的 `branch` 语义目前是会话层分叉（`tools/branches.py`），但**代码层**的多方案试跑没有隔离量具——直接在原目录跑多个候选的验证命令，候选之间会互相污染。
**落地**：候选目录逐份复制到临时副本再跑验证命令，原树只读；每条命令带独立超时预算，超时记为 rc=124（不挂死、不算通过）。

### D2. 收敛策略可插拔，且默认档位 fail-closed
四种接受模式里默认不是 `auto` 而是 `auto_with_fallback`：需要「分数过线 + 领先次名够多」才敢采纳，否则把决定权交回人，而不是随手挑一个。
**映射**：本仓以「不装成功」为铁律（verification-before-completion），但缺一个把「多方案择优」也纳入该纪律的量具。
**落地**：实现 `auto` / `auto_with_fallback` / `vote` / `manual` 四档；默认档位在并列或分差不足时**明确给出 NO-WINNER（rc=2）**，`manual` 永远不替人拍板（rc=4，winner 为空）。

### D3. 打分公式写死、可复算，测试项只认真实 exit code
`0.4/0.4/0.2` 三个权重是常量，`test_pass_ratio` 来自命令 rc，`quality_spread` 是候选间的相对领先幅度，`internal_consistency` 是与多数派的一致度——没有一项来自模型自述。
**映射**：本仓所有门都要求「证据是工具结果里的 rc」。
**落地**：`tools/selfscan2/selfscan2.py` 里权重是模块级常量 `W_PASS/W_LEAD/W_AGREE`，只有 `run_checks()` 真跑出来的 rc 能进公式；`--json` 落盘每个候选的原始 rc 明细，便于逐条复算。

## 本次落地

- 工具：`tools/selfscan2/selfscan2.py`（纯 stdlib，`--selftest` 带变异体验牙）
- 主要借鉴点：D2 + D3（**基于 D2 的「接受模式 + fail-closed 默认档」**，D3 提供可复算打分）
- 部署：`tools/selfscan2/` → 独立 public 仓库（描述含 derived from）

## 参考

- https://github.com/vstorm-co/pydantic-deepagents （★1103, MIT, Python）
- 同批命中（未采用）：https://github.com/massgen/MassGen （★1141）、https://github.com/OpenOSINT/OpenOSINT （★1777）
