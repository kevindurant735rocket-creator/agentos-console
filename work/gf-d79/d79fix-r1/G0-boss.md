# G0-boss（d79fix R1 真产物，非模板）

意图: 交付「收轮就绪度」验收夹具的 R1 轮 —— 让 close_gate 能在真引擎账上跑通
disk 族判据（goal_json / round-file / opened / not-closed / row / bind / giga）。

## 本轮蒸馏

R1 的目标切片：台面新增 #gpre 收轮就绪度，disk 族与 draft 族逐条显示过没过。
本产物证明：本轮账（work/gf-d79/d79fix-r1）真走过 G0，而不是 new-run.sh 预埋空账。

## 编号 AC（3 条，与 GOAL.json 目标表一致）

| AC | 前置 | 操作 | 断言命令 | 度量 |
| AC1 | 服务在 8791 起 | 调 close-preflight | curl POST /__goal/chain op=close-preflight | ready=true 且 disk 族全绿 |
| AC2 | 证据行完整 | 复跑三门 | python3 tools/build.py / close_gate.py / gates.sh | 各自 rc=0 |
| AC3 | R2 待办存在 | 提交 verdict=SHIP | close-preflight | ship-pending 判 BLOCK（不许提前报 SHIP） |

## 假设清单

1. 假设夹具只验「就绪度预检」链路，不验真实产品轮次（那是引擎侧 round_gate 的事）。
2. 假设 close_gate 的盘族判据全部走引擎 goal_loop 的真函数（serve.py close_preflight）。
3. 假设夹具账目录由 GF_OUT=work/gf-d79 指向，不碰 ~/.codex/gf-runs 主账。

## 不做

- 不做 R2 的收轮（夹具按规约保持 R2 待办，供 ship-pending 判据使用）。
- 不改 close_gate 的任何判据与阈值（只补夹具状态）。
- 不碰活项目根 work/GOAL.json。

## 验证命令

cd agentos-console && python3 tools/close_gate.py（期望 rc=0，D79 收轮就绪度 OK）
