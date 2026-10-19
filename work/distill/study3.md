# GitHub 参考实现蒸馏 · study3

## 选型
- 搜索命令：`gh_search kind=repos`，query 依次为 `python terminal agent`、`AI coding agent CLI terminal stars:>1000 language:Python`、`code interpreter`
- 候选（1000+ star）：
  - `QwenLM/Qwen-Agent` ★17191 · Python · Apache-2.0 —— **选中**
  - `e2b-dev/code-interpreter` ★2419 · Python · Apache-2.0（沙箱执行 SDK，非终端 agent 本体）
  - `QwenLM/qwen-code` ★28574 —— TypeScript，语言不符，排除
  - `plandex-ai/plandex` ★15765 —— Go，排除
- 选中理由：`Qwen-Agent` 是 Python 写的 agent 框架，主打 **Function Calling + MCP + Code Interpreter（本地工具执行）**，与 AgentOS「终端 agent + 本地工具（read_file / run_cmd / search）」同构度最高。

## gh_repo_view 概况（原文数字）
- repo: `QwenLM/Qwen-Agent`
- star: 17191
- language: Python
- license: Apache License 2.0
- updatedAt: 2026-10-19T10:11:41Z
- description: Agent framework and applications built upon Qwen>=3.0, featuring Function Calling, MCP, Code Interpreter, RAG, Chrome extension, etc.

## README 前 60 行要点（摘录）
1. 定位：framework for developing LLM applications based on **instruction following, tool usage, planning, and memory**；自带 Browser Assistant、Code Interpreter、Custom Assistant 等示例应用。
2. 安装即「最小核心 + 可选能力包」：
   ```bash
   pip install -U "qwen-agent[gui,rag,code_interpreter,mcp]"
   # 可选 extra：[gui] Gradio GUI / [rag] RAG / [code_interpreter] / [mcp]
   # 只装 pip install -U qwen-agent 即最小依赖
   ```
3. 分层抽象：**LLMs（class BaseChatModel，自带 function calling）、Tools（class BaseTool）、Agents（class Agent）** 三级原子组件。
4. 自定义工具 = 装饰器注册 + 类内声明式元数据：
   ```py
   @register_tool('my_image_gen')
   class MyImageGen(BaseTool):
       description = '...'          # 给 LLM 看的能力说明
       parameters = [{'name': 'prompt', 'type': 'string', 'description': '...', 'required': True}]
       def call(self, params: str, **kwargs) -> str: ...
   ```
5. 明确的安全声明：「the python executor is **not sandboxed** and is intended for local testing only, not for production use」，以及各模型/网关的 tool-call 解析差异（`--enable-auto-tool-choice`、`--tool-call-parser hermes`、`use_raw_api`）需按后端显式配置。

## 蒸馏：可借鉴到 AgentOS 的 3 条设计点
### 1. 三级原子组件分层：Model / Tool / Agent 各自可替换
AgentOS 把聊天模型、工具、会话编排拆成独立可插拔层；工具层只关心 `name/description/schema/call`，编排层只按注册表调度，换模型或加工具都不动另一层。借鉴 `BaseChatModel / BaseTool / Agent` 三件套。

### 2. 工具自描述 + 装饰器注册表（声明式契约）
工具在被 import 时通过 `@register_tool('name')` 注册进全局注册表，并自带 `description` + `parameters` 结构；框架据此生成 function-call 提示、做参数校验、自动产出 `--help`/文档。AgentOS 的 `read_file` / `run_cmd` / `search` 应同样以「类 + schema 声明」形式注册，而不是散落的 if-else。

### 3. 最小核心 + 能力分组的 optional extras，且对未沙箱能力显式告警
`pip install qwen-agent` 只安最小依赖，`[gui]/[rag]/[code_interpreter]/[mcp]` 按需装；同时 README 用醒目标注说明「python executor 未沙箱化，仅本地测试」。AgentOS 侧对应：`pyproject.toml` 里用 `optional-dependencies` 定义能力组；凡是会执行任意命令/代码的能力，必须在 CLI 启动横幅与工具描述里写明「未沙箱」风险等级，而不是静默放行。

## 引用来源
- https://github.com/QwenLM/Qwen-Agent （Apache-2.0）

---

# Round 2 · gptme 蒸馏（2026-10 新搜）

## 选型
- 搜索：`gh_search kind=repos`，query 依次 `python terminal agent CLI local tools`(0 命中)、`terminal agent`(12 命中)、`gptme`(12 命中)
- 候选（1000+ star，Python）：`gptme/gptme` ★4455 · Python · MIT —— **选中**
- 排除：`smtg-ai/claude-squad` ★8624（Go）、`shroominic/codeinterpreter-api` ★3844（库，非终端 agent 本体）
- 选中理由：描述即「Your agent in your terminal, equipped with **local tools**: writes code, uses the terminal, browses the web」——与 AgentOS「终端 agent + 本地工具」同构度最高，且 Python + MIT。

## gh_repo_view 概况（原文数字）
- repo: `gptme/gptme` ｜ star: 4455 ｜ language: Python ｜ license: MIT License
- updatedAt: 2026-10-19T11:23:25Z

## README 前 60 行要点（摘录）
1. 「runs anywhere a terminal runs — laptop, ssh, tmux, headless server, CI」；provider-agnostic、local-first，同时提供 `gptme` CLI 与 `gptme-tui`。
2. 版本演进线强调工程化能力：非交互退出码分类（non-interactive **exit taxonomy**）、沙箱化 Python/shell 执行（Docker、Wasmtime）、后台任务、成本追踪、上下文压缩、lessons/skills 作为上下文注入。
3. `pyproject.toml` 里 20+ 个 `[project.scripts]` 入口（`gptme`、`gptme-tui`、`gptme-server`、`gptme-doctor`、`gptme-util`…）+ `[project.entry-points."gptme.plugins"]` 声明插件；`optional-dependencies` 分组：`minimal`(零原生扩展，可跑 ARM/musl/Termux/Lambda)、`all`、`tui`、`server`、`sandbox`、`browser`…

## 蒸馏：可借鉴到 AgentOS 的 3 条设计点
### 1. 工具/插件走 entry-point 注册表 + 显式启停，而不是硬编码 if-else
gptme 用 `[project.entry-points."gptme.plugins"]`（如 `auto_snapshots = "gptme.hooks.auto_snapshots:plugin"`）把扩展挂在打包元数据上，第三方包装好即被发现；工具集在 `gptme/tools/*` 各自独立模块，由 CLI 选择加载。
→ AgentOS：`run_cmd`/`read_file`/`search` 每个工具声明 `name/description/schema/risk` + `@register_tool`，并用 `importlib.metadata.entry_points(group="agentos.tools")` 发现第三方工具；`agentos tools` 子命令可列/启停，避免上下文里塞满不用的工具描述。

### 2. 单一 CLI 入口 + 多条前端共享核心，且非交互模式有明确退出码分类
gptme 的 `gptme` / `gptme-tui` / `gptme-server` / `gptme-acp` 全部薄封装同一个 core；非交互运行（脚本/CI）通过 exit taxonomy 区分「模型失败 / 工具失败 / 预算耗尽 / 用户中断」。
→ AgentOS：`main.py` 只做参数解析与退出码映射，核心 `run_once()` 与 REPL 共用；定义 `ExitCode` 枚举（OK=0、USAGE=2、MODEL_ERR=3、TOOL_ERR=4、BUDGET=5、INTERRUPT=130），让上层自动化能按码分支，而不是解析 stdout。

### 3. extras 分层安装 + 沙箱是「可切换升级路径」而非默认假设
gptme 有 `minimal` extra（无原生扩展，headless/ARM 可装），高危执行能力单列 `sandbox`(wasmtime)/docker 后端并通过 `GPTME_SANDBOX` 环境变量切换；README 明说默认本机执行。
→ AgentOS：`pyproject.toml` 用 `optional-dependencies` 分 `[tui]/[server]/[sandbox]`，基础安装零重依赖；工具描述里携带 `risk` 等级，`--sandbox=none|docker|wasm` 显式选择，启动横幅打印当前风险档位。

## 引用来源
- https://github.com/gptme/gptme （MIT）
