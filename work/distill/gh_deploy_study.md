# GitHub 成熟实现蒸馏：gptme/gptme

- 搜索方式：`gh_search(kind=repos)`，query = `python cli terminal agent language:python stars:>1000`
- 候选（节选，star 为搜索返回的真实值）：
  - **gptme/gptme ★4456** — Your agent in your terminal, equipped with local tools
  - tconbeer/harlequin ★6472（SQL IDE，非 agent）
  - vstorm-co/pydantic-deepagents ★1103
  - massgen/MassGen ★1141
  - OpenOSINT/OpenOSINT ★1777
  - Harvil1/codeAgent ★995（未达 1000 门槛）
- 选定：**gptme/gptme**（唯一同时满足 "Python + terminal/agent + ★>1000 + 与 AgentOS 同类" 的仓库）
- 概况（`gh_repo_view`）：★4456 · MIT · Python · 描述 "Your agent in your terminal, equipped with local tools:
  writes code, uses the terminal, browses the web. Make your own persistent autonomous agent on top!"
- 证据来源：`gh_file_read(repo=gptme/gptme, path=README.md)`，读取内容至 Demos 段（约前 170 行），
  其余结论取自该 README 内嵌的 News/Changelog 条目（v0.28–v0.34）。

---

## 3 条可借鉴到 AgentOS 的设计点

### 1. 能力面 = "工具 + 技能 + 经验" 三层，经验层可增量注入
gptme 把扩展性拆成三档：**Plugins**（打包分发）、**Skills**（按需装载的能力包）、**Lessons**（v0.29
引入的"上下文教训"，针对某类任务在提示里注入一段经验）。v0.34 起 skills 直接暴露成 slash commands。

对 AgentOS 的映射：本仓库已有 `skills/` 与"自进化经验规则（ACTIVE 晋升）"，但那是全局注入。
可借鉴 gptme 的做法——经验带**触发条件**（哪类任务/哪类工具命中才注入），而不是每次会话无条件贴到
system prompt；并且让经验可被 slash command 直接点名调用，便于人工复现与回归验证。

### 2. 非交互场景要有"退出码分类学"，而不是笼统的 0/1
v0.33 明确列出 *non-interactive exit taxonomy*，同版本还加入 sandboxed Python/shell 执行
（Docker、Wasmtime）与 server auth hardening。要点是：把"模型放弃/工具失败/预算耗尽/被策略拒绝/
需要人审批"编码成不同的退出码与结构化输出，让 shell 脚本和 CI 能做分支判断。

对 AgentOS 的映射：我们所有 gate（cli_gate / act_gate / autonomy_gate 等）都靠 JSON 判定，
但 `run_cmd` 层面人机可读的失败原因仍混在 rc 与文本里。可引入一层"退出码 + 机器可读 reason 枚举"，
让外围脚本（install.sh、rollback.sh、CI）不必解析自然语言。

### 3. 长会话的三件套：上下文压缩 + token 感知 + 成本/内容寻址存储
v0.30 加 *context compression* 与 *subagent planner mode*；v0.29 加 *token awareness*；
v0.31 加 *background jobs / cost tracking / content-addressable storage*；v0.34 加 *context-scout
pre-pass*（回答前先做一次检索预扫）。

对 AgentOS 的映射：本仓库已有 subagent 与 jsonl 会话日志（`work/agent_sessions/`、`work/steer/`）。
可直接借鉴两点：(a) 日志按内容寻址/去重，避免同一大段工具输出在多次会话里重复膨胀（我们有大量
`*.json` gate 产物，体量已达数百 KB）；(b) 做事前先跑一次轻量检索预扫（对应现有 `search`/`gh_search`
能力），用检索结果裁剪注入上下文，而不是全量贴文件。

---

## 附带观察（不进 3 条主线，供部署参考）
- gptme 自己就是一个"可被脚本化调用"的 CLI（`gptme` + `gptme-util` 双子命令 + `gptme service init`
  做无头守护进程）。AgentOS 若要对外发布，**CLI 与常驻服务共用同一实现**比现在分两套更容易被外部集成。
- 其插件有**中心化 registry**（gptme-plugin-registry）做发现，降低了插件生态的碎片化。

## 未做/边界
- 本次仅只读访问 GitHub（`gh_search` / `gh_repo_view` / `gh_file_read`），未执行任何部署动作；
  未调用 `gh_deploy`。
- README 只读至 Demos 段（`gh_file_read` 返回内容在此处截止），Guardrails / Usage 等后段章节未逐字核对，
  相关结论均来自 README News 条目而非源码。
