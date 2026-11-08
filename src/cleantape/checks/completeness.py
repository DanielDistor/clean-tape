"""Completeness: gaps in the lookback window, failed drops, and raw row counts vs history."""

from __future__ import annotations

import datetime as dt

from cleantape.calendars import completion_rule, expected_latest_date, get_calendar
from cleantape.checks.base import CheckContext, CheckResult, Severity, result

NAME = "completeness"


def gap_results(ctx: CheckContext) -> list[CheckResult]:
    """Sessions with no value within the last N sessions (up to the expected/latest date)."""
    th = ctx.cfg.settings.checks.completeness
    out = []
    for inst in ctx.instruments:
        s = ctx.series(inst.id)
        if s.empty:
            out.append(
                result(
                    NAME, inst.id, None, Severity.FAIL, 0, ">0 rows", f"{inst.id}: no observations"
                )
            )
            continue
        cal = get_calendar(inst.calendar)
        rule = completion_rule(inst.source, inst.calendar, inst.is_future)
        expected = expected_latest_date(
            cal,
            ctx.now_utc,
            inst.expected_lag_business_days,
            rule,
            ctx.cfg.settings.nyse_close_buffer,
        )
        end = min(expected, s["date"].iloc[-1])  # lateness itself is freshness's job
        start = max(cal.shift_back(end, th.lookback_business_days - 1), s["date"].iloc[0])
        have = set(s["date"])
        missing = [d for d in cal.between(start, end) if d not in have]
        if len(missing) >= th.fail_missing_sessions:
            sev = Severity.FAIL
        elif missing:
            sev = Severity.WARN
        else:
            sev = Severity.OK
        shown = ", ".join(str(d) for d in missing[:5]) + ("…" if len(missing) > 5 else "")
        msg = (
            f"{inst.id}: no gaps in last {th.lookback_business_days} sessions"
            if not missing
            else f"{inst.id}: {len(missing)} missing session(s) {start}..{end}: {shown}"
        )
        out.append(result(NAME, inst.id, end, sev, len(missing), 0, msg))
    return out


def drop_results(ctx: CheckContext) -> list[CheckResult]:
    """Failed drops in the latest ingest run, and row counts vs the trailing median."""
    tol = ctx.cfg.settings.checks.completeness.row_count_tolerance
    m = ctx.manifest
    if m.empty:
        return [
            result(
                NAME,
                "manifest",
                None,
                Severity.FAIL,
                "empty",
                ">0 drops",
                "no drops have ever been received",
            )
        ]
    last_run = m["run_id"].max()
    run = m[m["run_id"] == last_run]
    as_of: dt.date = run["as_of_date"].iloc[0]
    out = []
    for _, d in run.iterrows():
        subject = f"{d['source']}:{d['dataset']}"
        if not d["ok"]:
            out.append(
                result(
                    NAME,
                    subject,
                    as_of,
                    Severity.WARN,
                    "failed",
                    "ok",
                    f"drop failed: {d['error']}",
                )
            )
            continue
        hist = m[
            (m["source"] == d["source"])
            & (m["dataset"] == d["dataset"])
            & m["ok"]
            & (m["window_days"] == d["window_days"])
            & (m["run_id"] < last_run)
        ]
        hist = hist.tail(20)
        if len(hist) < 3 or not d["rows"]:
            out.append(
                result(
                    NAME,
                    subject,
                    as_of,
                    Severity.OK,
                    d["rows"],
                    "n/a",
                    f"{subject}: {d['rows']} rows (not enough history to compare)",
                )
            )
            continue
        med = float(hist["rows"].median())
        ratio = float(d["rows"]) / med if med else float("inf")
        sev = Severity.WARN if abs(ratio - 1) > tol else Severity.OK
        out.append(
            result(
                NAME,
                subject,
                as_of,
                sev,
                d["rows"],
                f"{med:.0f} ±{tol:.0%}",
                f"{subject}: {d['rows']} rows vs median {med:.0f} ({ratio - 1:+.0%})",
            )
        )
    return out


def run(ctx: CheckContext) -> list[CheckResult]:
    return gap_results(ctx) + drop_results(ctx)
