#!/usr/bin/env python3
"""
Archive SPX intraday bars from Yahoo Finance, one CSV per trading date.

Why this exists: Yahoo only serves 5-minute bars for the last 60 calendar days.
Anything older is simply gone. Bot positions get analysed months after the fact,
so the bars have to be captured while they are still reachable and kept locally.

The script is ADDITIVE by design — it never overwrites a date that already has a
file, so days that have since aged out of Yahoo's window stay safe. Use --force
to deliberately re-pull a date.

Usage
  python3 fetch_spx_bars.py              # refresh 5m (60d) + 1h (730d)
  python3 fetch_spx_bars.py --force      # re-pull and overwrite existing dates
  python3 fetch_spx_bars.py --only 5m    # just the 5-minute archive

Layout
  data/spx-5m/YYYY-MM-DD.csv   time,open,high,low,close   (regular session, ET)
  data/spx-1h/YYYY-MM-DD.csv   same, hourly — covers dates older than the 5m window
  data/index.csv               one row per archived date: OHLC, range, bar count
"""

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SYMBOL = "%5EGSPC"          # ^GSPC — S&P 500 index (SPX)
UA = "Mozilla/5.0"

FEEDS = [
    # (label, subdir, interval, range) — see the window limits in README.md
    ("1m", "spx-1m", "1m", "chunked"),   # only ~30 days back, ≤8 days per request
    ("5m", "spx-5m", "5m", "60d"),
    ("1h", "spx-1h", "1h", "730d"),
]

# Yahoo refuses a 1-minute request whose window starts more than ~30 days ago, and
# caps any single request at 8 days. So walk back in 7-day chunks until refused.
ONE_MIN_MAX_DAYS = 30
ONE_MIN_CHUNK_DAYS = 7


def _nth_dow(year, month, dow, n):
    """Date of the nth `dow` (0=Mon) in a month, as a naive UTC-midnight datetime."""
    d = datetime(year, month, 1)
    d += timedelta(days=(dow - d.weekday()) % 7 + 7 * (n - 1))
    return d


def et_time(epoch):
    """
    Epoch seconds -> naive US/Eastern datetime.

    Written against the DST rules directly rather than zoneinfo/pytz so the script
    runs on the stock macOS python3 (3.7, no zoneinfo) as well as modern ones.
    US DST since 2007: 2nd Sunday of March 02:00 local -> 1st Sunday of November 02:00.
    """
    utc = datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)
    y = utc.year
    start = _nth_dow(y, 3, 6, 2) + timedelta(hours=7)   # 02:00 EST = 07:00 UTC
    end = _nth_dow(y, 11, 6, 1) + timedelta(hours=6)    # 02:00 EDT = 06:00 UTC
    return utc - timedelta(hours=4 if start <= utc < end else 5)


class TransportError(RuntimeError):
    """curl could not complete the request — a timeout or reset, not a refusal from Yahoo."""


def _get(query, soft=False, attempts=4):
    """
    One Yahoo chart request, retried on transport failure.

    A transport failure and a refusal from Yahoo are different things and callers must be able
    to tell them apart. `chart.error` means the request was out of range — for the 1-minute walk
    that is the signal that history has run out and the walk is done. A timeout says nothing at
    all about the data. Treating a timeout as "no more history" would truncate the archive
    silently, and treating it as fatal throws away the tiers that have not been fetched yet;
    unattended, one flaky request then costs a whole day of bars. So: refusal -> None when
    `soft`, transport failure -> retried, then TransportError for the caller to handle.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?{query}"
    last = ""
    for attempt in range(1, attempts + 1):
        out = subprocess.run(
            ["curl", "-s", "--max-time", "60", "-A", UA, url],
            capture_output=True, text=True,
        )
        if out.returncode:
            last = "curl exit %d %s" % (out.returncode, out.stderr[:200].strip())
        else:
            try:
                doc = json.loads(out.stdout)
            except ValueError as exc:
                last = "unparseable response (%s)" % exc
            else:
                err = doc.get("chart", {}).get("error")
                if err:
                    if soft:
                        return None
                    raise SystemExit("Yahoo rejected %s: %s" % (query, err))
                return doc["chart"]["result"][0]
        if attempt < attempts:
            time.sleep(attempt * 5)
    raise TransportError("%s after %d attempts: %s" % (query, attempts, last))


def fetch(interval, rng):
    return _get("interval=%s&range=%s" % (interval, rng))


def fetch_1m_chunks():
    """
    Collect every 1-minute day Yahoo will still serve, oldest reachable to today.

    Requests are windowed because Yahoo caps a single 1m call at 8 days; windows
    that reach past the ~30-day horizon come back as an error, which just ends the
    walk rather than failing the run.
    """
    now = int(time.time())
    floor = now - ONE_MIN_MAX_DAYS * 86400
    days = {}
    for k in range(ONE_MIN_MAX_DAYS // ONE_MIN_CHUNK_DAYS + 2):
        end = now - k * ONE_MIN_CHUNK_DAYS * 86400
        start = end - ONE_MIN_CHUNK_DAYS * 86400
        if end <= floor:
            break
        try:
            res = _get("interval=1m&period1=%d&period2=%d" % (max(start, floor), end),
                       soft=True)
        except TransportError as exc:
            # a chunk that would not load says nothing about the ones behind it — keep walking
            print("  1m chunk unreachable, skipping: %s" % exc)
            continue
        if res is None:
            break
        for date, rows in to_days(res).items():
            # a window's trailing "current price" stub is a single bar — ignore it
            if len(rows) > 100 or len(days.get(date, [])) < len(rows):
                days.setdefault(date, rows)
    return {d: r for d, r in days.items() if len(r) > 100}


def to_days(result):
    """Group bars by ET calendar date. Drops bars with a null close (holidays/halts)."""
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    days = {}
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        dt = et_time(t)
        # regular session only — skip any pre/post rows Yahoo slips in
        if not (9 * 60 + 30) <= (dt.hour * 60 + dt.minute) <= (16 * 60):
            continue
        days.setdefault(dt.strftime("%Y-%m-%d"), []).append([
            dt.strftime("%H:%M"),
            round(q["open"][i], 2), round(q["high"][i], 2),
            round(q["low"][i], 2), round(q["close"][i], 2),
        ])
    return days


def write_days(days, subdir, force):
    out_dir = ROOT / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    # today's session is still filling in, so never let a partial copy of it stick
    today = et_time(time.time()).strftime("%Y-%m-%d")
    written = skipped = 0
    for date, rows in sorted(days.items()):
        path = out_dir / f"{date}.csv"
        if path.exists() and not force and date != today:
            skipped += 1
            continue
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close"])
            w.writerows(rows)
        written += 1
    return written, skipped


def rebuild_index():
    """Manifest across every archived date, 5m preferred, 1h where that is all we have."""
    seen = {}
    for label, subdir in (("1h", "spx-1h"), ("5m", "spx-5m"), ("1m", "spx-1m")):  # finest wins
        d = ROOT / subdir
        if not d.is_dir():
            continue
        for p in d.glob("20??-??-??.csv"):
            seen[p.stem] = (label, p)

    rows = []
    for date in sorted(seen):
        res, path = seen[date]
        with path.open() as fh:
            bars = list(csv.reader(fh))[1:]
        if not bars:
            continue
        highs = [float(b[2]) for b in bars]
        lows = [float(b[3]) for b in bars]
        o, c = float(bars[0][1]), float(bars[-1][4])
        rows.append({
            "date": date,
            "dow": datetime.strptime(date, "%Y-%m-%d").strftime("%a"),
            "resolution": res,
            "bars": len(bars),
            "open": f"{o:.2f}", "high": f"{max(highs):.2f}",
            "low": f"{min(lows):.2f}", "close": f"{c:.2f}",
            "range_pts": f"{max(highs) - min(lows):.2f}",
            "net_pts": f"{c - o:+.2f}",
        })

    with (ROOT / "index.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite dates that already have a file")
    ap.add_argument("--only", choices=[f[0] for f in FEEDS],
                    help="fetch just one resolution")
    args = ap.parse_args()

    attempted, failed = 0, []
    for label, subdir, interval, rng in FEEDS:
        if args.only and args.only != label:
            continue
        attempted += 1
        try:
            days = fetch_1m_chunks() if rng == "chunked" else to_days(fetch(interval, rng))
        except TransportError as exc:
            print(f"{label:>3}  unreachable: {exc}")
            failed.append(label)
            continue
        written, skipped = write_days(days, subdir, args.force)
        span = f"{min(days)} → {max(days)}" if days else "nothing returned"
        print(f"{label:>3}  {span}   {len(days)} days available"
              f"   {written} written, {skipped} already archived")

    print(f"index.csv rebuilt: {rebuild_index()} dates")

    # Partial success is still success: one unreachable tier must not discard the others, and
    # the caller commits whatever landed. Only a clean sweep of failures is worth an error.
    if failed:
        print("tiers that could not be fetched: %s" % ", ".join(failed))
    return 1 if failed and len(failed) == attempted else 0


if __name__ == "__main__":
    sys.exit(main())
