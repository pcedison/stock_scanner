"""Wake-up windows follow the statutory filing calendar."""

from datetime import date

from backend.services.scheduler import should_wake_up


def test_q2_window_spans_the_general_and_financial_deadlines():
    assert should_wake_up(date(2026, 7, 30)).events == []
    assert "Q2_REPORT_WINDOW" in should_wake_up(date(2026, 8, 3)).events  # before the 8/14 general deadline
    assert "Q2_REPORT_WINDOW" in should_wake_up(date(2026, 8, 31)).events  # financial/KY deadline
    assert should_wake_up(date(2026, 9, 1)).events == ["MONTHLY_REVENUE_WINDOW"]


def test_q3_window_runs_to_the_extended_financial_deadline():
    assert "Q3_REPORT_WINDOW" in should_wake_up(date(2026, 11, 30)).events  # 11/29 Sunday -> Monday
    assert should_wake_up(date(2026, 12, 11)).status == "SLEEP"
