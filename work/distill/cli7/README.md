# cli7 — 蒸馏自 vstorm-co/pydantic-deepagents (★1103, Python, MIT)

最小可跑的终端 agent CLI 骨架，只实现 study7.md 里提炼的两条设计点的**可操作内核**：

1. **Live Run Forking**：一次 run 分多分支，CoW 式 overlay 隔离 + 测试真值加权评分选胜者。
2. **单 harness 双入口**：同一套 loop 既暴露为 CLI，也暴露为一行调用的 Python 函数；
   能力靠参数字典开关注入工具面。

## 运行

```bash
python3 cli7.py --demo
```

无第三方依赖，Python 3.10+。
