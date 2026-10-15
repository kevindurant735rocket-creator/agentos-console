#!/bin/bash
# AgentOS 交付驾驶舱 · 双击启动（macOS Finder）
# 作用：起本地服务 → 自动开浏览器 → 真树有变动就自动重抽+重构建+页面热重载
# 关闭：在此窗口按 Control+C
cd "$(dirname "$0")" || exit 1
PY=/opt/miniconda3/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)
echo "== AgentOS 交付驾驶舱 =="
"$PY" tools/agentos.py serve --open --watch-tree --port 8790
echo "已退出（rc=$?）。"
read -r -p "按回车关闭窗口…" _
