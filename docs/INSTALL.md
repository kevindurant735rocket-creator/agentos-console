# 安装（只装终端 CLI）

```bash
bash install.sh                 # 装到 ~/.local/bin，并在 shell 配置里加一行 PATH
bash install.sh --dest DIR      # 装到指定目录（不碰 shell 配置）
bash install.sh -q              # 安静模式
bash uninstall.sh               # 卸（只删本脚本装的那两样）
```

**装完得到什么**：一个 `agentos` 入口（生成式 shim，真源仍在仓库里）。
装完就能用：

```bash
agentos agent                 # 进终端 agent
agentos agent -p '把门链跑完'  # headless 一次跑完
agentos doctor                # 一键体检
```

**明确不装什么**（2026-10-20 起）：原生 macOS app、launchd 常驻服务、
浏览器驾驶舱、任何会自动弹窗的东西。终端 agent 是**按需跑**的。

配置在 `~/.agentos/config.toml`：

```bash
agentos config set llm.provider opencode
agentos config set llm.api_key <你的key>
agentos config set llm.no_fallback true   # 端点失败只重试云端，绝不静默换本地模型
```
