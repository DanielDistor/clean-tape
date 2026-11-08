from __future__ import annotations

import datetime as dt

import pytest

from cleantape.clock import FixedClock, SlotWindow, active_slot, decide_slot, fmt_ny, ny_today

WINDOWS = [
    SlotWindow("morning", dt.time(6, 30), dt.time(9, 30)),
    SlotWindow("evening", dt.time(18, 15), dt.time(22, 0)),
]


def utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.UTC)


# Cron entries (UTC). Morning: 11:37 (EST 06:37) and 10:37 (EDT 06:37).
# Evening: 23:37 (EST 18:37) and 22:37 (EDT 18:37).


@pytest.mark.parametrize(
    ("now", "slot"),
    [
        # Winter (EST, UTC-5)
        (utc(2025, 1, 15, 11, 37), "morning"),  # 06:37 EST
        (utc(2025, 1, 15, 10, 37), None),  # 05:37 EST: EDT cron fires too early in winter
        (utc(2025, 1, 15, 23, 37), "evening"),  # 18:37 EST
        (utc(2025, 1, 15, 22, 37), None),  # 17:37 EST
        # Summer (EDT, UTC-4)
        (utc(2025, 7, 15, 10, 37), "morning"),  # 06:37 EDT
        (utc(2025, 7, 15, 11, 37), "morning"),  # 07:37 EDT: both crons land in window
        (utc(2025, 7, 15, 22, 37), "evening"),  # 18:37 EDT
        (utc(2025, 7, 15, 23, 37), "evening"),  # 19:37 EDT
        # Delayed run still inside window
        (utc(2025, 1, 16, 2, 30), "evening"),  # 21:30 EST
        (utc(2025, 1, 16, 3, 5), None),  # 22:05 EST: too late
    ],
)
def test_active_slot_across_dst(now: dt.datetime, slot: str | None) -> None:
    assert active_slot(now, WINDOWS) == slot


def test_dst_switch_day_spring_forward() -> None:
    # 2025-03-09: clocks jump 02:00 -> 03:00 EST->EDT. 10:37 UTC = 06:37 EDT.
    assert active_slot(utc(2025, 3, 9, 10, 37), WINDOWS) == "morning"
    # The day before (still EST) the same UTC time is 05:37 -> outside.
    assert active_slot(utc(2025, 3, 8, 10, 37), WINDOWS) is None


def test_dst_switch_day_fall_back() -> None:
    # 2025-11-02: EDT->EST. 11:37 UTC = 06:37 EST.
    assert active_slot(utc(2025, 11, 2, 11, 37), WINDOWS) == "morning"
    assert active_slot(utc(2025, 11, 1, 11, 37), WINDOWS) == "morning"  # 07:37 EDT


def test_evening_run_after_utc_midnight_belongs_to_ny_day() -> None:
    now = utc(2025, 1, 16, 1, 0)  # 20:00 EST on Jan 15
    assert ny_today(now) == dt.date(2025, 1, 15)
    d = decide_slot(now, WINDOWS, "auto", succeeded=set())
    assert d.run and d.slot == "evening" and d.ny_date == dt.date(2025, 1, 15)


def test_already_ran_today_skips_second_cron() -> None:
    first = decide_slot(utc(2025, 7, 15, 10, 37), WINDOWS, "auto", succeeded=set())
    assert first.run
    second = decide_slot(
        utc(2025, 7, 15, 11, 37), WINDOWS, "auto", succeeded={(first.ny_date, "morning")}
    )
    assert not second.run and "already ran" in second.reason


def test_outside_window_does_nothing() -> None:
    d = decide_slot(utc(2025, 7, 15, 16, 0), WINDOWS, "auto", succeeded=set())
    assert not d.run and d.slot is None and "outside window" in d.reason


def test_explicit_slot_outside_its_window_skips() -> None:
    d = decide_slot(utc(2025, 7, 15, 10, 37), WINDOWS, "evening", succeeded=set())
    assert not d.run


def test_force_bypasses_guard() -> None:
    now = utc(2025, 7, 15, 11, 37)
    d = decide_slot(now, WINDOWS, "auto", succeeded={(dt.date(2025, 7, 15), "morning")}, force=True)
    assert d.run and d.slot == "morning"
    d2 = decide_slot(utc(2025, 7, 15, 17, 0), WINDOWS, "auto", succeeded=set(), force=True)
    assert d2.run and d2.slot == "evening"  # 13:00 EDT, outside windows -> evening
    d3 = decide_slot(utc(2025, 7, 15, 17, 0), WINDOWS, "morning", succeeded=set(), force=True)
    assert d3.slot == "morning"


def test_unknown_slot_rejected() -> None:
    with pytest.raises(ValueError, match="unknown slot"):
        decide_slot(utc(2025, 7, 15, 11, 0), WINDOWS, "lunch", succeeded=set())


def test_fixed_clock_requires_aware() -> None:
    with pytest.raises(ValueError):
        FixedClock(dt.datetime(2025, 1, 1))
    assert FixedClock(utc(2025, 1, 1, 0)).now_utc() == utc(2025, 1, 1, 0)


def test_fmt_ny() -> None:
    assert fmt_ny(utc(2025, 1, 15, 12, 5)) == "07:05 ET"
    assert fmt_ny(utc(2025, 7, 15, 12, 5), with_date=True) == "2025-07-15 08:05 ET"
