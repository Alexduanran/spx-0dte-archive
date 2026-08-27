#!/usr/bin/env python3
"""
Poll SPX 0DTE gamma exposure every 15 minutes and archive it.

WHY IT POLLS INSTEAD OF SNAPSHOTTING ONCE: the 0DTE gamma profile is not a property of the
day, it is a property of the *moment*. Near the open it is spread across strikes; as expiry
approaches it collapses into a spike at the at-the-money strike. A single daily snapshot
cannot tell you whether spot was being pulled toward a gamma magnet or pushed away from one.
Intraday evolution is the signal.

SOURCE: CBOE's public delayed-quote feed, `cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json`.
No authentication, ~13 MB per pull, roughly 15 minutes delayed. It carries per-contract
`gamma`, `open_interest` and `volume`, so gamma exposure is computed here from first
principles rather than taken on trust.

  0DTE lives under the **SPXW** root, not SPX. `_SPX.json` contains both (SPX = monthlies,
  SPXW = dailies/weeklies); filtering to root == 'SPX' silently drops every 0DTE contract.
  There is no separate `_SPXW.json` — that path returns 403.

VALIDATED against Option Alpha's own GEX view on 2026-08-13: same peak-gamma strike (7800),
same non-zero gamma band, and identical open interest at the peak (6376 calls / 537 puts).
Dollar magnitudes differ by a constant scale factor because OA normalises differently — the
series here is internally consistent, which is what matters for research.

WHY NOT OPTION ALPHA'S ENDPOINT: `a5.to.market.gex` requires an authenticated browser session
that expires during the day, and it has no history — `market.gex('SPX', <date>)` errors out.
This script depends on nothing but the network.

Outputs (both append-only, never overwritten):
  data/gex/summary.csv                  one row per snapshot — the derived levels
  data/gex/profile/YYYY-MM-DD_HHMM.csv  near-the-money gamma/OI profile at that moment

Run it as often as you like; it no-ops outside the regular session, so a plain
`StartInterval: 900` launchd job is safe. See data/README.md for the launchd setup.
"""

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
GEX = os.path.join(ROOT, 'gex')
PROF = os.path.join(GEX, 'profile')
SUMMARY = os.path.join(GEX, 'summary.csv')
URL = 'https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json'
UA = 'Mozilla/5.0'
OSI = re.compile(r'^([A-Z]+?)(\d{6})([CP])(\d{8})$')

# Regular session, with a little slack so a 15-minute timer still catches the open and close.
OPEN_MIN, CLOSE_MIN = 9 * 60 + 25, 16 * 60 + 5

US_HOLIDAYS_2026 = {
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
    '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
}


def et_now():
    """Current US/Eastern time. Computed from the US DST rules so this needs no tz database."""
    utc = datetime.utcnow()
    year = utc.year
    def nth_dow(month, dow, n):
        d = datetime(year, month, 1)
        return d + timedelta(days=(dow - d.weekday()) % 7 + 7 * (n - 1))
    dst_start = nth_dow(3, 6, 2).replace(hour=7)    # 2nd Sunday in March, 07:00 UTC
    dst_end = nth_dow(11, 6, 1).replace(hour=6)     # 1st Sunday in November, 06:00 UTC
    return utc - timedelta(hours=4 if dst_start <= utc < dst_end else 5)


def market_is_open(now):
    if now.weekday() >= 5:
        return False, 'weekend'
    if now.strftime('%Y-%m-%d') in US_HOLIDAYS_2026:
        return False, 'market holiday'
    m = now.hour * 60 + now.minute
    if not (OPEN_MIN <= m <= CLOSE_MIN):
        return False, 'outside 09:25-16:05 ET'
    return True, ''


def fetch():
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    r = subprocess.run(['curl', '-s', '--max-time', '120', '-H', 'User-Agent: ' + UA,
                        URL, '-o', path], capture_output=True)
    if r.returncode != 0 or os.path.getsize(path) < 100000:
        os.unlink(path)
        raise RuntimeError('CBOE fetch failed (rc=%s, size=%s)'
                           % (r.returncode, os.path.getsize(path) if os.path.exists(path) else 0))
    try:
        with open(path) as f:
            return json.load(f)
    finally:
        os.unlink(path)


def build(payload, today_yymmdd):
    spot = payload['data']['current_price']
    prof = defaultdict(lambda: dict(cg=0.0, pg=0.0, coi=0.0, poi=0.0, cvol=0.0, pvol=0.0))
    allnet = 0.0
    for o in payload['data']['options']:
        m = OSI.match(o['option'])
        if not m:
            continue
        root, exp, cp, strike = m.groups()
        g = o.get('gamma') or 0.0
        oi = o.get('open_interest') or 0.0
        vol = o.get('volume') or 0.0
        # dollar gamma per 1% move, puts entered negative (dealer-short-put convention)
        val = g * oi * 100 * spot * spot * 0.01
        allnet += val if cp == 'C' else -val
        if root != 'SPXW' or exp != today_yymmdd:
            continue
        k = int(strike) / 1000.0
        p = prof[k]
        if cp == 'C':
            p['cg'] += val; p['coi'] += oi; p['cvol'] += vol
        else:
            p['pg'] -= val; p['poi'] += oi; p['pvol'] += vol
    return spot, prof, allnet


def levels(spot, prof):
    """Derived levels: the numbers you actually regress against price action."""
    if not prof:
        return {}
    ks = sorted(prof)
    cs = sum(prof[k]['cg'] for k in ks)
    ps = sum(prof[k]['pg'] for k in ks)
    peak = max(ks, key=lambda k: abs(prof[k]['cg'] + prof[k]['pg']))
    above = [k for k in ks if k > spot]
    below = [k for k in ks if k < spot]
    call_wall = max(above, key=lambda k: prof[k]['cg']) if above else None
    put_wall = min(below, key=lambda k: prof[k]['pg']) if below else None
    # gamma-flip proxy: strike at which the running net gamma changes sign
    run, flip = 0.0, None
    for k in ks:
        nxt = run + prof[k]['cg'] + prof[k]['pg']
        if run <= 0 < nxt or run >= 0 > nxt:
            flip = k
        run = nxt
    return dict(
        callsum=round(cs), putsum=round(ps), net=round(cs + ps),
        peak_strike=peak, call_wall=call_wall, put_wall=put_wall, gamma_flip=flip,
        spot_minus_peak=round(spot - peak, 2),
        spot_minus_flip=round(spot - flip, 2) if flip else None,
        coi=round(sum(prof[k]['coi'] for k in ks)),
        poi=round(sum(prof[k]['poi'] for k in ks)),
        cvol=round(sum(prof[k]['cvol'] for k in ks)),
        pvol=round(sum(prof[k]['pvol'] for k in ks)),
        n_strikes=len(ks))


FIELDS = ['date', 'time', 'spot', 'net', 'callsum', 'putsum', 'net_all_expiries',
          'peak_strike', 'call_wall', 'put_wall', 'gamma_flip',
          'spot_minus_peak', 'spot_minus_flip', 'coi', 'poi', 'cvol', 'pvol', 'n_strikes']


def main():
    now = et_now()
    ok, why = market_is_open(now)
    if ok is False and '--force' not in sys.argv:
        print('skip: %s (%s ET)' % (why, now.strftime('%Y-%m-%d %H:%M')))
        return 0

    date = now.strftime('%Y-%m-%d')
    hhmm = now.strftime('%H:%M')
    payload = fetch()
    spot, prof, allnet = build(payload, now.strftime('%y%m%d'))
    if not prof:
        print('no 0DTE (SPXW %s) contracts in the feed at %s %s — nothing written'
              % (now.strftime('%y%m%d'), date, hhmm))
        return 0

    lv = levels(spot, prof)

    # summary.csv is append-only, so writing must be idempotent: two runs that overlap on the
    # same quarter hour would otherwise leave two rows for one moment and quietly corrupt any
    # time series built from it. Overlap is not hypothetical — the schedule now runs redundant
    # triggers precisely so a dropped one does not cost the slot, which means two jobs covering
    # the same window is the normal case, not the failure case.
    if os.path.exists(SUMMARY):
        with open(SUMMARY) as f:
            if any(r.get('date') == date and r.get('time') == hhmm
                   for r in csv.DictReader(f)):
                print('%s %s ET already recorded — skipping' % (date, hhmm))
                return 0

    os.makedirs(PROF, exist_ok=True)
    row = dict(date=date, time=hhmm, spot=round(spot, 2), net_all_expiries=round(allnet), **lv)
    new = not os.path.exists(SUMMARY)
    with open(SUMMARY, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
        if new:
            w.writeheader()
        w.writerow(row)

    lo, hi = spot * 0.98, spot * 1.02
    pfile = os.path.join(PROF, '%s_%s.csv' % (date, hhmm.replace(':', '')))
    with open(pfile, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['# spot=%.2f net0dte=%s peak=%s flip=%s' % (spot, lv['net'], lv['peak_strike'], lv['gamma_flip'])])
        w.writerow(['strike', 'cg', 'pg', 'total', 'coi', 'poi', 'cvol', 'pvol'])
        for k in sorted(prof):
            if lo <= k <= hi:
                p = prof[k]
                w.writerow([k, round(p['cg']), round(p['pg']), round(p['cg'] + p['pg']),
                            int(p['coi']), int(p['poi']), int(p['cvol']), int(p['pvol'])])

    print('%s %s ET  spot %.2f  net0DTE %.3gB  peak %s  flip %s  -> summary.csv + %s'
          % (date, hhmm, spot, lv['net'] / 1e9, lv['peak_strike'], lv['gamma_flip'],
             os.path.basename(pfile)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
