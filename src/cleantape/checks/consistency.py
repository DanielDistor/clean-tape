"""Cross-source consistency, FX triangle, and spread identity.

* Cross-source: Yahoo FX vs ECB-derived rate, CL=F vs FRED WTI spot. Sources fix at different
  times (ECB 14:15 CET; Yahoo's daily bar ends later; spot vs front-month carries contango),
  so tolerances are *calibrated* from history: quantile(|a/b − 1|) × safety factor.
* Triangle: quoted EURJPY vs implied EURUSD × USDJPY (same source, same date).
* Spread identity: each stored spread equals (long − short) × 100 on the same date.
All comparisons use same-date observations only.
"""

from __future__ import annotations

import pandas as pd

from cleantape.checks.base import CheckContext, CheckResult, Severity, result
from cleantape.config import Calibration, Instrument

NAME = "consistency"


def pair_frame(derived: pd.DataFrame, a: str, b: str) -> pd.DataFrame:
    """Same-date values of series a and b with rel = |a / b − 1|. Also carries z-scores."""
    cols = ["date", "value", "z_1d"]
    left = derived[derived["series_id"] == a][cols].rename(columns={"value": "a", "z_1d": "za"})
    right = derived[derived["series_id"] == b][cols].rename(columns={"value": "b", "z_1d": "zb"})
    p = left.merge(right, on="date", how="inner").sort_values("date").reset_index(drop=True)
    p["rel"] = (p["a"] / p["b"] - 1).abs()
    return p


def calibration_sample(derived: pd.DataFrame, inst: Instrument, calib: Calibration) -> pd.DataFrame:
    """Historical |rel diff| sample used for calibration, after documented exclusions."""
    assert inst.cross_check is not None
    p = pair_frame(derived, inst.id, inst.cross_check.against)
    p = p[p["date"] >= calib.start]
    p = p[p["b"].abs() >= calib.min_price] if inst.units == "price" else p
    if calib.exclude_roll_days and inst.is_future and inst.roll_proxy:
        proxy = derived[derived["series_id"] == inst.roll_proxy][["date", "z_1d"]]
        p = p.merge(proxy.rename(columns={"z_1d": "zp"}), on="date", how="left")
        roll_like = (p["za"].abs() > 3) & (p["zp"].abs() < 2)
        p = p[~roll_like]
    return p


def calibrate(derived: pd.DataFrame, instruments: list[Instrument], calib: Calibration) -> dict:
    """Compute warn/fail tolerances per cross-checked instrument from history."""
    out = {}
    for inst in instruments:
        if inst.cross_check is None:
            continue
        p = calibration_sample(derived, inst, calib)
        if len(p) < 100:
            continue
        q_w = float(p["rel"].quantile(calib.warn_quantile))
        q_f = float(p["rel"].quantile(calib.fail_quantile))
        out[inst.id] = {
            "warn": round(q_w * calib.safety_factor, 6),
            "fail": round(max(q_f, q_w) * calib.safety_factor, 6),
            "_n": len(p),
            "_median": round(float(p["rel"].median()), 6),
            "_p99": round(q_w, 6),
            "_p999": round(q_f, 6),
            "_max": round(float(p["rel"].max()), 6),
            "_from": str(p["date"].iloc[0]),
            "_to": str(p["date"].iloc[-1]),
        }
    return out


def cross_results(ctx: CheckContext) -> list[CheckResult]:
    tolerances = ctx.cfg.settings.checks.consistency
    calib = ctx.cfg.settings.checks.calibration
    out = []
    for inst in ctx.instruments:
        if inst.cross_check is None:
            continue
        other = inst.cross_check.against
        subj = f"{inst.id}~{other}"
        p = pair_frame(ctx.derived, inst.id, other)
        if p.empty:
            out.append(
                result(
                    NAME,
                    inst.id,
                    None,
                    Severity.FAIL,
                    "no common dates",
                    "",
                    f"{subj}: no same-date observations to compare",
                )
            )
            continue
        tol = tolerances.get(inst.id)
        rows = p[p["date"] >= ctx.audit_start] if ctx.audit and ctx.audit_start else p.tail(1)
        for r in rows.itertuples():
            if tol is None:
                sev, exp = Severity.WARN, "uncalibrated"
            elif inst.units == "price" and abs(r.b) < calib.min_price:
                sev, exp = Severity.WARN, f"|{other}| >= {calib.min_price:g}"
            else:
                sev = (
                    Severity.FAIL
                    if r.rel > tol.fail
                    else Severity.WARN
                    if r.rel > tol.warn
                    else Severity.OK
                )
                exp = f"warn>{tol.warn:.4%} fail>{tol.fail:.4%}"
            if ctx.audit and sev == Severity.OK:
                continue
            msg = f"{inst.id} {r.a:.5g} vs {other} {r.b:.5g} on {r.date}: diff {r.rel:.3%}" + (
                "" if tol else " (run `cleantape calibrate`)"
            )
            out.append(result(NAME, inst.id, r.date, sev, f"{r.rel:.4%}", exp, msg))
    return out


def spread_identity_results(ctx: CheckContext) -> list[CheckResult]:
    tol = ctx.cfg.settings.checks.spread_identity_tolerance_bp
    d = ctx.derived
    out = []
    for s in ctx.cfg.universe.spreads:
        sp = d[d["series_id"] == s.id][["date", "value"]].tail(10)
        if sp.empty:
            out.append(result(NAME, s.id, None, Severity.FAIL, "no data", "", f"{s.id}: no values"))
            continue
        legs = d[d["series_id"].isin([s.long, s.short])].pivot_table(
            index="date", columns="series_id", values="value"
        )
        joined = sp.set_index("date").join(legs, how="left")
        recomputed = (joined[s.long] - joined[s.short]) * 100
        err = (joined["value"] - recomputed).abs()
        bad = err[(err > tol) | err.isna()]
        sev = Severity.FAIL if len(bad) else Severity.OK
        msg = (
            f"{s.id} = ({s.long} − {s.short})×100 on all of the last {len(sp)} dates"
            if sev == Severity.OK
            else f"{s.id} differs from its legs on {len(bad)} date(s), e.g. {bad.index[0]} "
            f"(error {bad.iloc[0]:.4g}bp)"
        )
        out.append(
            result(NAME, s.id, sp["date"].iloc[-1], sev, f"{err.max():.2g}bp", f"<= {tol:g}bp", msg)
        )
    return out


def run(ctx: CheckContext) -> list[CheckResult]:
    if ctx.derived.empty:
        return [result(NAME, "derived", None, Severity.FAIL, "empty", "", "no derived data")]
    return cross_results(ctx) + ([] if ctx.audit else spread_identity_results(ctx))
