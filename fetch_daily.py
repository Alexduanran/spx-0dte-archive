#!/usr/bin/env python3
"""
Pull daily bars for SPX and the VIX complex from Yahoo, for the daily-timeframe
indicators (EMA/RSI/ATR) and the volatility term structure.

Unlike the intraday archive, daily history does NOT expire — Yahoo serves years of it —
so this simply overwrites on each run rather than accumulating per-date files.

  python3 fetch_daily.py            # 2y of ^GSPC, ^VIX, ^VIX9D
  python3 fetch_daily.py --range 5y

Writes data/daily/_raw_<name>.json (raw Yahoo payloads). Run build_features.py afterwards
to turn them into data/features/daily.csv.

^VIX3M is deliberately not fetched: Yahoo returns a single stub bar for it. If a 3-month
vol series is ever needed, ^VXV is the older ticker worth trying.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'daily'
UA = 'Mozilla/5.0'

SERIES = [
    ('%5EGSPC', 'spx'),      # S&P 500 index
    ('%5EVIX', 'vix'),       # 30-day implied vol
    ('%5EVIX9D', 'vix9d'),   # 9-day implied vol — vix9d/vix is the 0DTE-relevant term structure
]


def fetch(symbol, name, rng):
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
           f'?interval=1d&range={rng}')
    r = subprocess.run(['curl', '-s', '-H', f'User-Agent: {UA}', url],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f'  {name}: curl failed ({r.returncode})')
        return False
    try:
        d = json.loads(r.stdout)
        res = d['chart']['result'][0]
        n = len(res['timestamp'])
    except Exception as e:
        print(f'  {name}: unparseable response ({e})')
        return False
    OUT.mkdir(exist_ok=True)
    path = OUT / f'_raw_{name}.json'

    if n < 20:
        # A short response is not necessarily junk. Yahoo has degraded ^VIX9D to a live quote
        # with no history — one bar, every time — and simply discarding it would let the series
        # die quietly, taking vix9d_vix (the 9-day/30-day term structure, the vol measure that
        # actually matters for 0DTE) with it. Overwriting 500 stored bars with one would be
        # worse still. So fold the bar into what is already stored.
        #
        # This makes the daily VIX9D series perishable in the same way gamma exposure is: each
        # day's value is only obtainable on that day. The post-close schedule is what makes it
        # work — by 16:35 ET the quote is the settled close.
        merged = merge_into(path, res, name)
        if merged is None:
            print(f'  {name}: only {n} bars and nothing stored to merge into — skipped')
            return False
        print(f'  {name}: {n} bar folded into stored series ({merged} total)')
        return True

    path.write_text(r.stdout)
    print(f'  {name}: {n} bars')
    return True


def merge_into(path, res, name):
    """
    Add `res`'s bars to the stored payload, keyed by ET calendar date so a re-run the same day
    replaces that day rather than duplicating it. Returns the new bar count, or None if there is
    no stored payload to merge into.
    """
    if not path.exists():
        return None
    import datetime as dt

    doc = json.loads(path.read_text())
    old = doc['chart']['result'][0]
    off = old['meta'].get('gmtoffset', 0)

    def day(ts):
        return dt.datetime.utcfromtimestamp(ts + off).strftime('%Y-%m-%d')

    fields = [k for k in ('open', 'high', 'low', 'close', 'volume')
              if k in old['indicators']['quote'][0]]
    rows = {}
    for src in (old, res):
        q = src['indicators']['quote'][0]
        for i, ts in enumerate(src['timestamp']):
            if q['close'][i] is None:
                continue
            rows[day(ts)] = (ts, [q[f][i] for f in fields])

    order = sorted(rows)
    old['timestamp'] = [rows[d][0] for d in order]
    for j, f in enumerate(fields):
        old['indicators']['quote'][0][f] = [rows[d][1][j] for d in order]
    path.write_text(json.dumps(doc))
    return len(order)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--range', default='2y')
    a = ap.parse_args()
    # list(), not all() — all() short-circuits, so one bad series used to stop the rest from
    # being fetched at all.
    got = sum(fetch(sym, name, a.range) for sym, name in SERIES)
    print('next: python3 data/build_features.py')

    # Exit non-zero only if EVERY series failed. Yahoo intermittently answers ^VIX9D with a
    # one-bar stub; the guard in fetch() already refuses to overwrite good data with it, so the
    # previous copy stays valid and the cost is one stale vol column for a day. Treating that as
    # fatal aborted the caller's chain and stopped build_features.py from running at all — the
    # feature tables sat two days stale (2026-08-24 against bars through 08-25) while both
    # post-close runs reported nothing worse than a red step.
    sys.exit(0 if got else 1)
