from datetime import date

import pytest

from backend.services.filing_calendar import (
    active_financial_report_event,
    filing_context,
    latest_monthly_revenue_period,
    next_business_day,
    today_taipei,
)

# Full-year map of representative dates -> (active filing period, is_gap). Windows
# (inclusive) end at the final (financial-industry) deadline moved to the next business
# day: annual/Q4 Jan1-Mar31, Q1 Apr1-May30 (Sat -> Jun 1), Q2 Jul1-Aug31,
# Q3 Oct1-Nov29 (Sun -> Nov 30). Between windows there is a gap; in a gap the calendar
# falls back to the most recently closed window (the last published quarter) so the
# announced/pending freshness gate stays anchored instead of going unset. `is_gap`
# marks dates resolved via the fallback.
CALENDAR_SWEEP_2026 = [
    (date(2026, 1, 1), "2025Q4", False),    # annual window start
    (date(2026, 2, 15), "2025Q4", False),
    (date(2026, 3, 31), "2025Q4", False),   # annual deadline (inclusive)
    (date(2026, 4, 1), "2026Q1", False),    # Q1 start (contiguous with annual)
    (date(2026, 5, 15), "2026Q1", False),   # Q1 general deadline
    (date(2026, 5, 30), "2026Q1", False),   # Q1 financial deadline, a Saturday
    (date(2026, 5, 31), "2026Q1", False),   # ... extended to Monday Jun 1
    (date(2026, 6, 1), "2026Q1", False),    # extended financial deadline (inclusive)
    (date(2026, 6, 2), "2026Q1", True),     # gap -> last published Q1
    (date(2026, 6, 30), "2026Q1", True),    # gap -> Q1
    (date(2026, 7, 1), "2026Q2", False),    # Q2 start
    (date(2026, 8, 14), "2026Q2", False),   # Q2 general deadline
    (date(2026, 8, 31), "2026Q2", False),   # Q2 financial/KY deadline (inclusive)
    (date(2026, 9, 1), "2026Q2", True),     # gap -> Q2
    (date(2026, 9, 30), "2026Q2", True),    # gap -> Q2
    (date(2026, 10, 1), "2026Q3", False),   # Q3 start
    (date(2026, 11, 14), "2026Q3", False),  # Q3 general deadline
    (date(2026, 11, 29), "2026Q3", False),  # Q3 financial deadline, a Sunday
    (date(2026, 11, 30), "2026Q3", False),  # ... extended to Monday (inclusive)
    (date(2026, 12, 1), "2026Q3", True),    # gap -> Q3
    (date(2026, 12, 31), "2026Q3", True),   # gap -> Q3 until next year's annual window
]


def test_statutory_deadlines_by_industry():
    q2 = active_financial_report_event(date(2026, 8, 1))
    q3 = active_financial_report_event(date(2026, 11, 1))

    assert q2 is not None and q3 is not None
    assert (q2.general_deadline, q2.financial_deadline) == (date(2026, 8, 14), date(2026, 8, 31))
    assert (q3.general_deadline, q3.financial_deadline) == (date(2026, 11, 14), date(2026, 11, 29))
    assert q3.final_deadline == date(2026, 11, 30)  # Sunday -> next business day


def test_next_business_day_skips_weekends_and_market_holidays():
    assert next_business_day(date(2026, 8, 14)) == date(2026, 8, 14)
    assert next_business_day(date(2026, 10, 10)) == date(2026, 10, 12)  # Sat -> Mon
    assert next_business_day(date(2026, 9, 25)) == date(2026, 9, 29)  # holiday Fri, weekend, holiday Mon


def test_freshness_waits_for_the_q3_financial_deadline():
    assert filing_context(date(2026, 11, 20))["freshnessFinancialReport"]["period"] == "2026Q2"
    assert filing_context(date(2026, 12, 1))["freshnessFinancialReport"]["period"] == "2026Q3"


def test_monthly_revenue_lag_follows_the_extended_10th():
    # 2026-10-10 is a Saturday, so September revenue is due Monday Oct 12.
    assert latest_monthly_revenue_period(date(2026, 10, 12)) == "2026-08"
    assert latest_monthly_revenue_period(date(2026, 10, 13)) == "2026-09"


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


def test_filing_context_keeps_q2_active_but_freshness_targets_q1_before_deadline():
    context = filing_context(date(2026, 7, 1))

    assert context["activeFinancialReport"]["period"] == "2026Q2"
    assert context.get("freshnessFinancialReport") is not None
    assert context["freshnessFinancialReport"]["period"] == "2026Q1"


def test_filing_context_advances_freshness_after_q2_deadline():
    context = filing_context(date(2026, 9, 1))

    assert context["activeFinancialReport"]["period"] == "2026Q2"
    assert context.get("freshnessFinancialReport") is not None
    assert context["freshnessFinancialReport"]["period"] == "2026Q2"


@pytest.mark.parametrize(("today", "expected_period", "is_gap"), CALENDAR_SWEEP_2026)
def test_active_financial_report_event_across_calendar(today, expected_period, is_gap):
    event = active_financial_report_event(today)
    assert event is not None
    assert event.period == expected_period
    deadline = event.final_deadline
    if is_gap:
        # resolved via the fallback to the most recently closed window
        assert deadline < today
    else:
        assert today <= deadline  # inside an open filing window (deadline inclusive)


@pytest.mark.parametrize(("today", "expected_period", "is_gap"), CALENDAR_SWEEP_2026)
def test_filing_context_mirrors_active_event_and_always_has_revenue_period(today, expected_period, is_gap):
    context = filing_context(today)
    # monthlyRevenuePeriod is independent of the filing window and must always be set.
    assert context["monthlyRevenuePeriod"] == latest_monthly_revenue_period(today)
    # activeFinancialReport is always anchored now — to the open window, or in a
    # gap to the most recently published quarter.
    assert context["activeFinancialReport"]["period"] == expected_period


def test_latest_monthly_revenue_period_rolls_back_year():
    # 1/8 在每月 10 日前:12 月營收尚未公布(截止 1/10),故取再往前的 11 月。
    assert latest_monthly_revenue_period(date(2026, 1, 8)) == "2025-11"


def test_latest_monthly_revenue_period_is_previous_month():
    # 6/15 已過 10 日:5 月營收(截止 6/10)已公布,取上個月。
    assert latest_monthly_revenue_period(date(2026, 6, 15)) == "2026-05"


def test_latest_monthly_revenue_period_accounts_for_filing_lag():
    # 申報時差核心案例:每月 1~10 日取「兩個月前」,11 日起取「上個月」。
    assert latest_monthly_revenue_period(date(2026, 6, 1)) == "2026-04"   # 5 月營收尚未公布
    assert latest_monthly_revenue_period(date(2026, 6, 10)) == "2026-04"  # 截止當日仍保守取 4 月
    assert latest_monthly_revenue_period(date(2026, 6, 11)) == "2026-05"  # 隔日才取 5 月
    # 跨年 + 兩個月前需正確 rollover
    assert latest_monthly_revenue_period(date(2026, 2, 5)) == "2025-12"


def test_today_taipei_returns_a_date():
    # The single live-clock reader; everything else takes an injectable `today`.
    assert isinstance(today_taipei(), date)
