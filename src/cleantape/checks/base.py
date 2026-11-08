"""Shared types for data-quality checks."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import IntEnum
from functools import cached_property

import pandas as pd

from cleantape.config import Config, Instrument
from cleantape.transform.derive import best_series


class Severity(IntEnum):
    OK = 0
    WARN = 1
    FAIL = 2

    @classmethod
    def worst(cls, values: list[Severity] | list[str]) -> Severity:
        sev = [v if isinstance(v, Severity) else cls[v] for v in values]
        return max(sev, default=cls.FAIL)  # no results at all is itself a failure


@dataclass(frozen=True)
class CheckResult:
    check: str
    subject: str
    as_of_date: dt.date | None
    severity: Severity
    observed: str
    expected: str
    message: str


@dataclass
class CheckContext:
    """Everything a check may look at. Built once per run."""

    cfg: Config
    now_utc: dt.datetime
    run_id: str
    observations: pd.DataFrame
    derived: pd.DataFrame
    manifest: pd.DataFrame
    audit: bool = False
    audit_start: dt.date | None = None
    _cache: dict[str, object] = field(default_factory=dict)

    @cached_property
    def best(self) -> pd.DataFrame:
        """Best-source series (primary, else fallback), non-null values only."""
        return best_series(self.observations, self.cfg.universe)

    def series(self, instrument_id: str) -> pd.DataFrame:
        """Best values for one instrument, sorted by date (columns date, value, source)."""
        b = self.best
        return b[b["series_id"] == instrument_id].sort_values("date").reset_index(drop=True)

    @property
    def instruments(self) -> list[Instrument]:
        return self.cfg.universe.instruments

    @property
    def run_manifest(self) -> pd.DataFrame:
        m = self.manifest
        return m[m["run_id"] == self.run_id] if not m.empty else m


def result(
    check: str,
    subject: str,
    as_of: dt.date | None,
    severity: Severity,
    observed: object = "",
    expected: object = "",
    message: str = "",
) -> CheckResult:
    """Convenience constructor that stringifies observed/expected."""
    return CheckResult(check, subject, as_of, severity, str(observed), str(expected), message)


def fmt_move(value: float, change_mode: str) -> str:
    """'+38bp' or '-1.25%'."""
    return f"{value:+.0f}bp" if change_mode == "bp" else f"{value:+.2f}%"
