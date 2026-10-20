# AgentOS 终端 Agent

**只有终端面。** 网页驾驶舱与原生 macOS app 已于 2026-10-20 下线并删除
（连同它们量的 30+ 道门）。现在这个仓库就是一件事：**一个能真正干完活的
终端 agent** —— 真模型 + 真工具循环 + 可判定的完成契约 + 会自己进化的内核。

```bash
python3 tools/agentos.py agent            # 进终端 agent（REPL）
python3 tools/agentos.py agent -p '需求'   # headless 一次跑完，退出码可接 CI
bash install.sh                            # 把 agentos 装到 PATH（只装 CLI shim）
```

## 新机快速上手

```bash
git clone https://github.com/kevindurant735rocket-creator/agentos-console
cd agentos-console
python3 tools/agentos.py config set llm.provider opencode
python3 tools/agentos.py config set llm.api_key <你的key>   # 缺它必 401
python3 tools/agentos.py doctor                        # 一键体检（配置/key/端点/门锁）
python3 tools/agentos.py agent -p 'ping 一下自己'      # 第一次真模型对话
bash install.sh                                        # 可选：把 agentos 装进 PATH
```

配置都在 `~/.agentos/config.toml`（[llm] provider/model/api_key/no_fallback、
[gh] write_enabled、[agent] full_access）。`no_fallback="true"` = 云端端点失败只重试
主端点、绝不静默换本地模型。

## 收敛引擎与自进化旋钮（W23–W24 新增）

内核带收敛引擎（`tools/converge.py`），治的是**"探查很多、交付为零"**：

| 机制 | 判据 | 动作 |
|---|---|---|
| 阶段机 | 工具语义（探查/动手/验证） | 到边界注入方向指令 |
| 循环熔断 | 同 (工具, 参数指纹) 重复 ≥3 次 | 注入"换一条完全不同的路径" |
| 停滞探测 | 连续 N 步无产出（写盘成功 / rc=0） | 注入"立刻动手，或立刻交付并说清卡点" |
| 交付闸 | 终答缺证据 | 退回补证据（**不烧用户 step 预算**） |

**证据判据只认证据不认措辞**：模型说"完成了"不算证据，`write_file` 成功 + `run_cmd rc=0` 才算；
诚实报告失败（"写盘被拒，做不到"）是**合格终答**，不退回。

**自进化旋钮（W24）**：ACTIVE 规则不再只是往 prompt 里塞提示文字，而是能真拧行为旋钮。
引擎把打转分类计数（停滞/循环/只探查/改了没验/闸退回）写进信号账 → 挖掘器聚成规则 →
规则带 `action.op=converge_tune` → 晋升 ACTIVE 后，**下一轮引擎阈值自动收紧**（只许收紧、
下限 2、放宽被钳住）。

实测：连续 3 轮真实停滞 → 挖出 `evo-converge-stall`(count=3) → 晋升 ACTIVE →
下一轮快照 `max_stall 6→4`、`max_explore 6→4`。**agent 用自己的失败数据改了自己的行为。**

## 回放基准：证明「自进化真的改了行为」（W24 新增）

自进化闭环此前只能证明「规则合入了、门是绿的」——**证明不了行为变好了**。
`agentos replay` 把真实跑过的会话语料（`work/replay_corpus.jsonl`，每条带 note
标注源自哪次真实会话）用桩原样重放，**引擎开/关做 A/B**，用交付闸自己的
判据量「空口完成」（说完成了但全程零证据 = 2026-10-19 那次 23 步 900 秒事故的形状）。

实测 A/B（3 条真实语料）：

| | 交付 | **空口完成** | 干预 | 闸退回 |
|---|---|---|---|---|
| 引擎开 | 3/3 | **0** | 2 | 1 |
| 引擎关 | 3/3 | **1** | 0 | 0 |

差值 = **避免 1 条空口完成**。`replay_gate` 把它钉进门链，并带一个变异体：
两侧都强制关掉引擎 → 分辨力归零 → 门判红（证明语料不是摆设）。

## 任务契约：不同类型的任务，要不同的「完成证据」（W26 新增）

收敛引擎管的是「别绕路、别空口交付」；**任务契约**管的是「**这类任务的
完成证据是什么**」——一份通用证据要求（"跑过一条命令"）对每种任务都太松或太紧。

| 任务类型 | 硬要求（缺了不算完成） | 开工指令 |
|---|---|---|
| 缺陷修复 | 改完通过核对 | **先复现（看到红）→ 再改 → 再验（看到绿）** |
| 新增功能 | 跑过验证 / 写下并读回 | 写完必须证明新代码被执行到 |
| 重构 | 跑过验证 | 改前跑一次记基线，改后对比（行为不变） |
| 文档 | **写下并读回** | 落盘 + 内容对，命令路径要能核对 |
| 调研 | 结论有可核对来源 | 每条结论带路径:行号 / URL |
| 分析/盘点 | 结论来自跑出来的数 | 不许凭印象报数 |
| 操作/部署 | 跑过命令（状态可见） | 做完后跑一次确认命令 |

三条设计规矩：
1. **类型判定「最先出现的动作主导」**（「部署…并写好说明文档」是 ops 不是
   docs），且**必带理由**可复查；宁可判 generic 也不给错契约。
2. **写操作被环境锁住时撤掉写类要求**（plan 只读 / DSP 计划未审批）——
   要求一份够不着的证据，会让任务永远交不了付。
3. **判据只认证据不认措辞**：终答里写「已修复」不算；`write_file` 成功 +
   `run_cmd rc=0` 才算。红→绿闭环的三个状态（`repro_fail_seen` /
   `fix_after_repro` / `repro_green`）都进账本。

真机实测（缺陷修复）：`ls → 读 calc.py → 读 Makefile → make test（红）→
改 → make test（绿）`，账本 `repro_fail_seen=True / repro_green=True /
gate_retries=0`（一次放行，因为证据是真的）。

## 端点连不上 → 一直等（不放弃、不换本地模型）

`llm.no_fallback="true"` 时，端点**连不上**（传输层不通 / 网关过载 / 限流 /
DNS 失败 / EOF / 无响应）会**一直重试到端点恢复**：

```
端点连不上：模型端点不可达：[SSL: UNEXPECTED_EOF_WHILE_READING] …
  → 第 1 次重试，2s 后再试；**一直等到端点恢复**（不会改用本地模型；Ctrl-C 中断）
```

- 退避 2s→4s→…→封顶 30s，每轮检查 Esc/Ctrl-C 打断；时间不设上限。
- 预算可调：`llm.endpoint_retry_max`（**0 = 一直等**，>0 = 试 N 次）。
- **401/403/400 不在这一族**：key/协议问题等一辈子也不会好，还会把真病因
  藏起来 —— 那种**立刻报错 + 给处方**（探活 / 换 key / 查网关路由）。
- `agentos doctor` 会显式打出当前口径：
  `no_fallback=true（绝不换本地模型）· 端点连不上时一直重试到端点恢复`。

## 过程显示：Claude Code 式（W28）

工作过程**一次工具一行**，行上是**短标签**（不是完整 JSON）：

```
⏺ Write(hello.txt)
  ⎿ hello.txt · 写入 2 行 / 3 字节
⏺ Read(hello.txt)
  ⎿ 失败：路径不存在          ← 失败说原因
```

- 短标签：`Write(path)` / `Read(path)` / `Bash(cmd)` / `Grep(pattern)` /
  `Search(query)` / `Repo(slug)` / `Agent(prompt)` —— **只留一个关键参数**，
  文件正文绝不上屏；太长就截断（留尾巴，文件名/命令尾部才是有用信息）。
- 交互面**不再**多打一行 `[工具 X → ok]`（headless / `-p --stream` 的输出形状
  一字不动 —— 那是给 CI 解析用的）。
- 欢迎框 13 行 → **3 行**；状态行一条：`3 步 13.3s · 工具 2/2 · 本轮 ↑3327 ↓40 tok`。
- 终答要求**简短**（默认 3-6 行）：不写成长报告、不加粗小标题、不复述过程。
