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

## Automation

Three GitHub Actions workflows keep this current. They need no secrets: `GITHUB_TOKEN` with
`contents: write` is enough to commit back to the repo.

| Workflow | Cron (UTC) | What it does |
|---|---|---|
| `gex-morning.yml` | `20 13 * * 1-5` | holds one job open, snapshotting every quarter hour until 12:35 ET |
| `gex-afternoon.yml` | `40 16 * * 1-5` | same, 12:40 → 16:10 ET |
| `bars-eod.yml` | `35 20`, `35 21 * * 1-5` | after the close: intraday bars, daily series, rebuild features |

Two scheduling details are deliberate and worth not "simplifying" away:

- **The GEX workflows do not use a `*/15` cron.** GitHub's scheduled triggers are best-effort —
  under load a `schedule` event routinely fires 5–20 minutes late and can be dropped outright,
  which would leave the series ragged and gapped. Instead each run holds a single job open for
  its half of the session and sleeps to each quarter hour itself, so only the *start* depends on
  the scheduler. The session is split in two because a job is capped at **6 hours**.
- **The crons are set for EDT and tolerate EST.** A fixed UTC time drifts an hour twice a year.
  The GEX jobs simply start an hour early under EST and idle — `fetch_gex_poll.py` refuses
  anything outside 09:25–16:05 ET regardless. The bars job registers both 20:35 and 21:35 UTC
  and a guard step drops whichever lands before the close.

Every script self-guards weekends and US market holidays, so a fire on a non-trading day exits
in milliseconds without touching the network.

Run any of them by hand from the Actions tab (`workflow_dispatch`), or locally:

```bash
python3 fetch_gex_poll.py --force   # snapshot right now, ignoring the session guard
python3 fetch_spx_bars.py           # additive; --force to re-pull, --only 1m to fetch one tier
python3 fetch_daily.py && python3 build_features.py
```

Everything runs on the standard library plus `curl`. DST is computed from the US rules directly
rather than via `zoneinfo`, so the scripts work on old Python builds (3.7+) as well as modern
ones.
