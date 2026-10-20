#!/usr/bin/env bash
# uninstall.sh —— 卸掉终端 AgentOS 的 CLI shim。
#
# 只删「本脚本装的那些东西」：目标目录里的 agentos shim + shell 配置里
# 本脚本加的那一行 PATH。**不碰**仓库、不碰别的 PATH 项、不删别的东西。
#
# 用法：
#   bash uninstall.sh                # 卸 ~/.local/bin 里的
#   bash uninstall.sh --dest DIR     # 卸指定目录里的
#   bash uninstall.sh -q             # 安静模式
set -u

DEST="${HOME}/.local/bin"
QUIET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:-}"; shift 2 ;;
    --dest=*) DEST="${1#--dest=}"; shift ;;
    -q|--quiet) QUIET=1; shift ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "用法：bash uninstall.sh [--dest DIR] [-q]" >&2; exit 2 ;;
  esac
done

say() { [ "$QUIET" = "1" ] || echo "$*"; }

SHIM="${DEST}/agentos"
if [ -e "$SHIM" ] || [ -L "$SHIM" ]; then
  # 删文件只走 unlink（精确路径、无通配、无 -f）：装机脚本里出现
  # 「rm 带 -f」是本仓明令禁止的形态（打错一个变量就能删掉不该删的）。
  if python3 -c 'import os,sys
p = sys.argv[1]
try:
    os.unlink(p)
except FileNotFoundError:
    pass' "$SHIM"
  then
    say "已删：$SHIM"
  else
    say "删不掉：$SHIM（自己看一眼）"
  fi
else
  say "本来就没有：$SHIM"
fi

# 去掉本脚本加的那两行（连注释一起）
for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
  [ -f "${RC}" ] || continue
  if grep -q 'AgentOS 终端 CLI' "${RC}" 2>/dev/null; then
    python3 - "${RC}" <<'PY'
import re, sys
p = sys.argv[1]
try:
    s = open(p, encoding="utf-8", errors="replace").read()
except OSError:
    raise SystemExit(0)
s = re.sub(r"\n# AgentOS 终端 CLI（bin/agentos 的真源在仓库里，shim 只做入口）\n"
           r"export PATH=\"[^\"]*:\$PATH\"\n", "\n", s)
open(p, "w", encoding="utf-8").write(s)
PY
    say "已清 PATH 行：${RC}"
  fi
done

say "终端 AgentOS 的 shim 已卸（仓库里的代码没动）"
exit 0
