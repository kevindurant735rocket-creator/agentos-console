# AgentOS 仓库工作纪律（每条都是踩过坑的）

## 0. 这个仓库**只有终端面**（2026-10-20 起）

网页驾驶舱与原生 macOS app 已下线删除，连同它们量的 30+ 道门（门链 77→40）。
- **不要**再引入：常驻服务、launchd job、浏览器面、原生 app、GUI 弹窗。
  2026-10-20 用户明确要求：「工作的时候不要老是打开那个软件」。
- 终端 agent 是**按需跑**的：`agentos agent` / `agentos agent -p '…'`。
- 装机只做一件事：把 CLI shim 放进 PATH（`install.sh`）——不装 app、不写
  launchd、不起常驻服务。
- 如果以后要加任何「自己会弹出来」的东西，先问用户。

## 1. 永不上传自有内容到 GitHub

**铁律**：本仓库自己写的东西——代码、文档、笔记、克隆、实验产物——**一律不推到 GitHub**。
唯一允许出现在 GitHub 上的，是**本仓库这一份**（`agentos-console`），以及向**上游开源项目**
发起的 fork→PR（那属于贡献上游，不是上传自有内容）。

**"部署一个项目"的真实含义**（易被误解，先说死）：
- 部署 = **在本地研究 GitHub 上的高星项目，把可复用的设计蒸馏进本仓库**（本地写盘 + 进门链自检 + 本仓 commit/push）。
- 部署 ≠ 创建/推送新 GitHub 仓库。
- 上一次事故：把 `agentos-study-cli*` 系列（9 个 public 仓库）推到了 GitHub —— **方向性错误**，
  那些仓库是自有派生内容，且仓库名带内部代号，没有任何对外价值。已列入待删清单
  （删除需要 `gh auth refresh -h github.com -s delete_repo` 授权，token 目前没有 `delete_repo` scope）。

**正确姿势（本地蒸馏流水线）**：
```
gh_search（找 1k+ star） → gh_repo_view / gh_file_read（读源码）
  → 在本仓 tools/ 里写出适配后的实现 → 跑门自检 → 本仓 commit + push
```
克隆只落在 `work/distill/`（已 .gitignore），不提交进仓库。

## 2. 宣称完成必须有三证

- **日志**：命令的真实 rc / 输出（不许转述时改数字）
- **账本**：信号账 / 规则账 / 计划账能查到这一轮
- **真树 diff**：工作区的实际改动（`git diff` 看得到，不是"应该改了"）

三证缺一，就是**没做完**，如实说卡在哪。

## 3. 门与解释器

- 全链：`bash tools/gates.sh <门名>...`（**必须给门名**，零参数 = 一道门都没跑，不能判绿）
- 门驱动器统一走 `tools/.venv-pw/bin/python`（`gates.sh` 内部已固定）
- **不要在门驱动器里显式传 `sys.executable`**：会让 pyobjc 相关门假红。

## 4. 收敛纪律（别把 agent 又变回"探查很多"）

内核有收敛引擎（`tools/converge.py`）：阶段机 / 循环熔断 / 停滞探测 / 交付闸。
改它的时候守住三条：

1. **判据只认证据，不认措辞**（模型说"完成了"不算证据；`write_file` 成功 + `run_cmd rc=0` 才算）。
2. **诚实报告失败是真交付**（"写盘被拒，做不到"是合格终答，别退回）。
3. **阈值只许收紧不许放宽**（自进化旋钮 `apply_policy` 有钳制；放宽 = 拆护栏）。

## 4.1 门链与真实终端测试**不许并发**

2026-10-19 实测踩到：门链跑到 `abort_e2e` 时，我在旁边同时跑真实终端
agent 测试 → 该门判红（停止按钮/进程回收/中止提示全错）。单独重跑该门**全绿**。
原因：abort_e2e 驱动真浏览器 + 8790 真服务，并发跑会抢同一个环境。

规矩：**门链期间不跑真实终端测试**；真终端测试单独跑、跑完再起链。

## 5. 自进化闭环的形状

```
真实跑 → 信号账(signals.jsonl) → 挖掘(规则) → 提案 → 沙箱门 → 合入真树 → 真树复验 → ACTIVE
```
ACTIVE 规则**必须能改行为**，不只是往 prompt 里加提示文字：
收敛类规则通过 `action.op=converge_tune` 拧引擎旋钮（`max_stall` / `max_explore` / `loop_repeat`）。

## 5.1 端点连不上：两件事都做

**一、内核侧（已实装）**：端点「连不上」（传输层不通 / 网关过载 / 限流）→
**一直重试到端点恢复**，绝不改用本地模型（`llm.no_fallback="true"` 时预算=0=无上限）。
401/403/400 **不在**此列：key/协议问题等一辈子也不会好，还会把真病因藏起来 ——
那种要立刻报错 + 给处方。预算可调：`llm.endpoint_retry_max`（0=一直等，>0=试 N 次）。

**二、链路侧（本机常见病因）**：clash 把 opencode.ai 路由到死节点时，
表现是 `SSL_ERROR_SYSCALL` / `UNEXPECTED_EOF_WHILE_READING`。查+修：

```bash
SOCK=/var/run/clash-verge-service/users/501/verge-mihomo.sock
# 1) 看当前组选了谁
curl -sS --unix-socket "$SOCK" http://localhost/proxies | python3 -c "import json,sys;d=json.load(sys.stdin)['proxies'];print({k:v.get('now') for k,v in d.items() if v.get('type') in ('Selector','URLTest','Fallback')})"
# 2) 用 opencode.ai 自己的 URL 测各节点（别用 gstatic 测——它不代表本机要用的站点）
curl -sS --unix-socket "$SOCK" "http://localhost/proxies/<节点名>/delay?timeout=6000&url=https%3A%2F%2Fopencode.ai%2Fzen%2Fgo%2Fv1%2Fmodels"
# 3) 切到能用的节点
curl -sS -X PUT --unix-socket "$SOCK" -H 'Content-Type: application/json' -d '{"name":"<节点名>"}' "http://localhost/proxies/自动选择"
# 4) 复核
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 https://opencode.ai/zen/go/v1/models
```
2026-10-20 实测：`自动选择` 当时指着 `FR高速-法国`（到 opencode.ai 不通），
切到 `CA高速-加拿大02`（267ms）后立刻恢复 HTTP 200。

## 6. 云端模型不可达时

一直重试云端端点，**绝不用本地模型顶替**。配置口径：`llm.no_fallback` / `gh.write_enabled`
**只认字面 `"true"`**。
