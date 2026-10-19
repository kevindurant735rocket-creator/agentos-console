# GitHub 成熟实现研究 #3（重做轮）：gptme/gptme

> 本文件是重做轮的研究。上一轮（vstorm-co/pydantic-deepagents）原文保留在
> `work/distill/gh_deploy_study3.prev-pydantic-deepagents.md`，不覆盖、不丢证据。

- 检索方式：`gh_search(kind=repos)`
- 首查 `terminal agent python` → 仅 1 命中且 ★2（不合格，弃用）
- 改查 `coding agent CLI python stars:>1000` → 8 命中，全部 ≥1000 星：
  `teng-lin/notebooklm-py` ★19799、`gptme/gptme` ★4456、`evalstate/fast-agent` ★3943、
  `cocoindex-io/cocoindex-code` ★2927、`redhat-et/ripwire` ★2458、`he-yufeng/CoreCoder` ★1849、
  `awslabs/cli-agent-orchestrator` ★1453、`vstorm-co/pydantic-deepagents` ★1103
- 选中：**`gptme/gptme` ★4456 · Python · MIT**
  描述："Your agent in your terminal, equipped with local tools: writes code, uses the
  terminal, browses the web. Make your own persistent autonomous agent on top!"
- 为什么最相关：它是「终端里的 agent」本体（CLI/TUI/headless/RPC 多前端同一 harness），
  和 AgentOS console 的形态（终端 agent + tools/ 工具面 + 闸门 + 自检）几乎同构；
  notebooklm-py / cocoindex-code / ripwire 是领域工具，fast-agent / pydantic-deepagents
  是框架，cli-agent-orchestrator 是编排器，都偏一层。
- 读取证据：
  - `gh_repo_view(repo="gptme/gptme")` → star 4456 / MIT / Python / updatedAt 2026-10-19
  - `gh_file_read(repo="gptme/gptme", path="README.md")` 前 60 行（下表事实 1、2）
  - `gh_read(["api","repos/gptme/gptme/contents/gptme/cli?ref=master","--jq",".[].name"])`
    → cli/ 下有 `cmd_capabilities.py`、`cmd_hooks.py`、`cmd_skills.py`、`main.py`、`doctor.py` 等 36 项
  - `gh_file_read(path="gptme/constants.py")` → 无退出码常量（说明在 cli/main.py）
  - `gh_read(["search","code","taxonomy","--repo","gptme/gptme","--json","path"])`
    → 命中 `tests/test_cli_fatal_error_envelope.py`、`docs/releases/v0.33.0.md`
  - `gh_file_read(path="tests/test_cli_fatal_error_envelope.py")` → 全部退出码数值与分类规则

## 从 README / 源码观察到的事实

1. 定位是**「anywhere a terminal runs」**：laptop / ssh / tmux / headless / CI pipeline 同一套
   agent；provider-agnostic（Anthropic/OpenAI/Google/DeepSeek/OpenRouter/**Ollama 本地**），
   与本仓 `agent_core.py` 的「主端点 → 本地离线后端回退」是同一思路。
2. README News 里 v0.33.0 明确写 **"sandboxed Python/shell execution (Docker, Wasmtime),
   non-interactive exit taxonomy"**；v0.34.0 写 **"skills as slash commands"**、
   `gptme service init` 给 headless agent 起服务。
3. 退出码分类学是真的落了代码，不是文档口号
   （`tests/test_cli_fatal_error_envelope.py` 的测试矩阵，逐条复述）：
   - `EXIT_RATE_LIMIT = 75` ← 429 / `RateLimitError` / `usage_limit_reached`
   - `EXIT_AUTH_ERROR = 76` ← `AuthenticationError` / 消息含 401
   - `EXIT_MODEL_UNAVAIL = 77` ← 503 / `NotFoundError` / `ServiceUnavailableError`
   - 其余（含 `InternalServerError` 500/502/504、`RuntimeError`）→ **generic = 1**
   - 分类器是**纯函数** `_classify_fatal_error(exc) -> (error_class, exit_code)`，
     输入只有异常类型名与消息文本；**判断不出就降级 generic 1，不硬猜某个具体类**。
   - 除退出码外还落一条**机器可读错误事件**到 `conversation.jsonl`：
     `role=system` + `metadata.error=True` + `metadata.error_class` + `metadata.exit_code`
     （`_write_terminal_error_to_log`），且 logdir 不存在时**静默不抛**。
4. `gptme/eval/suites/*.py` 里普遍出现 `exit_code`：eval 判通过认的是**真实命令退出码**，
   不是模型自评。
5. 能力面是显式命令（`cmd_capabilities.py`）而非隐式默认；skills / hooks / mcp 各自独立命令族。

## 蒸馏 3 条可借鉴设计点（映射到 AgentOS）

### D1. 失败要「分类学 + 机器可读痕迹」，不是一律 rc=1
gptme 用 75/76/77/1 把「限额 / 鉴权 / 模型不可用 / 其他」拆开，让 headless 与 CI 能按类
决定重试、换 key 还是告警；同时把 `error_class` 与 `exit_code` 写进会话 JSONL，事后可对账。
**映射**：本仓 `agent_core.py` 内部有 `kind=provider/usage/limit/interrupted`，但 tools/ 下的
脚本/闸门大量 `sys.exit(1)` 一把梭——外部只看到「非 0」，分不清是判据没过还是参数错。
**落地**：`tools/exitcap/exitcap.py` —— 退出码分类学常量表 + 纯函数 `classify_fatal()` +
扫描器 `scan_source()/scan_dir()`，报告哪些脚本用了未登记退出码（rc=3），并输出 JSON。

### D2. 分类器必须是纯函数、无副作用、判不准就降级
`_classify_fatal_error` 只吃 `(异常类型名, 消息)`，返回 `(class, code)`；order 固定，
兜底 generic。**没有 IO、没有猜、没有「看起来像」**。
**映射**：本仓铁律是「证据是工具结果里的 rc」；分类器同样应可被单测逐条钉死。
**落地**：`classify_fatal()` 全部输入输出为 `str -> (str, int)`，`--selftest` 用 gptme 测试矩阵
的 11 条同款用例（含 `InternalServerError→1` 这条「不该被误分类」的反例）。

### D3. 错误痕迹要落盘且「落盘失败不放大事故」
`_write_terminal_error_to_log` 在 logdir 缺失时**静默返回**——错误上报是旁路，不能因为上
报失败把原事故变成崩溃。
**映射**：本仓 `audit` 走的是 fail-closed（落盘失败就别执行写），方向相反但同理：
**旁路日志容错，主流程判据 fail-closed**，两者不能混。
**落地**：`exitcap.py --json PATH` 落盘失败只打印 warn 并保留 rc 语义（退出码永远是主结论）。

## 本次落地

- 工具：`tools/exitcap/exitcap.py`（纯 stdlib，`--selftest` 带变异体验牙）
- 主要借鉴点：**D1（退出码分类学）**，并用 D2 的纯函数形态实现、D3 的旁路落盘容错
- 部署：`tools/exitcap/` → 独立 public 仓库，README 与仓库描述均注明 derived from gptme

## 参考

- https://github.com/gptme/gptme （★4456, MIT, Python）
- https://github.com/gptme/gptme/blob/master/tests/test_cli_fatal_error_envelope.py
- 同批命中（未采用）：notebooklm-py ★19799、fast-agent ★3943、cocoindex-code ★2927、
  ripwire ★2458、CoreCoder ★1849、cli-agent-orchestrator ★1453、pydantic-deepagents ★1103
