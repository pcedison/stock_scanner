"""The Worker and the seed builder must agree on trading-day awareness.

`cloudflare/worker_trading_calendar.py` duplicates `is_trading_day` and `refresh_reason`
from the backend because the Worker cannot import `backend`. Duplication is only safe
while something pins the two together, which is what this file does.
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


def test_weekends_are_never_trading_days():
    for impl in (backend_policy, worker_calendar):
        assert impl.is_trading_day(FRIDAY)
        assert not impl.is_trading_day(SATURDAY)
        assert not impl.is_trading_day(SUNDAY)
        assert impl.is_trading_day(MONDAY)
        assert not impl.is_trading_day(MONDAY, {MONDAY})


@pytest.mark.parametrize("values", [None, "not-a-list", [], ["nonsense"], [None]])
def test_a_missing_or_broken_closed_date_list_degrades_to_weekend_only(values):
    # The Worker reads this straight from the manifest; a bad value must not throw, and
    # weekend-only still removes most non-trading days.
    assert worker_calendar.parse_closed_dates(values) == set()


def test_closed_dates_are_read_from_iso_strings():
    parsed = worker_calendar.parse_closed_dates(["2026-02-16", "2026-02-17T00:00:00+08:00"])

    assert parsed == {date(2026, 2, 16), date(2026, 2, 17)}


def test_refresh_reason_matches_the_backend_filing_windows_every_day():
    # The Worker labels its policy with the same filing windows the seed builder uses.
    from backend.services.filing_calendar import market_closed_dates

    for year in (2026, 2027):
        closed = set(market_closed_dates(year))
        day = date(year, 1, 1)
        while day.year == year:
            backend_reason = backend_policy.refresh_policy(_taipei(day, 12))["reason"]
            assert worker_calendar.refresh_reason(day, closed) == backend_reason, day
            day += timedelta(days=1)


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"generatedAt": "2026-09-15T09:40:00+00:00", "nextRefreshAfter": "2026-09-16T09:30:00+00:00"},
         datetime(2026, 9, 16, 9, 30, tzinfo=UTC)),
        ({"generatedAt": "2026-09-15T09:40:00+00:00"}, None),  # legacy manifest
        ({"generatedAt": "2026-09-15T09:40:00+00:00", "nextRefreshAfter": "garbage"}, None),
        # Not after the build, or implausibly far out: ignored rather than trusted.
        ({"generatedAt": "2026-09-15T09:40:00+00:00", "nextRefreshAfter": "2026-09-15T09:00:00+00:00"}, None),
        ({"generatedAt": "2026-09-15T09:40:00+00:00", "nextRefreshAfter": "2026-10-15T09:30:00+00:00"}, None),
    ],
)
def test_manifest_next_refresh_is_trusted_only_when_plausible(manifest, expected):
    generated = datetime.fromisoformat(manifest["generatedAt"])
    assert worker_calendar.manifest_next_refresh(manifest, generated) == expected
