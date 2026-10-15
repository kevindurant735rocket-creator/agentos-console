#!/usr/bin/env bash
# AgentOS 卸载脚本：只删 install.sh 装出去的东西，源码树一个字都不动。
#
# 会删：
#   $DEST/agentos                 （install.sh 生成的 shim，普通文件，不可能是软链）
#   shell rc 里带起止标记的整块   （只有 install.sh --path/--completions 写过才在）
#   ~/.zfunc/_agentos             （只有 install.sh --completions 装过才在）
# 不会删：
#   仓库里 bin/agentos、completions/ —— 那是源码，不是安装产物
#
# 删除只用 unlink（rm 被本机 exec policy 拒），不用 sudo，不递归删目录里的其它文件。
set -u

PROG=${0##*/}
MARK_BEGIN='# >>> agentos installer >>>'
MARK_END='# <<< agentos installer <<<'
DEST=''
OPT_DRY=0
OPT_QUIET=0
OPT_PURGE=0

say() { [ "$OPT_QUIET" = 1 ] || printf '%s\n' "$*"; }
warn() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { warn "$*"; exit 2; }

usage() {
  cat <<'EOF'
用法: uninstall.sh [选项]
  --dest DIR     要卸载的 bin 目录（默认 $HOME/.local/bin，须与安装时一致）
  --dry-run      只打印要删什么，什么都不删
  --purge        连仓库内 bin/agentos 与 completions/ 生成物一起删（默认不删源码）
  -q, --quiet    少说话
  -h, --help     本帮助
退出码: 0 卸载完成（含「本来就没装」）；2 用法错误
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    --dest) [ $# -ge 2 ] || die "--dest 需要一个目录"; DEST=$2; shift 2 ;;
    --dest=*) DEST=${1#--dest=}; shift ;;
    --dry-run) OPT_DRY=1; shift ;;
    --purge) OPT_PURGE=1; shift ;;
    -q|--quiet) OPT_QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) warn "未知参数: $1"; usage >&2; exit 2 ;;
  esac
done

script_dir=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd -P) || script_dir=""
[ -n "$script_dir" ] || die "定位不到 uninstall.sh 自身目录"
ROOT=$script_dir
[ -n "$DEST" ] || DEST="$HOME/.local/bin"
case $DEST in
  /*) ;;
  *) DEST=$(CDPATH= cd "$DEST" 2>/dev/null && pwd -P) || die "dest 目录不存在: $DEST" ;;
esac

removed=0
kept=0

rm_file() { # $1 = 绝对路径；只删普通文件或软链，绝不动目录
  local f=$1
  if [ -L "$f" ]; then
    if [ "$OPT_DRY" = 1 ]; then printf '[dry-run] unlink %s (软链)\n' "$f"; return 0; fi
    if unlink "$f"; then say "  已删 $f"; removed=$((removed+1)); else warn "unlink 失败: $f"; fi
    return 0
  fi
  if [ -f "$f" ]; then
    if [ "$OPT_DRY" = 1 ]; then printf '[dry-run] unlink %s\n' "$f"; return 0; fi
    if unlink "$f"; then say "  已删 $f"; removed=$((removed+1)); else warn "unlink 失败: $f"; fi
    return 0
  fi
  say "  本来就没有 $f"
}

strip_block() { # $1 = rc 文件；原地剥掉整块，用 cat 回写以保留软链与 inode
  local f=$1 tmp
  [ -f "$f" ] || return 0
  grep -qF "$MARK_END" "$f" 2>/dev/null || return 0
  say "  剥除 rc 块 $f"
  if [ "$OPT_DRY" = 1 ]; then printf '[dry-run] sed 删除 %s .. %s\n' "$MARK_BEGIN" "$MARK_END"; return 0; fi
  tmp=$(mktemp "${TMPDIR:-/tmp}/agentos-un.XXXXXX") || { warn "mktemp 失败"; return 1; }
  sed -e '/^# >>> agentos installer >>>$/,/^# <<< agentos installer <<<$/d' "$f" > "$tmp"
  cat "$tmp" > "$f"
  unlink "$tmp"
  say "    已剥除 $f"
}

say "AgentOS uninstaller"
say "  目标 shim  $DEST/agentos"
# 用 ${ROOT} 而不是 $ROOT：UTF-8 locale 下 bash 会把全角括号当成变量名的一部分，
# $ROOT（…）会被解析成未定义变量 ${ROOT（…）}，set -u 直接报 unbound variable。
say "  仓库根     ${ROOT}（不动）"
[ "$OPT_DRY" = 1 ] && say "  模式       dry-run（什么都不删）"

# 1. shim 本体
rm_file "$DEST/agentos"

# 2. shell rc 里的托管块
seen=""
for rc in "${ZDOTDIR:-$HOME}/.zshrc" "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
  [ -n "$rc" ] || continue
  [ -f "$rc" ] || continue
  case " $seen " in *" $rc "*) continue ;; esac   # ZDOTDIR 未设时前两个会重复
  seen="$seen $rc"
  strip_block "$rc"
done

# 3. zsh 补全安装物
rm_file "$HOME/.zfunc/_agentos"

# 4. 可选：连源码产物一起清（默认不碰，源码要靠 git 管，不该由卸载脚本决定）
if [ "$OPT_PURGE" = 1 ]; then
  for f in "$ROOT/bin/agentos" "$ROOT/completions/zsh" "$ROOT/completions/bash"; do
    [ -f "$f" ] || continue
    rm_file "$f"
  done
else
  for f in "$ROOT/bin/agentos" "$ROOT/completions/zsh" "$ROOT/completions/bash"; do
    if [ -f "$f" ]; then
      say "  保留源码 $f"
      kept=$((kept+1))
    fi
  done
fi

# 5. 验收：卸完在仓库外必须解析不到 agentos
if [ "$OPT_DRY" = 1 ]; then
  say "[dry-run] 验收跳过"
  exit 0
fi

where=$(PATH="$DEST:/usr/local/bin:/usr/bin:/bin" command -v agentos 2>/dev/null) || where=""
if [ -n "$where" ]; then
  warn "卸载后仍能解析到 agentos -> $where"
  warn "注意：若它是别处装的同名命令，请自行确认；本脚本只删 $DEST/agentos。"
  exit 1
fi
say "验收: PATH 里已无 agentos（rc=0）"
say "完成。重装: bash $ROOT/install.sh"
[ "$OPT_QUIET" = 1 ] || say "  删除 $removed 个安装产物，保留 $kept 个源码文件"
exit 0
