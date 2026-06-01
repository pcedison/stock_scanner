"""Branch coverage for the wake-up scheduler's invalid-deadline guard."""

from datetime import date

from backend.services import scheduler
from backend.services.scheduler import should_wake_up


def test_should_wake_up_skips_invalid_calendar_deadlines(monkeypatch):
    # A deadline tuple that is not a valid date for the year must be skipped,
    # not raise, when building the wake-up decision.
    monkeypatch.setattr(scheduler, "FINANCIAL_REPORT_DEADLINES", frozenset({(2, 30)}))
    decision = should_wake_up(date(2026, 6, 1))
    assert decision.status in {"WAKE", "SLEEP"}  # completed without raising
    assert "FINANCIAL_REPORT_WINDOW" not in decision.events
