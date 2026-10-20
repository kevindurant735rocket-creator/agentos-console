#!/usr/bin/env bash
# install.sh —— 把**终端 AgentOS 的 CLI shim**装到目标目录。
#
# 2026-10-20 起，这个仓库只剩终端面（网页驾驶舱与原生 macOS app 已下线）。
# 所以本脚本**只做一件事**：把 `agentos` 这个 shim 放到 PATH 上。
#
# 明确不做（且以后也不做）：
#   · 不装原生 app、不写 launchd、不装常驻服务
#   · 不开浏览器、不注册 URL scheme
#   · 不往系统目录写东西（默认只写 ~/.local/bin）
#
# 用法：
#   bash install.sh                 # 装到 ~/.local/bin 并在 shell 配置里加一行 PATH
#   bash install.sh --dest DIR      # 装到指定目录（不碰 shell 配置）
#   bash install.sh -q              # 安静模式
set -u

DEST="${HOME}/.local/bin"
QUIET=0
ADD_PATH=1
ROOT="$(cd "$(dirname "$0")" && pwd -P)"

while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:-}"; shift 2; ADD_PATH=0 ;;
    --dest=*) DEST="${1#--dest=}"; shift; ADD_PATH=0 ;;
    -q|--quiet) QUIET=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "用法：bash install.sh [--dest DIR] [-q]" >&2; exit 2 ;;
  esac
done

say() { [ "$QUIET" = "1" ] || echo "$*"; }

if [ ! -f "${ROOT}/bin/agentos" ]; then
  echo "FAIL：${ROOT}/bin/agentos 不存在（仓库不完整？）" >&2
  exit 1
fi

mkdir -p "${DEST}" || { echo "FAIL：建不了 ${DEST}" >&2; exit 1; }
SHIM_OUT="${DEST}/agentos"

# 生成式 shim：把 __AGENTOS_ROOT__ 换成真实仓根（不做软链：软链的
# $0 解析、python 选择、仓库搬迁都不可控，卸载时也说不清该删谁）。
sed "s|__AGENTOS_ROOT__|${ROOT}|g" "${ROOT}/bin/agentos" > "${SHIM_OUT}.tmp" \
  || { echo "FAIL：写 shim 失败" >&2; exit 1; }
chmod 755 "${SHIM_OUT}.tmp" || { echo "FAIL：chmod 失败" >&2; exit 1; }
mv "${SHIM_OUT}.tmp" "${SHIM_OUT}" || { echo "FAIL：落 shim 失败" >&2; exit 1; }

# PATH 一行（只在默认目录时装，且幂等）
if [ "$ADD_PATH" = "1" ]; then
  for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [ -f "${RC}" ] || continue
    grep -q 'AgentOS 终端 CLI' "${RC}" 2>/dev/null && continue
    {
      echo ""
      echo "# AgentOS 终端 CLI（bin/agentos 的真源在仓库里，shim 只做入口）"
      echo "export PATH=\"${DEST}:\$PATH\""
    } >> "${RC}"
  done
fi

# 自检：装完的 shim 真能跑
if "${SHIM_OUT}" --version >/dev/null 2>&1; then
  say "已装：${SHIM_OUT}（终端 AgentOS；没有 app、没有常驻服务、没有浏览器）"
  say "用法：agentos agent            # 进终端 agent"
  say "      agentos agent -p '需求'   # headless 一次跑完"
  exit 0
fi
echo "FAIL：装完了但 shim 跑不起来（自己先查：${SHIM_OUT} --version）" >&2
exit 1
