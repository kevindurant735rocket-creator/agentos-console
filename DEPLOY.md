# AgentOS 部署与自我提升纪要（2026-10-15 W14）

来源（star≥1000）：
- codewhale-hq/Codewhale ★41092：provider 抽象 + Rust agent engine / terminal client 分离
- herdrdev/herdr ★44127：coding agents 的运行时底座（runtime）
- can1357/oh-my-pi ★35508：IDE 接线进 agent 回路
- esengine/DeepSeek-Reasonix ★35803：复杂工程任务的可靠 coding agent
- 1jehuang/jcode ★20832：高性能 harness（Rust）
- modem-dev/hunk ★9613：review-first 终端 diff 查看
- Gaurav-Gosain/tuios ★5383：感知 agent 行为的 tiling TUI
- asheshgoplani/agent-deck ★1085：多会话统一 TUI

蒸馏落地（本仓）：
1. provider 抽象：保持 `tools/agent_core.py` Provider 可克隆/可替换（subagent 并行已用 clone），新增 provider 只需实现 clone+终答口径，不碰主循环。
2. review-first：合入前必须 `evo_merge` 沙箱全绿+真树复验；`git diff` 先行（本纪要即先 git init 再提交）。
3. 多会话 TUI：`agentos sessions/resume/branch` 延续 agent-deck/tuios 思路（一屏多会话、可回放、可分支）。
4. 可靠 harness：autonomy verify 双闸（worker rc=0 ∧ verify 全 rc=0）+ 有界自救 ≤2 次，对应 Reasonix/jcode 可靠性。
5. 部署面：`git init --initial-branch=main` + `.gitignore`（__pycache__/*.pyc/.DS_Store/.venv-pw/work 活日志/快照/ bak*），应用面 `agentos health` rc=0。

部署状态：
- 本地 git 已初始化（main），待 `git add/commit` 后由用户执行 `gh_deploy` / push（写面需审批）。
