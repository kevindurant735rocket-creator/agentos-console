#!/usr/bin/env bash
# cleanup_study_repos.sh —— 删除误部署到 GitHub 的自建派生仓库
#
# 背景（2026-10-19 事故）：曾把 AgentOS 自建的派生小 CLI（agentos-study-cli*）
# 推成 8 个 public 仓库。那是方向性错误——自有内容永不上传 GitHub。
# 已在无授权情况下先行转 PRIVATE 止血（repo scope 足够）；删除需要
# delete_repo scope。
#
# 用法：
#   gh auth refresh -h github.com -s delete_repo     # 一次性授权（会打印一次性码）
#   bash tools/cleanup_study_repos.sh                 # 删除 + 逐个验 404
#
# 退出码：0 = 全部已删并验 404；1 = 仍有残留或 scope 不足。
set -uo pipefail

OWNER="${OWNER:-kevindurant735rocket-creator}"
REPOS=(
  agentos-study-cli
  agentos-study-cli2
  agentos-study-cli3
  agentos-study-cli4
  agentos-study-cli5
  agentos-study-cli6
  agentos-study-cli7
  agentos-study-cli8
)

echo "== 前置检查：token 是否有 delete_repo scope =="
scopes="$(gh auth status 2>&1 | grep -i 'Token scopes' || true)"
echo "  $scopes"
case "$scopes" in
  *delete_repo*) echo "  scope 就绪" ;;
  *)
    echo "  缺 delete_repo scope。请先执行："
    echo "    gh auth refresh -h github.com -s delete_repo"
    exit 1
    ;;
esac

fail=0
for r in "${REPOS[@]}"; do
  repo="$OWNER/$r"
  if gh repo delete "$repo" --yes >/dev/null 2>&1; then
    echo "  已删除 $repo"
  else
    echo "  删除失败 $repo（继续试下一个）"
    fail=1
  fi
done

echo "== 复核（期望全部 404）=="
for r in "${REPOS[@]}"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "https://api.github.com/repos/$OWNER/$r")"
  if [ "$code" = "404" ]; then
    echo "  OK  $r -> 404"
  else
    echo "  残留 $r -> HTTP $code（仍需处理）"
    fail=1
  fi
done

if [ "$fail" = "0" ]; then
  echo "CLEANUP_OK"
  exit 0
fi
echo "CLEANUP_RED"
exit 1
