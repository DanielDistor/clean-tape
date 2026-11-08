"""Every P&L rule against numbers computed by hand (shown in comments)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cleantape.config import Config, Shock
from cleantape.portfolio.pnl import (
    NA,
    Position,
    aggregate,
    fx_exposure_usd,
    load_positions,
    pnl_dv01,
    pnl_fx,
    pnl_shares,
    shock_pnl,
    totals,
    value_positions,
)

D = dt.date
T, PREV = D(2025, 11, 7), D(2025, 11, 6)  # Fri, Thu: sessions on every calendar


def test_shares_long_and_short() -> None:
    # long 1,000 SPY 500.00 -> 505.50: 1,000 × 5.50 = +5,500
    assert pnl_shares(1000, 505.50, 500.00) == pytest.approx(5500.0)
    # short 2,000 QQQ 400 -> 404: -2,000 × 4 = -8,000
    assert pnl_shares(-2000, 404.0, 400.0) == pytest.approx(-8000.0)


def test_dv01_long_and_short_duration() -> None:
    # long $25k DV01, 10y 4.10% -> 4.18% (+8bp): -25,000 × 8 = -200,000
    assert pnl_dv01(25_000, 4.18, 4.10) == pytest.approx(-200_000.0)
    # short $18k DV01 (dv01 = -18k), 2y 3.60% -> 3.55% (-5bp): -(-18,000) × -5 = -90,000
    assert pnl_dv01(-18_000, 3.55, 3.60) == pytest.approx(-90_000.0)


def test_fx_quote_usd_pair() -> None:
    # long EUR 10m, EURUSD 1.1500 -> 1.1550: 10,000,000 × 0.0050 = +50,000 USD
    assert pnl_fx("EURUSD", 10_000_000, 1.1550, 1.1500, {}) == pytest.approx(50_000.0)


def test_fx_base_usd_pair_converted_from_jpy() -> None:
    # short USD 6m vs JPY, USDJPY 150.00 -> 151.50:
    # JPY P&L = -6,000,000 × 1.50 = -9,000,000 JPY; ÷ 151.50 = -59,405.94 USD
    assert pnl_fx("USDJPY", -6_000_000, 151.50, 150.00, {}) == pytest.approx(-9_000_000 / 151.5)
    assert pnl_fx("USDJPY", -6_000_000, 151.50, 150.00, {}) == pytest.approx(-59_405.94, abs=0.01)


def test_fx_non_usd_cross_converts_via_usd_pair() -> None:
    # long EUR 1m vs JPY, EURJPY 170 -> 171: 1,000,000 JPY ÷ USDJPY 150 = 6,666.67 USD
    assert pnl_fx("EURJPY", 1_000_000, 171.0, 170.0, {"USDJPY": 150.0}) == pytest.approx(
        6666.666, abs=0.01
    )
    assert np.isnan(pnl_fx("EURJPY", 1_000_000, 171.0, 170.0, {}))


def test_fx_exposure_usd() -> None:
    assert fx_exposure_usd("EURUSD", 10_000_000, 1.15, {}) == pytest.approx(11_500_000)
    assert fx_exposure_usd("USDJPY", -6_000_000, 150.0, {}) == pytest.approx(-6_000_000)


# ------------------------------------------------------------------ valuation


def _positions() -> list[Position]:
    def p(
        pid: str,
        iid: str,
        book: str,
        kind: str,
        qty: float | None = None,
        dv01: float | None = None,
    ) -> Position:
        return Position(
            position_id=pid,
            instrument_id=iid,
            book=book,
            kind=kind,  # type: ignore[arg-type]
            quantity=qty,
            dv01_usd=dv01,
            entry_date=D(2025, 1, 2),
        )

    return [
        p("E1", "SPY", "equity_ls", "shares", qty=1000),
        p("E2", "QQQ", "equity_ls", "shares", qty=-2000),
        p("R1", "DGS10", "macro_rates", "dv01", dv01=25_000),
        p("R2", "DGS2", "macro_rates", "dv01", dv01=-18_000),
        p("F1", "EURUSD", "fx", "fx_notional", qty=10_000_000),
        p("F2", "USDJPY", "fx", "fx_notional", qty=-6_000_000),
    ]


def _wide() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "SPY": [500.0, 505.5],
            "QQQ": [400.0, 404.0],
            "DGS10": [4.10, 4.18],
            "DGS2": [3.60, 3.55],
            "EURUSD": [1.15, 1.155],
            "USDJPY": [150.0, 151.5],
        },
        index=[PREV, T],
    )


def _as_of() -> dict[str, dt.date]:
    return dict.fromkeys(["SPY", "QQQ", "DGS10", "DGS2", "EURUSD", "USDJPY"], T)


def test_value_positions_and_aggregates(cfg: Config) -> None:
    v = value_positions(_positions(), _wide(), cfg.universe, _as_of(), set())
    pnl = v.set_index("position_id")["pnl_usd"]
    assert pnl.to_dict() == pytest.approx(
        {
            "E1": 5_500.0,
            "E2": -8_000.0,
            "R1": -200_000.0,
            "R2": -90_000.0,
            "F1": 50_000.0,
            "F2": -9_000_000 / 151.5,
        }
    )
    books = aggregate(v, "book").set_index("book")
    assert books.loc["equity_ls", "pnl_usd"] == pytest.approx(-2_500.0)
    assert books.loc["macro_rates", "pnl_usd"] == pytest.approx(-290_000.0)
    assert books.loc["macro_rates", "dv01_usd"] == pytest.approx(7_000.0)
    # equity gross = 1000×505.5 + 2000×404 = 1,313,500; net = 505,500 − 808,000 = −302,500
    assert books.loc["equity_ls", "gross_exposure_usd"] == pytest.approx(1_313_500.0)
    assert books.loc["equity_ls", "net_exposure_usd"] == pytest.approx(-302_500.0)
    t = totals(v)
    assert t["pnl_usd"] == pytest.approx(
        5_500 - 8_000 - 200_000 - 90_000 + 50_000 - 9_000_000 / 151.5
    )
    assert t["n_a"] == 0


def test_missing_input_is_na_not_a_number(cfg: Config) -> None:
    w = _wide()
    w.loc[PREV, "SPY"] = np.nan
    v = value_positions(_positions(), w, cfg.universe, _as_of(), set()).set_index("position_id")
    assert v.loc["E1", "status"] == NA and np.isnan(v.loc["E1", "pnl_usd"])
    assert "2025-11-06" in v.loc["E1", "reason"]
    assert v.loc["E2", "status"] == "ok"
    # the book total is the sum of what *is* known, and reports one n/a
    b = aggregate(v.reset_index(), "book").set_index("book")
    assert (
        b.loc["equity_ls", "pnl_usd"] == pytest.approx(-8_000.0) and b.loc["equity_ls", "n_a"] == 1
    )


def test_fail_flagged_instrument_is_na(cfg: Config) -> None:
    v = value_positions(_positions(), _wide(), cfg.universe, _as_of(), {"DGS10"}).set_index(
        "position_id"
    )
    assert v.loc["R1", "status"] == NA and "FAIL" in v.loc["R1", "reason"]


def test_previous_session_uses_instrument_calendar(cfg: Config) -> None:
    # Veterans Day 2025-11-11: bonds closed. DGS10 on Wed 12th compares with Mon 10th.
    w = pd.DataFrame({"DGS10": [4.13, 4.08]}, index=[D(2025, 11, 10), D(2025, 11, 12)])
    pos = [p for p in _positions() if p.position_id == "R1"]
    v = value_positions(pos, w, cfg.universe, {"DGS10": D(2025, 11, 12)}, set())
    assert v["prev_date"].iloc[0] == D(2025, 11, 10)
    assert v["pnl_usd"].iloc[0] == pytest.approx(-25_000 * -5.0)  # yields fell 5bp: +125,000


def test_shocks_are_linear(cfg: Config) -> None:
    v = value_positions(_positions(), _wide(), cfg.universe, _as_of(), set())
    # rates +50bp: -(25,000 − 18,000) × 50 = -350,000
    assert shock_pnl(v, Shock(name="r", kind="rates_bp", size=50)) == pytest.approx(-350_000)
    # equities -5%: net equity exposure −302,500 × −5% = +15,125
    assert shock_pnl(
        v, Shock(name="e", kind="asset_class_pct", target="equities", size=-5)
    ) == pytest.approx(15_125)
    # USD +3%: EURUSD long 10m × 1.155 × −3% = −346,500; USDJPY short 6m: −6m × 3% = −180,000
    assert shock_pnl(v, Shock(name="u", kind="usd_pct", size=3)) == pytest.approx(
        -346_500 - 180_000
    )


def test_real_portfolio_file_validates(cfg: Config) -> None:
    pos = load_positions(cfg.path(cfg.settings.paths.portfolio), cfg.universe)
    assert len(pos) >= 12
    kinds = {p.kind for p in pos}
    assert kinds == {"shares", "dv01", "fx_notional"}
    assert any(p.kind == "shares" and (p.quantity or 0) < 0 for p in pos)  # a short
    dv01s = [p.dv01_usd for p in pos if p.kind == "dv01"]
    assert any(d > 0 for d in dv01s) and any(d < 0 for d in dv01s)  # long and short duration


def test_portfolio_rejects_futures_and_kind_mismatch(cfg: Config, tmp_path: Path) -> None:
    f = tmp_path / "p.csv"
    f.write_text(
        "position_id,instrument_id,book,kind,quantity,dv01_usd,entry_date,note\n"
        "X1,CL_F,commodities,shares,100,,2025-01-02,\n"
    )
    with pytest.raises(ValueError, match="display-only"):
        load_positions(f, cfg.universe)
    f.write_text(
        "position_id,instrument_id,book,kind,quantity,dv01_usd,entry_date,note\n"
        "X1,SPY,equity_ls,dv01,,1000,2025-01-02,\n"
    )
    with pytest.raises(ValueError, match="needs pct_yield"):
        load_positions(f, cfg.universe)
