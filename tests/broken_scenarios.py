"""Deliberately broken variants of real data, shared by tests and scripts/demo_checks.py.

Each scenario takes a copy of real observations (tests/fixtures/observations_sample.parquet,
captured 2026-11-07) and breaks it the way real feeds break.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from cleantape.checks.base import CheckContext
from cleantape.checks.runner import run_checks
from cleantape.clock import NY
from cleantape.config import Config
from cleantape.transform.derive import build_derived

from conftest import FIXTURES

# Saturday evening after the sample was captured: everything in the sample is "fresh".
NOW = dt.datetime(2026, 11, 7, 20, 0, tzinfo=NY).astimezone(dt.UTC)


def clean_obs() -> pd.DataFrame:
    return pd.read_parquet(FIXTURES / "observations_sample.parquet")


def clean_manifest(runs: int = 6) -> pd.DataFrame:
    """A plausible manifest: N previous lookback runs with stable row counts."""
    rows = []
    for k in range(runs):
        run_id = f"2026110{k}T233700Z"
        for src, ds, n in [
            ("fred", "DGS10", 13),
            ("yahoo", "daily_close", 300),
            ("ecb", "EXR", 78),
        ]:
            rows.append(
                {
                    "run_id": run_id,
                    "source": src,
                    "dataset": ds,
                    "as_of_date": dt.date(2026, 11, 1 + k),
                    "path": None,
                    "received_at_utc": pd.Timestamp("2026-11-01", tz="UTC"),
                    "http_status": 200,
                    "bytes": 1000,
                    "rows": n,
                    "sha256": "h",
                    "duration_ms": 10.0,
                    "ok": True,
                    "status": "unchanged",
                    "deferred_rows": 0,
                    "window_days": 18,
                    "error": None,
                }
            )
    m = pd.DataFrame(rows)
    m["http_status"] = m["http_status"].astype("Int32")
    m["rows"] = m["rows"].astype("Int64")
    return m


def context(
    cfg: Config,
    obs: pd.DataFrame,
    manifest: pd.DataFrame | None = None,
    derived: pd.DataFrame | None = None,
    now: dt.datetime = NOW,
) -> CheckContext:
    d = cfg.settings.derive
    if derived is None:
        derived = build_derived(
            obs, cfg.universe, d.zscore_window, d.zscore_min_obs, d.ffill_max_days
        )
    return CheckContext(
        cfg=cfg,
        now_utc=now,
        run_id="test",
        observations=obs,
        derived=derived,
        manifest=clean_manifest() if manifest is None else manifest,
    )


def _last(obs: pd.DataFrame, iid: str, n: int = 1) -> pd.Index:
    rows = obs[obs["instrument_id"] == iid].sort_values("date")
    return rows.index[-n:]


# ---------------------------------------------------------------- scenario mutators


def late_by(iid: str, sessions: int) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def f(o: pd.DataFrame) -> pd.DataFrame:
        return o.drop(index=_last(o, iid, sessions))

    return f


def gap_in_middle(iid: str, back: int = 4) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def f(o: pd.DataFrame) -> pd.DataFrame:
        return o.drop(index=_last(o, iid, back)[:1])

    return f


def set_last(iid: str, value: float) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def f(o: pd.DataFrame) -> pd.DataFrame:
        o = o.copy()
        o.loc[_last(o, iid), "value"] = value
        return o

    return f


def scale_last(iid: str, factor: float) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def f(o: pd.DataFrame) -> pd.DataFrame:
        o = o.copy()
        idx = _last(o, iid)
        o.loc[idx, "value"] = o.loc[idx, "value"] * factor
        return o

    return f


def bump_last(iid: str, delta: float) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def f(o: pd.DataFrame) -> pd.DataFrame:
        o = o.copy()
        idx = _last(o, iid)
        o.loc[idx, "value"] = o.loc[idx, "value"] + delta
        return o

    return f


def flatline(iid: str, sessions: int = 5) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def f(o: pd.DataFrame) -> pd.DataFrame:
        o = o.copy()
        idx = _last(o, iid, sessions)
        o.loc[idx, "value"] = o.loc[idx[0], "value"]
        return o

    return f


def duplicate_row(iid: str) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def f(o: pd.DataFrame) -> pd.DataFrame:
        return pd.concat([o, o.loc[_last(o, iid)]], ignore_index=True)

    return f


@dataclass(frozen=True)
class Scenario:
    name: str
    mutate: Callable[[pd.DataFrame], pd.DataFrame]
    check: str
    subject: str
    severity: str
    description: str


SCENARIOS = [
    Scenario(
        "late_1_day", late_by("SPY", 1), "freshness", "SPY", "WARN", "SPY missing its latest close"
    ),
    Scenario(
        "late_3_days",
        late_by("DGS10", 3),
        "freshness",
        "DGS10",
        "FAIL",
        "FRED 10y yield three sessions behind",
    ),
    Scenario(
        "gap",
        gap_in_middle("EURUSD"),
        "completeness",
        "EURUSD",
        "WARN",
        "one EURUSD session missing inside the lookback window",
    ),
    Scenario(
        "yield_250",
        set_last("DGS10", 250.0),
        "validity",
        "DGS10",
        "FAIL",
        "10y yield of 250% (decimal-point error)",
    ),
    Scenario("price_zero", set_last("SPY", 0.0), "validity", "SPY", "FAIL", "SPY price of 0"),
    Scenario(
        "null_on_session",
        set_last("USDJPY", np.nan),
        "validity",
        "USDJPY",
        "WARN",
        "null USDJPY value on a trading day",
    ),
    Scenario(
        "dead_feed",
        flatline("GLD", 5),
        "staleness",
        "GLD",
        "WARN",
        "GLD repeats the same price for 5 sessions",
    ),
    Scenario(
        "spike_38bp",
        bump_last("DGS10", 0.38),
        "outliers",
        "DGS10",
        "FAIL",
        "10y yield jumps 38bp in a day",
    ),
    Scenario(
        "ecb_off_5pct",
        scale_last("EURUSD_ECB", 1.05),
        "consistency",
        "EURUSD",
        "FAIL",
        "ECB EURUSD 5% away from Yahoo",
    ),
    Scenario(
        "triangle_1pct",
        scale_last("EURJPY", 1.01),
        "consistency",
        "EURJPY",
        "FAIL",
        "quoted EURJPY 1% off EURUSD×USDJPY",
    ),
    Scenario(
        "duplicate_key",
        duplicate_row("QQQ"),
        "validity",
        "observations",
        "FAIL",
        "same (date, instrument, source) stored twice",
    ),
]


def run_scenario(cfg: Config, sc: Scenario) -> pd.DataFrame:
    return run_checks(context(cfg, sc.mutate(clean_obs())))
