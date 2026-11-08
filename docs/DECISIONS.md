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
