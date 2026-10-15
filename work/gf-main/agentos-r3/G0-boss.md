# G0-boss（agentos-console R3 真产物，非模板）

意图: 跑通 R3 提升 —— 受控进化合入管线 tools/evo_merge.py 落地并上链
（evo_merge_gate 7/7 + evo_merge_selftest 8/8），让进化从「建议改 system
prompt」变成「沙箱全绿才合入、红了自动回退」。

## 本轮蒸馏

R3 的目标切片（目标表 R3）：受控进化合入落地。管线 = 机器可执行提案
（find 锚点恰一次 / 新文件 write）→ 路径闸（../、绝对路径、禁改区）→
复制式沙箱（tools/ 全量 + 同构 work/plans 基线）→ 沙箱全绿才合入真树
（字节快照 → 应用 → 真树复验）→ 任一环红则不碰真树或自动回退；
账本 work/evo_merges.jsonl 逐行 verdict。

本产物证明：本轮账（work/gf-main/agentos-r3）真走过 G0。

## 编号 AC（与根 GOAL.json 的 acs 一致）

| AC | 前置 | 操作 | 断言命令 | 度量 |
| AC3 | evo_merge.py 落地 | 跑正向门 | python3 tools/evo_merge_gate.py | EVO_MERGE_GATE 7/7 rc=0 |
| AC3 | 自证 8 例 | 跑反向门 | python3 tools/evo_merge_selftest.py | 8/8 rc=0，4变异体咬红 |
| AC3 | 好提案验收 | 合入后能力门 | python3 tools/capability_gate.py | capability_gate 10/10 rc=0 |
| AC3 | 上链 | gates.sh 正确链含双门 | 链长 wc -w | 70 门，evo 双门在链 |

非目标: 不改动已交付的历史轮次与产物；不触碰门判据与硬阻断清单。

## 假设清单

1. 假设沙箱基线必须与真仓同构（缺 work/plans 时门自己的零污染误红）。
2. 假设弱变异体=判据有洞（M3 路径闸专属探针的教训）。

## 不做

- 不做 R1/R2（已在各自轮次收）。
- 不改门判据与阈值。

## 验证命令

cd agentos-console && .venv-pw/bin/python tools/evo_merge_gate.py（期望
EVO_MERGE_GATE 7/7 rc=0）+ .venv-pw/bin/python tools/evo_merge_selftest.py
（期望 8/8 rc=0）
