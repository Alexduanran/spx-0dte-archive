#!/usr/bin/env python3
"""
Audit what the archive actually holds, and fail loudly when the 1-minute tier has a hole.

WHY THIS EXISTS. Twice now the collection has broken while every workflow run reported success,
and both times it took a week to notice. On 2026-08-28 the schedule fired after the close, so
six green runs captured nothing. On 2026-09-24 CBOE started answering with a 307 and curl was
not following redirects, so every fetch failed the size guard — again six green runs, again
silent. A green CI dashboard says a job ran, not that data landed. Only the archive knows.

WHAT IT CHECKS, AND THE ASYMMETRY THAT SHAPES IT.

  1-minute bars are the hard requirement and they are RECOVERABLE for 30 days: Yahoo still
  serves them, so a hole found inside that window can simply be re-fetched. A missing date is
  therefore an error worth waking someone for, and worth trying to fix automatically — which is
  what the workflow does with the exit code.

  Gamma exposure is best-effort and NOT recoverable at all: there is no historical source, so a
  missed quarter hour is gone before this check ever runs. Alerting on it would be noise with no
  remedy, so it is reported for information and never fails the run.

TRADING DAYS COME FROM YAHOO, NOT A HOLIDAY TABLE. Asking the archive which days should exist is
circular — if collection stopped entirely, every tier is missing the date and a table-free check
would shrug and call it a holiday. The daily ^GSPC series is the authority: a date appears there
if and only if there was a session. It costs about 10 KB and needs no yearly maintenance.

  python3 healthcheck.py            # audit the 30-day 1m window
  python3 healthcheck.py --days 60  # widen it (5m territory)

Exit 0 when every trading day in the window holds a plausible 1-minute session; 1 otherwise.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = 'Mozilla/5.0'
DAILY = ('https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC'
         '?interval=1d&range=3mo')

# A full session is 391 one-minute bars; an early close (1pm) is about 211. Anything at or above
# the half-day floor is plausible, so the two are separated rather than lumped into one threshold
# — a genuine half day must not read as a failure, and a truncated full day must not read as one
# of those.
FULL_DAY_MIN = 380
HALF_DAY_MIN = 200

# Snapshots per session if nothing is missed: 09:30 through 16:00 on the quarter hour.
GEX_SLOTS = 27


def et_now():
    """Current US/Eastern time, computed from the DST rules so no tz database is needed."""
    utc = datetime.utcnow()
    year = utc.year

    def nth_dow(month, dow, n):
        d = datetime(year, month, 1)
        return d + timedelta(days=(dow - d.weekday()) % 7 + 7 * (n - 1))

    start = nth_dow(3, 6, 2).replace(hour=7)
    end = nth_dow(11, 6, 1).replace(hour=6)
    return utc - timedelta(hours=4 if start <= utc < end else 5)


def trading_days(attempts=3):
    """Dates Yahoo's daily ^GSPC series carries — i.e. the days a session actually happened."""
    last = ''
    for attempt in range(1, attempts + 1):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        r = subprocess.run(['curl', '-sL', '--max-time', '60', '-w', '%{http_code}',
                            '-H', 'User-Agent: ' + UA, DAILY, '-o', path],
                           capture_output=True, text=True)
        code = (r.stdout or '').strip() or '?'
        try:
            if r.returncode == 0:
                with open(path) as f:
                    doc = json.load(f)
                res = doc['chart']['result'][0]
                off = res['meta'].get('gmtoffset', 0)
                q = res['indicators']['quote'][0]
                out = []
                for i, ts in enumerate(res['timestamp']):
                    if q['close'][i] is None:
                        continue
                    out.append(datetime.utcfromtimestamp(ts + off).strftime('%Y-%m-%d'))
                if out:
                    return sorted(set(out))
                last = 'HTTP %s, no dated bars' % code
            else:
                last = 'curl rc=%s HTTP %s' % (r.returncode, code)
        except (ValueError, KeyError, IndexError) as exc:
            last = 'HTTP %s, unparseable (%s)' % (code, exc)
        finally:
            os.unlink(path)
        if attempt < attempts:
            time.sleep(attempt * 5)
    raise SystemExit('cannot establish the trading calendar: %s' % last)


def bar_count(res, date):
    p = os.path.join(ROOT, 'spx-%s' % res, '%s.csv' % date)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return sum(1 for _ in f) - 1


def gex_counts():
    p = os.path.join(ROOT, 'gex', 'summary.csv')
    out = {}
    if os.path.exists(p):
        with open(p) as f:
            for row in csv.DictReader(f):
                out[row['date']] = out.get(row['date'], 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30,
                    help='window to audit, in calendar days (default 30 — the 1m recovery window)')
    args = ap.parse_args()

    now = et_now()
    today = now.strftime('%Y-%m-%d')
    floor = (now - timedelta(days=args.days)).strftime('%Y-%m-%d')

    days = [d for d in trading_days() if d >= floor]
    # Today is only auditable once the session has settled and the post-close job has had a
    # chance to run; before that a missing file is expected, not a fault.
    if days and days[-1] == today and now.hour * 60 + now.minute < 17 * 60:
        days.pop()
    if not days:
        print('nothing to audit in the last %d days' % args.days)
        return 0

    gex = gex_counts()
    bad, short, rows = [], [], []
    for d in days:
        n1 = bar_count('1m', d)
        if n1 is None:
            state = 'MISSING'
            bad.append(d)
        elif n1 < HALF_DAY_MIN:
            state = 'TRUNCATED'
            bad.append(d)
        elif n1 < FULL_DAY_MIN:
            state = 'half day?'
            short.append(d)
        else:
            state = 'ok'
        rows.append((d, n1, bar_count('5m', d), bar_count('1h', d),
                     gex.get(d, 0), state))

    print('1-minute audit, %s → %s  (%d sessions)' % (days[0], days[-1], len(days)))
    print()
    print('  date        1m    5m   1h   GEX    1m state')
    for d, n1, n5, nh, g, state in rows:
        print('  %s  %4s  %4s  %3s  %2d/%d   %s'
              % (d, '—' if n1 is None else n1, '—' if n5 is None else n5,
                 '—' if nh is None else nh, g, GEX_SLOTS, state))
    print()

    gtot = sum(r[4] for r in rows)
    print('GEX (informational — no historical source, so a gap here is already permanent): '
          '%d/%d slots, %.0f%%' % (gtot, GEX_SLOTS * len(days),
                                   100.0 * gtot / (GEX_SLOTS * len(days))))
    blank = [r[0] for r in rows if r[4] == 0]
    if blank:
        print('  sessions with no gamma at all: %s' % ', '.join(blank))

    if short:
        print('\nplausible early closes (between %d and %d bars), not treated as faults: %s'
              % (HALF_DAY_MIN, FULL_DAY_MIN, ', '.join(short)))

    if bad:
        print('\nFAIL: %d session(s) missing or truncated at 1-minute: %s'
              % (len(bad), ', '.join(bad)))
        print('These are still inside Yahoo\'s %d-day window, so `python3 fetch_spx_bars.py`'
              ' should recover them.' % args.days)
        return 1

    print('\nOK: every session in the window holds a full 1-minute set.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
