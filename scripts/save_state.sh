#!/usr/bin/env bash
# 把 .state 里的历史记录（history.jsonl）和推送记录（state.json）提交到 state 分支。
# state 分支只放这些数据，和代码历史无关。
set -euo pipefail

: "${GH_TOKEN:?}" "${REPO:?}"
cd .state
if [ ! -d .git ]; then
  # 第一次运行：state 分支还不存在，新建一个只含数据的孤立分支
  git init -q
  git checkout -q --orphan state
fi
git config user.name "vps-monitor"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then
  echo "state 没有变化"
  exit 0
fi
git commit -q -m "监控数据 $(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M')"
# 报告已经推送出去了，这里保存失败的话，下一路兜底触发读不到「已推送」，会再推一遍，
# 所以遇到网络抖动要重试。同一时间只有一次运行在写 state 分支，不会有冲突
for attempt in 1 2 3; do
  # token 只在这一条命令里出现在 URL 中，不写进 git 配置
  if git push -q "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" HEAD:refs/heads/state; then
    echo "state 已保存"
    exit 0
  fi
  if [ "$attempt" -lt 3 ]; then
    echo "state 保存失败，$((attempt * 10)) 秒后重试" >&2
    sleep $((attempt * 10))
  fi
done
echo "state 保存失败，已重试 3 次" >&2
exit 1
