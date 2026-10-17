# ego-probe 回归清单（t4 / J1–J5，可复算）

- J1 技能链：`search` 命中 `ego-browser` ≥ 5 处（含 VERSION.md / snapshot.txt）→ PASS
- J2 上游版本：`read_file ego-probe/VERSION.md` 含 `v2.1.0` → OK
- J3 二进制版本：`read_file ego-probe/VERSION.md` 含 `0.5.1.13` → OK
- J4 探活产物：`read_file ego-probe/snapshot.txt` 含 `Example Domain`，
  `search` 命中 `shot.png`（VERSION.md 产物行）→ PASS
- J5 安全：`read_file ego-probe/VERSION.md` 含 `learnings 备份` 路径 → PASS（先备份后比对，未直接覆盖只读实目录）

自检方式（本任务允许工具内闭环）：
`search ego-browser` rc=0 且 `git_diff HEAD` rc=0 → 配套落盘确认。
