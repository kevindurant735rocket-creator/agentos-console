# gptme 蒸馏（study6）

来源仓库：https://github.com/gptme/gptme （★4455，MIT，Python）
描述：Your agent in your terminal, equipped with local tools: writes code, uses the terminal, browses the web.

## 设计点 1：Provider-agnostic 的模型适配层
README 强调 "Provider-agnostic, local-first"：同一套终端 agent 可对接 Anthropic、OpenAI、Google、DeepSeek、OpenRouter，
以及 Ollama 等任意 OpenAI-compatible server。做法是收敛到一个统一的 chat/completions 抽象，
换模型只改配置而不是改调用点（与 AgentOS 的 /v1/models 探活 + 现场换模型思路一致）。

## 设计点 2：离散可调用工具面 + 沙箱执行 + guardrails
能力被拆成 shell、Python、web 浏览、vision、编辑（hashline patch / morph）等"工具"，
由 agent 以工具调用方式编排，而不是把一切塞进一段脚本。
同时提供沙箱化执行（Docker / Wasmtime）与 guardrails（权限/审批）——工具面越宽，越需要执行边界。

## 设计点 3：纯文件式本地记忆与可扩展性
状态全部落在工作区的普通文件里：skills / lessons / plugins 作为上下文被加载，
外加 MCP/ACP 集成与 `gptme-util memory` 跨 harness 记忆。
好处是 agent 状态可 diff、可 git 管理、可迁移，不依赖中心服务（local-first），
并且扩展靠加文件（插件/技能）而非改核心代码。
