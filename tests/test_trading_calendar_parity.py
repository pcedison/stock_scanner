"""The Worker and the seed builder must agree on when new data can exist.

`cloudflare/worker_trading_calendar.py` duplicates
`backend/services/cache_policy.next_publication_time` because the Worker cannot import
`backend`. Duplication is only safe while something pins the two together, which is what
this file does: every case runs through both and the answers must match.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.services import cache_policy as backend_policy
from cloudflare import worker_trading_calendar as worker_calendar

TAIPEI = ZoneInfo("Asia/Taipei")
# 2026-09-11 is a Friday, so 12th/13th are the weekend and the 14th is a Monday.
FRIDAY = date(2026, 9, 11)
SATURDAY = date(2026, 9, 12)
SUNDAY = date(2026, 9, 13)
MONDAY = date(2026, 9, 14)


def _taipei(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TAIPEI)


@pytest.mark.parametrize(
    "candidate",
    [
        _taipei(FRIDAY, 23, 0),
        _taipei(SATURDAY, 0, 1),
        _taipei(SATURDAY, 10, 7),
        _taipei(SUNDAY, 20, 0),
        _taipei(MONDAY, 8, 30),
        _taipei(MONDAY, 15, 0),
        _taipei(FRIDAY, 9, 0).astimezone(UTC),
        _taipei(SATURDAY, 7, 7).astimezone(UTC),
    ],
)
def test_both_implementations_agree(candidate):
    closed = {date(2026, 2, 16), date(2026, 2, 17)}
    assert backend_policy.next_publication_time(candidate, closed) == worker_calendar.next_publication_time(
        candidate, closed
    )


def test_a_weekend_deadline_waits_for_monday_afternoon():
    # The concrete production case: a seed built late Friday UTC lands on Saturday in
    # Taipei, and used to be called stale every three hours all weekend.
    saturday_morning = _taipei(SATURDAY, 7, 7)

    for impl in (backend_policy, worker_calendar):
        moved = impl.next_publication_time(saturday_morning).astimezone(TAIPEI)
        assert moved == _taipei(MONDAY, 15, 0)


def test_a_weekday_deadline_is_left_alone():
    # Intraday cadence during the revenue and financial windows must not be delayed.
    midday = _taipei(MONDAY, 13, 0)

    for impl in (backend_policy, worker_calendar):
        assert impl.next_publication_time(midday) == midday


def test_a_holiday_run_waits_for_the_next_open_day():
    # 2026-02-16 and 02-17 are TWSE Spring Festival closures in data/market_calendar_2026.json.
    closed = {date(2026, 2, 16), date(2026, 2, 17)}
    saturday_of_the_break = _taipei(date(2026, 2, 14), 9, 0)

    for impl in (backend_policy, worker_calendar):
        moved = impl.next_publication_time(saturday_of_the_break, closed).astimezone(TAIPEI)
        assert moved == _taipei(date(2026, 2, 18), 15, 0)


def test_weekends_are_never_trading_days():
    for impl in (backend_policy, worker_calendar):
        assert impl.is_trading_day(FRIDAY)
        assert not impl.is_trading_day(SATURDAY)
        assert not impl.is_trading_day(SUNDAY)
        assert impl.is_trading_day(MONDAY)
        assert not impl.is_trading_day(MONDAY, {MONDAY})


def test_the_publish_hour_is_after_the_close():
    # The market closes at 13:30; rebuilding before the official post-close files land
    # would capture an incomplete day.
    for impl in (backend_policy, worker_calendar):
        assert impl.TRADING_DAY_PUBLISH_HOUR >= 14


@pytest.mark.parametrize("values", [None, "not-a-list", [], ["nonsense"], [None]])
def test_a_missing_or_broken_closed_date_list_degrades_to_weekend_only(values):
    # The Worker reads this straight from the manifest; a bad value must not throw, and
    # weekend-only still removes most non-trading days.
    assert worker_calendar.parse_closed_dates(values) == set()
    assert worker_calendar.next_publication_time(
        _taipei(SATURDAY, 10, 0), worker_calendar.parse_closed_dates(values)
    ).astimezone(TAIPEI) == _taipei(MONDAY, 15, 0)


def test_closed_dates_are_read_from_iso_strings():
    parsed = worker_calendar.parse_closed_dates(["2026-02-16", "2026-02-17T00:00:00+08:00"])

    assert parsed == {date(2026, 2, 16), date(2026, 2, 17)}


def test_the_walk_forward_crosses_a_year_boundary():
    # 1 Jan is always closed; the seed builder ships next year's dates for exactly this.
    closed = {date(2027, 1, 1)}
    new_years_eve = _taipei(date(2026, 12, 31), 23, 0) + timedelta(days=1)

    for impl in (backend_policy, worker_calendar):
        moved = impl.next_publication_time(new_years_eve, closed).astimezone(TAIPEI)
        assert moved == _taipei(date(2027, 1, 4), 15, 0)
