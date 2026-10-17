# ego-probe 接线说明（t4 / M3 配套）

## 技能链（t2 定案 C1）
`~/.agents/skills/ego-browser` → `~/.codex/skills/ego-browser` →
`~/.local/share/ego/ego-skills` → `active_version_dir/.../ego-browser`
链尾含 `SKILL.md` + `learnings/` + `references/` + `scripts/`。

## 版本对照（t2 C2/C3，t3 落盘）
- 本地 SKILL：v2.0.0（2026-09-09，只读 App bundle）
- 上游 SKILL：v2.1.0（2026-10-08，`citrolabs/ego-lite:skills/ego-browser/SKILL.md`）
- 二进制：`~/.local/bin/ego-browser` → `ego-browser 0.5.1.13`
- 升级提示：实目录只读，本次只比对不覆盖；`ego-browser upgrade` 后重读 Skill
 （`ego-browser skill` 新命令需升级后可用）。

## 探活产物（t3）
- `ego-probe/snapshot.txt`：https://example.com 快照文本
- `ego-probe/shot.png`：全页截图
- `ego-probe/VERSION.md`：版本号记录
- `ego-probe/probe.err` / `stdout.txt`：探活原始输出

## 下一步
- 运行 `ego-browser upgrade`（需用户确认）→ 重读 Skill → 上游 diff 合入后复验。
- 判据见 `ego-probe/CHECK.md`（J1–J5）。
