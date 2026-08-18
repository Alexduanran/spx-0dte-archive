#!/usr/bin/env python3
"""
Drive fetch_gex_poll.py on a quarter-hour cadence for one stretch of the trading session.

WHY THIS EXISTS RATHER THAN A `*/15` CRON: GitHub Actions' scheduled triggers are best-effort.
Under load a `schedule` event routinely fires 5-20 minutes late and can be dropped outright, so
a 15-minute cron would yield a ragged, gap-ridden series — exactly the failure mode the GEX
archive cannot tolerate, since a missed snapshot has no historical source to recover it from.

Instead one workflow run holds a single job open for its half of the session and does its own
sleeping. The trigger only has to land *somewhere* near the start; every snapshot after that is
aligned to a real quarter hour by this loop, not by the scheduler.

The job is split morning/afternoon because a GitHub Actions job is capped at 6 hours and a full
09:20-16:10 ET session under EST (when the UTC cron fires an hour early in local terms) would
run 6h50m.

  python3 gex_session.py --until 12:35            # morning half
  python3 gex_session.py --until 16:10 --commit   # afternoon half, pushing as it goes

--commit pushes after every snapshot rather than once at the end. A job holds open for three
and a half hours, and an uncommitted runner that dies takes the whole half-session with it —
data that has no source to re-fetch from. Committing per snapshot caps that loss at 15 minutes.

Exits immediately, and successfully, on weekends and US market holidays — the cron fires every
weekday and it is this script's job to decide there is nothing to do.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_gex_poll import et_now, US_HOLIDAYS_2026   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
POLLER = os.path.join(HERE, 'fetch_gex_poll.py')
COMMITTER = os.path.join(HERE, '.github', 'commit-archive.sh')


def snapshot():
    """Run one poll. The poller self-guards the session window, so this is safe any time."""
    r = subprocess.run([sys.executable, POLLER], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(out or '(no output)', flush=True)
    return r.returncode == 0


def commit(label):
    """Push what we have so far. Never fatal — a failed push must not end the session."""
    r = subprocess.run(['bash', COMMITTER, 'gex snapshot ' + label],
                       capture_output=True, text=True)
    print((r.stdout + r.stderr).strip() or '(commit: no output)', flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--until', required=True, help='ET clock time to stop at, HH:MM')
    ap.add_argument('--every', type=int, default=15, help='cadence in minutes')
    ap.add_argument('--commit', action='store_true',
                    help='commit and push after each snapshot (CI); off for local runs')
    args = ap.parse_args()

    hh, mm = (int(x) for x in args.until.split(':'))
    cutoff = hh * 60 + mm

    now = et_now()
    if now.weekday() >= 5:
        print('skip: weekend (%s ET)' % now.strftime('%Y-%m-%d %H:%M'))
        return 0
    if now.strftime('%Y-%m-%d') in US_HOLIDAYS_2026:
        print('skip: market holiday (%s ET)' % now.strftime('%Y-%m-%d'))
        return 0

    print('gex_session: %s ET, polling every %dm until %s ET'
          % (now.strftime('%Y-%m-%d %H:%M'), args.every, args.until), flush=True)

    taken = 0
    while True:
        now = et_now()
        if now.hour * 60 + now.minute >= cutoff:
            break
        # sleep to the next exact multiple of --every past the hour, so snapshots land on
        # 09:30 / 09:45 / 10:00 ... regardless of how late the workflow was triggered
        wait = args.every * 60 - ((now.minute % args.every) * 60 + now.second)
        time.sleep(wait)

        now = et_now()
        if now.hour * 60 + now.minute >= cutoff:
            break
        snapshot()
        taken += 1
        if args.commit:
            commit(now.strftime('%Y-%m-%d %H:%M ET'))

    print('gex_session: done, %d snapshots attempted' % taken)
    return 0


if __name__ == '__main__':
    sys.exit(main())
