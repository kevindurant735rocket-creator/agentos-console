# study4 — gptme/gptme 蒸馏（★4455 · Python · MIT）

来源：https://github.com/gptme/gptme
选中理由：`gh_search query="python terminal agent stars:>1000 language:Python"` 命中 8 条中，
gptme 是 star 最高的「终端 agent」（★4455；harlequin ★6472 是 SQL IDE，不算 agent）。

## 3 条可借鉴设计点

1. **Provider 无关 + local-first 的模型层**
   README 强调 "Provider-agnostic, local-first, unconstrained"，一套 CLI 同时接 Anthropic /
   OpenAI / Google / DeepSeek / OpenRouter / Ollama / 任意 OpenAI 兼容端点。
   → 借鉴：把「模型选择」做成运行时配置而不是硬编码分支，端点失败可现场换模型（对本机
   403/400 端点排障尤其重要）。

2. **工具集是能力的唯一边界（shell / python / web / vision 一等公民）**
   "ships with shell, Python, web, vision" + 沙箱化执行（Docker / Wasmtime）+ 「非交互退出分类
   （non-interactive exit taxonomy）」。
   → 借鉴：CLI 里每个工具都该有明确的退出码语义，区分「agent 失败 / 工具失败 / 环境失败」，
   自动化流程才能据此决策，避免静默假成功。

3. **可扩展性分层：Plugins → Skills → Lessons，不塞进核心**
   插件系统（v0.30）+ 插件注册表（gptme-plugin-registry）+ skills as slash commands（v0.34）
   + Lessons 做上下文注入式引导；核心保持薄，扩展走注册发现。
   → 借鉴：我们的 skills（/skill 注入正文）机制与之同构——扩展以「发现 + 注入上下文」实现，
   而不是往主循环里加分支。

## 其他可取
- `gptme-tui`（Textual）：可折叠工具输出、状态栏显 token/agent 状态、作业中可排队 prompt。
- 自主 agent 走 `gptme service init` 无头化 + 定时预发布，先把「跑得久」验证充分。
