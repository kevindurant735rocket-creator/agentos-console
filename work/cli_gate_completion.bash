# AgentOS 终端 CLI 的 bash 补全。由 `agentos completion bash` 自包含打印。
#
# 生效方式：source <(agentos completion bash)
# 常驻：把上一行追加进 ~/.bashrc（install.sh 装完后会打印同样一行）。

_agentos() {
  local cur prev cmd i
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  # 这个集合必须与 _AGENTOS_SUBS / argparse 的 17 个子命令逐项相等：
  # 2026-10-10 实测这里漏了 gh（用户敲 agentos gh <Tab> 什么都补不出来），
  # 而 --help 里明明有 —— cli_gate 的「补全与真实子命令不漂移」就是抓这个的。
  local subs="serve health open tui gates resident headroom term gh agent config completion sessions resume steer watch leader"
  local globals="-V --version -h --help -p --print --url --port --timeout --no-color --output-format"
  local gates="resident_gate headroom_gate flake_retry_selftest indep_field_gate neg_a11y freshness_gate shoot view_dod view_dod_selftest http_shot_gate snap_gate a11y_walk a11y_human neg_a11y_human ux_audit a11y_served projspace_gate act_gate promo_gate capmap_gate abort_gate abort_e2e intent_gate retro_gate goal_gate round_gate close_gate layout_gate env_gate web_gate native_gate native_gate_selftest agent_core_gate neg_agent_core at_selftest neg_at_selftest minimize_check gh_gate tui_walk_gate cli_gate cli_gate_selftest evolution_gate capability_gate evolution_gate_selftest perf_gate dsp_gate dsp_gate_selftest leader_gate leader_selftest evo_merge_gate evo_merge_selftest kbd_gate_selftest rewind_gate rewind_selftest subagent_gate subagent_selftest mcp_gate mcp_selftest hooks_gate hooks_selftest repl_ux_gate repl_ux_selftest branches_gate branches_selftest supervise_gate supervise_selftest safety_gate safety_selftest autonomy_gate autonomy_selftest boss_gate"
  local shells="zsh bash"
  local terms="cat date find git grep head ls pwd python3 stat tail wc whoami"

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
