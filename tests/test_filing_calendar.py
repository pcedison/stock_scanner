from datetime import date

from backend.services.filing_calendar import active_financial_report_event, filing_context, latest_monthly_revenue_period


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


def test_latest_monthly_revenue_period_rolls_back_year():
    assert latest_monthly_revenue_period(date(2026, 1, 8)) == "2025-12"
