from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class FinancialReportEvent:
    kind: str
    fiscal_year: int
    quarter: int
    period: str
    label: str
    general_deadline: date
    financial_deadline: date | None = None


def today_taipei() -> date:
    from datetime import datetime

    return datetime.now(TAIPEI_TZ).date()


def latest_monthly_revenue_period(today: date) -> str:
    year = today.year
    month = today.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year}-{month:02d}"


def active_financial_report_event(today: date) -> FinancialReportEvent | None:
    """Return the current filing window that should drive announced/pending grouping."""

    year = today.year
    windows = [
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
            general_deadline=date(year, 8, 31),
        ),
        FinancialReportEvent(
            kind="quarterly",
            fiscal_year=year,
            quarter=3,
            period=f"{year}Q3",
            label=f"{year} 第 3 季季報",
            general_deadline=date(year, 11, 14),
        ),
    ]
    starts = [date(year, 1, 1), date(year, 4, 1), date(year, 7, 1), date(year, 10, 1)]
    for start, event in zip(starts, windows, strict=False):
        deadline = event.financial_deadline or event.general_deadline
        if start <= today <= deadline:
            return event
    return None


def filing_context(today: date | None = None) -> dict:
    target_date = today or today_taipei()
    event = active_financial_report_event(target_date)
    context = {
        "asOfDate": target_date.isoformat(),
        "monthlyRevenuePeriod": latest_monthly_revenue_period(target_date),
        "activeFinancialReport": None,
    }
    if event:
        context["activeFinancialReport"] = {
            "kind": event.kind,
            "fiscalYear": event.fiscal_year,
            "quarter": event.quarter,
            "period": event.period,
            "label": event.label,
            "generalDeadline": event.general_deadline.isoformat(),
            "financialDeadline": event.financial_deadline.isoformat() if event.financial_deadline else None,
        }
    return context
