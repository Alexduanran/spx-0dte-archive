#!/usr/bin/env python3
"""
Build the SPX 0DTE feature table used for bot analysis.

Two outputs, both keyed so they can be joined onto a bot's position log:

  data/features/daily.csv     one row per trading date — daily-timeframe technicals
                              (EMA20/50, RSI14, ATR14), the VIX complex, gap, opening
                              ranges, and realised intraday path stats from the bar archive.

  data/features/intraday.csv  one row per (date, 5-minute timestamp) — the same indicator
                              family computed on 5-minute bars, so an entry at 11:15 can be
                              scored with what was actually knowable at 11:15.

Inputs: data/spx-{1m,5m,1h}/ (see README.md) and data/daily/_raw_*.json from fetch_daily.py.

Everything here is computed from the local archive — no network. Re-run any time.
"""

import csv
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'features'
OUT.mkdir(exist_ok=True)


# ---------------------------------------------------------------- indicators

def ema(vals, n):
    """Exponential moving average; out[i] is None until there are n samples."""
    k = 2 / (n + 1)
    out, e = [], None
    for i, v in enumerate(vals):
        if i < n - 1:
            out.append(None)
            continue
        if e is None:
            e = sum(vals[: n]) / n          # seed with the SMA, the usual convention
        else:
            e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(vals, n=14):
    """Wilder's RSI."""
    out = [None] * len(vals)
    if len(vals) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars, n=14):
    """Wilder's ATR over (o,h,l,c) tuples."""
    out = [None] * len(bars)
    trs = []
    for i, (_, o, h, l, c) in enumerate(bars):
        pc = bars[i - 1][4] if i else c
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) <= n:
        return out
    a = sum(trs[1: n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


# ---------------------------------------------------------------- loading

def mins(t):
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def load_bars(date, prefer=('1m', '5m', '1h')):
    for res in prefer:
        p = ROOT / f'spx-{res}' / f'{date}.csv'
        if p.exists():
            rows = list(csv.DictReader(p.open()))
            return res, [(mins(r['time']), float(r['open']), float(r['high']),
                          float(r['low']), float(r['close'])) for r in rows]
    return None, None


def load_yahoo(name):
    """Parse a raw Yahoo chart JSON into {date: (o,h,l,c)}."""
    p = ROOT / 'daily' / f'_raw_{name}.json'
    if not p.exists():
        return {}
    import datetime as dt
    d = json.load(p.open())['chart']['result'][0]
    q = d['indicators']['quote'][0]
    out = {}
    for i, ts in enumerate(d['timestamp']):
        c = q['close'][i]
        if c is None:
            continue
        day = dt.datetime.utcfromtimestamp(ts + d['meta'].get('gmtoffset', 0)).strftime('%Y-%m-%d')
        out[day] = (q['open'][i], q['high'][i], q['low'][i], c)
    return out


# ---------------------------------------------------------------- daily table

def build_daily():
    spx = load_yahoo('spx')
    vix = load_yahoo('vix')
    vix9d = load_yahoo('vix9d')
    days = sorted(spx)
    closes = [spx[d][3] for d in days]
    bars_d = [(0,) + tuple(spx[d]) for d in days]

    e20, e50 = ema(closes, 20), ema(closes, 50)
    r14 = rsi(closes, 14)
    a14 = atr(bars_d, 14)

    rows = []
    for i, d in enumerate(days):
        o, h, l, c = spx[d]
        pc = spx[days[i - 1]][3] if i else None
        res, bb = load_bars(d, prefer=('5m', '1m', '1h'))
        intr = {}
        if bb and len(bb) > 10:
            cl = [x[4] for x in bb]
            plen = sum(abs(cl[j] - cl[j - 1]) for j in range(1, len(cl)))
            net = cl[-1] - bb[0][1]
            hi, lo = max(x[2] for x in bb), min(x[3] for x in bb)
            def orb(minutes):
                w = [x for x in bb if x[0] < 570 + minutes]
                return (max(x[2] for x in w), min(x[3] for x in w)) if w else (None, None)
            o15h, o15l = orb(15)
            o60h, o60l = orb(60)
            intr = dict(bar_res=res, path_len=round(plen, 1), net_pts=round(net, 1),
                        range_pts=round(hi - lo, 1),
                        trend_eff=round(abs(net) / plen, 3) if plen else None,
                        orb15_hi=o15h, orb15_lo=o15l, orb60_hi=o60h, orb60_lo=o60l)
        vx = vix.get(d, (None,) * 4)[3]
        v9 = vix9d.get(d, (None,) * 4)[3]
        rows.append(dict(
            date=d, open=o, high=h, low=l, close=c,
            gap_pts=round(o - pc, 2) if pc else None,
            gap_pct=round(100 * (o - pc) / pc, 3) if pc else None,
            ema20=round(e20[i], 2) if e20[i] else None,
            ema50=round(e50[i], 2) if e50[i] else None,
            px_vs_ema20=round(c - e20[i], 2) if e20[i] else None,
            px_vs_ema50=round(c - e50[i], 2) if e50[i] else None,
            ema20_vs_50=round(e20[i] - e50[i], 2) if e20[i] and e50[i] else None,
            rsi14=round(r14[i], 2) if r14[i] else None,
            atr14=round(a14[i], 2) if a14[i] else None,
            atr_pct=round(100 * a14[i] / c, 3) if a14[i] else None,
            vix=vx, vix9d=v9,
            vix9d_vix=round(v9 / vx, 4) if vx and v9 else None,
            **intr))
    keys = ['date', 'open', 'high', 'low', 'close', 'gap_pts', 'gap_pct',
            'ema20', 'ema50', 'px_vs_ema20', 'px_vs_ema50', 'ema20_vs_50',
            'rsi14', 'atr14', 'atr_pct', 'vix', 'vix9d', 'vix9d_vix',
            'bar_res', 'path_len', 'net_pts', 'range_pts', 'trend_eff',
            'orb15_hi', 'orb15_lo', 'orb60_hi', 'orb60_lo']
    with (OUT / 'daily.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print(f'features/daily.csv     {len(rows):5d} rows  {days[0]} -> {days[-1]}')
    return rows


# ---------------------------------------------------------------- intraday table

def build_intraday(start=None, end=None):
    files = sorted(p.stem for p in (ROOT / 'spx-5m').glob('*.csv'))
    if start:
        files = [d for d in files if d >= start]
    if end:
        files = [d for d in files if d <= end]
    out_rows = []
    for d in files:
        res, bb = load_bars(d, prefer=('5m',))
        if not bb or len(bb) < 30:
            continue
        cl = [x[4] for x in bb]
        e20, e50 = ema(cl, 20), ema(cl, 50)
        r14 = rsi(cl, 14)
        a14 = atr(bb, 14)
        day_open = bb[0][1]
        for i, (t, o, h, l, c) in enumerate(bb):
            hi = max(x[2] for x in bb[: i + 1])
            lo = min(x[3] for x in bb[: i + 1])
            plen = sum(abs(cl[j] - cl[j - 1]) for j in range(1, i + 1))
            out_rows.append(dict(
                date=d, time='%02d:%02d' % (t // 60, t % 60), close=c,
                ema20=round(e20[i], 2) if e20[i] else None,
                ema50=round(e50[i], 2) if e50[i] else None,
                px_vs_ema20=round(c - e20[i], 2) if e20[i] else None,
                ema20_vs_50=round(e20[i] - e50[i], 2) if e20[i] and e50[i] else None,
                rsi14=round(r14[i], 2) if r14[i] else None,
                atr14=round(a14[i], 3) if a14[i] else None,
                sofar_range=round(hi - lo, 2),
                sofar_path=round(plen, 2),
                sofar_eff=round(abs(c - day_open) / plen, 3) if plen else None,
                vs_day_open=round(c - day_open, 2)))
    with (OUT / 'intraday.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f'features/intraday.csv  {len(out_rows):5d} rows  {files[0]} -> {files[-1]}  (5m)')


if __name__ == '__main__':
    build_daily()
    build_intraday()
