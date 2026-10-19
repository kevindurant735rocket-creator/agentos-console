# GOALS — 目标表（先立表 → 一轮一行 → 每轮都要有提升）

- 需求: 实现一个本地库存后台，AC 全 PASS rc=0
- 目标: 交付「实现一个本地库存后台，AC 全 PASS rc=0」：AC 全部有断言且命令退出码为 0
- 轮次计划: R1..R3（每轮必须 new-run.sh 新开自己的 00-project 账，复用账 = 本轮没提升）
- 真源: `work/GOAL.json#goal_table`（手改本表会被 `goal dept-dispatch` 判红 rc=2）

| 轮 | 本轮目标（动词+交付物） | 本轮判据（命令/退出码） | 00-project 账 | 状态 | 动的AC | 提升(delta) | 证据 |
|---|---|---|---|---|---|---|---|
| R1 | 跑通 R1 提升：库存列表 3s 内可见 | AC 全 PASS，命令 rc=0 | - | 待办 | - | - | - |
| R2 | 跑通 R2 提升：扣减后库存数正确 | AC 全 PASS，命令 rc=0 | - | 待办 | - | - | - |
| R3 | 跑通 R3 提升：回滚可用 | AC 全 PASS，命令 rc=0 | - | 待办 | - | - | - |

## 规则（机检，退出门只有退出码）

- 目标表缺本轮行 → `plan-round` rc=2（先立表，再开轮）
- 目标/判据是空话术或占位 → `set-goal` rc=2
- 复用别的轮次用过的 00-project 账 → `plan-round` rc=2（每轮新开专属账）
- 台账「本轮目标/判据」与本表不一致，或本表被手改 → `dept-dispatch` rc=2
- 目标表还有未收轮的行 → `close-round --verdict SHIP` rc=2（每轮都要有提升才叫完成）
- delta 空/占位 + `--verdict CONTINUE` → `close-round` rc=2 不予记账，该行永远进不了「已收轮」
- delta 没指名动的 AC（正文无 ACn 且没给 `--ac-moved`）→ `close-round` rc=2（BLOCK 除外）
- 00-project 账缺 `ROUND-BIND.md` / 绑定被手改 → `dept-dispatch`·`close-round` rc=2
