# AgentOS CLI 参考

AgentOS 的终端入口。所有子命令都走同一个常驻服务（默认 `http://127.0.0.1:8790`），
CLI 本身只做路由与展示；安装见 [INSTALL.md](INSTALL.md)。

本文 rc 以 2026-10-10 17:39-18:05 在 macOS（Darwin）真跑复核所得为准
（部分摘自 16:27-16:42 首轮记录，命令均可复跑）。复核口径：
`agentos <args>; echo RC=$?` —— 不靠读码推断。

## 退出码契约

`tools/agentos.py:253` 写死：`0` 绿 / `1` 远端或解析失败 / `2` 用法错 / `3` 基础设施错。

## 全局 flag

| flag | 作用 |
|---|---|
| `--url URL` | 服务地址（默认 `http://127.0.0.1:8790`） |
| `--port PORT` | 服务端口（默认 8790；8787 是 headroom 常驻口，serve 拒抢） |
| `--timeout N` | 单次执行上限秒（`-p` 用，默认 300） |
| `--no-color` | 兼容预留：本 CLI 全量零 ANSI，本来就没有色 |
| `--output-format {text,json}` | `-p` 的输出格式（json 时 stdout 恰好一行，可 `json.loads`） |
| `-V, --version` | 打印 CLI 自身版本，不连服务 |
| `-p, --print [需求]` | headless 一次性模式：人话 → allowlist 动作 → 流式执行 |

零 ANSI 实测：`python3 tools/agentos.py health | rg -c $'\x1b'` → 0 命中（rg rc=1）；
`NO_COLOR=1 python3 tools/agentos.py health` rc=0，输出与不设变量一致。

## 子命令（共 13 个）

`python3 tools/agentos.py --help` → rc=0（2026-10-10 16:27 实跑，清单如下）。

### serve — 起本地服务

默认端口 8790，被占用则顺延下一个空闲口；8787 是 headroom proxy 常驻口，拒绝抢占。

```bash
python3 tools/agentos.py serve                 # 起到 8790；被占则顺延 8791/8792
python3 tools/agentos.py serve --port 8787     # rc=2，FAIL: 8787 是 headroom proxy 的常驻端口
python3 tools/agentos.py serve --port 8791     # rc=0（W1 实测：UP ...:8791，__health ok:true，kill 后端口释放）
```

顺延行为实测（W1 于 2026-10-09 记录）：两个实例同抢 8791，A 起到 8791、B 打印
`[port] 8791 被占用，顺延下一个` 起到 8792，两端 `__health` 均 `ok: true`。

### health — 判 `__health` 的 ok:true

```bash
python3 tools/agentos.py health
# rc=0，输出：
# health OK   http://127.0.0.1:8790/__health
#   index  e8bd8c9cd5ac:1791616786   data  6fdf2493fd74:1791470197
#   clients 1   started 2026-10-10 15:20:01   rebuilds 0
python3 tools/agentos.py health --port 8799    # rc=1：连接失败时诚实报非绿
```

### open — 浏览器打开驾驶舱首页

`BROWSER=/usr/bin/true agentos open` → rc=0，打印 `打开 http://127.0.0.1:8790/index.html`
（2026-10-10 18:00 实跑；用 `/usr/bin/true` 占位浏览器，不开真窗口也不改产物）。
不带 `BROWSER` 时会真的开一个浏览器标签页。

### tui — 终端交互主界面（13 视图）

```bash
python3 tools/agentos.py tui </dev/null         # rc=0
python3 tools/agentos.py tui --port 8799 </dev/null
# rc=1，每面打「取数失败: ... Connection refused」——服务没起时如实红，不画假数据
```

键位见 [KEYBINDINGS.md](KEYBINDINGS.md)。

### resident — 常驻真值门

```bash
python3 tools/agentos.py resident              # rc=0，RESIDENT_GATE OK 20/20（launchd 常驻实例核门）
```

### headroom — headroom 8787 契约门

```bash
python3 tools/agentos.py headroom              # 门链内实测 rc=0，HEADROOM_GATE OK 正向 18/18 · 变异体 47/47
```

单独跑一遍约数分钟；本文引用 2026-10-10 16:19 门链日志
`work/gates/headroom_gate.log` 的结果。裸探 8787：`curl http://127.0.0.1:8787/` → `HTTP=421`
（常驻口需要带 key 的客户端，裸请求拿不到页面，这是预期行为，不是故障）。

### gates — 转发 tools/gates.sh

```bash
python3 tools/agentos.py gates                                  # rc=2：零参数 = 假绿，拒跑
python3 tools/agentos.py gates resident_gate                    # 只跑点名门
```

门链条数**不写死**：2026-10-11 00:25 由 `tools/gates.sh:7` 现读为 45 门
（`evolution_gate`/`capability_gate`/`evolution_gate_selftest` 三道自我进化面门
在 `cli_gate`/`cli_gate_selftest` 之后补入；`perf_gate` 收尾，把 R6 性能面
（含 `agentos health` 端到端感知启动）纳入全链）。
要看当前真值：`sed -n '7p' tools/gates.sh | wc -w` 减 3。
完整链见 [README.md](../README.md) 与 `tools/gates.sh:7` 注释。

### term — 服务端只读跑一条命令

走 `POST /__term`，命令首 token 必须在服务端只读白名单里。白名单实测
（2026-10-10 16:41）：`cat date find git grep head ls pwd python3 stat tail wc whoami`。

```bash
python3 tools/agentos.py term ls -1 tools        # rc=0，末行「rc=0 · 0.01s · argv=/bin/ls -1 tools」
python3 tools/agentos.py term '__agentos_list_probe__'   # rc=1，报错自带白名单清单（可当 Discover 用）
```

### gh — GitHub 只读面

数据源是 `gh` CLI（`tools/gh_client.py` 收口，只读白名单在调用前拦写向量，零远端写）。

```bash
python3 tools/agentos.py gh repos --limit 3       # rc=0，输出 3 行仓库 + 一行 sources 说明真实命令
python3 tools/agentos.py gh --help                # rc=0
```

未认证/无网时会 rc≠0 并说明原因；本文 rc 来自 gh 已认证的本机。

### completion — 打印 shell 补全脚本

```bash
python3 tools/agentos.py completion zsh          # rc=0，输出可用 eval 的补全脚本
python3 tools/agentos.py completion fish         # rc=2（只支持 zsh/bash，--help 未承诺 fish）
```

### sessions / resume — 历史会话与回放

```bash
python3 tools/agentos.py sessions                 # rc=0，列 work/agent_sessions/cli-*.jsonl 历史
python3 tools/agentos.py resume cli-20261010-180121-a16d </dev/null
# rc=0，逐行重放该次会话记录，末行 resume <id> · action=None · action_rc=None · exit=0
```

### agent — 内置终端 agent（对标 Claude Code 的交互内核）

```bash
python3 tools/agentos.py agent                      # 交互 REPL，默认 accept-edits，默认开流式
python3 tools/agentos.py agent --mode plan          # 只读规划，任何写操作一律拒绝
python3 tools/agentos.py agent --no-stream          # 关掉流式
python3 tools/agentos.py agent -p '跑一遍门链并汇报真实 rc'   # headless，适合脚本
python3 tools/agentos.py agent --check              # 自检：本地桩端点，不碰真模型真网
python3 tools/agentos.py agent --provider opencode -p '...'  # OpenCode zen 端点（需 AGENTOS_LLM_API_KEY）
```

模型端点解析顺序：`--provider <名字>`（内置命名端点，当前 `opencode` = `https://opencode.ai/zen/go/v1` 的 `muse-spark-1.3-contributor`）→ `AGENTOS_LLM_BASE_URL` → `AGENTOS_LLM_LOCAL=ollama`（显式开关）→ `~/.codex/config.toml` → 抛 `no-provider`。命名 provider 给了就以它为准，可用 `AGENTOS_LLM_MODEL` 覆盖模型、`AGENTOS_LLM_API_KEY` 传 Bearer key。REPL 内命令：`/mode <plan|accept-edits|full>`、`/tools`、`/reset`、`/help`、`/exit`。

**开箱即用（自动回退）**：交互 REPL 默认走 `~/.codex/config.toml` 端点；主端点连不通（403/400/不可达）时内核自动回退到本机 Ollama（若在跑），stderr 逐字打印 `[回退] 主端点失败（...），改用本机离线后端 qwen3:0.6b`，REPL 不退出、上下文保留。**headless（`-p`）不回退**：没端点就 `rc=3`，免得 CI 被本机模型悄悄洗绿。退出码：`0` 给出最终回答 / `2` 用法或预算 / `3` 基础设施（provider/HTTP/harness）。

## headless 一次性模式 `-p`

```bash
python3 tools/agentos.py -p "状态"                                   # rc=0，人话 → allowlist 动作 → 流式执行
python3 tools/agentos.py --output-format json -p status </dev/null   # rc=0，stdout 恰好 1 行 JSON
python3 tools/agentos.py -p "" </dev/null                            # rc=2：FAIL: -p/--print 需要一句需求
```

`-p` 的动作走 `POST /__act` + `GET /__act/stream` SSE；超时单独给 `124`
（与 `3` 分开，CI 靠这个判断该不该重试；契约位置 `tools/agentos.py:694`，
W8 任务书已核，本文未单独复跑超时路径）。

## 常见 rc 速查

| 现象 | 命令 | rc |
|---|---|---|
| 正常健康 | `agentos health` | 0 |
| 服务没起 | `agentos health --port 8799` | 1 |
| TUI 连不上服务 | `agentos tui --port 8799 </dev/null` | 1 |
| 抢 headroom 端口 | `agentos serve --port 8787` | 2 |
| `-p` 空需求 | `agentos -p ""` | 2 |
| `gates` 零参 | `agentos gates` | 2 |
| 非 TTY 无子命令 | `agentos </dev/null` | 2 |
| 白名单外命令 | `agentos term '__agentos_list_probe__'` | 1 |

更多故障现象与处理见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

## 诚实清单

- `agentos open` 已用 `BROWSER=/usr/bin/true` 实跑（rc=0）；不带该变量会开真浏览器窗口，未跑。
- `-p` 超时路径（rc=124）本文未单独实跑；`-p "状态"`/`-p ""`/`-p` 路由失败已实跑。
- `agentos gh` 依赖本机 `gh` CLI 已认证；未认证时的 rc 本文未测。
- `agentos yaml` 不存在；`completion fish` 实测 rc=2（只支持 zsh/bash），不是漏写。
- `docs/INSTALL.md` 的子命令数已于 2026-10-10 18:03 同步为 12（`--help` 实测）。
- `cli_gate` 59 条判据里有 2 条（`gh-whoami-rc0`、`print-gh-route`）真打 GitHub API。
  2026-10-10 17:43 反向门 BASE 段就因一次网络瞬态红过这 2 条（同分钟手工复跑 rc=0），
  故这 2 条红时先 `agentos gh whoami` 分清「网断」还是「CLI 坏」，不要直接改门。
