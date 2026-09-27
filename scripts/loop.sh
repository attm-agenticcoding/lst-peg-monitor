#!/usr/bin/env bash
# 在一个 run 里每 INTERVAL 秒取一次快照；预算用完后用 workflow_dispatch 接力下一个 run。
set -u
INTERVAL=${INTERVAL:-600}   # 10 分钟
BUDGET=${BUDGET:-19800}     # 5.5 小时（job 超时 355 分钟，留余量给接力）
end=$(( $(date +%s) + BUDGET ))
git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

while :; do
  t0=$(date +%s)
  # 先同步：本轮用的就是 main 上最新的代码，推代码后最多 10 分钟生效
  git pull -q --rebase origin main || { git rebase --abort 2>/dev/null; git fetch -q origin main && git reset -q --hard origin/main; }
  node scripts/snapshot.js || echo "::warning::本轮快照失败（exit $?）"
  if [ -n "$(git status --porcelain data)" ]; then
    git add -A data
    git commit -q -m "data: snapshot $(date -u +%Y-%m-%dT%H:%MZ)"
    for i in 1 2 3; do
      git push -q origin HEAD:main && break
      echo "push 失败，rebase 后重试 ($i/3)"; sleep 5
      git pull -q --rebase origin main || true
    done
  fi
  next=$(( t0 + INTERVAL )); now=$(date +%s)
  [ "$next" -ge "$end" ] && break
  [ "$next" -gt "$now" ] && sleep $(( next - now ))
done

gh workflow run snapshot.yml --ref main && echo "已接力下一个 run"
