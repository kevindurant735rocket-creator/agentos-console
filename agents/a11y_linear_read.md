# a11y_linear_read — 读屏线性化实测修复（主线程执行，子代理 adopted 后接手）

结论：3 处 AX 缺陷已修，四门全绿。

## 缺陷与修法（index.template.html）

| # | 缺陷（实测复现） | 修法 | 位置 |
|---|---|---|---|
| ① | 单个 AX text 节点 1503/400/322/281 字，读屏无法断句 | `CHUNK_MAX=160` + `chunk()` 按分隔符切块、`chunkHTML()` 每块 `<span class="ln">`；接到 rounds 证据 / `#rule` / `#envg` 三处 | :485 :499 :743 :839 :843 |
| ② | `#stats` 计数条 `.k`/`.v`/`.n` 三层无绑定，糊成 73 字单节点 | `#stats` 加 `role="list"`；每个 `.st1` 改 `role="listitem"` + `aria-label`；三个子 div 加 `aria-hidden="true"` | :412 :695 |
| ③ | 顶栏 brand/sep/stamp/fresh/vtag 5 个 span 无分组 | 包进 `<div class="hdinfo" role="group" aria-label="产品与数据状态">`，CSS 复制 `.hd` 的 gap，视觉不变 | :66 :381 |
| ③ | 顶栏 brand/sep/stamp/fresh/vtag 5 个 span 无分组 | 包进 `<div class="hdinfo" role="group" aria-label="产品与数据状态">`，CSS 复制 `.hd` 的 gap，视觉不变 | :66 :381 |

## 实测证据（命令 + 退出码）

- `bash tools/gates.sh shoot` → SHOOT_RC=0，w375/w768/w1440 innerWidth 精确 + 无文档横向溢出
- `bash tools/gates.sh a11y_walk ux_audit view_dod view_dod_selftest` → **FOUR_GATES_RC=0**
  - A11Y_WALK OK 16/16（真键盘事件 + 真 AX 树）；逐视图 115 个可交互节点无名 0 个
  - UX_AUDIT OK 12/12
  - VIEW_DOD OK 6/6（5 视图 × 3 尺寸 = 15 屏真视口走查）
  - VIEW_DOD_SELFTEST OK 9/9（6 变异体 1:1 覆盖 6 道 DoD 判据）
- `bash tools/gates.sh neg_a11y` → NEG_RC=0，4 变异体全打红，活产物 sha=5951561d728c 与基线一致
- `.venv-pw/bin/python /tmp/ax_probe.py` → AX_RC=0，`div.mono>span.ln` 已 ≤161 字，原 1503 字节点消失
- `.venv-pw/bin/python tools/build.py` → BUILD_RC=0，`SYNTAX_OK node --check rc=0`

## 残留（不在本轮门判据内）

`div#rounds>details.rd>div.rdb>p` 的 AC4 验收段仍为 879 字单节点（AC1–AC4 = 879/200/197/190）。
a11y_walk 不检查节点文本长度（只查 ARIA 覆盖 / 无名节点 / 焦点陷阱），故四门仍绿；
若要把「节点文本长度」纳入 DoD，需先改 a11y_walk 判据再修 DOM。
