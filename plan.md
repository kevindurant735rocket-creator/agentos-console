# AgentOS 世界级补强 plan

目标：70门全绿基线上，补4个根源缺口，不动门链语义，达到可交付/可回滚。
范围：tools/仅4文件最小改 + Dockerfile/CI/.env/rollback四件套。
不做：模型换型、UI重构、全链70门重跑（只跑受影响门）。

模块/接口/改动：
1. 规划审核 dsp.py validate_plan加环检查 + unapprove_plan
   接口：validate_plan()多一条环拒，approve_plan()不变，新增unapprove_plan(sid)。
   验收：环计划save拒，dsp_gate绿，autonomy D2仍红正确。
   回滚：git checkout tools/dsp.py。
2. GitHub最小权限 gh_client.py 去掉("auth","token")只读放行
   接口：is_readonly(argv)对auth token返False。
   验收：gh_gate绿，gh auth token走_run拒。
   回滚：git checkout tools/gh_client.py。
3. 老板监督 steer.py commit原子写（tmp+replace）
   接口：push/pending/commit签名不变。
   验收：supervise_gate绿，并发不回放。
   回滚：git checkout tools/steer.py。
4. 自进化沙箱 evo_merge.py DEFAULT_GATES加safety_gate
   接口：--gates默认值变3门，显式传参不变。
   验收：evo_merge_gate绿，沙箱多一道安全门。
   回滚：git checkout tools/evo_merge.py。

部署四件套：Dockerfile + .github/workflows/gates.yml + .env.example + rollback.sh。
验收总闸：agent_core --check 140/140，safety_gate 9/9，dsp_gate，gh_gate，supervise_gate，evo_merge_gate绿。
回滚总闸：rollback.sh一键还原tools/。

## 第二波（世界级，2026-10-15）
5. 提案 intake 桥 agent_core._evo_propose 同写<id>.json（schema: id/sig/count/advice/kind/created/status）+ validate_proposal() 纯校验；--check 不动（140令牌冻结）。验收：json与md同名成对，非法提案拒收；回滚 checkout agent_core。
6. 监督诚实 watch.snapshot 区分空与坏（sess_err/steer_err），render 坏时写不可读而非无记录；happy path 字节不变。验收 supervise_gate 绿；回滚 checkout watch。
7. 撤销 kill-switch autonomy_gate 加 D13（跑1个→unapprove→再跑拒，零新增派发）；M1 覆盖。验收 autonomy_gate 13/13，selftest 10/10；回滚 checkout autonomy_gate。
8. serve promote/distill 维持只读转发（判定权在仓外引擎，刻意解耦），不造假执行体。

## 第三波（老板模块+完全权限，2026-10-15）
9. full_access：agent_core _hard_deny行不动，调用处ctx флаг bypass+审计；代码默认关，本机config开。验收：有flag放行、无flag仍拒，safety D5/M3绿。
10. boss.py独立进程：review/monitor/stop/pilot；确定性三件（schema+环+风险），拒必说理，verdict落盘。
11. boss_gate 7/7进链71。验收：B1-B7绿。回滚见rollback.sh。

## 第四波（一步到位，2026-10-15）
12. boss fix自救环 + memory执行体 + pilot --max-tasks；boss_gate B7/B8→9/9。
13. leader D9文案去 stale；README W14 波次；回滚脚本覆盖新文件。

## 第五波（2026-10-16）
14. 门链不删：门是测试不是运行时关卡，删门=删证据，与不撒谎冲突；运行时已零门。
15. pilot预检+续跑+预算；真实E2E实证（见progress）。

## 第六波（2026-10-16）
16. 去链上限+stuck自救+pilot自动fix；保命四件不动。

## 第七波（2026-10-16）
17. 子代理去钳+统一续跑；禁嵌套与子集闸不动。

## 第八波（2026-10-16）
18. boss模型大脑：review实质审+fix现想+replan；规则硬线+模型增益，fail-closed。

## 第九波（2026-10-16）
19. 并行监工supervise+检查点合并+pid判活；pilot默认并行。
