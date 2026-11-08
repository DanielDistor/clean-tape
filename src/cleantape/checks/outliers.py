"""Outliers: today's move vs its own trailing distribution (z-score from derive).

Futures: a jump on a roll day is not a market move. If the instrument's roll-proxy ETF (which
holds the same commodity) moved normally, the message says "possible futures roll" and the
severity is capped at WARN. Futures are display-only, so they never escalate to FAIL.
"""

from __future__ import annotations

import pandas as pd

from cleantape.checks.base import CheckContext, CheckResult, Severity, fmt_move, result
from cleantape.transform.derive import series_catalog

NAME = "outliers"


def classify(z: float, warn: float, fail: float) -> Severity:
    a = abs(z)
    return Severity.FAIL if a > fail else Severity.WARN if a > warn else Severity.OK


def run(ctx: CheckContext) -> list[CheckResult]:
    d = ctx.derived
    if d.empty:
        return [
            result(
                NAME, "derived", None, Severity.FAIL, "empty", ">0 rows", "derived table is empty"
            )
        ]
    th = ctx.cfg.settings.checks.outliers
    cat = series_catalog(ctx.cfg.universe)
    insts = ctx.cfg.universe.by_id
    z_by = d.set_index(["series_id", "date"])["z_1d"]
    out = []
    rows = (
        d[d["z_1d"].abs() > th.warn_z]
        if ctx.audit
        else (d.sort_values("date").groupby("series_id").tail(1))
    )
    if ctx.audit and ctx.audit_start:
        rows = rows[rows["date"] >= ctx.audit_start]
    for r in rows.itertuples():
        meta = cat.get(r.series_id)
        if meta is None:
            continue
        if pd.isna(r.z_1d):
            out.append(
                result(
                    NAME,
                    r.series_id,
                    r.date,
                    Severity.OK,
                    "n/a",
                    f"|z| <= {th.warn_z}",
                    f"{r.series_id}: no z-score (insufficient history or no 1d move)",
                )
            )
            continue
        sev = classify(r.z_1d, th.warn_z, th.fail_z)
        msg = f"{r.series_id} {fmt_move(r.chg_1d, meta.change_mode)} ({r.z_1d:+.1f}σ vs 1y)"
        if ctx.audit:
            msg += f" on {r.date}"
        inst = insts.get(r.series_id)
        if inst is not None and inst.is_future and sev > Severity.OK:
            pz = z_by.get((inst.roll_proxy, r.date)) if inst.roll_proxy else None
            if pz is not None and not pd.isna(pz) and abs(pz) < 2:
                msg += f" — possible futures roll ({inst.roll_proxy} moved {pz:+.1f}σ)"
            sev = min(sev, Severity.WARN)
        if sev > Severity.OK:
            msg += " — verify before use"
        out.append(
            result(
                NAME,
                r.series_id,
                r.date,
                Severity(sev),
                f"{r.z_1d:+.2f}",
                f"|z| <= {th.warn_z}",
                msg,
            )
        )
    if ctx.audit and not out:
        out.append(result(NAME, "derived", None, Severity.OK, 0, "", "no outliers in history"))
    return out
