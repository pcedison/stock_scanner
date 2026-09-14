from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.calendar import load_market_calendar

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 證券交易法 §36: monthly revenue by the 10th of the following month (insurers may use the
# 15th). A statutory deadline on a non-business day moves to the next business day.
MONTHLY_REVENUE_DEADLINE_DAY = 10


@lru_cache(maxsize=8)
def _closed_dates(year: int) -> frozenset[date]:
    return frozenset(load_market_calendar(year).closed_dates)


def market_closed_dates(year: int) -> frozenset[date]:
    """TWSE closed days for ``year`` and the next, so a walk forward can cross 31 Dec."""
    return _closed_dates(year) | _closed_dates(year + 1)


def next_business_day(day: date) -> date:
    """``day`` itself when the market is open, else the next open day (weekends + TWSE holidays)."""
    while day.weekday() >= 5 or day in _closed_dates(day.year):
        day += timedelta(days=1)
    return day


@dataclass(frozen=True)
class FinancialReportEvent:
    kind: str
    fiscal_year: int
    quarter: int
    period: str
    label: str
    general_deadline: date
    financial_deadline: date | None = None

    @property
    def final_deadline(self) -> date:
        """Last day any listed company may file this period, after holiday extension."""
        return next_business_day(self.financial_deadline or self.general_deadline)


def today_taipei() -> date:
    from datetime import datetime

    return datetime.now(TAIPEI_TZ).date()


def monthly_revenue_deadline(year: int, month: int) -> date:
    return next_business_day(date(year, month, MONTHLY_REVENUE_DEADLINE_DAY))


def latest_monthly_revenue_period(today: date) -> str:
    # 台股月營收採「每月 10 日前公布上月營收」(遇假日順延)。截止日(含)前,上個月的營收
    # 尚未(完整)公布,若直接取上個月會抓到還沒公布的期別,使營收年增率(E3 等)
    # 全市場缺值。故截止日(含)前往前推兩個月,之後才取上個月。
    months_back = 2 if today <= monthly_revenue_deadline(today.year, today.month) else 1
    year = today.year
    month = today.month - months_back
    while month <= 0:
        year -= 1
        month += 12
    return f"{year}-{month:02d}"


def financial_report_events(year: int) -> list[FinancialReportEvent]:
    """Statutory filing deadlines for reports filed during ``year``, chronological.

    General listed/OTC companies: annual 3/31, Q1 5/15, Q2 8/14, Q3 11/14 (§36: 45 days
    after each quarter). Financial holding companies, banks and insurers: Q1 5/30,
    Q2 8/31, Q3 11/29; primary-listed foreign (KY) companies also file Q2 by 8/31.
    """
    return [
        FinancialReportEvent(
            kind="annual",
            fiscal_year=year - 1,
            quarter=4,
            period=f"{year - 1}Q4",
            label=f"{year - 1} 年度財報",
            general_deadline=date(year, 3, 31),
        ),
        FinancialReportEvent(
            kind="quarterly",
            fiscal_year=year,
            quarter=1,
            period=f"{year}Q1",
            label=f"{year} 第 1 季季報",
            general_deadline=date(year, 5, 15),
            financial_deadline=date(year, 5, 30),
        ),
        FinancialReportEvent(
            kind="quarterly",
            fiscal_year=year,
            quarter=2,
            period=f"{year}Q2",
            label=f"{year} 第 2 季半年報",
            general_deadline=date(year, 8, 14),
            financial_deadline=date(year, 8, 31),
        ),
        FinancialReportEvent(
            kind="quarterly",
            fiscal_year=year,
            quarter=3,
            period=f"{year}Q3",
            label=f"{year} 第 3 季季報",
            general_deadline=date(year, 11, 14),
            financial_deadline=date(year, 11, 29),
        ),
    ]


_WINDOW_STARTS = ((1, 1), (4, 1), (7, 1), (10, 1))


def active_financial_report_event(today: date) -> FinancialReportEvent | None:
    """Return the current filing window that should drive announced/pending grouping."""

    year = today.year
    most_recently_closed: FinancialReportEvent | None = None
    for (month, day), event in zip(_WINDOW_STARTS, financial_report_events(year), strict=True):
        deadline = event.final_deadline
        if date(year, month, day) <= today <= deadline:
            return event
        if deadline < today:
            # windows are chronological, so the last match is the most recent
            most_recently_closed = event
    # In a between-windows gap, fall back to the most recently closed window so the
    # last published quarter still drives the announced/pending freshness gate
    # instead of leaving it unset. (None only for the pre-first-window degenerate
    # case, which cannot occur within a calendar year.)
    return most_recently_closed


def _financial_report_payload(event: FinancialReportEvent) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "fiscalYear": event.fiscal_year,
        "quarter": event.quarter,
        "period": event.period,
        "label": event.label,
        "generalDeadline": event.general_deadline.isoformat(),
        "financialDeadline": event.financial_deadline.isoformat() if event.financial_deadline else None,
    }


def freshness_financial_report_event(today: date) -> FinancialReportEvent | None:
    """Return the latest report period whose filing deadline has fully passed."""

    latest_due: FinancialReportEvent | None = None
    for year in (today.year - 1, today.year):
        for event in financial_report_events(year):
            if event.final_deadline < today:
                latest_due = event
    return latest_due


def filing_context(today: date | None = None) -> dict:
    target_date = today or today_taipei()
    event = active_financial_report_event(target_date)
    freshness_event = freshness_financial_report_event(target_date)
    context: dict[str, Any] = {
        "asOfDate": target_date.isoformat(),
        "monthlyRevenuePeriod": latest_monthly_revenue_period(target_date),
        "activeFinancialReport": None,
        "freshnessFinancialReport": None,
    }
    if event:
        context["activeFinancialReport"] = _financial_report_payload(event)
    if freshness_event:
        context["freshnessFinancialReport"] = _financial_report_payload(freshness_event)
    return context
