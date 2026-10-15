# 故障排查（每条都是真跑出来的）

本文 rc 均为 2026-10-10 16:27-16:42 在 macOS 真跑所得。处理建议都贴着退出码走：
修没修好，重跑同一条命令看 rc，不猜。

## 1. `agentos health` rc=1：服务没起

```bash
python3 tools/agentos.py health --port 8799
# FAIL: health ... 连接失败            # rc=1
```

处理：`python3 tools/agentos.py serve` 起服务；或双击「启动 AgentOS.command」；
常驻实例是否在位跑 `python3 tools/agentos.py resident`（rc=0 = RESIDENT_GATE OK 20/20）。

## 2. `agentos tui` 每面「取数失败」rc=1

```bash
python3 tools/agentos.py tui --port 8799 </dev/null
# 每面打「取数失败: ... Connection refused」   # rc=1
```

服务没起时如实红，不画假数据。处理同第 1 条。服务起来后 `tui </dev/null` rc=0。

## 3. `serve --port 8787` rc=2

```bash
python3 tools/agentos.py serve --port 8787
# FAIL: 8787 是 headroom proxy 的常驻端口，serve 不得抢占   # rc=2
```

8790 是默认口；被占用会顺延下一个空闲口，不会抢 8787。

## 4. 端口被占：实例顺延到 8791/8792，health 却看不见

8791 上可能有别的进程（本机 2026-10-10 就有一个外来实例占着 8791）。
再起一个实例会落到 8792，而 `agentos health` 默认打 8790，于是「服务明明起着却报不健康」。

```bash
python3 tools/agentos.py health --port 8792     # 对实际端口再判一次
lsof -iTCP:8792 -sTCP:LISTEN                    # 看是谁占着
```

顺延行为本身有实测背书（W1 2026-10-09）：两个实例同抢 8791，A 起到 8791、B 起到 8792，
两端 `__health` 均 `ok: true`，kill 后双双释放。

## 5. 双击启动器起了 8791，页面却是旧的/不对

常驻在线时再双击「启动 AgentOS.command」，它不双绑 8790，而是顺起到 8791
并打开 8791 的页面（W1 实测）。想看常驻那份数据：`python3 tools/agentos.py open`（打 8790）。

## 6. 裸探 headroom 8787 得到 HTTP=421

```bash
curl -s -o /dev/null -w 'HTTP=%{http_code}\n' --max-time 5 http://127.0.0.1:8787/
# HTTP=421        # curl rc=0
```

8787 是 headroom proxy 常驻口，要带 key 的客户端才拿得到页面。421 是预期行为，不是故障。

## 7. `agentos -p ""` rc=2

```bash
python3 tools/agentos.py -p "" </dev/null
# FAIL: -p/--print 需要一句需求     # rc=2
```

`-p` 要一句人话需求，例如 `-p "状态"`（rc=0）。

## 8. `agentos gates` 零参 rc=2

```bash
python3 tools/agentos.py gates            # rc=2：零参数 = 假绿，拒跑
python3 tools/agentos.py </dev/null       # rc=2：非 TTY 且无子命令
```

要点名门：`agentos gates resident_gate`。零参故意判红——不跑任何门却返回绿就是假绿。

## 9. `agentos term 'git status'` rc=1

```bash
python3 tools/agentos.py term 'git status' </dev/null
# FAIL: 命令未执行：命令不在只读白名单里：git status（可跑：cat date find git grep ...）
# rc=1
```

引号包整条命令时它只是一个 token，白名单按**首 token** 匹配。正确写法是分开传：

```bash
python3 tools/agentos.py term git status    # 或 term ls -1 tools（rc=0）
```

白名单实测：`cat date find git grep head ls pwd python3 stat tail wc whoami`。

## 10. `agentos completion fish` rc=2

只支持 zsh/bash，`--help` 没承诺 fish。要补全：`eval "$(python3 tools/agentos.py completion zsh)"`
（`completion zsh` rc=0），或 `bash install.sh --completions` 装到 `~/.zfunc/_agentos`。

## 11. `agentos: command not found`（rc=127）

```bash
PATH=/usr/bin:/bin agentos --version
# zsh:1: command not found: agentos      # rc=127
```

`~/.local/bin` 不在 PATH：`echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh`，
或重跑 `bash install.sh`（装的是生成式 shim，卸载只删它一个文件）。
仓库被搬走且没重装时，shim 自己也会 rc=127 并打印「定位不到 AgentOS 仓库」。

## 12. `agentos resume <id>` rc=2

```bash
python3 tools/agentos.py resume no-such-session </dev/null
# FAIL: 找不到会话 no-such-session（agentos sessions 看可用 id）   # rc=2
```

`agentos sessions`（rc=0）列可用 id，再回放。

## 13. `agentos gh` rc≠0

`gh` 面数据源是本机 `gh` CLI；没认证、没网或 scope 不够都会 rc≠0 并在输出里说明原因。
已认证时实测 `python3 tools/agentos.py gh repos --limit 3` rc=0，末行 `sources:` 打出真实 gh 命令。
本面是只读面：白名单在调用前拦写向量，零远端写。

## 还没被这些覆盖的

- `-p` 执行超时的 rc=124 路径本文未复跑（契约在 `tools/agentos.py:694`）。
- 服务重载瞬间 `health` 可能回 HTTP=502（区别于连接被拒，`tools/agentos.py:133` 有注释）；
稍等几秒重试即可，本文未构造该窗口复跑。
