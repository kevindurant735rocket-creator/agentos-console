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
- **PyCQA/bandit ★8326**（Apache-2.0，Python）：Python 静态安全缺陷扫描器，规则社区十余年沉淀
  （B1xx 硬编码凭据 / B3xx 不安全 urlopen·反序列化 / B6xx subprocess 面）

蒸馏落地（本仓）：
1. provider 抽象：保持 `tools/agent_core.py` Provider 可克隆/可替换（subagent 并行已用 clone），新增 provider 只需实现 clone+终答口径，不碰主循环。
2. review-first：合入前必须 `evo_merge` 沙箱全绿+真树复验；`git diff` 先行（本纪要即先 git init 再提交）。
3. 多会话 TUI：`agentos sessions/resume/branch` 延续 agent-deck/tuios 思路（一屏多会话、可回放、可分支）。
4. 可靠 harness：autonomy verify 双闸（worker rc=0 ∧ verify 全 rc=0）+ 有界自救 ≤2 次，对应 Reasonix/jcode 可靠性。
5. 部署面：`git init --initial-branch=main` + `.gitignore`（__pycache__/*.pyc/.DS_Store/.venv-pw/work 活日志/快照/ bak*），应用面 `agentos health` rc=0。

部署状态：
- 本地 git 已初始化（main），待 `git add/commit` 后由用户执行 `gh_deploy` / push（写面需审批）。

## 第二轮（2026-10-19）：bandit 作为静态安全量具部署

为什么是「部署真工具」而不是「蒸几条规则进来」：安全规则表是活的（CVE 与
攻击面随生态变），抄一份就僵；只有让每次改码都重新落地同一套社区规则，才有
复利。做法与边界：

- **不进运行时**：`tools/selfscan.py --install` 把 bandit==1.9.4 及其依赖
  （PyYAML/rich/stevedore…）`pip install --target .vendor/bandit`，`.vendor/`
  进 `.gitignore`。主链路仍 **100% 纯 stdlib**，只是门里多一个外部量具。
- **钉死版本**：`PINNED = "1.9.4"`。版本漂移直接判红（供应链变了要人确认），
  不做静默跟随。
- **只判新增**：存量 44 条 MEDIUM 已逐条审过——B310 是 32 处 `urlopen`，调用点
  前置已有 scheme 白名单（如 `_t_web_fetch` 只放 http/https、`_t_health` 只放
  回环）；B105 是门令牌字面量（`SNAP-GATE-OK` 之类）；B108/B102 在自测脚本里。
  这些进 `work/selfscan_baseline.json`（键=文件|规则 id，**不带行号**，行号漂移
  不该让门失效）。基线外新增 MEDIUM 或任何 HIGH → 红。
- **未部署 ≠ 绿**：`.vendor/bandit` 不在时 rc=3 并明说未部署，绝不返回 0。
- **量具没量到东西 ≠ 绿**：`loc==0`（扫了个空）判红。

落地文件：`tools/selfscan.py`（部署+扫描+基线判定，含纯函数 `verdict` 供桩测）、
`tools/selfscan_gate.py`（反向门 10 条：部署齐/生产真绿/喂 HIGH 判红/喂超基线
MEDIUM 判红/喂 loc=0 判红/空基线判红/未部署 rc=3/零残留）、
`tools/gates.sh` 链尾挂 `selfscan_gate`（71→72 门）、`agentos.py selfscan` 子命令。

实测（真树，非桩）：`loc=48515 HIGH=0 MEDIUM=44 LOW=345`，`SELFSCAN OK 3/3`
（rc=0）；`SELFSCAN_GATE OK 10/10`（rc=0）。
