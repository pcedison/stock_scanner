from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from backend.services.cache_policy import (
    EVENING_SLOT,
    MORNING_SLOT,
    active_publication_slots,
    in_financial_window,
    in_monthly_revenue_window,
    next_refresh_after,
    refresh_policy,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def _at(month: int, day: int, hour: int = 10, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TAIPEI)


def test_monthly_revenue_window_runs_from_the_1st_to_the_extended_10th():
    assert in_monthly_revenue_window(date(2026, 9, 1)) is True
    assert in_monthly_revenue_window(date(2026, 9, 10)) is True
    assert in_monthly_revenue_window(date(2026, 9, 11)) is False
    # 2026-10-10 is a Saturday: September revenue is due Monday 10/12.
    assert in_monthly_revenue_window(date(2026, 10, 12)) is True
    assert in_monthly_revenue_window(date(2026, 10, 13)) is False


def test_financial_window_covers_the_two_weeks_before_the_general_deadline_until_the_final_one():
    assert in_financial_window(date(2026, 7, 30)) is False
    assert in_financial_window(date(2026, 7, 31)) is True  # Q2 general deadline 8/14 - 14 days
    assert in_financial_window(date(2026, 8, 31)) is True  # Q2 financial/KY deadline
    assert in_financial_window(date(2026, 9, 1)) is False
    assert in_financial_window(date(2026, 5, 18)) is True  # between Q1 general and financial deadlines
    assert in_financial_window(date(2026, 6, 1)) is True  # 5/30 Saturday -> Monday 6/1
    assert in_financial_window(date(2026, 6, 2)) is False
    assert in_financial_window(date(2026, 11, 30)) is True  # 11/29 Sunday -> Monday
    assert in_financial_window(date(2026, 2, 28)) is False
    assert in_financial_window(date(2026, 3, 1)) is True  # large caps file the annual report by ~3/16


def test_refresh_policy_reason_and_cooldown():
    assert refresh_policy(_at(8, 10))["reason"] == "financial_report_window"
    assert refresh_policy(_at(9, 3))["reason"] == "monthly_revenue_window"
    assert refresh_policy(_at(9, 20))["reason"] == "routine_refresh"
    for moment in (_at(8, 10), _at(9, 3), _at(9, 20)):
        policy = refresh_policy(moment)
        assert policy["strategy"] == "stale_while_revalidate"
        assert policy["schedule"] == "publication_slots"
        # Cooldown between terminal jobs only; freshness itself follows the publication slots.
        assert policy["minIntervalSeconds"] == 3600


def test_active_slots_follow_trading_days_and_filing_windows():
    assert active_publication_slots(date(2026, 9, 21)) == [EVENING_SLOT]  # routine Monday
    assert active_publication_slots(date(2026, 10, 2)) == [MORNING_SLOT, EVENING_SLOT]  # revenue window
    assert active_publication_slots(date(2026, 9, 19)) == []  # Saturday
    assert active_publication_slots(date(2026, 9, 25), frozenset({date(2026, 9, 25)})) == []  # holiday


def test_next_refresh_waits_for_the_evening_publication_on_routine_days():
    assert next_refresh_after(_at(9, 15, 10)) == _at(9, 15, 18)
    assert next_refresh_after(_at(9, 15, 18, 10)) == _at(9, 16, 18)


def test_next_refresh_skips_the_weekend_and_market_holidays():
    assert next_refresh_after(_at(9, 18, 18, 15)) == _at(9, 21, 18)  # Fri -> Mon
    closed = frozenset({date(2026, 9, 25), date(2026, 9, 28)})
    assert next_refresh_after(_at(9, 24, 18, 15), closed) == _at(9, 29, 18)


def test_next_refresh_adds_the_morning_batch_inside_filing_windows():
    assert next_refresh_after(_at(10, 1, 18, 15)) == _at(10, 2, 6, 30)
    assert next_refresh_after(_at(10, 2, 7, 0)) == _at(10, 2, 18)
    assert next_refresh_after(_at(10, 2, 18, 15)) == _at(10, 5, 6, 30)  # Fri evening -> Mon morning


def test_next_refresh_never_lands_on_new_years_day_before_the_schedule_is_published():
    # data/market_calendar_2027.json does not exist yet (TWSE publishes it late in the year);
    # the fallback still closes 1/1, so the walk goes to Monday 1/4 (revenue window: morning slot).
    from backend.services.filing_calendar import market_closed_dates

    assert next_refresh_after(_at(12, 31, 20), market_closed_dates(2026)) == _at(1, 4, 6, 30, year=2027)


def test_next_refresh_keeps_the_callers_timezone():
    utc_build = datetime(2026, 9, 15, 2, 0, tzinfo=ZoneInfo("UTC"))
    result = next_refresh_after(utc_build)
    assert result.tzinfo == utc_build.tzinfo
    assert result == _at(9, 15, 18)
