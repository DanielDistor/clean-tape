# Build log

Things that went wrong, how they were caught, and what I learned. Format: what happened → how detected → fix → lesson.

## Phase 0 — source verification

### Stooq's keyless CSV download is gone
- **What happened:** the brief suggested Stooq (`stooq.com/q/d/l/?s=spy.us&i=d`) as the equities fallback. The endpoint now returns an HTML page with a JavaScript proof-of-work "verify your browser" challenge instead of CSV.
- **How detected:** probed the URL with curl before writing any code; response was `<!DOCTYPE html>…crypto.subtle.digest…`.
- **Fix:** switched the fallback to Nasdaq's public quote-history JSON (`api.nasdaq.com/api/quote/{SYM}/historical?assetclass=etf`), which works keylessly with a browser User-Agent. Its closes are *unadjusted* (no dividend adjustment), unlike Yahoo's `auto_adjust=True`, so fallback rows are tagged `source=nasdaq` and documented.
- **Lesson:** free data sources rot; verify every endpoint before designing around it, and design the store so a value's source is always recorded.

### FRED multi-series CSV ignores the date filter
- **What happened:** `fredgraph.csv?id=DGS10,DGS2&cosd=2026-10-20` returns the full history back to 1976, while the single-series request honours `cosd`.
- **How detected:** inspected the first lines of both responses.
- **Fix:** one request per FRED series (9 small requests), each with `cosd`. Also confirmed the date column is now `observation_date` (it used to be `DATE`), so the parser accepts either.
- **Lesson:** "can I batch this?" needs checking against behaviour, not just status codes.

## Phase 3 — ingestion

### FRED now encodes missing values as empty, not `.`
- **What happened:** the brief (and older FRED docs) say missing = `.`. The current fredgraph CSV leaves the field empty (`2025-11-11,`).
- **How detected:** read the captured fixture around Veterans Day.
- **Fix:** the parser treats both `.` and empty as null; a hand-made legacy fixture (`DATE` header + `.`) keeps the old format under test.
- **Lesson:** handle both the documented and the observed format; you don't control when a vendor switches.

### WTI spot follows the federal calendar, not NYSE
- **What happened:** I first put DCOILWTICO on the NYSE calendar. The backfill had null values on 2023-10-09, 2024-10-14, 2025-10-13 (Columbus Day) and 2023-11-10, 2024-11-11, 2025-11-11 (Veterans Day) — days NYSE trades.
- **How detected:** listed every stored null after the first backfill.
- **Fix:** moved WTI_SPOT to `us_bond`. Remaining nulls (2020-11-27, 2021-11-26 day-after-Thanksgiving; 2025-01-09 national day of mourning for President Carter) are genuine source gaps the validity audit reports.
- **Lesson:** "which calendar does this series follow?" is an empirical question; let the data tell you.

### HY OAS history is truncated to ~3 years
- **What happened:** BAMLH0A0HYM2 only returns data from 2023-11 onward despite requesting 2020-01-01 (ICE licensing limits what FRED redistributes).
- **How detected:** per-instrument row counts after backfill (786 rows vs ~1,713 for Treasuries).
- **Fix:** none possible keylessly; it still exceeds the 120-observation z-score minimum. Documented as a limitation.

### Partial-bar rule + idempotency skipped a final close (caught by a test)
- **What happened:** a drop received at 16:20 ET contains the day's final close but the rule (store only after 16:30) holds it back. A later run that downloads byte-identical data was classed `unchanged` and skipped, so the close wouldn't be stored until the next day's lookback.
- **How detected:** `test_partial_bars_are_not_stored` simulated a midday run then an evening run with identical bytes; the evening assertion failed.
- **Fix:** the manifest records `deferred_rows` (bars held back). An identical hash is only `unchanged` if an earlier copy had nothing deferred; otherwise it's `recheck` and re-processed.
- **Lesson:** idempotency keys must capture everything that affects the output, including time-dependent filtering — not just the input bytes.

### Yahoo emits weekend-dated FX bars
- **What happened:** a Saturday-evening ingest returned rows dated Saturday 2026-11-07 and Sunday 2026-11-08 for AUDUSD, EURJPY, USDJPY, USDCAD. Held back as "in progress" that night, they'd have been stored as real observations on Monday.
- **How detected:** investigated why the Yahoo drop showed `deferred_rows=4` on a weekend.
- **Fix:** normalisation drops weekend-dated rows for every instrument (also removes ICE's month-end weekend HY OAS marks). Test added.

### Nasdaq returns nothing for windows ending in the past
- **What happened:** fixture capture for Oct–Nov 2025 got `rows: null` with HTTP 200; `fromdate=2021-01-01&todate=<today>` returns 1,469 rows.
- **How detected:** the captured fixture was 179 bytes.
- **Fix:** fallback always asks up to today (it does anyway); a 0-row answer is now a failed drop, not a silent success.
- **Lesson:** HTTP 200 doesn't mean data — count rows.
