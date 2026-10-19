# agentos-study-cli2

一个最小 Python 终端 CLI 骨架，**derived from [Aider-AI/aider](https://github.com/Aider-AI/aider)**（★49630, Apache-2.0）。

仅用于学习/演示：把 aider README 里观察到的三个设计点落到可运行的最小骨架上。

## 借鉴点 → 代码映射

| 借鉴点（来自 aider） | 本骨架实现 |
|---|---|
| **D1** 改动自动提交、可 diff / undo | `snapshot` / `undo` 子命令：写操作前打快照，可一键回到编辑前 |
| **D2** 编辑后自动 lint & test，失败可修 | `verify` 子命令：按序执行验证命令，收集真实 rc，任一失败即非零退出 |
| **D3** `--model` 可插拔 + repomap 控制上下文 | `--model` / `AGENTOS_MODEL` 注入，无硬编码；`map` 生成受限规模的项目地图 |

## 安装

```bash
pip install -e .
```

## 用法

```bash
# 项目地图（路径 + 行数 + 首行摘要），限制条目数控制上下文体积
agentos-study-cli2 map --root . --limit 40

# 打快照（写面操作前）
agentos-study-cli2 snapshot --root . --label before-edit

# 回滚到最近一个快照
agentos-study-cli2 undo --root .

# 验证闭环：顺序执行命令，任一失败即非零退出
agentos-study-cli2 verify --cmd "python -c 'print(1)'" --cmd "python -m compileall -q ."

# 模型可插拔
agentos-study-cli2 info --model sonnet
AGENTOS_MODEL=deepseek agentos-study-cli2 info
```

## 说明

- 无网络、无外部依赖，纯标准库。
- 所有命令回真实退出码，便于被上一层 agent 的 verification 步骤直接消费。

## License

本骨架代码为演示用途；设计思路来源于 Aider-AI/aider（Apache-2.0）。
