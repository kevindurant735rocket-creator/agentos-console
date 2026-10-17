# ego-browser 部署记录（t3）

- 二进制：/Users/zhangfengrui/.local/bin/ego-browser → `ego-browser 0.5.1.13` / chromium 152.0.7977.54 / node v24.18.1
- 本地 SKILL：v2.0.0（2026-09-09，实目录为只读 App bundle：/Applications/ego lite.app/.../Versions/0.5.1.13/Resources/ego-skills/ego-browser）
- 上游 SKILL：v2.1.0（2026-10-08，bootstrap 重构：`ego-browser skill` 动态指南；本地二进制报 `Unknown command: skill`，需 `ego-browser upgrade` 后重读）
- 上游已拉取到 /tmp：ego-upstream-SKILL.md（1594B）/ references_install.md（4479B）/ clearing-state（4140B）；实目录只读故本次未覆盖，仅比对（install.md 有增量 diff，clearing-state 无 diff）
- learnings 备份：/tmp/ego-browser-learnings-bak-20261016-183624（github/google/x-com，ls rc=0）
- 探活：https://example.com 快照 + 全页截图，见 snapshot.txt / shot.png
- 产物：ego-probe/snapshot.txt（1548B）/ ego-probe/shot.png（82547B）
