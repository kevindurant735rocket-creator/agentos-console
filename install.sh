#!/usr/bin/env bash
# AgentOS 装机脚本：把一个生成式 shim 装到 ~/.local/bin/agentos。
#
# 设计约束（写在前头，改代码前先读）：
#   1. 幂等。第二次跑，产物与第一次逐字节相同（B2 验收就是这句话）。
#   2. 不碰源码树里除 completions/ 以外的任何文件。
#   3. 默认不改任何 shell rc 文件 —— 只打印 PATH 建议。要写 rc 必须显式 --path/--completions，
#      且写入的是一整块带起止标记的内容，安装时「先剥后贴」，永远不会贴出第二份。
#   4. 不用 sudo，不用 readlink -f，不用 rm（删临时文件用 unlink）。
#   5. 项目根从脚本自身位置推导，不硬编码 /Users/zhangfengrui。
set -u

PROG=${0##*/}
MARK_BEGIN='# >>> agentos installer >>>'
MARK_END='# <<< agentos installer <<<'
DEST=''
OPT_PATH=0
OPT_COMP=0
OPT_REFRESH=0
OPT_DRY=0
OPT_QUIET=0
OPT_UNINST=0

say() { [ "$OPT_QUIET" = 1 ] || printf '%s\n' "$*"; }
warn() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { warn "$*"; exit 2; }

usage() {
  cat <<'EOF'
用法: install.sh [选项]
  --dest DIR       装到哪个 bin 目录（默认 $HOME/.local/bin）
  --path           幂等地把 DEST 追加进 shell rc（默认只打印建议，不动 rc）
  --completions    额外装 zsh 补全到 ~/.zfunc/_agentos 并写 rc（同样幂等）
  --refresh-completions  用 `agentos completion` 重新生成仓库内 completions/
  --dry-run        只打印要做什么，什么都不写
  -q, --quiet      少说话
  --uninstall      等价于跑 uninstall.sh
  -h, --help       本帮助
退出码: 0 成功；2 用法/环境错误；1 自检失败（shim 装上了但跑不起来）
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    --dest) [ $# -ge 2 ] || die "--dest 需要一个目录"; DEST=$2; shift 2 ;;
    --dest=*) DEST=${1#--dest=}; shift ;;
    --path) OPT_PATH=1; shift ;;
    --completions) OPT_COMP=1; shift ;;
    --refresh-completions) OPT_REFRESH=1; shift ;;
    --dry-run) OPT_DRY=1; shift ;;
    -q|--quiet) OPT_QUIET=1; shift ;;
    --uninstall) OPT_UNINST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) warn "未知参数: $1"; usage >&2; exit 2 ;;
  esac
done

# env -i 干净 shell 里 PATH 只有 /usr/bin:/bin，Python.framework 与 homebrew 都看不见。
# 这里只改本进程的 PATH，不动用户的 rc —— 让「干净环境也能装」成立。
PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
export PATH

# 项目根 = install.sh 所在目录（install.sh 在仓库根）。dirname 不用 --，BSD dirname 不认。
script_dir=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd -P) || script_dir=""
[ -n "$script_dir" ] || die "定位不到 install.sh 自身目录"
ROOT=$script_dir
[ -f "$ROOT/tools/agentos.py" ] || die "$ROOT/tools/agentos.py 不存在：install.sh 必须放在仓库根"
[ -f "$ROOT/bin/agentos" ] || die "$ROOT/bin/agentos 不存在：shim 模板缺失"

if [ "$OPT_UNINST" = 1 ]; then
  # 必须把已解析的 --dest 显式透传：用户写 `install.sh --uninstall --dest ~/bin`
  # 时 $@ 里的 --dest 已被 shift 吃掉，不补传就会去卸默认的 ~/.local/bin。
  exec bash "$ROOT/uninstall.sh" --dest "$DEST" "$@"
fi

[ -n "$DEST" ] || DEST="$HOME/.local/bin"
case $DEST in
  /*) ;;
  *) DEST=$(CDPATH= cd "$DEST" 2>/dev/null && pwd -P) || die "dest 目录不存在: ${DEST}（先用 mkdir -p 建好）" ;;
esac

run() {
  if [ "$OPT_DRY" = 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

say "AgentOS installer"
say "  仓库根     $ROOT"
say "  安装到     $DEST/agentos"
say "  python     $(command -v python3 2>/dev/null || printf '(未找到，将用仓库内虚拟环境)')"
[ "$OPT_DRY" = 1 ] && say "  模式       dry-run（不写任何文件）"

# ---- 1. 建目标目录 ---------------------------------------------------------
if [ -d "$DEST" ]; then
  [ -w "$DEST" ] || die "$DEST 不可写。换个目录：install.sh --dest ~/bin"
else
  run mkdir -p "$DEST" || die "建不了 ${DEST}（这就是停止条件：换 --dest，别强写别处）"
  [ "$OPT_DRY" = 1 ] || [ -w "$DEST" ] || die "$DEST 刚建好但不可写"
fi

# ---- 2. 生成 shim：把模板里的 __AGENTOS_ROOT__ 换成真实仓库根 --------------
# 用 awk 而不是 sed：路径里出现 & 或 \ 时 sed 的替换串会变形；awk 只多一层
# 「含 & 或反斜杠就拒绝」的显式检查，比静默生成坏文件好。
case $ROOT in
  *'&'*|*'\'*) die "仓库根里含 & 或反斜杠，无法安全生成 shim: $ROOT" ;;
esac
tmp_shim="$DEST/.agentos.$$"
if [ "$OPT_DRY" = 1 ]; then
  printf '[dry-run] %s\n' "生成 $tmp_shim 并安装为 $DEST/agentos (mode 0755)"
else
  ROOT="$ROOT" awk '{ gsub(/__AGENTOS_ROOT__/, ENVIRON["ROOT"]); print }' \
    "$ROOT/bin/agentos" > "$tmp_shim" || { unlink "$tmp_shim" 2>/dev/null; die "生成 shim 失败"; }
  chmod 0755 "$tmp_shim" || { unlink "$tmp_shim" 2>/dev/null; die "chmod 0755 失败"; }
  # 先杀掉可能存在的同名软链/目录，再原子替换，落盘一定是普通文件
  if [ -L "$DEST/agentos" ] || [ -d "$DEST/agentos" ]; then
    unlink "$DEST/agentos" 2>/dev/null || true
  fi
  mv -f "$tmp_shim" "$DEST/agentos" || { unlink "$tmp_shim" 2>/dev/null; die "安装到 $DEST/agentos 失败"; }
fi

# ---- 3. completions：仓库里没有就用 CLI 生成；--refresh 强制重生成 --------
for sh in zsh bash; do
  target="$ROOT/completions/$sh"
  if [ -f "$target" ] && [ "$OPT_REFRESH" = 0 ]; then
    continue
  fi
  if [ ! -d "$ROOT/completions" ]; then
    run mkdir -p "$ROOT/completions" || true
  fi
  if [ ! -w "$ROOT/completions" ]; then
    say "  跳过补全生成：$ROOT/completions 不可写（不影响 shim 安装）"
    continue
  fi
  if command -v python3 >/dev/null 2>&1 && python3 "$ROOT/tools/agentos.py" completion "$sh" </dev/null >"$target.tmp" 2>/dev/null; then
    chmod 0644 "$target.tmp"
    mv -f "$target.tmp" "$target"
    say "  生成补全 completions/$sh"
  else
    unlink "$target.tmp" 2>/dev/null || true
    say "  跳过补全生成（agentos completion $sh 失败），沿用已有文件"
  fi
done

# ---- 4. 自检：装完必须真能跑，不能只宣告 --------------------------------
if [ "$OPT_DRY" = 1 ]; then
  say "[dry-run] 自检跳过"
else
  ver=$("$DEST/agentos" --version </dev/null 2>&1)
  vrc=$?
  if [ $vrc -ne 0 ]; then
    warn "自检失败：$DEST/agentos --version rc=$vrc"
    warn "输出: $ver"
    exit 1
  fi
  say "  自检       $DEST/agentos --version rc=0 -> $ver"
  # shim 必须是普通可执行文件，不是软链（软链会让「装了什么」说不清）
  if [ -L "$DEST/agentos" ]; then
    warn "$DEST/agentos 竟然是软链，装机契约被破坏"
    exit 1
  fi
fi

# ---- 5. 可选的 shell rc 改动：一整块，先剥后贴，永远只有一份 -------------
rc_pick() {
  if [ -n "${ZDOTDIR:-}" ] && [ -f "$ZDOTDIR/.zshrc" ]; then printf '%s' "$ZDOTDIR/.zshrc"; return 0; fi
  if [ -f "$HOME/.zshrc" ]; then printf '%s' "$HOME/.zshrc"; return 0; fi
  if [ -f "$HOME/.bashrc" ]; then printf '%s' "$HOME/.bashrc"; return 0; fi
  if [ -f "$HOME/.bash_profile" ]; then printf '%s' "$HOME/.bash_profile"; return 0; fi
  return 1
}

strip_block() { # $1 = rc 文件；原地剥掉整块，用 cat 回写以保留软链与 inode
  local f=$1 tmp
  [ -f "$f" ] || return 0
  grep -qF "$MARK_END" "$f" 2>/dev/null || return 0
  tmp=$(mktemp "${TMPDIR:-/tmp}/agentos-rc.XXXXXX") || return 1
  sed -e '/^# >>> agentos installer >>>$/,/^# <<< agentos installer <<<$/d' "$f" > "$tmp"
  cat "$tmp" > "$f"
  unlink "$tmp"
}

if [ "$OPT_PATH" = 1 ] || [ "$OPT_COMP" = 1 ]; then
  rc=$(rc_pick) || rc=""
  if [ -z "$rc" ]; then
    warn "找不到 ~/.zshrc / ~/.bashrc / ~/.bash_profile，rc 写入跳过（PATH 建议见下方打印）"
  else
    say "  写 rc       $rc"
    if [ "$OPT_DRY" = 1 ]; then
      printf '[dry-run] %s\n' "剥除旧块后追加新块到 $rc"
    else
      strip_block "$rc" || warn "剥除旧块失败: $rc"
      {
        printf '%s\n' "$MARK_BEGIN"
        if [ "$OPT_PATH" = 1 ]; then
          printf 'export PATH="%s:$PATH"\n' "$DEST"
        fi
        if [ "$OPT_COMP" = 1 ]; then
          printf 'fpath=(~/.zfunc $fpath)\n'
          printf 'autoload -Uz compinit && compinit\n'
        fi
        printf '%s\n' "$MARK_END"
      } >> "$rc" || warn "写入 $rc 失败"
    fi
  fi
fi

if [ "$OPT_COMP" = 1 ]; then
  zfunc="$HOME/.zfunc"
  if [ "$OPT_DRY" = 1 ]; then
    printf '[dry-run] %s\n' "mkdir -p $zfunc; 安装补全到 $zfunc/_agentos"
  else
    if mkdir -p "$zfunc" 2>/dev/null && [ -w "$zfunc" ]; then
      if [ -f "$ROOT/completions/zsh" ]; then
        install -m 0644 "$ROOT/completions/zsh" "$zfunc/_agentos" 2>/dev/null \
          && say "  补全       $zfunc/_agentos" \
          || warn "装补全到 $zfunc/_agentos 失败（不影响 shim）"
      else
        warn "$ROOT/completions/zsh 不存在，跳过 zsh 补全安装"
      fi
    else
      warn "$zfunc 不可写，跳过 zsh 补全安装"
    fi
  fi
fi

# ---- 6. 收尾：把「用户还得自己做什么」说清楚 -----------------------------
say ""
say "完成。卸载: bash $ROOT/uninstall.sh"
if command -v agentos >/dev/null 2>&1; then
  say "agentos 已在 PATH 中: $(command -v agentos)"
else
  say "$DEST 不在当前 PATH 里，把下面这行加进你的 shell rc 然后重开终端："
  say "  export PATH=\"$DEST:\$PATH\""
  say "或下次安装时带上 --path 让 install.sh 幂等代写。"
fi
say "补全:   source $ROOT/completions/bash      (bash)"
say "        source $ROOT/completions/zsh       (zsh；要常驻带 --completions)"
exit 0
