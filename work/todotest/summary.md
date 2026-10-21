# todotest 仓库摘要

## 仓库结构（`ls -la` 实测，rc=0）

| 路径 | 类型 | 大小 |
|---|---|---|
| `run.exp` | 文件 | 390 B |
| `session.log` | 文件 | 5613 B（16:39 时为 5613 B） |
| `work/` | 目录 | 内含 `todos/`（任务账本 JSON） |

**注意：本仓库没有 README 文件。** 首次探查时 `list_dir .` 与 `ls -la` 均未发现 README，因此「读 README」一步实际读的是 `run.exp` 与目录结构。

## run.exp 的内容（`cat run.exp` 实测，rc=0）

一段 expect 脚本，用于驱动 AgentOS 的 agent 终端交互：

1. `set timeout 700`
2. `log_file -a work/todotest/session.log` —— 会话输出追加写入 `session.log`
3. `spawn python3 tools/agentos.py agent --root work/todotest` —— 在 `work/todotest` 作根目录启动 agent
4. 等提示符 `> `，sleep 1，然后发送本次任务指令（用 todo 工具列三步清单）
5. 再等 `> `，sleep 8，发送 `/exit` 退出

## 一句话总结

这是一个用来验证 AgentOS「todo 任务账本」流程的最小测试仓：`run.exp` 负责自动化跑一轮对话，`session.log` 存这一轮的终端记录，`work/todos/` 存账本状态。
