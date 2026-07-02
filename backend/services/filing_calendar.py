from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
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
    # 台股月營收採「每月 10 日前公布上月營收」。因此每月 1~10 日時,上個月的營收
    # 尚未(完整)公布,若直接取上個月會抓到還沒公布的期別,使營收年增率(E3 等)
    # 全市場缺值。故 10 日(含)前往前推兩個月,11 日起才取上個月。
    months_back = 2 if today.day <= 10 else 1
    year = today.year
    month = today.month - months_back
    while month <= 0:
        year -= 1
        month += 12
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
    most_recently_closed: FinancialReportEvent | None = None
    for start, event in zip(starts, windows, strict=False):
        deadline = event.financial_deadline or event.general_deadline
        if start <= today <= deadline:
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
    latest_deadline: date | None = None
    for year in (today.year - 1, today.year):
        deadline_dates = (
            date(year, 3, 31),
            date(year, 5, 30),
            date(year, 8, 31),
            date(year, 11, 14),
        )
        for deadline_date in deadline_dates:
            event = active_financial_report_event(deadline_date)
            if not event:
                continue
            deadline = event.financial_deadline or event.general_deadline
            if deadline < today and (latest_deadline is None or latest_deadline < deadline):
                latest_due = event
                latest_deadline = deadline
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
