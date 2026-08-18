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
    if n < 20:
        print(f'  {name}: only {n} bars — refusing to overwrite, check the ticker')
        return False
    OUT.mkdir(exist_ok=True)
    (OUT / f'_raw_{name}.json').write_text(r.stdout)
    print(f'  {name}: {n} bars')
    return True


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--range', default='2y')
    a = ap.parse_args()
    ok = all(fetch(sym, name, a.range) for sym, name in SERIES)
    print('next: python3 data/build_features.py')
    sys.exit(0 if ok else 1)
