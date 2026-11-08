"""Business-day calendars, publication-lag math and the no-partial-bar rule.

Four calendars:

* ``nyse``       – exchange_calendars XNYS sessions.
* ``us_bond``    – approximation of the SIFMA US bond-market calendar: weekdays minus US
                   federal holidays (pandas ``USFederalHolidayCalendar``) minus Good Friday.
                   Columbus Day and Veterans Day are bond-market holidays while NYSE trades,
                   so Treasury yields missing on those days are *not* late.
                   Known gaps: SIFMA early closes are treated as full sessions; one-off closures
                   (e.g. national days of mourning) are not modelled.
* ``ecb_target`` – TARGET2 calendar: weekdays except 1 Jan, Good Friday, Easter Monday,
                   1 May, 25 Dec, 26 Dec.
* ``fx_weekday`` – Monday to Friday.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass
from functools import cache
from typing import Literal
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    EasterMonday,
    GoodFriday,
    Holiday,
    USFederalHolidayCalendar,
)

from cleantape.clock import NY, ny, ny_today

CAL_START = dt.date(2015, 1, 1)
CAL_END = dt.date(2030, 12, 31)
BERLIN = ZoneInfo("Europe/Berlin")

CompletionRule = Literal["nyse_close", "next_ny_day", "ecb_publish"]

# ECB reference rates are set at 14:15 CET and published "around 16:00 CET".
ECB_PUBLISH_CET = dt.time(16, 30)


class _TargetCalendar(AbstractHolidayCalendar):
    rules = [  # noqa: RUF012 - pandas API expects a class attribute list
        Holiday("New Year", month=1, day=1),
        GoodFriday,
        EasterMonday,
        Holiday("Labour Day", month=5, day=1),
        Holiday("Christmas", month=12, day=25),
        Holiday("St Stephen", month=12, day=26),
    ]


def _weekdays(start: dt.date, end: dt.date) -> pd.DatetimeIndex:
    return pd.bdate_range(start, end)


def _build(name: str) -> list[dt.date]:
    if name == "nyse":
        cal = xcals.get_calendar("XNYS", start=CAL_START.isoformat(), end=CAL_END.isoformat())
        return [d.date() for d in cal.sessions]
    days = _weekdays(CAL_START, CAL_END)
    if name == "fx_weekday":
        holidays = pd.DatetimeIndex([])
    elif name == "us_bond":
        fed = USFederalHolidayCalendar().holidays(CAL_START, CAL_END)
        gf = pd.DatetimeIndex(GoodFriday.dates(CAL_START, CAL_END))
        holidays = fed.union(gf)
    elif name == "ecb_target":
        holidays = _TargetCalendar().holidays(CAL_START, CAL_END)
    else:
        raise ValueError(f"unknown calendar {name!r}")
    return [d.date() for d in days.difference(holidays)]


@dataclass(frozen=True)
class BusinessCalendar:
    """A sorted list of session dates with lookup helpers."""

    name: str
    sessions: tuple[dt.date, ...]

    def _check(self, d: dt.date) -> None:
        if not (CAL_START <= d <= CAL_END):
            raise ValueError(f"{d} outside supported calendar range {CAL_START}..{CAL_END}")

    def is_session(self, d: dt.date) -> bool:
        """True if ``d`` is a business day on this calendar."""
        self._check(d)
        i = bisect.bisect_left(self.sessions, d)
        return i < len(self.sessions) and self.sessions[i] == d

    def on_or_before(self, d: dt.date) -> dt.date:
        """Latest session <= ``d``."""
        self._check(d)
        i = bisect.bisect_right(self.sessions, d)
        return self.sessions[i - 1]

    def previous(self, d: dt.date) -> dt.date:
        """Latest session strictly before ``d``."""
        return self.on_or_before(d - dt.timedelta(days=1))

    def shift_back(self, d: dt.date, n: int) -> dt.date:
        """The session ``n`` sessions before session ``on_or_before(d)`` (n=0 → itself)."""
        if n < 0:
            raise ValueError("n must be >= 0")
        i = bisect.bisect_right(self.sessions, d) - 1
        return self.sessions[i - n]

    def between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """Sessions in the closed interval [start, end]."""
        lo = bisect.bisect_left(self.sessions, start)
        hi = bisect.bisect_right(self.sessions, end)
        return list(self.sessions[lo:hi])

    def sessions_after(self, start: dt.date, end: dt.date) -> int:
        """Number of sessions in the half-open interval (start, end]; 0 if end <= start."""
        if end <= start:
            return 0
        return len(self.between(start + dt.timedelta(days=1), end))


@cache
def get_calendar(name: str) -> BusinessCalendar:
    """Return (cached) calendar by name: nyse, us_bond, ecb_target, fx_weekday."""
    return BusinessCalendar(name, tuple(_build(name)))


# --------------------------------------------------------------- partial bars


def completion_rule(source: str, calendar: str, is_future: bool) -> CompletionRule:
    """Which rule decides that the daily value for date D is final.

    NYSE-listed instruments (equities, ETFs, VIX) close at 16:00 ET; FX and futures trade
    nearly 24h so their "daily" bar is only final once D is over in New York; ECB rates are
    final after publication on D. FRED series follow the close rule; their publication lag is
    handled separately by ``expected_lag_business_days``.
    """
    if source == "ecb":
        return "ecb_publish"
    if is_future or calendar == "fx_weekday":
        return "next_ny_day"
    return "nyse_close"


def bar_is_complete(
    day: dt.date, now_utc: dt.datetime, rule: CompletionRule, close_buffer: dt.time
) -> bool:
    """True if a daily observation dated ``day`` is final (not an in-progress bar) at ``now_utc``.

    * ``nyse_close``:  now >= ``close_buffer`` (16:30) New York time on ``day``.
    * ``next_ny_day``: ``day`` is strictly before today's New York date.
    * ``ecb_publish``: now >= 16:30 Frankfurt time on ``day``.
    """
    if rule == "nyse_close":
        return now_utc >= dt.datetime.combine(day, close_buffer, tzinfo=NY)
    if rule == "next_ny_day":
        return day < ny_today(now_utc)
    if rule == "ecb_publish":
        return now_utc >= dt.datetime.combine(day, ECB_PUBLISH_CET, tzinfo=BERLIN)
    raise ValueError(f"unknown completion rule {rule!r}")


def last_complete_session(
    cal: BusinessCalendar, now_utc: dt.datetime, rule: CompletionRule, close_buffer: dt.time
) -> dt.date:
    """Most recent session on ``cal`` whose bar is complete at ``now_utc``."""
    d = cal.on_or_before(ny(now_utc).date())
    while not bar_is_complete(d, now_utc, rule, close_buffer):
        d = cal.previous(d)
    return d


def expected_latest_date(
    cal: BusinessCalendar,
    now_utc: dt.datetime,
    lag_business_days: int,
    rule: CompletionRule,
    close_buffer: dt.time,
) -> dt.date:
    """The latest observation date we should have by ``now_utc``.

    = last complete session on the instrument's calendar, minus its publication lag
    (in sessions of the same calendar).
    """
    last = last_complete_session(cal, now_utc, rule, close_buffer)
    return cal.shift_back(last, lag_business_days)


def business_days_behind(cal: BusinessCalendar, latest: dt.date, expected: dt.date) -> int:
    """How many sessions ``latest`` trails ``expected`` (0 if up to date or ahead)."""
    return cal.sessions_after(latest, expected)
