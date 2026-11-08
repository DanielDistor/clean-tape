"""FRED: keyless fredgraph CSV (one series per request), or the keyed API if FRED_API_KEY is set.

Verified behaviour (see docs/BUILD_LOG.md): the date column is ``observation_date``
(historically ``DATE``); missing values are ``.`` or empty; a multi-series request
ignores the ``cosd`` start-date filter, so we request one series at a time.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import requests

from cleantape.clock import Clock
from cleantape.config import Ingest, Instrument
from cleantape.ingest.base import RawDrop, http_get, timed_drop

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API_URL = "https://api.stlouisfed.org/fred/series/observations"


def count_csv_rows(body: bytes) -> int:
    """Data rows in a CSV body (lines minus header)."""
    lines = [ln for ln in body.decode("utf-8", "replace").splitlines() if ln.strip()]
    return max(len(lines) - 1, 0)


def count_api_rows(body: bytes) -> int:
    return len(json.loads(body).get("observations", []))


class FredSource:
    name = "fred"

    def __init__(self, opts: Ingest) -> None:
        self.opts = opts
        self.session = requests.Session()
        self.api_key = os.environ.get("FRED_API_KEY") or None

    def _get(self, url: str, params: dict[str, str]) -> tuple[int, bytes]:
        return http_get(
            url,
            params=params,
            timeout_s=self.opts.http_timeout_s,
            retries=self.opts.retries,
            base_s=self.opts.backoff_base_s,
            user_agent=self.opts.user_agent,
            session=self.session,
        )

    def fetch(
        self, instruments: list[Instrument], start: dt.date, end: dt.date, clock: Clock
    ) -> list[RawDrop]:
        drops = []
        for inst in instruments:
            sym = inst.source_symbol
            if self.api_key:
                params = {
                    "series_id": sym,
                    "api_key": self.api_key,
                    "file_type": "json",
                    "observation_start": start.isoformat(),
                    "observation_end": end.isoformat(),
                }
                drops.append(
                    timed_drop(
                        self.name,
                        sym,
                        "json",
                        clock,
                        lambda p=params: self._get(API_URL, p),
                        count_api_rows,
                    )
                )
            else:
                params = {"id": sym, "cosd": start.isoformat(), "coed": end.isoformat()}
                drops.append(
                    timed_drop(
                        self.name,
                        sym,
                        "csv",
                        clock,
                        lambda p=params: self._get(CSV_URL, p),
                        count_csv_rows,
                    )
                )
        return drops
