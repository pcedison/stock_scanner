from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from backend.services.cache_policy import MONTHLY_REVENUE_WINDOW_END_DAY, MONTHLY_REVENUE_WINDOW_START_DAY
from backend.services.calendar import load_market_calendar


@dataclass(frozen=True)
class WakeUpDecision:
    status: str
    events: list[str]
    today: str
    nextTradingDay: str
    calendarSource: str


def should_wake_up(today: Optional[date] = None) -> WakeUpDecision:
    today = today or date.today()
    calendar = load_market_calendar(today.year)
    events: list[str] = []

    if MONTHLY_REVENUE_WINDOW_START_DAY <= today.day <= MONTHLY_REVENUE_WINDOW_END_DAY:
        events.append("MONTHLY_REVENUE_WINDOW")
    if today.month == 3 and today.day >= 25:
        events.append("ANNUAL_REPORT_WINDOW")
    if today.month == 5 and 10 <= today.day <= 20:
        events.append("Q1_REPORT_WINDOW")
    if today.month == 8 and 10 <= today.day <= 20:
        events.append("Q2_REPORT_WINDOW")
    if today.month == 11 and 10 <= today.day <= 20:
        events.append("Q3_REPORT_WINDOW")
    if any(day.month == today.month for day in calendar.spring_festival_dates):
        events.append("SPRING_FESTIVAL_GUARD")

    status = "WAKE" if events else "SLEEP"
    return WakeUpDecision(
        status=status,
        events=events,
        today=today.isoformat(),
        nextTradingDay=calendar.next_trading_day(today).isoformat(),
        calendarSource=calendar.source,
    )
