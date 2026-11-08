from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from cleantape.config import Config
from cleantape.transform.derive import (
    SeriesMeta,
    best_series,
    build_derived,
    change,
    crosses,
    series_changes,
    spreads,
    zscores,
)

D = dt.date
TS = pd.Timestamp("2025-01-01", tz="UTC")


def obs_rows(rows: list[tuple[dt.date, str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": d,
                "instrument_id": i,
                "source": s,
                "value": v,
                "ingested_at_utc": TS,
                "raw_sha256": "h",
            }
            for d, i, s, v in rows
        ]
    )


def test_change_bp_for_yields_and_spreads() -> None:
    # 4.10% -> 4.25% = +15bp
    assert change(np.array([4.25]), np.array([4.10]), "bp", "pct_yield")[0] == pytest.approx(15.0)
    # spread already in bp: 30bp -> 42bp = +12bp
    assert change(np.array([42.0]), np.array([30.0]), "bp", "bp_spread")[0] == pytest.approx(12.0)


def test_change_pct_for_prices() -> None:
    assert change(np.array([102.0]), np.array([100.0]), "pct", "price")[0] == pytest.approx(2.0)
    # WTI went negative in April 2020: % change against a non-positive base is undefined-ish but
    # finite; division by zero must give NaN, not inf
    assert np.isnan(change(np.array([1.0]), np.array([0.0]), "pct", "price")[0])


def test_spread_uses_same_date_only(cfg: Config) -> None:
    o = obs_rows(
        [
            (D(2025, 11, 6), "DGS10", "fred", 4.11),
            (D(2025, 11, 6), "DGS2", "fred", 3.57),
            (D(2025, 11, 7), "DGS10", "fred", 4.11),  # DGS2 missing on the 7th
            (D(2025, 11, 10), "DGS10", "fred", 4.13),
            (D(2025, 11, 10), "DGS2", "fred", 3.60),
        ]
    )
    s = spreads(best_series(o, cfg.universe), cfg.universe)
    s2s10s = s[s["series_id"] == "S2S10S"].set_index("date")["value"]
    assert s2s10s.index.tolist() == [D(2025, 11, 6), D(2025, 11, 10)]  # no value for the 7th
    assert s2s10s[D(2025, 11, 6)] == pytest.approx(54.0)
    assert s2s10s[D(2025, 11, 10)] == pytest.approx(53.0)


def test_implied_cross(cfg: Config) -> None:
    o = obs_rows(
        [(D(2025, 11, 7), "EURUSD", "yahoo", 1.156), (D(2025, 11, 7), "USDJPY", "yahoo", 153.1)]
    )
    c = crosses(best_series(o, cfg.universe), cfg.universe)
    assert c["value"].iloc[0] == pytest.approx(1.156 * 153.1)


def test_best_series_prefers_primary_and_fills_with_fallback(cfg: Config) -> None:
    o = obs_rows(
        [
            (D(2025, 11, 6), "SPY", "yahoo", 670.0),
            (D(2025, 11, 6), "SPY", "nasdaq", 671.0),
            (D(2025, 11, 7), "SPY", "nasdaq", 672.0),
            (D(2025, 11, 10), "SPY", "yahoo", float("nan")),
            (D(2025, 11, 10), "SPY", "nasdaq", 673.0),
        ]
    )
    b = best_series(o, cfg.universe).set_index("date")
    assert b.loc[D(2025, 11, 6), "source"] == "yahoo" and b.loc[D(2025, 11, 6), "value"] == 670.0
    assert b.loc[D(2025, 11, 7), "source"] == "nasdaq"
    assert b.loc[D(2025, 11, 10), "source"] == "nasdaq"  # null primary doesn't block fallback


def _meta(calendar: str = "nyse", mode: str = "pct", units: str = "price") -> SeriesMeta:
    return SeriesMeta("X", "X", "equities", units, mode, calendar, "equities", 1, "instrument")


def test_1d_change_is_null_across_a_gap() -> None:
    one = pd.DataFrame({"date": [D(2025, 11, 5), D(2025, 11, 7)], "value": [100.0, 110.0]})
    out = series_changes(one, _meta(), 252, 120, 3)
    assert np.isnan(out["chg_1d"].iloc[1])  # Nov 6 missing → no 1d change on Nov 7


def test_1d_change_skips_holidays_on_own_calendar() -> None:
    # NYSE closed Thanksgiving 2025-11-27: Friday's 1d change is vs Wednesday
    one = pd.DataFrame({"date": [D(2025, 11, 26), D(2025, 11, 28)], "value": [100.0, 101.0]})
    out = series_changes(one, _meta(), 252, 120, 3)
    assert out["chg_1d"].iloc[1] == pytest.approx(1.0)


def test_bond_calendar_1d_change_across_veterans_day() -> None:
    one = pd.DataFrame({"date": [D(2025, 11, 10), D(2025, 11, 12)], "value": [4.13, 4.08]})
    out = series_changes(one, _meta("us_bond", "bp", "pct_yield"), 252, 120, 3)
    assert out["chg_1d"].iloc[1] == pytest.approx(-5.0)


def test_ytd_and_1w_changes() -> None:
    days = pd.bdate_range("2024-12-20", "2025-01-10").date
    one = pd.DataFrame({"date": days, "value": np.arange(len(days), dtype=float) + 100})
    out = series_changes(one, _meta("fx_weekday"), 252, 120, 3).set_index("date")
    # YTD ref = last session on or before 2024-12-31 (Tue) = value at 2024-12-31
    v_dec31 = out.loc[D(2024, 12, 31), "value"]
    assert out.loc[D(2025, 1, 10), "chg_ytd"] == pytest.approx(
        (out.loc[D(2025, 1, 10), "value"] / v_dec31 - 1) * 100
    )
    # 1w: Fri Jan 10 vs Fri Jan 3
    assert out.loc[D(2025, 1, 10), "chg_1w"] == pytest.approx(
        (out.loc[D(2025, 1, 10), "value"] / out.loc[D(2025, 1, 3), "value"] - 1) * 100
    )


def test_zscore_excludes_today_and_needs_min_history() -> None:
    rng = np.random.default_rng(0)
    moves = rng.normal(0, 1, 200)
    moves[-1] = 10.0
    z = zscores(moves, window=252, min_obs=120)
    assert np.isnan(z[:120]).all() and not np.isnan(z[120])
    hist = moves[:-1]
    expected = (10.0 - hist.mean()) / hist.std(ddof=1)
    assert z[-1] == pytest.approx(expected)  # today's spike is not in its own baseline


def test_zscore_window_caps_history() -> None:
    moves = np.concatenate(
        [np.full(300, 5.0) + np.tile([0.1, -0.1], 150), np.tile([1.0, -1.0], 130), [3.0]]
    )
    z = zscores(moves, window=252, min_obs=120)
    hist = moves[-253:-1]
    assert z[-1] == pytest.approx((3.0 - hist.mean()) / hist.std(ddof=1))


def test_build_derived_end_to_end(cfg: Config) -> None:
    o = obs_rows(
        [
            (D(2025, 11, 6), "DGS10", "fred", 4.11),
            (D(2025, 11, 6), "DGS2", "fred", 3.57),
            (D(2025, 11, 7), "DGS10", "fred", 4.11),
            (D(2025, 11, 7), "DGS2", "fred", 3.55),
        ]
    )
    d = build_derived(o, cfg.universe, 252, 120, 3)
    s = d[(d["series_id"] == "S2S10S") & (d["date"] == D(2025, 11, 7))]
    assert s["value"].iloc[0] == pytest.approx(56.0) and s["chg_1d"].iloc[0] == pytest.approx(2.0)
    assert set(d.columns) >= {"chg_1d", "chg_1w", "chg_1m", "chg_ytd", "z_1d", "source"}
