"""Source protocol, the RawDrop record, and a retrying HTTP helper."""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import requests

from cleantape.clock import Clock
from cleantape.config import Instrument
from cleantape.logging_setup import get_logger

log = get_logger("ingest")


@dataclass
class RawDrop:
    """One untouched response from a source: a "vendor file drop"."""

    source: str
    dataset: str
    ext: str  # csv | json
    content: bytes
    received_at_utc: dt.datetime
    http_status: int | None
    duration_ms: float
    ok: bool
    error: str | None = None
    rows: int | None = None
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class Source(Protocol):
    name: str

    def fetch(
        self, instruments: list[Instrument], start: dt.date, end: dt.date, clock: Clock
    ) -> list[RawDrop]: ...


class FetchError(RuntimeError):
    """A source request failed after all retries."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def backoff_delays(retries: int, base_s: float, rng: random.Random | None = None) -> list[float]:
    """Exponential backoff with full jitter: delay_k ~ U(0.5, 1.0) * base * 2**k."""
    rng = rng or random.Random()
    return [base_s * (2**k) * rng.uniform(0.5, 1.0) for k in range(retries)]


def with_retries[T](
    fn: Callable[[], T],
    *,
    what: str,
    retries: int,
    base_s: float,
    sleep: Callable[[float], None] = time.sleep,
    retry_if: Callable[[Exception], bool] = lambda e: True,
) -> T:
    """Call ``fn``; on exception retry with jittered exponential backoff, then re-raise."""
    delays = backoff_delays(retries, base_s)
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= retries or not retry_if(e):
                raise
            wait = delays[attempt]
            log.warning(
                "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                what,
                attempt + 1,
                retries + 1,
                e,
                wait,
            )
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def _retryable_http(e: Exception) -> bool:
    if isinstance(e, FetchError):
        return e.status is None or e.status == 429 or e.status >= 500
    return isinstance(e, requests.RequestException)


def http_get(
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout_s: float,
    retries: int,
    base_s: float,
    user_agent: str,
    session: requests.Session | None = None,
) -> tuple[int, bytes]:
    """GET with timeout and retries on network errors, 429 and 5xx. Returns (status, body)."""
    sess = session or requests.Session()

    def once() -> tuple[int, bytes]:
        r = sess.get(
            url,
            params=params,
            timeout=timeout_s,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
        )
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code} from {r.url[:120]}", status=r.status_code)
        return r.status_code, r.content

    return with_retries(
        once, what=f"GET {url}", retries=retries, base_s=base_s, retry_if=_retryable_http
    )


def timed_drop(
    source: str,
    dataset: str,
    ext: str,
    clock: Clock,
    fetch: Callable[[], tuple[int, bytes]],
    count_rows: Callable[[bytes], int],
) -> RawDrop:
    """Run ``fetch`` and wrap the outcome (success or failure) in a RawDrop. Never raises."""
    t0 = time.perf_counter()
    try:
        status, body = fetch()
        rows = count_rows(body)
        return RawDrop(
            source,
            dataset,
            ext,
            body,
            clock.now_utc(),
            status,
            (time.perf_counter() - t0) * 1000,
            ok=True,
            rows=rows,
        )
    except Exception as e:
        status = e.status if isinstance(e, FetchError) else None
        log.error("source=%s dataset=%s failed: %s", source, dataset, e)
        return RawDrop(
            source,
            dataset,
            ext,
            b"",
            clock.now_utc(),
            status,
            (time.perf_counter() - t0) * 1000,
            ok=False,
            error=str(e)[:500],
        )
