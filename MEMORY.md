# MEMORY.md — agentos-console

## 一句话定位

纯终端 AgentOS 终端 agent：REPL/headless 两种入口，自带收敛引擎（阶段机/熔断/停滞/
交付闸）、任务契约、任务账本、自审 diff、41 道门作为唯一验收口径。**只有终端面**：
网页驾驶舱、原生 app、常驻服务、浏览器面已全部下线删除（2026-10-21）。

## 目录地图

| 路径 | 装什么 |
|---|---|
| `tools/agent_core.py` | 内核主文件（222KB，**上限 256KB** —— 超过 agent 就读不了自己） |
| `tools/agent_core_selftest.py` | `--check` 自检正文（169 条，条数被 10+ 门文件钉住） |
| `tools/agent_provider.py` | provider / 端点层 |
| `tools/agent_session.py` | 会话 / 压缩 / rewind 层 |
| `tools/converge.py` | 收敛引擎（阶段机/熔断/停滞/交付闸/有界失败修复回路/自审 diff） |
| `tools/todo.py` | 任务账本（**增量更新**：`action=update/add`） |
| `tools/gates.sh` | 41 门正确链（**零参数 = 假绿**，必须显式给门名） |
| `work/selfscan_baseline.json` | bandit 基线（**按文件记** → 代码搬家会显红） |
| `docs/ARCHITECTURE.md` | 结构框架 + 已知缺口 |

## 关键约定

- **门链是唯一验收口径**；`bash tools/gates.sh <门名…>`（零参数直接 exit 2）。
- 改 `agent_core.py` 的自检条数 → 10+ 个门文件里的 `169/169` 计数钉要同步。
- 交付闸 fail-closed：证据不足 / 账本有 pending → 判「未交付」。
- 证据只认证据不认措辞；诚实报告失败算真交付。
- **账本是任务域模型**（学 ruvnet/ruflo v3 的 `Task` 实体）：状态迁移有守卫
  （`done` 不许打回 `pending`，重开必须 `reopen="<理由>"` 且留痕）、项可带
  `depends_on`（成环拒、悬空忽略）、`priority` 决定就绪项顺序。改账本语义时
  `tools/todo_selftest.py` 的**变异体自证**要跟着更新（锚点在守卫函数体上）。

## 已知坑位（都踩过，勿重蹈）

1. **内核拆成多模块后，门的「补丁/变异打在哪」全部要改**：
   - 负向门把掏空补丁打在 `agent_core.<名字>` 上 → 函数搬走后打空 → 门假绿。
     必须打在**函数真正所在模块**（`fn.__module__`），**还原也必须还原到同一个模块**
     （还原打错 → 掏空版永久留在模块里 → 后面所有用例失真）。
   - 变异副本必须暂存**整棵 `tools/` 树**且目录形状为 `<dir>/tools/*`
     （内核按 `ROOT/tools` 绝对路径加载子模块），副本要**按原名**叫 `agent_core.py`
     且其目录在 `sys.path[0]`（否则自检导入原版 → 变异咬不到）。
   - 源码锚点类判据要数**整个内核模块集**（搬家 ≠ 功能消失）。
2. **自检懒导入要按 `__file__` 所在目录找模块**（`append` 不抢前），
   否则拷贝/变异副本会导入原版 → 假绿。
3. **selfscan 基线按文件记**：代码搬家会被判成"新增 MEDIUM"。外科式改那两条，
   不要整体重建（会掩盖真新增）。
4. **工具参数分派顺序**：`action` 分派必须排在「参数缺省」判断**之前**
   （`todo(action=update, changes=…)` 不带 items 时曾被当成读 → 更新被吞）。
5. `read_file` 上限 256KB：内核主文件必须 < 256KB，否则 agent 读不了自己的主文件。
6. 端点连不上要**一直重试**（401/403/400 才是 key/协议问题，立即报错给处方）。
7. `gates.sh` 优先用带 playwright 的解释器；没有就退回 python3 并如实说明
   （终端链**没有一道门需要浏览器**）。
