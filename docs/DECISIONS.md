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
