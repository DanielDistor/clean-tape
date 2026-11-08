from __future__ import annotations

import datetime as dt

import pytest

from cleantape.calendars import (
    bar_is_complete,
    business_days_behind,
    completion_rule,
    expected_latest_date,
    get_calendar,
    last_complete_session,
)
from cleantape.clock import NY

D = dt.date
BUF = dt.time(16, 30)


def et(y: int, m: int, d: int, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(dt.UTC)


# ------------------------------------------------------------------ calendars


@pytest.mark.parametrize(
    "day",
    [D(2024, 10, 14), D(2025, 10, 13), D(2024, 11, 11), D(2025, 11, 11)],
    ids=["columbus24", "columbus25", "veterans24", "veterans25"],
)
def test_columbus_and_veterans_day_nyse_open_bonds_closed(day: dt.date) -> None:
    assert get_calendar("nyse").is_session(day)
    assert not get_calendar("us_bond").is_session(day)


@pytest.mark.parametrize("day", [D(2024, 3, 29), D(2025, 4, 18), D(2026, 4, 3)])
def test_good_friday_closed_everywhere_except_fx(day: dt.date) -> None:
    assert not get_calendar("nyse").is_session(day)
    assert not get_calendar("us_bond").is_session(day)
    assert not get_calendar("ecb_target").is_session(day)
    assert get_calendar("fx_weekday").is_session(day)


def test_ecb_target_holidays_2025() -> None:
    ecb = get_calendar("ecb_target")
    for day in [
        D(2025, 1, 1),
        D(2025, 4, 18),
        D(2025, 4, 21),
        D(2025, 5, 1),
        D(2025, 12, 25),
        D(2025, 12, 26),
    ]:
        assert not ecb.is_session(day), day
    # US holidays are TARGET business days
    for day in [D(2025, 7, 4), D(2025, 11, 27), D(2025, 1, 20)]:
        assert ecb.is_session(day), day
    # Easter Monday: ECB closed, NYSE open
    assert get_calendar("nyse").is_session(D(2025, 4, 21))


def test_juneteenth_and_thanksgiving() -> None:
    nyse, bond = get_calendar("nyse"), get_calendar("us_bond")
    for day in [D(2024, 6, 19), D(2025, 11, 27)]:
        assert not nyse.is_session(day) and not bond.is_session(day)


def test_weekends_never_sessions() -> None:
    for name in ["nyse", "us_bond", "ecb_target", "fx_weekday"]:
        cal = get_calendar(name)
        assert not cal.is_session(D(2025, 11, 8)) and not cal.is_session(D(2025, 11, 9))


def test_navigation_helpers() -> None:
    nyse = get_calendar("nyse")
    assert nyse.previous(D(2025, 11, 10)) == D(2025, 11, 7)  # Monday -> Friday
    assert nyse.on_or_before(D(2025, 11, 9)) == D(2025, 11, 7)
    assert nyse.shift_back(D(2025, 11, 10), 2) == D(2025, 11, 6)
    assert nyse.sessions_after(D(2025, 11, 6), D(2025, 11, 10)) == 2
    assert nyse.sessions_after(D(2025, 11, 10), D(2025, 11, 6)) == 0


def test_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="outside supported"):
        get_calendar("nyse").is_session(D(2040, 1, 2))


# ------------------------------------------------------------------ partial bars


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (et(2025, 11, 7, 9, 45), False),  # market open
        (et(2025, 11, 7, 16, 5), False),  # just after close, before buffer
        (et(2025, 11, 7, 16, 29), False),
        (et(2025, 11, 7, 16, 30), True),
        (et(2025, 11, 7, 23, 0), True),
        (et(2025, 11, 8, 7, 0), True),
    ],
)
def test_nyse_partial_bar(now: dt.datetime, expected: bool) -> None:
    assert bar_is_complete(D(2025, 11, 7), now, "nyse_close", BUF) is expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (et(2025, 11, 7, 18, 0), False),  # same NY day: FX bar still forming
        (et(2025, 11, 7, 23, 59), False),
        (et(2025, 11, 8, 0, 1), True),  # NY date has rolled
    ],
)
def test_fx_partial_bar(now: dt.datetime, expected: bool) -> None:
    assert bar_is_complete(D(2025, 11, 7), now, "next_ny_day", BUF) is expected


def test_fx_rule_uses_ny_date_not_utc_date() -> None:
    # 01:00 UTC on Nov 8 is still Nov 7 in New York -> Nov 7 FX bar not final.
    now = dt.datetime(2025, 11, 8, 1, 0, tzinfo=dt.UTC)
    assert bar_is_complete(D(2025, 11, 7), now, "next_ny_day", BUF) is False


def test_ecb_publish_rule_handles_cet_offset() -> None:
    # 16:30 Berlin (CET, UTC+1 in November) = 10:30 ET
    assert not bar_is_complete(D(2025, 11, 7), et(2025, 11, 7, 10, 29), "ecb_publish", BUF)
    assert bar_is_complete(D(2025, 11, 7), et(2025, 11, 7, 10, 30), "ecb_publish", BUF)


def test_completion_rule_mapping() -> None:
    assert completion_rule("yahoo", "nyse", is_future=False) == "nyse_close"
    assert completion_rule("yahoo", "nyse", is_future=True) == "next_ny_day"
    assert completion_rule("yahoo", "fx_weekday", is_future=False) == "next_ny_day"
    assert completion_rule("ecb", "ecb_target", is_future=False) == "ecb_publish"
    assert completion_rule("fred", "us_bond", is_future=False) == "nyse_close"


# ------------------------------------------------------------------ expected latest date


def test_last_complete_session_weekend() -> None:
    nyse = get_calendar("nyse")
    # Saturday morning: Friday is the last complete session
    assert last_complete_session(nyse, et(2025, 11, 8, 8), "nyse_close", BUF) == D(2025, 11, 7)
    # Monday 07:00: still Friday
    assert last_complete_session(nyse, et(2025, 11, 10, 7), "nyse_close", BUF) == D(2025, 11, 7)
    # Monday 19:00: Monday
    assert last_complete_session(nyse, et(2025, 11, 10, 19), "nyse_close", BUF) == D(2025, 11, 10)


def test_treasury_lag_one_day_monday_morning() -> None:
    bond = get_calendar("us_bond")
    # Monday morning: last complete = Fri; lag 1 -> Thu
    assert expected_latest_date(bond, et(2025, 11, 10, 7), 1, "nyse_close", BUF) == D(2025, 11, 6)
    # Monday evening: last complete = Mon; lag 1 -> Fri
    assert expected_latest_date(bond, et(2025, 11, 10, 19), 1, "nyse_close", BUF) == D(2025, 11, 7)


def test_treasury_after_veterans_day() -> None:
    bond = get_calendar("us_bond")
    # Wed 2025-11-12 evening: bond sessions ... Mon 10, (Tue 11 closed), Wed 12. lag 1 -> Mon 10.
    assert expected_latest_date(bond, et(2025, 11, 12, 19), 1, "nyse_close", BUF) == D(2025, 11, 10)
    # On Veterans Day evening itself the latest expected is Fri 7 (Mon 10 minus 1), not Mon.
    assert expected_latest_date(bond, et(2025, 11, 11, 19), 1, "nyse_close", BUF) == D(2025, 11, 7)


def test_equities_after_thanksgiving() -> None:
    nyse = get_calendar("nyse")
    # Thanksgiving evening 2025-11-27: last NYSE session = Wed 26
    assert expected_latest_date(nyse, et(2025, 11, 27, 19), 0, "nyse_close", BUF) == D(2025, 11, 26)


def test_fx_monday_morning_expects_friday() -> None:
    fx = get_calendar("fx_weekday")
    assert expected_latest_date(fx, et(2025, 11, 10, 7), 0, "next_ny_day", BUF) == D(2025, 11, 7)
    # Monday evening: Monday's FX bar is not final yet -> still Friday
    assert expected_latest_date(fx, et(2025, 11, 10, 19), 0, "next_ny_day", BUF) == D(2025, 11, 7)


def test_ecb_after_easter() -> None:
    ecb = get_calendar("ecb_target")
    # Tuesday after Easter 2025 (Apr 22) morning ET 07:00 = 13:00 Berlin, before publish
    # -> last complete = Thu Apr 17 (Fri 18 and Mon 21 are holidays)
    assert expected_latest_date(ecb, et(2025, 4, 22, 7), 0, "ecb_publish", BUF) == D(2025, 4, 17)


def test_wti_lag_across_holiday() -> None:
    nyse = get_calendar("nyse")
    # Tue 2025-12-02 evening, lag 3 -> sessions Dec 2, Dec 1, Nov 28, Nov 26 -> Nov 26
    assert expected_latest_date(nyse, et(2025, 12, 2, 19), 3, "nyse_close", BUF) == D(2025, 11, 26)


def test_business_days_behind_skips_holidays() -> None:
    bond = get_calendar("us_bond")
    # Latest Fri Nov 7, expected Wed Nov 12: Mon 10 and Wed 12 are sessions -> 2 behind
    assert business_days_behind(bond, D(2025, 11, 7), D(2025, 11, 12)) == 2
    assert business_days_behind(bond, D(2025, 11, 12), D(2025, 11, 12)) == 0
