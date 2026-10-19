# agentos-study-cli3

最小终端 agent CLI 骨架。**derived from** [gptme/gptme](https://github.com/gptme/gptme)（★4455 · Python · MIT）的 README / `pyproject.toml` 设计蒸馏，为 AgentOS 学习用。

> 蒸馏全文见 `work/distill/study3.md`（Round 2 节）。

## 三条借鉴点 → 代码落点

| # | gptme 做法 | 本骨架落点 |
|---|---|---|
| 1 | `[project.entry-points."gptme.plugins"]` 声明式注册插件，工具各自独立模块、按需加载 | `main.py: ToolRegistry.register` / `ToolRegistry.discover()`（`importlib.metadata.entry_points(group="agentos3.tools")`）；`agentos3 tools` 子命令列出已注册工具与风险等级 |
| 2 | `gptme` / `gptme-tui` / `gptme-server` 共享同一 core；非交互运行有明确 **exit taxonomy** | `main.py: ExitCode` 枚举（OK/USAGE/MODEL_ERR/TOOL_ERR/BUDGET/INTERRUPT），`run_once()` 与 REPL 共用；一键 `python main.py run "..."` 走同一路径 |
| 3 | `minimal` extra 零原生扩展 + `[sandbox]`/docker/wasm 可切换，README 显式声明默认非沙箱 | `pyproject.toml` 的 `optional-dependencies`（tui/server/sandbox/all）；工具的 `risk` 字段 + `--sandbox` 开关 + 启动横幅打印风险档位 |

## 用法

```bash
python main.py --help
python main.py tools                    # 借用点 1：列工具注册表（含 risk）
python main.py run "读一下 README"       # 借用点 2：非交互，退出码可编程
python main.py chat                     # 交互 REPL，与 run 共用 core
echo "hi" | python main.py run -        # stdin 模式
python main.py --sandbox docker run "x" # 借用点 3：风险档位显式切换
```

退出码（借用点 2）：

| code | 含义 |
|---|---|
| 0 | 正常结束 |
| 2 | 用法错误 |
| 3 | 模型/后端错误 |
| 4 | 工具执行失败 |
| 5 | 预算（步数/token）耗尽 |
| 130 | 用户中断 |

## 范围声明

这是**骨架**，不是可用 agent：不接任何真实模型，`DemoPlanner` 只按关键词选工具演示调用链；`run_cmd` 工具默认 **未沙箱**，仅本地学习用途。
