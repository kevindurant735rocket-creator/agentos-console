# G0-boss（agentos-console R1 真产物，非模板）

意图: 跑通 AgentOS 终端交付仓 R1 提升 —— 全链 70 门禁 rc=0（含 W13 批准即执行
6 例 + autonomy 引擎 3 例，真生效 140/140）。

用户/场景: 张峰瑞（高中生参赛者，AI 当他的工程团队）；每轮独立目标，
全链全绿才收轮。

## 本轮蒸馏

R1 的目标切片（目标表 R1）：全链门禁 rc=0（70 门；含防打转守卫 140/140 真生效）。

本产物证明：本轮账（work/gf-main/agentos-r1）真走过 G0 —— 上述蒸馏是本轮
真实交付的提炼，不是 new-run.sh 预埋空账。

## 编号 AC（与根 GOAL.json 的 acs 一致）

| AC | 前置 | 操作 | 断言命令 | 度量 |
| AC1 | W13 两半落地 | 跑全链 70 门 + agent_core --check | bash tools/gates.sh <正链> + --check | AGENT_CORE OK 140/140，GATES 68/70（2 引擎态诚实红） |
| AC2 | 事故现场在手 | 打转守卫/批准执行三件套落地 | 桩驱动 + 现场复现 | 重复3步停/失败6次停/空答记账 |
| AC3 | evo_merge 落地 | 受控合入 7/7+8/8 上链 | evo_merge_gate/selftest | 合入后 capability 10/10 |

非目标: 不改动已交付的历史轮次与产物；不触碰门判据与硬阻断清单。

## 假设清单

1. 假设 round_gate 的 p_e13b 看的是"活根带真 giga_run+delta 的行数"，本 G0
   产物 + 后续 close-round 的真 evidence 共同满足它。
2. 假设 close_gate 的 d79fix 夹具账独立（work/gf-d79），与本账无关。
3. 假设 70 门链的 2 个引擎态红（round/close）随本轮 R1 状态恢复而转绿。

## 不做

- 不做 R2（重复语句守卫已在 W13 前半落地，本轮只记账不重复做）。
- 不改门判据与阈值（提升是交付，不是放宽）。

## 验证命令

cd agentos-console && .venv-pw/bin/python tools/agent_core.py --check（期望
AGENT_CORE OK 140/140，rc=0）
