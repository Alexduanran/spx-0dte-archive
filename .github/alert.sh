#!/usr/bin/env bash
# Raise, update, or clear the one standing issue that tracks archive health.
#
# One issue, reused — not one per failure. The thing being tracked is a condition ("the 1-minute
# tier has a hole"), not an event, and a new issue every twelve hours would bury the signal it
# exists to carry. A repeat is only worth a comment when the set of failing dates has actually
# changed; otherwise the standing issue already says everything true.
set -uo pipefail

TITLE='Archive health: 1-minute tier has a gap'
REPORT="${REPORT:-report.txt}"
RESOLVE=0
[ "${1:-}" = '--resolve' ] && RESOLVE=1

num=$(gh issue list --state open --limit 50 --json number,title \
        --jq ".[] | select(.title == \"${TITLE}\") | .number" | head -1)

if [ "$RESOLVE" = 1 ]; then
  if [ -n "$num" ]; then
    gh issue comment "$num" --body "Recovered — every session in the window now holds a full 1-minute set.

\`\`\`
$(cat "$REPORT")
\`\`\`"
    gh issue close "$num"
    echo "closed #${num}"
  else
    echo "healthy, nothing open"
  fi
  exit 0
fi

# The FAIL line names exactly which sessions are bad; it is the fingerprint of this condition.
fail=$(grep -m1 '^FAIL:' "$REPORT" || echo 'FAIL: (no summary line)')

if [ -z "$num" ]; then
  gh issue create --title "$TITLE" --body "$(cat <<BODY
A scheduled audit found the 1-minute tier incomplete, and re-running \`fetch_spx_bars.py\` did
not recover it. 1-minute bars are the hard requirement here and Yahoo only serves them for 30
days, so a gap that persists is on a clock — once a date leaves that window it cannot be
recovered at that resolution ever.

${fail}

\`\`\`
$(cat "$REPORT")
\`\`\`

This issue is reused for as long as the condition lasts and closes itself once the window is
clean again.
BODY
)"
  echo "opened a new issue"
  exit 0
fi

# Already open: say something only if the failing dates changed.
if gh issue view "$num" --json body,comments \
     --jq '.body, (.comments[].body)' | grep -qF "$fail"; then
  echo "#${num} already reports: ${fail}"
else
  gh issue comment "$num" --body "Still failing, and the affected sessions have changed.

${fail}

\`\`\`
$(cat "$REPORT")
\`\`\`"
  echo "commented on #${num}"
fi
