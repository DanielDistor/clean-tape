"""Validity: unexpected nulls, values outside realistic bounds, duplicate keys.

Bounds are set wide enough for real history (WTI front-month −$37.63 on 2020-04-20, VIX ~82
in March 2020, 3M bills ≈ 0% in 2020–21) yet tight enough that broken data (a yield of
250, a price of 0) fails. ``cleantape check --audit`` runs this over all history.
"""

from __future__ import annotations

import datetime as dt

from cleantape.checks.base import CheckContext, CheckResult, Severity, result

NAME = "validity"


def _window_start(ctx: CheckContext) -> dt.date:
    if ctx.audit:
        return ctx.audit_start or dt.date(1900, 1, 1)
    lookback = ctx.cfg.settings.checks.completeness.lookback_business_days
    return ctx.now_utc.date() - dt.timedelta(days=int(lookback * 7 / 5) + 4)


def run(ctx: CheckContext) -> list[CheckResult]:
    o = ctx.observations
    if o.empty:
        return [
            result(
                NAME,
                "observations",
                None,
                Severity.FAIL,
                "empty",
                ">0 rows",
                "observations table is empty",
            )
        ]
    out: list[CheckResult] = []
    dupes = o.duplicated(["date", "instrument_id", "source"], keep=False)
    if dupes.any():
        out.append(
            result(
                NAME,
                "observations",
                None,
                Severity.FAIL,
                int(dupes.sum()),
                0,
                f"{int(dupes.sum())} duplicate (date, instrument, source) keys",
            )
        )
    start = _window_start(ctx)
    win = o[o["date"] >= start]
    for inst in ctx.instruments:
        rows = win[win["instrument_id"] == inst.id]
        if rows.empty:
            continue  # absence is freshness/completeness's job
        problems: list[str] = []
        sev = Severity.OK
        nulls = rows[rows["value"].isna()]
        if len(nulls):
            sev = Severity.WARN
            problems.append(
                f"{len(nulls)} null value(s) on session days, e.g. {nulls['date'].iloc[0]}"
            )
        b = inst.bounds
        oob = rows[(rows["value"] < b.min) | (rows["value"] > b.max)]
        if len(oob):
            sev = Severity.FAIL
            worst = oob.iloc[0]
            problems.append(
                f"{len(oob)} value(s) outside [{b.min:g}, {b.max:g}], e.g. "
                f"{worst['value']:g} on {worst['date']} ({worst['source']})"
            )
        last = rows["date"].max()
        msg = f"{inst.id}: " + ("; ".join(problems) if problems else "values valid")
        observed = f"{len(nulls)} nulls, {len(oob)} out of bounds"
        out.append(result(NAME, inst.id, last, sev, observed, f"[{b.min:g}, {b.max:g}]", msg))
    return out
