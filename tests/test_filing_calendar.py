from datetime import date

import pytest

from backend.services.filing_calendar import (
    active_financial_report_event,
    filing_context,
    latest_monthly_revenue_period,
    today_taipei,
)

# Full-year map of representative dates -> the active filing period, or None when
# the date falls in a between-windows gap. Windows (inclusive): annual/Q4
# Jan1-Mar31, Q1 Apr1-May30, Q2 Jul1-Aug31, Q3 Oct1-Nov14. The day after each
# deadline opens a gap until the next window's start. This guard exists because
# crossing a boundary (e.g. the day after the 30-May Q1 deadline) previously
# broke date-fragile tests; see the gap entries below.
CALENDAR_SWEEP_2026 = [
    (date(2026, 1, 1), "2025Q4"),    # annual window start
    (date(2026, 2, 15), "2025Q4"),
    (date(2026, 3, 31), "2025Q4"),   # annual deadline (inclusive)
    (date(2026, 4, 1), "2026Q1"),    # Q1 start (contiguous with annual)
    (date(2026, 5, 15), "2026Q1"),   # Q1 general deadline
    (date(2026, 5, 30), "2026Q1"),   # Q1 financial deadline (inclusive)
    (date(2026, 5, 31), None),       # gap begins (the 2026-05-31 regression date)
    (date(2026, 6, 30), None),       # gap
    (date(2026, 7, 1), "2026Q2"),    # Q2 start
    (date(2026, 8, 31), "2026Q2"),   # Q2 deadline (inclusive)
    (date(2026, 9, 1), None),        # gap
    (date(2026, 9, 30), None),       # gap
    (date(2026, 10, 1), "2026Q3"),   # Q3 start
    (date(2026, 11, 14), "2026Q3"),  # Q3 deadline (inclusive)
    (date(2026, 11, 15), None),      # gap
    (date(2026, 12, 31), None),      # gap until next year's annual window
]


def test_filing_context_identifies_q1_window():
    context = filing_context(date(2026, 5, 14))

    assert context["monthlyRevenuePeriod"] == "2026-04"
    assert context["activeFinancialReport"]["period"] == "2026Q1"
    assert context["activeFinancialReport"]["generalDeadline"] == "2026-05-15"
    assert context["activeFinancialReport"]["financialDeadline"] == "2026-05-30"


def test_filing_context_identifies_annual_window():
    event = active_financial_report_event(date(2026, 3, 31))

    assert event is not None
    assert event.kind == "annual"
    assert event.period == "2025Q4"


@pytest.mark.parametrize(("today", "expected_period"), CALENDAR_SWEEP_2026)
def test_active_financial_report_event_across_calendar(today, expected_period):
    event = active_financial_report_event(today)
    if expected_period is None:
        assert event is None, f"{today} should be a between-windows gap"
    else:
        assert event is not None, f"{today} should be inside the {expected_period} window"
        assert event.period == expected_period


@pytest.mark.parametrize(("today", "expected_period"), CALENDAR_SWEEP_2026)
def test_filing_context_mirrors_active_event_and_always_has_revenue_period(today, expected_period):
    context = filing_context(today)
    # monthlyRevenuePeriod is independent of the filing window and must always be set.
    assert context["monthlyRevenuePeriod"] == latest_monthly_revenue_period(today)
    if expected_period is None:
        assert context["activeFinancialReport"] is None
    else:
        assert context["activeFinancialReport"]["period"] == expected_period


@pytest.mark.parametrize(
    ("deadline", "next_start"),
    [
        (date(2026, 3, 31), date(2026, 4, 1)),   # annual -> Q1 (contiguous, no gap)
        (date(2026, 5, 30), date(2026, 5, 31)),  # Q1 -> gap
        (date(2026, 8, 31), date(2026, 9, 1)),   # Q2 -> gap
        (date(2026, 11, 14), date(2026, 11, 15)),  # Q3 -> gap
    ],
)
def test_filing_window_deadline_is_inclusive_and_day_after_advances(deadline, next_start):
    on_deadline = active_financial_report_event(deadline)
    day_after = active_financial_report_event(next_start)
    assert on_deadline is not None
    # The day after a deadline either opens a gap (None) or rolls into the next
    # window, but never stays in the just-closed one.
    assert day_after is None or day_after.period != on_deadline.period


def test_latest_monthly_revenue_period_rolls_back_year():
    assert latest_monthly_revenue_period(date(2026, 1, 8)) == "2025-12"


def test_latest_monthly_revenue_period_is_previous_month():
    assert latest_monthly_revenue_period(date(2026, 6, 15)) == "2026-05"


def test_today_taipei_returns_a_date():
    # The single live-clock reader; everything else takes an injectable `today`.
    assert isinstance(today_taipei(), date)
