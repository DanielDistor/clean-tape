"""Time handling: injectable "now", New York / UTC conversion, and slot windows.

Rules (see docs/ARCHITECTURE.md):
* every timestamp the pipeline stores is timezone-aware UTC;
* market dates are plain ``datetime.date``;
* anything shown to people is converted to America/New_York.

Nothing outside this module calls ``datetime.now()``. Code receives a ``Clock`` so
timing tests can pin "now" to any instant (including across DST switches).
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = dt.UTC


class Clock(Protocol):
    def now_utc(self) -> dt.datetime: ...


@dataclass(frozen=True)
class SystemClock:
    """The real wall clock."""

    def now_utc(self) -> dt.datetime:
        return dt.datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    """A clock frozen at one instant (tests, reruns of a past day)."""

    at: dt.datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")

    def now_utc(self) -> dt.datetime:
        return self.at.astimezone(UTC)


def get_clock() -> Clock:
    """System clock, unless CLEANTAPE_NOW (ISO-8601 with offset) pins the time."""
    pinned = os.environ.get("CLEANTAPE_NOW")
    if pinned:
        return FixedClock(dt.datetime.fromisoformat(pinned))
    return SystemClock()


def ny(ts: dt.datetime) -> dt.datetime:
    """Convert an aware datetime to New York time."""
    if ts.tzinfo is None:
        raise ValueError("naive datetime; all timestamps must be timezone-aware")
    return ts.astimezone(NY)


def ny_today(now_utc: dt.datetime) -> dt.date:
    """The calendar date in New York at ``now_utc``."""
    return ny(now_utc).date()


def ny_datetime(day: dt.date, at: dt.time) -> dt.datetime:
    """An aware datetime for wall-clock time ``at`` on ``day`` in New York."""
    return dt.datetime.combine(day, at, tzinfo=NY)


def fmt_ny(ts: dt.datetime, with_date: bool = False) -> str:
    """Human display of a timestamp in ET, e.g. '07:42 ET' or '2026-11-06 07:42 ET'."""
    local = ny(ts)
    return local.strftime("%Y-%m-%d %H:%M ET" if with_date else "%H:%M ET")


# ----------------------------------------------------------------- slot windows


@dataclass(frozen=True)
class SlotWindow:
    name: str
    start: dt.time
    end: dt.time


@dataclass(frozen=True)
class SlotDecision:
    run: bool
    slot: str | None
    ny_date: dt.date
    reason: str


def active_slot(now_utc: dt.datetime, windows: list[SlotWindow]) -> str | None:
    """Name of the slot whose New York window contains ``now_utc`` (start inclusive)."""
    t = ny(now_utc).time()
    for w in windows:
        if w.start <= t < w.end:
            return w.name
    return None


def decide_slot(
    now_utc: dt.datetime,
    windows: list[SlotWindow],
    requested: str,
    succeeded: set[tuple[dt.date, str]],
    force: bool = False,
) -> SlotDecision:
    """Decide whether this invocation should do work.

    ``requested`` is ``auto`` or an explicit slot name. GitHub cron fires twice per slot
    (one entry for EST, one for EDT) and can be late, so ``auto`` runs only when inside
    the window and only if that slot has not already succeeded for today's NY date.
    ``force`` bypasses both conditions (manual workflow_dispatch / laptop runs).
    """
    today = ny_today(now_utc)
    names = {w.name for w in windows}
    if requested != "auto" and requested not in names:
        raise ValueError(f"unknown slot {requested!r}; expected auto or one of {sorted(names)}")
    in_window = active_slot(now_utc, windows)
    slot = in_window if requested == "auto" else requested
    if force:
        return SlotDecision(True, slot or "evening", today, "forced")
    if slot is None:
        return SlotDecision(False, None, today, f"outside window ({fmt_ny(now_utc)})")
    if requested != "auto" and in_window != requested:
        return SlotDecision(False, slot, today, f"outside {slot} window ({fmt_ny(now_utc)})")
    if (today, slot) in succeeded:
        return SlotDecision(False, slot, today, f"{slot} already ran for {today}")
    return SlotDecision(True, slot, today, f"inside {slot} window")
