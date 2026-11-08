"""Run all checks, persist results, compute overall status."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import pandas as pd

from cleantape.checks import (
    completeness,
    consistency,
    freshness,
    outliers,
    schema,
    staleness,
    validity,
)
from cleantape.checks.base import CheckContext, CheckResult, Severity, result
from cleantape.clock import ny_today
from cleantape.config import Config
from cleantape.logging_setup import get_logger
from cleantape.store.state import State

log = get_logger("checks")

CHECKS: dict[str, Callable[[CheckContext], list[CheckResult]]] = {
    "freshness": freshness.run,
    "completeness": completeness.run,
    "schema": schema.run,
    "validity": validity.run,
    "staleness": staleness.run,
    "outliers": outliers.run,
    "consistency": consistency.run,
}
AUDIT_CHECKS = ("validity", "staleness", "outliers", "consistency")


def build_context(
    cfg: Config,
    state: State,
    now_utc: dt.datetime,
    run_id: str,
    audit: bool = False,
    audit_start: dt.date | None = None,
) -> CheckContext:
    return CheckContext(
        cfg=cfg,
        now_utc=now_utc,
        run_id=run_id,
        observations=state.read("observations"),
        derived=state.read("derived"),
        manifest=state.read("manifest"),
        audit=audit,
        audit_start=audit_start,
    )


def run_checks(ctx: CheckContext) -> pd.DataFrame:
    """Run every check (or the audit subset). A crashing check is itself a FAIL row."""
    names = AUDIT_CHECKS if ctx.audit else tuple(CHECKS)
    rows: list[CheckResult] = []
    for name in names:
        try:
            got = CHECKS[name](ctx)
        except Exception as e:  # a broken check must never look like a passing one
            log.exception("check %s crashed", name)
            got = [result(name, "check", None, Severity.FAIL, "crashed", "", f"check crashed: {e}")]
        if not got:  # empty input must never silently pass
            got = [
                result(
                    name,
                    "check",
                    None,
                    Severity.FAIL,
                    "no results",
                    ">=1",
                    "check produced no results (no data?)",
                )
            ]
        rows.extend(got)
    df = pd.DataFrame([r.__dict__ for r in rows])
    df["severity"] = df["severity"].map(lambda s: Severity(s).name)
    df.insert(0, "run_id", ctx.run_id)
    df["as_of_date"] = df["as_of_date"].where(df["as_of_date"].notna(), ny_today(ctx.now_utc))
    return df


def overall(results: pd.DataFrame) -> Severity:
    """Worst severity; no results at all is FAIL."""
    return Severity.worst(results["severity"].tolist()) if not results.empty else Severity.FAIL


def persist(state: State, results: pd.DataFrame) -> None:
    state.append(
        "check_results",
        results[
            [
                "run_id",
                "check",
                "subject",
                "as_of_date",
                "severity",
                "observed",
                "expected",
                "message",
            ]
        ],
    )


def affected(results: pd.DataFrame, severity: str = "FAIL") -> list[str]:
    """Instrument/series ids with at least one result of ``severity``."""
    r = results[results["severity"] == severity]
    return sorted(set(r["subject"]))
