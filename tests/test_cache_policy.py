from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from backend.services.cache_policy import (
    FINANCIAL_REPORT_DEADLINES,
    MONTHLY_REVENUE_WINDOW_END_DAY,
    in_financial_window,
    refresh_policy,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def _dt(month: int, day: int, year: int = 2026) -> datetime:
    return datetime(year, month, day, 10, 0, tzinfo=TAIPEI)


def test_financial_deadlines_are_defined():
    assert (5, 15) in FINANCIAL_REPORT_DEADLINES
    assert (8, 31) in FINANCIAL_REPORT_DEADLINES
    assert len(FINANCIAL_REPORT_DEADLINES) == 5


def test_in_financial_window_on_deadline():
    assert in_financial_window(_dt(3, 31)) is True
    assert in_financial_window(_dt(5, 15)) is True
    assert in_financial_window(_dt(5, 30)) is True
    assert in_financial_window(_dt(8, 31)) is True
    assert in_financial_window(_dt(11, 14)) is True


def test_in_financial_window_within_3_days():
    assert in_financial_window(_dt(5, 13)) is True  # 2 days before 5/15
    assert in_financial_window(_dt(5, 18)) is True  # 3 days after 5/15
    assert in_financial_window(_dt(5, 19)) is False  # 4 days after 5/15


def test_in_financial_window_routine_day():
    assert in_financial_window(_dt(7, 1)) is False
    assert in_financial_window(_dt(4, 1)) is False


def test_refresh_policy_financial_window():
    policy = refresh_policy(_dt(5, 15))
    assert policy["reason"] == "financial_report_window"
    assert policy["minIntervalSeconds"] == 7200


def test_refresh_policy_monthly_revenue_window():
    policy = refresh_policy(_dt(3, 10))  # day 10 in 8-15 range
    assert policy["reason"] == "monthly_revenue_window"
    assert policy["minIntervalSeconds"] == 10800


def test_monthly_revenue_window_keeps_late_filing_followup_days():
    assert MONTHLY_REVENUE_WINDOW_END_DAY == 15
    assert refresh_policy(_dt(7, 15))["reason"] == "monthly_revenue_window"


def test_refresh_policy_routine():
    policy = refresh_policy(_dt(7, 20))
    assert policy["reason"] == "routine_refresh"
    assert policy["minIntervalSeconds"] == 43200


def test_refresh_policy_financial_takes_priority_over_revenue_window():
    # 3/31 is a financial deadline; if it falls within day 8-12 window we still get financial
    policy = refresh_policy(_dt(3, 31))
    assert policy["reason"] == "financial_report_window"


def test_refresh_policy_strategy_is_stale_while_revalidate():
    for day in (15, 10, 20):
        policy = refresh_policy(_dt(5, day))
        assert policy["strategy"] == "stale_while_revalidate"
