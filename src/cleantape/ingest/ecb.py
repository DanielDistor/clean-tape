"""ECB Data Portal SDMX REST API: euro foreign-exchange reference rates (EXR dataset).

Endpoint verified: ``https://data-api.ecb.europa.eu/service/data/EXR/D.{CCY+CCY}.EUR.SP00.A``
with ``format=csvdata``. Quotes are *foreign currency per 1 EUR*; conversion to Yahoo pair
conventions happens in ``transform.normalize.ecb_cross``.
"""

from __future__ import annotations

import datetime as dt

import requests

from cleantape.clock import Clock
from cleantape.config import Ingest, Instrument
from cleantape.ingest.base import RawDrop, http_get, timed_drop
from cleantape.ingest.fred import count_csv_rows

BASE_URL = "https://data-api.ecb.europa.eu/service/data/EXR"


def currencies_needed(pairs: list[str]) -> list[str]:
    """Non-EUR currency codes needed to build the given 6-letter pairs."""
    ccys = {p[:3] for p in pairs} | {p[3:] for p in pairs}
    ccys.discard("EUR")
    return sorted(ccys)


class EcbSource:
    name = "ecb"

    def __init__(self, opts: Ingest) -> None:
        self.opts = opts
        self.session = requests.Session()

    def fetch(
        self, instruments: list[Instrument], start: dt.date, end: dt.date, clock: Clock
    ) -> list[RawDrop]:
        ccys = currencies_needed([i.source_symbol for i in instruments])
        if not ccys:
            return []
        url = f"{BASE_URL}/D.{'+'.join(ccys)}.EUR.SP00.A"
        params = {
            "startPeriod": start.isoformat(),
            "endPeriod": end.isoformat(),
            "format": "csvdata",
            "detail": "dataonly",
        }

        def get() -> tuple[int, bytes]:
            return http_get(
                url,
                params=params,
                timeout_s=self.opts.http_timeout_s,
                retries=self.opts.retries,
                base_s=self.opts.backoff_base_s,
                user_agent=self.opts.user_agent,
                session=self.session,
            )

        return [timed_drop(self.name, "EXR", "csv", clock, get, count_csv_rows)]
