# AgentOS 终端 CLI 的 bash 补全。由 `agentos completion bash` 自包含打印。
#
# 生效方式：source <(agentos completion bash)
# 常驻：把上一行追加进 ~/.bashrc（install.sh 装完后会打印同样一行）。

_agentos() {
  local cur prev cmd i
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  # 这个集合必须与 _AGENTOS_SUBS / argparse 的 21 个子命令逐项相等：
  # 2026-10-10 实测这里漏了 gh（用户敲 agentos gh <Tab> 什么都补不出来），
  # 而 --help 里明明有 —— cli_gate 的「补全与真实子命令不漂移」就是抓这个的。
  # 2026-10-19 复测：doctor/evolve（W22 加的子命令）也漏在这串里，
  # 同样「--help 里有、Tab 补不出来」—— 一起补上。
  local subs="gates selfscan gh agent config completion sessions steer watch leader doctor evolve replay"
  local globals="-V --version -h --help -p --print --url --port --timeout --no-color --output-format"
  local gates="历史备份，正确链从最早的"
  local shells="zsh bash"
  local terms=""

  cmd=""
  for ((i = 1; i < COMP_CWORD; i++)); do
    case "${COMP_WORDS[i]}" in
      -*) continue ;;
      *) cmd="${COMP_WORDS[i]}"; break ;;
    esac
  done

  if [ -z "$cmd" ]; then
    case "$cur" in
      -*) COMPREPLY=( $(compgen -W "$globals" -- "$cur") ); return 0 ;;
    esac
    COMPREPLY=( $(compgen -W "$subs" -- "$cur") )
    return 0
  fi

  case "$cmd" in
    gates) COMPREPLY=( $(compgen -W "$gates" -- "$cur") ) ;;
    completion) COMPREPLY=( $(compgen -W "$shells" -- "$cur") ) ;;
    term) COMPREPLY=( $(compgen -W "$terms" -- "$cur") ) ;;
    *) COMPREPLY=( $(compgen -W "$globals" -- "$cur") ) ;;
  esac
  return 0
}

complete -F _agentos agentos
