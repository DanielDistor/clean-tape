"""Stale values: the same value for N+ consecutive sessions — the classic dead-feed symptom."""

from __future__ import annotations

import pandas as pd

from cleantape.checks.base import CheckContext, CheckResult, Severity, result

NAME = "staleness"


def trailing_repeat(values: pd.Series) -> int:
    """Length of the run of identical values at the end of ``values``."""
    if values.empty:
        return 0
    v = values.to_numpy()
    n = 1
    for x in v[-2::-1]:
        if x != v[-1]:
            break
        n += 1
    return n


def repeat_runs(values: pd.Series, min_len: int) -> list[tuple[int, int]]:
    """(start_index, length) of every run of identical values with length >= ``min_len``."""
    if values.empty:
        return []
    groups = (values != values.shift()).cumsum()
    runs = groups.groupby(groups).agg(["size", lambda g: g.index[0]])
    runs.columns = ["size", "start"]
    return [(int(r.start), int(r.size)) for r in runs.itertuples() if r.size >= min_len]


def run(ctx: CheckContext) -> list[CheckResult]:
    n = ctx.cfg.settings.checks.staleness.warn_sessions
    out = []
    for inst in ctx.instruments:
        s = ctx.series(inst.id)
        if s.empty:
            out.append(
                result(NAME, inst.id, None, Severity.FAIL, "no data", "", f"{inst.id}: no data")
            )
            continue
        if ctx.audit:
            runs = repeat_runs(s["value"], n)
            sev = Severity.WARN if runs else Severity.OK
            ex = ""
            if runs:
                start, length = max(runs, key=lambda r: r[1])
                ex = f", longest {length} from {s['date'].iloc[start]}"
            msg = f"{inst.id}: {len(runs)} run(s) of >= {n} identical values{ex}"
            out.append(result(NAME, inst.id, s["date"].iloc[-1], sev, len(runs), 0, msg))
            continue
        k = trailing_repeat(s["value"])
        sev = Severity.WARN if k >= n else Severity.OK
        msg = (
            f"{inst.id}: value {s['value'].iloc[-1]:g} unchanged for {k} sessions — dead feed?"
            if sev
            else f"{inst.id}: value moving"
        )
        out.append(result(NAME, inst.id, s["date"].iloc[-1], sev, k, f"< {n}", msg))
    return out
