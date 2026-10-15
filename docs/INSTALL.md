# 安装 / 卸载

AgentOS 的终端入口是一份**生成式 shim**：`install.sh` 把仓库里的 `bin/agentos` 模板
读出来、把里面的 `__AGENTOS_ROOT__` 换成你机器上的真实仓库根，再装成
`~/.local/bin/agentos`。不是软链，所以「装了什么」永远说得清：就一个 3KB 的
POSIX shell 脚本，卸载只删它。

## 装

```bash
cd /Users/zhangfengrui/Documents/Codex/2026-10-01/agentos-console   # 换成你的仓库根
bash install.sh
```

实测（2026-10-10，macOS Darwin，bash 3.14 的 python3）：

```text
$ bash install.sh
  python     /usr/local/bin/python3
  自检       /Users/zhangfengrui/.local/bin/agentos --version rc=0 -> AgentOS 0.2.0
完成。卸载: bash .../uninstall.sh
agentos 已在 PATH 中: /Users/zhangfengrui/.local/bin/agentos
$ echo $?
0
```

装完在任意目录都能用：

```bash
agentos --version        # AgentOS 0.2.0
agentos tui              # 十三视图终端主界面
agentos --help           # 13 个子命令 + 全局 flag（2026-10-11 02:13 复核，13 = 上面 `子命令` 段逐项数出）
```

## 验

```bash
agentos --version </dev/null ; echo $?    # 0
agentos --help   </dev/null ; echo $?     # 0
agentos health              ; echo $?     # 0 = 8790 服务活着；非 0 就 agentos serve
agentos resident            ; echo $?     # 0 = 常驻实例（RunAtLoad 开机自启）就位
```

## PATH

`install.sh` **默认不动你的 `~/.zshrc` / `~/.bashrc`**，只在装完打印该加哪一行。
要它代写就显式加开关，写的是一整块带起止标记的内容，装两次也只有一份：

```bash
bash install.sh --path          # 幂等追加 export PATH
bash install.sh --path --completions   # 再顺手装 zsh 补全到 ~/.zfunc/_agentos
```

手动也可以：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
exec zsh
```

不想污染 `~/.local/bin` 就装到别处，卸载时用同一个目录：

```bash
bash install.sh --dest ~/bin
bash uninstall.sh --dest ~/bin
```

## 卸

```bash
bash uninstall.sh
```

只删装出去的东西：`~/.local/bin/agentos`、rc 里带标记的那一块、 `~/.zfunc/_agentos`。
仓库里的 `bin/agentos` 和 `completions/` 是源码，**不动**。卸载后还会自跑一次验收：

```bash
$ bash uninstall.sh
  已删 /Users/zhangfengrui/.local/bin/agentos
  保留源码 .../bin/agentos
验收: PATH 里已无 agentos（rc=0）
$ command -v agentos ; echo $?
1
```

`--dry-run` 先看会删什么再真删；`--purge` 才会连源码产物一起删（默认不删，
源码归 git 管，不该由卸载脚本决定）。

## 补全

```bash
source <(agentos completion bash)      # 当前 shell 生效
source <(agentos completion zsh)       # 当前 shell 生效
```

仓库里已有生成好的两份：`completions/bash`、`completions/zsh`，语法门
`bash -n` / `zsh -n` 均 rc=0。以后 CLI 的子命令表变了，重新生成：

```bash
bash install.sh --refresh-completions
```

## 离线模型后端（可选）

`agentos agent` 默认复用 `~/.codex/config.toml` 里的模型端点；那台机器上外部端点
被上游挡住时（本文档实测：本地代理 `GET /v1/models` 200，`POST /chat/completions`
被上游拒），可以改走本机 Ollama 的 OpenAI 兼容口，全离线、不依赖外网：

```bash
# 显式开开关：base_url 由开关决定，模型名可用 AGENTOS_LLM_MODEL 覆盖
AGENTOS_LLM_LOCAL=ollama AGENTOS_LLM_MODEL=qwen3:0.6b \
  agentos agent -p --mode plan '用 read_file 读 README.md 前 3 行'

# 等价写法：自己写全 base_url（走 agent_core 的环境变量面）
AGENTOS_LLM_BASE_URL=http://127.0.0.1:11434/v1 AGENTOS_LLM_MODEL=qwen3:0.6b \
  agentos agent -p --mode plan '用 read_file 读 README.md 前 3 行'
```

前置：`ollama list` 至少有一个模型（本文档实测用 `qwen3:0.6b`），且
`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:11434/v1/models` 返回 `200`。

实测（2026-10-11 02:12，macOS）：上面第一条 → **rc=0**，`2 步，10.8s，工具调用 1 次`。
0.6b 这类小模型的答案质量另说，这里验的是**内核链路真跑通**（模型 → 工具 → 回灌 → 终答）。

**为什么必须显式开、不许自动探测**：本机常年跑着 Ollama，自动探测会把
`neg_agent_core` 的「空 HOME 必须抛 `no-provider`」判据变红 —— 那等于把
「没配端点」谎成「有端点」。`python3 tools/agent_core.py --check` 第 16 条把这条
边界钉进了自检（开 = 给 11434，未开 = `no-provider`）。

### 交互 REPL 自动回退（开箱即用）

不用记环境变量：`agentos agent` 交互 REPL 默认走 `~/.codex/config.toml` 的端点，
主端点连不通（403/400/不可达）时内核会**自动**回退到本机 Ollama（若在跑），并在
stderr 逐字打印 `[回退] 主端点失败（...），改用本机离线后端 qwen3:0.6b`，然后
继续这一轮。REPL 不会因为端点错误退出，上下文保留，默认开流式（`--no-stream` 关）。

```bash
# 什么都不用设，直接进 REPL；端点挡了就自动回退
agentos agent
```

边界：回退只给**交互 REPL**。headless（`agentos agent -p '...'`）**不**回退 ——
没端点就 `rc=3`，免得 CI 里「没配端点」被本机模型悄悄洗绿。实测（2026-10-11）：
默认环境 REPL → `rc=0`（含 `[回退]`）；显式 `AGENTOS_LLM_LOCAL=ollama` → `rc=0`；
headless 空 HOME 无端点 → `rc=3`；REPL 连续两轮端点失败仍不退出 → `rc=0`。
回退路径由 `agent_core.py --check` 第 18 组用桩端点双向证伪（33/33）。

## 幂等

`install.sh` 跑第二次，产物与第一次逐字节相同：

```bash
$ md5 -q ~/.local/bin/agentos
1dc4695c3d5b505d89a9cd0fc4c412f7
$ bash install.sh -q ; echo $?
0
$ md5 -q ~/.local/bin/agentos
1dc4695c3d5b505d89a9cd0fc4c412f7
```

干净环境（`env -i`，PATH 只剩 `/usr/bin:/bin`）也能装：

```bash
cd /path/to/agentos-console && env -i HOME="$HOME" PATH=/usr/bin:/bin bash install.sh ; echo $?
0
```

## 排障

1. `agentos: command not found`
   PATH 里没有安装目录。`ls -l ~/.local/bin/agentos` 确认在，然后
   `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh`。
   或者一开始就 `bash install.sh --path`。
2. `agentos: 定位不到 AgentOS 仓库`（rc=127）
   仓库被移走或改名了，shim 里内嵌的根已失效。回仓库根重跑
   `bash install.sh` 重新生成一份即可。
3. `agentos: 没找到可用的 python`（rc=127）
   本机没有能编译 `tools/agentos.py` 的解释器（需要 Python 3.9+）。
   装一个 Python 3，或用 `AGENTOS_PYTHON=/path/to/python3 agentos --version` 临时指定。
4. `agentos serve --port 8787` 报 `FAIL: 8787 是 headroom proxy 的常驻端口`
   这是故意拒的：8787 归常驻 headroom proxy。用默认 `agentos serve`（8790，被占则顺延）。
5. 卸载后 `command -v agentos` 还能解析到
   那是别处装的同名命令，不是这份脚本装的。`ls -l "$(command -v agentos)"` 看是谁；
   本脚本只删安装时用的那个 `--dest`。

## 环境要求

macOS 或 Linux + `bash` + Python 3.9+。不需要 `sudo`，不需要 `readlink -f`
（BSD 的 readlink 没这个选项，shim 只用 POSIX 的 `dirname` + `pwd -P`）。
