"""Ingest orchestration: fetch every source, land drops, normalise, upsert observations."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from cleantape.clock import Clock, ny_today
from cleantape.config import Config
from cleantape.ingest.base import RawDrop, Source
from cleantape.ingest.ecb import EcbSource
from cleantape.ingest.fred import FredSource
from cleantape.ingest.landing import land, record
from cleantape.ingest.nasdaq import NasdaqSource
from cleantape.ingest.yahoo import YahooSource
from cleantape.logging_setup import get_logger
from cleantape.store.state import State
from cleantape.transform.normalize import to_observations, upsert

log = get_logger("ingest.run")


@dataclass
class IngestSummary:
    drops: int = 0
    failed_sources: list[str] = field(default_factory=list)
    inserted: int = 0
    revised: int = 0
    unchanged_drops: int = 0
    parse_errors: list[str] = field(default_factory=list)
    fallback_used: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_sources and not self.parse_errors


def default_sources(cfg: Config) -> dict[str, Source]:
    opts = cfg.settings.ingest
    return {
        "yahoo": YahooSource(opts),
        "nasdaq": NasdaqSource(opts),
        "fred": FredSource(opts),
        "ecb": EcbSource(opts),
    }


def lookback_start(today: dt.date, business_days: int) -> dt.date:
    """Start date covering ``business_days`` weekdays before ``today`` (plus holiday slack)."""
    return today - dt.timedelta(days=int(business_days * 7 / 5) + 4)


def fetch_all(
    cfg: Config, sources: dict[str, Source], start: dt.date, end: dt.date, clock: Clock
) -> tuple[list[RawDrop], list[str]]:
    """Fetch all sources. A failing source never stops the others. Returns (drops, fallbacks)."""
    u = cfg.universe
    drops: list[RawDrop] = []
    yahoo = sources["yahoo"].fetch(u.by_source("yahoo"), start, end, clock)
    drops += yahoo
    missing = set()
    for d in yahoo:
        missing |= {s for s in d.meta.get("missing", "").split(",") if s}
    need_fb = [i for i in u.by_source("yahoo") if i.source_symbol in missing and i.fallback]
    if need_fb:
        log.warning(
            "yahoo missing %d symbols with a fallback; using nasdaq for %s",
            len(need_fb),
            [i.id for i in need_fb],
        )
        drops += sources["nasdaq"].fetch(need_fb, start, end, clock)
    no_fb = sorted(
        i.id for i in u.by_source("yahoo") if i.source_symbol in missing and not i.fallback
    )
    if no_fb:
        log.warning("yahoo missing symbols with no fallback (freshness check will flag): %s", no_fb)
    for name in ("fred", "ecb"):
        drops += sources[name].fetch(u.by_source(name), start, end, clock)
    return drops, [i.id for i in need_fb]


def run_ingest(
    cfg: Config,
    state: State,
    clock: Clock,
    run_id: str,
    start: dt.date,
    end: dt.date | None = None,
    sources: dict[str, Source] | None = None,
) -> IngestSummary:
    """Fetch → land → normalise → upsert. Writes observations and manifest."""
    sources = sources or default_sources(cfg)
    today = ny_today(clock.now_utc())
    end = end or today
    log.info("ingest window %s..%s", start, end)
    drops, fallbacks = fetch_all(cfg, sources, start, end, clock)
    landed = land(state, drops, run_id, today)
    obs_new, parse_errors = to_observations(landed, cfg.universe, cfg.settings.nyse_close_buffer)
    record(state, landed, run_id, today)
    table, inserted, revised = upsert(state.read("observations"), obs_new)
    state.write("observations", table)
    state.prune_raw(today, cfg.settings.ingest.raw_retention_days)
    summary = IngestSummary(
        drops=len(drops),
        failed_sources=sorted({d.source for d in drops if not d.ok}),
        inserted=inserted,
        revised=revised,
        unchanged_drops=sum(1 for ld in landed if ld.status == "unchanged"),
        parse_errors=parse_errors,
        fallback_used=fallbacks,
    )
    log.info("ingest done: %s", summary)
    return summary
