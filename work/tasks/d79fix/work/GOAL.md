# GOAL — 对话目标（唯一方向锚）

- 需求: D79 收轮就绪度验收夹具（按 close_gate 源码规约重建：R1 OPEN、3 条 AC、R2 待办）
- 目标: 交付「D79 收轮就绪度验收夹具（按 close_gate 源码规约重建：R1 OPEN、3 条 AC、R2 待办）」：AC 全部有断言且命令退出码为 0
- 轮次计划: R1..R2（每轮必须走**本轮专属** gigafactory 账 + 部门子智能体）
- 目标表: `work/GOALS.md`（每轮一行：目标/判据/00-project 账/状态/提升/证据）
- 成功判据: 台面新增 #gpre 收轮就绪度：disk 族与 draft 族逐条显示过没过，缺哪条写清 why；证据行给可复跑命令：build.py / close_gate.py / gates.sh 各自 rc=0；裁决语义 CONTINUE/SHIP 两端引擎都识别（SHIP 时若 R2 待办必须拦）
- 非目标: 不改动产品代码与门判据（本夹具只验收轮就绪链路）；不触碰活项目根 work/GOAL.json（测试一律进 taskdir）
- 对话目标 id: （未绑定：调 create_goal 后 bind-goal）

## CREATE_GOAL_ARG

```
交付「D79 收轮就绪度验收夹具（按 close_gate 源码规约重建：R1 OPEN、3 条 AC、R2 待办）」：AC 全部有断言且命令退出码为 0
```
