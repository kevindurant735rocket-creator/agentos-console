# cli8 —— hello CLI

一个极小的 Python 终端 agent 骨架，蒸馏自 [gptme/gptme](https://github.com/gptme/gptme)（MIT，★4455）的两条设计点：

1. **Provider 无关**：`--provider` / `--model`（或 `AGENT_PROVIDER` / `AGENT_MODEL` 环境变量）只改配置，不动主循环。
2. **能力=工具**：工具注册表 `TOOLS` + `@tool` 装饰器，CLI 入口保持极薄。

## 运行

```bash
python3 main.py                 # hello
python3 main.py -t upper hello  # HELLO
python3 main.py -V
```

退出码：0 成功，1 工具执行失败（便于脚本化）。

## 结构

- `main.py` —— argparse 入口 + 工具注册表
