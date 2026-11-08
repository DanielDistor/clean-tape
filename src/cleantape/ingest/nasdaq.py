"""Nasdaq public quote-history JSON: keyless fallback for US-listed equities/ETFs.

Replaces the Stooq CSV download suggested in the brief (Stooq now serves a JavaScript
challenge page). Caveat: closes are NOT dividend-adjusted, unlike Yahoo with
``auto_adjust=True``. Rows are stored with ``source=nasdaq`` so they're always distinguishable.
"""

from __future__ import annotations

import datetime as dt
import json

import requests

from cleantape.clock import Clock
from cleantape.config import Ingest, Instrument
from cleantape.ingest.base import FetchError, RawDrop, http_get, timed_drop

URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"


def count_rows(body: bytes) -> int:
    data = json.loads(body)
    if not data.get("data"):
        msg = (data.get("status") or {}).get("bCodeMessage")
        raise FetchError(f"nasdaq returned no data: {msg}")
    rows = ((data["data"].get("tradesTable") or {}).get("rows")) or []
    if not rows:  # Nasdaq answers 200 with null rows for windows ending in the past
        raise FetchError("nasdaq returned 0 rows")
    return len(rows)


class NasdaqSource:
    name = "nasdaq"

    def __init__(self, opts: Ingest) -> None:
        self.opts = opts
        self.session = requests.Session()

    def fetch(
        self, instruments: list[Instrument], start: dt.date, end: dt.date, clock: Clock
    ) -> list[RawDrop]:
        drops = []
        for inst in instruments:
            fb = inst.fallback
            if fb is None or fb.source != "nasdaq":
                continue
            params = {
                "assetclass": fb.asset_type,
                "fromdate": start.isoformat(),
                "todate": end.isoformat(),
                "limit": "9999",
            }
            url = URL.format(symbol=fb.source_symbol)

            def get(u: str = url, p: dict[str, str] = params) -> tuple[int, bytes]:
                return http_get(
                    u,
                    params=p,
                    timeout_s=self.opts.http_timeout_s,
                    retries=self.opts.retries,
                    base_s=self.opts.backoff_base_s,
                    user_agent=self.opts.user_agent,
                    session=self.session,
                )

            drops.append(timed_drop(self.name, fb.source_symbol, "json", clock, get, count_rows))
        return drops
