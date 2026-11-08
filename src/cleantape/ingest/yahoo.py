"""Yahoo Finance via yfinance: equities, ETFs, VIX, FX and front-month futures.

* One batched ``yf.download`` call for all symbols (never one call per ticker).
* ``auto_adjust=True``: equity/ETF closes are split- and dividend-adjusted, so daily
  changes are total-return-like. FX and futures are unaffected.
* yfinance returns a DataFrame, not a file; the raw drop is that frame's Close panel
  serialised as long CSV (date,symbol,close) — as close to "untouched" as the API allows.
* Column shapes differ by version/call: MultiIndex (field, ticker), MultiIndex
  (ticker, field) with ``group_by='ticker'``, or flat columns for a single ticker.
  ``frame_to_long`` handles all three.
"""

from __future__ import annotations

import datetime as dt
import io
import time

import pandas as pd

from cleantape.clock import Clock
from cleantape.config import Ingest, Instrument
from cleantape.ingest.base import FetchError, RawDrop, with_retries
from cleantape.ingest.fred import count_csv_rows
from cleantape.logging_setup import get_logger

log = get_logger("ingest.yahoo")
FIELD = "Close"


def frame_to_long(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """Convert any yfinance download shape to long format with columns date, symbol, close.

    Rows with a missing close are dropped (a missing value is simply absent).
    """
    if df is None or df.empty:
        return pd.DataFrame({"date": [], "symbol": [], "close": []})
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = set(df.columns.get_level_values(0))
        if FIELD in lvl0:
            wide = df[FIELD]
        elif FIELD in set(df.columns.get_level_values(1)):
            wide = df.xs(FIELD, axis=1, level=1)
        else:
            raise ValueError(f"no {FIELD!r} level in yfinance columns: {list(df.columns)[:5]}")
    else:
        if FIELD not in df.columns:
            raise ValueError(f"no {FIELD!r} column in yfinance frame: {list(df.columns)}")
        if len(symbols) != 1:
            raise ValueError("flat yfinance frame but more than one symbol requested")
        wide = df[[FIELD]].rename(columns={FIELD: symbols[0]})
    wide = wide.copy()
    wide.index = pd.to_datetime(wide.index).tz_localize(None).normalize()
    wide.index.name = "date"
    wide.columns = [str(c) for c in wide.columns]
    long = wide.reset_index().melt(id_vars="date", var_name="symbol", value_name="close")
    long = long.dropna(subset=["close"])
    long["date"] = long["date"].dt.date.astype(str)
    return long.sort_values(["symbol", "date"]).reset_index(drop=True)[["date", "symbol", "close"]]


def _download(symbols: list[str], start: dt.date, end: dt.date) -> pd.DataFrame:
    import yfinance as yf

    return yf.download(
        symbols,
        start=start.isoformat(),
        end=(end + dt.timedelta(days=1)).isoformat(),  # yfinance end is exclusive
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="column",
    )


class YahooSource:
    name = "yahoo"

    def __init__(self, opts: Ingest, downloader=_download) -> None:
        self.opts = opts
        self.downloader = downloader

    def _batch(self, symbols: list[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        def once() -> pd.DataFrame:
            long = frame_to_long(self.downloader(symbols, start, end), symbols)
            if long.empty:
                raise FetchError("yfinance returned no data (rate-limited or blocked?)")
            return long

        return with_retries(
            once,
            what=f"yfinance batch of {len(symbols)}",
            retries=self.opts.retries,
            base_s=self.opts.backoff_base_s,
        )

    def fetch(
        self, instruments: list[Instrument], start: dt.date, end: dt.date, clock: Clock
    ) -> list[RawDrop]:
        symbols = sorted({i.source_symbol for i in instruments})
        if not symbols:
            return []
        t0 = time.perf_counter()
        try:
            long = self._batch(symbols, start, end)
            missing = sorted(set(symbols) - set(long["symbol"]))
            if missing:  # one batched retry for symbols that came back empty
                log.warning("yahoo: no data for %s; retrying those once", missing)
                try:
                    extra = self._batch(missing, start, end)
                    long = pd.concat([long, extra], ignore_index=True)
                except Exception as e:
                    log.warning("yahoo retry for missing symbols failed: %s", e)
                missing = sorted(set(symbols) - set(long["symbol"]))
            buf = io.StringIO()
            long.to_csv(buf, index=False)
            body = buf.getvalue().encode()
            drop = RawDrop(
                self.name,
                "daily_close",
                "csv",
                body,
                clock.now_utc(),
                200,
                (time.perf_counter() - t0) * 1000,
                ok=True,
                rows=count_csv_rows(body),
                meta={"missing": ",".join(missing)},
            )
            if missing:
                drop.error = f"no data for: {', '.join(missing)}"
            return [drop]
        except Exception as e:
            log.error("source=yahoo failed: %s", e)
            return [
                RawDrop(
                    self.name,
                    "daily_close",
                    "csv",
                    b"",
                    clock.now_utc(),
                    None,
                    (time.perf_counter() - t0) * 1000,
                    ok=False,
                    error=str(e)[:500],
                    meta={"missing": ",".join(symbols)},
                )
            ]
