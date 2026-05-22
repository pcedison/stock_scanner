from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from backend.services.cache_policy import FINANCIAL_REPORT_DEADLINES, MONTHLY_REVENUE_WINDOW_END_DAY, MONTHLY_REVENUE_WINDOW_START_DAY
from backend.services.calendar import load_market_calendar

_FINANCIAL_WINDOW_DAYS = 3

_DEADLINE_EVENT: dict[tuple[int, int], str] = {
    (3, 31): "ANNUAL_REPORT_WINDOW",
    (5, 15): "Q1_REPORT_WINDOW",
    (5, 30): "Q1_REPORT_WINDOW",
    (8, 31): "Q2_REPORT_WINDOW",
    (11, 14): "Q3_REPORT_WINDOW",
}


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

    for month, day in FINANCIAL_REPORT_DEADLINES:
        try:
            deadline = date(today.year, month, day)
        except ValueError:
            continue
        if abs((today - deadline).days) <= _FINANCIAL_WINDOW_DAYS:
            label = _DEADLINE_EVENT.get((month, day), "FINANCIAL_REPORT_WINDOW")
            if label not in events:
                events.append(label)

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
