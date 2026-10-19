# study8 · GitHub 成熟实现蒸馏

来源仓库：gptme/gptme ★4455 · Python · MIT · https://github.com/gptme/gptme
（README 前 30 行要点：个人 AI agent，跑在任何有终端的地方；provider-agnostic、local-first、unconstrained）

## 设计点 1：Provider 无关的本地优先 LLM 层
- 一套统一的模型抽象，同时接 Anthropic / OpenAI / Google / DeepSeek / OpenRouter / Ollama 及任意
  OpenAI 兼容 server，用户可随时换模型而不改 agent 逻辑（“your data, your models, your terminal”）。
- 落地要点：把 provider 选择收敛到一个工厂/配置层（模型字符串 → provider 适配器），
  agent 主循环只依赖统一接口 `chat(messages, tools) -> Response`；密钥与 base_url 走环境变量注入。

## 设计点 2：能力=工具，扩展=插件/技能/lessons，安全靠 Guardrails
- 核心 agent 能力全部以“工具”形式暴露（shell、python、web、vision 等），主循环按 LLM 的 tool_call 分发执行。
- 生态层：plugin 系统 + skills（可作 slash command）+ lessons（上下文注入的经验规则），且提供 Sandbox 执行
  （Docker / Wasmtime）与 guardrails 作为安全边界；这也正是本仓库 subagent/经验规则的同类设计。

## 对本仓库的启发
- CLI 入口保持极薄：解析参数 → 组装 provider → 跑 loop；能力都放工具里。
- 输出与退出码要可脚本化（非交互模式下用 exit taxonomy 区分成功/失败原因）。
