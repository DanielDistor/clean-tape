# Decisions

One line of reasoning each. Newest at the bottom of each section.

## Scope
- **No email delivery.** This is a portfolio project with no real recipients; the EOD report is published on the site (HTML + Excel) instead. Dropped SMTP/`--send` and the "email on OK→FAIL" morning rule.
- **No `backfill.yml` workflow.** Yahoo often blocks GitHub Actions IPs, so history is bootstrapped on a laptop (`make bootstrap`) and pushed with `scripts/push_state.sh`; one reliable path beats two half-working ones.
- **No "arrived later than usual" freshness WARN / arrival-time chart.** With twice-daily batch pulls, receipt time measures our schedule, not the vendor; the date-based freshness check is the meaningful one.

## Tooling
- **uv + pinned `==` versions** in pyproject.toml and `uv.lock`; CI uses `uv sync --locked` so laptop and Actions run identical code.
- **pandas 3.x** (current at build time); code avoids chained assignment and relies on copy-on-write semantics.
- **Ruff rules E,F,W,I,B,UP,SIM,RUF,PTH,DTZ,N.** `DTZ` enforces timezone-aware datetimes in library code, which backs the "all timestamps UTC" rule mechanically.

## Config
- **Pydantic models with `extra="forbid"`**: a typo in YAML fails at load time instead of being silently ignored.
- **ECB series are separate instruments (`EURUSD_ECB`, …) with `display_group: null`.** They're stored, checked and cross-compared but not shown as their own rows; `source_symbol` is the Yahoo-convention pair and the parser derives it from EUR-based quotes.
- **`CLEANTAPE_NOW` env var pins the clock** for reruns of a past day and deterministic demos; tests use `FixedClock` directly.

## Ingestion
- **Equities fallback = Nasdaq quote-history JSON**, not Stooq (Stooq now serves a JS challenge). Unadjusted closes; stored as `source=nasdaq` and only fetched for symbols Yahoo didn't return. VIX, FX and futures have no keyless fallback — the freshness check catches their absence.
- **One FRED request per series** because multi-series requests ignore `cosd` (full history back to 1976 each time).
- **ECB `detail=dataonly`** cuts the drop from ~2.2 MB to ~0.6 MB for 2020→today and from 13 KB to ~1 KB per week; we only need TIME_PERIOD/OBS_VALUE/CURRENCY.
- **Yahoo raw drop = long CSV of the Close panel.** yfinance gives a DataFrame, not a file; serialising the close panel is the closest thing to an untouched drop.
- **Weekend-dated rows are never stored** (Yahoo emits in-progress weekend FX bars; ICE marks HY OAS on month-end weekends).
- **Manifest status `recheck`**: an identical drop is re-processed if bars were held back last time as in-progress (see BUILD_LOG).
- **Landing compares hashes per (source, dataset, as_of_date)**; data-level revisions are counted separately in the observation upsert (`revised=`), which is the number that matters.

## Calendars & lags (measured 2026-11-07, Saturday evening ET)
- **Publication lags** (sessions behind the last complete session): UST yields & HY OAS 1, T10YIE 0 (configured 1 to avoid false alarms), DTWEXBGS 5 (Fed H.10 is released weekly on Mondays), DCOILWTICO 3 observed; configured 5 because EIA spot prices update weekly.
- **WTI spot (DCOILWTICO) uses the `us_bond` (federal) calendar, not NYSE**: FRED has no value on Columbus Day, Veterans Day or the day after Thanksgiving.
- **Futures use the NYSE calendar** as an approximation of CME's; partial-bar rule is "NY date has rolled", like FX.
- **ECB partial-bar rule**: a fix dated D is final after 16:30 Frankfurt time on D (published ~16:00 CET).

## Transform
- **Best series = primary source, fallback fills gaps only**; the chosen `source` is kept on every derived row so a fallback day is always visible.
- **1d change requires the previous session's own observation** (no bridging gaps); 1w/1m/YTD use the last observation on or before the reference date within 3 days (`ffill_max_days`).
- **z-score = (move − mean) / std of the previous 252 valid moves**, ddof=1, null below 120. Mean-adjusted because some series (e.g. 2022 front-end yields) trend.
- **Spreads stored in bp, yields in %**; `change_mode: bp` multiplies yield differences by 100 but not spread differences.
- **DuckDB is in-memory only**, with views over parquet built on each `cleantape query`; nothing to keep in sync.

## Checks
- **Severity = worst of all rows; zero rows = FAIL**; a check that crashes produces a FAIL row rather than disappearing.
- **Freshness/completeness split**: freshness owns "the latest date is late"; completeness owns gaps *before* the latest date, failed drops and row counts (so one problem isn't reported three times).
- **Failed drop = WARN, not FAIL**: a dead Yahoo may be covered by the Nasdaq fallback; whether the *data* is late is freshness's call.
- **Row counts compared only against earlier drops with the same `window_days`**, so a backfill drop never skews the lookback median.
- **Staleness is WARN-only**: ZIRP-era 3M bills sat at exactly 0.05% for 10 sessions (2021-07-15) and 2-decimal FRED yields legitimately repeat; it's a prompt to look, not proof of a dead feed.
- **Futures outliers never exceed WARN**; if the roll-proxy ETF (USO/GLD/CPER/UNG) moved < 2σ the message says "possible futures roll".
- **Calibrated tolerances live in generated `config/calibration.yaml`** (committed), merged into `settings.checks.consistency` at load. Regenerate with `cleantape calibrate`.
- **Calibration method**: relative diff |a/b − 1| on same-date pairs from 2021-01-01; WARN = p99 × 1.5, FAIL = p99.9 × 1.5. 2020 excluded because Mar–Apr 2020 Cushing spot traded up to 42% below front-month CL (storage crisis) — real, but it would make the tolerance useless. Prices < $10 and futures roll-like days (|z| > 3 with roll proxy |z| < 2) are excluded.

  | pair | n | median | p99 | p99.9 | WARN > | FAIL > |
  |---|---|---|---|---|---|---|
  | EURUSD vs ECB | 1499 | 0.17% | 0.97% | 1.70% | 1.46% | 2.54% |
  | USDJPY vs ECB | 1499 | 0.21% | 1.51% | 2.42% | 2.26% | 3.63% |
  | GBPUSD vs ECB | 1499 | 0.19% | 1.08% | 1.77% | 1.62% | 2.65% |
  | AUDUSD vs ECB | 1499 | 0.27% | 1.32% | 2.19% | 1.98% | 3.29% |
  | USDCAD vs ECB | 1499 | 0.13% | 0.76% | 1.20% | 1.14% | 1.79% |
  | USDCHF vs ECB | 1499 | 0.19% | 1.02% | 1.97% | 1.52% | 2.95% |
  | EURJPY vs EURUSD×USDJPY | 1521 | 0.007% | 0.067% | 0.60% | 0.10% | 0.90% |
  | CL=F vs WTI spot | 1458 | 0.44% | 5.82% | 7.08% | 8.73% | 10.62% |

  The FX cross-source tolerances are wide (~1.5%) because the ECB fixes at 14:15 CET while Yahoo's daily bar closes hours later; this check catches gross errors (a wrong pair, an un-inverted quote, a stale feed), not basis-point drift.

## Portfolio (MOCK)
- **Commodities are held via ETFs (USO, GLD, CPER, UNG), futures are display-only.** Yahoo's `CL=F` is a continuous front-month series: on roll days it jumps from one contract to the next, so `qty × ΔP` would book the contango/backwardation gap as P&L that no real position earned. ETFs roll internally, so their price change *is* the holder's return. `load_positions` rejects futures outright.
- **Each position is valued on its own instrument's expected latest date** (calendar + lag), shown on every row. Forcing a single date would make rates P&L "n/a" every morning, because FRED yields lag equities by one session.
- **n/a, not zero**: a position with a missing price on t or t−1, or any FAIL check on its instrument, shows "n/a — data issue"; book totals sum the known positions and report how many are n/a.
- **DV01 positions have zero notional exposure** in gross/net; their risk is reported as DV01 instead.
- **Shocks are first-order linear** (DV01 × bp, exposure × %, FX notional × %); no convexity, no correlation, no cross-asset spillover. USD ±3% moves USDxxx pairs up/down and xxxUSD pairs the other way.
- **30-day cumulative P&L chart treats missing data as zero** for that day (it's a picture of the trend, not an official number), and records how many positions were n/a.
