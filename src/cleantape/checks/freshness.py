"""Freshness: is the latest stored date as recent as the calendar + publication lag allow?"""

from __future__ import annotations

from cleantape.calendars import (
    business_days_behind,
    completion_rule,
    expected_latest_date,
    get_calendar,
)
from cleantape.checks.base import CheckContext, CheckResult, Severity, result

NAME = "freshness"


def run(ctx: CheckContext) -> list[CheckResult]:
    """One row per instrument. 1 business day behind = WARN; >= fail threshold = FAIL."""
    th = ctx.cfg.settings.checks.freshness
    buf = ctx.cfg.settings.nyse_close_buffer
    out = []
    for inst in ctx.instruments:
        cal = get_calendar(inst.calendar)
        rule = completion_rule(inst.source, inst.calendar, inst.is_future)
        expected = expected_latest_date(
            cal, ctx.now_utc, inst.expected_lag_business_days, rule, buf
        )
        s = ctx.series(inst.id)
        if s.empty:
            out.append(
                result(
                    NAME,
                    inst.id,
                    None,
                    Severity.FAIL,
                    "no data",
                    expected,
                    f"{inst.id}: no data stored at all",
                )
            )
            continue
        latest = s["date"].iloc[-1]
        behind = business_days_behind(cal, latest, expected)
        if behind >= th.fail_business_days:
            sev = Severity.FAIL
        elif behind >= th.warn_business_days:
            sev = Severity.WARN
        else:
            sev = Severity.OK
        msg = (
            f"{inst.id} up to date ({latest})"
            if sev == Severity.OK
            else f"{inst.id} latest {latest}, expected {expected} "
            f"({behind} business day{'s' if behind != 1 else ''} behind on {inst.calendar})"
        )
        out.append(result(NAME, inst.id, latest, sev, latest, expected, msg))
    return out
