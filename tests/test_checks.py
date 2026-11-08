from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from cleantape.checks import consistency, outliers, schema
from cleantape.checks.base import Severity
from cleantape.checks.runner import affected, overall, run_checks
from cleantape.clock import NY
from cleantape.config import Config

from broken_scenarios import SCENARIOS, Scenario, clean_manifest, clean_obs, context, run_scenario
from conftest import FIXTURES


def sev(results: pd.DataFrame, check: str, subject: str) -> str:
    rows = results[(results["check"] == check) & (results["subject"] == subject)]
    assert not rows.empty, f"no {check} result for {subject}"
    return Severity.worst(rows["severity"].tolist()).name


@pytest.fixture(scope="module")
def clean_results(cfg: Config) -> pd.DataFrame:
    return run_checks(context(cfg, clean_obs()))


def test_clean_real_data_passes(clean_results: pd.DataFrame) -> None:
    problems = clean_results[clean_results["severity"] != "OK"]
    assert problems.empty, problems[["check", "subject", "message"]].to_string()
    assert overall(clean_results) == Severity.OK


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.name)
def test_each_broken_scenario_is_caught(
    cfg: Config, clean_results: pd.DataFrame, sc: Scenario
) -> None:
    if sc.subject != "observations":  # table-level rows only appear when something is wrong
        assert sev(clean_results, sc.check, sc.subject) == "OK"
    res = run_scenario(cfg, sc)
    assert sev(res, sc.check, sc.subject) == sc.severity, res[res["severity"] != "OK"][
        ["check", "subject", "severity", "message"]
    ].to_string()


def test_fail_scenarios_mark_affected_instruments(cfg: Config) -> None:
    res = run_scenario(cfg, next(s for s in SCENARIOS if s.name == "yield_250"))
    assert overall(res) == Severity.FAIL
    assert "DGS10" in affected(res)


def test_outlier_message_is_human(cfg: Config) -> None:
    res = run_scenario(cfg, next(s for s in SCENARIOS if s.name == "spike_38bp"))
    msg = res[(res["check"] == "outliers") & (res["subject"] == "DGS10")]["message"].iloc[0]
    assert msg.startswith("DGS10 +") and "bp (" in msg and "σ vs 1y) — verify before use" in msg


def test_empty_input_is_fail_everywhere(cfg: Config) -> None:
    empty = clean_obs().iloc[0:0]
    res = run_checks(context(cfg, empty, manifest=clean_manifest().iloc[0:0]))
    assert overall(res) == Severity.FAIL
    for check in [
        "freshness",
        "completeness",
        "schema",
        "validity",
        "staleness",
        "outliers",
        "consistency",
    ]:
        assert "FAIL" in set(res[res["check"] == check]["severity"]), check


def test_columbus_day_missing_treasury_is_not_late(cfg: Config) -> None:
    o = clean_obs()
    o = o[o["date"] <= dt.date(2025, 10, 13)]
    now = dt.datetime(2025, 10, 14, 7, 0, tzinfo=NY).astimezone(dt.UTC)  # Tue morning
    res = run_checks(context(cfg, o, now=now))
    assert sev(res, "freshness", "DGS10") == "OK"  # FRED has nothing for Mon 13th: bond holiday
    assert sev(res, "freshness", "SPY") == "OK"  # NYSE traded on the 13th and we have it
    assert sev(res, "completeness", "DGS10") == "OK"


def test_row_count_drop_warns(cfg: Config) -> None:
    m = clean_manifest()
    last = m["run_id"] == m["run_id"].max()
    m.loc[last & (m["source"] == "yahoo"), "rows"] = 120  # 60% fewer rows than usual
    res = run_checks(context(cfg, clean_obs(), manifest=m))
    assert sev(res, "completeness", "yahoo:daily_close") == "WARN"


def test_failed_drop_warns(cfg: Config) -> None:
    m = clean_manifest()
    last = m["run_id"] == m["run_id"].max()
    sel = last & (m["source"] == "ecb")
    m.loc[sel, "ok"] = False
    m.loc[sel, "error"] = "HTTP 503"
    res = run_checks(context(cfg, clean_obs(), manifest=m))
    assert sev(res, "completeness", "ecb:EXR") == "WARN"


def test_spread_identity_detects_corrupted_spread(cfg: Config) -> None:
    ctx = context(cfg, clean_obs())
    d = ctx.derived.copy()
    i = d[d["series_id"] == "S2S10S"].index[-1]
    d.loc[i, "value"] += 1.0
    res = consistency.spread_identity_results(context(cfg, clean_obs(), derived=d))
    r = next(x for x in res if x.subject == "S2S10S")
    assert r.severity == Severity.FAIL


def test_uncalibrated_pair_is_warn_not_ok(cfg: Config) -> None:
    ctx = context(cfg, clean_obs())
    object.__setattr__(ctx.cfg.settings.checks, "consistency", {})  # frozen model: test-only
    try:
        res = consistency.cross_results(ctx)
    finally:
        from cleantape.config import load_config

        object.__setattr__(
            ctx.cfg.settings.checks,
            "consistency",
            load_config(ctx.cfg.root).settings.checks.consistency,
        )
    assert all(r.severity == Severity.WARN for r in res)
    assert "calibrate" in res[0].message


def test_futures_outlier_capped_and_roll_hinted(cfg: Config) -> None:
    o = clean_obs()
    idx = o[(o["instrument_id"] == "CL_F")].sort_values("date").index[-1]
    o.loc[idx, "value"] *= 1.15  # +15% jump in CL=F while USO is calm
    res = outliers.run(context(cfg, o))
    r = next(x for x in res if x.subject == "CL_F")
    assert r.severity == Severity.WARN and "possible futures roll (USO" in r.message


def test_calibration_from_history(cfg: Config) -> None:
    ctx = context(cfg, clean_obs())
    tol = consistency.calibrate(
        ctx.derived, cfg.universe.instruments, cfg.settings.checks.calibration
    )
    assert {"EURUSD", "USDJPY", "EURJPY", "CL_F"} <= set(tol)
    for v in tol.values():
        assert 0 < v["warn"] <= v["fail"]
    assert tol["EURJPY"]["warn"] < tol["EURUSD"]["warn"]  # triangle is tighter than cross-source


def test_severity_worst_of_nothing_is_fail() -> None:
    assert Severity.worst([]) == Severity.FAIL
    assert Severity.worst(["OK", "WARN"]) == Severity.WARN


# ---------------------------------------------------------------- schema on raw drops


@pytest.mark.parametrize(
    ("source", "path"),
    [
        ("fred", "fred_DGS10.csv"),
        ("fred", "fred_DGS10_legacy_header_dots.csv"),
        ("ecb", "ecb_exr.csv"),
        ("nasdaq", "nasdaq_SPY.json"),
    ],
)
def test_schema_accepts_real_drops(source: str, path: str) -> None:
    assert schema.validate(source, (FIXTURES / path).read_bytes()) == []


@pytest.mark.parametrize(
    ("source", "path", "needle"),
    [
        ("fred", "broken/fred_no_date_column.csv", "no date column"),
        ("fred", "broken/fred_non_numeric.csv", "non-numeric"),
        ("ecb", "broken/ecb_missing_obs_value.csv", "OBS_VALUE"),
        ("yahoo", "broken/yahoo_bad_close.csv", "non-numeric"),
        ("yahoo", "broken/yahoo_renamed_columns.csv", "missing columns"),
    ],
)
def test_schema_rejects_broken_drops(source: str, path: str, needle: str) -> None:
    problems = schema.validate(source, (FIXTURES / path).read_bytes())
    assert problems and needle in problems[0]


def test_schema_empty_body() -> None:
    assert schema.validate("fred", b"") == ["empty body"]
