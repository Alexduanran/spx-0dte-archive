# SPX intraday market-data archive

Per-date OHLC bars for the S&P 500 index (`^GSPC`) at 1-minute, 5-minute and hourly resolution,
a daily-timeframe indicator/volatility table, and 15-minute snapshots of SPX **0DTE gamma
exposure** by strike. Collected automatically by GitHub Actions; everything here comes from
public, unauthenticated endpoints.

**Why archive rather than fetch on demand.** Both of the underlying feeds are perishable, in
different ways:

- **Yahoo's intraday history expires, and finer bars expire faster** — 1-minute survives about
  **30 days**, 5-minute **60 days**, hourly **730 days**. Once a date falls outside its window
  it cannot be recovered at that resolution, at any price.
- **Gamma exposure has no history at all.** The 0DTE profile is a property of the *moment*, not
  of the day: near the open it sits spread across strikes, and by expiry it collapses into a
  spike at the at-the-money strike. No free source sells historical SPX open interest by strike,
  so this series exists only to the extent it was snapshotted as it happened. **Every 15-minute
  slot that is not captured is gone permanently.** That is the entire reason this repo runs on a
  schedule instead of on demand.

XSP is one tenth of SPX; multiply these prices by 0.1 for XSP-quoted comparisons.

## Layout

```
├── fetch_spx_bars.py     intraday bar refresh (additive; --force to re-pull)
├── fetch_daily.py        daily bars for ^GSPC / ^VIX / ^VIX9D (overwrites; never expires)
├── fetch_gex_poll.py     one 0DTE gamma-exposure snapshot
├── gex_session.py        drives the poller on a quarter-hour cadence for half a session
├── build_features.py     turns the archive into features/daily.csv + features/intraday.csv
├── index.csv             manifest: one row per date — resolution, bars, OHLC, range, net
├── spx-1m/YYYY-MM-DD.csv 1-minute bars   ← finest available, shortest shelf life
├── spx-5m/YYYY-MM-DD.csv 5-minute bars   ← the workhorse for opening-range / entry-timing work
├── spx-1h/YYYY-MM-DD.csv hourly bars     ← deep history, and the fallback for dates that have
│                                           aged out of the finer windows
├── daily/_raw_*.json     raw Yahoo daily payloads (^GSPC, ^VIX, ^VIX9D)
├── features/daily.csv    one row per date: EMA20/50, RSI14, ATR14, VIX, VIX9D/VIX, gap,
│                         opening ranges, realised path length / trend efficiency
├── features/intraday.csv the same indicators computed on 5-minute bars, so an 11:15 reading
│                         is scored with 11:15 information
├── features/intraday_1m.csv  the same at 1-minute resolution — two EMA pairs, see below
├── gex/summary.csv       one row per snapshot — the derived gamma levels
└── gex/profile/          near-the-money gamma/OI profile at each snapshot
```

A date can appear in more than one bar folder. Use the finest one present; `index.csv` records
which resolution is best available per date.

Each bar file:

```csv
time,open,high,low,close
09:30,7763.18,7777.28,7763.18,7776.42
09:35,7776.47,7787.20,7776.42,7786.87
```

`time` is **America/New_York**, regular session only (09:30–16:00). No pre/post-market rows.
No volume — Yahoo's index feed does not carry meaningful volume for `^GSPC`.

Source: Yahoo Finance chart API. These are index prices, not tradeable quotes — good for path
and range reconstruction, not for modelling fills.

### Data quality

The 1-minute and 5-minute sets were cross-checked against each other: aggregating the 1m bars
into 5-minute buckets reproduced the archived 5m highs and lows across all **1,561** overlapping
windows, to within 0.6 index points.

Known quirks in the hourly history:

- Several short sessions are legitimate **half days** (July 3rd, the day after Thanksgiving,
  Christmas Eve — 4 bars, 13:00 close), not missing data:
  `2023-11-24, 2024-07-03, 2024-11-29, 2024-12-24, 2025-07-03, 2025-11-28, 2025-12-24`.
- Two are genuine holes in Yahoo's data: `2026-01-30` (2 bars) and `2026-02-02`
  (3 bars, session starts 13:30).
- Prices carry 2 decimals. Treat these files as the precise copy and re-derive figures from
  them rather than trusting rounded values quoted elsewhere.

## Gamma exposure (`gex/`)

`fetch_gex_poll.py` reads CBOE's public delayed-quote feed
(`cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json`) — no authentication, ~13 MB per
pull, roughly 15 minutes delayed. It carries per-contract `gamma`, `open_interest` and `volume`,
so gamma exposure is computed here from first principles rather than taken on trust.

> **0DTE lives under the `SPXW` root, not `SPX`.** `_SPX.json` contains both (SPX = monthlies,
> SPXW = dailies/weeklies); filtering to `root == 'SPX'` silently drops every 0DTE contract.
> There is no separate `_SPXW.json` — that path returns 403.

`gex/summary.csv` is append-only, one row per snapshot: spot, net 0DTE gamma, call/put sums,
peak-gamma strike, call wall, put wall, and a gamma-flip proxy (the strike at which running net
gamma changes sign), plus spot's distance to the peak and flip. `gex/profile/<date>_<HHMM>.csv`
holds the by-strike detail within ±2% of spot: `cg`/`pg` call/put gamma exposure in dollars,
`total`, `coi`/`poi` open interest, `cvol`/`pvol` volume.

**Snapshot time is part of the data.** A post-close file shows gamma that is exactly zero
everywhere except a narrow band around spot — that is the expiry gamma spike, not corruption.
Compare like with like, or days will not be comparable.

## Two EMA pairs in `features/intraday_1m.csv`

`ema20`/`ema50` restart every morning. No overnight gap leaks into them, but they are undefined
until enough bars have accumulated — on 1-minute bars that is **09:49** for EMA20 and **10:19**
for EMA50, which is after a 15-minute opening-range entry has already been taken. Across the
archive they carry a value on 95.1% and 87.4% of bars.

`ema20_cont`/`ema50_cont` run unbroken across the whole archive, so they hold a value from the
first bar of the day (99.8% / 99.5% coverage) at the cost of folding the overnight gap in. On
2026-08-18 the index opened at 7704.16 after a 7746.14 close, and `ema20_cont` was still sitting
at 7744.52 — forty points above spot, describing yesterday rather than today. By the close the
two pairs agree exactly, the seed having long since washed out.

Neither is correct in the abstract. Use the reset pair to describe a session on its own terms,
and the continuous pair when a signal has to exist early in the day — and never compare one
against the other across the 09:49/10:19 boundary.

## Automation

Three GitHub Actions workflows keep this current. They need no secrets: `GITHUB_TOKEN` with
`contents: write` is enough to commit back to the repo.

| Workflow | Cron (UTC) | Covers |
|---|---|---|
| `gex-1-open.yml` | `15 11` + `15 12` | 09:30 → 11:30 ET |
| `gex-2-midday.yml` | `30 13` + `30 14` | 11:45 → 13:45 ET |
| `gex-3-close.yml` | `45 15` + `45 16` | 14:00 → 16:00 ET |
| `bars-eod.yml` | `35 20` + `35 21` | after the close: intraday bars, daily series, features |

Three details are deliberate and worth not "simplifying" away:

- **No `*/15` cron.** GitHub's scheduled triggers are best-effort; a `schedule` event routinely
  fires late and can be dropped outright, which would leave the series ragged and gapped.
  Instead each run holds a single job open for its segment and sleeps to each quarter hour
  itself, so only the *start* depends on the scheduler.
- **Each segment starts about two and a quarter hours early, with a backup cron an hour after
  the primary.** The start still has to arrive before the segment's first snapshot is due, and
  it does not reliably. Measured here over one week: the morning trigger ran 54, 55, 57, 55 and
  66 minutes late, the afternoon one 36–45, a post-close trigger 206 — and on 2026-08-27 the
  morning trigger never fired at all, costing all thirteen slots from 09:30 to 12:30. The
  session is in three parts rather than two because a job is capped at **6 hours** and 07:15 to
  16:10 ET is nearly nine.
- **The segments overlap on purpose, and that is safe.** Segment 2 starts at 09:30 ET, inside
  segment 1's window, so a segment whose trigger is dropped is backed up by the next one.
  Writes are idempotent — `fetch_gex_poll.py` refuses a `(date, time)` it already holds, checked
  before the 13 MB fetch rather than after — and each segment owns its concurrency group, so a
  backup firing while its primary still works simply queues and exits on the cutoff.

The crons are UTC and set for EDT. Under EST each starts an hour earlier in ET terms and idles
longer, which the timeout absorbs; the poller refuses anything outside 09:25–16:05 ET anyway.
The bars job registers 20:35 and 21:35 with a guard dropping anything before 16:20 ET, so under
EST the early one is dropped and under EDT both run — the duplicate writes nothing.

Every script self-guards weekends and US market holidays, so a fire on a non-trading day exits
in milliseconds without touching the network.

Run any of them by hand from the Actions tab (`workflow_dispatch`), or locally:

```bash
python3 fetch_gex_poll.py --force   # snapshot right now, ignoring the session guard
python3 fetch_spx_bars.py           # additive; --force to re-pull, --only 1m to fetch one tier
python3 fetch_daily.py && python3 build_features.py
```

### `^VIX9D` is captured one day at a time

Yahoo has degraded `^VIX9D` to a live quote with no history — a request for any range returns a
single bar. `fetch_daily.py` therefore folds that one bar into the stored series rather than
either discarding it (which would let `vix9d`, and the `vix9d_vix` term-structure ratio, quietly
die) or overwriting hundreds of good bars with it. **This makes the daily VIX9D series
perishable in the same way gamma exposure is:** each day's value is only obtainable on that day.
The post-close schedule is what makes it work, since by 16:35 ET the quote is the settled close.
`^GSPC` and `^VIX` still return full history and are refreshed wholesale.

Everything runs on the standard library plus `curl`. DST is computed from the US rules directly
rather than via `zoneinfo`, so the scripts work on old Python builds (3.7+) as well as modern
ones.
