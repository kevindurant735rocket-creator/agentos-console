# 框架级提升提案：evo-tool-fail-read_file

- 签名：tool-fail:read_file（×6，触发阈值 3）
- 现有建议：read_file 失败多为路径不存在或越界：先 list_dir 确认结构再读，路径以项目根为基准
- 提议方向（人审采纳；学习只建议，永不擅改门与判据）：
  1. 工具缺陷 → 修工具实现并补 --check 用例；
  2. 使用方式 → 建议升级进 AGENTS.md 项目记忆；
  3. 量具空白 → 提议新判据（先出 --check 断言草案）。
- 生成时间：2026-10-21 16:56:03
- 机器 intake：同名 .json（validate_proposal 可验）
