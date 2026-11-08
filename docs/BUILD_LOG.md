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
