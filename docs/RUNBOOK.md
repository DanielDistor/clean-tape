# Runbook

What to do when something is red. All commands run from the repo root (`uv run cleantape …`).

## First look

1. Open the site's **Health** page (red/amber light in the header links there), or run `uv run cleantape status`.
2. In GitHub: **Actions → daily → latest run**: the "Run pipeline" step shows every stage with its duration; logs are attached as the `logs-<run id>` artifact.

## A feed is late (freshness WARN/FAIL)

1. Check the message: `DGS10 latest 2026-11-04, expected 2026-11-05 (1 business day behind on us_bond)`.
2. Is it a holiday the calendar doesn't know (SIFMA early close, a national day of mourning)? Then it's expected; note it in BUILD_LOG.md if it's new.
3. Is the source itself behind? Open it directly (e.g. `https://fred.stlouisfed.org/series/DGS10`). If so, wait. The 10-business-day lookback picks the value up automatically on a later run.
4. Did our fetch fail? Look at the manifest table on Health (`error` column) or `uv run cleantape query "SELECT * FROM manifest WHERE NOT ok ORDER BY received_at_utc DESC LIMIT 20"`.
5. If the lag has really changed for good, update `expected_lag_business_days` in `config/universe.yaml` and record why in DECISIONS.md.

## Yahoo is blocked / rate-limited

- Symptoms: manifest row `yahoo … error`, `fallback_used=[SPY, …]` in the ingest log, freshness FAIL on VIX/FX/futures (no fallback).
- ETFs fall back to Nasdaq automatically (unadjusted closes, tagged `fallback` on the Overview).
- If it's persistent on Actions IPs: run on your laptop and push the state:
  ```bash
  uv run cleantape state pull
  uv run cleantape run --slot evening --force
  scripts/push_state.sh
  ```
  then re-run the `daily` workflow with `force` to redeploy the site.

## Checks FAIL

1. Read the FAIL rows on Health. Each says what it saw and what it expected.
2. **Validity / outliers**: compare the value with a public source (Treasury.gov, Cboe, the ECB website). Real move (e.g. a crisis day)? Then the FAIL is doing its job: note it and move on; it clears on the next day's data. Bad print? It's usually revised by the source; the lookback ingest picks up the revision (`revised=` in the ingest log).
3. **Consistency**: two sources disagree beyond the calibrated tolerance. Check which one is wrong. If tolerances are stale (regime change), recalibrate: `uv run cleantape calibrate` (writes `config/calibration.yaml`), review the numbers, commit.
4. **Schema**: a vendor changed its format. Look at the raw drop under `state/raw/{source}/{date}/`, update the parser in `transform/normalize.py`, add the new format as a fixture + test.
5. The site and report already flag affected numbers; nothing else needs to be done for readers.

## Rerun a day

```bash
uv run cleantape state pull                      # get the latest state
CLEANTAPE_NOW=2026-11-06T19:00:00-05:00 uv run cleantape run --slot evening --force
scripts/push_state.sh                            # publish the updated state
```
Or in GitHub: **Actions → daily → Run workflow** (slot `evening`, force ✓). The report for a specific date: `uv run cleantape report --date 2026-11-06`.

## Re-bootstrap (rebuild history from scratch)

```bash
rm -rf state
make bootstrap            # backfill from 2020-01-01 (≈15 s)
make run                  # one full run so checks, report and site exist
scripts/push_state.sh     # replaces origin/data with ./state
```
Then run the `daily` workflow once to redeploy the site.

## Add an instrument

1. Add one entry to `config/universe.yaml` (id, source, symbol, units, change_mode, calendar, lag, bounds, display group/order; optional `fallback`, `cross_check`).
2. `uv run cleantape config` validates it (fails fast on typos and unknown references).
3. `uv run cleantape backfill --start 2020-01-01` (idempotent; only new data is inserted), then `uv run cleantape check --audit` to confirm the bounds fit history.
4. If it has a `cross_check`, run `uv run cleantape calibrate`.
5. Commit the config. No code changes are needed.

## GitHub limits

- **Scheduled workflows stop after 60 days without repository activity** (public repos). GitHub emails a warning. To re-enable: **Actions → daily → "Enable workflow"**, or push any commit.
- **Cron is not punctual**: runs can be delayed by an hour or more, or dropped under load. The slot windows and two crons per slot absorb this. A slot that never ran shows up as the "pipeline may be down" banner the next morning.
- **Free GitHub Pages requires a public repository** and **Settings → Pages → Source = "GitHub Actions"**.
