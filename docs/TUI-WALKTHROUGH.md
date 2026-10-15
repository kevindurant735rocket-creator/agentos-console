# TUI 走查文档（`agentos tui`）

本文所有数字都是本回合在
`/Users/zhangfengrui/Documents/Codex/2026-10-01/agentos-console`
真跑出来的，每条都带 `文件:行号` 或「命令 + rc」。全篇零 ANSI 转义字节。
注意：`tools/agentos.py` 仍被并发编辑，本文行号是 2026-10-10 本次核验快照；
若与盘上对不上，以 `tools/agentos.py` 当行为准。

## 1. 这是什么 · 给谁看 · 怎么起

- 对象：`python3 tools/agentos.py tui` —— AgentOS Console 的终端交互主界面，
  共 13 个视图（面），每面走真 8790 服务的只读数据。
- 给谁看：接手本项目的人、要改 TUI 的人、要复核「13 视图口径一致」的人。
- 怎么起（有 TTY 的终端里）：

  ```
  python3 tools/agentos.py tui
  ```

  非 TTY（管道 / CI / 自动化）时自动走 `cmd_tui`（`tools/agentos.py:2779`），
  一次性打印 13 段真数据后退出，不进 raw 模式。
- 本回合实测启动：
  `python3 tools/agentos.py tui > /tmp/w22_tui.txt 2>&1` → rc=0；
  `grep -c '^== ' /tmp/w22_tui.txt` = 13（13 个视图段）；
  `grep -c $'\x1b' /tmp/w22_tui.txt` = 0（非 TTY 路径零 ANSI 字节）。
- 8790 健康：`curl -s -m 3 http://127.0.0.1:8790/__health` → HTTP 200，
  返回 `{"ok": true, ...}`（本次 `HEALTH_RC=200`）。

## 2. 13 视图总览

视图名单真源：`tools/agentos.py:60-61` 的 `VIEWS` 元组（13 项）。

| # | 视图 | tab id | TUI 直达键 | 取数入口（tools/agentos.py） |
|---|---|---|---|---|
| 1 | 总览 | `over` | `1` | `load_overview` :1284 |
| 2 | 轮次 | `round` | `2` | `load_rounds` :1329 |
| 3 | 阶段矩阵 | `mx` | `3` | `load_matrix` :1347 |
| 4 | 证据账本 | `led` | `4` | `load_ledger` :1364 |
| 5 | 引擎 | `eng` | `5` | `load_engine` :1433 |
| 6 | Agent 台 | `work` | `6` | `load_agent` :2284 |
| 7 | 目标台 | `goal` | `7` | `load_goal` :1467 |
| 8 | 复盘台 | `retro` | `8` | `load_retro` :1519 |
| 9 | 能力地图 | `cap` | `9` | `load_cap` :1567 |
| 10 | 工作区 | `ws` | `0` | `load_ws` :1611（`_ws_dirs` :1603） |
| 11 | 终端 | `term` | 无数字键，Tab | `load_term` :1828 |
| 12 | 文件 | `file` | 无数字键，Tab | `load_file` :1871 |
| 13 | 设置 | `set` | 无数字键，Tab | `load_set` :1903 |

取数函数在 `LOADERS` 元组里按同一顺序登记（`tools/agentos.py:2294-2296`），
并有 `assert len(LOADERS) == len(VIEWS)`（`tools/agentos.py:2301-2303`）兜底。

## 3. 逐视图

下面每节的「实测」行取自本轮门写出的 `work/tui_walk.json`（`elapsed`=7.81s、
`child_rc`=0、`reader_at`=AT-UNVERIFIED），字段是该面 `body_lines` 与 `first_body`。

### 3.1 总览（`over`，直达键 `1`）

- 展示什么：8790 服务健康摘要，首行给健康裁决。
- 数据来源：`load_overview`（`tools/agentos.py:1284`），取 `GET /__health`
  （`tools/agentos.py:1285`）。
- 实测：body 15 行；首行 `> 健康                 ok`。

### 3.2 轮次（`round`，直达键 `2`）

- 展示什么：目标链当前轮次与目标文本。
- 数据来源：`load_rounds`（`tools/agentos.py:1329`），经
  `fetch_goal_json`（`tools/agentos.py:1307`）打
  `POST /__goal/chain op=status`（`tools/agentos.py:1313`）。
- 实测：body 11 行；首行 `> 目标                 构建 AgentOS 根源自我进化...`。

### 3.3 阶段矩阵（`mx`，直达键 `3`）

- 展示什么：矩阵规模与轮次目标的阶段分解。
- 数据来源：`load_matrix`（`tools/agentos.py:1347`），读 `work/GOAL.json`
  的只读面（服务端降级路径）。
- 实测：body 11 行；首行 `> 矩阵规模             8 行 × 轮次目标`。

### 3.4 证据账本（`led`，直达键 `4`）

- 展示什么：证据载荷时间、目录与分歧项。
- 数据来源：`load_ledger`（`tools/agentos.py:1364`），取
  `GET /__fs?op=read&path=data.json`。
- 实测：body 21 行；首行 `  载荷时间             2026-10-01T15:31:13...`。

### 3.5 引擎（`eng`，直达键 `5`）

- 展示什么：全局裁决与推荐动作。
- 数据来源：`load_engine`（`tools/agentos.py:1433`），取 `GET /__health`
  （`tools/agentos.py:1434`）与 `GET /__act/recommend`（`tools/agentos.py:1438`）。
- 实测：body 7 行；首行 `> 全局裁决             PASS`。

### 3.6 Agent 台（`work`，直达键 `6`）

- 展示什么：工具调用卡片与运行态；动作只认服务端 allowlist。
- 数据来源：`load_agent`（`tools/agentos.py:2284`），取 `GET /__health`、
  `GET /__act` 的 `__list`（`tools/agentos.py:1991`）与
  `GET /__act/recommend`（`tools/agentos.py:1997`）。
- 实测：body 22 行；首行
  `■ 工具调用卡片  动作只认服务端 allowlist · rc 为唯一裁决`。

### 3.7 目标台（`goal`，直达键 `7`）

- 展示什么：目标链状态明细。
- 数据来源：`load_goal`（`tools/agentos.py:1467`），打
  `POST /__goal/chain op=status`（同轮次面入口）。
- 实测：body 21 行；首行 `> 目标                 构建 AgentOS 根源自我进化...`。

### 3.8 复盘台（`retro`，直达键 `8`）

- 展示什么：复发簇统计与阈值。
- 数据来源：`load_retro`（`tools/agentos.py:1519`），取
  `GET /__recurrence`（`tools/agentos.py:1526`）。
- 实测：body 21 行；首行 `> 复发簇               13 个 · 阈值 3 run ...`。

### 3.9 能力地图（`cap`，直达键 `9`）

- 展示什么：能力总数与清单。
- 数据来源：`load_cap`（`tools/agentos.py:1567`），取
  `GET /__capmap`（`tools/agentos.py:1569`）。
- 实测：body 21 行；首行 `> 能力总数             36`。

### 3.10 工作区（`ws`，直达键 `0`）

- 展示什么：当前项目与目录树。
- 数据来源：`load_ws`（`tools/agentos.py:1611`，`_ws_dirs` :1603），取
  `GET /__projects`（`tools/agentos.py:1613`）与 `GET /__fs?op=list&depth=1`。
- 实测：body 21 行；首行 `> 当前项目             console（console）`。

### 3.11 终端（`term`，无数字键，Tab）

- 展示什么：只读白名单命令与执行回执。
- 数据来源：`load_term`（`tools/agentos.py:1828`），经
  `POST /__term`（`tools/agentos.py:1708`）。
- 实测：body 6 行；首行
  `■ 只读白名单  cat date find git grep head ls pwd python3 stat tail wc whoami`。

### 3.12 文件（`file`，无数字键，Tab）

- 展示什么：目录列表与文件内容（只读，越界由服务端拒）。
- 数据来源：`load_file`（`tools/agentos.py:1871`），取
  `GET /__fs?op=list` / `op=read`。
- 实测：body 20 行；首行 `■ 目录  .`。

### 3.13 设置（`set`，无数字键，Tab）

- 展示什么：构建指纹与三路只读状态。
- 数据来源：`load_set`（`tools/agentos.py:1903`），取 `GET /__fs`、
  `GET /__projects`（`tools/agentos.py:1922`）与 `GET /__health`
  （`tools/agentos.py:1933`）。
- 实测：body 16 行；首行 `> html_sha256          5a118f537f548c1...`。

## 4. 键位

键位分发真源是 `run()` 主循环（`tools/agentos.py:2635`），逐键分支如下。

### 4.1 数字直达键（1-9 与 0）

- 分支：`tools/agentos.py:2755-2769`。可识别键列的是 ASCII 数字表
  `("1"..."9", "0")`，**不是** `key.isdigit()`。
- 硬规矩一：`0` = 第 10 面（`n = 10 if key == "0" else int(key)`，
  `tools/agentos.py:2765`），与 web 端同一口径。
- 硬规矩二：判据列 ASCII 数字表而不是 `isdigit()` —— `isdigit()` 认全角
  「１」与上标「²」，后者 `int()` 直接抛 `ValueError`，会让 TUI 崩。
- 第 11-13 面不给数字键，界面上也不标（见 4.4）。

### 4.2 移动与切面

| 键 | 行为 | 出处 |
|---|---|---|
| `↑` | 光标上移一行，清当前明细 | tools/agentos.py:2665 |
| `↓` | 光标下移一行，清当前明细 | tools/agentos.py:2668 |
| `Tab` / `→` | 下一面（13 面循环） | tools/agentos.py:2672 |
| `Shift-Tab` / `←` | 上一面（循环） | tools/agentos.py:2682 |
| `←`（仅文件面） | 回上一层目录，不切面 | tools/agentos.py:2675 |

### 4.3 动作键

| 键 | 行为 | 出处 |
|---|---|---|
| `回车` | 当前行明细；Agent 台跑工具/授权卡；文件面进目录/读文件 | tools/agentos.py:2685 |
| `a`（Agent 台） | 授权开关（对当前审批卡） | tools/agentos.py:2721 |
| `i` 或 `/` | 进输入行；仅 Agent 台/终端/文件三面有输入行 | tools/agentos.py:2729 |
| `c`（Agent 台） | 直接进 composer 输入态 | tools/agentos.py:2747 |
| `s`（Agent 台） | 中止正在跑的运行 | tools/agentos.py:2751 |
| `r` | 刷新本面取数 | tools/agentos.py:2770 |
| `q` / `Ctrl-C` / `Esc` | 退出 | tools/agentos.py:2663 |

### 4.4 退出与收尾

- 退出分支：`tools/agentos.py:2663`（`key in ("q", "\x03", "esc")`），
  `run()` 在 `finally` 里还原光标并清屏（`tools/agentos.py:2771-2774`）。
- 数字键标由 `_tab_digit`（`tools/agentos.py:2329`）计算；`n` 越界或 `n>10`
  返回空串（`tools/agentos.py:2336`），因此第 11-13 面标签前没有数字。

## 5. 契约

### 5.1 footer 行格式

- 真源：`tools/agentos.py:2555-2557`，格式串
  `视图 %d/%d · 数据源: %s`（`self.vi + 1`、`len(VIEWS)`、`self.source`）。
- 走查据此判「按键后 footer=视图 N」：见 `work/tui_walk.json` 每条键的
  `expect`（例如 `按 1 后 footer=视图 1 且 tab 括住该面`）。

### 5.2 tab 高亮契约

- tab 栏由 `paint()`（`tools/agentos.py:2464`）绘制，分两行、每行最多 7 面
  （`tools/agentos.py:2480`）。
- 激活面用方括号包住：`"[%s]" % label if i == self.vi else " %s " % label`
  （`tools/agentos.py:2479`）。所以「哪一面高亮」= 哪一面的 token 带方括号。
- 走查判据在 `tools/tui_walk.py:480-487`：把抓到的激活 tab 名字去掉数字前缀
  （`tab_name`，`tools/tui_walk.py:189`）后与 `VIEWS` 常量逐项比对，
  得 `tab_matches_views` 布尔。

### 5.3 数字键与标签一致

- 门要求屏上 kbd 标签与 `DIGIT_KEYS` 一一对应，第 11 面起不得有 kbd 标签
  （`tools/a11y_walk.py:306-321`）。

## 6. 诚实缺口

- `reader_at = AT-UNVERIFIED`（`tools/tui_walk.py:65`）。
- 原因（原文）：`READER_WHY`（`tools/tui_walk.py:66-67`）——
  「macOS VoiceOver 的辅助功能权限（TCC）不能由本沙箱进程授予，OS 层读屏
  未实测；本文件只证据到『剥 ANSI 后的纯文本』这一层。」
- 含义：当前 aria / 可访问性覆盖是**机检**（PTY 抓帧、剥序列后比对文本），
  **不是**真屏幕阅读器实测。这是一条已知缺口，不许粉饰。
- `work/tui_walk_gate.py` 里也有一条判据专门盯这点：
  `BASE reader_at 诚实（=AT-UNVERIFIED，不冒充 OS 读屏）`（本回合 12/12 门通过）。

## 7. 复现

本回合实跑，两条命令与真实 rc：

```
python3 tools/agentos.py tui > /tmp/w22_tui.txt 2>&1 ; echo $?   # rc=0
python3 tools/tui_walk_gate.py ; echo $?                          # rc=0
```

- 第 1 条：rc=0，`grep -c '^== '` = 13，ANSI 字节计数 = 0。
- 第 2 条：rc=0，末两行
  `== TUI_WALK_GATE OK  12/12 门通过（BASE 真 PTY 走查 + 2 个变异体）==` 与
  `TUI_WALK_GATE-OK 2/2 例`；`work/tui_walk.json` 本轮被 BASE 覆写
  （`child_rc`=0、keys 35/35、views 13/13）。

## 8. 相关文件

- `tools/agentos.py` —— TUI 实现（VIEWS / LOADERS / run / paint）。
- `tools/tui_walk.py` —— 真 PTY 走查抓帧与判据。
- `tools/tui_walk_gate.py` —— 走查门（BASE + 2 变异体）。
- `work/tui_walk.json` —— 走查证据（每键 expect/detail/sent）。
- `work/tui_walk_gate.json` —— 门自证（12 条判据全 ok）。
- `index.template.html:897-909` —— web 端 13 个 `role="tab"` 同名口径。
