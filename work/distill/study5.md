# gptme 蒸馏笔记（study5）

- 来源仓库：gptme/gptme（★4455，Python，MIT）
- 定位：a personal AI agent that runs anywhere a terminal runs
- 入选理由：query `python terminal agent stars:>1000 language:Python` 命中 8 个结果中，
  真正意义上的"终端 agent"最高星为 gptme（★4455）；更高星的 harlequin（★6472）是 SQL IDE，
  不属于 agent 类，故取 gptme。

## 3 条设计点

1. **终端即运行时，双入口一体**：同一个核心既提供 plain `gptme` CLI（脚本化/非交互），
   也提供 Textual 的 `gptme-tui`（队列化提交 prompt、可折叠工具输出、状态栏显示模型/token/状态）；
   强调在 laptop、ssh、tmux、headless server、CI pipeline 上行为一致。
2. **内置工具 + 可沙箱执行 + 可扩展**：shell / python / web / vision 等工具开箱即用，
   执行可落到沙箱（Docker、Wasmtime）；扩展走插件（plugin registry）、skills（可作 slash 命令）、
   lessons（上下文相关指导）三层，不用改核心。
3. **provider 无关 + 本地优先 + 长会话工程化**：Anthropic/OpenAI/Google/DeepSeek/OpenRouter/Ollama
   及任意 OpenAI 兼容端点可切换；配套上下文压缩（context compression）、token 感知、
   成本追踪（cost tracking）、后台任务与内容寻址存储，用于支撑长期自主运行（autonomous agent）。
