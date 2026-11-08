from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import pytest

from cleantape.ingest.yahoo import frame_to_long
from cleantape.transform.normalize import (
    ParseError,
    ecb_cross,
    ecb_pairs,
    parse_ecb_csv,
    parse_fred_csv,
    parse_nasdaq_json,
    parse_yahoo_long,
)

from conftest import FIXTURES

D = dt.date


def _val(df: pd.DataFrame, day: dt.date, sym: str | None = None) -> float:
    rows = df[(df["date"] == day) & ((df["symbol"] == sym) if sym else True)]
    assert len(rows) == 1, rows
    return float(rows["value"].iloc[0])


# ------------------------------------------------------------------ FRED


def test_fred_current_format_blank_is_null() -> None:
    df = parse_fred_csv((FIXTURES / "fred_DGS10.csv").read_bytes())
    assert set(df["symbol"]) == {"DGS10"}
    assert _val(df, D(2025, 11, 10)) == 4.13
    assert pd.isna(_val(df, D(2025, 11, 11)))  # Veterans Day: bonds closed


def test_fred_legacy_header_and_dot_missing() -> None:
    df = parse_fred_csv((FIXTURES / "fred_DGS10_legacy_header_dots.csv").read_bytes())
    assert pd.isna(_val(df, D(2025, 11, 11)))
    assert _val(df, D(2025, 11, 10)) == 4.13
    assert df["value"].dtype == "float64"


def test_fred_multi_series_csv() -> None:
    body = b"observation_date,DGS10,DGS2\n2025-11-10,4.13,3.60\n2025-11-11,.,\n"
    df = parse_fred_csv(body)
    assert _val(df, D(2025, 11, 10), "DGS2") == 3.60
    assert df["value"].isna().sum() == 2


def test_fred_without_date_column_fails_loudly() -> None:
    with pytest.raises(ParseError, match="no date column"):
        parse_fred_csv(b"when,DGS10\n2025-11-10,4.13\n")


# ------------------------------------------------------------------ Yahoo


def _yahoo_multi() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "yahoo_multi.csv", header=[0, 1], index_col=0, parse_dates=True)


def test_yahoo_multiindex_field_first() -> None:
    long = frame_to_long(_yahoo_multi(), ["SPY", "EURUSD=X", "CL=F", "^VIX"])
    assert set(long["symbol"]) == {"SPY", "EURUSD=X", "CL=F", "^VIX"}
    assert list(long.columns) == ["date", "symbol", "close"]
    assert long["close"].notna().all()


def test_yahoo_multiindex_ticker_first_matches() -> None:
    multi = _yahoo_multi()
    swapped = multi.swaplevel(0, 1, axis=1).sort_index(axis=1)  # group_by="ticker" shape
    a = frame_to_long(multi, ["SPY", "EURUSD=X", "CL=F", "^VIX"])
    b = frame_to_long(swapped, ["SPY", "EURUSD=X", "CL=F", "^VIX"])
    pd.testing.assert_frame_equal(a, b)


def test_yahoo_flat_single_ticker() -> None:
    flat = pd.read_csv(FIXTURES / "yahoo_flat.csv", index_col=0, parse_dates=True)
    long = frame_to_long(flat, ["SPY"])
    multi = frame_to_long(_yahoo_multi(), ["SPY", "EURUSD=X", "CL=F", "^VIX"])
    spy_multi = multi[multi["symbol"] == "SPY"].reset_index(drop=True)
    pd.testing.assert_frame_equal(long, spy_multi)


def test_yahoo_failed_ticker_column_is_dropped() -> None:
    multi = _yahoo_multi()
    multi[("Close", "BOGUS")] = float("nan")
    long = frame_to_long(multi, ["SPY", "BOGUS"])
    assert "BOGUS" not in set(long["symbol"])


def test_yahoo_empty_frame() -> None:
    assert frame_to_long(pd.DataFrame(), ["SPY"]).empty


def test_yahoo_long_roundtrip() -> None:
    long = frame_to_long(_yahoo_multi(), ["SPY", "EURUSD=X", "CL=F", "^VIX"])
    buf = io.StringIO()
    long.to_csv(buf, index=False)
    parsed = parse_yahoo_long(buf.getvalue().encode())
    assert len(parsed) == len(long)
    assert parsed["date"].iloc[0] == D(2025, 10, 1)


# ------------------------------------------------------------------ Nasdaq


def test_nasdaq_parses_us_dates_and_money() -> None:
    df = parse_nasdaq_json((FIXTURES / "nasdaq_SPY.json").read_bytes(), "SPY")
    assert len(df) > 5 and df["value"].notna().all()
    assert isinstance(df["date"].iloc[0], dt.date)
    body = b'{"data":{"tradesTable":{"rows":[{"date":"11/06/2026","close":"$1,235.29"}]}}}'
    assert parse_nasdaq_json(body, "X")["value"].iloc[0] == 1235.29


# ------------------------------------------------------------------ ECB


def test_ecb_cross_conventions() -> None:
    per_eur = {
        "USD": 1.1561,
        "JPY": 176.99,
        "GBP": 0.8811,
        "CHF": 0.9312,
        "CAD": 1.6311,
        "AUD": 1.7838,
    }
    assert ecb_cross(per_eur, "EURUSD") == pytest.approx(1.1561)
    assert ecb_cross(per_eur, "EURJPY") == pytest.approx(176.99)
    assert ecb_cross(per_eur, "USDJPY") == pytest.approx(176.99 / 1.1561)  # ≈153.09
    assert ecb_cross(per_eur, "GBPUSD") == pytest.approx(1.1561 / 0.8811)  # ≈1.3121
    assert ecb_cross(per_eur, "AUDUSD") == pytest.approx(1.1561 / 1.7838)  # ≈0.6481
    assert ecb_cross(per_eur, "USDCAD") == pytest.approx(1.6311 / 1.1561)  # ≈1.4109
    assert ecb_cross(per_eur, "USDCHF") == pytest.approx(0.9312 / 1.1561)  # ≈0.8055
    with pytest.raises(KeyError, match="NOK"):
        ecb_cross(per_eur, "USDNOK")


def test_ecb_fixture_to_pairs() -> None:
    per_eur = parse_ecb_csv((FIXTURES / "ecb_exr.csv").read_bytes())
    pairs = ecb_pairs(per_eur, ["EURUSD", "USDJPY", "GBPUSD"])
    assert _val(pairs, D(2025, 11, 7), "EURUSD") == pytest.approx(1.1561)
    assert _val(pairs, D(2025, 11, 7), "USDJPY") == pytest.approx(176.99 / 1.1561)
    # Yahoo-convention USDJPY should be ~150, not ~0.0065 (an un-inverted quote)
    assert 100 < _val(pairs, D(2025, 11, 7), "USDJPY") < 200


def test_ecb_missing_columns_fails() -> None:
    with pytest.raises(ParseError, match="missing columns"):
        parse_ecb_csv(b"KEY,TIME_PERIOD\nx,2025-01-01\n")
