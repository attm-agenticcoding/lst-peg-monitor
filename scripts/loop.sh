#!/usr/bin/env bash
# 在一个 run 里每 INTERVAL 秒采样一次（采样当场判断预警、需要时直接推送）；
# 历史每 COMMIT_EVERY 次提交一次（有推送时立刻提交）；预算用完后用 workflow_dispatch 接力下一个 run。
set -u
INTERVAL=${INTERVAL:-120}     # 2 分钟采样
COMMIT_EVERY=${COMMIT_EVERY:-5} # 10 分钟提交一次，避免每 2 分钟触发一次 Pages 构建
BUDGET=${BUDGET:-19800}       # 5.5 小时（job 超时 355 分钟，留余量给接力）
export URGENT_FLAG=${URGENT_FLAG:-/tmp/lpm-urgent}
end=$(( $(date +%s) + BUDGET ))
git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

commit_push() {
  [ -z "$(git status --porcelain data)" ] && return 0
  git add -A data
  git commit -q -m "data: snapshot $(date -u +%Y-%m-%dT%H:%MZ)"
  for i in 1 2 3; do
    # 顺带同步 main 上的新代码：推代码后最多 10 分钟生效
    git pull -q --rebase origin main || { git rebase --abort 2>/dev/null; echo "rebase 失败"; }
    git push -q origin HEAD:main && return 0
    echo "push 失败，重试 ($i/3)"; sleep 5
  done
}

i=0
lido_done_slot=''
while :; do
  t0=$(date +%s); i=$((i + 1))
  node scripts/snapshot.js || echo "::warning::本轮采样失败（exit $?）"
  lido_slot=$(( $(date +%s) / 43200 ))
  if [ "$lido_done_slot" != "$lido_slot" ]; then
    node scripts/lido-daily-dispatch.js
    lido_status=$?
    # Dispatch/consumed slot (10), or an API failure (1): do not repeat this slot
    # in this process. Active leases/runs remain eligible for a later check.
    [ "$lido_status" -ne 0 ] && lido_done_slot=$lido_slot
  fi
  if [ -f "$URGENT_FLAG" ] || [ $((i % COMMIT_EVERY)) -eq 0 ]; then
    rm -f "$URGENT_FLAG"
    commit_push
  fi
  next=$(( t0 + INTERVAL )); now=$(date +%s)
  [ "$next" -ge "$end" ] && break
  [ "$next" -gt "$now" ] && sleep $(( next - now ))
done

commit_push   # 交接前把最后几条也推上去
gh workflow run snapshot.yml --ref main && echo "已接力下一个 run"
