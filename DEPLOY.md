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

## 第三轮（2026-10-19 W25）：把「验证」从任务末尾前移到**动作边界**

来源（star≥1000，**只读上游源码，蒸馏进本仓，不上传任何自有内容**）：
- **Aider-AI/aider ★49636**（Apache-2.0）：`aider/coders/base_coder.py:1616-1621`
  ```python
  if edited and self.auto_test:
      test_errors = self.commands.cmd_test(self.test_cmd)
      self.test_outcome = not test_errors
      if test_errors:
          ...
          self.reflected_message = test_errors
  ```
  三个可蒸馏的设计点：① 验证由**编辑事件**触发，不是任务结束时才验一次；
  ② 失败报错**回灌**下一轮（`reflected_message`）；③ 每次改动的结论被记成
  布尔量（`test_outcome`）——「这次改动过没过检查」是可查的结论，不是印象。
- **SWE-agent/SWE-agent ★20597**（MIT）：`sweagent/environment/swe_env.py:226-228`
  工具失败必须**带 exit code** 回灌：
  `msg = f"Command {input!r} failed ({r.exit_code=}): {error_msg}"` ——
  报错是可行动的观察，不是原始异常。
- **OpenHands/OpenHands ★91270**（MIT）：运行时安全分析器对**每个动作**
  先评分再决定放不放行（我们已用自己的收敛引擎做同一件事，见 W23/W24）。

蒸馏落地（本仓，**本轮新写**）：
1. `tools/verify_cmd.py`：探测「这个仓库跑什么命令能证明改动是对的」
   （Makefile 有 test 目标 / package.json 有 test 脚本 / Cargo.toml / go.mod /
   Python 项目配置 → `pytest -q`）。**探测不到就返回 None** —— 猜一条必然
   失败的命令，会把「没找到验证方式」伪装成「验证失败」。
2. `tools/converge.py`：写盘成功 → 进入**待核对态**；核对指令在步骤边界注入，
   带上「该跑哪条命令 + 为什么是这条」；每轮只催一次（连催会淹掉模型）。
3. **核对结论入账**：`verify_outcome` = "" / "pass" / "fail"，新改动让旧结论作废；
   核对失败（rc≠0）时指令措辞从「还没核对过」改成「上一次核对**没通过**，
   先读懂报错再改」——这是上游 `reflected_message` 的等价物。
4. **有意偏离上游**：aider 是**内核自己跑**测试命令；AgentOS 不这么做 ——
   命令仍由 agent 通过 `run_cmd` 走白名单与留痕（内核代跑会绕过审计链）。
   引擎只负责**在正确的时机说「现在去核对」**。

门与自检：converge_selftest 37→**53/53** · converge_gate 19→**23/23** ·
agent_core 自检 152→**153**（新增「改完盘后核对指令真进 messages」内核级判据）。

上游源码副本只落在 `work/distill/w25/`（已 .gitignore），不进仓库。

## 第四轮（2026-10-20 W26）：任务契约 —— 不同类型的任务，要不同的「完成证据」

来源（star≥1000，**只读上游源码，蒸馏进本仓**）：
- **SWE-bench/SWE-bench ★6057**（MIT）`swebench/harness/constants/__init__.py:30-33`：
  ```python
  FAIL_TO_PASS = "FAIL_TO_PASS"
  FAIL_TO_FAIL = "FAIL_TO_FAIL"
  PASS_TO_PASS = "PASS_TO_PASS"
  PASS_TO_FAIL = "PASS_TO_FAIL"
  ```
  这个四分类就是本波的核心思想：**一个修复算不算数，由「改前失败的测试
  改后是否通过」判定**（fail→pass），而不是由「补丁看起来对不对」判定。
  这也说明「红→绿」不是我的发明，是这个领域的验收标准本身。
- 沿用 W25 的 **aider ★49636**（改完自动跑 lint/test）与
  **SWE-agent ★20597**（工具失败带 exit code 回灌），本波把它们和
  SWE-bench 的验收标准合成**一张按任务类型的完成契约表**。

蒸馏落地（本仓，**本轮新写**）：
1. `tools/task_contract.py`：
   - **类型判定**：bugfix / feature / refactor / docs / research / analysis /
     ops / generic。判定规则 =「**最先出现的动作主导**」（「把服务部署到
     8790 并写好说明文档」是 ops 不是 docs），命中词保守（宁可判 generic，
     也不给错契约——错契约会逼模型做它不该做的事）；判定**必带理由**，可复查。
   - **契约表**：每种类型给「硬要求（缺了就退回交付）+ 软要求（缺了只记账）
     + 开工指令（这类任务该怎么做）」。
   - **写操作被环境锁住时撤掉写类要求**（`relax_for_writes`）：plan 只读 /
     DSP 计划未审批时，写盘证据是**够不着**的证据，硬要它会让任务永远交不了
     付（闸门一直退到预算耗尽）。**契约必须服从环境现实。**
2. `tools/converge.py`：契约判据进闸门（`evidence_ok`，支持 any-of 组合）；
   新增红→绿证据账（`repro_fail_seen` / `fix_after_repro` / `repro_green`）；
   **盲改提醒**（缺陷修复类任务动手改了却从没看到原 bug 失败的样子 → 注入
   「先复现」指令，软提醒不硬拦：有些 bug 确实复现不了，硬拦只会逼模型
   编一个假复现）。
3. `tools/agent_core.py`：契约段写进 **system prompt**（模型开工前就知道
   「这类任务怎么算完成」）；任务类型进收敛快照 → 可被挖掘器统计
   （`contract:bugfix-blind` = 盲改型缺陷修复 → 自动生成自进化规则）。

**真机实测（缺陷修复任务，真实终端）**：
```
1. run_cmd ls -la → 2. read_file calc.py → 3. read_file Makefile
4. run_cmd make test      ← 复现：看到失败（红）
5. edit_file calc.py      ← 改
6. run_cmd make test      ← 再验：通过（绿）
```
账本：`contract_type=bugfix`、`repro_fail_seen=True`、`repro_green=True`、
`verify_outcome=pass`、`gate_retries=0`（交付闸一次放行——因为证据是真的）。

**门与自检**：task_contract_selftest **25/25**（新门项，已上链）·
converge_selftest 58 · converge_gate 24→**31/31**（含契约判据 + 变异体：
摘掉契约检查必红）· agent_core 自检 153→**155**。
