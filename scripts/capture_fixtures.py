"""One-off: download small real samples from each source into tests/fixtures/.

Run with `make fixtures`. Tests never touch the network; they read these files.
The window (Oct–Nov 2025) deliberately spans Columbus Day and Veterans Day.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import requests
import yfinance as yf

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
START, END = dt.date(2025, 10, 1), dt.date(2025, 11, 21)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) clean-tape/0.1"


def get(url: str, **params: str) -> bytes:
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.content


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for sid in ["DGS10", "DGS2", "DCOILWTICO"]:
        body = get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv",
            id=sid,
            cosd=START.isoformat(),
            coed=END.isoformat(),
        )
        (OUT / f"fred_{sid}.csv").write_bytes(body)
    body = get(
        "https://data-api.ecb.europa.eu/service/data/EXR/D.AUD+CAD+CHF+GBP+JPY+USD.EUR.SP00.A",
        startPeriod=START.isoformat(),
        endPeriod=END.isoformat(),
        format="csvdata",
        detail="dataonly",
    )
    (OUT / "ecb_exr.csv").write_bytes(body)
    # Nasdaq returns zero rows unless `todate` is recent (see BUILD_LOG), so sample the last
    # few weeks instead of the fixed window.
    today = dt.datetime.now(dt.UTC).date()
    body = get(
        "https://api.nasdaq.com/api/quote/SPY/historical",
        assetclass="etf",
        fromdate=(today - dt.timedelta(days=21)).isoformat(),
        todate=today.isoformat(),
        limit="9999",
    )
    (OUT / "nasdaq_SPY.json").write_bytes(body)
    end_excl = (END + dt.timedelta(days=1)).isoformat()
    multi = yf.download(
        ["SPY", "EURUSD=X", "CL=F", "^VIX"],
        start=START.isoformat(),
        end=end_excl,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    multi.to_csv(OUT / "yahoo_multi.csv")
    flat = yf.download(
        "SPY",
        start=START.isoformat(),
        end=end_excl,
        auto_adjust=True,
        progress=False,
        multi_level_index=False,
    )
    flat.to_csv(OUT / "yahoo_flat.csv")
    for p in sorted(OUT.iterdir()):
        print(f"{p.name:24s} {p.stat().st_size:>8d} bytes")


if __name__ == "__main__":
    main()
