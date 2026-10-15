# GOALS — 目标表（先立表 → 一轮一行 → 每轮都要有提升）

- 需求: AgentOS 终端 Agent 全面完善（台账重建：原 work/GOAL.json 于 2026-10-14 状态事故中被清，按引擎正典 set-goal 重建）：以用户要求为中心、真实可用、能真实完成任务
- 目标: 交付「AgentOS 终端 Agent 全面完善（台账重建：原 work/GOAL.json 于 2026-10-14 状态事故中被清，按引擎正典 set-goal 重建）：以用户要求为中心、真实可用、能真实完成任务」：AC 全部有断言且命令退出码为 0
- 轮次计划: R1..R4（每轮必须 new-run.sh 新开自己的 00-project 账，复用账 = 本轮没提升）
- 真源: `work/GOAL.json#goal_table`（手改本表会被 `goal dept-dispatch` 判红 rc=2）

| 轮 | 本轮目标（动词+交付物） | 本轮判据（命令/退出码） | 00-project 账 | 状态 | 动的AC | 提升(delta) | 证据 |
|---|---|---|---|---|---|---|---|
| R1 | 跑通 R1 提升：全链门禁 rc=0（66 门；含防打转守卫 131/131 真生效） | AC 全 PASS，命令 rc=0 | agentos-r1 | 已收轮 | - | AC1 全链证据齐：agent_core --check 140/140 rc=0、leader_gate 10/10 rc=0、evo_merge_gate 7/7 rc=0、neg_agent_core 32/32 rc=0；70门链68/70（2引擎态诚实红待本轮收） | `agent_core --check → AGENT_CORE OK 140/140 rc=0；leader_gate → LEADER_GATE 10/10 rc=0；evo_merge_gate → EVO_MERGE_GATE 7/7 rc=0；neg_agent_core → NEG_AGENT_CORE OK 32/32 rc=0；全链 w13_chain → 68/70（round+close 引擎态）` |
| R2 | 跑通 R2 提升：用户报告的缺陷闭环：重复语句/连续失败 3-6 步内停、空答如实记账 | AC 全 PASS，命令 rc=0 | agentos-r2 | 已收轮 | - | AC2 三守卫落地：重复语句≥3步停（桩实测steps==3）/同工具连续失败≥6停（实测第6步）/空答empty-answer记账；批准即执行四根钉：计划口径随闸（自由面不再谎报锁死）+_looks_approved（拍板命中/不同意不误判）+零证据打脸err:no-evidence-claim+跨轮复读err:stale-reply | `agent_core --check → AGENT_CORE OK 140/140 rc=0；dsp_gate → DSP_GATE OK 18/18 rc=0；supervise_gate → SUPERVISE_GATE_OK rc=0；neg_agent_core → 32/32 rc=0` |
| R3 | 跑通 R3 提升：受控进化合入落地：坏提案必拦回退、好提案合入后能力门仍绿 | AC 全 PASS，命令 rc=0 | agentos-r3 | 已收轮 | - | AC3 受控合入落地：evo_merge_gate 7/7 rc=0、evo_merge_selftest 8/8 rc=0（4变异体咬红）、好提案合入后capability_gate 10/10 rc=0、链含双门（70门）；沙箱基线同构+M3判据洞两条教训 | `evo_merge_gate → EVO_MERGE_GATE 7/7 rc=0；evo_merge_selftest → 8/8 rc=0；capability_gate → 10/10 rc=0；链长 70 门 evo 双门在链 rc=0` |
| R4 | 跑通收官交付验证：复验R1/R2/R3已收轮证据并交付70门链70/70 | 70门链70/70 rc=0 | agentos-r4 | 已完成 | AC1,AC2,AC3 | AC1/AC2/AC3 三轮已收轮证据复验全绿：agent_core --check 140/140 rc=0、leader_gate 10/10 rc=0、evo_merge_gate 7/7 rc=0、dsp_gate 18/18 rc=0、supervise_gate OK rc=0、capability_gate 10/10 rc=0、round_gate D78 OK rc=0；交付70门链终跑 | `agent_core --check → AGENT_CORE OK 140/140 rc=0；round_gate → D78 OK rc=0；dsp_gate → 18/18 rc=0；supervise_gate → OK rc=0；capability_gate → 10/10 rc=0；evo_merge_gate → 7/7 rc=0` |

## 规则（机检，退出门只有退出码）

- 目标表缺本轮行 → `plan-round` rc=2（先立表，再开轮）
- 目标/判据是空话术或占位 → `set-goal` rc=2
- 复用别的轮次用过的 00-project 账 → `plan-round` rc=2（每轮新开专属账）
- 台账「本轮目标/判据」与本表不一致，或本表被手改 → `dept-dispatch` rc=2
- 目标表还有未收轮的行 → `close-round --verdict SHIP` rc=2（每轮都要有提升才叫完成）
- delta 空/占位 + `--verdict CONTINUE` → `close-round` rc=2 不予记账，该行永远进不了「已收轮」
- delta 没指名动的 AC（正文无 ACn 且没给 `--ac-moved`）→ `close-round` rc=2（BLOCK 除外）
- 00-project 账缺 `ROUND-BIND.md` / 绑定被手改 → `dept-dispatch`·`close-round` rc=2
