#!/usr/bin/env bash
# Commit and push whatever the run added to the archive.
#
# Called with `if: always()` so a job that is cancelled or times out mid-session still keeps the
# snapshots it did manage to take — a partial day is worth far more than nothing, since none of
# this data can be re-fetched later.
#
# The push retries against a moving remote rather than failing: the workflows are serialised by
# a concurrency group, but a manual `workflow_dispatch` can still land alongside a scheduled run.
set -uo pipefail
MSG="${1:-archive update}"

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -A
if git diff --cached --quiet; then
  echo "nothing new to commit"
  exit 0
fi
git commit -q -m "${MSG} ($(date -u '+%Y-%m-%d %H:%M UTC'))"

for attempt in 1 2 3 4 5; do
  if git push -q; then
    echo "pushed on attempt ${attempt}"
    exit 0
  fi
  echo "push rejected, rebasing onto remote (attempt ${attempt})"
  git pull --rebase -q || { echo "rebase failed"; exit 1; }
  sleep $(( attempt * 5 ))
done

echo "could not push after 5 attempts"
exit 1
