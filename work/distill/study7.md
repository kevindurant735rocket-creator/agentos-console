# Study 7 — vstorm-co/pydantic-deepagents

选定仓库：**vstorm-co/pydantic-deepagents** ★1103 · Python · MIT License
（GitHub 描述：Open-source, self-hosted Claude Code — 终端 AI 助手 + 其背后的 Python 框架。
tool-calling、沙箱执行、多 agent 团队、skills）
检索方式：gh_search repos "terminal AI coding agent CLI python stars:>1000" → 命中 1 条 ★1103。
gh_repo_view 核实：star=1103, language=Python, license=MIT, updatedAt=2026-10-19T02:10:30Z。

## 设计点 1：Live Run Forking —— 一支 run 分出多分支，用测试退出码 + AI judge 选出胜者

**做法**：当 agent 在岔路口（例如"用装饰器重构还是用上下文管理器重构"）时不再赌一次，
而是把这一次 run 复制成 N 个并行分支：

- 每个分支是**完全隔离**的：写时复制（copy-on-write）文件系统 overlay——读穿透到父层，
  写只留在本地；各自带自己的 steering message；各自带自己的 `budget_usd` 上限。
- 调度器用 4 种验收模式解决分叉：`manual` / `auto` / `auto_with_fallback`（默认）/ `vote`。
- 每个分支可以跑真实测试命令（`test_command="pytest -q"`, `test_timeout_s=120`），
  置信度公式：`quality_spread*0.4 + test_pass_ratio*0.4 + internal_consistency*0.2`。
- 胜出分支的 history 被采纳为父 run 的后续，继续往下跑，而不是重开。

**蒸馏要点（可迁移到 AgentOS 的地方）**：
1. 分支隔离靠 CoW overlay 而不是 git worktree 或全量拷贝——成本线性于写入量，不是仓库大小。
2. 裁判不用 LLM 自由点评，而是把**可执行的真值**（测试通过率）以 0.4 权重注入评分，
   这样"看起来更好"的方案不会自动胜出。
3. 默认 `auto_with_fallback` 说明产品判断：全自动是目标状态，回退到人工 picker 是保底路径。
4. 分支自带 budget 上限 —— 分叉不会把账单放大到不可控。

来源：https://github.com/vstorm-co/pydantic-deepagents/blob/main/README.md
（"⑂ Live Run Forking" 一节）

## 设计点 2：同一套 harness 既做终端助手，也做一行调用的 Python 框架

**做法**：README 明确说这是"two things in one repo"，且**跑的是同一个 harness**：

- 终端侧：Claude Code 风格的 TUI，`pydantic-deep` 命令；`install.sh` 自己装 `uv` + CLI，
  用户不需要先配 Python 环境。能力面：规划、改文件、跑命令、搜网页、跨会话记忆、
  派生 sub-agent、接 MCP server。
- 框架侧：`create_deep_agent(model="anthropic:claude-sonnet-4-6")` 一行给出一个带
  文件系统、shell、planning、memory、sub-agents、沙箱执行、MCP、无限上下文的 agent；
  能力靠参数开启（如 `forking=True` 才注入 `fork_run`/`inspect_branches`/`merge_or_select`/
  `diff_branches`/`fork_cost`/`terminate_branch` 这组工具）。
- 统一选型约束：基于 Pydantic AI，**任意模型**（Claude/GPT/Gemini/本地），100% type-safe，MIT。

**蒸馏要点（可迁移到 AgentOS 的地方）**：
1. "CLI 和 SDK 共用一套 harness"是分层的正解：产品是框架的第一个用户，
   避免出现"演示脚本一套逻辑、库另一套逻辑"的双份实现漂移。
2. 能力以**参数开关注入工具集**（`forking=True` → 6 个新工具），而不是继承/子类化——
   工具面随配置变化，schema 保持不变，对 type-safe 友好。
3. 终端助手的能力清单刻意对齐 Claude Code（plan/edit/run/search/memory/subagents/MCP），
   把差异化只押在 forking 一个点上——功能面跟跑、单点超车，是省力的竞争策略。
4. 安装脚本负责引导 `uv`，把 Python 环境这个最常见的首跑门槛吃掉。

来源：https://github.com/vstorm-co/pydantic-deepagents/blob/main/README.md
（"Two ways to use it" / "Why pydantic-deep?" 两节）
