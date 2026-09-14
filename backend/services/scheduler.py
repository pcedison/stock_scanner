from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from backend.services.cache_policy import (
    ANNUAL_WINDOW_LEAD_DAYS,
    FINANCIAL_WINDOW_LEAD_DAYS,
    in_monthly_revenue_window,
)
from backend.services.calendar import load_market_calendar
from backend.services.filing_calendar import financial_report_events


@dataclass(frozen=True)
class WakeUpDecision:
    status: str
    events: list[str]
    today: str
    nextTradingDay: str
    calendarSource: str


def _financial_window_event(today: date) -> str | None:
    for event in financial_report_events(today.year):
        lead = ANNUAL_WINDOW_LEAD_DAYS if event.kind == "annual" else FINANCIAL_WINDOW_LEAD_DAYS
        if event.general_deadline - timedelta(days=lead) <= today <= event.final_deadline:
            return "ANNUAL_REPORT_WINDOW" if event.kind == "annual" else f"Q{event.quarter}_REPORT_WINDOW"
    return None


def should_wake_up(today: date | None = None) -> WakeUpDecision:
    today = today or date.today()
    calendar = load_market_calendar(today.year)
    events: list[str] = []

    if in_monthly_revenue_window(today):
        events.append("MONTHLY_REVENUE_WINDOW")

    if financial_event := _financial_window_event(today):
        events.append(financial_event)

    if any(d.month == today.month for d in calendar.spring_festival_dates):
        events.append("SPRING_FESTIVAL_GUARD")

    status = "WAKE" if events else "SLEEP"
    return WakeUpDecision(
        status=status,
        events=events,
        today=today.isoformat(),
        nextTradingDay=calendar.next_trading_day(today).isoformat(),
        calendarSource=calendar.source,
    )
