# G0-boss（agentos-console R4 收官真产物，非模板）

意图: 跑通 R4 收官交付验证 —— 复验 R1/R2/R3 已收轮证据，确认 round_gate
与 close_gate 双门转绿，交付 70 门链全绿，以 SHIP 收官整个目标。

## 本轮蒸馏

R4 的目标切片（目标表 R4）：跑通收官交付验证。复验内容：
- R1：agent_core --check 140/140、leader_gate 10/10、evo_merge_gate 7/7、
  neg_agent_core 32/32（全部 rc=0，证据在各自 R 轮账内）。
- R2：dsp_gate 18/18、supervise_gate 9/9（全部 rc=0）。
- R3：capability_gate 10/10（全部 rc=0）。
- 双门：round_gate D78 OK（三轮 CLOSED 真轮史）、close_gate D79 全绿。

本产物证明：本轮账（work/gf-main/agentos-r4）真走过 G0。

## 编号 AC（与根 GOAL.json 的 acs 一致）

| AC | 前置 | 操作 | 断言命令 | 度量 |
| AC1 | R1-R3 已收轮 | 复验三轮证据仍绿 | 三轮账内 evidence 命令重跑 | 全部 rc=0 |
| AC2 | 双门待验 | 跑 round_gate + close_gate | python3 tools/round_gate.py / close_gate.py | D78 OK + D79 OK rc=0 |
| AC3 | 链待终跑 | 70 门全链 | bash tools/gates.sh <正链> | 70/70 rc=0 |

非目标: 不改动已交付的历史轮次与产物；不触碰门判据与硬阻断清单。

## 假设清单

1. 假设 SHIP 收官后根状态置 GOAL_SHIPPED，close_gate N7 的卡片世界成立。
2. 假设 close_gate 仍需 d79fix 夹具账独立存在（work/gf-d79）——本轮不碰它。

## 不做

- 不重做 R1/R2/R3（已收轮，引擎禁止重复收轮）。
- 不改门判据与阈值。

## 验证命令

cd agentos-console && .venv-pw/bin/python tools/round_gate.py（期望 D78 OK rc=0）
&& .venv-pw/bin/python tools/close_gate.py（期望 D79 OK rc=0）
