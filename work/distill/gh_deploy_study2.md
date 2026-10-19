# GitHub 成熟实现研究 #2：Aider-AI/aider

- 检索方式：`gh_search(kind=repos)`
- 命中：`Aider-AI/aider` ★49630 · Python · Apache-2.0 · "aider is AI pair programming in your terminal"
- 同类性：终端 agent + 本地工具（文件编辑、shell 执行、git、lint/test），与 AgentOS 定位一致。
- 读取：`gh_file_read(repo="Aider-AI/aider", path="README.md")` 前 80 行

## 从 README 观察到的事实

1. 安装走"引导器 → 具体工具"两段式：`pip install aider-install` 然后 `aider-install`，再 `aider ...` 启动。安装/自举与日常使用解耦。
2. 模型可插拔：`--model deepseek` / `--model sonnet` / `--model o3-mini`，同时支持本地模型；模型只是参数，不是硬编码分支。
3. 仓库地图（repomap）：对整个代码库建"地图"以在大型项目里工作，而不是每次全量塞上下文。
4. git 集成内建：自动提交 + 有意义的 commit message，用户可用熟悉的 git 工具 diff / undo AI 改动——即"AI 改动可回滚"是一等公民。
5. lint & test 自动执行：每次改动后自动跑测试/lint，并能修复被报告的问题——形成"改 → 验 → 修"的闭环。
6. 输出/输入通道多：IDE 内使用（watch 注释）、图片与网页、语音、复制粘贴到 web chat。终端只是主通道之一，核心是可复用的会话层。

## 蒸馏 3 条可借鉴设计点（映射到 AgentOS）

### D1. 改动必须可回滚：把 VCS 当作安全网，而不是可选插件
Aider 每次编辑后自动 commit，用户随时用 git 回滚。AgentOS 的写面（write_file / edit_file）目前依赖外层审批矩阵，但没有"一键回到编辑前"的一等语义。
**落地**：CLI 骨架里提供 `snapshot` 与 `undo` 子命令；每次写操作前后打快照，`undo` 回退到上一个快照。审计轨迹与回滚能力绑定。

### D2. 验证闭环内建：改完必须跑验证，并允许"看到失败再修"
Aider 在编辑后自动 lint/test，并能根据报错继续修。
**落地**：CLI 骨架里提供 `verify` 子命令，按配置的命令列表顺序执行、收集真实 rc，任一失败即整体失败并以非零码退出——即 verification-before-completion 的机器化版本，禁止"没跑就说通过"。

### D3. 模型/后端可插拔 + 项目级上下文档（repomap 思路）
Aider 用 `--model` 切模型、用 repomap 控制上下文规模。
**落地**：CLI 骨架里模型通过 `--model` / 环境变量注入，业务代码不写死分支；同时提供 `map` 子命令，扫描项目生成受限规模的"文件地图"（路径 + 行数 + 首行摘要），作为给模型的上下文锚点，避免全量塞入。

## 参考

- https://github.com/Aider-AI/aider （★49630, Apache-2.0）
- 衍生骨架：`work/distill/agentos_study_cli2/`（derived from Aider-AI/aider）
